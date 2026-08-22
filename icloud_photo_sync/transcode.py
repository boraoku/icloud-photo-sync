"""Shelling out to ``ffmpeg``/``ffprobe`` for the mechanics of one conversion.

:mod:`icloud_photo_sync.video_optimise` is pure policy — it decides *whether*
and *how* to re-encode a clip from a :class:`~icloud_photo_sync.video_optimise.VideoProbe`,
with no subprocess in sight, so that every rule there is unit-testable without a
media file. This module is the other half: turning a path into a
:class:`~icloud_photo_sync.video_optimise.VideoProbe` (:func:`probe`), turning
an :class:`~icloud_photo_sync.video_optimise.Encode` into an ``ffmpeg``
command line (:func:`build_argv`), and running that command line to completion
or failure (:func:`convert`). Nothing here raises for an ordinary failure —
a missing binary, a file ``ffprobe`` cannot read, an encode that times out —
because every caller already has a policy for "this video could not be
handled": skip it and move on. That mirrors :mod:`icloud_photo_sync.poster`,
the other module that shells out to ffmpeg on a best-effort basis.

**Rotation.** An iPhone shot in portrait stores its video stream as landscape
plus a -90 (or +90) rotation matrix; the player rotates the decoded frame at
display time. Reading the raw stream width/height without accounting for that
matrix silently treats every portrait phone clip as landscape, which is wrong
for the majority of a phone library — so :func:`probe` swaps width and height
whenever the rotation is an odd multiple of 90.

**Frame rate.** :func:`probe` reads ``r_frame_rate`` — the container's nominal
rate — not ``avg_frame_rate``. A variable-rate clip's average can drift well
below its nominal rate, and it is the nominal rate that says whether a clip is
slow motion (see ``SLOMO_FPS`` in :mod:`video_optimise`).

**Metadata.** :func:`build_argv` passes ``-map_metadata 0`` together with
``-movflags use_metadata_tags``, which is what carries
``com.apple.quicktime.creationdate`` (with its timezone) and
``com.apple.quicktime.location.ISO6709`` through to the output — and that is
what lets Photos file the converted clip on the right date. Verified to work
against real Photos imports; do not change it without re-verifying.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .logutil import get_logger
from .video_optimise import Encode, VideoProbe

logger = get_logger(__name__)

FFPROBE_TIMEOUT = 60.0
DEFAULT_ENCODE_TIMEOUT = 3600.0

_TERMINATE_GRACE = 5.0
"""Seconds to let ``ffmpeg`` exit after ``terminate()`` before ``kill()``."""

_encoder_cache: dict[str, bool] = {}
"""Memoised :func:`encoder_available` results — shells out, so ask once per run."""


# --- availability --------------------------------------------------------------


def ffmpeg_available() -> bool:
    """Both ``ffmpeg`` and ``ffprobe`` are on PATH."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def encoder_available(name: str = "hevc_videotoolbox") -> bool:
    """True when ``ffmpeg -encoders`` lists ``name``. Cached after the first call."""
    if name in _encoder_cache:
        return _encoder_cache[name]
    result = False
    if shutil.which("ffmpeg") is not None:
        try:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=FFPROBE_TIMEOUT,
            )
            result = proc.returncode == 0 and name in proc.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("ffmpeg -encoders failed: %s", exc)
    _encoder_cache[name] = result
    return result


# --- probing ---------------------------------------------------------------------


def _run_ffprobe(argv: list[str]) -> dict | None:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("ffprobe failed: %s", exc)
        return None
    if proc.returncode != 0:
        logger.debug("ffprobe exited %d: %s", proc.returncode, proc.stderr[:500])
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        logger.debug("ffprobe produced unparseable JSON: %s", exc)
        return None


def _probe_has_audio(path: Path) -> bool:
    """False for anything but a confirmed audio stream — silent clips exist."""
    data = _run_ffprobe([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name", "-of", "json", str(path),
    ])
    return bool(data and data.get("streams"))


