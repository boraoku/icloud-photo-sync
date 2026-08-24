"""CLI-level tests: mostly ``_run_guarded``.

``local-clean --icloud-delete``, ``video-clean --icloud-delete`` and
``video-optimise`` each call ``clean_icloud.arm()`` deep inside their own
``run_*`` function, well after argument parsing — so nothing at the CLI call
site was catching what it can raise. ``sync`` and ``icloud-delete`` already
handled this at their own call sites (and are not retested here; they were
never broken). A session that had simply expired between one run and the next
surfaced, for the other three, as a raw Python traceback
(``SessionExpiredError: ...``) instead of red text and a clean exit code —
read by a user as "this app broke", not "please log in again". Every test here
drives the real ``typer`` command through :class:`~typer.testing.CliRunner`,
because the bug was specifically about what reaches the terminal, not about
what any internal function returns.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from icloud_photo_sync import cli
from icloud_photo_sync.cli import _run_guarded, app
from icloud_photo_sync.errors import (
    AccountPreconditionError,
    ICloudSyncError,
    SessionExpiredError,
)

runner = CliRunner()


# --- _run_guarded, in isolation -----------------------------------------------


class TestRunGuarded:
    def test_session_expired_exits_3_with_the_login_guidance(self):
        def raiser():
            raise SessionExpiredError("iCloud session expired or two-factor "
                                      "authentication is required.\n"
                                      "Run:  icloud-photo-sync login")
        with pytest.raises(typer.Exit) as exc:
            _run_guarded(raiser)
        assert exc.value.exit_code == 3

    def test_account_precondition_exits_2(self):
        def raiser():
            raise AccountPreconditionError("ADP is on.")
        with pytest.raises(typer.Exit) as exc:
            _run_guarded(raiser)
        assert exc.value.exit_code == 2

    def test_generic_icloud_error_exits_1(self):
        def raiser():
            raise ICloudSyncError("something else.")
        with pytest.raises(typer.Exit) as exc:
            _run_guarded(raiser)
        assert exc.value.exit_code == 1

    def test_the_command_return_code_passes_through_on_success(self):
        with pytest.raises(typer.Exit) as exc:
            _run_guarded(lambda: 0)
        assert exc.value.exit_code == 0

    def test_a_non_zero_return_code_passes_through_too(self):
        with pytest.raises(typer.Exit) as exc:
            _run_guarded(lambda: 5)
        assert exc.value.exit_code == 5

    def test_an_unrelated_exception_is_not_swallowed(self):
        # _run_guarded only narrows iCloud auth/precondition failures; anything
        # else must still surface as itself, not be silently downgraded.
        def raiser():
            raise ValueError("not an iCloud problem")
        with pytest.raises(ValueError):
            _run_guarded(raiser)


# --- the three previously-unguarded commands, end to end ----------------------


@pytest.fixture
def fake_session_expired(monkeypatch):
    """Make every command's ``arm()`` fail exactly as an expired session does.

    ``optimise.py`` imports ``arm`` by name (``from .clean_icloud import arm``),
    so it needs its own binding patched; ``local_clean.py`` and
    ``video_clean.py`` call it as ``clean_icloud.arm(...)``, so patching the
    attribute on the ``clean_icloud`` module covers both at once.
    """
    def raise_expired(*a, **k):
        raise SessionExpiredError(
            "iCloud session expired or two-factor authentication is required.\n"
            "Run:  icloud-photo-sync login")
    monkeypatch.setattr(cli.clean_icloud, "arm", raise_expired)
    from icloud_photo_sync import optimise as optimise_module
    monkeypatch.setattr(optimise_module, "arm", raise_expired)
    # Preflight would otherwise refuse first on a machine without ffmpeg —
    # irrelevant to what this test is about, so it is made to pass unconditionally.
    monkeypatch.setattr(optimise_module.tc, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(optimise_module.tc, "encoder_available", lambda name=None: True)


class TestSessionExpiredIsCleanEverywhere:
    def _assert_clean_failure(self, result):
        # A clean exit still shows up as SystemExit(code) in CliRunner's
        # bookkeeping for any non-zero code -- that alone isn't the signal.
        # What must never be true is the RAW error becoming the thing that
        # terminated the process; that is exactly the unhandled traceback this
        # fix exists to prevent.
        assert result.exit_code == 3, result.output
        assert not isinstance(result.exception, ICloudSyncError), (
            f"the raw {type(result.exception).__name__} reached the CLI "
            "boundary uncaught instead of being turned into a clean message"
        )
        assert "Run:" in result.output and "login" in result.output

    def test_local_clean_icloud_delete(self, tmp_path, fake_session_expired):
        result = runner.invoke(app, [
            "-u", "person@example.com", "-d", str(tmp_path),
            "local-clean", "--icloud-delete",
        ])
        self._assert_clean_failure(result)

    def test_video_clean_icloud_delete(self, tmp_path, fake_session_expired):
        result = runner.invoke(app, [
            "-u", "person@example.com", "-d", str(tmp_path),
            "video-clean", "--icloud-delete",
        ])
        self._assert_clean_failure(result)

    def test_video_optimise(self, tmp_path, fake_session_expired):
        result = runner.invoke(app, [
            "-u", "person@example.com", "-d", str(tmp_path), "video-optimise",
        ])
        self._assert_clean_failure(result)

    def test_video_optimise_offline_is_unaffected(self, tmp_path, fake_session_expired):
        # --offline never resolves an Apple ID, so arm() is never called at
        # all; this is the negative case proving the fixture targets the right
        # thing rather than something that always happens to exit cleanly.
        result = runner.invoke(app, [
            "-u", "person@example.com", "-d", str(tmp_path),
            "video-optimise", "--offline",
        ])
        assert result.exit_code == 0
        assert "login" not in result.output


# --- the long-running commands must actually report progress -------------------


class TestProgressIsWired:
    """The bug this guards: ``photo-optimise-external`` built a tqdm bar
    internally but the CLI never handed it a factory, so a 47,000-photo run
    printed nothing at all for over an hour. The bar working is not enough —
    the wiring has to be there, which is exactly what was missing.
    """

    def test_photo_optimise_external_passes_a_progress_factory(self, tmp_path, monkeypatch):
        seen = {}

        def fake_run(config, *, echo=None, progress=None, dry_run=False):
            seen["progress"] = progress
            return 0

        monkeypatch.setattr(cli, "run_photo_optimise_external", fake_run)
        result = runner.invoke(app, [
            "-d", str(tmp_path), "photo-optimise-external", "--dry-run",
        ])

        assert result.exit_code == 0
        assert seen["progress"] is not None, "no progress factory reached Phase A"
        assert callable(seen["progress"])

    def test_video_optimise_external_passes_a_progress_factory(self, tmp_path, monkeypatch):
        seen = {}

        def fake_run(config, **kwargs):
            seen.update(kwargs)
            return 0

        monkeypatch.setattr(cli, "run_optimise_external", fake_run)
        result = runner.invoke(app, [
            "-d", str(tmp_path), "video-optimise-external", "--dry-run",
        ])

        assert result.exit_code == 0
        assert seen.get("progress") is not None
