"""Durable job store for the ``video-optimise`` command (C5-style).

One row per video, carried through select -> convert -> review -> upload ->
swap. A Ctrl-C at any point must be resumable by re-running the command, and
this store is what makes that true: every phase reads its input rows from
here rather than from memory, and every transition is a single committed
write. WAL mode keeps it crash-safe the same way :mod:`icloud_photo_sync.state`
is: a hard kill never leaves a corrupt DB, only a row stuck one phase behind.

The policy that decides *what* to convert and *whether* an output is good
enough lives in :mod:`icloud_photo_sync.video_optimise`; this module only
records the outcome of that policy being applied to one video.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .state import utc_now_iso

SCHEMA_VERSION = "3"

# --- status vocabulary ---------------------------------------------------

STATUS_SELECTED = "selected"                # chosen in the browser, not yet converted
STATUS_CONVERTED = "converted"              # encoded AND colour-verified AND smaller
STATUS_NOT_WORTH_IT = "not_worth_it"        # output not enough smaller; original kept
STATUS_COLOUR_MISMATCH = "colour_mismatch"  # output lost its colour; original kept
STATUS_CONVERT_FAILED = "convert_failed"    # ffmpeg failed
STATUS_REJECTED = "rejected"                # user chose to keep the original
STATUS_UPLOADED = "uploaded"                # replacement in iCloud, read back and verified
STATUS_SWAPPED = "swapped"                  # uploaded AND original verified deleted from iCloud
STATUS_SWAP_FAILED = "swap_failed"          # the upload or the delete did not complete

ALL_STATUSES = (
    STATUS_SELECTED,
    STATUS_CONVERTED,
    STATUS_NOT_WORTH_IT,
    STATUS_COLOUR_MISMATCH,
    STATUS_CONVERT_FAILED,
    STATUS_REJECTED,
    STATUS_UPLOADED,
    STATUS_SWAPPED,
    STATUS_SWAP_FAILED,
)

# Rows in one of these states represent completed (or user-decided) work; a
# re-``add()`` of the same ``rel`` must never clobber them back to 'selected'.
_TERMINAL_STATUSES = frozenset(
    {
        STATUS_CONVERTED,
        STATUS_NOT_WORTH_IT,
        STATUS_COLOUR_MISMATCH,
        STATUS_REJECTED,
        STATUS_UPLOADED,
        STATUS_SWAPPED,
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    -- COLLATE NOCASE because the photo volume is case-insensitive: a video
    -- converted from IMG_1234.MOV is written back as IMG_1234.mov (the output
    -- container is always QuickTime), and those are ONE file on disk. Keyed
    -- case-sensitively they became two rows, so a re-run saw the optimised file
    -- as an untracked video. Built-in NOCASE folds ASCII only, which covers
    -- every filename a camera produces; a non-ASCII near-miss would merely cost
    -- a duplicate row, and the "would not save enough" measurement still stops
    -- the file being converted twice.
    rel            TEXT PRIMARY KEY COLLATE NOCASE,   -- posix path under output_root
    asset_id       TEXT,               -- the ORIGINAL iCloud asset
    src_bytes      INTEGER NOT NULL,
    src_probe      TEXT,               -- json blob: the VideoProbe as recorded
    plan           TEXT,               -- json blob: the chosen Encode
    status         TEXT NOT NULL DEFAULT 'selected'
                   CHECK (status IN (
                       'selected','converted','not_worth_it','colour_mismatch',
                       'convert_failed','rejected','uploaded','swapped','swap_failed'
                   )),
    out_rel        TEXT,               -- converted file, relative to output_root
    out_bytes      INTEGER,
    out_probe      TEXT,               -- json blob: what the colour check saw
    new_asset_id   TEXT,               -- only ever set after a VERIFIED upload
    -- When the local side of this row was finished: original trashed and the
    -- conversion moved into its place. Without it the cleanup had no memory and
    -- re-offered every swapped row on every run -- and by then the path it
    -- checks holds the *replacement*, so it trashed the optimised file.
    local_done     TEXT,
    error          TEXT,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Identity of the account+folder this job store describes, stamped the same
# way :mod:`icloud_photo_sync.state` stamps its manifest.
IDENTITY_KEYS = ("apple_id", "output_root")

_now = utc_now_iso

# Mutations are batched for the same reason as the sync manifest: rows here
# are re-derivable by re-running the phase they belong to (the filesystem and
# iCloud hold the truth), so losing the last couple of seconds on a crash only
# costs re-doing a little work, never losing it silently.
COMMIT_EVERY_OPS = 200
COMMIT_EVERY_SECS = 2.0


def _dumps(value: dict | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


def _loads(text: str | None) -> dict | None:
    """Decode a stored JSON blob, tolerating NULL and corruption alike.

    A malformed blob is a reporting problem, not a reason to crash a resume:
    every caller of these helpers just wants "do I have this detail or not".
    """
    if not text:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def probe_of(row: sqlite3.Row) -> dict | None:
    return _loads(row["src_probe"])


def plan_of(row: sqlite3.Row) -> dict | None:
    return _loads(row["plan"])


def out_probe_of(row: sqlite3.Row) -> dict | None:
    return _loads(row["out_probe"])


class OptimiseJob:
    """Transactional access to the video-optimise job store."""

    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = Path(db_path)
        self.read_only = read_only
        if read_only:
            # Refuse to create. An empty auto-created store looks exactly like
            # "nothing selected yet", which is a silent lie for a phase that
            # expects a prior phase to have run.
            if not self.db_path.exists():
                raise FileNotFoundError(
                    f"No optimise job store at {self.db_path}. Nothing has been "
                    "selected for this Apple ID and folder yet."
                )
            self._conn = self._connect_read_only()
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA query_only=ON")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._migrate_rel_collation()
            self._migrate_add_local_done()
        self._dirty = 0
        self._last_commit = time.monotonic()
        if not read_only and self.get_meta("schema_version") != SCHEMA_VERSION:
            self.set_meta("schema_version", SCHEMA_VERSION)

    def _migrate_add_local_done(self) -> None:
        """Add ``local_done`` (schema 2 -> 3). Plain column add, no rebuild.

        Existing ``swapped`` rows are stamped as already done: their local
        cleanup either happened or was declined, and re-offering it is exactly
        the bug this column exists to prevent.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        if not cols or "local_done" in cols:
            return
        self._conn.execute("ALTER TABLE jobs ADD COLUMN local_done TEXT")
        self._conn.execute(
            "UPDATE jobs SET local_done = ? WHERE status = ?", (_now(), STATUS_SWAPPED))
        self._conn.commit()

    def _migrate_rel_collation(self) -> None:
        """Rebuild ``jobs`` with a case-insensitive key (schema 1 -> 2).

        SQLite cannot alter a column's collation in place, so the table is
        recreated and copied. Rows that differ only in case are collapsed to the
        furthest-along one: on a case-insensitive volume they always described a
        single file, and the more advanced row is the one carrying real work
        (a verified replacement id, a finished conversion).
        """
        cur = self._conn.execute("PRAGMA table_info(jobs)")
        cols = {row[1]: row for row in cur.fetchall()}
        if not cols:
            return                              # fresh database
        sql = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        if sql and "COLLATE NOCASE" in (sql[0] or ""):
            return                              # already migrated

        rank = {s: i for i, s in enumerate((
            STATUS_CONVERT_FAILED, STATUS_SWAP_FAILED, STATUS_SELECTED,
            STATUS_COLOUR_MISMATCH, STATUS_NOT_WORTH_IT, STATUS_REJECTED,
            STATUS_CONVERTED, STATUS_UPLOADED, STATUS_SWAPPED))}
        keep: dict[str, sqlite3.Row] = {}
        for row in self._conn.execute("SELECT * FROM jobs"):
            key = row["rel"].casefold()
            best = keep.get(key)
            if best is None or (rank.get(row["status"], -1), row["updated_at"] or "") > \
                    (rank.get(best["status"], -1), best["updated_at"] or ""):
                keep[key] = row

        names = [c for c in cols]
        placeholders = ",".join("?" * len(names))
        self._conn.execute("ALTER TABLE jobs RENAME TO jobs_old")
        self._conn.executescript(_SCHEMA)
        self._conn.executemany(
            f"INSERT INTO jobs ({','.join(names)}) VALUES ({placeholders})",
            [tuple(row[n] for n in names) for row in keep.values()],
        )
        self._conn.execute("DROP TABLE jobs_old")
        self._conn.commit()

    def _connect_read_only(self) -> sqlite3.Connection:
        """Open without the ability to write, coping with WAL.

        Mirrors :meth:`icloud_photo_sync.state.StateStore._connect_read_only`:
        ``mode=ro`` cannot open a WAL database missing its ``-shm`` sidecar
        (SQLite needs to create it, and a read-only connection may not), so
        that case falls back to ``query_only``, which still refuses writes.
        """
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        try:
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

    def __enter__(self) -> "OptimiseJob":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- jobs ------------------------------------------------------------

    def get(self, rel: str) -> sqlite3.Row | None:
        cur = self._conn.execute("SELECT * FROM jobs WHERE rel = ?", (rel,))
        return cur.fetchone()

    def add(
        self,
        rel: str,
        *,
        asset_id: str | None,
        src_bytes: int,
        src_probe: dict | None,
        plan: dict | None,
    ) -> None:
        """Insert or replace a ``selected`` row.

        Idempotent with respect to completed work: re-selecting a video whose
        row is already in a terminal state (``converted``, ``uploaded``,
        ``swapped``, ``rejected``, ``not_worth_it``, ``colour_mismatch``) is a
        no-op — resetting it to ``selected`` would throw away real progress
        (or a user's decision) on every resume, since ``add()`` is exactly
        what a re-run of the selection phase calls for every candidate it
        sees again. A row left in ``convert_failed`` or ``swap_failed`` *is*
        overwritten back to ``selected``, because those states exist so a
        retry is possible.
        """
        existing = self.get(rel)
        if existing is not None and existing["status"] in _TERMINAL_STATUSES:
            return
        self._conn.execute(
            """
            INSERT INTO jobs
                (rel, asset_id, src_bytes, src_probe, plan, status, updated_at)
            VALUES (?, ?, ?, ?, ?, 'selected', ?)
            ON CONFLICT(rel) DO UPDATE SET
                asset_id    = excluded.asset_id,
                src_bytes   = excluded.src_bytes,
                src_probe   = excluded.src_probe,
                plan        = excluded.plan,
                status      = 'selected',
                out_rel     = NULL,
                out_bytes   = NULL,
                out_probe   = NULL,
                error       = NULL,
                updated_at  = excluded.updated_at
            """,
            (rel, asset_id, src_bytes, _dumps(src_probe), _dumps(plan), _now()),
        )
        self._maybe_commit()

    def mark_converted(self, rel: str, *, out_rel: str, out_bytes: int, out_probe: dict | None) -> None:
        self._conn.execute(
            """
            UPDATE jobs SET status = 'converted', out_rel = ?, out_bytes = ?,
                            out_probe = ?, error = NULL, updated_at = ?
            WHERE rel = ?
            """,
            (out_rel, out_bytes, _dumps(out_probe), _now(), rel),
        )
        self._maybe_commit()

    def mark_skipped(self, rel: str, status: str, error: str = "") -> None:
        """Record why a video was not converted (or why the encode failed).

        ``status`` must be one of ``not_worth_it``, ``colour_mismatch`` or
        ``convert_failed`` — the three ways the convert phase can decline to
        produce a usable replacement while leaving the original untouched.
        """
        if status not in (STATUS_NOT_WORTH_IT, STATUS_COLOUR_MISMATCH, STATUS_CONVERT_FAILED):
            raise ValueError(f"not a skip status: {status!r}")
        self._conn.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE rel = ?",
            (status, error[:1000] if error else None, _now(), rel),
        )
        self._maybe_commit()

    def mark_rejected(self, rel: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = 'rejected', updated_at = ? WHERE rel = ?",
            (_now(), rel),
        )
        self._maybe_commit()

    def mark_uploaded(self, rel: str, new_asset_id: str) -> None:
        """Record a verified upload.

        Raises ``ValueError`` on an empty ``new_asset_id``: an ``uploaded``
        row is the only evidence the swap engine has that a replacement
        exists in iCloud, so recording one without an id would let the swap
        phase delete an original with nothing to point at in its place.
        """
        if not new_asset_id:
            raise ValueError("mark_uploaded requires a non-empty new_asset_id")
        self._conn.execute(
            """
            UPDATE jobs SET status = 'uploaded', new_asset_id = ?, error = NULL,
                            updated_at = ?
            WHERE rel = ?
            """,
            (new_asset_id, _now(), rel),
        )
        self._maybe_commit()

    def mark_swapped(self, rel: str) -> None:
        """Record that the original has been verified deleted from iCloud.

        Raises ``ValueError`` if the row has no ``new_asset_id`` yet — this is
        the ordering fence at the storage layer: a row cannot become
        ``swapped`` without first having passed through :meth:`mark_uploaded`,
        so a caller cannot delete an original before its replacement is known
        to exist, even by mistake.
        """
        row = self.get(rel)
        if row is None or not row["new_asset_id"]:
            raise ValueError(f"cannot swap {rel!r}: no verified new_asset_id on record")
        self._conn.execute(
            "UPDATE jobs SET status = 'swapped', error = NULL, updated_at = ? WHERE rel = ?",
            (_now(), rel),
        )
        self._maybe_commit()

    def set_asset_id(self, rel: str, asset_id: str) -> bool:
        """Fill in the *original's* iCloud id on a row that has none yet.

        A ``--no-upload`` run resolves no Apple ID, so it records no asset ids.
        If the same job is later resumed online, its converted rows would
        otherwise be unswappable forever — :meth:`add` deliberately refuses to
        reset a converted row, so the id can never arrive by that route. This is
        the one way it can, and it only ever fills a blank: an id already on a
        row is evidence from a run that *did* have the manifest, and must not be
        overwritten by a later guess.
        """
        if not asset_id:
            return False
        cur = self._conn.execute(
            "UPDATE jobs SET asset_id = ?, updated_at = ? "
            "WHERE rel = ? AND (asset_id IS NULL OR asset_id = '')",
            (asset_id, _now(), rel),
        )
        self._maybe_commit()
        return cur.rowcount > 0

    def mark_swap_failed(self, rel: str, error: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = 'swap_failed', error = ?, updated_at = ? WHERE rel = ?",
            (error[:1000] if error else None, _now(), rel),
        )
        self._maybe_commit()

    def set_out_rel(self, rel: str, out_rel: str) -> None:
        """Repoint a row at a moved conversion (see the work-dir migration)."""
        self._conn.execute(
            "UPDATE jobs SET out_rel = ?, updated_at = ? WHERE rel = ?",
            (out_rel, _now(), rel),
        )
        self._maybe_commit()

    def reset_swap_failed(self) -> int:
        """Send every ``swap_failed`` row back to ``converted`` for another try.

        Without this, a failed swap strands the row forever: the convert phase
        only looks at ``selected``, the swap phase only at ``converted`` and
        ``uploaded``, so "re-run to retry" — which the failure message promises —
        would find nothing to do and exit having converted 0 videos.

        ``new_asset_id`` is deliberately left in place. A row whose upload
        succeeded but whose delete was refused keeps its verified replacement,
        and the swap engine sees the id and skips straight to the delete rather
        than uploading the same bytes twice.
        """
        cur = self._conn.execute(
            "UPDATE jobs SET status = 'converted', updated_at = ? "
            "WHERE status = 'swap_failed'",
            (_now(),),
        )
        self._maybe_commit()
        return cur.rowcount

    # --- queries -----------------------------------------------------------

    def pending_conversion(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM jobs WHERE status = 'selected' ORDER BY src_bytes DESC"
        ).fetchall()

    def converted(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM jobs WHERE status = 'converted' ORDER BY src_bytes DESC"
        ).fetchall()

    def approved(self) -> list[sqlite3.Row]:
        """Rows ready for the swap phase: converted, or already uploaded.

        Including ``uploaded`` rows is what makes a resumed run skip a
        re-upload but still go on to verify-and-delete the original.
        """
        return self._conn.execute(
            "SELECT * FROM jobs WHERE status IN ('converted', 'uploaded') "
            "ORDER BY src_bytes DESC"
        ).fetchall()

    def needs_local_cleanup(self) -> list[sqlite3.Row]:
        """Swapped rows whose local side has not been finished yet.

        The cleanup is offered from here rather than from :meth:`swapped` so it
        is asked once per video, ever.
        """
        return self._conn.execute(
            "SELECT * FROM jobs WHERE status = 'swapped' AND local_done IS NULL "
            "ORDER BY updated_at"
        ).fetchall()

    def mark_local_done(self, rel: str) -> None:
        """Record that this row's original is gone and its conversion is placed."""
        self._conn.execute(
            "UPDATE jobs SET local_done = ?, updated_at = ? WHERE rel = ?",
            (_now(), _now(), rel),
        )
        self._maybe_commit()

    def swapped(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM jobs WHERE status = 'swapped' ORDER BY updated_at"
        ).fetchall()

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        return self._conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY updated_at",
            statuses,
        ).fetchall()

    # --- reporting -----------------------------------------------------------

    def counts(self) -> dict[str, int]:
        cur = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        out = {status: 0 for status in ALL_STATUSES}
        for row in cur:
            out[row["status"]] = row["n"]
        return out

    def totals(self) -> dict[str, int]:
        """Bytes freed, summed over ``swapped`` rows only.

        Anything short of ``swapped`` has not actually released space yet:
        ``converted``/``uploaded`` still have the original sitting in iCloud,
        so counting them here would overstate what has actually been freed.
        """
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(src_bytes), 0) AS src, COALESCE(SUM(out_bytes), 0) AS out_ "
            "FROM jobs WHERE status = 'swapped'"
        )
        row = cur.fetchone()
        src_bytes = int(row["src"])
        out_bytes = int(row["out_"])
        return {"src_bytes": src_bytes, "out_bytes": out_bytes, "freed": src_bytes - out_bytes}

    def clear(self) -> None:
        """Drop every row, for ``--restart``. ``meta`` (identity, schema) survives."""
        self._conn.execute("DELETE FROM jobs")
        self.flush()

    # --- identity ------------------------------------------------------------

    def stamp_identity(self, **fields: str | None) -> None:
        for key, value in fields.items():
            if key not in IDENTITY_KEYS:
                raise KeyError(f"unknown identity field: {key!r}")
            if value:
                self.set_meta(key, str(value))
        self.flush()

    def identity(self) -> dict[str, str]:
        out = {}
        for key in IDENTITY_KEYS:
            value = self.get_meta(key)
            if value is not None:
                out[key] = value
        return out

    # --- meta ------------------------------------------------------------

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
