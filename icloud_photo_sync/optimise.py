"""``video-optimise``: re-encode the big videos, then put them back into iCloud.

The command has three long phases — convert, swap, clean up — and every one of
them is interruptible with Ctrl-C and resumable by re-running the same command.
What makes that true is :mod:`icloud_photo_sync.optimise_job`: one row per
video, carried through every phase, so the work already done is a fact on disk
rather than something held in memory.

Two orderings in here are load-bearing.

**Upload before delete, per video.** The brief this was built from said the
other way round. If an upload fails after the delete, the only surviving copy of
that clip is in Recently Deleted on a thirty-day fuse. If a delete fails after
the upload, the worst case is a duplicate in Photos. There is no version of this
where delete-first is safer, so :func:`_swap_one` uploads, reads the new asset
back, and only then touches the original — and
:class:`~icloud_photo_sync.video_optimise.Swap` refuses to be constructed
without a verified new asset id, so the ordering cannot be got wrong by editing
this file carelessly.

**Verify before believing.** An upload returning 200 is not evidence that the
replacement exists, and a modify returning success is not evidence that the
original is gone. Both are read back. This is the same rule
:mod:`icloud_photo_sync.icloud_delete` already lives by, and it is why a swap
costs four round trips instead of two.

The local originals are never unlinked, only moved to the Trash, and only after
their row is ``swapped``.
"""

from __future__ import annotations

import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Callable, Sequence

import typer

from . import icloud_client as ic
from . import optimise_job as oj
from . import optimise_review as orv
from . import transcode as tc
from . import video_optimise as vo
from .auth import SessionManager
from .clean_icloud import RECOVERY_DAYS, ArmedICloud, SURE_PHRASE, _normalise, arm
from .config import (
    OPTIMISED_DIRNAME,
    VIDEO_SUFFIXES,
    ICloudDeleteConfig,
    VideoOptimiseConfig,
)
from .errors import ICloudSyncError
from .local_clean import iter_media_files
from .logutil import get_logger
from .models import AssetRef
from .poster import PosterCache, probe_durations
from .state import StateStore
from .trash import move_to_trash
from .video_optimise import human_size as _size

logger = get_logger(__name__)

ARM_NOTE_OPTIMISE = (
    "Nothing is deleted until an optimised copy has been found in iCloud and "
    "read back — and then only after you type a confirmation here."
)

_EXCLUDE = frozenset({OPTIMISED_DIRNAME})
"""The hand-off folder is library-adjacent, not library content: without this
the scan would offer this command's own output back to it for re-conversion."""

PROBE_WORKERS = 8
"""ffprobe processes in flight while scanning."""

COMPARE_TOP_N = 10
"""How many pairs the summary screen shows before offering the full list."""

@dataclass
class Totals:
    converted: int = 0
    freed_local: int = 0
    swapped: int = 0
    freed_cloud: int = 0
    failed: int = 0


# --- scanning ----------------------------------------------------------------


def scan_videos(root: Path) -> list[tuple[Path, str, int]]:
    """Every video under ``root``, largest first. Shares ``local-clean``'s walk."""
    found = [(p, rel, st.st_size)
             for p, rel, st in iter_media_files(root, VIDEO_SUFFIXES, _EXCLUDE)]
    found.sort(key=lambda t: (-t[2], t[1]))
    return found


def image_stems(root: Path) -> frozenset[str]:
    """``dir/stem`` for every still in the tree — the Live Photo guard's input."""
    from .config import IMAGE_SUFFIXES
    extra = IMAGE_SUFFIXES | {".heic", ".heif"}
    return frozenset(vo.stem_key(rel)
                     for _, rel, _ in iter_media_files(root, extra, _EXCLUDE))


def probe_all(
    videos: Sequence[tuple[Path, str, int]],
    *,
    probe_fn: Callable[[Path, str], vo.VideoProbe | None] = tc.probe,
    progress=None,
    cancel: Event | None = None,
    workers: int = PROBE_WORKERS,
) -> list[vo.VideoProbe | None]:
    """ffprobe every video, reporting progress. Results stay in input order.

    Each probe is two short subprocess round trips, so the cost is dominated by
    process spawn and metadata reads rather than CPU — a small pool turns what
    would be ten minutes on a library this size into under one, and this is the
    first thing the command does, before the user has been shown anything.
    ``workers`` is deliberately modest: the point is to hide latency, not to
    saturate the disk the videos are about to be read from.
    """
    bar = progress(total=len(videos), desc="Probing videos", unit="file") if progress else None
    out: list[vo.VideoProbe | None] = [None] * len(videos)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(probe_fn, path, rel): i
                       for i, (path, rel, _) in enumerate(videos)}
            for future in as_completed(futures):
                if cancel is not None and cancel.is_set():
                    # Drop whatever has not started; the in-flight few finish.
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    out[futures[future]] = future.result()
                except Exception as exc:      # one bad file must not end the scan
                    logger.debug("probe failed for %s: %s",
                                 videos[futures[future]][1], exc)
                if bar:
                    bar.update(1)
    finally:
        if bar:
            bar.close()
    return out


# --- reporting ---------------------------------------------------------------


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "—"


def library_summary(root: Path) -> tuple[int, int]:
    """``(total_files, total_bytes)`` under ``root``, hidden dirs pruned."""
    total_files = total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            try:
                total_bytes += (Path(dirpath) / name).stat().st_size
                total_files += 1
            except OSError:
                continue
    return total_files, total_bytes


