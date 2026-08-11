"""``video-clean``: list downloaded videos largest-first and trash the big ones.

Videos are the heaviest thing in a synced photo tree, so freeing disk space
mostly means finding and deleting a handful of large clips. This command walks
the tree for video files, opens a browser page listing them from largest to
smallest, and lets the user preview any clip and move whatever they choose to
the macOS Trash. No iCloud login and no local model — the scan is a plain
``stat`` walk and every deletion is the user's explicit choice (nothing is
pre-selected). The only state kept between runs is the grid's poster-frame
cache (:mod:`icloud_photo_sync.poster`).
"""

from __future__ import annotations

import secrets
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import typer
from tqdm import tqdm

from . import clean_icloud
from .config import VIDEO_SUFFIXES, VideoCleanConfig
from .local_clean import iter_media_files
from .logutil import get_logger
from .poster import PosterCache, probe_durations
from .trash import move_to_trash
from .video_review import VideoItem, VideoReviewServer

logger = get_logger(__name__)



@dataclass(frozen=True)
class VideoFile:
    path: Path      # absolute
    rel: str        # posix path relative to output_root
    size: int
    mtime_ns: int


def _human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def scan_videos(root: Path, min_bytes: int = 0) -> list[VideoFile]:
    """Return video files under ``root``, largest first.

    Shares the walk rules of :func:`~icloud_photo_sync.local_clean.scan_images`
    (prunes hidden directories, skips dot-files/symlinks/zero-byte files;
    ``.part`` downloads are excluded by the suffix filter). Files smaller than
    ``min_bytes`` are dropped. Sorted by descending size with the relative path
    as a tiebreak, so the order is deterministic.
    """
    found: list[VideoFile] = []
    for p, rel, st in iter_media_files(root, VIDEO_SUFFIXES):
        if st.st_size < min_bytes:
            continue
        found.append(
            VideoFile(path=p, rel=rel, size=st.st_size, mtime_ns=st.st_mtime_ns)
        )
    found.sort(key=lambda f: (-f.size, f.rel))
    return found


def run_video_clean(config: VideoCleanConfig, icloud=None) -> int:
    """Execute the scan → review → trash flow. Returns an exit code.

    ``icloud`` is an optional :class:`~icloud_photo_sync.config.ICloudDeleteConfig`.
    When given, the iCloud session is checked before the scan; when None, nothing
    here touches an Apple ID at all.
    """
    armed = clean_icloud.arm(icloud) if icloud is not None else None
    root = config.output_root
    typer.secho(f"Scanning {root} for videos…", fg=typer.colors.BLUE)
    videos = scan_videos(root, config.min_bytes)
    total_bytes = sum(v.size for v in videos)
    typer.echo(f"Found {len(videos)} video(s), {_human_size(total_bytes)} total.")
    if not videos:
        typer.secho("No videos found.", fg=typer.colors.GREEN)
        return 0

    # Spotlight answers for the whole library in one call, so the grid can show
    # lengths without the scan stopping being effectively instant.
    durations = probe_durations(v.path for v in videos)
    items = [
        VideoItem(index=i, path=v.path, rel=v.rel, size=v.size,
                  mtime_ns=v.mtime_ns, duration=durations.get(v.path))
        for i, v in enumerate(videos)
    ]
    size_by_rel = {v.rel: v.size for v in videos}

    server = VideoReviewServer(
        items=items, trash_fn=move_to_trash,
        token=secrets.token_urlsafe(16), port=config.port,
        posters=PosterCache(config.poster_cache_dir),
        icloud_armed=armed is not None,
    )
    server.start()
    url = server.url
    typer.secho(f"\nReview page: {url}", fg=typer.colors.BLUE)
    if config.open_browser:
        webbrowser.open(url)
    else:
        typer.echo("Open that URL in your browser to review and trash videos.")
    typer.echo(
        "Pick the videos you want gone and click Move to Trash — or press Ctrl-C "
        "here to end. Files already moved to Trash stay in the Trash."
    )

    interrupted = False
    try:
        server.wait_finished()
    except KeyboardInterrupt:
        interrupted = True

    server.close()
    outcome = server.outcome
    freed = sum(size_by_rel.get(rel, 0) for rel in outcome.moved)

    typer.secho(
        f"Moved {len(outcome.moved)} file(s) to the Trash "
        f"({_human_size(freed)} freed).",
        fg=typer.colors.GREEN,
    )
    if armed is None and outcome.moved:
        # Still in iCloud, so the next sync brings them all back.
        typer.secho("They are still in iCloud, so the next sync will download "
                    "them again. To delete them there too:\n"
                    "  icloud-photo-sync icloud-delete --scan-trashed --dry-run",
                    fg=typer.colors.YELLOW)
    icloud_code = clean_icloud.finish_and_report(armed, outcome,
                                                source="video-clean", progress=tqdm)
    if outcome.failed:
        typer.secho(f"{len(outcome.failed)} could not be moved:",
                    fg=typer.colors.RED, err=True)
        for rel, err in outcome.failed:
            typer.secho(f"  {rel}: {err}", fg=typer.colors.RED, err=True)
        return 1
    return icloud_code or (130 if interrupted else 0)
