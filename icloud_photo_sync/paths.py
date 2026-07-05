"""Destination path resolution (C4).

Maps an asset to ``<root>/<YYYY>/<MM>/<filename>`` using the capture date, with
zero-padded months and deterministic collision suffixing.

Timezone decision (made once, never changed — folder stability depends on it):
the folder is derived from the capture timestamp in **UTC**. iCloud reports the
capture instant as a UTC-aware datetime and does not reliably expose the original
local offset, so UTC is the only choice that maps a given asset to the *same*
``YYYY/MM`` on every run regardless of where/when the sync is run. A handful of
photos taken near a month boundary may land in the adjacent month; this is
accepted for v1 and documented in the README.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .models import AssetRef

UNKNOWN_DATE_DIR = "unknown-date"

# Path separators + control characters are the only things we strip; Unicode is
# preserved so filenames match what the user sees in Photos.
_UNSAFE = re.compile(r"[/\\\x00-\x1f]")


def safe_filename(name: str) -> str:
    """Make ``name`` safe to use as a single path component."""
    name = _UNSAFE.sub("_", name or "").strip().strip(".")
    # Avoid reserved bare names.
    if not name or name in {".", ".."}:
        return "unnamed"
    return name


def year_month(dt: datetime) -> tuple[str, str]:
    """Return ``("YYYY", "MM")`` in UTC, month zero-padded."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return f"{dt.year:04d}", f"{dt.month:02d}"


class PathResolver:
    """Resolves asset → relative/absolute destination paths under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def relative_dest(self, asset: AssetRef) -> Path:
        """Canonical (pre-collision) ``YYYY/MM/filename`` relative to root."""
        dt = asset.capture_dt or asset.added_dt
        fname = safe_filename(asset.filename)
        if dt is None:
            return Path(UNKNOWN_DATE_DIR) / fname
        yyyy, mm = year_month(dt)
        return Path(yyyy) / mm / fname

    @staticmethod
    def disambiguate(rel: Path, is_taken: Callable[[Path], bool]) -> Path:
        """Append ``-1``, ``-2``… before the extension until the path is free.

        ``is_taken(candidate)`` decides whether a relative path is already
        claimed by a *different* asset (checked against state + filesystem by
        the caller).
        """
        if not is_taken(rel):
            return rel
        stem, suffix, parent = rel.stem, rel.suffix, rel.parent
        n = 1
        while True:
            candidate = parent / f"{stem}-{n}{suffix}"
            if not is_taken(candidate):
                return candidate
            n += 1

    def absolute(self, rel: Path) -> Path:
        return self.root / rel

    def ensure_parent(self, abs_path: Path) -> None:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
