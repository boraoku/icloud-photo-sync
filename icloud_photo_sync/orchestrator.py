"""Sync orchestration (C7).

Drives a run: enumerate → resolve a stable destination → record in state →
download. Three behaviours, all one-way (download/add only, never delete):

* **full**        — iterate everything, skip what's already complete (resumable).
* **incremental** — iterate newest-first, stop after ``until_found`` consecutive
                    already-have assets (new photos are at the top).
* **watch**       — repeat the incremental pass on an interval.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from .config import AppConfig
from .downloader import Downloader
from .errors import OperationCancelled, SessionExpiredError, TransientError
from .icloud_client import ICloudClient
from .logutil import get_logger
from .models import AssetRef, DownloadOutcome
from .paths import PathResolver
from .state import StateStore

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        logger.info("Starting incremental pass (stop after %d consecutive seen).", until_found)
        consecutive = 0
        seen = 0
        try:
            for asset in self.client.iter_all_assets():
                self._raise_if_cancelled()
                row = self.state.get(asset.id)
                if row is not None and row["status"] == "completed" and self._on_disk_ok(asset, row):
                    stats.skipped += 1
                    consecutive += 1
                    if consecutive >= until_found:
                        stats.stopped_early = True
                        logger.info("Hit %d consecutive already-have assets; stopping.", until_found)
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
            except TransientError as exc:
                logger.warning("Pass failed transiently (%s); will retry next interval.", exc)
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
        """Compute (or reuse) a stable relative destination and record it."""
        row = self.state.get(asset.id)
        if row is not None and row["dest_path"]:
            rel = Path(row["dest_path"])
        else:
            rel = self.paths.relative_dest(asset)
            rel = self.paths.disambiguate(rel, self._taken_predicate(asset.id))
        self.state.register(asset, rel.as_posix())
        return rel

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
        if asset.size is None:
            return True
        return abs_dest.stat().st_size == asset.size

    # -- helpers --------------------------------------------------------------

    def _heartbeat(self, seen: int, total: int | None, stats: RunStats) -> None:
        if seen % 200 == 0:
            suffix = f"/{total}" if total else ""
            logger.info("…processed %d%s (%s)", seen, suffix, stats.summary())

    def _raise_if_cancelled(self) -> None:
        if self.cancel.is_set():
            raise OperationCancelled()

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.cancel.is_set():
            time.sleep(min(1.0, deadline - time.monotonic()))