def _report_library(root, videos, plan, echo) -> None:
    video_bytes = sum(size for _, _, size in videos)
    files, total = library_summary(root)
    echo("")
    echo(f"  Videos     {len(videos):>6,} files   {_size(video_bytes):>10}   "
         f"{_percent(video_bytes, total)} of your library", fg=typer.colors.WHITE)
    echo(f"  Everything {files:>6,} files   {_size(total):>10}")

    hdr = sum(1 for c in plan.candidates if c.probe.is_hdr)
    slow = sum(1 for c in plan.candidates if c.probe.is_slow_motion)
    echo("")
    if not plan.candidates:
        echo("Nothing here is worth re-encoding.", fg=typer.colors.GREEN)
    else:
        echo(f"Worth optimising: {len(plan.candidates):,} videos   "
             f"{_size(plan.source_bytes)} → about {_size(plan.predicted_bytes)}   "
             f"frees {_size(plan.predicted_saving)}", fg=typer.colors.WHITE)
        detail = [f"{hdr} HDR"] if hdr else []
        if slow:
            detail.append(f"{slow} slow motion (frame rate kept)")
        detail.append(f"{len(plan.skipped)} skipped")
        echo("  " + " · ".join(detail))
        echo(f"  about {plan.duration / 3600:.1f} h of footage")

    by_reason: dict[str, int] = {}
    for skip in plan.skipped:
        by_reason[skip.reason] = by_reason.get(skip.reason, 0) + 1
    for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        echo(f"    {count:>5}  {reason}")


def _select_items(plan: vo.OptimisePlan, videos, durations) -> list[orv.SelectItem]:
    """Every scanned video as a grid row — skipped ones greyed out, not hidden."""
    by_rel = {rel: (path, size) for path, rel, size in videos}
    mtimes: dict[str, int] = {}
    for path, rel, _ in videos:
        try:
            mtimes[rel] = path.stat().st_mtime_ns
        except OSError:
            mtimes[rel] = 0

    items: list[orv.SelectItem] = []
    index = 0
    for candidate in plan.candidates:
        p = candidate.probe
        path, size = by_rel.get(p.rel, (Path(p.rel), p.size))
        items.append(orv.SelectItem(
            index=index, path=path, rel=p.rel, size=size, mtime_ns=mtimes.get(p.rel, 0),
            duration=durations.get(path, p.duration),
            predicted_size=candidate.predicted_size,
            out_width=candidate.encode.width, out_height=candidate.encode.height,
            src_width=p.width, src_height=p.height, fps=p.fps,
            hdr=p.is_hdr, slow_motion=p.is_slow_motion,
            keeps_frame_rate=candidate.encode.fps is None and p.is_slow_motion,
        ))
        index += 1
    for skip in plan.skipped:
        path, size = by_rel.get(skip.rel, (Path(skip.rel), 0))
        items.append(orv.SelectItem(
            index=index, path=path, rel=skip.rel, size=size,
            mtime_ns=mtimes.get(skip.rel, 0), duration=durations.get(path),
            skip_reason=skip.reason,
        ))
        index += 1
    return items


# --- conversion --------------------------------------------------------------


def _convert_all(
    job: oj.OptimiseJob,
    config: VideoOptimiseConfig,
    *,
    echo,
    progress=None,
    cancel: Event | None = None,
    convert_fn=tc.convert,
    probe_fn=tc.probe,
) -> Totals:
    """Encode every ``selected`` row, verifying colour and size on the output.

    A row only becomes ``converted`` when the file that was actually produced is
    both smaller and still the colour it started as. Both are measurements of
    the real output rather than predictions, so neither can be wrong — which is
    the whole reason this gate exists here as well as before the encode.
    """
    rows = job.pending_conversion()
    totals = Totals()
    if not rows:
        return totals

    echo(f"\nConverting {len(rows)} video(s) → short side {config.short_side}, HEVC",
         fg=typer.colors.BLUE)
    echo(f"Work directory: {config.work_dir}   (originals untouched)")

    taken = {p.name for p in config.work_dir.glob("*.mov")}
    taken |= {r["out_rel"] for r in job.by_status(
        oj.STATUS_CONVERTED, oj.STATUS_REJECTED, oj.STATUS_UPLOADED,
        oj.STATUS_SWAPPED) if r["out_rel"]}

    bar = progress(total=len(rows), desc="Converting", unit="video") if progress else None
    try:
        for row in rows:
            if cancel is not None and cancel.is_set():
                echo("\nStopped. Re-run the same command to carry on where this "
                     "left off.", fg=typer.colors.YELLOW)
                break
            rel = row["rel"]
            src = config.output_root / rel
            probe_data = oj.probe_of(row)
            plan_data = oj.plan_of(row)
            if probe_data is None or plan_data is None:
                job.mark_skipped(rel, oj.STATUS_CONVERT_FAILED, "no recorded plan")
                totals.failed += 1
                continue
            source = vo.VideoProbe(**probe_data)
            encode = vo.Encode(**plan_data)
            # Named at the moment of conversion, against what is already there:
            # the flat folder collides where the dated tree did not (17 real
            # collisions among 647 candidates on the library this was built for).
            name = vo.flat_name(rel, taken=taken)
            taken.add(name)
            dest = config.work_path(name)

            result = convert_fn(src, dest, encode, has_audio=source.has_audio,
                                duration=source.duration, cancel=cancel)
            if result.cancelled:
                echo("\nStopped mid-file; that video will start again next time.",
                     fg=typer.colors.YELLOW)
                break
            if not result.ok:
                job.mark_skipped(rel, oj.STATUS_CONVERT_FAILED, result.error)
                totals.failed += 1
                echo(f"  ✗ {rel}: {result.error.splitlines()[0][:120]}"
                     if result.error else f"  ✗ {rel}", fg=typer.colors.RED)
                if bar:
                    bar.update(1)
                continue

            output = probe_fn(dest, rel)
            if output is None:
                dest.unlink(missing_ok=True)
                job.mark_skipped(rel, oj.STATUS_CONVERT_FAILED,
                                 "the converted file could not be read back")
                totals.failed += 1
                if bar:
                    bar.update(1)
                continue

            verdict = vo.accept_output(source, output)
            if verdict is not None:
                # The original is kept, and the reason is recorded rather than
                # summarised: "it lost its colour" is a different problem from
                # "it barely shrank" and the user may want to act on it.
                dest.unlink(missing_ok=True)
                status = (oj.STATUS_COLOUR_MISMATCH
                          if verdict.reason == vo.SKIP_COLOUR_MISMATCH
                          else oj.STATUS_NOT_WORTH_IT)
                job.mark_skipped(rel, status, verdict.detail)
                colour = typer.colors.RED if status == oj.STATUS_COLOUR_MISMATCH \
                    else typer.colors.YELLOW
                echo(f"  ⊘ {rel}: {verdict.reason} — kept the original "
                     f"({verdict.detail})", fg=colour)
                if bar:
                    bar.update(1)
                continue

            job.mark_converted(rel, out_rel=name, out_bytes=result.size,
                               out_probe=_probe_dict(output))
            totals.converted += 1
            totals.freed_local += max(0, source.size - result.size)
            echo(f"  ✓ {rel}  {_size(source.size)} → {_size(result.size)}  "
                 f"({100 * (1 - result.size / source.size):.0f}% smaller, "
                 f"{_colour_arrow(source, output)})", fg=typer.colors.GREEN)
            if bar:
                bar.update(1)
    finally:
        if bar:
            bar.close()
    return totals


