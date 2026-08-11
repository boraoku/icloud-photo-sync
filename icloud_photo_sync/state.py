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
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .errors import ManifestMissingError
from .models import AssetRef

# v2 adds `remote_deletions` and the identity keys in `meta`. Purely additive:
# every statement is IF NOT EXISTS, so a v1 database gains them on open and a
# v2 database still opens fine under code that knows nothing about them.
SCHEMA_VERSION = "2"

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

-- Assets this tool asked iCloud to move to Recently Deleted. Deliberately a
-- separate table rather than an `assets.status` value: that column means
-- "download state" and widening its CHECK constraint would put the sync path
-- at risk for no gain. Rows here make a re-run idempotent.
CREATE TABLE IF NOT EXISTS remote_deletions (
    asset_id      TEXT PRIMARY KEY,
    dest_path     TEXT,
    filename      TEXT,
    capture_dt    TEXT,
    expected_size INTEGER,
    deleted_at    TEXT,
    verified_at   TEXT,
    receipt_path  TEXT,
    status        TEXT NOT NULL DEFAULT 'deleted'
);
"""

# Identity of the account+folder this manifest describes, stamped on every
# authenticated run. The database *filename* encodes the same pair as a hash,
# but a hash cannot be read back — and "which library am I about to mutate?"
# deserves an answer that can be checked, not one that can only be recomputed.
IDENTITY_KEYS = ("apple_id", "output_root", "dsid", "account_name")


def utc_now_iso() -> str:
    """Timestamp format shared by every column in this database."""
    return datetime.now(timezone.utc).isoformat()


_now = utc_now_iso


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def fold_dest(dest_rel: str) -> str:
    """Fold a relative path to the identity APFS actually gives a file.

    Case-insensitive and normalisation-insensitive, so two manifest rows that
    differ only that way fold together — which is the point: on disk they are
    one file.
    """
    return unicodedata.normalize("NFC", dest_rel).casefold()


def _dest_forms(dest_rel: str) -> list[str]:
    """The spellings ``dest_rel`` may have been stored as, most likely first."""
    forms = [dest_rel,
             unicodedata.normalize("NFC", dest_rel),
             unicodedata.normalize("NFD", dest_rel)]
    return list(dict.fromkeys(forms))


_IN_CHUNK = 400   # ids per SQL `IN (...)`; well under SQLite's variable limit


# Mutations are batched: progress rows are advisory (the filesystem holds the
# truth — completed files by their size, partials by their .part), so losing
# the last couple of seconds on a crash only costs re-verifying a few files.
COMMIT_EVERY_OPS = 200
COMMIT_EVERY_SECS = 2.0


class StateStore:
    """Transactional access to the SQLite manifest."""

    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = Path(db_path)
        self.read_only = read_only
        if read_only:
            # Refuse to create. An empty auto-created manifest is the signature
            # of "wrong Apple ID or wrong folder", and callers that are about to
            # delete things need that to be a loud error, not a zero-row answer.
            if not self.db_path.exists():
                raise ManifestMissingError(
                    f"No sync manifest at {self.db_path}. Nothing has been synced "
                    "for this Apple ID and folder, so no file here can be traced "
                    "back to an iCloud asset."
                )
            self._conn = self._connect_read_only()
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=30000")
            # Enforced at the SQL layer as well as (usually) the OS one, so the
            # fallback below is no weaker in practice: writes still raise.
            self._conn.execute("PRAGMA query_only=ON")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._dirty = 0
        self._last_commit = time.monotonic()
        if not read_only and self.get_meta("schema_version") != SCHEMA_VERSION:
            self.set_meta("schema_version", SCHEMA_VERSION)

    def _connect_read_only(self) -> sqlite3.Connection:
        """Open without the ability to write, coping with WAL.

        ``mode=ro`` is the stronger guarantee, but a WAL database whose ``-shm``
        sidecar is absent cannot be opened that way at all: SQLite has to create
        the shared-memory file and a read-only connection may not. That is the
        normal state of a manifest after a reboot or a restore, so falling back
        to ``query_only`` — which still refuses every write — is the difference
        between the feature working and it failing to open at all.
        """
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        try:
            # connect() is lazy: a WAL database it cannot open only fails here.
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            return conn
        except sqlite3.OperationalError:
            conn.close()
            return sqlite3.connect(str(self.db_path), timeout=30.0)

    def _maybe_commit(self) -> None:
        self._dirty += 1
        now = time.monotonic()
        if self._dirty >= COMMIT_EVERY_OPS or now - self._last_commit >= COMMIT_EVERY_SECS:
            self.flush()

    def flush(self) -> None:
        """Commit any batched mutations."""
        self._conn.commit()
        self._dirty = 0
        self._last_commit = time.monotonic()

    def close(self) -> None:
        try:
            self.flush()
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
        self._maybe_commit()

    def record_partial(self, asset_id: str, bytes_done: int) -> None:
        self._conn.execute(
            "UPDATE assets SET bytes_done = ?, status = 'pending', updated_at = ? "
            "WHERE id = ?",
            (bytes_done, _now(), asset_id),
        )
        self._maybe_commit()

    def mark_completed(self, asset_id: str, size: int) -> None:
        self._conn.execute(
            "UPDATE assets SET status = 'completed', bytes_done = ?, "
            "expected_size = COALESCE(expected_size, ?), error = NULL, updated_at = ? "
            "WHERE id = ?",
            (size, size, _now(), asset_id),
        )
        self._maybe_commit()

    def mark_failed(self, asset_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE assets SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error[:1000], _now(), asset_id),
        )
        self._maybe_commit()

    def update_dest(self, asset_id: str, dest_rel: str) -> None:
        """Re-point an asset to a new destination (collision re-resolution)."""
        self._conn.execute(
            "UPDATE assets SET dest_path = ?, updated_at = ? WHERE id = ?",
            (dest_rel, _now(), asset_id),
        )
        self._maybe_commit()

    def path_owner(self, dest_rel: str) -> str | None:
        """Asset id that already claims ``dest_rel``, or None.

        Answers "is this name taken?" for download planning, where any one
        claimant is answer enough. Do **not** reuse it to decide what a local
        file *is*: its ``LIMIT 1`` hides a second claimant, and deletion has to
        see that. Use :meth:`rows_for_dest`.
        """
        cur = self._conn.execute(
            "SELECT id FROM assets WHERE dest_path = ? LIMIT 1", (dest_rel,)
        )
        row = cur.fetchone()
        return row["id"] if row else None

    def rows_for_dest(self, dest_rel: str) -> list[sqlite3.Row]:
        """Every asset row claiming ``dest_rel`` — all of them, deliberately.

        Returns whole rows because a caller deciding whether a file may be
        deleted needs the corroborating columns (status, size, filename, capture
        date), not just an id. macOS writes both NFC and NFD spellings, so each
        normalisation is tried.
        """
        forms = _dest_forms(dest_rel)
        placeholders = ",".join("?" * len(forms))
        cur = self._conn.execute(
            f"SELECT * FROM assets WHERE dest_path IN ({placeholders})", forms
        )
        return list(cur)

    def colliding_dest_paths(self) -> set[str]:
        """Case/normalisation-folded dest_paths claimed by more than one asset.

        One scan, once per run. APFS is case-insensitive by default and macOS
        mixes Unicode normalisations, so two rows differing only that way name a
        *single* file on disk — an ambiguity that an exact-match lookup cannot
        see. Callers fold a path the same way (:func:`fold_dest`) to test it.
        """
        counts: dict[str, int] = {}
        cur = self._conn.execute(
            "SELECT dest_path FROM assets WHERE dest_path IS NOT NULL"
        )
        for row in cur:
            key = fold_dest(row["dest_path"])
            counts[key] = counts.get(key, 0) + 1
        return {key for key, n in counts.items() if n > 1}

    # --- remote (iCloud) deletions -------------------------------------------

    def record_remote_deletion(
        self,
        *,
        asset_id: str,
        dest_path: str | None,
        filename: str | None,
        capture_dt: str | None,
        expected_size: int | None,
        receipt_path: str | None,
        verified_at: str | None,
        status: str = "deleted",
    ) -> None:
        """Record a *verified* remote deletion, committing immediately.

        Unlike download progress — which the filesystem can always re-derive —
        this is the only local record that an irreversible remote effect
        happened, so it is never left sitting in a batch.
        """
        self._conn.execute(
            """
            INSERT OR REPLACE INTO remote_deletions
                (asset_id, dest_path, filename, capture_dt, expected_size,
                 deleted_at, verified_at, receipt_path, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (asset_id, dest_path, filename, capture_dt, expected_size,
             _now(), verified_at, receipt_path, status),
        )
        self.flush()

    def _has_deletions_table(self) -> bool:
        """A manifest written before this feature has no table — and no deletions.

        Read-only opens cannot create it, and that is the normal case: planning a
        deletion reads the manifest without writing to it.
        """
        cur = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='remote_deletions'"
        )
        return cur.fetchone() is not None

    def remote_deleted_ids(self, asset_ids: Iterable[str]) -> set[str]:
        """Which of ``asset_ids`` this tool has already deleted from iCloud."""
        ids = list(asset_ids)
        if not ids or not self._has_deletions_table():
            return set()
        found: set[str] = set()
        for start in range(0, len(ids), _IN_CHUNK):
            chunk = ids[start:start + _IN_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            cur = self._conn.execute(
                f"SELECT asset_id FROM remote_deletions "
                f"WHERE status = 'deleted' AND asset_id IN ({placeholders})",
                chunk,
            )
            found.update(row["asset_id"] for row in cur)
        return found

    def remote_deletion_count(self) -> int:
        if not self._has_deletions_table():
            return 0
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM remote_deletions WHERE status = 'deleted'"
        )
        return int(cur.fetchone()["n"])

    # --- identity ------------------------------------------------------------

    def stamp_identity(self, **fields: str | None) -> None:
        """Record whose library and which folder this manifest describes."""
        for key, value in fields.items():
            if key not in IDENTITY_KEYS:
                raise KeyError(f"unknown identity field: {key!r}")
            if value:
                self.set_meta(key, str(value))
        self.flush()

    def identity(self) -> dict[str, str]:
        """The stamped identity; keys are absent when never stamped."""
        out = {}
        for key in IDENTITY_KEYS:
            value = self.get_meta(key)
            if value is not None:
                out[key] = value
        return out

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

    def iter_completed(self) -> list[sqlite3.Row]:
        """Every completed row, ordered by destination.

        Used by ``retro_clean`` to reconcile the manifest against the tree. A
        real library is tens of thousands of rows, which is a few MB — small
        enough to hold, and holding it avoids a cursor open across the whole
        filesystem walk that follows.
        """
        return self._conn.execute(
            "SELECT * FROM assets WHERE status = 'completed' ORDER BY dest_path"
        ).fetchall()

    def iter_failed(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM assets WHERE status = 'failed' ORDER BY updated_at DESC"
        if limit is not None:
            return self._conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
        return self._conn.execute(sql).fetchall()

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
        self.flush()  # meta writes mark pass boundaries — persist everything
