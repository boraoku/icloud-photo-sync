"""``photo-optimise-external``: recover missing photo dates, then hand off to
``local-clean``.

Two independent phases run in sequence on the same folder, for a photo
library that already exists on disk outside this tool's active iCloud
management — no Apple ID is ever resolved, no iCloud contact happens in
either phase.

**Phase A** (this module's own code): every photo under the folder — not just
the small ones ``local-clean`` looks at, the whole library — gets a capture
date worked out via :func:`icloud_photo_sync.metadata.infer_capture_date` and
stamped in, in place, when one is genuinely missing. A photo that already
carries a date is left untouched entirely, mtime included: unlike ``sync`` and
``video-optimise``, where a *freshly written* file's mtime is the actual bug
being fixed, an existing library file that already has a correct embedded
date has nothing wrong with its mtime for anything to fall back to — so
there's no reason to touch it, and no reason to re-touch it on every re-run
of this command against the same, already-correct library.

**Phase B**: the existing ``local-clean`` flow, called as-is, unchanged.
Running date-recovery first means anything you decide to keep already has a
correct date; anything you end up trashing was dated for free.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple, Sequence

import typer

from . import date_cache as dc
from . import metadata as md
from .config import LocalCleanConfig
from .local_clean import iter_media_files, run_local_clean
from .logutil import get_logger

logger = get_logger(__name__)


@dataclass
class DateRecoverySummary:
    """One run's tally, bucketed by how each photo's date was resolved."""

    already_had_date: int = 0
    stamped_from_filename: int = 0
    stamped_from_folder: int = 0
    unknown: int = 0
    failed: int = 0
    tool_unavailable: int = 0
    skipped_cached: int = 0
    """How many of the counts above came from the cache rather than a fresh
    read. Deliberately NOT a bucket of its own: a cached photo is already
    counted under the outcome it had, and adding it here as well would double
    every resumed run's total."""
    interrupted: bool = False

    @property
    def stamped(self) -> int:
        return self.stamped_from_filename + self.stamped_from_folder

    @property
    def total(self) -> int:
        """Every photo considered, each counted exactly once."""
        return (self.already_had_date + self.stamped + self.unknown
                + self.failed + self.tool_unavailable)


class Photo(NamedTuple):
    """A scanned photo plus the fingerprint the resume cache keys on."""

    path: Path
    rel: str
    size: int
    mtime_ns: int


def scan_photos(root: Path) -> list[Photo]:
    """Every photo under ``root`` this module can date — the full library,
    not ``local-clean``'s narrower ≤ 1 MB junk-image scan (date recovery
    isn't junk-detection, it applies regardless of size)."""
    found = [
        Photo(path=p, rel=rel, size=st.st_size, mtime_ns=st.st_mtime_ns)
        for p, rel, st in iter_media_files(root, md.IMAGE_SUFFIXES)
    ]
    found.sort(key=lambda ph: ph.rel)
    return found


def _split_cached(
    photos: Sequence[Photo], cache: dc.DateCache | None,
) -> tuple[list[Photo], dict[str, int]]:
    """Split into (still to check, tally of what the cache already settled).

    A cache hit needs the file's size and mtime to match what was recorded,
    so an edited photo is re-examined rather than trusted.
    """
    todo: list[Photo] = []
    settled: dict[str, int] = {}
    if cache is None:
        return list(photos), settled
    for photo in photos:
        outcome = cache.get(photo.rel, photo.size, photo.mtime_ns)
        if outcome is None:
            todo.append(photo)
        else:
            settled[outcome] = settled.get(outcome, 0) + 1
    return todo, settled


def _apply_settled(summary: DateRecoverySummary, settled: dict[str, int]) -> None:
    """Fold the cache's own tally into this run's summary, so the totals
    describe the whole library rather than only the part re-examined."""
    for outcome, count in settled.items():
        summary.skipped_cached += count
        if outcome == dc.OUTCOME_ALREADY_PRESENT:
            summary.already_had_date += count
        elif outcome == dc.OUTCOME_STAMPED_FILENAME:
            summary.stamped_from_filename += count
        elif outcome == dc.OUTCOME_STAMPED_FOLDER:
            summary.stamped_from_folder += count
        elif outcome == dc.OUTCOME_UNKNOWN:
            summary.unknown += count


