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

from .icloud_delete import (
    DEFAULT_BATCH_SIZE as DEFAULT_DELETE_BATCH,
    DEFAULT_MAX_DELETE,
    MAX_DELETE_CEILING,
)

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

# video-clean: hidden cache dir inside the photo tree (dot-dir: the scan prunes it).
POSTER_CACHE_DIRNAME = ".icloud-photo-sync"

# video-optimise. The rationale for each of these lives in
# :mod:`icloud_photo_sync.video_optimise`, which owns the policy; these are only
# the CLI-overridable defaults. The floor is the important one: on a real
# 1,504-video library the 637 clips at or above 20 MiB held 90% of all video
# bytes, and the 722 smaller ones would have added four hours of encoding for
# 2.5 GiB.
DEFAULT_OPTIMISE_MIN_BYTES = 20 * 1024 * 1024
DEFAULT_OPTIMISE_SHORT_SIDE = 1080     # the SHORTER side, never a bounding box
DEFAULT_OPTIMISE_MAX_FPS = 30.0        # never applied to slow motion
DEFAULT_OPTIMISE_HDR_BITRATE = 8_000_000
DEFAULT_OPTIMISE_SDR_BITRATE = 6_000_000
DEFAULT_OPTIMISE_MIN_FREE = 5 * 1024 * 1024 * 1024   # headroom on the output volume

# Where video-optimise leaves finished conversions for the user to upload. A
# plain, visible folder at the top of the photo tree, deliberately NOT inside
# the dot-directory the poster cache uses: Finder and the iOS Files app both
# hide dot-folders, and this folder exists to be opened and dragged out of.
# Every scanner excludes it by name (see local_clean.iter_media_files).
OPTIMISED_DIRNAME = "optimised"

