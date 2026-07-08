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
from dataclasses import dataclass
from pathlib import Path

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

APP_NAME = "icloud-photo-sync"
KEYRING_SERVICE = "icloud-photo-sync"
ENV_USERNAME = "ICLOUD_SYNC_USERNAME"
ENV_LM_URL = "ICLOUD_SYNC_LM_URL"

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

# local-clean: local vision-model classification of small screenshots/memes.
DEFAULT_LM_BASE_URL = "http://127.0.0.1:1234"   # LM Studio, OpenAI-compatible
DEFAULT_LM_MODEL = "qwen/qwen3.5-9b"            # vision-capable
DEFAULT_CLEAN_MAX_BYTES = 1 * 1024 * 1024       # only consider images ≤ 1 MiB
DEFAULT_LM_CONNECT_TIMEOUT = 5.0
DEFAULT_LM_READ_TIMEOUT = 180.0                 # ~15s typical; headroom for cold load
DEFAULT_FLAG_CATEGORIES = ("screenshot", "meme", "other")
DEFAULT_THUMB_MAX_DIM = 1024                    # px; downscale for both LM and review


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
            if not hasattr(cfg, k):
                # A silently-dropped typo would run with defaults and be
                # near-impossible to debug; fail loudly instead.
                raise TypeError(f"unknown config override: {k!r}")
            if v is not None:
                setattr(cfg, k, v)
        return cfg

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)


def _clean_cache_key(output_root: Path) -> str:
    raw = str(output_root.resolve()).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:10]


@dataclass
class LocalCleanConfig:
    """Configuration for the credential-free ``local-clean`` command.

    Deliberately independent of :class:`AppConfig`: ``local-clean`` needs no
    Apple ID, and its classification cache is keyed by the photo tree alone
    (a file's category is a property of the file, not of any account), so two
    accounts syncing to the same tree share one cache.
    """

    output_root: Path
    cache_db: Path
    logs_dir: Path

    lm_base_url: str = DEFAULT_LM_BASE_URL
    lm_model: str = DEFAULT_LM_MODEL
    max_bytes: int = DEFAULT_CLEAN_MAX_BYTES
    flag_categories: tuple[str, ...] = DEFAULT_FLAG_CATEGORIES
    thumb_max_dim: int = DEFAULT_THUMB_MAX_DIM
    port: int = 0
    limit: int | None = None
    reclassify: bool = False
    open_browser: bool = True
    verbose: bool = False

    connect_timeout: float = DEFAULT_LM_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_LM_READ_TIMEOUT

    @classmethod
    def create(
        cls,
        output_root: Path,
        *,
        config_root: Path | None = None,
        **overrides,
    ) -> "LocalCleanConfig":
        output_root = Path(output_root).resolve()
        config_root = (config_root or default_config_root()).resolve()
        logs_dir = config_root / "logs"
        state_dir = config_root / "state"
        cache_db = state_dir / f"local-clean-{_clean_cache_key(output_root)}.db"

        for d in (config_root, logs_dir, state_dir):
            d.mkdir(parents=True, exist_ok=True)

        cfg = cls(output_root=output_root, cache_db=cache_db, logs_dir=logs_dir)
        for k, v in overrides.items():
            if not hasattr(cfg, k):
                # Match AppConfig.create: a silently-dropped typo would run with
                # defaults and be near-impossible to debug; fail loudly instead.
                raise TypeError(f"unknown config override: {k!r}")
            if v is not None:
                setattr(cfg, k, v)
        return cfg

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)


@dataclass
class VideoCleanConfig:
    """Configuration for the credential-free ``video-clean`` command.

    Like :class:`LocalCleanConfig` this needs no Apple ID, but it also needs no
    classification cache or vision model: the scan is a plain size-sorted walk
    and every decision is the user's, so the only knobs are an optional
    minimum-size floor and the review server's browser/port behaviour.
    """

    output_root: Path
    logs_dir: Path

    min_bytes: int = 0
    port: int = 0
    open_browser: bool = True
    verbose: bool = False

    @classmethod
    def create(
        cls,
        output_root: Path,
        *,
        config_root: Path | None = None,
        **overrides,
    ) -> "VideoCleanConfig":
        output_root = Path(output_root).resolve()
        config_root = (config_root or default_config_root()).resolve()
        logs_dir = config_root / "logs"

        for d in (config_root, logs_dir):
            d.mkdir(parents=True, exist_ok=True)

        cfg = cls(output_root=output_root, logs_dir=logs_dir)
        for k, v in overrides.items():
            if not hasattr(cfg, k):
                # Match AppConfig.create: a silently-dropped typo would run with
                # defaults and be near-impossible to debug; fail loudly instead.
                raise TypeError(f"unknown config override: {k!r}")
            if v is not None:
                setattr(cfg, k, v)
        return cfg


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
