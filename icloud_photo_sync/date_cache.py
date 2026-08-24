"""Resume cache for ``photo-optimise-external``'s date-recovery phase.

A sidecar SQLite database, one row per photo, recording what the last run
concluded about that file's capture date, keyed by ``(rel, size, mtime_ns)``.
Without it, stopping a 47,000-photo run partway and re-running redid every
photo from the start; with it, a re-run skips straight past everything
already settled and never spawns ``exiftool`` for those files at all.

Structurally this mirrors :class:`icloud_photo_sync.clean_cache.CleanCache`
(WAL, busy_timeout, meta table, context manager). The one deliberate
difference is commit granularity: a classification costs ~15 seconds, so that
cache commits per row, whereas a batched date read costs ~2.5ms per file, so
committing per row would cost more than the work it protects. Here the caller
commits per batch — a Ctrl-C loses at most one batch, seconds of work.

**Which outcomes are worth remembering** is a correctness question, not a
performance one, and the answer differs per outcome — see :data:`TERMINAL`.
A file that was read successfully will give the same answer next time, so it
is cached; a file whose read or write *failed* may well succeed on the next
run (the tool gets installed, the disk frees up), so it is deliberately never
cached and is always retried.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1"

# --- outcome vocabulary ------------------------------------------------------

OUTCOME_ALREADY_PRESENT = "already_present"
OUTCOME_STAMPED_FILENAME = "stamped_filename"
OUTCOME_STAMPED_FOLDER = "stamped_folder"
OUTCOME_UNKNOWN = "unknown"

TERMINAL = frozenset({
    OUTCOME_ALREADY_PRESENT,
    OUTCOME_STAMPED_FILENAME,
    OUTCOME_STAMPED_FOLDER,
    OUTCOME_UNKNOWN,
})
"""Outcomes a later run can trust without re-reading the file.

``unknown`` is in here on purpose. It means "no embedded date, and neither
the filename nor the enclosing folder carries one either" — all three inputs
are properties of a file that has not changed, so re-asking is guaranteed to
get the same answer. Should any of them change, the key changes with it: a
rename or a move changes ``rel``, and an edit changes ``size``/``mtime_ns``.
``--recheck-dates`` exists for the case where the *rules* changed instead.

Failures are deliberately absent: ``exiftool`` missing, a full disk, a
locked file are all transient, and caching them would make a run that fixed
the underlying problem look like it had nothing to do.
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS date_checks (
    -- COLLATE NOCASE for the same reason optimise_job does it: the photo
    -- volume is case-insensitive, so IMG_1.HEIC and IMG_1.heic are one file
    -- and must never occupy two rows with two different verdicts.
    rel        TEXT PRIMARY KEY COLLATE NOCASE,
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    outcome    TEXT NOT NULL
               CHECK (outcome IN ('already_present','stamped_filename',
                                  'stamped_folder','unknown')),
    checked_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DateCache:
    """Transactional access to the date-recovery cache."""

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

    def get(self, rel: str, size: int, mtime_ns: int) -> str | None:
        """The cached outcome for this exact file, or ``None`` for a miss.

        A hit requires size and mtime to match too, so a photo edited since
        it was checked is re-checked rather than trusted.
        """
        row = self._conn.execute(
            "SELECT outcome FROM date_checks "
            "WHERE rel = ? AND size = ? AND mtime_ns = ?",
            (rel, size, mtime_ns),
        ).fetchone()
        return row["outcome"] if row else None

    def put(self, rel: str, size: int, mtime_ns: int, outcome: str) -> None:
        """Record one file's outcome. Does NOT commit — see :meth:`flush`.

        ``size``/``mtime_ns`` must be the file's state *after* any stamping,
        not before: stamping rewrites the file and then sets its mtime to the
        capture date, so recording the pre-write values would guarantee a
        cache miss on the very next run and re-read every stamped file.

        A non-terminal outcome is silently ignored rather than rejected —
        "don't remember failures" is this module's policy, and making every
        caller restate it invites one of them getting it wrong.
        """
        if outcome not in TERMINAL:
            return
        self._conn.execute(
            """
            INSERT INTO date_checks (rel, size, mtime_ns, outcome, checked_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(rel) DO UPDATE SET
                size       = excluded.size,
                mtime_ns   = excluded.mtime_ns,
                outcome    = excluded.outcome,
                checked_at = excluded.checked_at
            """,
            (rel, size, mtime_ns, outcome, _now()),
        )

    def flush(self) -> None:
        """Commit everything staged since the last flush.

        Called once per batch rather than per file: a batched read costs
        ~2.5ms per file, so a commit per row would dominate the very work
        this cache exists to avoid. One batch is the most a Ctrl-C can lose.
        """
        self._conn.commit()

    def clear(self) -> None:
        """Forget every verdict — what ``--recheck-dates`` runs."""
        self._conn.execute("DELETE FROM date_checks")
        self._conn.commit()

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM date_checks").fetchone()
        return row["n"] if row else 0

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
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> "DateCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
