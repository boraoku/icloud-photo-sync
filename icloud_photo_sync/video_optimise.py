"""Deciding which videos are worth re-encoding, at what settings, and whether the
result may replace the original.

Pure policy: no ffmpeg, no ffprobe, no SQLite, no ``typer``, no network. Every
rule here is a function of a :class:`VideoProbe`, so the whole decision surface
is testable without a media file. The tool mechanics live in
:mod:`icloud_photo_sync.transcode`; the job store in
:mod:`icloud_photo_sync.optimise_job`.

The governing asymmetry is the same one :mod:`icloud_photo_sync.icloud_delete`
lives by, only sharper: a video this module declines to convert costs nothing at
all, while a video it converts badly and then swaps into iCloud costs the
original footage. So a clip has to earn its way past three separate gates:

* :func:`classify` — before any work. Is it big enough to matter, is there
  anything to downscale, and would the re-encode actually save space?
* :func:`accept_output` — after the encode, on the file that was produced.
  Did it come out smaller, and did it keep its colour? Both are *measurements*
  of the real output, not predictions, so neither can be wrong.
* :class:`Swap` — before the delete. Constructing one without a verified new
  asset id raises, so "delete the original" cannot be expressed for a video
  whose replacement is not known to exist.

Three rules deserve their reasons stated, because each of them silently
destroys footage if it is got wrong.

**Colour.** Washing out is a labelling failure, not a data-loss one: HLG's
transfer curve is far flatter than bt709 gamma, so a player that reads HLG
pixels as bt709 lifts the blacks and drains the saturation. The pixels were
fine. The label was wrong. :func:`colour_class` reduces a probe to the things
that must not change, and :func:`accept_output` compares the two.

**Bit depth follows the source, both ways.** Dropping a 10-bit HDR source to
8 bits bands the gradients; promoting an 8-bit source to 10 bits is just as
wrong, and measurably worse — it grew a 348 MiB test clip to 419 MiB.

**Frame rate is never touched above 60 fps.** Forcing 30 fps on a 240 fps clip
drops seven of every eight frames and the result plays back at normal speed:
the slow motion is destroyed, silently, and by then the original is gone. A
slow-motion clip already at the target resolution therefore has nothing this
module can do for it, and is skipped rather than re-encoded for a small saving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .logutil import get_logger

logger = get_logger(__name__)

# --- Targets -----------------------------------------------------------------

TARGET_SHORT_SIDE = 1080
"""The **shorter** side of the output, never a bounding box.

Fitting every clip into a 1920x1080 box turns a portrait 1080x1920 video into
608x1080 — a two-thirds loss of resolution — and portrait clips are the majority
of a phone library.
"""

TARGET_FPS = 30.0
SLOMO_FPS = 60.5
"""Above this the source is slow motion (or a high-rate capture) and its frame
rate is left alone. 60.5 rather than 60 so a nominal 60 fps clip is not caught
by a rounding artefact in the container's rate."""

BITRATE_HDR = 8_000_000     # bits/s at a full 1920x1080 30 fps, 10-bit source
BITRATE_SDR = 6_000_000     # bits/s at a full 1920x1080 30 fps, 8-bit source
AUDIO_BITRATE = 128_000

FULL_HD_PIXELS = 1920 * 1080
FPS_EXPONENT = 0.6
"""Bitrate scales sub-linearly with frame rate: successive frames of a 120 fps
capture are far more alike than successive frames at 30 fps, so doubling the
rate costs well under double the bits."""

VIDEOTOOLBOX_EFFICIENCY = 0.73
"""What ``hevc_videotoolbox`` actually delivers against a ``-b:v`` target.

Measured at 5.84 Mbps against an 8 Mbps request and 4.34 against 6, on four
different real clips — the same ratio each time. Used only to *predict* an
output size; nothing acts on the prediction without measuring the real file
afterwards. A test asserts it, so a future ffmpeg that changes the ratio fails
the suite rather than quietly drifting every projection.
"""

WORTH_IT_RATIO = 0.75
"""An output at or above this fraction of its source is not worth swapping."""

DEFAULT_MIN_BYTES = 20 * 1024 * 1024
"""Videos below this are skipped by default.

On a real 1,504-video library the 637 clips at or above 20 MiB held 90% of all
video bytes, and the 722 smaller ones would have added four hours of encoding
for 2.5 GiB.
"""

LIVE_PHOTO_MAX_SECONDS = 4.0

HDR_TRANSFERS = frozenset({"arib-std-b67", "smpte2084"})
"""HLG and PQ. Everything else is treated as SDR."""

