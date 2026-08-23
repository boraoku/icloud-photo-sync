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

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import typer

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

    @property
    def stamped(self) -> int:
        return self.stamped_from_filename + self.stamped_from_folder

    @property
    def total(self) -> int:
        return (self.already_had_date + self.stamped + self.unknown
                + self.failed + self.tool_unavailable)


def scan_photos(root: Path) -> list[tuple[Path, str]]:
    """Every photo under ``root`` this module can date — the full library,
    not ``local-clean``'s narrower ≤ 1 MB junk-image scan (date recovery
    isn't junk-detection, it applies regardless of size)."""
    return sorted(
        ((p, rel) for p, rel, _st in iter_media_files(root, md.IMAGE_SUFFIXES)),
        key=lambda t: t[1],
    )


def recover_dates(
    root: Path, *, echo: Callable[..., None] = typer.secho,
    progress=None, dry_run: bool = False,
) -> DateRecoverySummary:
    """Phase A: stamp a missing capture date into every photo that has none.

    Never touches a photo whose own embedded date is already present — see
    the module docstring for why that's deliberate, not an oversight.
    ``dry_run`` reports what would be stamped and writes nothing.
    """
    photos = scan_photos(root)
    summary = DateRecoverySummary()
    if not photos:
        return summary

    echo(f"\nChecking {len(photos)} photo(s) for a capture date…", fg=typer.colors.BLUE)
    bar = progress(total=len(photos), desc="Checking dates", unit="photo") if progress else None
    tool_warned = False
    try:
        for path, rel in photos:
            capture_dt, source = md.infer_capture_date(path, rel)
            if source == md.SOURCE_EMBEDDED:
                summary.already_had_date += 1
            elif capture_dt is None:
                summary.unknown += 1
            elif dry_run:
                if source == md.SOURCE_FILENAME:
                    summary.stamped_from_filename += 1
                else:
                    summary.stamped_from_folder += 1
            else:
                outcome = md.ensure_capture_date(path, capture_dt)
                if outcome == md.MetadataOutcome.STAMPED:
                    if source == md.SOURCE_FILENAME:
                        summary.stamped_from_filename += 1
                    else:
                        summary.stamped_from_folder += 1
                elif outcome == md.MetadataOutcome.TOOL_UNAVAILABLE:
                    summary.tool_unavailable += 1
                    if not tool_warned:
                        tool_warned = True
                        echo(f"  {md.required_tool_name(path)} not found — dates cannot "
                             "be stamped into photos this run.", fg=typer.colors.YELLOW)
                else:
                    summary.failed += 1
            if bar:
                bar.update(1)
    finally:
        if bar:
            bar.close()
    return summary


def _report_recovery(summary: DateRecoverySummary, echo, *, dry_run: bool) -> None:
    verb = "Would stamp" if dry_run else "Stamped"
    echo(f"  {summary.already_had_date} already had a date.")
    if summary.stamped:
        echo(f"  {verb} {summary.stamped_from_filename} from the filename, "
             f"{summary.stamped_from_folder} from the folder.", fg=typer.colors.GREEN)
    if summary.unknown:
        echo(f"  {summary.unknown} left unknown — no embedded date, filename or "
             "folder to guess from.", fg=typer.colors.YELLOW)
    if summary.failed:
        echo(f"  {summary.failed} could not be read or written.", fg=typer.colors.RED)


def run_photo_optimise_external(
    config: LocalCleanConfig, *, echo: Callable[..., None] = typer.secho,
    progress=None, dry_run: bool = False,
) -> int:
    """Phase A (recover missing dates) then Phase B (``local-clean``, as-is).

    No Apple ID is ever resolved in either phase — this command never
    contacts iCloud, matching ``video-optimise-external``.
    """
    summary = recover_dates(config.output_root, echo=echo, progress=progress,
                            dry_run=dry_run)
    _report_recovery(summary, echo, dry_run=dry_run)
    if dry_run:
        echo("\nDry run — nothing was stamped, and local-clean was not run.",
             fg=typer.colors.YELLOW)
        return 0
    return run_local_clean(config)
