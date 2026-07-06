"""Trash-mover tests with an injected osascript runner."""

import subprocess
from pathlib import Path

from icloud_photo_sync.trash import _escape, move_to_trash


class FakeProc:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def make_runner(fail_on=None, calls=None):
    """Runner that trashes files (deletes them) unless their path is in fail_on."""
    fail_on = set(fail_on or [])

    def runner(argv, input="", capture_output=True, text=True, timeout=None):
        # Which paths does this batch reference?
        batch = [ln for ln in input.splitlines() if "POSIX file" in ln]
        if calls is not None:
            calls.append(input)
        # Extract the abs paths from the script's POSIX file entries.
        paths = []
        for token in input.split('POSIX file "')[1:]:
            paths.append(token.split('"')[0])
        bad = [p for p in paths if p in fail_on]
        if bad:
            return FakeProc(returncode=1, stderr="error: -10004 or similar")
        for p in paths:
            Path(p).unlink(missing_ok=True)
        return FakeProc(returncode=0)

    return runner


def _touch(tmp_path, name):
    p = tmp_path / name
    p.write_text("x")
    return p


def test_escape_handles_quotes_and_backslashes():
    s = _escape(Path('/a/b"c\\d.jpg'))
    assert s == 'POSIX file "/a/b\\"c\\\\d.jpg"'


def test_all_ok(tmp_path):
    paths = [_touch(tmp_path, f"f{i}.jpg") for i in range(3)]
    results = move_to_trash(paths, runner=make_runner())
    assert all(r.ok for r in results)
    assert all(not p.exists() for p in paths)


def test_batches(tmp_path):
    paths = [_touch(tmp_path, f"f{i}.jpg") for i in range(250)]
    calls = []
    results = move_to_trash(paths, runner=make_runner(calls=calls), batch_size=100)
    assert len(calls) == 3  # 100 + 100 + 50
    assert all(r.ok for r in results)


def test_failing_file_isolated_by_retry(tmp_path):
    paths = [_touch(tmp_path, f"f{i}.jpg") for i in range(5)]
    bad = str(paths[2])
    calls = []
    results = move_to_trash(
        paths, runner=make_runner(fail_on={bad}, calls=calls), batch_size=100
    )
    by_path = {str(r.path): r for r in results}
    assert by_path[bad].ok is False
    assert paths[2].exists()  # still on disk
    for p in paths:
        if str(p) != bad:
            assert by_path[str(p)].ok is True
            assert not p.exists()
    # One batch call (fails), then 5 per-file retries.
    assert len(calls) == 1 + 5


def test_runner_exception_reported(tmp_path):
    p = _touch(tmp_path, "f.jpg")

    def runner(*a, **k):
        raise OSError("osascript missing")

    results = move_to_trash([p], runner=runner)
    assert results[0].ok is False
    assert p.exists()
