"""Single-asset download with two-level resume (C6).

Streams one asset's *original* to ``dest.part``, then atomically renames it into
place. Resumes via HTTP range when the server allows it, restarts the file when
it does not (the behaviour the spec calls out for large videos), verifies the
final byte count, and never presents a half-written file as complete.

It is cancellation-aware: on SIGINT/SIGTERM the in-flight ``.part`` and the
state row are flushed before raising, so the next run picks up where it stopped.
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path
from threading import Event

import requests

from .config import AppConfig, PARTIAL_FLUSH_BYTES, PARTIAL_FLUSH_SECS
from .errors import (
    DownloadError,
    IntegrityError,
    OperationCancelled,
    TransientError,
)
from .icloud_client import ICloudClient
from .logutil import get_logger
from .models import AssetRef, DownloadOutcome
from .state import StateStore

logger = get_logger(__name__)

_RETRYABLE = (TransientError, IntegrityError, requests.exceptions.RequestException)


class Downloader:
    def __init__(
        self,
        client: ICloudClient,
        state: StateStore,
        config: AppConfig,
        cancel_event: Event | None = None,
    ) -> None:
        self.client = client
        self.state = state
        self.config = config
        self.cancel = cancel_event or Event()

    # -- public ---------------------------------------------------------------

    def download(self, asset: AssetRef, abs_dest: Path) -> DownloadOutcome:
        expected = asset.size
        part = abs_dest.with_name(abs_dest.name + ".part")

        # Already complete on disk? (correct size, or unknown size → trust it).
        if abs_dest.exists():
            actual = abs_dest.stat().st_size
            if expected is None or actual == expected:
                self.state.mark_completed(asset.id, actual)
                return DownloadOutcome.SKIPPED
            logger.debug(
                "%s exists but is %d bytes (expected %d); re-downloading",
                abs_dest.name, actual, expected,
            )

        abs_dest.parent.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        last_err: Exception | None = None

        attempt = 0
        while attempt <= self.config.max_retries:
            self._raise_if_cancelled()
            try:
                resp, range_ok, total = self.client.open_stream(asset, byte_offset=have)
                if have > 0 and not range_ok:
                    logger.info("Range not honoured for %s — restarting file", asset.filename)
                    part.unlink(missing_ok=True)
                    have = 0
                mode = "ab" if have > 0 else "wb"
                have = self._stream_to_part(resp, part, mode, have, asset, total or expected)

                final_size = part.stat().st_size
                if expected is not None and final_size != expected:
                    raise IntegrityError(
                        f"size mismatch for {asset.filename}: got {final_size}, "
                        f"expected {expected}"
                    )

                self._finalize(part, abs_dest)
                self.state.mark_completed(asset.id, final_size)
                return DownloadOutcome.DOWNLOADED

            except OperationCancelled:
                raise
            except DownloadError as exc:  # non-retryable
                last_err = exc
                break
            except _RETRYABLE as exc:
                last_err = exc
                attempt += 1
                # An integrity failure means the bytes are wrong — start over.
                if isinstance(exc, IntegrityError):
                    part.unlink(missing_ok=True)
                    have = 0
                else:
                    have = part.stat().st_size if part.exists() else 0
                if attempt > self.config.max_retries:
                    break
                delay = self._backoff(attempt)
                logger.warning(
                    "Download of %s failed (%s); retry %d/%d in %.1fs",
                    asset.filename, exc, attempt, self.config.max_retries, delay,
                )
                self._interruptible_sleep(delay)

        self.state.mark_failed(asset.id, str(last_err) if last_err else "unknown error")
        logger.error("Giving up on %s: %s", asset.filename, last_err)
        return DownloadOutcome.FAILED

    # -- internals ------------------------------------------------------------

    def _stream_to_part(
        self, resp, part: Path, mode: str, have: int, asset: AssetRef, total: int | None
    ) -> int:
        flushed_since = 0
        last_flush = time.monotonic()
        bar = self._make_bar(asset, have, total)
        try:
            with open(part, mode) as f:
                for chunk in resp.iter_content(self.config.chunk_size):
                    if self.cancel.is_set():
                        f.flush()
                        os.fsync(f.fileno())
                        self.state.record_partial(asset.id, have)
                        raise OperationCancelled()
                    if not chunk:
                        continue
                    f.write(chunk)
                    have += len(chunk)
                    flushed_since += len(chunk)
                    if bar is not None:
                        bar.update(len(chunk))
                    now = time.monotonic()
                    if flushed_since >= PARTIAL_FLUSH_BYTES or now - last_flush >= PARTIAL_FLUSH_SECS:
                        f.flush()
                        self.state.record_partial(asset.id, have)
                        flushed_since = 0
                        last_flush = now
                f.flush()
                os.fsync(f.fileno())
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
            if bar is not None:
                bar.close()
        self.state.record_partial(asset.id, have)
        return have

    @staticmethod
    def _finalize(part: Path, dest: Path) -> None:
        """Atomically move the verified .part into place, durably."""
        os.replace(part, dest)
        # Persist the rename by fsync-ing the containing directory (best effort).
        try:
            dir_fd = os.open(str(dest.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    def _backoff(self, attempt: int) -> float:
        base = min(self.config.backoff_cap, self.config.backoff_base ** attempt)
        return base * (0.5 + random.random() * 0.5)  # 50–100% jitter

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._raise_if_cancelled()
            time.sleep(min(0.25, deadline - time.monotonic()))

    def _raise_if_cancelled(self) -> None:
        if self.cancel.is_set():
            raise OperationCancelled()

    def _make_bar(self, asset: AssetRef, have: int, total: int | None):
        if not self.config.show_progress or not sys.stderr.isatty():
            return None
        try:
            from tqdm import tqdm

            desc = asset.filename if len(asset.filename) <= 28 else asset.filename[:25] + "…"
            return tqdm(
                total=total,
                initial=have,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=desc,
                leave=False,
                dynamic_ncols=True,
            )
        except Exception:  # noqa: BLE001 - progress is optional
            return None
