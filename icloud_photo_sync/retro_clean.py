"""Reconstructing which files a past clean session trashed, after the fact.

``--icloud-delete`` measures each file's size microseconds before Finder takes
it. A session run without the flag records nothing, so for files trashed earlier
that measurement does not exist and cannot be recovered — the file is gone.

This module assembles what evidence *does* survive, so that
:func:`icloud_photo_sync.icloud_delete.build_plan` can weigh it as an explicitly
weaker class. All the I/O lives here; the rules live in ``icloud_delete``, which
stays pure and importable without an account.

Four independent sources, none of which needs the file to still exist:

``last_full_pass_at`` + an all-completed manifest
    ``run_full`` only skips an asset without writing when ``_on_disk_ok``
    confirms the file present *at its expected size*; everything else ends
    ``completed`` (file written) or ``failed``. So a finished pass over a
    manifest with no pending and no failed rows is a moment at which every
    completed row was provably on disk. That timestamp bounds the window in
    which the files can have been removed.

the scan envelope
    A file outside every clean command's scan envelope was never shown to the
    user by this tool, so a clean session cannot explain its absence. This is a
    filter, not an argument: most files inside the envelope were never offered
    either. Its real work is the tripwire — if anything *outside* the envelope
    is missing too, something other than a clean session removed files and the
    whole reconstruction is unsafe.

the rotating log
    ``review.py`` logs every ``POST /trash`` at DEBUG. That gives the number and
    the timing of trash rounds inside the window, and — because rotation is
    size-based and therefore contiguous — whether the log covers the window at
    all. It never records *which* files, only that a round happened.

the classification cache
    ``local_clean`` deletes a file's cache row when it trashes it, so a missing
    file whose row survived was not trashed by ``local-clean``. The converse is
    not evidence: an unclassified file also has no row.

This module deliberately imports neither ``auth`` nor ``icloud_client``: nothing
here should be able to reach the network.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from .config import IMAGE_SUFFIXES, VIDEO_SUFFIXES
from .icloud_delete import (
    SKIP_ELSEWHERE,
    SKIP_NOT_CLASSIFIED,
    SKIP_STILL_CLASSIFIED,
    RetroEvidence,
    Skip,
)
from .logutil import get_logger
from .state import StateStore

logger = get_logger(__name__)

LOG_BASENAME = "icloud-photo-sync.log"
_LOG_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_TRASH_LINE = "POST /trash"
# A log line's timestamp is naive local time (logging's asctime), while the
# manifest's is UTC. Never compare the two without attaching the local offset.
_LOG_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

ENVELOPE_LOCAL_CLEAN = "local-clean"
ENVELOPE_VIDEO_CLEAN = "video-clean"


# --- the manifest's own verified moment ----------------------------------------


def verified_present_at(state: StateStore) -> tuple[str | None, str]:
    """When every completed row was last provably on disk. ``(when, why_not)``.

    ``last_full_pass_at`` alone is not enough: it is written whenever the
    enumeration loop finishes, even if downloads inside it failed. Pairing it
    with an all-completed manifest is what makes it mean "the tree was whole".
    """
    stamp = state.get_meta("last_full_pass_at")
    if not stamp:
        return None, ("this folder's manifest has never recorded a completed full "
                      "pass, so a file being absent says nothing about whether it "
                      "was ever downloaded. Run `icloud-photo-sync sync` first.")
    counts = state.counts()
    unfinished = counts["pending"] + counts["failed"]
    if unfinished:
        return None, (f"{unfinished} of this folder's {counts['total']} tracked "
                      f"assets are pending or failed, so the last pass did not "
                      "leave the tree whole and absence cannot be read as deletion.")
    return stamp, ""


def missing_completed_rows(
    state: StateStore, output_root: Path,
) -> list[sqlite3.Row]:
    """Completed rows with nothing at ``dest_path``."""
    root = Path(output_root)
    return [row for row in state.iter_completed()
            if row["dest_path"] and not (root / row["dest_path"]).exists()]


# --- the scan envelope ----------------------------------------------------------


def envelope_for(
    rel: str, size: int | None, *, max_bytes: int, min_bytes: int,
) -> str | None:
    """Which clean command's scan could have shown this file, if any.

    The thresholds are the *current* defaults or whatever the user declares —
    a past session's ``--max-size`` was never recorded anywhere, so this is an
    assumption and the caller is expected to print it.

    Note how much weaker this is for video: ``video-clean``'s default
    ``--min-size`` is 0, so it really does list every video in the tree, and the
    envelope honestly admits all of them. For images the 1 MiB ceiling excludes
    most of the library. The caller warns when that asymmetry is load-bearing.
    """
    suffix = Path(rel).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        # local-clean filters on the real file size; expected_size is the best
        # surviving proxy for it and the two agreed at download time.
        if size is not None and size > max_bytes:
            return None
        return ENVELOPE_LOCAL_CLEAN
    if suffix in VIDEO_SUFFIXES:
        if size is not None and size < min_bytes:
            return None
        return ENVELOPE_VIDEO_CLEAN
    return None


# --- the classification cache ---------------------------------------------------


def cache_vetoes(cache_db: Path, rels: Sequence[str]) -> dict[str, Skip]:
    """Disqualify files the classification cache argues against.

    Two directions, both one-way:

    * a *surviving* row means ``local_clean`` did not trash this file, because
      trashing purges the row;
    * a rel outside the range the classifier actually walked means it was never
      offered at all.

    Opened read-only and never created — an auto-made empty cache would make
    every file look unclassified and veto the whole run.
    """
    path = Path(cache_db)
    if not path.exists():
        logger.debug("no classification cache at %s; skipping its vetoes", path)
        return {}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        logger.debug("classification cache at %s is unreadable", path)
        return {}
    try:
        conn.row_factory = sqlite3.Row
        cached = {r["path"] for r in conn.execute("SELECT path FROM classifications")}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()

    if not cached:
        return {}
    low, high = min(cached), max(cached)

    out: dict[str, Skip] = {}
    for rel in rels:
        if rel in cached:
            out[rel] = Skip(rel, SKIP_STILL_CLASSIFIED)
        elif not (low <= rel <= high):
            out[rel] = Skip(rel, SKIP_NOT_CLASSIFIED)
    return out


# --- second copies --------------------------------------------------------------


def copy_vetoes(
    rows: Iterable[sqlite3.Row], roots: Sequence[Path],
) -> dict[str, Skip]:
    """Disqualify anything still present at the same size under another root.

    A second local copy at the exact expected size is the strongest single-file
    evidence available and it points away from deletion, so it wins.
    """
    out: dict[str, Skip] = {}
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            logger.warning("corroboration root %s is not readable; ignoring it", base)
            continue
        for row in rows:
            rel = row["dest_path"]
            if rel in out:
                continue
            candidate = base / rel
            try:
                if candidate.is_file() and row["expected_size"] is not None \
                        and candidate.stat().st_size == int(row["expected_size"]):
                    out[rel] = Skip(rel, SKIP_ELSEWHERE, str(candidate))
            except OSError:
                continue
    return out


# --- the rotating log -----------------------------------------------------------


@dataclass(frozen=True)
class TrashLog:
    rounds: list[datetime] = field(default_factory=list)
    """``POST /trash`` timestamps inside the window, oldest first."""

    oldest_entry: datetime | None = None
    """The first timestamp in the oldest surviving log file."""

    files: tuple[Path, ...] = ()

    def covers(self, since: datetime) -> bool:
        """Whether the log reaches back far enough to see the whole window."""
        return self.oldest_entry is not None and self.oldest_entry <= since


def _parse_stamp(line: str) -> datetime | None:
    match = _LOG_STAMP.match(line)
    if match is None:
        return None
    try:
        # astimezone() on a naive value reads it as local time, which is what
        # logging wrote — the manifest's timestamps are UTC.
        return datetime.strptime(match.group(1), _LOG_STAMP_FORMAT).astimezone()
    except ValueError:
        return None


def trash_events(logs_dir: Path, since: datetime) -> TrashLog:
    """Every trash round logged at or after ``since``, plus the log's reach."""
    directory = Path(logs_dir)
    files = sorted(p for p in directory.glob(f"{LOG_BASENAME}*") if p.is_file()) \
        if directory.is_dir() else []
    if not files:
        return TrashLog()

    rounds: list[datetime] = []
    oldest: datetime | None = None
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stamp = _parse_stamp(line)
            if stamp is None:
                continue
            if oldest is None or stamp < oldest:
                oldest = stamp
            if _TRASH_LINE in line and stamp >= since:
                rounds.append(stamp)
    rounds.sort()
    return TrashLog(rounds=rounds, oldest_entry=oldest, files=tuple(files))