def _colour_arrow(source: vo.VideoProbe, output: vo.VideoProbe) -> str:
    src, out = vo.colour_text(source), vo.colour_text(output)
    return f"{src} → {out}" if src != out else f"{src} kept"


def _probe_dict(probe: vo.VideoProbe) -> dict:
    return {
        "rel": probe.rel, "size": probe.size, "width": probe.width,
        "height": probe.height, "fps": probe.fps, "duration": probe.duration,
        "codec": probe.codec, "pix_fmt": probe.pix_fmt, "transfer": probe.transfer,
        "primaries": probe.primaries, "colorspace": probe.colorspace,
        "has_audio": probe.has_audio,
    }


def _encode_dict(encode: vo.Encode) -> dict:
    return {
        "width": encode.width, "height": encode.height, "fps": encode.fps,
        "bitrate": encode.bitrate, "profile": encode.profile,
        "pix_fmt": encode.pix_fmt, "transfer": encode.transfer,
        "primaries": encode.primaries, "colorspace": encode.colorspace,
    }


# --- comparison --------------------------------------------------------------


def _pairs(job: oj.OptimiseJob, config: VideoOptimiseConfig,
           rows: Sequence) -> list[orv.ComparePair]:
    pairs: list[orv.ComparePair] = []
    for index, row in enumerate(rows):
        src_data, out_data = oj.probe_of(row), oj.out_probe_of(row)
        if src_data is None or out_data is None:
            continue
        source, output = vo.VideoProbe(**src_data), vo.VideoProbe(**out_data)
        pairs.append(orv.ComparePair(
            index=index, rel=row["rel"],
            src_path=config.output_root / row["rel"],
            out_path=config.work_dir / row["out_rel"],
            src_size=source.size, out_size=row["out_bytes"],
            src_label=f"{source.width}×{source.height} · "
                      f"{source.bitrate / 1e6:.0f} Mbps · {vo.colour_text(source)}",
            out_label=f"{output.width}×{output.height} · "
                      f"{output.bitrate / 1e6:.1f} Mbps · {vo.colour_text(output)}",
            colour_label=_colour_arrow(source, output),
            duration=source.duration, hdr=source.is_hdr,
            slow_motion=source.is_slow_motion,
        ))
    return pairs


# --- reconciliation: finding the uploads the user made by hand ---------------

RECONCILE_SCAN_LIMIT = 4000
"""How deep into the newest-first listing to look before giving up.

A guard against enumerating a whole library when nothing matches; far more than
any plausible hand-upload batch.
"""

RECONCILE_BACKDATE = timedelta(hours=6)
"""How far before the oldest pending conversion to keep scanning.

An upload cannot predate the file it carries, so the conversion's own mtime is a
floor. The slack absorbs clock skew between this Mac and Apple's servers, and
the fact that Photos may stamp ``addedDate`` from the importing device.
"""

RECONCILE_AMBIGUOUS = "more than one new iCloud asset matches this file"
RECONCILE_MISSING = "not found in iCloud yet"


@dataclass(frozen=True)
class Arrival:
    """One asset that appeared in iCloud and matches a conversion we are owed."""

    rel: str
    asset_id: str
    filename: str
    size: int


def _pending_uploads(job: oj.OptimiseJob, config: VideoOptimiseConfig) -> list:
    """Converted rows whose file is still sitting in the hand-off folder."""
    return [row for row in job.converted()
            if row["out_rel"] and (config.work_dir / row["out_rel"]).is_file()]


