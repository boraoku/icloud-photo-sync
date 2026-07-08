"""``local-clean``: find small screenshots/memes/junk images and trash them.

Walks the photo tree for small JPG/PNG files and classifies each with a local
vision model (cached and resumable). A browser review grid opens right away and
newly flagged images stream into it as classification proceeds, so review can
start within seconds rather than after the whole library is processed. The user
trashes any number of rounds and clicks Finish (or Ctrl-C) to end. No iCloud
login required.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
import time
import webbrowser
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import typer
from tqdm import tqdm

from .classifier import Classification, LMStudioClassifier, prepare_image
from .clean_cache import CleanCache
from .config import LocalCleanConfig
from .errors import ClassificationError, ClassifierUnavailableError
from .logutil import get_logger
from .review import FlaggedItem, ReviewServer
from .trash import move_to_trash

logger = get_logger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
SECONDS_PER_IMAGE = 15  # rough LM Studio latency, for the ETA hint
CONSECUTIVE_ERROR_LIMIT = 5  # abort a run if the model dies mid-stream
ZERO_FLAGGED_GRACE = 3.0  # let the open tab's final poll render "Nothing flagged"


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


def iter_media_files(
    root: Path, suffixes: set[str]
) -> Iterator[tuple[Path, str, os.stat_result]]:
    """Yield ``(path, rel, stat)`` for regular files under ``root`` matching ``suffixes``.

    Prunes hidden directories, skips dot-files (e.g. AppleDouble sidecars like
    ``._IMG_0042.JPG``), symlinks, and zero-byte files. ``.part`` in-progress
    downloads are excluded by the suffix filter. ``suffixes`` are matched
    case-insensitively; ``rel`` is the posix path relative to ``root``.
    """
    root = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):  # AppleDouble sidecars, .DS_Store, etc.
                continue
            if Path(name).suffix.lower() not in suffixes:
                continue
            p = Path(dirpath) / name
            try:
                if p.is_symlink():
                    continue
                st = p.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue
            yield p, p.relative_to(root).as_posix(), st


def scan_images(root: Path, max_bytes: int) -> list[ImageFile]:
    """Return small JPG/PNG files under ``root``, sorted by relative path.

    Excludes symlinks, ``.part`` in-progress downloads, zero-byte files, and
    anything over ``max_bytes``; prunes hidden directories and skips dot-files
    (e.g. AppleDouble sidecars like ``._IMG_0042.JPG``).
    """
    found: list[ImageFile] = []
    for p, rel, st in iter_media_files(root, IMAGE_SUFFIXES):
        if st.st_size > max_bytes:
            continue
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
    """Execute the scan → stream-classify → review → trash flow. Returns exit code."""
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
        cached, process = _split_cached(images, cache, config)
        n_misses = sum(1 for img in process if img.rel not in cached)

        # Fail fast (exit 1, no browser) if the model is down but work remains —
        # done BEFORE the server starts, matching the previous behavior.
        classifier = _make_classifier(config, work_dir, cache) if n_misses else None

        server = ReviewServer(
            thumbs_dir=work_dir, trash_fn=move_to_trash,
            token=secrets.token_urlsafe(16), port=config.port,
        )
        server.start()
        url = server.url
        typer.secho(f"\nReview page: {url}", fg=typer.colors.BLUE)
        if config.open_browser:
            webbrowser.open(url)
        else:
            typer.echo("Open that URL in your browser; results stream in as they classify.")
        if n_misses:
            typer.echo(
                f"Classifying {n_misses} new image(s) via {config.lm_model} "
                f"(est. {_fmt_eta(n_misses)}). Ctrl-C is safe — progress is cached."
            )

        interrupted = False
        results: dict[str, Classification | None] | None = None
        try:
            results = _classify_and_publish(
                process, cached, classifier, cache, server, config, work_dir
            )
        except KeyboardInterrupt:
            interrupted = True
        server.mark_done()
        if results is not None:
            _print_category_summary(results, config)
        return _finish_session(server, cache, interrupted)


def _split_cached(
    images: list[ImageFile], cache: CleanCache, config: LocalCleanConfig
) -> tuple[dict[str, Classification], list[ImageFile]]:
    """Split into (cached verdicts, ordered process list).

    ``process`` is in scan order and holds every cache hit plus up to ``--limit``
    misses; the excess misses are deferred to a later run.
    """
    cached: dict[str, Classification] = {}
    misses: list[ImageFile] = []
    for img in images:
        c = None if config.reclassify else cache.get(img, config.lm_model)
        if c is not None:
            cached[img.rel] = c
        else:
            misses.append(img)

    excluded: set[str] = set()
    if config.limit is not None and len(misses) > config.limit:
        excluded = {m.rel for m in misses[config.limit:]}
        typer.secho(
            f"--limit {config.limit}: classifying {config.limit} new image(s) now, "
            f"{len(excluded)} left for a later run.",
            fg=typer.colors.YELLOW,
        )
    process = [img for img in images if img.rel not in excluded]
    return cached, process


def _make_classifier(
    config: LocalCleanConfig, work_dir: Path, cache: CleanCache
) -> LMStudioClassifier:
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
    return classifier


def _classify_and_publish(
    process, cached, classifier, cache, server, config, work_dir
) -> dict[str, Classification | None]:
    """The unified loop: verdict from cache or LM, publish flagged items live.

    Sole producer of flagged indices (monotonic). Returns {rel: Classification|None}.
    """
    results: dict[str, Classification | None] = {}
    total = len(process)
    next_index = 0
    errors = 0
    consecutive = 0
    for i, img in enumerate(tqdm(process, desc="Classifying", unit="img")):
        if server.finish_requested:
            break
        c = cached.get(img.rel)
        if c is None:
            try:
                c = classifier.classify(img.path)
                cache.put(img, config.lm_model, c)
                consecutive = 0
            except ClassificationError as exc:
                errors += 1
                consecutive += 1
                results[img.rel] = None
                logger.warning("skipping %s: %s", img.rel, exc)
                server.set_progress(i + 1, total)
                if consecutive >= CONSECUTIVE_ERROR_LIMIT:
                    tqdm.write(
                        f"Aborting after {consecutive} consecutive classification "
                        "errors — is the model still loaded? Progress is cached."
                    )
                    break
                continue
        results[img.rel] = c
        server.set_progress(i + 1, total)
        if c.category in config.flag_categories and img.path.exists():
            if _publish_flagged(img, c, next_index, server, work_dir, config):
                next_index += 1
    if errors:
        tqdm.write(f"{errors} image(s) could not be classified (skipped).")
    return results


def _publish_flagged(img, c, index, server, work_dir, config) -> bool:
    """Write the thumbnail (BEFORE publishing) and publish. False on thumb failure."""
    try:
        data, _ = prepare_image(img.path, config.thumb_max_dim, work_dir)
        (work_dir / f"{index}.jpg").write_bytes(data)
    except OSError as exc:
        logger.debug("thumbnail failed for %s: %s", img.rel, exc)
        return False
    server.publish(
        FlaggedItem(
            index=index, path=img.path, rel=img.rel,
            category=c.category, confidence=c.confidence,
            reason=c.reason, size=img.size,
        )
    )
    return True


def _print_category_summary(results, config) -> None:
    counts: dict[str, int] = {}
    for c in results.values():
        if c is not None:
            counts[c.category] = counts.get(c.category, 0) + 1
    parts = [f"{cat}={counts[cat]}" for cat in sorted(counts)]
    if parts:
        typer.echo("Categories: " + ", ".join(parts))
    typer.echo(f"Flagged for deletion: {', '.join(config.flag_categories)}")


def _finish_session(server: ReviewServer, cache: CleanCache, interrupted: bool) -> int:
    """Wait for the user to finish (or Ctrl-C), then report and clean up."""
    # Zero flagged and classification completed: nothing to review.
    if not interrupted and server.item_count == 0:
        try:
            time.sleep(ZERO_FLAGGED_GRACE)  # let the tab render "Nothing flagged"
        except KeyboardInterrupt:
            pass
        server.close()
        cache.close()
        typer.secho("Nothing flagged — all small images look like real photos.",
                    fg=typer.colors.GREEN)
        return 0

    if not interrupted:
        typer.echo(
            "Classification finished. Review in the browser and click Finish "
            "there — or press Ctrl-C here to end. Files already moved to Trash "
            "stay in the Trash."
        )
        try:
            server.wait_finished()
        except KeyboardInterrupt:
            interrupted = True

    server.close()
    outcome = server.outcome
    cache.remove(outcome.moved)
    cache.close()

    typer.secho(f"Moved {len(outcome.moved)} file(s) to the Trash.",
                fg=typer.colors.GREEN)
    if outcome.failed:
        typer.secho(f"{len(outcome.failed)} could not be moved:",
                    fg=typer.colors.RED, err=True)
        for rel, err in outcome.failed:
            typer.secho(f"  {rel}: {err}", fg=typer.colors.RED, err=True)
    if interrupted and not outcome.moved and not outcome.failed:
        typer.secho("Progress is cached; re-run local-clean to resume.",
                    fg=typer.colors.YELLOW)
    if outcome.failed:
        return 1
    return 130 if interrupted else 0
