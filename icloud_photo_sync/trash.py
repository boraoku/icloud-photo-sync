"""Move files to the macOS Trash via Finder.

Uses a batched ``osascript`` call that asks Finder to ``delete`` the files.
Going through Finder (rather than a low-level ``NSFileManager`` trash) means the
files get proper *Put Back* metadata and land in the correct per-volume
``.Trashes`` — important here since the photo tree lives on an external drive.

The first Finder-automation call triggers a one-time macOS consent prompt
("… wants to control Finder"). If the user denies it, Finder returns error
-1743 and this module surfaces a remediation hint.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .logutil import get_logger

logger = get_logger(__name__)

BATCH_SIZE = 100

# Finder AppleScript error for "not authorized to send Apple events".
_NOT_AUTHORIZED = "-1743"
_TCC_HINT = (
    "macOS blocked control of Finder. Grant permission in System Settings → "
    "Privacy & Security → Automation, then re-run."
)


@dataclass
class TrashResult:
    path: Path
    ok: bool
    error: str | None = None


def _escape(path: Path) -> str:
    """Escape a path for an AppleScript double-quoted string literal."""
    s = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'POSIX file "{s}"'


def _script(paths: Sequence[Path]) -> str:
    items = ", ".join(_escape(p) for p in paths)
    return f'tell application "Finder"\n\tdelete {{{items}}}\nend tell\n'


def _run_batch(paths: Sequence[Path], runner: Callable) -> subprocess.CompletedProcess:
    return runner(
        ["osascript", "-"],
        input=_script(paths),
        capture_output=True,
        text=True,
        timeout=120,
    )


def move_to_trash(
    paths: Sequence[Path],
    *,
    runner: Callable = subprocess.run,
    batch_size: int = BATCH_SIZE,
) -> list[TrashResult]:
    """Move ``paths`` to the Trash, returning a per-file result.

    Files are trashed in batches (one ``osascript`` call each) to amortize the
    ~0.5s per-invocation overhead. If a batch fails, it is retried one file at a
    time so failures can be attributed to individual files; ``path.exists()`` is
    the final ground truth regardless of what Finder reports.

    ``runner`` is injectable for testing.
    """
    results: list[TrashResult] = []
    paths = list(paths)
    for start in range(0, len(paths), batch_size):
        batch = paths[start : start + batch_size]
        try:
            proc = _run_batch(batch, runner)
        except (OSError, subprocess.SubprocessError) as exc:
            results.extend(_retry_individually(batch, runner, str(exc)))
            continue

        if proc.returncode == 0:
            results.extend(_verify(batch, None))
        else:
            stderr = (proc.stderr or "").strip()
            if len(batch) == 1:
                results.extend(_verify(batch, stderr))
            else:
                # A whole-batch failure hides which file broke it; isolate.
                results.extend(_retry_individually(batch, runner, stderr))
    return results


def _retry_individually(
    batch: Sequence[Path], runner: Callable, batch_error: str
) -> list[TrashResult]:
    out: list[TrashResult] = []
    for p in batch:
        try:
            proc = _run_batch([p], runner)
            err = None if proc.returncode == 0 else (proc.stderr or "").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            err = str(exc)
        out.extend(_verify([p], err or batch_error))
    return out


def _verify(batch: Sequence[Path], error: str | None) -> list[TrashResult]:
    """Resolve each file's outcome by whether it still exists on disk."""
    out: list[TrashResult] = []
    for p in batch:
        gone = not p.exists()
        if gone:
            out.append(TrashResult(path=p, ok=True))
        else:
            msg = error or "file still present after delete"
            if error and _NOT_AUTHORIZED in error:
                msg = _TCC_HINT
            out.append(TrashResult(path=p, ok=False, error=msg))
    return out