def _fingerprint(path: Path, fallback: Photo) -> tuple[int, int]:
    """The file's size and mtime as they are *now*.

    Read after any stamping, never before: stamping rewrites the file and
    then sets its mtime to the capture date, so caching the pre-write
    fingerprint would miss on the very next run and re-read every photo this
    one just fixed.
    """
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns
    except OSError:
        return fallback.size, fallback.mtime_ns


def recover_dates(
    root: Path, *, echo: Callable[..., None] = typer.secho,
    progress=None, dry_run: bool = False, cache: dc.DateCache | None = None,
) -> DateRecoverySummary:
    """Phase A: stamp a missing capture date into every photo that has none.

    Never touches a photo whose own embedded date is already present — see
    the module docstring for why that's deliberate, not an oversight.
    ``dry_run`` reports what would be stamped and writes nothing (and records
    nothing, since it changed nothing).

    Embedded dates are read in bulk — one ``exiftool`` per batch rather than
    per photo, which is the difference between minutes and hours on a real
    library (see :func:`icloud_photo_sync.metadata.read_embedded_capture_dates`).
    Everything below that first rung of the ladder is pure Python and free.

    ``cache`` makes the phase resumable: anything it has already settled is
    skipped without touching ``exiftool`` at all, and each batch is committed
    as it completes, so a Ctrl-C costs one batch rather than the whole run.
    """
    echo(f"\nScanning {root} for photos…", fg=typer.colors.BLUE)
    photos = scan_photos(root)
    summary = DateRecoverySummary()
    if not photos:
        echo("No photos found.", fg=typer.colors.GREEN)
        return summary

    todo, settled = _split_cached(photos, cache)
    _apply_settled(summary, settled)

    if settled:
        echo(f"Found {len(photos):,} photo(s).  {summary.skipped_cached:,} already "
             f"checked in an earlier run; {len(todo):,} to check.")
    else:
        echo(f"Found {len(photos):,} photo(s) to check.")
    if not todo:
        return summary

    bar = progress(total=len(todo), desc="Reading dates", unit="photo") if progress else None
    tool_warned = False
    try:
        for start in range(0, len(todo), md.EXIFTOOL_BATCH):
            batch = todo[start:start + md.EXIFTOOL_BATCH]
            embedded = md.read_embedded_capture_dates([ph.path for ph in batch])
            for photo in batch:
                tool_warned = _handle_one(
                    photo, embedded.get(photo.path), summary, cache,
                    dry_run=dry_run, echo=echo, tool_warned=tool_warned,
                )
                if bar:
                    bar.update(1)
            if cache is not None:
                cache.flush()
    except KeyboardInterrupt:
        # Everything committed so far stands; the next run picks up from the
        # start of the batch that was in flight.
        summary.interrupted = True
        if cache is not None:
            cache.flush()
    finally:
        if bar:
            bar.close()
    return summary


