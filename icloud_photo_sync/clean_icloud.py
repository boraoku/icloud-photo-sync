"""The terminal side of "also delete from iCloud", shared by both clean commands.

Two moments, deliberately far apart:

:func:`arm` runs **before the scan**, so an expired session costs two seconds
rather than an hour of reviewing that ends in "run login". It proves the account
can be reached, that this build can delete at all, and that the manifest really
describes this Apple ID and this folder — then closes everything again, because a
review session can run for hours and a held-open handle buys nothing.

:func:`finish_and_report` runs **after the browser session ends**, when the files
are already in the Trash and every remaining decision is reversible. It plans,
shows exactly what would go, writes the manifest, asks for the count to be typed
back, and only then deletes — verifying each batch.

Every prompt and every line of output is injected, so the whole flow is testable
without a terminal or an account.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable, Sequence

import typer

from . import icloud_client as ic
from . import icloud_delete as idel
from . import retro_clean
from .auth import SessionManager
from .config import DEFAULT_CLEAN_MAX_BYTES, ICloudDeleteConfig
from .errors import ICloudSyncError, ManifestMismatchError
from .logutil import get_logger
from .review import TrashOutcome
from .state import StateStore

logger = get_logger(__name__)

RECOVERY_DAYS = 30


@dataclass(frozen=True)
class ArmedICloud:
    """A pre-flighted authorisation for one review session. Holds no handles."""

    config: ICloudDeleteConfig
    account_name: str
    dsid: str
    tracked_assets: int

    @property
    def apple_id(self) -> str:
        return self.config.app.apple_id

    @property
    def who(self) -> str:
        return f"{self.account_name} <{self.apple_id}>" if self.account_name else self.apple_id


def arm(
    icloud: ICloudDeleteConfig,
    *,
    session_factory: Callable[..., SessionManager] = SessionManager,
    echo: Callable[..., None] = typer.secho,
) -> ArmedICloud:
    """Prove we could delete, before the user trashes anything. Raises on doubt."""
    echo("Checking your iCloud session…", fg=typer.colors.BLUE)

    # Read-only, and it refuses to create: an empty auto-made manifest is the
    # signature of a wrong Apple ID or a wrong folder, not of an empty library.
    with StateStore(icloud.app.state_db, read_only=True) as state:
        tracked = state.counts()["completed"]
        stamped = state.identity()

    session = session_factory(icloud.app)
    service, client = session.resume()
    if not client.supports_delete():
        raise ICloudSyncError(
            "This pyicloud build cannot delete photos (no CloudKit client). "
            "Nothing was changed; re-run without --icloud-delete."
        )

    dsid = ic.account_dsid(service)
    name = ic.account_name(service)
    _check_identity(stamped, icloud, dsid)

    # Record who this manifest belongs to, so a later run can check rather than
    # recompute. Safe to write: the manifest's own filename is derived from this
    # Apple ID and folder, so we are only writing down what is already true.
    with StateStore(icloud.app.state_db) as state:
        state.stamp_identity(apple_id=icloud.app.apple_id,
                             output_root=str(icloud.app.output_root),
                             dsid=dsid, account_name=name)

    armed = ArmedICloud(config=icloud, account_name=name, dsid=dsid,
                        tracked_assets=tracked)
    echo(f"iCloud deletion armed for {armed.who} "
         f"({tracked:,} assets tracked for this folder)", fg=typer.colors.YELLOW)
    echo("Files you trash will also be offered for deletion from iCloud when the "
         "review ends — with a final confirmation here.")
    return armed


def _check_identity(stamped: dict, icloud: ICloudDeleteConfig, dsid: str) -> None:
    """Refuse if the manifest was written for a different account or folder."""
    if stamped.get("apple_id") and stamped["apple_id"].lower() != icloud.app.apple_id.lower():
        raise ManifestMismatchError(
            f"That manifest belongs to {stamped['apple_id']}, not {icloud.app.apple_id}."
        )
    if stamped.get("dsid") and dsid and stamped["dsid"] != dsid:
        raise ManifestMismatchError(
            "The signed-in account is not the one this manifest was written for."
        )
    if stamped.get("output_root") and \
            Path(stamped["output_root"]) != icloud.app.output_root:
        raise ManifestMismatchError(
            f"That manifest describes {stamped['output_root']}, "
            f"not {icloud.app.output_root}."
        )


# --- after the review ---------------------------------------------------------


def finish_and_report(
    armed: ArmedICloud | None,
    outcome: TrashOutcome,
    *,
    source: str,
    session_factory: Callable[..., SessionManager] = SessionManager,
    confirm: Callable[[int], bool] | None = None,
    echo: Callable[..., None] = typer.secho,
    progress: Callable | None = None,
    cancel: Event | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Plan → show → manifest → confirm → delete → verify → report. Returns an exit code.

    ``armed is None`` (the credential-free path) returns 0 without doing
    anything at all.
    """
    if armed is None or not outcome.icloud:
        return 0

    icloud = armed.config
    stamp = (now or _utc_now)().strftime("%Y%m%d-%H%M%S")
    manifest_path = icloud.manifest_dir / f"{stamp}-{source}-{icloud.state_key}.json"

    with StateStore(icloud.app.state_db, read_only=True) as state:
        plan = idel.build_plan(outcome.icloud, state=state,
                               output_root=icloud.app.output_root,
                               sizes=outcome.sizes)

    _report_plan(plan, len(outcome.icloud), echo)
    idel.write_manifest(manifest_path, plan, {
        "source": source, "apple_id": armed.apple_id, "dsid": armed.dsid,
        "output_root": str(icloud.app.output_root), "created_at": stamp,
        "evidence": idel.EVIDENCE_MEASURED,
    })
    echo(f"Manifest: {manifest_path}")

    if not plan.candidates:
        echo("Nothing to delete from iCloud.", fg=typer.colors.GREEN)
        return 0

    refusal = plan.guard_refusal(completed_rows=armed.tracked_assets,
                                 max_delete=icloud.max_delete)
    if refusal:
        echo(refusal, fg=typer.colors.RED, err=True)
        return 1

    if icloud.dry_run:
        echo("Dry run: nothing was deleted from iCloud.", fg=typer.colors.YELLOW)
        return 0

    echo(f"\nDeleting from iCloud removes these photos from EVERY device signed "
         f"into {armed.who}.", fg=typer.colors.YELLOW)
    echo(f"They stay recoverable in Recently Deleted for about {RECOVERY_DAYS} days.")
    if not (confirm or _confirm_by_count)(len(plan.candidates)):
        echo("Not confirmed — nothing was deleted from iCloud.", fg=typer.colors.YELLOW)
        return 0

    return _apply(armed, plan, manifest_path, source=source,
                  session_factory=session_factory, echo=echo,
                  progress=progress, cancel=cancel)