# The scan envelopes: which files each clean command will even look at. They live
# here rather than in the clean modules because ``retro_clean`` reasons about
# them too — "no clean command could have offered this file" is only answerable
# if the envelope is one shared definition. Re-exported from local_clean and
# video_clean, which is where you would look for them first.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
VIDEO_SUFFIXES = {
    ".mov", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".3gp", ".3g2",
    ".wmv", ".webm", ".mkv", ".mts", ".m2ts",
}


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

    Like :class:`LocalCleanConfig` this needs no Apple ID, and no vision model:
    the scan is a plain size-sorted walk and every decision is the user's, so
    the only knobs are an optional minimum-size floor and the review server's
    browser/port behaviour. Its one piece of state is the poster-frame cache
    the review grid draws from, kept in a hidden directory inside the photo
    tree so it travels with the files it describes.
    """

    output_root: Path
    logs_dir: Path
    poster_cache_dir: Path

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

        # Posters live beside the videos they belong to — a hidden directory, so
        # the scan (which prunes dot-dirs) never sees them, and unplugging the
        # drive takes its cache with it. A read-only or full volume falls back
        # to the per-user config root rather than failing the command.
        poster_cache_dir = output_root / POSTER_CACHE_DIRNAME / "posters"
        try:
            poster_cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            poster_cache_dir = (
                config_root / "cache"
                / f"video-posters-{_clean_cache_key(output_root)}"
            )
            poster_cache_dir.mkdir(parents=True, exist_ok=True)

        cfg = cls(output_root=output_root, logs_dir=logs_dir,
                  poster_cache_dir=poster_cache_dir)
        for k, v in overrides.items():
            if not hasattr(cfg, k):
                # Match AppConfig.create: a silently-dropped typo would run with
                # defaults and be near-impossible to debug; fail loudly instead.
                raise TypeError(f"unknown config override: {k!r}")
            if v is not None:
                setattr(cfg, k, v)
        return cfg


@dataclass
class VideoOptimiseConfig:
    """Configuration for ``video-optimise``: re-encode big videos, then swap them.

    Like :class:`VideoCleanConfig` the *conversion* half needs no Apple ID — with
    ``--no-upload`` the whole scan/convert/compare flow runs offline, and the
    credential-free contract holds. Only the swap phase resolves an Apple ID, and
    it does so through :class:`ICloudDeleteConfig` exactly as the clean commands
    do, so there is never a second opinion about which library a folder is.

    Two directories matter. Converted files land in ``work_dir``, a hidden folder
    inside the photo tree: the scanners prune dot-directories so a half-finished
    run can never be mistaken for library content, and unplugging the drive takes
    the work in progress with it. The job database lives in the config root
    instead, keyed by ``(apple_id, output_root)`` like every other durable file
    here, because a resumable job must survive the tree being remounted.
    """

    output_root: Path
    logs_dir: Path
    work_dir: Path
    job_db: Path
    poster_cache_dir: Path

    min_bytes: int = DEFAULT_OPTIMISE_MIN_BYTES
    short_side: int = DEFAULT_OPTIMISE_SHORT_SIDE
    max_fps: float = DEFAULT_OPTIMISE_MAX_FPS
    hdr_bitrate: int = DEFAULT_OPTIMISE_HDR_BITRATE
    sdr_bitrate: int = DEFAULT_OPTIMISE_SDR_BITRATE

    skip_hdr: bool = False
    hdr_only: bool = False
    limit: int | None = None
    """``--limit``: convert at most N videos this run, resuming the rest later."""

    min_free_bytes: int = DEFAULT_OPTIMISE_MIN_FREE
    dry_run: bool = False
    offline: bool = False
    """Convert only: resolve no Apple ID, read no Keychain, touch no network.

    Renamed from ``--no-upload``, which stopped describing anything once Apple
    closed the upload endpoint — nothing uploads on any path now. What the flag
    still controls is real, though: whether this command has an iCloud identity
    at all, which is the credential-free contract ``local-clean`` and
    ``video-clean`` also hold.
    """
    reconcile_only: bool = False
    """Finish yesterday's uploads and stop: reconcile, delete, clean up."""
    retry_colour_mismatch: bool = False
    """Give every ``colour_mismatch`` row one more attempt. See
    :meth:`icloud_photo_sync.optimise_job.OptimiseJob.retry_colour_mismatch`
    for why this needs an explicit flag rather than happening on every run."""
    restart: bool = False

    port: int = 0
    open_browser: bool = True
    verbose: bool = False

    @classmethod
    def create(
        cls,
        output_root: Path,
        *,
        apple_id: str | None = None,
        config_root: Path | None = None,
        **overrides,
    ) -> "VideoOptimiseConfig":
        output_root = Path(output_root).resolve()
        config_root = (config_root or default_config_root()).resolve()
        logs_dir = config_root / "logs"
        state_dir = config_root / "state"
        for d in (config_root, logs_dir, state_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Keyed by the folder alone when no Apple ID is in play (--no-upload), so
        # an offline run and a later online one on the same tree share one job.
        key = (_state_key(apple_id, output_root) if apple_id
               else _clean_cache_key(output_root))

        # Visible, flat, at the top of the tree: the user has to be able to find
        # this folder and drag its contents into iCloud, which a dot-directory
        # and a nested YYYY/MM structure both make needlessly hard. A read-only
        # or full volume falls back to the config root rather than failing.
        work_dir = output_root / OPTIMISED_DIRNAME
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            work_dir = config_root / "cache" / f"optimised-{_clean_cache_key(output_root)}"
            work_dir.mkdir(parents=True, exist_ok=True)

        poster_cache_dir = output_root / POSTER_CACHE_DIRNAME / "posters"
        try:
            poster_cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            poster_cache_dir = (config_root / "cache"
                                / f"video-posters-{_clean_cache_key(output_root)}")
            poster_cache_dir.mkdir(parents=True, exist_ok=True)

        cfg = cls(
            output_root=output_root, logs_dir=logs_dir, work_dir=work_dir,
            job_db=state_dir / f"video-optimise-{key}.db",
            poster_cache_dir=poster_cache_dir,
        )
        for k, v in overrides.items():
            if not hasattr(cfg, k):
                # Match AppConfig.create: a silently-dropped typo would run with
                # defaults and be near-impossible to debug; fail loudly instead.
                raise TypeError(f"unknown config override: {k!r}")
            if v is not None:
                setattr(cfg, k, v)
        if cfg.skip_hdr and cfg.hdr_only:
            raise ValueError("--skip-hdr and --hdr-only ask for opposite things.")
        return cfg

    @property
    def legacy_work_dir(self) -> Path:
        """Where conversions lived before the folder became visible and flat.

        Kept so a job started under the old layout can be migrated rather than
        re-encoded from scratch — the files are hours of work and perfectly good.
        """
        return self.output_root / POSTER_CACHE_DIRNAME / "optimised"

    def work_path(self, name: str) -> Path:
        """Absolute path of a flat conversion name inside :attr:`work_dir`."""
        return self.work_dir / name


@dataclass
class ICloudDeleteConfig:
    """Opt-in "also delete from iCloud" side-car for the clean commands.

    Deliberately *not* a field of :class:`LocalCleanConfig` /
    :class:`VideoCleanConfig`: those keep their "no iCloud login" contract, and
    this is the only object in either flow that knows an Apple ID exists. It
    wraps :class:`AppConfig` verbatim so the manifest it reads is guaranteed to
    be the very same file ``sync`` writes — same ``(apple_id, output_root)``
    derivation, no second opinion about which library a folder belongs to.
    """

    app: AppConfig
    manifest_dir: Path
    state_key: str
    cache_db: Path
    """local-clean's classification cache — read-only evidence for retro_clean.

    Derived exactly as :class:`LocalCleanConfig` derives it, so both names point
    at one file. Never created from here; an absent cache simply contributes no
    evidence.
    """
    dry_run: bool = False

    max_delete: int | None = None
    """``--max-delete``, or None when the user did not say.

    None is meaningful, not a stand-in for the default: the measured path falls
    back to :data:`DEFAULT_MAX_DELETE` per run, while a retrospective run bounds
    itself by :data:`MAX_DELETE_CEILING` instead — it asks for one deliberate
    confirmation covering everything rather than one per N assets.
    """

    batch_size: int = DEFAULT_DELETE_BATCH

    @property
    def per_run_limit(self) -> int:
        """The measured path's cap: what ``--max-delete`` means when unset."""
        return DEFAULT_MAX_DELETE if self.max_delete is None else self.max_delete

    @classmethod
    def create(
        cls,
        apple_id: str,
        output_root: Path,
        *,
        config_root: Path | None = None,
        **overrides,
    ) -> "ICloudDeleteConfig":
        app = AppConfig.create(apple_id, output_root, config_root=config_root)
        manifest_dir = app.config_root / "deletions"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        cfg = cls(
            app=app,
            manifest_dir=manifest_dir,
            state_key=_state_key(app.apple_id, app.output_root),
            cache_db=(app.config_root / "state"
                      / f"local-clean-{_clean_cache_key(app.output_root)}.db"),
        )
        for k, v in overrides.items():
            if not hasattr(cfg, k):
                raise TypeError(f"unknown config override: {k!r}")
            if v is not None:
                setattr(cfg, k, v)
        if cfg.max_delete is not None and cfg.max_delete > MAX_DELETE_CEILING:
            raise ValueError(
                f"--max-delete cannot exceed {MAX_DELETE_CEILING}; split the work instead."
            )
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
