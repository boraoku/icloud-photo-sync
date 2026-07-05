"""Sync orchestration (C7).

Drives a run: enumerate → resolve a stable destination → record in state →
download. Three behaviours, all one-way (download/add only, never delete):

* **full**        — iterate everything, skip what's already complete (resumable).
* **incremental** — iterate newest-first, stop after ``until_found`` consecutive
                    already-have assets (new photos are at the top).
* **watch**       — repeat the incremental pass on an interval.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from .config import AppConfig
from .downloader import Downloader
from .errors import (
    LibraryIndexingError,
    OperationCancelled,
    SessionExpiredError,
    TransientError,
)
from .icloud_client import ICloudClient
from .logutil import get_logger
from .models import AssetRef, DownloadOutcome
from .paths import PathResolver
from .state import StateStore, utc_now_iso as _now

logger = get_logger(__name__)


@dataclass
class RunStats:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: bool = False
    stopped_early: bool = False

    def add(self, outcome: DownloadOutcome) -> None:
        if outcome == DownloadOutcome.DOWNLOADED:
            self.downloaded += 1
        elif outcome == DownloadOutcome.SKIPPED:
            self.skipped += 1
        elif outcome == DownloadOutcome.FAILED:
            self.failed += 1

    def summary(self) -> str:
        bits = [
            f"{self.downloaded} downloaded",
            f"{self.skipped} skipped",
            f"{self.failed} failed",
        ]
        if self.stopped_early:
            bits.append("stopped early")
        if self.cancelled:
            bits.append("CANCELLED")
        return ", ".join(bits)


class Orchestrator:
    def __init__(
        self,
        client: ICloudClient,
        state: StateStore,
        paths: PathResolver,
        downloader: Downloader,
        config: AppConfig,
        cancel_event: Event | None = None,
    ) -> None:
        self.client = client
        self.state = state
        self.paths = paths
        self.downloader = downloader
        self.config = config
        self.cancel = cancel_event or Event()

    # -- runs -----------------------------------------------------------------

    def run_full(self) -> RunStats:
        stats = RunStats()
        total = self.client.count()
        logger.info(
            "Starting full pass%s.", f" — {total} assets in iCloud" if total else ""
        )
        seen = 0
        try:
            for asset in self.client.iter_all_assets():
                self._raise_if_cancelled()
                row = self.state.get(asset.id)
                if row is not None and row["status"] == "completed" and self._on_disk_ok(asset, row):
                    stats.skipped += 1  # fast path: one SELECT + one stat, no writes
                else:
                    stats.add(self._process(asset))
                seen += 1
                self._heartbeat(seen, total, stats)
            self.state.set_meta("last_full_pass_at", _now())
        except OperationCancelled:
            stats.cancelled = True
        logger.info("Full pass: %s.", stats.summary())
        return stats

    def run_incremental(self, until_found: int | None = None) -> RunStats:
        until_found = until_found or self.config.until_found
        stats = RunStats()
        # Early-stop is only sound over a newest-ADDED-first ordering: brand-new
        # additions must come first even when their capture date is old
        # (imports/AirDrops/scans). The capture-date-ordered "All Photos" album
        # would bury those, so if the added-date listing is unavailable we scan
        # everything rather than silently miss photos.
        assets = self.client.iter_added_desc()
        early_stop = assets is not None
        if early_stop:
            logger.info(
                "Starting incremental pass (stop after %d consecutive seen).", until_found
            )
        else:
            logger.warning(
                "Added-date listing unavailable; scanning the full library "
                "for new items (no early stop — this is slower but safe)."
            )
            assets = self.client.iter_all_assets()
        consecutive = 0
        seen = 0
        try:
            for asset in assets:
                self._raise_if_cancelled()
                row = self.state.get(asset.id)
                if row is not None and row["status"] == "completed" and self._on_disk_ok(asset, row):
                    stats.skipped += 1
                    if early_stop:
                        consecutive += 1
                        if consecutive >= until_found:
                            stats.stopped_early = True
                            logger.info(
                                "Hit %d consecutive already-have assets; stopping.", until_found
                            )
                            break
                    continue
                consecutive = 0
                stats.add(self._process(asset))
                seen += 1
                self._heartbeat(seen, None, stats)
            self.state.set_meta("last_update_at", _now())
        except OperationCancelled:
            stats.cancelled = True
        logger.info("Incremental pass: %s.", stats.summary())
        return stats

    def run_watch(self, interval: int | None = None, until_found: int | None = None) -> None:
        interval = interval or self.config.watch_interval
        logger.info("Watch mode: incremental pass every %ds. Ctrl-C to stop.", interval)
        while not self.cancel.is_set():
            try:
                self.run_incremental(until_found)
            except SessionExpiredError:
                logger.error("Session expired. Run `icloud-photo-sync login`, then restart watch.")
                return
            except OperationCancelled:
                break
            except (TransientError, LibraryIndexingError) as exc:
                # Both are Apple-side temporary conditions — ride them out.
                logger.warning("Pass failed (%s); will retry next interval.", exc)
            if self.cancel.is_set():
                break
            logger.info("Sleeping %ds until next pass…", interval)
            self._interruptible_sleep(interval)
        logger.info("Watch stopped.")

    # -- per-asset ------------------------------------------------------------

    def _process(self, asset: AssetRef) -> DownloadOutcome:
        rel = self._resolve_dest(asset)
        abs_dest = self.paths.absolute(rel)
        try:
            outcome = self.downloader.download(asset, abs_dest)
        except OperationCancelled:
            raise
        if outcome == DownloadOutcome.DOWNLOADED:
            logger.info("✓ %s → %s", asset.filename, rel.as_posix())
        elif outcome == DownloadOutcome.SKIPPED:
            logger.debug("• already have %s", rel.as_posix())
        return outcome

    def _resolve_dest(self, asset: AssetRef) -> Path:
        """Compute (or reuse) a stable relative destination and record it.

        Policy (one-way guarantee — we never overwrite an existing file):
        * A recorded destination is reused; but if a file we never finalized
          now occupies it and its size doesn't match, the asset is re-pointed
          to a suffixed name (any in-progress ``.part`` moves along with it).
        * With no state row, an existing file at the canonical path whose size
          matches iCloud's is **adopted** as already-downloaded (recovers a
          lost DB / pre-populated folder without re-downloading terabytes);
          any other existing file is treated as a collision and suffixed.
        """
        row = self.state.get(asset.id)
        expected = asset.size
        if expected is None and row is not None:
            expected = row["expected_size"]

        if row is not None and row["dest_path"]:
            rel = Path(row["dest_path"])
            abs_dest = self.paths.absolute(rel)
            if (
                row["status"] != "completed"
                and abs_dest.exists()
                and (expected is None or abs_dest.stat().st_size != expected)
            ):
                # We never finalized this path, yet a non-matching file sits
                # there (e.g. the user's own). Leave it alone and move aside.
                # The occupied path counts as taken even though we own its row.
                base_taken = self._taken_predicate(asset.id)
                new_rel = self.paths.disambiguate(
                    self.paths.relative_dest(asset),
                    lambda p: p == rel or base_taken(p),
                )
                logger.warning(
                    "Destination %s is occupied by a file we did not write; using %s",
                    rel.as_posix(), new_rel.as_posix(),
                )
                self._move_part(rel, new_rel)
                self.state.register(asset, new_rel.as_posix())
                self.state.update_dest(asset.id, new_rel.as_posix())
                return new_rel
            self.state.register(asset, rel.as_posix())
            return rel

        rel = self.paths.relative_dest(asset)
        abs_dest = self.paths.absolute(rel)
        if abs_dest.exists() and expected is not None and abs_dest.stat().st_size == expected:
            logger.info("Adopting existing file %s (size matches iCloud).", rel.as_posix())
            self.state.register(asset, rel.as_posix())
            self.state.mark_completed(asset.id, expected)
            return rel
        rel = self.paths.disambiguate(rel, self._taken_predicate(asset.id))
        self.state.register(asset, rel.as_posix())
        return rel

    def _move_part(self, old_rel: Path, new_rel: Path) -> None:
        """Carry partial progress along when an asset is re-pointed."""
        old_part = self.paths.absolute(old_rel).with_name(old_rel.name + ".part")
        if not old_part.exists():
            return
        new_part = self.paths.absolute(new_rel).with_name(new_rel.name + ".part")
        try:
            new_part.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_part, new_part)
        except OSError:
            old_part.unlink(missing_ok=True)  # .part files are ours to discard

    def _taken_predicate(self, self_id: str):
        def taken(rel: Path) -> bool:
            owner = self.state.path_owner(rel.as_posix())
            if owner is not None and owner != self_id:
                return True
            # A pre-existing file we don't already own also blocks the name.
            if owner != self_id and self.paths.absolute(rel).exists():
                return True
            return False

        return taken

    def _on_disk_ok(self, asset: AssetRef, row) -> bool:
        abs_dest = self.paths.absolute(Path(row["dest_path"])) if row["dest_path"] else None
        if abs_dest is None or not abs_dest.exists():
            return False
        # Fall back to the manifest's recorded size when iCloud transiently
        # doesn't report one — never let a size hiccup bless a truncated file.
        expected = asset.size if asset.size is not None else row["expected_size"]
        if expected is None:
            return True
        return abs_dest.stat().st_size == expected

    # -- helpers --------------------------------------------------------------

    def _heartbeat(self, seen: int, total: int | None, stats: RunStats) -> None:
        if seen % 200 == 0:
            suffix = f"/{total}" if total else ""
            logger.info("…processed %d%s (%s)", seen, suffix, stats.summary())

    def _raise_if_cancelled(self) -> None:
        if self.cancel.is_set():
            raise OperationCancelled()

    def _interruptible_sleep(self, seconds: float) -> None:
        # Event.wait wakes immediately on cancel (plain sleep would not — PEP 475).
        self.cancel.wait(seconds)
