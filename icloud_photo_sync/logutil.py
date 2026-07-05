"""Logging setup (C9).

Human-readable console output plus a rotating debug log in the config dir.
``tqdm`` progress bars are written to stderr by the downloader; to avoid
tearing them, log records are emitted via ``tqdm.write`` when a bar is active.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False
_CONSOLE_HANDLER: logging.Handler | None = None


class _TqdmAwareHandler(logging.StreamHandler):
    """Console handler that plays nicely with an active tqdm progress bar."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from tqdm import tqdm

            msg = self.format(record)
            tqdm.write(msg, file=self.stream)
            self.flush()
        except Exception:  # pragma: no cover - fall back to normal behaviour
            super().emit(record)


def _apply_verbosity(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    if _CONSOLE_HANDLER is not None:
        _CONSOLE_HANDLER.setLevel(level)
    for noisy in ("urllib3", "pyicloud", "requests"):
        logging.getLogger(noisy).setLevel(
            logging.DEBUG if verbose else logging.WARNING
        )


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    """Configure root logging once. Safe to call multiple times."""
    global _CONFIGURED, _CONSOLE_HANDLER
    if _CONFIGURED:
        # Flipping verbosity must reach the handler too — levels on the
        # logger alone are filtered out again at the console handler.
        _apply_verbosity(verbose)
        return

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = _TqdmAwareHandler()
    console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    _CONSOLE_HANDLER = console

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fileh = RotatingFileHandler(
            log_dir / "icloud-photo-sync.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fileh.setLevel(logging.DEBUG)
        fileh.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(fileh)
    except OSError:
        # Logging to a file is best-effort; never block a sync because of it.
        pass

    # Console level + third-party noise share one knob with the repeat path.
    _apply_verbosity(verbose)

    _CONFIGURED = True


def get_logger(name: str = "icloud_photo_sync") -> logging.Logger:
    return logging.getLogger(name)
