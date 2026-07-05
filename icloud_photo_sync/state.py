"""Durable sync manifest (C5).

A SQLite database, one row per asset, recording where it goes, its expected
size, partial progress, and status. This powers stop/resume (skip completed,
resume partials) and incremental update. WAL mode keeps it crash-safe: a hard
kill never leaves a corrupt DB.

The DB lives in the config dir (keyed by apple_id + output_root), so the photo
tree stays clean while each output folder keeps its own manifest.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import AssetRef

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id            TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    capture_dt    TEXT,
    added_dt      TEXT,
    dest_path     TEXT,
    expected_size INTEGER,
    bytes_done    INTEGER DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','completed','failed')),
    error         TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_dest ON assets(dest_path);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


class StateStore:
    """Transactional access to the SQLite manifest."""

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

    def close(self) -> None:
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- assets --------------------------------------------------------------

    def get(self, asset_id: str) -> sqlite3.Row | None:
        cur = self._conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,))
        return cur.fetchone()

    def register(self, asset: AssetRef, dest_rel: str) -> None:
        """Insert a new pending row, or refresh metadata on an existing one.

        ``dest_path``, ``bytes_done``, ``status`` and ``error`` are preserved on
        conflict so an asset never changes destination and progress is never
        reset just because we re-enumerated it. ``expected_size``, ``capture_dt``
        and ``added_dt`` are COALESCEd: a later enumeration that transiently
        fails to read them (NULL) must never erase known-good values — the
        stored size is the integrity ground truth for files on disk.
        """
        self._conn.execute(
            """
            INSERT INTO assets
                (id, filename, capture_dt, added_dt, dest_path,
                 expected_size, bytes_done, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, 'pending', ?)
            ON CONFLICT(id) DO UPDATE SET
                filename      = excluded.filename,
                capture_dt    = COALESCE(excluded.capture_dt, assets.capture_dt),
                added_dt      = COALESCE(excluded.added_dt, assets.added_dt),
                expected_size = COALESCE(excluded.expected_size, assets.expected_size),
                updated_at    = excluded.updated_at
            """,
            (
                asset.id,
                asset.filename,
                _iso(asset.capture_dt),
                _iso(asset.added_dt),
                dest_rel,
                asset.size,
                _now(),
            ),
        )
        self._conn.commit()

    def record_partial(self, asset_id: str, bytes_done: int) -> None:
        self._conn.execute(
            "UPDATE assets SET bytes_done = ?, status = 'pending', updated_at = ? "
            "WHERE id = ?",
            (bytes_done, _now(), asset_id),
        )
        self._conn.commit()

    def mark_completed(self, asset_id: str, size: int) -> None:
        self._conn.execute(
            "UPDATE assets SET status = 'completed', bytes_done = ?, "
            "expected_size = COALESCE(expected_size, ?), error = NULL, updated_at = ? "
            "WHERE id = ?",
            (size, size, _now(), asset_id),
        )
        self._conn.commit()

    def mark_failed(self, asset_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE assets SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error[:1000], _now(), asset_id),
        )
        self._conn.commit()

    def update_dest(self, asset_id: str, dest_rel: str) -> None:
        """Re-point an asset to a new destination (collision re-resolution)."""
        self._conn.execute(
            "UPDATE assets SET dest_path = ?, updated_at = ? WHERE id = ?",
            (dest_rel, _now(), asset_id),
        )
        self._conn.commit()

    def path_owner(self, dest_rel: str) -> str | None:
        """Asset id that already claims ``dest_rel``, or None."""
        cur = self._conn.execute(
            "SELECT id FROM assets WHERE dest_path = ? LIMIT 1", (dest_rel,)
        )
        row = cur.fetchone()
        return row["id"] if row else None

    # --- reporting -----------------------------------------------------------

    def counts(self) -> dict[str, int]:
        cur = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM assets GROUP BY status"
        )
        out = {"pending": 0, "completed": 0, "failed": 0}
        for row in cur:
            out[row["status"]] = row["n"]
        out["total"] = out["pending"] + out["completed"] + out["failed"]
        return out

    def total_bytes_completed(self) -> int:
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(bytes_done), 0) AS b FROM assets WHERE status = 'completed'"
        )
        return int(cur.fetchone()["b"])

    def iter_failed(self) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM assets WHERE status = 'failed' ORDER BY updated_at DESC"
        )
        return cur.fetchall()

    # --- meta ----------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        cur = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()
