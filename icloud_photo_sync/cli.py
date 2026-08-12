"""Command-line surface (C8).

Mental model:  ``sync`` = start,  Ctrl-C = stop,  ``sync`` again = resume,
``sync --watch N`` = stay current.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from threading import Event
from typing import Optional

import typer
from tqdm import tqdm

from . import clean_icloud
from . import config as cfg
from . import icloud_client as ic
from .auth import SessionManager
from .classifier import CATEGORIES
from .config import (
    MIN_WATCH_INTERVAL,
    AppConfig,
    ICloudDeleteConfig,
    LocalCleanConfig,
    VideoCleanConfig,
)
from .downloader import Downloader
from .local_clean import run_local_clean, _parse_size
from .video_clean import run_video_clean
from .errors import (
    AccountPreconditionError,
    AcceptTermsError,
    ICloudSyncError,
    SessionExpiredError,
)
from .logutil import get_logger, setup_logging
from .orchestrator import Orchestrator
from .paths import PathResolver
from .state import StateStore

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="One-way iCloud Photos downloader → ./YYYY/MM/ (by capture date).",
)
logger = get_logger("icloud_photo_sync")


class AppContext:
    def __init__(self, username, directory, verbose, reset_keyring):
        self.username = username
        self.directory = directory
        self.verbose = verbose
        self.reset_keyring = reset_keyring


@app.callback()
def main(
    ctx: typer.Context,
    username: Optional[str] = typer.Option(
        None, "--username", "-u", help="Apple ID (else $ICLOUD_SYNC_USERNAME or prompt)."
    ),
    directory: Optional[Path] = typer.Option(
        None, "--directory", "-d", help="Output root (default: current directory)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
    reset_keyring: bool = typer.Option(
        False, "--reset-keyring", help="Forget the stored iCloud password."
    ),
) -> None:
    ctx.obj = AppContext(username, directory, verbose, reset_keyring)


# --- helpers -----------------------------------------------------------------


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024 or unit == "TiB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} TiB"


def _build_config(ctx: typer.Context, **overrides) -> AppConfig:
    octx: AppContext = ctx.obj
    apple_id = cfg.resolve_username(octx.username)
    if not apple_id:
        apple_id = typer.prompt("Apple ID (email)")
    output_root = (octx.directory or Path.cwd()).resolve()
    config = AppConfig.create(apple_id, output_root, verbose=octx.verbose, **overrides)
    setup_logging(config.logs_dir, config.verbose)
    if octx.reset_keyring:
        if cfg.delete_password(apple_id):
            typer.secho(f"Cleared stored password for {apple_id}.", fg=typer.colors.YELLOW)
        else:
            typer.secho("No stored password to clear.", fg=typer.colors.YELLOW)
        # pyicloud keeps its own keychain entry and silently falls back to it;
        # clear that too or a stale password keeps being replayed at Apple.
        if ic.clear_engine_credentials(apple_id):
            typer.secho("Cleared pyicloud's keychain entry as well.", fg=typer.colors.YELLOW)
    return config


def _build_icloud_delete(
    ctx: typer.Context, output_root: Path, *, enabled: bool, **overrides
) -> "ICloudDeleteConfig | None":
    """Resolve the Apple ID only when the user opted in.

    This call site *is* the credential-free contract: without --icloud-delete no
    Apple ID is resolved, no Keychain is read and no network is touched.
    """
    if not enabled:
        flags = {"dry_run": "--icloud-dry-run", "max_delete": "--max-delete"}
        bad = [flags.get(name, f"--{name.replace('_', '-')}")
               for name, value in overrides.items() if value]
        if bad:
            raise typer.BadParameter(
                f"{bad[0]} only means something together with --icloud-delete."
            )
        return None
    octx: AppContext = ctx.obj
    apple_id = cfg.resolve_username(octx.username) or typer.prompt("Apple ID (email)")
    return ICloudDeleteConfig.create(apple_id, output_root, **overrides)


def _fail(message: str, code: int) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _install_signal_handlers(cancel: Event) -> None:
    def handler(signum, frame):  # noqa: ANN001
        if cancel.is_set():
            typer.secho("\nForced stop.", fg=typer.colors.RED, err=True)
            os._exit(130)
        cancel.set()
        typer.secho(
            "\nStopping after the current chunk… (Ctrl-C again to force).",
            fg=typer.colors.YELLOW,
            err=True,
        )

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):  # not in main thread (e.g. tests)
        pass


# --- commands ----------------------------------------------------------------


@app.command()
def login(ctx: typer.Context) -> None:
    """Authenticate and persist the session (handles 2FA). Run once initially."""
    config = _build_config(ctx)
    sm = SessionManager(config)

    password = cfg.get_password(config.apple_id)
    had_saved = password is not None
    if not password:
        password = typer.prompt(
            f"iCloud password for {config.apple_id}", hide_input=True
        )

    def code_provider(prompt: str) -> str:
        return typer.prompt(prompt)

    retried = False
    while True:
        try:
            service, client = sm.login(password, code_provider)
            sm.verify_access(client)
            break
        except AccountPreconditionError as exc:
            _fail(str(exc), 2)
        except AcceptTermsError as exc:
            _fail(str(exc), 4)
        except ICloudSyncError as exc:
            # A saved Keychain password may be stale (Apple ID password was
            # changed): re-prompt once instead of failing into a dead end.
            if had_saved and not retried:
                retried = True
                had_saved = False
                typer.secho(
                    f"Login with the saved Keychain password failed: {exc}",
                    fg=typer.colors.YELLOW,
                )
                password = typer.prompt(
                    f"iCloud password for {config.apple_id}", hide_input=True
                )
                continue
            _fail(f"Login failed: {exc}", 1)

    if not had_saved:
        if typer.confirm("Save password to the macOS Keychain for future runs?", default=True):
            if cfg.set_password(config.apple_id, password):
                typer.secho("Password saved to Keychain.", fg=typer.colors.GREEN)
            else:
                typer.secho("Could not save to Keychain (continuing).", fg=typer.colors.YELLOW)

    name = ic.account_name(service) or config.apple_id
    typer.secho(f"Login successful for {name}. Session persisted.", fg=typer.colors.GREEN)


@app.command()
def sync(
    ctx: typer.Context,
    update: bool = typer.Option(False, "--update", help="Incremental pass only (newest-first, early-stop)."),
    watch: Optional[int] = typer.Option(None, "--watch", metavar="SECONDS", help="Loop the incremental pass every N seconds."),
    until_found: Optional[int] = typer.Option(None, "--until-found", help="Consecutive already-have assets before an incremental pass stops."),
) -> None:
    """Download everything not yet downloaded. Stop with Ctrl-C; re-run to resume."""
    config = _build_config(ctx, until_found=until_found)
    sm = SessionManager(config)

    try:
        service, client = sm.resume()
    except SessionExpiredError as exc:
        _fail(str(exc), 3)
    except AccountPreconditionError as exc:
        _fail(str(exc), 2)
    except ICloudSyncError as exc:
        _fail(f"Could not start: {exc}", 1)

    if watch is not None and watch < MIN_WATCH_INTERVAL:
        typer.secho(
            f"--watch raised to the minimum interval of {MIN_WATCH_INTERVAL}s "
            "to avoid throttling.",
            fg=typer.colors.YELLOW,
        )
        watch = MIN_WATCH_INTERVAL

    state = StateStore(config.state_db)
    paths = PathResolver(config.output_root)
    cancel = Event()
    _install_signal_handlers(cancel)
    client.set_cancel_event(cancel)  # makes the indexing wait Ctrl-C-able
    downloader = Downloader(client, state, config, cancel)
    orch = Orchestrator(client, state, paths, downloader, config, cancel)

    typer.secho(f"Output: {config.output_root}", fg=typer.colors.BLUE)
    try:
        if watch is not None:
            orch.run_watch(interval=watch, until_found=until_found)
        elif update:
            stats = orch.run_incremental(until_found)
            _print_stats(stats)
        else:
            stats = orch.run_full()
            _print_stats(stats)
    except ICloudSyncError as exc:
        typer.secho(f"\nStopped: {exc}", fg=typer.colors.RED, err=True)
    finally:
        state.close()


@app.command()
def status(ctx: typer.Context) -> None:
    """Show completed / pending / failed counts, last pass times, and total bytes."""
    config = _build_config(ctx)
    state = StateStore(config.state_db)
    try:
        counts = state.counts()
        done_bytes = state.total_bytes_completed()
        last_full = state.get_meta("last_full_pass_at")
        last_update = state.get_meta("last_update_at")

        typer.secho(f"iCloud account : {config.apple_id}", bold=True)
        typer.echo(f"Output root    : {config.output_root}")
        typer.echo(f"State DB       : {config.state_db}")
        typer.echo("")
        typer.secho(f"Completed : {counts['completed']}", fg=typer.colors.GREEN)
        typer.secho(f"Pending   : {counts['pending']}", fg=typer.colors.YELLOW)
        typer.secho(f"Failed    : {counts['failed']}", fg=(typer.colors.RED if counts["failed"] else None))
        typer.echo(f"Total seen: {counts['total']}")
        typer.echo(f"Downloaded: {_human(done_bytes)}")
        typer.echo("")
        typer.echo(f"Last full pass : {last_full or 'never'}")
        typer.echo(f"Last update    : {last_update or 'never'}")

        if counts["failed"]:
            typer.secho(
                f"\nFailed assets (retried on next sync) — showing "
                f"{min(counts['failed'], 10)} of {counts['failed']}:",
                fg=typer.colors.RED,
            )
            for row in state.iter_failed(limit=10):
                typer.echo(f"  {row['filename']}: {row['error']}")
    finally:
        state.close()


@app.command("local-clean")
def local_clean(
    ctx: typer.Context,
    max_size: str = typer.Option(
        "1MB", "--max-size", help="Only images at or below this size (e.g. 500KB, 2MB)."
    ),
    lm_url: str = typer.Option(
        cfg.DEFAULT_LM_BASE_URL, "--lm-url", envvar=cfg.ENV_LM_URL,
        help="Local vision-model base URL (LM Studio, OpenAI-compatible).",
    ),
    lm_model: str = typer.Option(
        cfg.DEFAULT_LM_MODEL, "--lm-model", help="Vision model name."
    ),
    flag: str = typer.Option(
        ",".join(cfg.DEFAULT_FLAG_CATEGORIES), "--flag",
        help="Comma-separated categories to flag for deletion "
             f"(any of: {', '.join(CATEGORIES)}).",
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Classify at most N new images this run (resume later)."
    ),
    port: int = typer.Option(0, "--port", help="Review server port (0 = auto)."),
    reclassify: bool = typer.Option(
        False, "--reclassify", help="Ignore the classification cache and redo everything."
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the review URL instead of opening a browser."
    ),
    icloud_delete: bool = typer.Option(
        False, "--icloud-delete",
        help="Also offer to delete trashed files from iCloud (asks before doing it).",
    ),
    icloud_dry_run: bool = typer.Option(
        False, "--icloud-dry-run", help="Show what would be deleted from iCloud, delete nothing."
    ),
    max_delete: Optional[int] = typer.Option(
        None, "--max-delete", help="Cap iCloud deletions per run (default 500)."
    ),
) -> None:
    """Find small screenshots/memes locally, review them, move to Trash. No iCloud login unless --icloud-delete."""
    octx: AppContext = ctx.obj
    output_root = (octx.directory or Path.cwd()).resolve()

    flag_categories = tuple(c.strip() for c in flag.split(",") if c.strip())
    bad = [c for c in flag_categories if c not in CATEGORIES]
    if bad:
        raise typer.BadParameter(
            f"unknown --flag categories {bad}; choose from {', '.join(CATEGORIES)}"
        )

    config = LocalCleanConfig.create(
        output_root,
        max_bytes=_parse_size(max_size),
        lm_base_url=lm_url,
        lm_model=lm_model,
        flag_categories=flag_categories,
        limit=limit,
        port=port,
        reclassify=reclassify,
        open_browser=not no_browser,
        verbose=octx.verbose,
    )
    setup_logging(config.logs_dir, config.verbose)
    icloud = _build_icloud_delete(ctx, output_root, enabled=icloud_delete,
                                  dry_run=icloud_dry_run, max_delete=max_delete)
    raise typer.Exit(run_local_clean(config, icloud))


@app.command("video-clean")
def video_clean(
    ctx: typer.Context,
    min_size: str = typer.Option(
        "0", "--min-size", help="Only list videos at or above this size (e.g. 50MB, 1GB)."
    ),
    port: int = typer.Option(0, "--port", help="Review server port (0 = auto)."),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the review URL instead of opening a browser."
    ),
    icloud_delete: bool = typer.Option(
        False, "--icloud-delete",
        help="Also offer to delete trashed videos from iCloud (asks before doing it).",
    ),
    icloud_dry_run: bool = typer.Option(
        False, "--icloud-dry-run", help="Show what would be deleted from iCloud, delete nothing."
    ),
    max_delete: Optional[int] = typer.Option(
        None, "--max-delete", help="Cap iCloud deletions per run (default 500)."
    ),
) -> None:
    """List downloaded videos largest-first, preview them, move selections to Trash. No iCloud login unless --icloud-delete."""
    octx: AppContext = ctx.obj
    output_root = (octx.directory or Path.cwd()).resolve()

    config = VideoCleanConfig.create(
        output_root,
        min_bytes=_parse_size(min_size),
        port=port,
        open_browser=not no_browser,
        verbose=octx.verbose,
    )
    setup_logging(config.logs_dir, config.verbose)
    icloud = _build_icloud_delete(ctx, output_root, enabled=icloud_delete,
                                  dry_run=icloud_dry_run, max_delete=max_delete)
    raise typer.Exit(run_video_clean(config, icloud))


@app.command("icloud-delete")
def icloud_delete_cmd(
    ctx: typer.Context,
    from_manifest: Optional[Path] = typer.Option(
        None, "--from", metavar="MANIFEST",
        help="Apply a deletion manifest written by a clean session.",
    ),
    last: bool = typer.Option(
        False, "--last", help="Use the newest manifest for this Apple ID and folder."
    ),
    explain: Optional[Path] = typer.Option(
        None, "--explain", metavar="RECEIPT",
        help="Read-only: report the current iCloud state of everything in a receipt.",
    ),
    scan_trashed: bool = typer.Option(
        False, "--scan-trashed",
        help="Reconcile the folder against the manifest and offer files trashed "
             "by an earlier session that ran without --icloud-delete.",
    ),
    max_size: str = typer.Option(
        "1MB", "--max-size",
        help="--scan-trashed: the --max-size those local-clean sessions used.",
    ),
    min_size: str = typer.Option(
        "0", "--min-size",
        help="--scan-trashed: the --min-size those video-clean sessions used.",
    ),
    corroborate_root: Optional[list[Path]] = typer.Option(
        None, "--corroborate-root", metavar="DIR",
        help="--scan-trashed: another copy of this library; anything still there "
             "at the same size is left alone. Repeatable.",
    ),
    no_review: bool = typer.Option(
        False, "--no-review",
        help="--scan-trashed: skip the thumbnail review and rely on the evidence "
             "gates alone.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be deleted, delete nothing."
    ),
    max_delete: Optional[int] = typer.Option(
        None, "--max-delete",
        help="Refuse the run above N assets (default 500; --scan-trashed 2000)."
    ),
) -> None:
    """Finish (or retry) deleting already-trashed files from iCloud.

    A clean session writes its manifest before touching iCloud, so an expired
    session, a dropped connection or a Ctrl-C never loses the work: come back
    here with --last. Anything already deleted is skipped, so re-running only
    retries what did not land.

    --scan-trashed covers the other case: files trashed by a session that never
    armed iCloud deletion at all, so no manifest exists. That evidence is weaker
    — the file was gone before this ran, so its size could not be measured — and
    the run refuses outright unless the whole reconstruction holds together.
    Start with --dry-run.
    """
    octx: AppContext = ctx.obj
    output_root = (octx.directory or Path.cwd()).resolve()
    if not (from_manifest or last or explain or scan_trashed):
        raise typer.BadParameter(
            "pass --from MANIFEST, --last, --explain RECEIPT, or --scan-trashed.")
    if scan_trashed and (from_manifest or last or explain):
        raise typer.BadParameter(
            "--scan-trashed builds its own plan; it cannot be combined with "
            "--from, --last or --explain.")

    icloud = _build_icloud_delete(ctx, output_root, enabled=True,
                                  dry_run=dry_run, max_delete=max_delete)
    setup_logging(icloud.app.logs_dir, octx.verbose)
    try:
        if explain:
            raise typer.Exit(clean_icloud.explain_receipt(icloud, explain))
        if scan_trashed:
            raise typer.Exit(clean_icloud.run_retro(
                icloud,
                max_bytes=_parse_size(max_size),
                min_bytes=_parse_size(min_size),
                corroborate_roots=[p.resolve() for p in (corroborate_root or [])],
                no_review=no_review,
                progress=tqdm,
            ))
        raise typer.Exit(clean_icloud.run_from_manifest(icloud, from_manifest))
    except AccountPreconditionError as exc:
        _fail(str(exc), 2)
    except SessionExpiredError as exc:
        _fail(str(exc), 3)
    except ICloudSyncError as exc:
        _fail(str(exc), 1)


def _print_stats(stats) -> None:
    color = typer.colors.GREEN
    if stats.failed:
        color = typer.colors.YELLOW
    if stats.cancelled:
        color = typer.colors.YELLOW
    typer.secho(f"\nDone: {stats.summary()}", fg=color)
    typer.echo("Run `icloud-photo-sync status` for details.")


def main_entry() -> None:
    app()


if __name__ == "__main__":
    app()
