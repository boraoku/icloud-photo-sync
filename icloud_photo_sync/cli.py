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

from . import config as cfg
from . import icloud_client as ic
from .auth import SessionManager
from .config import MIN_WATCH_INTERVAL, AppConfig
from .downloader import Downloader
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