# --- putting it together --------------------------------------------------------


@dataclass(frozen=True)
class RetroScan:
    """Everything the terminal needs to print, and the plan needs to decide."""

    missing: list[sqlite3.Row]
    evidence: RetroEvidence | None
    structural: list[str]
    """Whole-run refusals. Non-empty means nothing may be deleted."""

    verified_present_at: str | None
    trash_log: TrashLog
    out_of_envelope: list[str]
    max_bytes: int
    min_bytes: int

    @property
    def rels(self) -> list[str]:
        return [row["dest_path"] for row in self.missing]


def scan(
    state: StateStore,
    *,
    output_root: Path,
    logs_dir: Path,
    cache_db: Path,
    max_bytes: int,
    min_bytes: int,
    corroborate_roots: Sequence[Path] = (),
    reviewed: frozenset[str] | None = None,
) -> RetroScan:
    """Reconcile the manifest against the tree and weigh what is missing."""
    root = Path(output_root)
    empty = RetroScan(missing=[], evidence=None, structural=[], verified_present_at=None,
                      trash_log=TrashLog(), out_of_envelope=[],
                      max_bytes=max_bytes, min_bytes=min_bytes)

    if not root.is_dir():
        return replace_structural(empty, [
            f"{root} is not a readable directory. If that drive is not mounted, "
            "every tracked file would look deleted — refusing to look further."])

    stamp, why_not = verified_present_at(state)
    if stamp is None:
        return replace_structural(empty, [why_not])
    since = datetime.fromisoformat(stamp)

    missing = missing_completed_rows(state, root)
    trash_log = trash_events(logs_dir, since)

    envelopes: dict[str, str] = {}
    out_of_envelope: list[str] = []
    for row in missing:
        rel = row["dest_path"]
        which = envelope_for(rel, row["expected_size"],
                             max_bytes=max_bytes, min_bytes=min_bytes)
        if which is None:
            out_of_envelope.append(rel)
        else:
            envelopes[rel] = which

    vetoes: dict[str, Skip] = {}
    image_rels = [rel for rel, which in envelopes.items()
                  if which == ENVELOPE_LOCAL_CLEAN]
    vetoes.update(cache_vetoes(cache_db, image_rels))
    if corroborate_roots:
        vetoes.update(copy_vetoes(missing, corroborate_roots))

    structural: list[str] = []
    if not missing:
        pass                      # nothing to explain; the caller reports "none"
    elif not trash_log.files:
        structural.append(
            "no log files were found, so there is no record that this tool ever "
            "trashed anything. Refusing to treat absence as deletion.")
    elif not trash_log.covers(since):
        structural.append(
            f"the logs only reach back to {fmt_time(trash_log.oldest_entry)}, which is "
            f"after {fmt_time(since)} — there is an unobserved gap in the window, so "
            "something else could have removed these files unseen.")
    elif not trash_log.rounds:
        structural.append(
            f"{len(missing)} tracked files are missing but no trash round is logged "
            f"since {fmt_time(since)}. Something other than this tool removed them, so "
            "the reconstruction cannot be trusted.")

    if out_of_envelope:
        shown = ", ".join(sorted(out_of_envelope)[:5])
        more = f" and {len(out_of_envelope) - 5} more" if len(out_of_envelope) > 5 else ""
        structural.append(
            f"{len(out_of_envelope)} missing file(s) fall outside every clean "
            f"command's scan envelope, so no clean session can explain them: "
            f"{shown}{more}. Check whether the drive is fully mounted or whether "
            "the folder was reorganised, then look at those files specifically.")

    return RetroScan(
        missing=missing,
        evidence=RetroEvidence(
            verified_present_at=stamp, envelopes=envelopes, vetoes=vetoes,
            corroboration=_corroboration(envelopes, vetoes, stamp, trash_log),
            reviewed=reviewed,
        ),
        structural=structural,
        verified_present_at=stamp,
        trash_log=trash_log,
        out_of_envelope=out_of_envelope,
        max_bytes=max_bytes,
        min_bytes=min_bytes,
    )


