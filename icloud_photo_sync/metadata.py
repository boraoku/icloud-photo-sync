"""Filling in a capture date that's genuinely missing. Never overwriting one.

The failure this exists to close: WhatsApp strips embedded capture-date
metadata before a video is re-shared. iCloud's own record of the capture date
(``assetDate``) is unaffected — folder placement stays correct — but the local
*file* carries nothing. Every write this codebase performs after that point
(``sync`` downloading it, ``video-optimise`` re-encoding it) naturally sets the
file's mtime to "now", because that's what writing a file does. When such a
file is later re-imported into Photos by hand — the one step Apple reserves
for its own clients, see :mod:`icloud_photo_sync.icloud_client` — Photos has no
embedded date to read and falls back to that mtime: the day it was downloaded
or converted, not the day it was filmed.

:func:`ensure_capture_date` is the fix, called once a file's true capture date
is known (from iCloud, or from the sync manifest): it always sets the
filesystem mtime to that date — cheap, universal, and the actual fallback that
caused the bug — and, only when the embedded date is genuinely *absent*,
stamps it in too. A mismatched embedded date is never touched; deciding which
of two conflicting dates is right is a different, harder problem this does not
take on.

Both stamps are metadata-only rewrites, verified empirically before this was
written: ``ffmpeg -c copy`` remuxes a video's container without touching a
single video/audio sample (37ms on a real 13 MB clip, byte count essentially
unchanged), and ``exiftool``'s ``-o`` form does the same for EXIF, writing to a
new file we swap in ourselves rather than trusting a tool's own in-place
write — the same ``.part`` → :func:`os.replace` discipline every other writer
in this codebase uses. Neither tool is required for this module to import;
their absence is reported once, per file, as :attr:`MetadataOutcome.
TOOL_UNAVAILABLE` — this is an enrichment layered on top of a correct
download, never a new precondition for one. ``sips``, macOS's own image tool,
was tried first and ruled out: it labels the relevant property
``(read-only)`` and refuses to set it, confirmed against a real file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Sequence

from .logutil import get_logger

logger = get_logger(__name__)

FFPROBE_TIMEOUT = 30.0
FFMPEG_TIMEOUT = 300.0
"""Generous: a stream-copy remux is fast, but a large video on a slow disk
should not be mistaken for a hang."""
EXIFTOOL_TIMEOUT = 30.0

# Deliberately this module's own sets, not config.py's IMAGE_SUFFIXES: that one
# is scoped to local-clean's junk-image scan (.jpg/.jpeg/.png only, ≤1 MiB) and
# changing its meaning would be a surprise to a different feature. HEIC is the
# format iCloud actually hands back for photos, so it has to be here.
VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".3gp", ".3g2",
                  ".wmv", ".webm", ".mkv", ".mts", ".m2ts"}
IMAGE_SUFFIXES = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}

_tool_cache: dict[str, bool] = {}
"""Memoised availability checks — shells out, so ask once per process."""


class MetadataOutcome(str, Enum):
    ALREADY_PRESENT = "already present"
    STAMPED = "stamped"
    UNSUPPORTED_TYPE = "unsupported type"      # not a video/image suffix we handle
    TOOL_UNAVAILABLE = "tool unavailable"
    FAILED = "failed"                          # the tool ran and could not tell us / could not write


def exiftool_available() -> bool:
    """True when ``exiftool`` is on PATH. Cached after the first call."""
    if "exiftool" not in _tool_cache:
        _tool_cache["exiftool"] = shutil.which("exiftool") is not None
    return _tool_cache["exiftool"]


def ffmpeg_pair_available() -> bool:
    """True when both ``ffmpeg`` and ``ffprobe`` are on PATH."""
    if "ffmpeg_pair" not in _tool_cache:
        _tool_cache["ffmpeg_pair"] = (
            shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
        )
    return _tool_cache["ffmpeg_pair"]


def required_tool_name(path: Path) -> str | None:
    """Which external tool ``ensure_capture_date`` would need for ``path`` —
    for a caller building a one-time "please install X" notice. ``None`` for
    an unsupported suffix, where no tool would be consulted at all."""
    suffix = path.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "ffmpeg/ffprobe"
    if suffix in IMAGE_SUFFIXES:
        return "exiftool"
    return None


# --- formatting ----------------------------------------------------------------


def _at_offset(dt: datetime, tz_offset_seconds: int | None) -> datetime:
    """``dt`` converted to the given local offset, or UTC when none is known."""
    tz = timezone(timedelta(seconds=tz_offset_seconds)) if tz_offset_seconds is not None \
        else timezone.utc
    return dt.astimezone(tz)


def _quicktime_stamp(dt: datetime, tz_offset_seconds: int | None) -> str:
    """``com.apple.quicktime.creationdate`` form: local time with its offset —
    verified against real Apple-recorded videos to be what that field actually
    looks like (e.g. ``2024-05-03T13:34:46+0300``)."""
    return _at_offset(dt, tz_offset_seconds).strftime("%Y-%m-%dT%H:%M:%S%z")


def _creation_time_stamp(dt: datetime) -> str:
    """The generic ``creation_time`` tag: UTC, ``Z``-suffixed — the portable
    form every tool that reads it expects, independent of local offset."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _exif_stamp(dt: datetime, tz_offset_seconds: int | None) -> str:
    """EXIF's ``DateTimeOriginal`` form: ``YYYY:MM:DD HH:MM:SS``. EXIF has no
    offset field of its own, so this is the best-known local wall-clock time."""
    return _at_offset(dt, tz_offset_seconds).strftime("%Y:%m:%d %H:%M:%S")


