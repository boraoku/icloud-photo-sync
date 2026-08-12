"""Deciding which trashed files may be deleted from iCloud — and proving it happened.

Pure policy: no ``pyicloud``, no ``typer``, no network. The iCloud mechanics live
in :mod:`icloud_photo_sync.icloud_client` behind a small protocol
(:class:`AssetOps`), so every rule here is testable without an account.

The governing principle is that **refusing is cheap and deleting is not**. A file
this tool declines to delete costs one re-download on the next ``sync``; a file it
deletes wrongly costs a photo. So a trashed file has to earn its way through two
independent ladders before anything happens to it:

* :func:`build_plan` — local evidence. Exactly one manifest row claims the path,
  that row is a completed download, its recorded size matches what the file
  actually weighed the moment before it was trashed, and the file really is gone
  from disk. Anything else is skipped and reported, never guessed at.
* :func:`gate_remote` — what iCloud says right now, seconds before the modify:
  the filename, original size and capture date on the live records must still
  agree with the manifest. This is what catches a manifest row that has drifted.

Names are never matched on. Thousands of assets in a real library share a
filename, so a filename match is not evidence of anything.

There are two classes of evidence, and the difference is not cosmetic:

``EVIDENCE_MEASURED``
    The file was ``stat()``-ed microseconds before Finder took it, so the size
    in the plan is an independent measurement of the bytes that left the disk.

``EVIDENCE_RETROSPECTIVE``
    The file was already gone when we looked, so no size could be measured. The
    only size available is the manifest's own ``expected_size``, and comparing
    that with itself proves nothing. :func:`build_plan` therefore *refuses to
    accept a ``sizes`` mapping at all* in this mode — the tautology is not
    discouraged, it is unrepresentable. What stands in for the measurement is
    assembled by :mod:`icloud_photo_sync.retro_clean` and arrives as
    :class:`RetroEvidence`. The class travels into the manifest, the receipt and
    the confirmation prompt, so nothing downstream can mistake one for the other.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from .logutil import get_logger
from .state import StateStore, fold_dest

logger = get_logger(__name__)

DEFAULT_BATCH_SIZE = 25
DEFAULT_MAX_DELETE = 500          # per run, before --max-delete
MAX_DELETE_CEILING = 2000         # --max-delete cannot exceed this
LIBRARY_FRACTION_LIMIT = 0.25     # of the manifest's completed rows
# Below this the ratio says nothing: clearing half of a 20-photo folder is a
# perfectly ordinary afternoon, while half of a 25,000-photo library is a bug.
FRACTION_GUARD_MIN_LIBRARY = 200
CAPTURE_TOLERANCE_SECONDS = 1.0

# How well we know that this file left the disk on purpose. See the module
# docstring — these are not interchangeable.
EVIDENCE_MEASURED = "measured"            # stat()'d immediately before trashing
EVIDENCE_RETROSPECTIVE = "retrospective"  # already gone; no size was ever measured
EVIDENCE_CLASSES = (EVIDENCE_MEASURED, EVIDENCE_RETROSPECTIVE)

# Why a trashed file was left alone. These are shown to the user verbatim.
SKIP_NO_ROW = "not tracked by this folder's sync manifest"
SKIP_AMBIGUOUS = "more than one iCloud asset claims this path"
SKIP_NOT_COMPLETED = "the manifest row is not a completed download"
SKIP_SIZE = "the file's size did not match the manifest"
SKIP_ON_DISK = "the file is back on disk"
SKIP_ALREADY = "already deleted from iCloud"

# Retrospective-only refusals.
SKIP_OUT_OF_ENVELOPE = "no clean command could have offered this file"
SKIP_NOT_CLASSIFIED = "the classifier never reached this file"
SKIP_STILL_CLASSIFIED = "the classification cache still holds this file"
SKIP_UNVERIFIED_ROW = "no completed pass ever saw this file on disk"
SKIP_ELSEWHERE = "an identical copy exists elsewhere"
SKIP_NOT_REVIEWED = "not selected during the thumbnail review"

# gate_remote verdicts.
GATE_OK = "ok"
GATE_ALREADY = "already"
GATE_REFUSE = "refuse"


@dataclass(frozen=True)
class Candidate:
    """One trashed file that has passed every local check.

    ``evidence`` and ``corroboration`` default so that manifests written before
    they existed still load through :func:`read_manifest`.
    """

    rel: str
    asset_id: str
    filename: str
    capture_dt: str | None
    expected_size: int | None
    local_size: int | None
    evidence: str = EVIDENCE_MEASURED
    corroboration: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetroEvidence:
    """What stands in for the size measurement when the file is already gone.

    Assembled by :mod:`icloud_photo_sync.retro_clean`, which owns all the I/O.
    Everything here is per-``rel`` so :func:`build_plan` stays a pure lookup.
    """

    verified_present_at: str
    """Timestamp of a pass that provably saw every completed row on disk."""

    envelopes: Mapping[str, str]
    """rel → the clean command whose scan could have offered it."""

    vetoes: Mapping[str, "Skip"]
    """rel → a ready-made :class:`Skip` that disqualifies it outright."""

    corroboration: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    reviewed: frozenset[str] | None = None
    """What the user ticked in the thumbnail review; None if it did not run."""


@dataclass(frozen=True)
class Skip:
    rel: str
    reason: str
    detail: str = ""


@dataclass
class DeletionPlan:
    candidates: list[Candidate] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.candidates)

    @property
    def asset_ids(self) -> list[str]:
        return [c.asset_id for c in self.candidates]

    def guard_refusal(self, *, completed_rows: int, max_delete: int) -> str | None:
        """Why this plan is too big to be believable, or None.

        Not a safety belt for the user — a tripwire for us. A cleanup session
        that matches a quarter of the library means the matching is wrong, and
        the right response to that is to stop, not to delete carefully.
        """
        if len(self.candidates) > max_delete:
            return (f"{len(self.candidates)} assets exceeds the {max_delete}-per-run "
                    "limit. Re-run with --max-delete to raise it, or trash fewer "
                    "files at a time.")
        return self._fraction_refusal(completed_rows)

    def _fraction_refusal(self, completed_rows: int) -> str | None:
        if completed_rows >= FRACTION_GUARD_MIN_LIBRARY \
                and len(self.candidates) > completed_rows * LIBRARY_FRACTION_LIMIT:
            return (f"{len(self.candidates)} assets is more than "
                    f"{LIBRARY_FRACTION_LIMIT:.0%} of the {completed_rows} tracked for "
                    "this folder. That is not a cleanup — something is wrong with the "
                    "matching. Nothing has been deleted.")
        return None

    def retro_refusal(
        self, *, completed_rows: int, structural: Sequence[str],
        max_delete: int | None = None,
    ) -> str | None:
        """Whole-run tripwires for a retrospective plan.

        ``structural`` comes from :func:`icloud_photo_sync.retro_clean.scan` and
        holds conditions that invalidate the *entire* reconstruction rather than
        any one file — an unmounted volume, a manifest that never completed a
        pass, a gap in the logs, a missing file that no clean command could have
        shown. When the premise is broken the answer is to stop, because a
        carefully-filtered subset of a wrong premise is still wrong.

        A retrospective run is confirmed once, for everything, so this ceiling is
        what bounds it — not the measured path's per-run cap. ``max_delete``
        lowers it when the user asks; unset, the hard ceiling applies. Splitting
        the consent instead was worse on both counts: it added a session resume
        per slice, and a prompt answered three times is a prompt that stops being
        read.
        """
        if structural:
            return structural[0]
        ceiling = MAX_DELETE_CEILING if max_delete is None \
            else min(MAX_DELETE_CEILING, max_delete)
        if len(self.candidates) > ceiling:
            return (f"{len(self.candidates)} assets exceeds the {ceiling}-asset "
                    "ceiling for a single run. Narrow --max-size, raise "
                    "--max-delete, or clean up in stages.")
        return self._fraction_refusal(completed_rows)


class AssetOps(Protocol):
    """The slice of :class:`~icloud_photo_sync.icloud_client.ICloudClient` used here."""

    def lookup_assets(self, asset_ids: Sequence[str]): ...
    def delete_assets(self, assets: Sequence[object]): ...
    def verify_deleted(self, asset_ids: Sequence[str]) -> dict[str, bool]: ...


# --- local evidence -----------------------------------------------------------


def _check_evidence(
    evidence: str, sizes: Mapping[str, int | None] | None, retro: RetroEvidence | None,
) -> None:
    """Reject any combination that would let weak evidence pass as strong."""
    if evidence not in EVIDENCE_CLASSES:
        raise ValueError(f"unknown evidence class {evidence!r}")
    if evidence == EVIDENCE_MEASURED:
        if sizes is None:
            raise ValueError("measured evidence needs the sizes stat()'d before trashing")
        if retro is not None:
            raise ValueError("retro evidence was supplied for a measured plan")
    else:
        if sizes is not None:
            # The only size a caller could have is the manifest's own
            # expected_size, and checking that against itself always passes.
            raise ValueError(
                "retrospective evidence cannot take sizes: the file was already "
                "gone, so no size was measured and comparing the manifest with "
                "itself proves nothing")
        if retro is None:
            raise ValueError("retrospective evidence needs a RetroEvidence")


def build_plan(
    rels: Sequence[str],
    *,
    state: StateStore,
    output_root: Path,
    sizes: Mapping[str, int | None] | None = None,
    evidence: str = EVIDENCE_MEASURED,
    retro: RetroEvidence | None = None,
) -> DeletionPlan:
    """Decide which of ``rels`` may be offered for deletion.

    ``sizes`` are the file sizes measured immediately before trashing — not at
    scan time, which for a long ``local-clean`` session can be hours stale. It is
    required for :data:`EVIDENCE_MEASURED` and forbidden for
    :data:`EVIDENCE_RETROSPECTIVE`, where ``retro`` takes its place.
    """
    _check_evidence(evidence, sizes, retro)
    plan = DeletionPlan()
    collisions = state.colliding_dest_paths()
    rows_by_rel: dict[str, list] = {}

    for rel in rels:
        if (output_root / rel).exists():
            plan.skipped.append(Skip(rel, SKIP_ON_DISK))
            continue
        rows = state.rows_for_dest(rel)
        if not rows:
            plan.skipped.append(Skip(rel, SKIP_NO_ROW))
            continue
        if len(rows) > 1 or fold_dest(rel) in collisions:
            claimants = ", ".join(sorted(r["id"] for r in rows))
            plan.skipped.append(Skip(rel, SKIP_AMBIGUOUS, claimants))
            continue
        rows_by_rel[rel] = rows

    already = state.remote_deleted_ids(
        rows[0]["id"] for rows in rows_by_rel.values()
    )

    for rel, rows in rows_by_rel.items():
        row = rows[0]
        if row["status"] != "completed":
            plan.skipped.append(Skip(rel, SKIP_NOT_COMPLETED, str(row["status"])))
            continue
        if row["id"] in already:
            plan.skipped.append(Skip(rel, SKIP_ALREADY))
            continue

        if evidence == EVIDENCE_RETROSPECTIVE:
            skip = _retro_skip(rel, row, retro)
            if skip is not None:
                plan.skipped.append(skip)
                continue
            # bytes_done is what this machine actually wrote to disk, which is a
            # different number from iCloud's expected_size even though the two
            # usually agree — never a copy of the value we are checking against.
            local_size = row["bytes_done"]
            corroboration = tuple(retro.corroboration.get(rel, ()))
        else:
            local_size = sizes.get(rel)
            if row["expected_size"] is None or local_size is None \
                    or int(row["expected_size"]) != int(local_size):
                plan.skipped.append(Skip(
                    rel, SKIP_SIZE,
                    f"manifest {row['expected_size']} vs file {local_size}"))
                continue
            corroboration = ()

        plan.candidates.append(Candidate(
            rel=rel, asset_id=row["id"], filename=row["filename"],
            capture_dt=row["capture_dt"],
            expected_size=None if row["expected_size"] is None
            else int(row["expected_size"]),
            local_size=None if local_size is None else int(local_size),
            evidence=evidence, corroboration=corroboration,
        ))
    return plan


def _retro_skip(rel: str, row, retro: RetroEvidence) -> Skip | None:
    """The rungs that replace the size check when the file is already gone."""
    updated_at = row["updated_at"]
    if not updated_at or str(updated_at) > retro.verified_present_at:
        # The row changed after the last pass that vouched for the whole tree, so
        # nothing ever confirmed this particular file reached the disk.
        return Skip(rel, SKIP_UNVERIFIED_ROW, str(updated_at or "never"))
    veto = retro.vetoes.get(rel)
    if veto is not None:
        return veto
    if rel not in retro.envelopes:
        return Skip(rel, SKIP_OUT_OF_ENVELOPE)
    if retro.reviewed is not None and rel not in retro.reviewed:
        return Skip(rel, SKIP_NOT_REVIEWED)
    return None


# --- what iCloud says right now -----------------------------------------------


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def gate_remote(candidate: Candidate, remote) -> tuple[str, str]:
    """Compare the live records against the manifest. Returns ``(verdict, why)``.

    Runs seconds before the modify, on records fetched in the same breath — the
    change tag quoted in the delete has to be that fresh, and so does this
    evidence.
    """
    if remote.in_shared_library:
        return GATE_REFUSE, "the asset lives in a shared library"
    if remote.is_expunged:
        return GATE_REFUSE, "the asset is already permanently deleted in iCloud"
    if remote.is_deleted:
        return GATE_ALREADY, "already in iCloud's Recently Deleted"
    if remote.filename and candidate.filename and remote.filename != candidate.filename:
        return GATE_REFUSE, (f"iCloud calls this asset {remote.filename!r}, the "
                             f"manifest says {candidate.filename!r}")
    if remote.size is not None and candidate.expected_size is not None \
            and int(remote.size) != int(candidate.expected_size):
        return GATE_REFUSE, (f"iCloud reports {remote.size} bytes, the manifest "
                             f"and the trashed file say {candidate.expected_size}")
    expected_dt = _parse_dt(candidate.capture_dt)
    if expected_dt is not None and remote.capture_dt is not None:
        drift = abs((remote.capture_dt - expected_dt).total_seconds())
        if drift > CAPTURE_TOLERANCE_SECONDS:
            return GATE_REFUSE, (f"capture date differs by {drift:.0f}s from the "
                                 "manifest")
    return GATE_OK, ""


# --- doing it -----------------------------------------------------------------


@dataclass
class DeleteReport:
    deleted: list[Candidate] = field(default_factory=list)
    already: list[Candidate] = field(default_factory=list)
    failed: list[tuple[Candidate, str]] = field(default_factory=list)
    refused: list[tuple[Candidate, str]] = field(default_factory=list)
    unverified: list[Candidate] = field(default_factory=list)
    cancelled: bool = False

    def exit_code(self) -> int:
        if self.unverified:
            return 5          # distinct: the outcome is unknown, not merely failed
        if self.cancelled:
            return 130
        return 1 if self.failed else 0


def execute(
    plan: DeletionPlan,
    ops: AssetOps,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    cancel: Event | None = None,
    on_progress: Callable[[int], None] | None = None,
    on_resolved: Callable[[Candidate, str, str], None] | None = None,
) -> DeleteReport:
    """Delete the plan's candidates, batch by batch, verifying each batch.

    Stops immediately if a delete cannot be verified: an unverified delete means
    our model of the API is wrong, and repeating an operation whose effect we
    cannot read would multiply an unknown amount of damage.

    Cancellation is honoured *between* batches only. A batch is one request;
    abandoning it in flight would leave its outcome unknowable, which is the one
    state this design refuses to end up in.
    """
    report = DeleteReport()
    by_id = {c.asset_id: c for c in plan.candidates}

    def resolve(candidate: Candidate, status: str, detail: str = "") -> None:
        if on_resolved is not None:
            on_resolved(candidate, status, detail)

    for start in range(0, len(plan.candidates), batch_size):
        if cancel is not None and cancel.is_set():
            report.cancelled = True
            break
        batch = plan.candidates[start:start + batch_size]

        found, missing = ops.lookup_assets([c.asset_id for c in batch])
        for asset_id in missing:
            candidate = by_id[asset_id]
            report.already.append(candidate)
            resolve(candidate, "already", "no longer in iCloud")

        approved, remotes = [], []
        for candidate in batch:
            remote = found.get(candidate.asset_id)
            if remote is None:
                continue                      # counted as missing above
            verdict, why = gate_remote(candidate, remote)
            if verdict == GATE_ALREADY:
                report.already.append(candidate)
                resolve(candidate, "already", why)
            elif verdict == GATE_REFUSE:
                report.refused.append((candidate, why))
                resolve(candidate, "refused", why)
                logger.warning("refusing to delete %s: %s", candidate.rel, why)
            else:
                approved.append(candidate)
                remotes.append(remote)

        if not approved:
            if on_progress is not None:
                on_progress(len(batch))
            continue

        results = {r.asset_id: r for r in ops.delete_assets(remotes)}
        verified = ops.verify_deleted([c.asset_id for c in approved])

        for candidate in approved:
            result = results.get(candidate.asset_id)
            confirmed = verified.get(candidate.asset_id, False)
            if result is not None and result.already_deleted and confirmed:
                report.already.append(candidate)
                resolve(candidate, "already", "already gone when we asked")
            elif result is not None and result.ok and confirmed:
                report.deleted.append(candidate)
                resolve(candidate, "deleted", "")
            elif result is not None and not result.ok:
                report.failed.append((candidate, result.error or "unknown error"))
                resolve(candidate, "failed", result.error or "")
            else:
                # Accepted by the API but not visibly deleted: stop the run.
                report.unverified.append(candidate)
                resolve(candidate, "unverified", "iCloud accepted the delete but "
                                                 "the asset does not read as deleted")

        if on_progress is not None:
            on_progress(len(batch))
        if report.unverified:
            logger.error("Stopping: a deletion could not be verified.")
            break

    return report


# --- audit trail --------------------------------------------------------------


def _fsync_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def write_manifest(path: Path, plan: DeletionPlan, meta: Mapping[str, object]) -> Path:
    """Record what is *about* to be deleted, durably, before anything is.

    Written first on purpose: an expired session, a dropped network or a Ctrl-C
    then leaves an artifact the user can act on instead of a mystery.
    """
    payload = {
        "version": 2,
        "kind": "icloud-deletion-plan",
        **dict(meta),
        "candidates": [vars(c) for c in plan.candidates],
        "skipped": [vars(s) for s in plan.skipped],
    }
    _fsync_write(path, json.dumps(payload, indent=2, default=str))
    return path


def read_manifest(path: Path) -> tuple[list[Candidate], dict]:
    """Load a manifest. Version 1 predates evidence classes and reads as measured."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    candidates = []
    for entry in data.get("candidates", []):
        fields = dict(entry)
        # JSON has no tuples, and a list field would make Candidate unhashable.
        fields["corroboration"] = tuple(fields.get("corroboration") or ())
        candidates.append(Candidate(**fields))
    meta = {k: v for k, v in data.items() if k not in ("candidates", "skipped")}
    return candidates, meta


def latest_manifest(manifest_dir: Path, state_key: str) -> Path | None:
    """Newest manifest for this account + folder, for ``--last``."""
    manifests = sorted(Path(manifest_dir).glob(f"*-{state_key}.json"))
    return manifests[-1] if manifests else None


class Receipt:
    """Append-only record of every asset touched, flushed as it happens.

    Each asset gets an intent line before the request and a result line after,
    so even a hard kill leaves evidence of what may have been affected.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a", encoding="utf-8")

    def write(self, **entry) -> None:
        self._handle.write(json.dumps(entry, default=str) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def intent(self, candidate: Candidate) -> None:
        self.write(phase="intent", rel=candidate.rel, asset_id=candidate.asset_id,
                   filename=candidate.filename, size=candidate.expected_size,
                   capture_dt=candidate.capture_dt, evidence=candidate.evidence,
                   corroboration=list(candidate.corroboration))

    def result(self, candidate: Candidate, status: str, detail: str = "") -> None:
        self.write(phase="result", rel=candidate.rel, asset_id=candidate.asset_id,
                   status=status, detail=detail)

    def trailer(self, **summary) -> None:
        self.write(phase="summary", **summary)

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "Receipt":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def iter_receipt(path: Path) -> Iterable[dict]:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)