TEN_BIT_PIX_FMTS = frozenset({
    "yuv420p10le", "yuv420p10be", "p010le", "p010be",
    "yuv422p10le", "yuv444p10le", "yuv420p12le", "p210le",
})

PROFILE_10BIT, PROFILE_8BIT = "main10", "main"
PIX_FMT_10BIT, PIX_FMT_8BIT = "p010le", "nv12"

# --- Why a video was left alone. Shown to the user verbatim. -----------------

SKIP_TOO_SMALL = "smaller than the size floor"
SKIP_UNPROBEABLE = "ffprobe could not read this file"
SKIP_NOTHING_TO_GAIN = "re-encoding would not save enough to be worth it"
SKIP_SLOMO_AT_TARGET = "slow motion already at the target resolution"
SKIP_LIVE_PHOTO = "looks like the video half of a Live Photo"

# Post-encode refusals — measurements of the file that was actually produced.
SKIP_NOT_SMALLER = "the converted file was not enough smaller"
SKIP_COLOUR_MISMATCH = "the converted file did not keep its colour"


@dataclass(frozen=True)
class Skip:
    """One video that will not be converted, and why."""

    rel: str
    reason: str
    detail: str = ""


# --- What ffprobe found ------------------------------------------------------


@dataclass(frozen=True)
class VideoProbe:
    """One video's shape, as read by :mod:`icloud_photo_sync.transcode`.

    ``width``/``height`` are **display** dimensions: an iPhone portrait clip is
    stored as a landscape stream with a -90 rotation matrix, and reading the raw
    stream dimensions is exactly how the scale arithmetic goes wrong.
    """

    rel: str
    size: int
    width: int
    height: int
    fps: float
    """``r_frame_rate`` — the container's nominal rate, not the average. A
    variable-rate clip's *average* can sit at 43 fps while it is really a 60 fps
    capture, and it is the nominal rate that says whether this is slow motion."""
    duration: float
    codec: str = ""
    pix_fmt: str | None = None
    transfer: str | None = None
    primaries: str | None = None
    colorspace: str | None = None
    has_audio: bool = True

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)

    @property
    def bitrate(self) -> float:
        """Bits per second, from the file size — the only rate that matters here.

        The container's declared ``bit_rate`` is often absent and sometimes
        wrong; what a clip costs is what it weighs.
        """
        return self.size * 8 / self.duration if self.duration > 0 else 0.0

    @property
    def is_hdr(self) -> bool:
        return self.transfer in HDR_TRANSFERS

    @property
    def is_ten_bit(self) -> bool:
        return self.pix_fmt in TEN_BIT_PIX_FMTS

    @property
    def is_slow_motion(self) -> bool:
        return self.fps > SLOMO_FPS


@dataclass(frozen=True)
class Encode:
    """The settings chosen for one video. Turned into argv by ``transcode``."""

    width: int
    height: int
    fps: float | None
    """``None`` means "do not pass ``-r`` at all" — slow motion keeps its rate."""
    bitrate: int
    """Goes to ``-b:v``. See :data:`VIDEOTOOLBOX_EFFICIENCY` for what it yields."""
    profile: str
    pix_fmt: str
    transfer: str | None = None
    primaries: str | None = None
    colorspace: str | None = None

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class Candidate:
    """One video that passed every pre-encode gate."""

    probe: VideoProbe
    encode: Encode
    predicted_size: int

    @property
    def rel(self) -> str:
        return self.probe.rel

    @property
    def predicted_saving(self) -> int:
        return max(0, self.probe.size - self.predicted_size)


# --- The arithmetic ----------------------------------------------------------