# --- video -----------------------------------------------------------------------


def _video_date_tags(path: Path) -> dict | None:
    """The two date tags ffprobe can see, or ``None`` if it couldn't tell.

    Shared by :func:`_video_has_date` (does either exist?) and
    :func:`_read_video_embedded_date` (what does one actually say?) — one
    subprocess call, two questions.
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format_tags=creation_time,com.apple.quicktime.creationdate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("ffprobe failed reading %s: %s", path, exc)
        return None
    if proc.returncode != 0:
        logger.debug("ffprobe exited %d reading %s", proc.returncode, path)
        return None
    try:
        return (json.loads(proc.stdout).get("format") or {}).get("tags") or {}
    except (ValueError, TypeError):
        return None


def _video_has_date(path: Path) -> bool | None:
    """Does the container already carry a capture date? ``None`` = couldn't tell.

    Checking both tags: a file could plausibly carry one and not the other, and
    treating a ``None`` read as "absent" would risk overwriting a date this
    function simply failed to see — the read-before-write discipline this
    module exists to honour.
    """
    tags = _video_date_tags(path)
    if tags is None:
        return None
    return bool(tags.get("creation_time")) or bool(tags.get("com.apple.quicktime.creationdate"))


def _read_video_embedded_date(path: Path) -> datetime | None:
    """The actual value of whichever date tag is present, parsed — not just
    whether one exists. ``creation_time`` is preferred: it's UTC and
    unambiguous, where the QuickTime tag's offset still has to be trusted."""
    tags = _video_date_tags(path)
    if not tags:
        return None
    creation = tags.get("creation_time")
    if creation:
        try:
            return datetime.strptime(creation, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    quicktime = tags.get("com.apple.quicktime.creationdate")
    if quicktime:
        try:
            return datetime.strptime(quicktime, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            pass
    return None


def _stamp_video(path: Path, dt: datetime, tz_offset_seconds: int | None) -> bool:
    """Stream-copy remux: write both date tags, touch no video/audio sample."""
    part = path.with_name(path.name + ".meta.part")
    part.unlink(missing_ok=True)
    argv = [
        "ffmpeg", "-y", "-v", "error", "-nostdin", "-i", str(path),
        "-map", "0", "-c", "copy", "-map_metadata", "0",
        "-metadata", f"creation_time={_creation_time_stamp(dt)}",
        "-metadata", f"com.apple.quicktime.creationdate={_quicktime_stamp(dt, tz_offset_seconds)}",
        "-movflags", "use_metadata_tags+faststart",
        # Named explicitly rather than inferred from the extension: ffmpeg
        # cannot guess a container from ".meta.part" (verified — it refuses
        # outright), and the ".part" suffix is what keeps a reader from ever
        # seeing a half-written file.
        "-f", "mov",
        str(part),
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("ffmpeg metadata stamp failed for %s: %s", path, exc)
        part.unlink(missing_ok=True)
        return False
    if proc.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
        logger.debug("ffmpeg metadata stamp failed for %s: %s", path,
                     (proc.stderr or "")[:300])
        part.unlink(missing_ok=True)
        return False
    os.replace(part, path)
    return True


# --- image -------------------------------------------------------------------


def _image_date_lines(path: Path) -> list[str] | None:
    """The two date tags' raw text, one per line (blank = absent), or ``None``
    if exiftool couldn't be asked. Shared by :func:`_image_has_date` and
    :func:`_read_image_embedded_date`."""
    try:
        proc = subprocess.run(
            ["exiftool", "-s3", "-DateTimeOriginal", "-CreateDate", str(path)],
            capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("exiftool failed reading %s: %s", path, exc)
        return None
    if proc.returncode != 0:
        logger.debug("exiftool exited %d reading %s", proc.returncode, path)
        return None
    # -s3 prints one value per line, blank for a tag that is not present.
    return proc.stdout.splitlines()


def _image_has_date(path: Path) -> bool | None:
    lines = _image_date_lines(path)
    if lines is None:
        return None
    return any(line.strip() for line in lines)


def _parse_exif_datetime(text: str | None) -> datetime | None:
    """EXIF's ``YYYY:MM:DD HH:MM:SS``, parsed. EXIF has no offset field, so
    this is read back as a naive local wall-clock time and treated as UTC —
    the same assumption :func:`_exif_stamp` makes on write. A blank, a
    malformed value, or exiftool's ``0000:00:00 00:00:00`` placeholder all
    come back as ``None``."""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _read_image_embedded_date(path: Path) -> datetime | None:
    """The actual value of whichever date tag is present, parsed."""
    lines = _image_date_lines(path)
    if not lines:
        return None
    for line in lines:
        parsed = _parse_exif_datetime(line)
        if parsed is not None:
            return parsed
    return None


EXIFTOOL_BATCH = 500
"""Files per ``exiftool`` invocation when reading in bulk.

Large enough that process startup — which is what actually costs, ~89ms
against ~3.6ms of real work per file — is amortised to nothing, small enough
that a caller still gets frequent progress updates and a Ctrl-C never
discards much. Paths go through an argument file rather than the command
line, so this bound is about responsiveness, not ``ARG_MAX``.
"""

EXIFTOOL_BATCH_TIMEOUT = 300.0


def read_embedded_capture_dates(
    paths: Sequence[Path], *, timeout: float = EXIFTOOL_BATCH_TIMEOUT,
) -> dict[Path, datetime | None]:
    """Every path's embedded EXIF capture date, read in ONE ``exiftool`` run.

    The bulk equivalent of :func:`_read_image_embedded_date`, and the reason
    ``photo-optimise-external`` finishes in minutes rather than hours: reading
    two tags takes ~3.6ms, but spawning a process to do it takes ~89ms, so a
    per-file invocation spends 96% of its time on startup. Measured against
    real HEICs, batching this way is ~25x faster end to end.

    Every path in ``paths`` appears in the result. ``None`` means "no date" —
    genuinely absent, unparseable, or a file exiftool could not read at all.
    Callers must treat all three the same way they treat a failed single-file
    read: as grounds to look elsewhere for a date, never as grounds to skip
    the read-before-write check.

    Paths are passed via an argument file (``-@``), which sidesteps
    ``ARG_MAX`` entirely and handles spaces, parentheses and non-ASCII names
    without any quoting of our own — all verified against real filenames.
    """
    remaining = [p for p in paths if p.suffix.lower() in IMAGE_SUFFIXES]
    out: dict[Path, datetime | None] = {p: None for p in paths}
    if not remaining or not exiftool_available():
        return out

    for start in range(0, len(remaining), EXIFTOOL_BATCH):
        chunk = remaining[start:start + EXIFTOOL_BATCH]
        out.update(_read_one_batch(chunk, timeout))
    return out


def _read_one_batch(chunk: Sequence[Path], timeout: float) -> dict[Path, datetime | None]:
    """One ``exiftool -@`` run. Never raises: a batch that fails for any
    reason reports every file in it as dateless, which is the same answer a
    failed single-file read gives."""
    results: dict[Path, datetime | None] = {p: None for p in chunk}
    # Resolved once here so the JSON's SourceFile can be matched back to the
    # exact Path object the caller handed us, whatever form it was in.
    by_resolved = {str(p.resolve()): p for p in chunk}

    argfile = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".args", delete=False, encoding="utf-8",
        ) as fh:
            argfile = Path(fh.name)
            # Options first, then one path per line — exiftool applies the
            # tag selection to every file that follows.
            fh.write("-j\n-s3\n-DateTimeOriginal\n-CreateDate\n")
            for resolved in by_resolved:
                fh.write(resolved + "\n")
        proc = subprocess.run(
            ["exiftool", "-@", str(argfile)],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("batched exiftool read failed for %d file(s): %s", len(chunk), exc)
        return results
    finally:
        if argfile is not None:
            argfile.unlink(missing_ok=True)

    # A non-zero exit is normal here: exiftool returns 1 when ANY file in the
    # batch was unreadable, while still emitting good JSON for the rest. The
    # payload is what matters, not the code.
    try:
        entries = json.loads(proc.stdout or "[]")
    except (ValueError, TypeError):
        logger.debug("batched exiftool read returned unparseable JSON for %d file(s)",
                     len(chunk))
        return results

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = by_resolved.get(str(entry.get("SourceFile", "")))
        if path is None:
            continue
        results[path] = (_parse_exif_datetime(entry.get("DateTimeOriginal"))
                         or _parse_exif_datetime(entry.get("CreateDate")))
    return results


def _stamp_image(path: Path, dt: datetime, tz_offset_seconds: int | None) -> bool:
    stamp = _exif_stamp(dt, tz_offset_seconds)
    part = path.with_name(path.name + ".meta.part")
    part.unlink(missing_ok=True)
    argv = [
        "exiftool", "-q", "-q",
        f"-DateTimeOriginal={stamp}", f"-CreateDate={stamp}", f"-ModifyDate={stamp}",
        "-o", str(part), str(path),
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("exiftool stamp failed for %s: %s", path, exc)
        part.unlink(missing_ok=True)
        return False
    if proc.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
        logger.debug("exiftool stamp failed for %s: %s", path, (proc.stderr or "")[:300])
        part.unlink(missing_ok=True)
        return False
    os.replace(part, path)
    return True


# --- the entry point -----------------------------------------------------------


def ensure_capture_date(
    path: Path, capture_dt: datetime, *, tz_offset_seconds: int | None = None,
    known_absent: bool = False,
) -> MetadataOutcome:
    """Make sure ``path`` reflects ``capture_dt`` — mtime always, metadata if absent.

    Two independent actions, matching the two ways a fallback can go wrong:

    1. ``os.utime()`` — unconditional, regardless of what follows. This is the
       actual mechanism that misdated files in the field, and setting it
       correctly costs nothing even on a file that never needed it.
    2. Embedded metadata — only touched when the file's own copy is genuinely
       *absent* (see :func:`_video_has_date` / :func:`_image_has_date`). A file
       that already carries a date, even one that looks wrong, is left alone;
       correcting a mismatch is a different problem from filling a blank.

    ``known_absent`` says the caller has ALREADY established, by a real read,
    that this file carries no embedded date — so the presence check here would
    re-read the same file to reach the same conclusion. It skips that second
    read, nothing else; it does not relax the read-before-write rule, because
    the read still happened, just earlier and in bulk. Only
    :func:`read_embedded_capture_dates` currently justifies passing it, and a
    caller that has not actually read the file must not.

    Never raises. A file type this module doesn't recognise, a missing tool, or
    a failed read/write all come back as a specific :class:`MetadataOutcome`
    for the caller to log or ignore — this is enrichment, not a precondition,
    and nothing here may turn a successful download or a colour-verified
    conversion into a failure.
    """
    try:
        outcome = _ensure_embedded_date(path, capture_dt, tz_offset_seconds,
                                        known_absent)
    finally:
        # LAST, unconditionally, deliberately after any stamp attempt: a
        # successful stamp replaces the file via os.replace(path), and the
        # replacement file's own mtime (the moment ffmpeg/exiftool wrote it)
        # would silently clobber an mtime set before that swap — observed
        # live on the very first real test of this function, which is
        # exactly the bug this module exists to close.
        try:
            ts = capture_dt.timestamp()
            os.utime(path, (ts, ts))
        except (OSError, OverflowError, ValueError) as exc:
            logger.debug("could not set mtime on %s: %s", path, exc)
    return outcome


def _ensure_embedded_date(
    path: Path, capture_dt: datetime, tz_offset_seconds: int | None,
    known_absent: bool = False,
) -> MetadataOutcome:
    suffix = path.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        if not ffmpeg_pair_available():
            return MetadataOutcome.TOOL_UNAVAILABLE
        has = False if known_absent else _video_has_date(path)
        if has is None:
            return MetadataOutcome.FAILED
        if has:
            return MetadataOutcome.ALREADY_PRESENT
        return (MetadataOutcome.STAMPED if _stamp_video(path, capture_dt, tz_offset_seconds)
                else MetadataOutcome.FAILED)

    if suffix in IMAGE_SUFFIXES:
        if not exiftool_available():
            return MetadataOutcome.TOOL_UNAVAILABLE
        has = False if known_absent else _image_has_date(path)
        if has is None:
            return MetadataOutcome.FAILED
        if has:
            return MetadataOutcome.ALREADY_PRESENT
        return (MetadataOutcome.STAMPED if _stamp_image(path, capture_dt, tz_offset_seconds)
                else MetadataOutcome.FAILED)

    return MetadataOutcome.UNSUPPORTED_TYPE


# --- inferring a date with no manifest at all -------------------------------
#
# ``ensure_capture_date`` above assumes the caller already knows the true
# capture date (from iCloud, or from the sync manifest). The two
# ``*-external`` commands have neither — a folder of files that may never
# have passed through this tool's own download at all — so this is a second,
# independent ladder for finding a date worth trying, used only when nothing
# authoritative is available. Every rung here is a *guess in decreasing order
# of confidence*, not a lookup, which is why the last one is deliberately
# coarse (the middle of a month) rather than silently inventing a day.

CaptureDateSource = str
"""One of ``"embedded"``, ``"filename"``, ``"folder"`` or ``"unknown"`` — which
rung of :func:`infer_capture_date` answered, for a caller that wants to report
what it did rather than just act on it."""

SOURCE_EMBEDDED: CaptureDateSource = "embedded"
SOURCE_FILENAME: CaptureDateSource = "filename"
SOURCE_FOLDER: CaptureDateSource = "folder"
SOURCE_UNKNOWN: CaptureDateSource = "unknown"


def read_embedded_capture_date(path: Path) -> datetime | None:
    """The file's own embedded capture date, parsed — or ``None`` if it has
    none, the type is unsupported, or the read failed. Dispatches by suffix
    exactly like :func:`_ensure_embedded_date`, but returns the real value
    instead of a presence check: the first, most-trusted rung of
    :func:`infer_capture_date`."""
    suffix = path.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return _read_video_embedded_date(path)
    if suffix in IMAGE_SUFFIXES:
        return _read_image_embedded_date(path)
    return None


# WhatsApp's own naming: IMG-20191025-WA0007.jpg, VID-20191025-WA0003.mp4 —
# a date with no time. Matched by the "-WA<digits>" suffix, not the IMG-/VID-
# prefix, since that suffix is the actually-distinctive signal.
_WA_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})-WA\d+", re.IGNORECASE)

# Generic camera/phone timestamp naming: IMG_20191025_150712.jpg,
# VID_20191025_150712.mp4, or a bare 20191025_150712.mp4 — date AND time.
# Deliberately narrow (an underscore-separated 8-digit/6-digit pair) rather
# than "any 8 digits that happen to parse as a date": a serial number or a
# resolution tag could coincidentally validate as a date, and this module's
# whole discipline is to guess only when the shape is actually recognisable.
_CAMERA_DATETIME_RE = re.compile(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})")


def infer_capture_date_from_name(name: str) -> datetime | None:
    """A date guessed from a filename WhatsApp or a camera app produced.

    The datetime pattern is tried first — when it matches, it's strictly more
    informative than the date-only one. Either way an invalid calendar date
    (month 13, day 32) is rejected via the same construction that would
    reject it anywhere else, not specially detected.
    """
    m = _CAMERA_DATETIME_RE.search(name)
    if m:
        try:
            return datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
        except ValueError:
            pass
    m = _WA_DATE_RE.search(name)
    if m:
        try:
            year, month, day = (int(g) for g in m.groups())
            return datetime(year, month, day, 12, 0, 0, tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_MONTH_RE = re.compile(r"^(0[1-9]|1[0-2])$")


def infer_capture_date_from_path(rel: str) -> datetime | None:
    """A date guessed from a ``YYYY/MM`` folder — this tool's own ``sync``
    layout. The 15th at noon UTC: the middle of the only interval actually
    known, so no single guessed instant is more wrong than any other."""
    parts = PurePosixPath(rel).parts
    if len(parts) < 2:
        return None
    year, month = parts[0], parts[1]
    if not _YEAR_RE.match(year) or not _MONTH_RE.match(month):
        return None
    return datetime(int(year), int(month), 15, 12, 0, 0, tzinfo=timezone.utc)


def infer_capture_date_from(
    embedded: datetime | None, name: str, rel: str,
) -> tuple[datetime | None, CaptureDateSource]:
    """The ladder itself, given an already-read embedded date.

    Split out from :func:`infer_capture_date` so the one-file and the bulk
    callers share a single definition of the priority order rather than each
    reimplementing it: ``photo-optimise-external`` reads thousands of
    embedded dates in one go (:func:`read_embedded_capture_dates`) and then
    walks the rest of the ladder here, per file, in pure Python.
    """
    if embedded is not None:
        return embedded, SOURCE_EMBEDDED
    from_name = infer_capture_date_from_name(name)
    if from_name is not None:
        return from_name, SOURCE_FILENAME
    from_path = infer_capture_date_from_path(rel)
    if from_path is not None:
        return from_path, SOURCE_FOLDER
    return None, SOURCE_UNKNOWN


def infer_capture_date(path: Path, rel: str) -> tuple[datetime | None, CaptureDateSource]:
    """The full ladder for a file with no authoritative date on hand: the
    file's own embedded metadata, then its filename, then (last resort) the
    ``YYYY/MM`` folder it's sitting in. ``(None, SOURCE_UNKNOWN)`` when
    nothing answers — never a guess where no signal exists at all.
    """
    return infer_capture_date_from(read_embedded_capture_date(path), path.name, rel)