def _apply(
    armed: ArmedICloud,
    plan: idel.DeletionPlan,
    manifest_path: Path,
    *,
    source: str,
    session_factory,
    echo,
    progress,
    cancel,
) -> int:
    icloud = armed.config
    receipt_path = manifest_path.with_suffix(".receipt.jsonl")

    try:
        service, client = session_factory(icloud.app).resume()
    except ICloudSyncError as exc:
        echo(f"\n{exc}", fg=typer.colors.RED, err=True)
        echo("Nothing was deleted from iCloud. After logging in, run:\n"
             "  icloud-photo-sync icloud-delete --last", err=True)
        return 3
    if ic.account_dsid(service) != armed.dsid:
        echo("The signed-in account changed since this session started; "
             "nothing was deleted.", fg=typer.colors.RED, err=True)
        return 2

    bar = progress(total=len(plan.candidates), desc="Deleting from iCloud",
                   unit="asset") if progress else None

    with idel.Receipt(receipt_path) as receipt, \
            StateStore(icloud.app.state_db) as state:
        for candidate in plan.candidates:
            receipt.intent(candidate)

        def resolved(candidate, status: str, detail: str) -> None:
            receipt.result(candidate, status, detail)
            if status == "deleted":
                state.record_remote_deletion(
                    asset_id=candidate.asset_id, dest_path=candidate.rel,
                    filename=candidate.filename, capture_dt=candidate.capture_dt,
                    expected_size=candidate.expected_size,
                    receipt_path=str(receipt_path), verified_at=_utc_now().isoformat(),
                )

        report = idel.execute(
            plan, client, batch_size=icloud.batch_size, cancel=cancel,
            on_progress=(bar.update if bar else None), on_resolved=resolved,
        )
        receipt.trailer(source=source, apple_id=armed.apple_id, dsid=armed.dsid,
                        deleted=len(report.deleted), already=len(report.already),
                        failed=len(report.failed), refused=len(report.refused),
                        unverified=len(report.unverified),
                        cancelled=report.cancelled)
    if bar:
        bar.close()

    _report_result(report, receipt_path, echo)
    return report.exit_code()


