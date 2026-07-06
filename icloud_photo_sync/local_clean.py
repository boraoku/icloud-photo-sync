"""``local-clean``: find small screenshots/memes/junk images and trash them.

Walks the photo tree for small JPG/PNG files, classifies each with a local
vision model (cached and resumable), then opens a browser review grid where the
user confirms which to move to the Trash. Requires no iCloud login.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import typer
from tqdm import tqdm

from .classifier import LMStudioClassifier, prepare_image
from .clean_cache import CleanCache
from .config import LocalCleanConfig
from .errors import ClassificationError, ClassifierUnavailableError
from .logutil import get_logger
from .review import FlaggedItem, ReviewServer
from .trash import move_to_trash

logger = get_logger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
SECONDS_PER_IMAGE = 15  # rough LM Studio latency, for the ETA hint


@dataclass(frozen=True)
class ImageFile:
    path: Path      # absolute
    rel: str        # posix path relative to output_root (the cache key)
    size: int
    mtime_ns: int


def _parse_size(text: str) -> int:
    """Parse '1MB' / '500KB' / '1048576' into a byte count."""
    s = text.strip().upper().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(B|KB|MB|GB|KIB|MIB|GIB)?", s)
    if not m:
        raise typer.BadParameter(f"invalid size: {text!r} (try 1MB, 500KB, 1048576)")
    value = float(m.group(1))
    unit = m.group(2) or "B"
    factor = {
        "B": 1,
        "KB": 1000, "MB": 1000**2, "GB": 1000**3,
        "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3,
    }[unit]
    return int(value * factor)


def scan_images(root: Path, max_bytes: int) -> list[ImageFile]:
    """Return small JPG/PNG files under ``root``, sorted by relative path.

    Excludes symlinks, ``.part`` in-progress downloads, zero-byte files, and
    anything over ``max_bytes``; prunes hidden directories.
    """
    root = Path(root).resolve()
    found: list[ImageFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            p = Path(dirpath) / name
            try:
                if p.is_symlink():
                    continue
                st = p.stat()
            except OSError:
                continue
            if not (0 < st.st_size <= max_bytes):
                continue
            rel = p.relative_to(root).as_posix()
            found.append(
                ImageFile(path=p, rel=rel, size=st.st_size, mtime_ns=st.st_mtime_ns)
            )
    found.sort(key=lambda f: f.rel)
    return found


def _fmt_eta(n: int) -> str:
    secs = n * SECONDS_PER_IMAGE
    if secs < 90:
        return f"~{secs}s"
    mins = secs / 60
    if mins < 90:
        return f"~{mins:.0f} min"
    return f"~{mins / 60:.1f} h"


def run_local_clean(config: LocalCleanConfig) -> int:
    """Execute the scan → classify → review → trash flow. Returns an exit code."""
    root = config.output_root
    typer.secho(f"Scanning {root} for images ≤ "
                f"{config.max_bytes // 1024} KiB…", fg=typer.colors.BLUE)
    images = scan_images(root, config.max_bytes)
    typer.echo(f"Found {len(images)} candidate image(s).")
    if not images:
        return 0

    cache = CleanCache(config.cache_db)
    with tempfile.TemporaryDirectory(prefix="local-clean-") as tmp:
        work_dir = Path(tmp)
        try:
            classified = _classify_all(images, cache, config, work_dir)
        except KeyboardInterrupt:
            # Every verdict was committed the moment it landed (CleanCache.put),
            # so there is nothing to save here — just report and let the user
            # re-run to resume from the cache.
            cache.close()
            typer.secho(
                "\nStopped. Cached results are preserved; re-run local-clean "
                "to resume.",
                fg=typer.colors.YELLOW,
            )
            return 130

        _print_category_summary(images, classified, config)

        flagged: list[FlaggedItem] = []
        for img in images:
            c = classified.get(img.rel)
            if c is None or c.category not in config.flag_categories:
                continue
            if not img.path.exists():  # trashed/moved since the scan
                continue
            flagged.append(
                FlaggedItem(
                    index=len(flagged),
                    path=img.path,
                    rel=img.rel,
                    category=c.category,
                    confidence=c.confidence,
                    reason=c.reason,
                    size=img.size,
                )
            )

        if not flagged:
            cache.close()
            typer.secho(
                "Nothing flagged — all small images look like real photos.",
                fg=typer.colors.GREEN,
            )
            return 0

        _generate_thumbnails(flagged, work_dir, config)
        return _review_and_trash(flagged, cache, work_dir, config)


def _classify_all(images, cache, config, work_dir) -> dict[str, object]:
    """Return {rel: Classification|None}. Cache hits are free; misses hit the LM."""
    results: dict[str, object] = {}
    todo = []
    for img in images:
        cached = None if config.reclassify else cache.get(img, config.lm_model)
        if cached is not None:
            results[img.rel] = cached
        else:
            todo.append(img)

    if config.limit is not None and len(todo) > config.limit:
        skipped = len(todo) - config.limit
        todo = todo[: config.limit]
        typer.secho(
            f"--limit {config.limit}: classifying {len(todo)} now, "
            f"{skipped} left for a later run.",
            fg=typer.colors.YELLOW,
        )

    if not todo:
        return results

    classifier = LMStudioClassifier(
        base_url=config.lm_base_url,
        model=config.lm_model,
        timeout=config.timeout,
        max_dim=config.thumb_max_dim,
        work_dir=work_dir,
    )
    try:
        classifier.check_available()
    except ClassifierUnavailableError as exc:
        cache.close()
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.echo(
        f"Classifying {len(todo)} image(s) via {config.lm_model} "
        f"(est. {_fmt_eta(len(todo))}). Ctrl-C is safe — progress is cached."
    )
    errors = 0
    for img in tqdm(todo, desc="Classifying", unit="img"):
        try:
            c = classifier.classify(img.path)
            cache.put(img, config.lm_model, c)
            results[img.rel] = c
        except ClassificationError as exc:
            errors += 1
            results[img.rel] = None
            logger.warning("skipping %s: %s", img.rel, exc)
    if errors:
        typer.secho(f"{errors} image(s) could not be classified (skipped).",
                    fg=typer.colors.YELLOW)
    return results


def _print_category_summary(images, classified, config) -> None:
    counts: dict[str, int] = {}
    for img in images:
        c = classified.get(img.rel)
        if c is not None:
            counts[c.category] = counts.get(c.category, 0) + 1
    parts = [f"{cat}={counts[cat]}" for cat in sorted(counts)]
    if parts:
        typer.echo("Categories: " + ", ".join(parts))
    flagged_cats = ", ".join(config.flag_categories)
    typer.echo(f"Flagging for deletion: {flagged_cats}")


def _generate_thumbnails(flagged, work_dir, config) -> None:
    for it in tqdm(flagged, desc="Thumbnails", unit="img"):
        try:
            data, _ = prepare_image(it.path, config.thumb_max_dim, work_dir)
            (work_dir / f"{it.index}.jpg").write_bytes(data)
        except OSError as exc:
            logger.debug("thumbnail failed for %s: %s", it.rel, exc)


def _review_and_trash(flagged, cache, work_dir, config) -> int:
    token = secrets.token_urlsafe(16)
    server = ReviewServer(
        items=flagged,
        thumbs_dir=work_dir,
        trash_fn=move_to_trash,
        token=token,
        port=config.port,
    )
    url = server.url
    typer.secho(f"\nReview {len(flagged)} flagged image(s): {url}",
                fg=typer.colors.BLUE)
    if config.open_browser:
        webbrowser.open(url)
    else:
        typer.echo("Open that URL in your browser to review.")
    typer.echo("Waiting for your selection… (Ctrl-C to cancel without deleting)")

    outcome = server.serve()
    if outcome is None:
        cache.close()
        typer.secho("Cancelled — nothing was moved. Classifications are cached.",
                    fg=typer.colors.YELLOW)
        return 0

    cache.remove(outcome.moved)
    cache.close()
    typer.secho(f"Moved {len(outcome.moved)} file(s) to the Trash.",
                fg=typer.colors.GREEN)
    if outcome.failed:
        typer.secho(f"{len(outcome.failed)} could not be moved:",
                    fg=typer.colors.RED, err=True)
        for rel, err in outcome.failed:
            typer.secho(f"  {rel}: {err}", fg=typer.colors.RED, err=True)
        return 1
    return 0