def target_dimensions(
    width: int, height: int, short_side: int = TARGET_SHORT_SIDE
) -> tuple[int, int]:
    """Scale so the **shorter** side is at most ``short_side``. Never upscales.

    Both results are even, which every 4:2:0 encoder requires. Rounding down
    rather than to nearest keeps the output inside the requested bound.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"bad dimensions {width}x{height}")
    k = min(1.0, short_side / min(width, height))
    return max(2, int(width * k) // 2 * 2), max(2, int(height * k) // 2 * 2)


def target_bitrate(
    pixels: int, fps: float, *, hdr: bool,
    hdr_bitrate: int = BITRATE_HDR, sdr_bitrate: int = BITRATE_SDR,
) -> int:
    """The ``-b:v`` value for an output of ``pixels`` at ``fps``.

    Linear in pixel count, sub-linear in frame rate (:data:`FPS_EXPONENT`).
    """
    base = hdr_bitrate if hdr else sdr_bitrate
    fps_factor = (max(fps, 1.0) / TARGET_FPS) ** FPS_EXPONENT
    return max(1, round(base * (pixels / FULL_HD_PIXELS) * fps_factor))


def predicted_bitrate(encode: Encode) -> float:
    """What :data:`VIDEOTOOLBOX_EFFICIENCY` says ``encode`` will really produce.

    The frame rate is already priced into ``encode.bitrate``, so this is only
    the encoder's shortfall plus the audio track.
    """
    return encode.bitrate * VIDEOTOOLBOX_EFFICIENCY + AUDIO_BITRATE


def predicted_size(probe: VideoProbe, encode: Encode) -> int:
    return max(1, round(predicted_bitrate(encode) * probe.duration / 8))


def choose_encode(
    probe: VideoProbe,
    *,
    short_side: int = TARGET_SHORT_SIDE,
    max_fps: float = TARGET_FPS,
    hdr_bitrate: int = BITRATE_HDR,
    sdr_bitrate: int = BITRATE_SDR,
) -> Encode:
    """Settings for ``probe``: dimensions, frame rate, bitrate, depth, colour.

    Depth and colour are copied from the source rather than chosen — see the
    module docstring. Slow motion gets ``fps=None``, which means no ``-r`` flag.
    """
    out_w, out_h = target_dimensions(probe.width, probe.height, short_side)
    # ``None`` means "pass no ``-r`` at all", and that is right in two cases:
    # slow motion, whose rate must not be touched, and any clip already at or
    # below the cap, which has nothing to change. The second matters more than
    # it looks: a 30000/1001 clip reports 29.97002997…, and asking ffmpeg to
    # resample it to a rounded version of its own rate duplicates and drops
    # frames for no reason at all.
    out_fps = None if (probe.is_slow_motion or probe.fps <= max_fps) else max_fps
    # Bit depth follows the MEASURED source depth, never the HDR flag alone.
    # HLG/PQ almost always ride on a 10-bit source, but not always: a handful
    # of real files here are 8-bit yuv420p carrying full BT.2020/HLG tags (some
    # "_cmp_compressed" proxies re-encoded by something other than a camera).
    # `or probe.is_hdr` used to force those to p010le regardless, producing a
    # 10-bit output from an 8-bit input — exactly the promotion this module's
    # docstring says never happens, and accept_output correctly rejected every
    # one of them afterwards as a colour mismatch (depth "8" -> "10").
    ten_bit = probe.is_ten_bit
    return Encode(
        width=out_w,
        height=out_h,
        fps=out_fps,
        bitrate=target_bitrate(
            out_w * out_h, out_fps if out_fps is not None else probe.fps,
            hdr=probe.is_hdr, hdr_bitrate=hdr_bitrate, sdr_bitrate=sdr_bitrate,
        ),
        profile=PROFILE_10BIT if ten_bit else PROFILE_8BIT,
        pix_fmt=PIX_FMT_10BIT if ten_bit else PIX_FMT_8BIT,
        transfer=probe.transfer,
        primaries=probe.primaries,
        colorspace=probe.colorspace,
    )


# --- Colour ------------------------------------------------------------------


def colour_class(probe: VideoProbe) -> tuple[str, str, str, str]:
    """The four things a conversion must not change.

    Reduced to a comparable tuple so :func:`accept_output` is one equality.
    ``None`` and the string ``"unknown"`` both normalise to ``""``: ffprobe uses
    them interchangeably for an untagged stream, and an untagged source that
    stays untagged has not lost anything.
    """
    def norm(value: str | None) -> str:
        text = (value or "").strip().lower()
        return "" if text in {"unknown", "reserved", "n/a"} else text

    return (
        norm(probe.transfer),
        norm(probe.primaries),
        norm(probe.colorspace),
        "10" if probe.is_ten_bit else "8",
    )


def colour_matches(source: VideoProbe, output: VideoProbe) -> bool:
    """Did ``output`` keep ``source``'s colour?

    An untagged source is allowed to come back tagged bt709 — ffmpeg names what
    was previously implicit, and bt709 is what an untagged stream already meant.
    Nothing else may move: an HDR source that comes back SDR, or a 10-bit source
    that comes back 8-bit, has lost something no bitrate can return.
    """
    src, out = colour_class(source), colour_class(output)
    if src == out:
        return True
    if not source.transfer and not source.primaries:
        # Untagged in. Accept bt709/bt709 out, provided the depth is unchanged.
        return out[:3] in {("", "", ""), ("bt709", "bt709", "bt709")} and src[3] == out[3]
    return False


# --- Gates -------------------------------------------------------------------


def classify(
    probe: VideoProbe | None,
    *,
    rel: str | None = None,
    min_bytes: int = DEFAULT_MIN_BYTES,
    short_side: int = TARGET_SHORT_SIDE,
    max_fps: float = TARGET_FPS,
    hdr_bitrate: int = BITRATE_HDR,
    sdr_bitrate: int = BITRATE_SDR,
    has_image_sibling: bool = False,
    skip_hdr: bool = False,
    hdr_only: bool = False,
) -> Candidate | Skip:
    """Decide what to do with one video, before any work is done.

    ``probe`` may be None for a file ffprobe could not read, in which case
    ``rel`` supplies the name for the :class:`Skip`.
    """
    if probe is None:
        return Skip(rel or "?", SKIP_UNPROBEABLE)
    if probe.size < min_bytes:
        return Skip(probe.rel, SKIP_TOO_SMALL, _bytes(probe.size))
    if probe.duration <= 0 or probe.width <= 0 or probe.height <= 0:
        return Skip(probe.rel, SKIP_UNPROBEABLE, "no duration or dimensions")
    if has_image_sibling and probe.duration <= LIVE_PHOTO_MAX_SECONDS:
        return Skip(probe.rel, SKIP_LIVE_PHOTO, f"{probe.duration:.1f}s beside a still")
    if skip_hdr and probe.is_hdr:
        return Skip(probe.rel, "excluded by --skip-hdr")
    if hdr_only and not probe.is_hdr:
        return Skip(probe.rel, "excluded by --hdr-only")

    encode = choose_encode(
        probe, short_side=short_side, max_fps=max_fps,
        hdr_bitrate=hdr_bitrate, sdr_bitrate=sdr_bitrate,
    )

    # Slow motion may only be downscaled. If there is nothing to downscale there
    # is nothing worth putting nine thousand frames of unusual footage through.
    if probe.is_slow_motion and (encode.width, encode.height) == (probe.width, probe.height):
        return Skip(probe.rel, SKIP_SLOMO_AT_TARGET,
                    f"{probe.width}x{probe.height} @{round(probe.fps)}fps")

    size = predicted_size(probe, encode)
    if size >= probe.size * WORTH_IT_RATIO:
        return Skip(probe.rel, SKIP_NOTHING_TO_GAIN,
                    f"{_bytes(probe.size)} → about {_bytes(size)}")
    return Candidate(probe=probe, encode=encode, predicted_size=size)


def accept_output(
    source: VideoProbe, output: VideoProbe, *, worth_it: float = WORTH_IT_RATIO
) -> Skip | None:
    """Judge the file that was actually produced. ``None`` means keep it.

    Both checks are measurements of the real output rather than predictions, so
    neither can be wrong — which is the entire reason this gate exists after the
    encode as well as before it.
    """
    if not colour_matches(source, output):
        return Skip(
            source.rel, SKIP_COLOUR_MISMATCH,
            f"{_colour_text(source)} → {_colour_text(output)}",
        )
    if output.size >= source.size * worth_it:
        return Skip(source.rel, SKIP_NOT_SMALLER,
                    f"{_bytes(output.size)} of {_bytes(source.size)}")
    return None


# --- The swap ----------------------------------------------------------------


@dataclass(frozen=True)
class Swap:
    """Permission to delete one original, because its replacement exists.

    The ordering rule — upload first, delete second — is enforced here rather
    than in the engine that performs it: a :class:`Swap` cannot be constructed
    without a verified new asset id, so "delete this original" is not an
    expressible instruction for a video whose replacement is not known to be in
    iCloud. If the upload failed, the worst case is a duplicate; if the delete
    had gone first, the worst case is a gap.
    """

    rel: str
    old_asset_id: str
    new_asset_id: str
    old_size: int
    new_size: int

    def __post_init__(self) -> None:
        if not self.old_asset_id:
            raise ValueError("a swap needs the original's asset id")
        if not self.new_asset_id:
            raise ValueError(
                "refusing to build a swap without a verified new asset id: the "
                "replacement must be uploaded and read back before the original "
                "can be deleted"
            )
        if self.old_asset_id == self.new_asset_id:
            raise ValueError("the replacement cannot be the original")

    @property
    def freed(self) -> int:
        return max(0, self.old_size - self.new_size)


@dataclass(frozen=True)
class OptimisePlan:
    """Everything one run intends to do, and everything it declined."""

    candidates: tuple[Candidate, ...]
    skipped: tuple[Skip, ...]

    @property
    def source_bytes(self) -> int:
        return sum(c.probe.size for c in self.candidates)

    @property
    def predicted_bytes(self) -> int:
        return sum(c.predicted_size for c in self.candidates)

    @property
    def predicted_saving(self) -> int:
        return self.source_bytes - self.predicted_bytes

    @property
    def duration(self) -> float:
        return sum(c.probe.duration for c in self.candidates)

    def refusal(
        self, *, free_bytes: int | None = None, max_convert: int | None = None,
        min_free: int = 5 * 1024 * 1024 * 1024,
    ) -> str | None:
        """A whole-run refusal, or None. Each names what to do about it."""
        if free_bytes is not None and free_bytes < min_free:
            return (
                f"Only {_bytes(free_bytes)} free on the output volume; this run "
                f"needs room for the converted copies alongside the originals. "
                f"Free up {_bytes(min_free)} and try again."
            )
        if max_convert is not None and len(self.candidates) > max_convert:
            return (
                f"{len(self.candidates)} videos exceeds the {max_convert}-video "
                "limit for one run. Raise --limit, or narrow the selection with "
                "--min-size."
            )
        return None


def build_plan(
    probes: Iterable[VideoProbe | None],
    *,
    rels: Sequence[str] | None = None,
    image_stems: frozenset[str] = frozenset(),
    **options,
) -> OptimisePlan:
    """Classify every probe, largest first.

    ``image_stems`` are the ``dir/stem`` keys of every still in the tree; a video
    whose key is in the set has a same-named image beside it, which is the Live
    Photo signature :func:`classify` guards against.
    """
    names = list(rels or [])
    candidates: list[Candidate] = []
    skipped: list[Skip] = []
    for index, probe in enumerate(probes):
        rel = probe.rel if probe is not None else (
            names[index] if index < len(names) else "?"
        )
        result = classify(
            probe, rel=rel,
            has_image_sibling=stem_key(rel) in image_stems,
            **options,
        )
        (candidates if isinstance(result, Candidate) else skipped).append(result)
    candidates.sort(key=lambda c: (-c.predicted_saving, c.rel))
    skipped.sort(key=lambda s: s.rel)
    return OptimisePlan(tuple(candidates), tuple(skipped))


def flat_name(rel: str, *, taken: frozenset[str] | set[str] = frozenset()) -> str:
    """The conversion's filename inside the flat ``optimised/`` hand-off folder.

    ``2024/05/IMG_1.MOV`` becomes ``IMG_1.mov`` — always ``.mov``, since the
    output is a QuickTime container whatever the source was.

    Flattening a dated tree collides: on a real 1,504-video library, 647
    candidates shared **17 basenames** (``IMG_0003`` appears five times, from
    different years). A colliding name takes the source's month as a suffix —
    ``IMG_0003-2019-06.mov`` — which is unique by construction, because two
    files with the same stem cannot occupy the same month of the same tree.
    A third-level fallback counts, so the function is total rather than
    almost-total; it should never be reached.

    ``taken`` is compared case-insensitively: the photo volume is
    case-insensitive, so ``IMG_1.mov`` and ``img_1.MOV`` are one file there.
    """
    lowered = {t.lower() for t in taken}
    head, _, tail = rel.rpartition("/")
    stem = tail.rsplit(".", 1)[0] if "." in tail else tail
    candidate = f"{stem}.mov"
    if candidate.lower() not in lowered:
        return candidate

    parts = head.split("/")
    if len(parts) >= 2:
        candidate = f"{stem}-{parts[0]}-{parts[1]}.mov"
        if candidate.lower() not in lowered:
            return candidate

    base = f"{stem}-{head.replace('/', '-')}" if head else stem
    for n in range(2, 10_000):
        candidate = f"{base}-{n}.mov"
        if candidate.lower() not in lowered:
            return candidate
    raise ValueError(f"cannot find a free name for {rel!r}")


def stem_key(rel: str) -> str:
    """``2024/05/IMG_1.MOV`` -> ``2024/05/img_1``. Case-folded: HFS+ is too."""
    head, _, tail = rel.rpartition("/")
    stem = tail.rsplit(".", 1)[0] if "." in tail else tail
    return f"{head}/{stem}".lower() if head else stem.lower()


# --- Formatting shared by every reporter -------------------------------------


def _bytes(n: int | float) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def _colour_text(probe: VideoProbe) -> str:
    """``HLG HDR 10-bit`` / ``bt709 8-bit`` — for a skip line the user reads."""
    depth = "10-bit" if probe.is_ten_bit else "8-bit"
    if probe.transfer == "arib-std-b67":
        return f"HLG HDR {depth}"
    if probe.transfer == "smpte2084":
        return f"PQ HDR {depth}"
    return f"{probe.transfer or 'untagged'} {depth}"


human_size = _bytes
colour_text = _colour_text