# --- output -------------------------------------------------------------------


def _report_plan(plan: idel.DeletionPlan, queued: int, echo) -> None:
    echo(f"\niCloud deletion — {queued} file(s) queued:")
    echo(f"  eligible                 {len(plan.candidates)}")
    by_reason: dict[str, list] = {}
    for skip in plan.skipped:
        by_reason.setdefault(skip.reason, []).append(skip)
    for reason, skips in by_reason.items():
        echo(f"  left in iCloud ({len(skips)}): {reason}")
        for skip in skips[:10]:
            detail = f"  [{skip.detail}]" if skip.detail else ""
            echo(f"      {skip.rel}{detail}")
        if len(skips) > 10:
            echo(f"      … and {len(skips) - 10} more (see the manifest)")


def _fmt_size(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MiB" if n >= 1024 * 1024 else f"{n:,} B"


def _report_scan(scan_result, armed: ArmedICloud, echo) -> None:
    """Show the reconstruction before showing any conclusion drawn from it."""
    icloud = armed.config
    log = scan_result.trash_log
    echo(f"\nRetrospective scan — {icloud.app.output_root}")
    echo(f"  last verified complete pass  {scan_result.verified_present_at or '—'}")
    echo(f"  log files read               {len(log.files)}"
         f"{f' (back to {retro_clean.fmt_time(log.oldest_entry)})' if log.files else ''}")
    echo(f"  trash rounds in the window   {len(log.rounds)}"
         + (f"  ({retro_clean.fmt_time(log.rounds[-1])} most recent)" if log.rounds else ""))
    echo(f"  envelopes assumed            local-clean ≤ {_fmt_size(scan_result.max_bytes)}"
         " (--max-size)")
    echo(f"                               video-clean ≥ {_fmt_size(scan_result.min_bytes)}"
         " (--min-size)")
    echo(f"  tracked files missing        {len(scan_result.missing):,} "
         f"of {armed.tracked_assets:,}")
    echo(f"  outside every envelope       {len(scan_result.out_of_envelope)}")

    videos = sum(1 for which in (scan_result.evidence.envelopes.values()
                                 if scan_result.evidence else ())
                 if which == retro_clean.ENVELOPE_VIDEO_CLEAN)
    if videos and scan_result.min_bytes == 0:
        # Worth saying plainly: for video the envelope excludes nothing, so it
        # contributes no evidence and the tripwire cannot fire on a video.
        echo(f"\n  Caution: --min-size is 0, so every video counts as inside the "
             f"video-clean envelope. For the {videos} missing video(s) that test "
             "rules nothing out — set --min-size to what those sessions used, or "
             "review the thumbnails carefully.", fg=typer.colors.YELLOW)


def _report_evidence(scan_result, plan: idel.DeletionPlan, echo) -> None:
    """Spell out what is standing in for the measurement that never happened."""
    if not plan.candidates:
        return
    log = scan_result.trash_log
    echo("\nEVIDENCE: retrospective. These files were already gone when this ran, so "
         "their size could NOT be measured before deletion. The normal check — the "
         "manifest size against the file's size the moment before it was trashed — "
         "did not happen.", fg=typer.colors.YELLOW)
    echo("What stands in for it:")
    echo(f"  • every row was verified on disk at {scan_result.verified_present_at} "
         "by a completed pass")
    echo(f"  • all {len(plan.candidates)} fall inside a clean command's scan envelope")
    echo("  • none still has a classification-cache row (trashing purges it)")
    if log.rounds:
        echo(f"  • {len(log.rounds)} trash round(s) logged in the window and nothing "
             "else removed files")


def _report_result(report: idel.DeleteReport, receipt_path: Path, echo) -> None:
    echo("")
    if report.deleted:
        echo(f"Moved {len(report.deleted)} asset(s) to iCloud's Recently Deleted.",
             fg=typer.colors.GREEN)
    if report.already:
        echo(f"{len(report.already)} were already gone from iCloud.")
    for candidate, why in report.refused:
        echo(f"Left alone: {candidate.rel} — {why}", fg=typer.colors.YELLOW)
    for candidate, why in report.failed:
        echo(f"Failed: {candidate.rel} — {why}", fg=typer.colors.RED, err=True)

    if report.unverified:
        echo("\nSTOPPED: iCloud accepted a deletion but the asset does not read as "
             "deleted, so the run was halted before doing it again.",
             fg=typer.colors.RED, err=True)
        for candidate in report.unverified:
            echo(f"  {candidate.rel} ({candidate.filename})", err=True)
        echo("Check Photos → Recently Deleted for those files. If they are there, "
             "the delete worked and this is a bug worth reporting; if they are not, "
             "they were not deleted.", err=True)
    if report.cancelled:
        echo("Stopped early. Re-run:  icloud-photo-sync icloud-delete --last",
             fg=typer.colors.YELLOW)

    if report.deleted:
        echo(f"\nRecoverable for about {RECOVERY_DAYS} days:")
        echo("  iPhone/iPad : Photos → Albums → Recently Deleted → Recover")
        echo("  macOS Photos: sidebar → Recently Deleted → Recover")
        echo("  Web         : icloud.com/photos → Recently Deleted → Recover")
        if any(c.evidence == idel.EVIDENCE_RETROSPECTIVE for c in report.deleted):
            # Saying "still in the Trash" here would be a lie: these files were
            # already gone before the run started.
            echo("Your local copies were already gone, so Recently Deleted is the "
                 "only copy of these until it expires.", fg=typer.colors.YELLOW)
        else:
            echo("Your local copies are still in the macOS Trash until you empty it "
                 "(Finder → Trash → Put Back).")
    echo(f"Receipt: {receipt_path}")


# --- prompts ------------------------------------------------------------------


def _confirm_by_count(
    count: int, *, phrase: str = "", prompt: Callable[..., str] = typer.prompt,
) -> bool:
    """Make the user type the number, not just a key.

    A y/N is one careless keystroke, and an up-arrow from shell history repeats
    it perfectly. Typing the count means the number on screen was actually read,
    and a plan whose size changed cannot be confirmed by muscle memory.

    ``phrase`` extends that to the evidence class: a retrospective run asks for
    ``delete 500 retrospective``, so a confirmation recalled from a measured run
    cannot be reused, and the weaker class has to be typed out to be accepted.
    """
    if not sys.stdin.isatty():
        raise ICloudSyncError(
            "Refusing to delete from iCloud without an interactive confirmation."
        )
    wanted = f"delete {count} {phrase}".strip() if phrase else str(count)
    answer = prompt(f"Type  {wanted}  to continue" if phrase else
                    f"Type the number of assets to delete ({count}) to continue",
                    default="", show_default=False)
    return str(answer).strip() == wanted


def _confirm_retrospective(count: int, *, prompt: Callable[..., str] = typer.prompt) -> bool:
    return _confirm_by_count(count, phrase="retrospective", prompt=prompt)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --- recovery / retry ---------------------------------------------------------


def run_from_manifest(
    icloud: ICloudDeleteConfig,
    manifest_path: Path | None,
    *,
    session_factory: Callable[..., SessionManager] = SessionManager,
    confirm: Callable[[int], bool] | None = None,
    echo: Callable[..., None] = typer.secho,
    progress: Callable | None = None,
    cancel: Event | None = None,
) -> int:
    """Apply (or re-apply) a manifest written by an earlier review session.

    This is what every failure message points at: a session that ended in an
    expired token, a dropped connection or a Ctrl-C already wrote the manifest,
    so the work is never lost — and anything already recorded as deleted is
    filtered out, so re-running only retries what did not land.
    """
    if manifest_path is None:
        manifest_path = idel.latest_manifest(icloud.manifest_dir, icloud.state_key)
    if manifest_path is None or not Path(manifest_path).exists():
        echo("No deletion manifest found for this Apple ID and folder.",
             fg=typer.colors.RED, err=True)
        return 2

    candidates, meta = idel.read_manifest(Path(manifest_path))
    echo(f"Manifest: {manifest_path}")
    echo(f"  written by : {meta.get('source', '?')} at {meta.get('created_at', '?')}")
    echo(f"  account    : {meta.get('apple_id', '?')}")
    echo(f"  folder     : {meta.get('output_root', '?')}")

    if str(meta.get("output_root")) != str(icloud.app.output_root):
        echo(f"That manifest is for {meta.get('output_root')}, not "
             f"{icloud.app.output_root}. Refusing.", fg=typer.colors.RED, err=True)
        return 2

    evidence = str(meta.get("evidence") or idel.EVIDENCE_MEASURED)
    echo(f"  evidence   : {evidence}")

    armed = arm(icloud, session_factory=session_factory, echo=echo)
    rels = [c.rel for c in candidates]

    # Re-derive from scratch: the manifest is a record of intent, never a licence.
    # Files may have been restored and rows may have changed since it was written.
    # A retrospective manifest has to re-run its whole scan for the same reason —
    # replaying its recorded sizes would quietly re-bless the weaker evidence as
    # a measurement that never happened.
    retrospective = evidence == idel.EVIDENCE_RETROSPECTIVE
    if retrospective:
        scan_result = _scan_for(icloud, meta)
        if scan_result.structural:
            echo(scan_result.structural[0], fg=typer.colors.RED, err=True)
            return 2
        with StateStore(icloud.app.state_db, read_only=True) as state:
            plan = idel.build_plan(
                rels, state=state, output_root=icloud.app.output_root,
                evidence=idel.EVIDENCE_RETROSPECTIVE, retro=scan_result.evidence)
    else:
        with StateStore(icloud.app.state_db, read_only=True) as state:
            plan = idel.build_plan(
                rels, state=state, output_root=icloud.app.output_root,
                sizes={c.rel: c.local_size for c in candidates})

    _report_plan(plan, len(candidates), echo)
    if not plan.candidates:
        echo("Nothing left to delete from iCloud.", fg=typer.colors.GREEN)
        return 0

    # A retrospective plan is spent in confirmed slices rather than refused at
    # the per-run cap — "trash fewer files at a time" is not advice anyone can
    # act on when the files are already gone.
    refusal = (plan.retro_refusal(completed_rows=armed.tracked_assets,
                                  structural=scan_result.structural)
               if retrospective else
               plan.guard_refusal(completed_rows=armed.tracked_assets,
                                  max_delete=icloud.max_delete))
    if refusal:
        echo(refusal, fg=typer.colors.RED, err=True)
        return 1
    if icloud.dry_run:
        echo("Dry run: nothing was deleted from iCloud.", fg=typer.colors.YELLOW)
        return 0

    if retrospective:
        return _apply_in_slices(
            armed, plan, Path(manifest_path), source="icloud-delete",
            confirm=confirm or _confirm_retrospective,
            session_factory=session_factory, echo=echo,
            progress=progress, cancel=cancel)

    if not (confirm or _confirm_by_count)(len(plan.candidates)):
        echo("Not confirmed — nothing was deleted from iCloud.", fg=typer.colors.YELLOW)
        return 0

    return _apply(armed, plan, Path(manifest_path), source="icloud-delete",
                  session_factory=session_factory, echo=echo,
                  progress=progress, cancel=cancel)


def _scan_for(icloud: ICloudDeleteConfig, meta: dict) -> "retro_clean.RetroScan":
    """Re-run a retrospective scan, honouring the envelope the manifest declared."""
    with StateStore(icloud.app.state_db, read_only=True) as state:
        return retro_clean.scan(
            state,
            output_root=icloud.app.output_root,
            logs_dir=icloud.app.logs_dir,
            cache_db=icloud.cache_db,
            max_bytes=int(meta.get("max_bytes") or DEFAULT_CLEAN_MAX_BYTES),
            min_bytes=int(meta.get("min_bytes") or 0),
        )


def run_retro(
    icloud: ICloudDeleteConfig,
    *,
    max_bytes: int = DEFAULT_CLEAN_MAX_BYTES,
    min_bytes: int = 0,
    corroborate_roots: Sequence[Path] = (),
    review: Callable[[ArmedICloud, list[idel.Candidate]], frozenset[str] | None] | None = None,
    no_review: bool = False,
    session_factory: Callable[..., SessionManager] = SessionManager,
    confirm: Callable[[int], bool] | None = None,
    echo: Callable[..., None] = typer.secho,
    progress: Callable | None = None,
    cancel: Event | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Reconcile the tree against the manifest and offer what is missing.

    The evidence here is weaker than the in-session path by construction — see
    :mod:`icloud_photo_sync.retro_clean` — so this asks for more before it acts:
    the whole-run tripwires must all pass, the user looks at iCloud's own
    thumbnails, and the deletion is sliced into separately-confirmed batches.
    """
    armed = arm(icloud, session_factory=session_factory, echo=echo)

    with StateStore(icloud.app.state_db, read_only=True) as state:
        scan_result = retro_clean.scan(
            state, output_root=icloud.app.output_root, logs_dir=icloud.app.logs_dir,
            cache_db=icloud.cache_db, max_bytes=max_bytes, min_bytes=min_bytes,
            corroborate_roots=corroborate_roots)

    _report_scan(scan_result, armed, echo)
    if scan_result.structural:
        for line in scan_result.structural:
            echo(f"\nRefusing: {line}", fg=typer.colors.RED, err=True)
        echo("\nNothing was deleted from iCloud.", fg=typer.colors.RED, err=True)
        return 2
    if not scan_result.missing:
        echo("\nEvery tracked file is present on disk — nothing to delete.",
             fg=typer.colors.GREEN)
        return 0

    def plan_with(reviewed: frozenset[str] | None) -> idel.DeletionPlan:
        evidence = scan_result.evidence
        if reviewed is not None:
            evidence = replace(evidence, reviewed=reviewed)
        with StateStore(icloud.app.state_db, read_only=True) as state:
            return idel.build_plan(
                scan_result.rels, state=state, output_root=icloud.app.output_root,
                evidence=idel.EVIDENCE_RETROSPECTIVE, retro=evidence)

    plan = plan_with(None)
    if plan.candidates and not no_review and not icloud.dry_run:
        look = review or _review_with_thumbnails(session_factory, echo, progress)
        reviewed = look(armed, plan.candidates)
        if reviewed is not None:
            plan = plan_with(reviewed)

    _report_plan(plan, len(scan_result.missing), echo)
    _report_evidence(scan_result, plan, echo)

    stamp = (now or _utc_now)().strftime("%Y%m%d-%H%M%S")
    manifest_path = (icloud.manifest_dir
                     / f"{stamp}-retro-clean-{icloud.state_key}.json")
    idel.write_manifest(manifest_path, plan, {
        "source": "retro-clean", "apple_id": armed.apple_id, "dsid": armed.dsid,
        "output_root": str(icloud.app.output_root), "created_at": stamp,
        "evidence": idel.EVIDENCE_RETROSPECTIVE,
        "verified_present_at": scan_result.verified_present_at,
        "max_bytes": max_bytes, "min_bytes": min_bytes,
        "trash_rounds": [r.isoformat() for r in scan_result.trash_log.rounds],
    })
    echo(f"Manifest: {manifest_path}")

    if not plan.candidates:
        echo("Nothing to delete from iCloud.", fg=typer.colors.GREEN)
        return 0

    refusal = plan.retro_refusal(completed_rows=armed.tracked_assets,
                                 structural=scan_result.structural)
    if refusal:
        echo(refusal, fg=typer.colors.RED, err=True)
        return 1
    if icloud.dry_run:
        echo("Dry run: nothing was deleted from iCloud.", fg=typer.colors.YELLOW)
        echo("Next:  icloud-photo-sync icloud-delete --scan-trashed")
        return 0

    return _apply_in_slices(armed, plan, manifest_path,
                            confirm=confirm or _confirm_retrospective,
                            session_factory=session_factory, echo=echo,
                            progress=progress, cancel=cancel)


def _review_with_thumbnails(session_factory, echo, progress):
    """The real review step, resolved late so tests never open a session."""
    def look(armed: ArmedICloud, candidates: list[idel.Candidate]) -> frozenset[str] | None:
        from . import retro_review
        _service, client = session_factory(armed.config.app).resume()
        return retro_review.review_candidates(
            client, candidates, echo=echo, progress=progress)
    return look


def _apply_in_slices(
    armed: ArmedICloud,
    plan: idel.DeletionPlan,
    manifest_path: Path,
    *,
    confirm: Callable[[int], bool],
    session_factory,
    echo,
    progress,
    cancel,
    source: str = "retro-clean",
) -> int:
    """Delete in confirmed slices of ``max_delete``.

    The per-run cap exists so a large plan costs proportionally more deliberate
    consent, not so it can be raised with a flag. Each slice re-plans against
    the current tree, so files restored by a concurrent ``sync`` drop out, and
    stopping between slices leaves everything already done recorded.
    """
    icloud = armed.config
    size = max(1, icloud.max_delete)
    slices = [plan.candidates[i:i + size]
              for i in range(0, len(plan.candidates), size)]
    worst = 0

    for number, batch in enumerate(slices, start=1):
        if cancel is not None and cancel.is_set():
            echo("\nStopped before the next batch.", fg=typer.colors.YELLOW)
            return worst or 130
        still = [c for c in batch if not (icloud.app.output_root / c.rel).exists()]
        restored = len(batch) - len(still)
        if restored:
            echo(f"\n{restored} file(s) are back on disk and were dropped from "
                 f"batch {number}.", fg=typer.colors.YELLOW)
        if not still:
            continue

        echo(f"\nDeleting from iCloud removes these photos from EVERY device signed "
             f"into {armed.who}.", fg=typer.colors.YELLOW)
        echo(f"They stay recoverable in Recently Deleted for about {RECOVERY_DAYS} days.")
        echo("Your LOCAL copies are already gone — for these files, Recently Deleted "
             "is the only copy that will exist.", fg=typer.colors.YELLOW)
        if len(slices) > 1:
            echo(f"\nBatch {number} of {len(slices)} — {len(still)} of "
                 f"{len(plan.candidates)} assets.")
        if not confirm(len(still)):
            echo("Not confirmed — stopping here.", fg=typer.colors.YELLOW)
            return worst

        code = _apply(armed, idel.DeletionPlan(candidates=still), manifest_path,
                      source=source, session_factory=session_factory,
                      echo=echo, progress=progress, cancel=cancel)
        worst = code or worst
        if code in (2, 3, 5):
            return code            # session or verification failure: stop entirely
    return worst


def explain_receipt(
    icloud: ICloudDeleteConfig,
    receipt_path: Path,
    *,
    session_factory: Callable[..., SessionManager] = SessionManager,
    echo: Callable[..., None] = typer.secho,
) -> int:
    """Read-only: report what iCloud currently thinks of every asset in a receipt.

    The answer to "did that really happen, and is it still recoverable?" without
    offering a restore button — Photos does restores correctly, and a field flip
    from here would risk a half-restored asset.
    """
    entries = [e for e in idel.iter_receipt(Path(receipt_path))
               if e.get("phase") == "result"]
    if not entries:
        echo("That receipt records no results.", fg=typer.colors.YELLOW)
        return 0

    _, client = session_factory(icloud.app).resume()
    found, missing = client.lookup_assets([e["asset_id"] for e in entries])
    for entry in entries:
        remote = found.get(entry["asset_id"])
        if remote is None:
            state = "gone from iCloud entirely"
        elif remote.is_expunged:
            state = "permanently deleted (past the recovery window)"
        elif remote.is_deleted:
            state = "in Recently Deleted — still recoverable"
        else:
            state = "still in your library"
        echo(f"  {entry['rel']}: recorded {entry['status']} → {state}")
    echo(f"\n{len(missing)} of {len(entries)} are no longer readable at all.")
    return 0
