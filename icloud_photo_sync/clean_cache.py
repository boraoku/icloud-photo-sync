"""Classification cache for ``local-clean``.

A sidecar SQLite database, one row per image, recording the vision model's
verdict keyed by ``(path, size, mtime_ns, model)``. This makes classification
resumable: a re-run skips every unchanged, already-classified file, and — since
each verdict is committed the moment it lands — a Ctrl-C never loses work.

Structurally this mirrors :class:`icloud_photo_sync.state.StateStore` (WAL,
busy_timeout, meta table, context manager) but commits per write. At ~15s per
classification a commit per row is free, and loss-free resume is worth it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .classifier import Classification

if TYPE_CHECKING:
    from .local_clean import ImageFile

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS classifications (
    path          TEXT PRIMARY KEY,
    size          INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    model         TEXT NOT NULL,
    category      TEXT NOT NULL
                  CHECK (category IN ('screenshot','meme','photo','other')),
    confidence    REAL,
    reason        TEXT,
    classified_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CleanCache:
    """Transactional access to the classification cache."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", SCHEMA_VERSION)

    def get(self, img: "ImageFile", model: str) -> Classification | None:
        """Cached verdict for ``img`` under ``model``, or None.

        A hit requires ``(path, size, mtime_ns, model)`` to all match, so an
        edited file or a switched model reclassifies automatically.
        """
        row = self._conn.execute(
            "SELECT category, confidence, reason FROM classifications "
            "WHERE path = ? AND size = ? AND mtime_ns = ? AND model = ?",
            (img.rel, img.size, img.mtime_ns, model),
        ).fetchone()
        if row is None:
            return None
        return Classification(
            category=row["category"],
            confidence=row["confidence"],
            reason=row["reason"] or "",
        )

    def put(self, img: "ImageFile", model: str, c: Classification) -> None:
        """Upsert a verdict and commit immediately (loss-free resume)."""
        self._conn.execute(
            """
            INSERT INTO classifications
                (path, size, mtime_ns, model, category, confidence, reason, classified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size          = excluded.size,
                mtime_ns      = excluded.mtime_ns,
                model         = excluded.model,
                category      = excluded.category,
                confidence    = excluded.confidence,
                reason        = excluded.reason,
                classified_at = excluded.classified_at
            """,
            (
                img.rel,
                img.size,
                img.mtime_ns,
                model,
                c.category,
                c.confidence,
                c.reason,
                _now(),
            ),
        )
        self._conn.commit()

    def remove(self, rels: Iterable[str]) -> None:
        """Drop rows for the given relative paths (called after trashing)."""
        rels = list(rels)
        if not rels:
            return
        self._conn.executemany(
            "DELETE FROM classifications WHERE path = ?", ((r,) for r in rels)
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CleanCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