def _handle_one(
    photo: Photo, embedded, summary: DateRecoverySummary,
    cache: dc.DateCache | None, *, dry_run: bool, echo, tool_warned: bool,
) -> bool:
    """Resolve one photo, tally it, and record it. Returns ``tool_warned``,
    so a missing tool is reported once per run rather than once per file."""
    capture_dt, source = md.infer_capture_date_from(embedded, photo.path.name, photo.rel)

    def remember(outcome: str) -> None:
        if cache is not None and not dry_run:
            size, mtime_ns = _fingerprint(photo.path, photo)
            cache.put(photo.rel, size, mtime_ns, outcome)

    if source == md.SOURCE_EMBEDDED:
        summary.already_had_date += 1
        remember(dc.OUTCOME_ALREADY_PRESENT)
        return tool_warned

    if capture_dt is None:
        summary.unknown += 1
        remember(dc.OUTCOME_UNKNOWN)
        return tool_warned

    stamped_outcome = (dc.OUTCOME_STAMPED_FILENAME if source == md.SOURCE_FILENAME
                       else dc.OUTCOME_STAMPED_FOLDER)
    if dry_run:
        if source == md.SOURCE_FILENAME:
            summary.stamped_from_filename += 1
        else:
            summary.stamped_from_folder += 1
        return tool_warned

    # known_absent: the batch read above already established this file carries
    # no embedded date, so the presence check inside ensure_capture_date would
    # re-read the same file to reach the same answer. The read-before-write
    # rule still holds — the read simply happened in bulk.
    outcome = md.ensure_capture_date(photo.path, capture_dt, known_absent=True)
    if outcome == md.MetadataOutcome.STAMPED:
        if source == md.SOURCE_FILENAME:
            summary.stamped_from_filename += 1
        else:
            summary.stamped_from_folder += 1
        remember(stamped_outcome)
    elif outcome == md.MetadataOutcome.ALREADY_PRESENT:
        # Should be unreachable given known_absent, but if it ever happens the
        # honest answer is "it had a date", not "we stamped it".
        summary.already_had_date += 1
        remember(dc.OUTCOME_ALREADY_PRESENT)
    elif outcome == md.MetadataOutcome.TOOL_UNAVAILABLE:
        summary.tool_unavailable += 1
        if not tool_warned:
            tool_warned = True
            echo(f"  {md.required_tool_name(photo.path)} not found — dates cannot "
                 "be stamped into photos this run.", fg=typer.colors.YELLOW)
    else:
        summary.failed += 1
    return tool_warned


def _report_recovery(summary: DateRecoverySummary, echo, *, dry_run: bool) -> None:
    verb = "Would stamp" if dry_run else "Stamped"
    echo("")
    echo(f"  {summary.already_had_date:,} already had a date.")
    if summary.stamped:
        echo(f"  {verb} {summary.stamped_from_filename:,} from the filename, "
             f"{summary.stamped_from_folder:,} from the folder.", fg=typer.colors.GREEN)
    if summary.unknown:
        echo(f"  {summary.unknown:,} left unknown — no embedded date, filename or "
             "folder to guess from.", fg=typer.colors.YELLOW)
    if summary.tool_unavailable:
        echo(f"  {summary.tool_unavailable:,} could not be stamped — exiftool is "
             "not installed.", fg=typer.colors.YELLOW)
    if summary.failed:
        echo(f"  {summary.failed:,} could not be read or written.", fg=typer.colors.RED)
    if summary.skipped_cached:
        echo(f"  ({summary.skipped_cached:,} of those were settled by an earlier "
             "run and not re-read.)", fg=typer.colors.BLUE)


def run_photo_optimise_external(
    config: LocalCleanConfig, *, echo: Callable[..., None] = typer.secho,
    progress=None, dry_run: bool = False,
) -> int:
    """Phase A (recover missing dates) then Phase B (``local-clean``, as-is).

    No Apple ID is ever resolved in either phase — this command never
    contacts iCloud, matching ``video-optimise-external``.
    """
    cache_ctx = (dc.DateCache(config.date_cache_db)
                 if config.date_cache_db is not None else _NoCache())
    with cache_ctx as cache:
        if cache is not None and config.recheck_dates:
            cache.clear()
            echo("--recheck-dates: re-examining every photo.", fg=typer.colors.BLUE)
        summary = recover_dates(config.output_root, echo=echo, progress=progress,
                                dry_run=dry_run, cache=cache)
    _report_recovery(summary, echo, dry_run=dry_run)

    if summary.interrupted:
        echo("\nStopped. Everything checked so far is remembered — re-run to "
             "carry on where this left off.", fg=typer.colors.YELLOW)
        return 130
    if dry_run:
        echo("\nDry run — nothing was stamped, and local-clean was not run.",
             fg=typer.colors.YELLOW)
        return 0
    return run_local_clean(config)


class _NoCache:
    """Stands in for the date cache when no path was configured, so the
    caller's ``with`` block reads the same either way."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
