"""Showing a retrospective deletion plan before it happens, using iCloud's thumbnails.

The local files are gone, so there is nothing on disk to render. iCloud still
holds a JPEG thumbnail for every asset, on the same CPLMaster record
:meth:`~icloud_photo_sync.icloud_client.ICloudClient.lookup_assets` already
fetches — so the candidates can be shown without a second kind of request.

This is the step that restores *intent*. Every other signal in
:mod:`icloud_photo_sync.retro_clean` infers what the user must have meant from
evidence left lying around; this one asks. Whatever comes back is the whole
selection: :func:`review_candidates` returns only what was ticked, and the plan
is rebuilt from it.

The review server is the ordinary :class:`~icloud_photo_sync.review.ReviewServer`
with a **no-op trash function** injected. Nothing here can touch the filesystem:
the paths handed to it do not exist, and the injected function never calls
:mod:`icloud_photo_sync.trash` at all.
"""

from __future__ import annotations

import secrets
import tempfile
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Sequence

import typer

from . import icloud_delete as idel
from .logutil import get_logger
from .review import FlaggedItem, ReviewServer
from .trash import TrashResult

logger = get_logger(__name__)

THUMB_CHUNK = 100
THUMB_WORKERS = 8      # latency-bound CDN GETs, not CPU work


def _noop_trash(paths: Sequence[Path]) -> list[TrashResult]:
    """Accept every "trash" without touching anything.

    A retrospective review selects; it does not move files, because there are no
    files left to move. Reporting ok is what routes the selection into
    ``outcome.icloud``, which is the only thing this page produces.
    """
    return [TrashResult(path=p, ok=True) for p in paths]


def thumbnail_urls(
    client, candidates: Sequence[idel.Candidate],
) -> dict[int, str]:
    """Resolve every thumbnail URL first, in one tight run of lookups.

    Deliberately separated from downloading. The lookups need iCloud's
    authenticated session; the CDN fetches that follow do not. Interleaving them
    used to stretch a handful of authenticated calls across a quarter of an hour
    of slow transfers, which is exactly the shape that trips Apple's PCS
    consent (see ``ICloudClient._photos_raw``). Done this way they finish in
    seconds.

    A chunk that fails is logged and skipped: its candidates simply show without
    a picture. Losing the whole review — and with it the scan it took minutes to
    build — over a decorative fetch would be the worse trade.
    """
    urls: dict[int, str] = {}
    for start in range(0, len(candidates), THUMB_CHUNK):
        batch = candidates[start:start + THUMB_CHUNK]
        try:
            found, _missing = client.lookup_assets([c.asset_id for c in batch])
        except Exception as exc:  # noqa: BLE001 - cosmetic step, never fatal
            logger.warning("could not look up thumbnails for %d asset(s): %s",
                           len(batch), exc)
            continue
        for offset, candidate in enumerate(batch):
            remote = found.get(candidate.asset_id)
            url = getattr(remote, "thumb_url", None) if remote else None
            if url:
                urls[start + offset] = url
    return urls


def fetch_thumbnails(
    client,
    candidates: Sequence[idel.Candidate],
    thumbs_dir: Path,
    *,
    progress: Callable | None = None,
    workers: int = THUMB_WORKERS,
) -> dict[str, Path]:
    """Write ``<index>.jpg`` for every candidate we can get a thumbnail for.

    A missing thumbnail is not an error: the card renders without an image and
    the user still sees the filename, size and date. Refusing to review because
    one CDN fetch failed would be a worse outcome than a blank tile.

    Downloads run on a small pool. They are latency-bound signed-URL GETs, so
    serialising them was costing about a second each — and the longer the whole
    step takes, the more chances there are for the session underneath it to be
    withdrawn.
    """
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    urls = thumbnail_urls(client, candidates)
    written: dict[str, Path] = {}
    bar = progress(total=len(candidates), desc="Fetching iCloud thumbnails",
                   unit="thumb") if progress else None

    def fetch(item: tuple[int, str]) -> tuple[int, bytes | None]:
        index, url = item
        try:
            return index, client.thumbnail_bytes(url)
        except Exception as exc:  # noqa: BLE001 - one tile, never the run
            logger.debug("thumbnail %d failed: %s", index, exc)
            return index, None

    try:
        if bar:
            bar.update(len(candidates) - len(urls))     # the ones with no URL
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for index, data in pool.map(fetch, urls.items()):
                if data:
                    path = thumbs_dir / f"{index}.jpg"
                    path.write_bytes(data)
                    written[candidates[index].rel] = path
                if bar:
                    bar.update(1)
    finally:
        if bar:
            bar.close()
    return written


def review_candidates(
    client,
    candidates: Sequence[idel.Candidate],
    *,
    echo: Callable[..., None] = typer.secho,
    progress: Callable | None = None,
    open_browser: bool = True,
) -> frozenset[str] | None:
    """Show the candidates and return the rels the user actually ticked.

    Returns ``None`` only if there was nothing to review, which leaves the
    caller's plan untouched.
    """
    if not candidates:
        return None

    with tempfile.TemporaryDirectory(prefix="icloud-retro-review-") as tmp:
        thumbs_dir = Path(tmp)
        written = fetch_thumbnails(client, candidates, thumbs_dir, progress=progress)
        missing = len(candidates) - len(written)

        server = ReviewServer(
            thumbs_dir=thumbs_dir,
            trash_fn=_noop_trash,
            token=secrets.token_urlsafe(24),
            icloud_armed=True,
            retro=True,
        )
        try:
            for index, candidate in enumerate(candidates):
                server.publish(FlaggedItem(
                    index=index,
                    # This path does not exist — that is the point. The injected
                    # trash function never opens it, and nothing else does.
                    path=Path(candidate.rel),
                    rel=candidate.rel,
                    category="missing",
                    confidence=1.0,
                    reason=" · ".join(candidate.corroboration)
                           or "tracked by the manifest, absent from disk",
                    size=candidate.expected_size or 0,
                ))
            server.set_progress(len(candidates), len(candidates))
            server.mark_done()
            server.start()

            echo(f"\nReview page: {server.url}", fg=typer.colors.BLUE)
            echo("These files are already gone from your disk. What you see are "
                 "iCloud's own thumbnails.")
            echo("Select the ones you meant to delete; anything you leave "
                 "unselected stays in iCloud.")
            if missing:
                # Say it here rather than let blank tiles read as "iCloud has
                # nothing for this" — the file may be perfectly fine.
                echo(f"{missing} of {len(candidates)} thumbnails could not be "
                     "fetched and show as blank tiles; that says nothing about "
                     "the asset itself.", fg=typer.colors.YELLOW)
            echo("Click Finish in the page when you are done — or press Ctrl-C "
                 "here, which keeps whatever you have already selected.")
            if open_browser:
                webbrowser.open(server.url)
            try:
                server.wait_finished()
            except KeyboardInterrupt:
                # The selection is not lost work to be tidied away: the user made
                # it deliberately and the server recorded it. Throwing it out on
                # Ctrl-C silently undid a review that had already happened. The
                # typed confirmation in the terminal is still the gate.
                picked = frozenset(server.outcome.icloud)
                echo(f"\nReview interrupted — keeping the {len(picked)} file(s) "
                     "you had already selected." if picked else
                     "\nReview interrupted — nothing had been selected.",
                     fg=typer.colors.YELLOW)
                return picked
            return frozenset(server.outcome.icloud)
        finally:
            server.close()
