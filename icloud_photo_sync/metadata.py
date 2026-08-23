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
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

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
_VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".3gp", ".3g2",
                   ".wmv", ".webm", ".mkv", ".mts", ".m2ts"}
_IMAGE_SUFFIXES = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}

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
    if suffix in _VIDEO_SUFFIXES:
        return "ffmpeg/ffprobe"
    if suffix in _IMAGE_SUFFIXES:
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


def _video_has_date(path: Path) -> bool | None:
    """Does the container already carry a capture date? ``None`` = couldn't tell.

    Checking both tags: a file could plausibly carry one and not the other, and
    treating a ``None`` read as "absent" would risk overwriting a date this
    function simply failed to see — the read-before-write discipline this
    module exists to honour.
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
        tags = (json.loads(proc.stdout).get("format") or {}).get("tags") or {}
    except (ValueError, TypeError):
        return None
    return bool(tags.get("creation_time")) or bool(tags.get("com.apple.quicktime.creationdate"))


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


def _image_has_date(path: Path) -> bool | None:
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
    return any(line.strip() for line in proc.stdout.splitlines())


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

    Never raises. A file type this module doesn't recognise, a missing tool, or
    a failed read/write all come back as a specific :class:`MetadataOutcome`
    for the caller to log or ignore — this is enrichment, not a precondition,
    and nothing here may turn a successful download or a colour-verified
    conversion into a failure.
    """
    try:
        outcome = _ensure_embedded_date(path, capture_dt, tz_offset_seconds)
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
) -> MetadataOutcome:
    suffix = path.suffix.lower()
    if suffix in _VIDEO_SUFFIXES:
        if not ffmpeg_pair_available():
            return MetadataOutcome.TOOL_UNAVAILABLE
        has = _video_has_date(path)
        if has is None:
            return MetadataOutcome.FAILED
        if has:
            return MetadataOutcome.ALREADY_PRESENT
        return (MetadataOutcome.STAMPED if _stamp_video(path, capture_dt, tz_offset_seconds)
                else MetadataOutcome.FAILED)

    if suffix in _IMAGE_SUFFIXES:
        if not exiftool_available():
            return MetadataOutcome.TOOL_UNAVAILABLE
        has = _image_has_date(path)
        if has is None:
            return MetadataOutcome.FAILED
        if has:
            return MetadataOutcome.ALREADY_PRESENT
        return (MetadataOutcome.STAMPED if _stamp_image(path, capture_dt, tz_offset_seconds)
                else MetadataOutcome.FAILED)

    return MetadataOutcome.UNSUPPORTED_TYPE