def _display_dimensions(stream: dict) -> tuple[int, int] | None:
    """Stream ``width``/``height``, swapped if a rotation matrix says so."""
    width, height = stream.get("width"), stream.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        return None
    if width <= 0 or height <= 0:
        return None
    for side in stream.get("side_data_list") or []:
        rotation = side.get("rotation")
        if rotation is None:
            continue
        try:
            degrees = int(rotation)
        except (TypeError, ValueError):
            continue
        # A quarter-turn (either direction) swaps which side is "up"; a
        # half-turn (180) does not.
        if abs(degrees) % 180 == 90:
            width, height = height, width
        break
    return width, height


def _parse_fps(raw: str | None) -> float:
    """``r_frame_rate`` is ``"num/den"``; a zero or malformed denominator is 0.0."""
    if not raw:
        return 0.0
    num, _, den = raw.partition("/")
    try:
        numerator = float(num)
        denominator = float(den) if den else 1.0
    except ValueError:
        return 0.0
    if denominator == 0:
        return 0.0
    return numerator / denominator


def probe(path: Path, rel: str) -> VideoProbe | None:
    """Read one video's shape with ``ffprobe``. None rather than raising.

    Two separate ``ffprobe`` calls: one for the video stream (dimensions,
    rate, colour, rotation) plus the container duration, and one to check for
    an audio stream. Splitting them keeps the ``-select_streams`` filters
    simple and each call cheap.
    """
    if shutil.which("ffprobe") is None:
        return None

    data = _run_ffprobe([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,codec_name,pix_fmt,color_transfer,"
        "color_primaries,color_space:stream_side_data=rotation:format=duration",
        "-of", "json", str(path),
    ])
    if data is None:
        return None

    streams = data.get("streams") or []
    if not streams:
        logger.debug("no video stream for %s", path)
        return None
    stream = streams[0]

    dims = _display_dimensions(stream)
    if dims is None:
        logger.debug("no usable dimensions for %s", path)
        return None
    width, height = dims

    duration_raw = (data.get("format") or {}).get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        logger.debug("no duration for %s", path)
        return None
    if duration <= 0:
        logger.debug("non-positive duration for %s", path)
        return None

    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.debug("stat failed for %s: %s", path, exc)
        return None

    def norm(value: object) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    return VideoProbe(
        rel=rel,
        size=size,
        width=width,
        height=height,
        fps=_parse_fps(stream.get("r_frame_rate")),
        duration=duration,
        codec=norm(stream.get("codec_name")) or "",
        pix_fmt=norm(stream.get("pix_fmt")),
        transfer=norm(stream.get("color_transfer")),
        primaries=norm(stream.get("color_primaries")),
        colorspace=norm(stream.get("color_space")),
        has_audio=_probe_has_audio(path),
    )


# --- building the ffmpeg command line --------------------------------------------


def build_argv(src: Path, dest: Path, encode: Encode, *, has_audio: bool = True) -> list[str]:
    """The exact ``ffmpeg`` argv for one conversion. Every value comes from ``encode``."""
    argv: list[str] = [
        "ffmpeg", "-y", "-v", "error", "-nostdin", "-i", str(src),
        "-map", "0:v:0",
    ]
    if has_audio:
        # "?" makes a missing audio stream a no-op instead of a hard error.
        argv += ["-map", "0:a?"]

    argv += [
        "-c:v", "hevc_videotoolbox",
        "-tag:v", "hvc1",           # without this QuickTime/Photos refuse to play the file
        "-profile:v", str(encode.profile),
        "-pix_fmt", str(encode.pix_fmt),
        # format= inside the filter chain re-asserts the pixel format after
        # scaling — a filter that silently resets it is the realistic way a
        # colour tag gets lost.
        "-vf", f"scale={encode.width}:{encode.height},format={encode.pix_fmt}",
    ]
    if encode.fps is not None:
        # Omitted entirely for slow motion: forcing 30 fps onto a 240 fps
        # clip would drop seven of every eight frames and play back at
        # normal speed, destroying the slow motion.
        argv += ["-r", f"{encode.fps:g}"]

    bitrate = str(encode.bitrate)
    argv += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]

    if encode.primaries:
        argv += ["-color_primaries", str(encode.primaries)]
    if encode.transfer:
        argv += ["-color_trc", str(encode.transfer)]
    if encode.colorspace:
        argv += ["-colorspace", str(encode.colorspace)]
    argv += ["-color_range", "tv"]

    if has_audio:
        argv += ["-c:a", "aac", "-b:a", "128000"]

    argv += [
        # Carries com.apple.quicktime.creationdate/location.ISO6709 through,
        # which is what lets Photos file the result on the right date. Timed
        # metadata (codec_name=unknown) tracks are deliberately not mapped:
        # ffmpeg cannot reliably copy those.
        "-map_metadata", "0",
        "-movflags", "use_metadata_tags+faststart",
        "-progress", "pipe:1", "-nostats",
        # Name the muxer rather than letting ffmpeg infer it from the extension:
        # the encode writes to "<name>.mov.part" so a reader never sees a half
        # file, and ffmpeg cannot guess a container from ".part".
        "-f", "mov",
        str(dest),
    ]
    return [str(a) for a in argv]