def _oldest_conversion_time(rows, config: VideoOptimiseConfig) -> datetime:
    """The earliest moment any pending conversion could have been uploaded."""
    stamps = []
    for row in rows:
        path = config.work_dir / row["out_rel"]
        try:
            stamps.append(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
        except OSError:
            continue
    if not stamps:
        return datetime.now(timezone.utc) - RECONCILE_BACKDATE
    return min(stamps) - RECONCILE_BACKDATE


def reconcile(
    job: oj.OptimiseJob, client, config: VideoOptimiseConfig, *, echo,
    scan_limit: int = RECONCILE_SCAN_LIMIT,
) -> tuple[int, int]:
    """Match hand-uploaded assets to the conversions waiting for them.

    Apple closed both programmatic upload routes (see
    :mod:`icloud_photo_sync.icloud_client`), so the user performs the upload and
    this function detects the result. It is the *only* source of a verified
    ``new_asset_id``, and therefore the only thing that can ever authorise
    deleting an original — which is why it matches strictly and refuses when it
    cannot be certain.

    A row matches an asset when the **filename is exactly the conversion's flat
    name** and the **byte size is exactly what we produced**. Both Photos and
    icloud.com upload the file unaltered, so an exact size match is available and
    worth demanding: a wrong match here deletes the wrong video.

    Returns ``(found, still_waiting)``.
    """
    pending = _pending_uploads(job, config)
    if not pending:
        return 0, 0

    wanted: dict[tuple[str, int], list] = {}
    for row in pending:
        wanted.setdefault((row["out_rel"].lower(), row["out_bytes"]), []).append(row)

    since = _oldest_conversion_time(pending, config)
    originals = {row["asset_id"] for row in pending if row["asset_id"]}

    newest = client.iter_added_desc()
    if newest is None:
        echo("Could not list recent iCloud additions, so nothing could be "
             "matched up. Nothing was changed.", fg=typer.colors.YELLOW)
        return 0, len(pending)

    hits: dict[tuple[str, int], list[str]] = {}
    seen = 0
    for asset in newest:
        seen += 1
        if seen > scan_limit:
            break
        added = asset.added_dt
        if added is not None and added < since:
            break                       # newest-first: everything after is older
        key = ((asset.filename or "").lower(), asset.size or -1)
        if key in wanted and asset.id not in originals:
            hits.setdefault(key, []).append(asset.id)

    found = 0
    for key, rows in wanted.items():
        ids = hits.get(key, [])
        for row in rows:
            if len(ids) == 1 and len(rows) == 1:
                job.mark_uploaded(row["rel"], ids[0])
                found += 1
            elif len(ids) > 1 or len(rows) > 1:
                # Refusing costs one more run; guessing deletes the wrong video.
                echo(f"  ? {row['rel']}: {RECONCILE_AMBIGUOUS} — left alone",
                     fg=typer.colors.YELLOW)
    return found, len(pending) - found


# --- the swap ----------------------------------------------------------------


def _swap_one(
    row, *, client, state: StateStore, job: oj.OptimiseJob,
    config: VideoOptimiseConfig, echo,
) -> tuple[bool, int]:
    """Upload, verify, delete, verify. Returns ``(swapped, bytes freed)``.

    Every early return leaves the original in iCloud. The only path that deletes
    it runs after :func:`reconcile` has found the replacement in iCloud *and*
    this function has read it back again, and even then the delete is expressed
    through :class:`~video_optimise.Swap`, which will not construct without that
    verified id.
    """
    rel = row["rel"]
    out_path = config.work_dir / row["out_rel"]
    old_id = row["asset_id"]
    if not old_id:
        job.mark_swap_failed(rel, "no iCloud asset id recorded for this video")
        return False, 0
    if not out_path.is_file() or out_path.stat().st_size != row["out_bytes"]:
        job.mark_swap_failed(rel, "the converted file is missing or has changed")
        return False, 0

    new_id = row["new_asset_id"]
    if not new_id:
        # Unreachable through the delete loop, which only ever passes `uploaded`
        # rows. Loud rather than silent: reaching here would mean something had
        # started deleting originals whose replacement nobody had confirmed.
        raise ValueError(
            f"refusing to delete the original of {rel!r}: no verified "
            "replacement is recorded for it"
        )

    # The replacement was verified by reconciliation. Only now may the original go.
    # Re-read it here anyway: reconciliation may have run minutes or days ago,
    # and the user could have deleted the upload again in between.
    if client.verify_present(new_id) is None:
        job.mark_swap_failed(rel, "the replacement is no longer in iCloud")
        echo(f"  ✗ {rel}: the uploaded copy is no longer in iCloud — the "
             "original is untouched", fg=typer.colors.RED)
        return False, 0

    found, missing = client.lookup_assets([old_id])
    if old_id in missing:
        job.mark_swapped(rel)          # already gone; a resumed run lands here
        return True, max(0, row["src_bytes"] - row["out_bytes"])
    old = found[old_id]
    if old.in_shared_library:
        job.mark_swap_failed(rel, "the original lives in a shared library")
        return False, 0
    if old.is_deleted or old.is_expunged:
        job.mark_swapped(rel)
        return True, max(0, row["src_bytes"] - row["out_bytes"])

    swap = vo.Swap(rel=rel, old_asset_id=old_id, new_asset_id=new_id,
                   old_size=row["src_bytes"], new_size=row["out_bytes"])

    results = client.delete_assets([old])
    if not results or not results[0].ok:
        detail = results[0].error if results else "no outcome returned"
        job.mark_swap_failed(rel, f"delete refused: {detail}")
        echo(f"  ✗ {rel}: iCloud refused the delete ({detail}). The replacement "
             "is uploaded; re-run to finish.", fg=typer.colors.RED)
        return False, 0
    if not client.verify_deleted([old_id]).get(old_id):
        job.mark_swap_failed(rel, "iCloud accepted the delete but still has the asset")
        return False, 0

    job.mark_swapped(rel)
    _record_swap(state, row, new_id, config, client)
    return True, swap.freed


def _record_swap(state: StateStore | None, row, new_id: str,
                 config: VideoOptimiseConfig, client) -> None:
    """Teach the sync manifest about the replacement.

    Without this the next ``sync`` sees an asset it has never downloaded and
    fetches the optimised copy back as a *third* file beside the original and
    the conversion.
    """
    if state is None:
        return                         # --no-upload, or a caller with no manifest
    try:
        asset = client.verify_present(new_id)
        if asset is None:
            return
        out_abs = config.work_dir / row["out_rel"]
        try:
            dest_rel = out_abs.relative_to(config.output_root).as_posix()
        except ValueError:
            return                     # work dir is off-tree; let sync decide
        ref = AssetRef(id=new_id, filename=asset.filename or out_abs.name,
                       capture_dt=asset.capture_dt, added_dt=None,
                       size=asset.size or row["out_bytes"])
        state.register(ref, dest_rel)
        state.mark_completed(new_id, row["out_bytes"])
        state.record_remote_deletion(
            asset_id=row["asset_id"], dest_path=row["rel"],
            filename=Path(row["rel"]).name, capture_dt=None,
            expected_size=row["src_bytes"],
            # The job database is this deletion's provenance: it holds the
            # converted file, the verified replacement id and the timestamps.
            receipt_path=str(config.job_db),
            verified_at=datetime.now(timezone.utc).isoformat(),
        )
    except (ICloudSyncError, OSError) as exc:
        # The swap itself succeeded; a bookkeeping failure must not undo it.
        logger.warning("could not record the swap for %s: %s", row["rel"], exc)


def confirm_delete(count: int, *, prompt: Callable[..., str] = typer.prompt) -> bool:
    """One typed confirmation for the whole run, then the safety phrase.

    Deliberately not one per batch: the earlier deletion feature learned that
    asking three times for one decision trains the user to type without reading.

    The phrase says *delete originals* rather than *swap*, because that is
    literally what this step does now — the replacement is already in iCloud,
    put there by hand, and the only thing left to do is destructive.
    """
    if not sys.stdin.isatty():
        raise ICloudSyncError(
            "Refusing to delete videos from iCloud without an interactive confirmation."
        )
    wanted = f"delete {count} originals"
    if _normalise(prompt(f"Type  {wanted}  to continue", default="",
                         show_default=False)) != _normalise(wanted):
        return False
    return _normalise(prompt(f"Now type  {SURE_PHRASE}  to do it",
                             default="", show_default=False)) == _normalise(SURE_PHRASE)


def _reconcile_and_delete(
    job: oj.OptimiseJob, config: VideoOptimiseConfig, armed: ArmedICloud,
    *, session_factory, echo, progress=None, cancel: Event | None = None,
    confirm_fn=None,
) -> Totals:
    """Find the user's hand-uploads, then delete the originals they replace.

    This is the whole iCloud half of the command now. It runs *before* the scan,
    so a run whose only purpose is finishing yesterday's uploads never has to
    walk the library first.
    """
    totals = Totals()
    icloud = armed.config
    try:
        service, client = session_factory(icloud.app).resume()
    except ICloudSyncError as exc:
        echo(f"\n{exc}", fg=typer.colors.RED, err=True)
        echo("Nothing was changed in iCloud. Log in, then re-run the same command.",
             err=True)
        return totals
    if ic.account_dsid(service) != armed.dsid:
        echo("The signed-in account changed since this run started; nothing was "
             "changed in iCloud.", fg=typer.colors.RED, err=True)
        return totals

    waiting_before = len(_pending_uploads(job, config))
    if waiting_before:
        echo(f"\nChecking iCloud for the {waiting_before} converted file(s) "
             "waiting to be uploaded…", fg=typer.colors.BLUE)
        found, waiting = reconcile(job, client, config, echo=echo)
        if found:
            echo(f"  ✓ found {found} of them in iCloud.", fg=typer.colors.GREEN)
        if waiting:
            echo(f"  · {waiting} still waiting. Upload them from "
                 f"{config.work_dir} and run this again.", fg=typer.colors.YELLOW)

    rows = job.by_status(oj.STATUS_UPLOADED)
    if not rows:
        return totals

    _warn_before_delete(rows, armed, echo)
    confirm_fn = confirm_fn or confirm_delete
    if not confirm_fn(len(rows)):
        echo("Not confirmed; nothing was deleted from iCloud.",
             fg=typer.colors.YELLOW)
        return totals

    bar = progress(total=len(rows), desc="Deleting originals", unit="video") \
        if progress else None
    try:
        with StateStore(icloud.app.state_db) as state:
            for row in rows:
                if cancel is not None and cancel.is_set():
                    echo("\nStopped. Re-run the same command to finish the rest.",
                         fg=typer.colors.YELLOW)
                    break
                try:
                    ok, freed = _swap_one(row, client=client, state=state, job=job,
                                          config=config, echo=echo)
                except ICloudSyncError as exc:
                    job.mark_swap_failed(row["rel"], str(exc))
                    echo(f"  ✗ {row['rel']}: {exc}", fg=typer.colors.RED)
                    ok, freed = False, 0
                if ok:
                    totals.swapped += 1
                    totals.freed_cloud += freed
                    echo(f"  ✓ {row['rel']}  replacement verified → original "
                         f"deleted → verified  ({_size(freed)} freed)",
                         fg=typer.colors.GREEN)
                else:
                    totals.failed += 1
                if bar:
                    bar.update(1)
    finally:
        if bar:
            bar.close()
    return totals


# --- local cleanup -----------------------------------------------------------


def _cleanup_locals(
    job: oj.OptimiseJob, config: VideoOptimiseConfig, *, echo,
    state_db: Path | None = None,
    confirm: Callable[[str], bool] = typer.confirm, trash_fn=move_to_trash,
) -> int:
    """Offer to Trash the originals of swapped videos, then move the copies in.

    Only ``swapped`` rows are eligible, and they go to the Trash rather than
    being unlinked: a swap that turns out to have been a mistake is recoverable
    from Recently Deleted for thirty days, and this keeps the local side
    recoverable for as long as the user's Trash holds.
    """
    rows = job.swapped()
    originals = [(row, config.output_root / row["rel"]) for row in rows]
    live = [(row, path) for row, path in originals if path.is_file()]
    if not live:
        return 0
    total = sum(path.stat().st_size for _, path in live)
    echo("")
    if not confirm(f"Move the {len(live)} local original(s) to the Trash? "
                   f"({_size(total)})"):
        echo("Kept. The optimised copies are in "
             f"{config.work_dir}.", fg=typer.colors.YELLOW)
        return 0

    results = trash_fn([path for _, path in live])
    moved = {r.path for r in results if r.ok}
    failed = [r for r in results if not r.ok]
    echo(f"  ✓ {len(moved)} moved to the Trash.", fg=typer.colors.GREEN)
    for result in failed:
        echo(f"  ✗ {result.path}: {result.error}", fg=typer.colors.RED, err=True)

    # With the original out of the way the converted copy can take its place in
    # the tree, so the library looks the way it did before, only smaller. The
    # sync manifest is repointed at the same time, or the next pass would see a
    # completed asset with nothing at its recorded path and fetch it again.
    placed = 0
    state_ctx = StateStore(state_db) if state_db is not None else _NoState()
    with state_ctx as state:
        for row, path in live:
            if path in moved and _place_converted(row, config, state):
                placed += 1
    if placed:
        echo(f"  ✓ {placed} optimised file(s) moved into the library.",
             fg=typer.colors.GREEN)
    return len(failed)


class _NoState:
    """Stands in for the sync manifest on an offline (``--no-upload``) run."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _place_converted(row, config: VideoOptimiseConfig, state) -> bool:
    src = config.work_dir / row["out_rel"]
    dest = (config.output_root / row["rel"]).with_suffix(".mov")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    except OSError as exc:
        logger.warning("could not move %s into place: %s", src, exc)
        return False
    if state is not None and row["new_asset_id"]:
        try:
            state.update_dest(row["new_asset_id"],
                              dest.relative_to(config.output_root).as_posix())
        except Exception as exc:                  # bookkeeping only
            logger.debug("could not repoint %s: %s", row["new_asset_id"], exc)
    return True


def _discard_rejected(job: oj.OptimiseJob, config: VideoOptimiseConfig, *, echo,
                      confirm: Callable[[str], bool] = typer.confirm) -> None:
    rows = [r for r in job.by_status(oj.STATUS_REJECTED) if r["out_rel"]]
    live = [(r, config.work_dir / r["out_rel"]) for r in rows]
    live = [(r, p) for r, p in live if p.is_file()]
    if not live:
        return
    total = sum(p.stat().st_size for _, p in live)
    if not confirm(f"Discard the {len(live)} converted file(s) you kept the "
                   f"original of? ({_size(total)})"):
        return
    for _, path in live:
        path.unlink(missing_ok=True)
    echo(f"  ✓ Discarded {len(live)} converted file(s).", fg=typer.colors.GREEN)


def _prune_empty_dirs(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if not dirnames and not filenames and Path(dirpath) != root:
            try:
                Path(dirpath).rmdir()
            except OSError:
                pass


# --- the command ------------------------------------------------------------


def _preflight(config: VideoOptimiseConfig, echo) -> str | None:
    """Whole-run refusals that have nothing to do with any one video."""
    if not config.output_root.is_dir():
        return f"{config.output_root} is not a folder I can read."
    if not tc.ffmpeg_available():
        return ("ffmpeg and ffprobe are not on PATH, so nothing can be converted.\n"
                "  brew install ffmpeg")
    if not tc.encoder_available():
        return ("This ffmpeg has no hevc_videotoolbox encoder, which is what makes "
                "the conversion fast enough to be worth doing on this machine.\n"
                "  brew install ffmpeg")
    return None


def _free_bytes(path: Path) -> int | None:
    try:
        stats = os.statvfs(path)
        return stats.f_bavail * stats.f_frsize
    except OSError:
        return None


def run_optimise(
    config: VideoOptimiseConfig,
    icloud: ICloudDeleteConfig | None = None,
    *,
    session_factory=SessionManager,
    echo=typer.secho,
    progress=None,
    confirm: Callable[..., bool] = typer.confirm,
    prompt: Callable[..., str] = typer.prompt,
    choose=orv.choose_videos,
    compare=orv.compare_results,
    cancel: Event | None = None,
    probe_fn=tc.probe,
    convert_fn=tc.convert,
) -> int:
    """Reconcile → delete → scan → select → convert → hand off. Exit code.

    Reconciliation comes first, deliberately. A run whose only purpose is
    finishing yesterday's uploads should not have to walk a 25,000-file library
    before it does the one thing it was started for, and the delete phase is the
    only irreversible part of the command — it belongs at the front, while the
    user is still paying attention, not after ten minutes of encoding.

    ``icloud`` is None for ``--offline``, and then nothing here resolves an
    Apple ID, reads the Keychain or touches the network — the same
    credential-free contract ``local-clean`` and ``video-clean`` hold.
    """
    problem = _preflight(config, echo)
    if problem:
        echo(problem, fg=typer.colors.RED, err=True)
        return 2

    armed = arm(icloud, note=ARM_NOTE_OPTIMISE) if icloud is not None else None
    root = config.output_root
    totals = Totals()

    with oj.OptimiseJob(config.job_db) as job:
        if config.restart:
            job.clear()
        state_db = armed.config.app.state_db if armed is not None else None
        if state_db is not None:
            _backfill_asset_ids(job, state_db)
        # "Re-run to retry" has to be true: a failed delete goes back to the
        # queue rather than stranding its conversion forever. This runs BEFORE
        # the migration, which only considers live rows — otherwise a job whose
        # last run ended in failures would never get its files moved.
        job.reset_swap_failed()
        _migrate_work_dir(job, config, echo)

        # --- phase one: finish what is already in flight ---------------------
        if armed is not None:
            totals = _reconcile_and_delete(
                job, config, armed, session_factory=session_factory, echo=echo,
                progress=progress, cancel=cancel,
                confirm_fn=lambda n: confirm_delete(n, prompt=prompt))
            _cleanup_locals(job, config, echo=echo, state_db=state_db,
                            confirm=confirm)
            _discard_rejected(job, config, echo=echo, confirm=confirm)
            _report_deletes(job, totals, echo)

        # Whatever else happened, if files are still sitting in the hand-off
        # folder the user needs to be told — on every run, not just the one that
        # produced them. Finder is only opened at the end of a fresh conversion
        # (see _run_phases); popping it open on every invocation would be rude.
        if config.reconcile_only:
            _report_handoff(job, config, echo, opened=False)
            return 1 if totals.failed else 0
        if armed is None and _pending_uploads(job, config):
            _report_handoff(job, config, echo, opened=False)
        if cancel is not None and cancel.is_set():
            return 130

        # --- phase two: convert some more ------------------------------------
        echo(f"\nScanning {root} for videos…", fg=typer.colors.BLUE)
        videos = scan_videos(root)
        if not videos:
            echo("No videos found.", fg=typer.colors.GREEN)
            return 0

        probes = probe_all(videos, probe_fn=probe_fn, progress=progress,
                           cancel=cancel)
        if cancel is not None and cancel.is_set():
            return 130
        plan = vo.build_plan(
            probes, rels=[rel for _, rel, _ in videos],
            image_stems=image_stems(root),
            min_bytes=config.min_bytes, short_side=config.short_side,
            max_fps=config.max_fps, hdr_bitrate=config.hdr_bitrate,
            sdr_bitrate=config.sdr_bitrate, skip_hdr=config.skip_hdr,
            hdr_only=config.hdr_only,
        )
        _report_library(root, videos, plan, echo)

        refusal = plan.refusal(free_bytes=_free_bytes(root), max_convert=None,
                               min_free=config.min_free_bytes)
        if refusal:
            echo(f"\n{refusal}", fg=typer.colors.RED, err=True)
            return 2
        if config.dry_run:
            _report_dry_run(plan, config, echo)
            return 0
        if not plan.candidates:
            _report_handoff(job, config, echo, opened=False)
            return 1 if totals.failed else 0

        code = _run_phases(job, config, plan, videos, armed,
                           echo=echo, progress=progress, confirm=confirm,
                           choose=choose, compare=compare, cancel=cancel,
                           probe_fn=probe_fn, convert_fn=convert_fn)
    return code or (1 if totals.failed else 0)


def _report_dry_run(plan: vo.OptimisePlan, config: VideoOptimiseConfig, echo) -> None:
    echo("\nDry run — nothing will be converted, uploaded or deleted.",
         fg=typer.colors.YELLOW)
    for candidate in plan.candidates[:20]:
        argv = tc.build_argv(config.output_root / candidate.rel,
                             config.work_path(candidate.rel), candidate.encode,
                             has_audio=candidate.probe.has_audio)
        echo(f"\n  {candidate.rel}  {_size(candidate.probe.size)} → about "
             f"{_size(candidate.predicted_size)}")
        echo("    " + " ".join(argv), fg=typer.colors.BLUE)
    if len(plan.candidates) > 20:
        echo(f"\n  … and {len(plan.candidates) - 20} more.")


def _seed(job: oj.OptimiseJob, plan: vo.OptimisePlan, picked: set[int],
          items: Sequence[orv.SelectItem], state_db: Path | None) -> int:
    """Write the ticked videos into the job, with their iCloud asset ids."""
    by_rel = {c.rel: c for c in plan.candidates}
    chosen = [items[i].rel for i in sorted(picked)
              if 0 <= i < len(items) and items[i].selectable]
    asset_ids: dict[str, str] = {}
    if state_db is not None and state_db.exists():
        with StateStore(state_db, read_only=True) as state:
            for row in state.iter_completed():
                if row["dest_path"] in by_rel:
                    asset_ids[row["dest_path"]] = row["id"]
    for rel in chosen:
        candidate = by_rel.get(rel)
        if candidate is None:
            continue
        job.add(rel, asset_id=asset_ids.get(rel), src_bytes=candidate.probe.size,
                src_probe=_probe_dict(candidate.probe),
                plan=_encode_dict(candidate.encode))
    return len(chosen)


def _run_phases(
    job: oj.OptimiseJob, config: VideoOptimiseConfig, plan: vo.OptimisePlan,
    videos, armed: ArmedICloud | None, *, echo, progress,
    confirm, choose, compare, cancel, probe_fn, convert_fn,
) -> int:
    """Select → convert → review → hand off. No iCloud mutation happens here."""
    state_db = armed.config.app.state_db if armed is not None else None
    counts = job.counts()
    if counts[oj.STATUS_SELECTED]:
        echo(f"\nResuming: {counts[oj.STATUS_SELECTED]} video(s) still to convert.",
             fg=typer.colors.YELLOW)
    else:
        durations = probe_durations(path for path, _, _ in videos)
        items = _select_items(plan, videos, durations)
        picked = choose(items, port=config.port, open_browser=config.open_browser,
                        posters=PosterCache(config.poster_cache_dir),
                        echo=lambda text: echo(text))
        if not picked:
            echo("Nothing selected; nothing was changed.", fg=typer.colors.YELLOW)
            return 0
        limited = sorted(picked)[:config.limit] if config.limit else sorted(picked)
        seeded = _seed(job, plan, set(limited), items, state_db)
        if config.limit and len(picked) > len(limited):
            echo(f"--limit {config.limit}: converting {seeded} of {len(picked)} now; "
                 "re-run to continue with the rest.", fg=typer.colors.YELLOW)
        if not seeded:
            return 0

    totals = _convert_all(job, config, echo=echo, progress=progress, cancel=cancel,
                          convert_fn=convert_fn, probe_fn=probe_fn)
    if cancel is not None and cancel.is_set():
        return 130

    converted = job.converted()
    if not converted:
        _report_conversion(job, totals, echo)
        return 1 if totals.failed else 0

    # --- the comparison screen: a quality gate before the user spends effort --
    pairs = _pairs(job, config, converted)
    outcome = compare(pairs[:COMPARE_TOP_N], review_all=False, total=len(pairs),
                      port=config.port, open_browser=config.open_browser,
                      posters=PosterCache(config.poster_cache_dir),
                      echo=lambda text: echo(text))
    if outcome.choice == orv.CHOICE_REVIEW_ALL:
        outcome = compare(pairs, review_all=True, total=len(pairs),
                          port=config.port, open_browser=config.open_browser,
                          posters=PosterCache(config.poster_cache_dir),
                          echo=lambda text: echo(text))
        keep = {p.index for p in pairs} - set(outcome.selected)
        for index in keep:
            job.mark_rejected(pairs[index].rel)
        if outcome.choice == orv.CHOICE_CANCEL:
            echo("Cancelled. The conversions are still in "
                 f"{config.work_dir}.", fg=typer.colors.YELLOW)
            return 0
    elif outcome.choice == orv.CHOICE_CANCEL:
        echo("Cancelled. The conversions are still in "
             f"{config.work_dir} — re-run to pick up where this stopped.",
             fg=typer.colors.YELLOW)
        return 0

    _discard_rejected(job, config, echo=echo, confirm=confirm)
    _report_conversion(job, totals, echo)
    _report_handoff(job, config, echo)
    return 1 if totals.failed else 0


def _report_handoff(job: oj.OptimiseJob, config: VideoOptimiseConfig, echo,
                    *, opened: bool = True) -> None:
    """Tell the user what to upload, from where, and what to do afterwards.

    This is the hinge of the whole command: Apple accepts uploads only from its
    own clients, so the one step the tool cannot take is spelled out here, and
    the folder is opened in Finder so the files are already in front of them.
    """
    pending = _pending_uploads(job, config)
    if not pending:
        return
    total = sum(row["out_bytes"] or 0 for row in pending)
    echo("")
    echo(f"{len(pending)} optimised video(s), {_size(total)}, are ready in:",
         fg=typer.colors.GREEN)
    echo(f"  {config.work_dir}", fg=typer.colors.WHITE)
    echo("")
    echo("Upload them to iCloud Photos yourself — Apple no longer accepts")
    echo("uploads from anything but its own apps. Any of these works:")
    echo("  · icloud.com/photos in a browser: drag the files in")
    echo("  · Photos on a Mac: File → Import, or drag them in")
    echo("  · iPhone/iPad: copy the folder over and add them from Files")
    echo("")
    echo("Then run  icloud-photo-sync video-optimise  again. It checks iCloud",
         fg=typer.colors.YELLOW)
    echo("for the uploads first, and offers to delete the originals they")
    echo("replace. Your originals are untouched until then.")
    if opened:
        _open_folder(config.work_dir)


def _open_folder(path: Path) -> None:
    """Show the hand-off folder in Finder. Best effort; never fails the run."""
    import subprocess
    try:
        subprocess.run(["open", str(path)], check=False, capture_output=True,
                       timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("could not open %s: %s", path, exc)


def _migrate_work_dir(job: oj.OptimiseJob, config: VideoOptimiseConfig, echo) -> int:
    """Flatten conversions written under the old hidden, nested work directory.

    Those files are hours of encoding and perfectly good; re-doing them because
    the folder moved would be wasteful and would look like a bug. Rows are
    matched by the ``/`` in their recorded ``out_rel``, which only the old
    ``YYYY/MM/NAME.mov`` layout ever produced.
    """
    stale = [row for row in job.by_status(
        oj.STATUS_CONVERTED, oj.STATUS_REJECTED, oj.STATUS_UPLOADED,
        oj.STATUS_SWAP_FAILED)
        if row["out_rel"] and "/" in row["out_rel"]]
    if not stale:
        return 0
    taken = {p.name for p in config.work_dir.glob("*.mov")}
    moved = 0
    for row in stale:
        old_path = config.legacy_work_dir / row["out_rel"]
        if not old_path.is_file():
            continue
        name = vo.flat_name(row["rel"], taken=taken)
        try:
            config.work_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_path), str(config.work_path(name)))
        except OSError as exc:
            logger.warning("could not migrate %s: %s", old_path, exc)
            continue
        taken.add(name)
        job.set_out_rel(row["rel"], name)
        moved += 1
    if moved:
        echo(f"Moved {moved} earlier conversion(s) into {config.work_dir}.",
             fg=typer.colors.BLUE)
        _prune_empty_dirs(config.legacy_work_dir)
    return moved


def _backfill_asset_ids(job: oj.OptimiseJob, state_db: Path) -> int:
    """Give rows from an offline run the iCloud ids they could not have had.

    ``--offline`` resolves no Apple ID, so it seeds rows with ``asset_id`` NULL.
    Resuming that job online has to be able to fill them in, or every one of
    those conversions is stranded: they are already ``converted``, and
    :meth:`OptimiseJob.add` will not reset a converted row.
    """
    if not state_db.exists():
        return 0
    wanted = {row["rel"] for row in job.by_status(
        oj.STATUS_SELECTED, oj.STATUS_CONVERTED, oj.STATUS_SWAP_FAILED)
        if not row["asset_id"]}
    if not wanted:
        return 0
    filled = 0
    with StateStore(state_db, read_only=True) as state:
        for row in state.iter_completed():
            if row["dest_path"] in wanted and job.set_asset_id(row["dest_path"], row["id"]):
                filled += 1
    if filled:
        logger.info("filled in %d iCloud asset id(s) from the sync manifest", filled)
    return filled


def _warn_before_delete(rows, armed: ArmedICloud, echo) -> None:
    total_old = sum(r["src_bytes"] for r in rows)
    total_new = sum(r["out_bytes"] or 0 for r in rows)
    echo("")
    echo(f"{len(rows)} optimised copy(ies) are now in iCloud for {armed.who}.",
         fg=typer.colors.GREEN)
    echo(f"Deleting the originals they replace frees "
         f"{_size(max(0, total_old - total_new))} "
         f"({_size(total_old)} → {_size(total_new)}).")
    echo("")
    echo("  Each replacement was found in iCloud by filename and exact byte")
    echo("  size, and is read back once more immediately before its original")
    echo("  is touched. Nothing is deleted whose replacement is not there.")
    echo("")
    echo("  Your uploaded copy is a NEW asset. It keeps its capture date,",
         fg=typer.colors.YELLOW)
    echo("  timezone and location, but has no album membership, Favourites,")
    echo("  captions, keywords, face tags or place in Memories, and its")
    echo("  \"Added\" date is when you uploaded it, so Recently Added reorders.")
    echo("")
    echo(f"  Originals go to Recently Deleted, recoverable for {RECOVERY_DAYS} days.")


def _report_conversion(job: oj.OptimiseJob, totals: Totals, echo) -> None:
    counts = job.counts()
    echo("")
    echo(f"Converted {counts[oj.STATUS_CONVERTED] + counts[oj.STATUS_UPLOADED] + counts[oj.STATUS_SWAPPED]}"
         f" video(s).", fg=typer.colors.GREEN)
    for status, label in (
        (oj.STATUS_NOT_WORTH_IT, "left alone — the output was not enough smaller"),
        (oj.STATUS_COLOUR_MISMATCH, "left alone — the output lost its colour"),
        (oj.STATUS_CONVERT_FAILED, "failed to convert"),
    ):
        if counts[status]:
            colour = (typer.colors.RED if status != oj.STATUS_NOT_WORTH_IT
                      else typer.colors.YELLOW)
            echo(f"  {counts[status]:>5}  {label}", fg=colour)


def _report_deletes(job: oj.OptimiseJob, totals: Totals, echo) -> None:
    if not (totals.swapped or totals.failed):
        return
    counts = job.counts()
    echo("")
    if totals.swapped:
        echo(f"Deleted {totals.swapped} original(s) from iCloud, freeing "
             f"{_size(totals.freed_cloud)}.", fg=typer.colors.GREEN)
    if counts[oj.STATUS_SWAP_FAILED]:
        echo(f"  {counts[oj.STATUS_SWAP_FAILED]} could not be deleted; those "
             "originals are untouched. Re-run to retry.", fg=typer.colors.RED)