def replace_structural(scan_result: RetroScan, structural: list[str]) -> RetroScan:
    """A refusal carries no evidence — the premise it would rest on is broken."""
    return RetroScan(
        missing=scan_result.missing, evidence=None, structural=structural,
        verified_present_at=scan_result.verified_present_at,
        trash_log=scan_result.trash_log, out_of_envelope=scan_result.out_of_envelope,
        max_bytes=scan_result.max_bytes, min_bytes=scan_result.min_bytes,
    )


def _corroboration(
    envelopes: dict[str, str], vetoes: dict[str, Skip], stamp: str, trash_log: TrashLog,
) -> dict[str, tuple[str, ...]]:
    """Per-file notes recorded in the manifest and the receipt."""
    rounds = (f"{len(trash_log.rounds)} trash round(s) logged since {stamp}"
              if trash_log.rounds else "")
    out: dict[str, tuple[str, ...]] = {}
    for rel, which in envelopes.items():
        if rel in vetoes:
            continue
        notes = [f"on disk at {stamp}", f"inside the {which} envelope"]
        if which == ENVELOPE_LOCAL_CLEAN:
            notes.append("classification cache row absent (trashing purges it)")
        if rounds:
            notes.append(rounds)
        out[rel] = tuple(notes)
    return out


def fmt_time(when: datetime | None) -> str:
    return "never" if when is None else when.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
