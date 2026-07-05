"""Shared, engine-agnostic data types.

``AssetRef`` is the lightweight value object the orchestrator/downloader/state
work with. It deliberately does NOT import ``pyicloud`` so the rest of the
program stays decoupled from the iCloud engine; the opaque ``raw`` handle is
only ever touched by :mod:`icloud_photo_sync.icloud_client`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


@dataclass
class AssetRef:
    """A single photo/video asset, normalised across iCloud engines."""

    id: str
    """Stable unique id from iCloud. Primary key for state + de-duplication."""

    filename: str
    """Original filename, e.g. ``IMG_1234.HEIC`` (may collide across assets)."""

    capture_dt: datetime | None
    """Capture/created date used for the ``YYYY/MM`` folder. ``None`` if unknown."""

    added_dt: datetime | None
    """When the asset was added to iCloud. Ordering only — never for folders."""

    size: int | None
    """Byte size of the ``original`` rendition, if iCloud reported it."""

    raw: Any = field(default=None, repr=False, compare=False)
    """Opaque handle to the underlying engine asset. Only the client touches it."""


class DownloadOutcome(str, Enum):
    """Result of attempting to download a single asset."""

    SKIPPED = "skipped"      # already complete on disk
    DOWNLOADED = "downloaded"
    FAILED = "failed"        # gave up after retries; recorded for next run
    CANCELLED = "cancelled"  # user stopped mid-transfer
