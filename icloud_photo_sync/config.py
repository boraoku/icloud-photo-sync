"""Configuration & credentials (C1).

Resolves the Apple ID, password (via macOS Keychain), the photo output root
(the launch directory by default), and all runtime tunables. Runtime data
(cookies, state DB, logs) lives under the user config dir — never mixed into
the photo tree.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

APP_NAME = "icloud-photo-sync"
KEYRING_SERVICE = "icloud-photo-sync"
ENV_USERNAME = "ICLOUD_SYNC_USERNAME"

# Conservative defaults (all overridable via CLI flags where it makes sense).
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024        # 8 MiB streamed reads
DEFAULT_MAX_RETRIES = 5                       # per-file transient-error attempts
DEFAULT_BACKOFF_BASE = 2.0                    # exponential backoff base (seconds)
DEFAULT_BACKOFF_CAP = 60.0                    # max sleep between attempts
DEFAULT_CONNECT_TIMEOUT = 30.0
DEFAULT_READ_TIMEOUT = 60.0
DEFAULT_UNTIL_FOUND = 50                      # consecutive seen → stop incremental
DEFAULT_WATCH_INTERVAL = 1800                 # 30 min between watch passes
MIN_WATCH_INTERVAL = 300                      # floor to avoid throttling
DEFAULT_INDEXING_RETRY = 30.0                 # wait between "still indexing" retries
DEFAULT_INDEXING_MAX_WAIT = 1800.0           # give up waiting for indexing after 30m
PARTIAL_FLUSH_BYTES = 16 * 1024 * 1024        # throttle bytes_done writes to state
PARTIAL_FLUSH_SECS = 5.0


def default_config_root() -> Path:
    """Per-user config/data dir. macOS: ``~/Library/Application Support``."""
    base = Path.home() / "Library" / "Application Support"
    if not base.exists():  # non-macOS / minimal envs (tests, CI)
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


def _sanitize(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "_", text)


def _state_key(apple_id: str, output_root: Path) -> str:
    raw = f"{apple_id.lower()}|{output_root.resolve()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:10]


@dataclass
class AppConfig:
    """Fully-resolved configuration for a run."""

    apple_id: str
    output_root: Path
    config_root: Path
    cookie_dir: Path
    state_db: Path
    logs_dir: Path

    # Tunables
    chunk_size: int = DEFAULT_CHUNK_SIZE
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_cap: float = DEFAULT_BACKOFF_CAP
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    until_found: int = DEFAULT_UNTIL_FOUND
    watch_interval: int = DEFAULT_WATCH_INTERVAL
    indexing_retry: float = DEFAULT_INDEXING_RETRY
    indexing_max_wait: float = DEFAULT_INDEXING_MAX_WAIT

    verbose: bool = False
    show_progress: bool = True

    extra: dict = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        apple_id: str,
        output_root: Path,
        *,
        config_root: Path | None = None,
        **overrides,
    ) -> "AppConfig":
        apple_id = apple_id.strip()
        output_root = Path(output_root).resolve()
        config_root = (config_root or default_config_root()).resolve()
        cookie_dir = config_root / "cookies"
        logs_dir = config_root / "logs"
        state_dir = config_root / "state"
        state_db = state_dir / f"{_sanitize(apple_id)}-{_state_key(apple_id, output_root)}.db"

        for d in (config_root, cookie_dir, logs_dir, state_dir):
            d.mkdir(parents=True, exist_ok=True)

        cfg = cls(
            apple_id=apple_id,
            output_root=output_root,
            config_root=config_root,
            cookie_dir=cookie_dir,
            state_db=state_db,
            logs_dir=logs_dir,
        )
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)


# --- Credentials (macOS Keychain via `keyring`) ------------------------------


def get_password(apple_id: str) -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, apple_id)
    except KeyringError:
        return None


def set_password(apple_id: str, password: str) -> bool:
    try:
        keyring.set_password(KEYRING_SERVICE, apple_id, password)
        return True
    except KeyringError:
        return False


def delete_password(apple_id: str) -> bool:
    try:
        keyring.delete_password(KEYRING_SERVICE, apple_id)
        return True
    except PasswordDeleteError:
        return False  # nothing stored
    except KeyringError:
        return False


def resolve_username(cli_value: str | None) -> str | None:
    """Username from the flag or the environment. ``None`` ⇒ caller must prompt."""
    if cli_value:
        return cli_value.strip()
    env = os.environ.get(ENV_USERNAME)
    return env.strip() if env else None