# --- running the encode -----------------------------------------------------------


@dataclass(frozen=True)
class ConvertResult:
    ok: bool
    size: int = 0
    error: str = ""
    cancelled: bool = False


def _parse_progress_line(line: str, total_seconds: float) -> float | None:
    """``out_time_us=<µs>`` -> a 0..1 fraction of ``total_seconds``, else None."""
    key, sep, value = line.partition("=")
    if not sep or key != "out_time_us":
        return None
    try:
        out_time_us = int(value)
    except ValueError:
        return None
    if total_seconds <= 0:
        return None
    return min(1.0, (out_time_us / 1_000_000.0) / total_seconds)


def convert(
    src: Path,
    dest: Path,
    encode: Encode,
    *,
    has_audio: bool = True,
    duration: float = 0.0,
    timeout: float = DEFAULT_ENCODE_TIMEOUT,
    cancel: threading.Event | None = None,
    on_progress: Callable[[float], None] | None = None,
    runner: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> ConvertResult:
    """Run ``ffmpeg`` to produce ``dest`` from ``src``. Never raises.

    Writes to ``dest`` + ``.part`` and renames onto ``dest`` only once ffmpeg
    exits 0, so a reader can never observe a half-written file. ``duration``
    (seconds) is only used to turn ``out_time_us`` progress lines into a
    fraction for ``on_progress``; it does not affect the encode itself.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    part.unlink(missing_ok=True)   # clear a stale .part from an earlier crash

    argv = build_argv(src, part, encode, has_audio=has_audio)

    try:
        proc = runner(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("ffmpeg failed to start for %s: %s", src, exc)
        return ConvertResult(ok=False, error=str(exc)[:500])

    cancelled = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            fraction = _parse_progress_line(line.strip(), duration)
            if fraction is not None and on_progress is not None:
                try:
                    on_progress(fraction)
                except Exception as exc:      # a bad callback must not kill the encode
                    logger.debug("on_progress callback raised: %s", exc)

        if cancelled:
            _terminate(proc)
            part.unlink(missing_ok=True)
            return ConvertResult(ok=False, cancelled=True)

        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate(proc)
            part.unlink(missing_ok=True)
            return ConvertResult(ok=False, error="ffmpeg timed out")

        stderr_text = ""
        if proc.stderr is not None:
            stderr_text = proc.stderr.read() or ""

        if returncode != 0:
            part.unlink(missing_ok=True)
            return ConvertResult(ok=False, error=stderr_text.strip()[:500])

        os.replace(part, dest)
        return ConvertResult(ok=True, size=dest.stat().st_size)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("ffmpeg conversion failed for %s: %s", src, exc)
        part.unlink(missing_ok=True)
        return ConvertResult(ok=False, error=str(exc)[:500])


def _terminate(proc: subprocess.Popen) -> None:
    """Ask nicely, then insist. Never raises even if the process is already gone."""
    try:
        proc.terminate()
        proc.wait(timeout=_TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=_TERMINATE_GRACE)
        except (OSError, subprocess.SubprocessError):
            pass
    except (OSError, subprocess.SubprocessError):
        pass
