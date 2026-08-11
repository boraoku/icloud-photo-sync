"""The deletion policy: what may be deleted, and what happened when we tried.

No network and no pyicloud — :class:`FakeOps` stands in for the client, so every
rule is exercised directly.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from icloud_photo_sync.icloud_delete import (
    GATE_ALREADY,
    GATE_OK,
    GATE_REFUSE,
    SKIP_ALREADY,
    SKIP_AMBIGUOUS,
    SKIP_NOT_COMPLETED,
    SKIP_NO_ROW,
    SKIP_ON_DISK,
    SKIP_SIZE,
    Candidate,
    DeletionPlan,
    Receipt,
    Skip,
    build_plan,
    execute,
    gate_remote,
    iter_receipt,
    latest_manifest,
    read_manifest,
    write_manifest,
)
from icloud_photo_sync.models import AssetRef
from icloud_photo_sync.state import StateStore

CAPTURE = datetime(2024, 3, 11, 9, 14, tzinfo=timezone.utc)


# --- helpers ------------------------------------------------------------------


def _asset(id="a1", filename="IMG_1.MOV", size=100):
    return AssetRef(id=id, filename=filename, capture_dt=CAPTURE,
                    added_dt=CAPTURE, size=size)


def tracked(store, rel, *, id="a1", filename="IMG_1.MOV", size=100, completed=True):
    store.register(_asset(id=id, filename=filename, size=size), rel)
    if completed:
        store.mark_completed(id, size)
    return rel


@dataclass
class FakeRemote:
    asset_id: str = "a1"
    filename: str | None = "IMG_1.MOV"
    size: int | None = 100
    capture_dt: datetime | None = CAPTURE
    is_deleted: bool = False
    is_expunged: bool = False
    in_shared_library: bool = False


@dataclass
class FakeResult:
    asset_id: str
    ok: bool = True
    error: str | None = None
    already_deleted: bool = False


class FakeOps:
    """Scriptable stand-in for the iCloud client."""

    def __init__(self, *, remotes=None, missing=(), results=None, verified=None,
                 batch_hook=None):
        self.remotes = remotes if remotes is not None else {}
        self.missing = set(missing)
        self.results = results or {}
        self.verified = verified
        self.batch_hook = batch_hook
        self.deleted_batches: list[list[str]] = []

    def lookup_assets(self, asset_ids):
        found = {i: self.remotes.get(i, FakeRemote(asset_id=i))
                 for i in asset_ids if i not in self.missing}
        return found, [i for i in asset_ids if i in self.missing]

    def delete_assets(self, assets):
        ids = [a.asset_id for a in assets]
        self.deleted_batches.append(ids)
        if self.batch_hook is not None:
            self.batch_hook(ids)
        return [self.results.get(i, FakeResult(asset_id=i)) for i in ids]

    def verify_deleted(self, asset_ids):
        if self.verified is None:
            return {i: True for i in asset_ids}
        return {i: self.verified.get(i, True) for i in asset_ids}


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture
def root(tmp_path):
    out = tmp_path / "photos"
    out.mkdir()
    return out


# --- build_plan: the local ladder --------------------------------------------


def test_a_tracked_completed_file_is_eligible(store, root):
    rel = tracked(store, "2024/03/IMG_1.MOV")
    plan = build_plan([rel], state=store, output_root=root, sizes={rel: 100})

    assert plan.skipped == []
    assert [c.asset_id for c in plan.candidates] == ["a1"]
    assert plan.candidates[0].filename == "IMG_1.MOV"


def test_an_untracked_file_is_never_deleted(store, root):
    plan = build_plan(["2019/07/scan.jpg"], state=store, output_root=root,
                      sizes={"2019/07/scan.jpg": 100})

    assert plan.candidates == []
    assert plan.skipped[0].reason == SKIP_NO_ROW


def test_two_assets_claiming_one_path_are_refused(store, root):
    """The real case in this library: nothing can break the tie, so refuse."""
    rel = "2024/01/IMG_3691.HEIC"
    tracked(store, rel, id="a1")
    tracked(store, rel, id="a2")

    plan = build_plan([rel], state=store, output_root=root, sizes={rel: 100})

    assert plan.candidates == []
    assert plan.skipped[0].reason == SKIP_AMBIGUOUS
    assert "a1" in plan.skipped[0].detail and "a2" in plan.skipped[0].detail


def test_paths_differing_only_by_case_are_refused(store, root):
    """APFS folds them to one file, so the mapping is genuinely ambiguous."""
    tracked(store, "2024/03/IMG_1.MOV", id="a1")
    tracked(store, "2024/03/img_1.mov", id="a2")

    plan = build_plan(["2024/03/IMG_1.MOV"], state=store, output_root=root,
                      sizes={"2024/03/IMG_1.MOV": 100})

    assert plan.candidates == []
    assert plan.skipped[0].reason == SKIP_AMBIGUOUS


def test_an_incomplete_download_is_refused(store, root):
    rel = tracked(store, "2024/03/IMG_1.MOV", completed=False)
    plan = build_plan([rel], state=store, output_root=root, sizes={rel: 100})

    assert plan.candidates == []
    assert plan.skipped[0].reason == SKIP_NOT_COMPLETED


def test_a_size_mismatch_is_refused(store, root):
    rel = tracked(store, "2024/03/IMG_1.MOV", size=100)
    plan = build_plan([rel], state=store, output_root=root, sizes={rel: 999})

    assert plan.candidates == []
    assert plan.skipped[0].reason == SKIP_SIZE
    assert "100" in plan.skipped[0].detail and "999" in plan.skipped[0].detail


def test_an_unknown_local_size_is_refused(store, root):
    rel = tracked(store, "2024/03/IMG_1.MOV")
    plan = build_plan([rel], state=store, output_root=root, sizes={})

    assert plan.candidates == []
    assert plan.skipped[0].reason == SKIP_SIZE


def test_a_file_back_on_disk_is_refused(store, root):
    """Put Back in Finder must undo the intent, not just the move."""
    rel = tracked(store, "2024/03/IMG_1.MOV")
    (root / "2024/03").mkdir(parents=True)
    (root / rel).write_bytes(b"x" * 100)

    plan = build_plan([rel], state=store, output_root=root, sizes={rel: 100})

    assert plan.candidates == []
    assert plan.skipped[0].reason == SKIP_ON_DISK


def test_an_already_deleted_asset_is_not_offered_twice(store, root):
    rel = tracked(store, "2024/03/IMG_1.MOV")
    store.record_remote_deletion(
        asset_id="a1", dest_path=rel, filename="IMG_1.MOV", capture_dt=None,
        expected_size=100, receipt_path=None, verified_at="now")

    plan = build_plan([rel], state=store, output_root=root, sizes={rel: 100})

    assert plan.candidates == []
    assert plan.skipped[0].reason == SKIP_ALREADY


def test_unicode_normalisation_still_matches(store, root):
    stored = "2024/03/café.MOV"       # NFD, as macOS often writes it
    tracked(store, stored)
    asked = "2024/03/café.MOV"         # NFC

    plan = build_plan([asked], state=store, output_root=root, sizes={asked: 100})
    assert [c.asset_id for c in plan.candidates] == ["a1"]


def test_a_disambiguated_name_is_matched_by_path_not_filename(store, root):
    rel = tracked(store, "2011/06/IMG_18-1.JPG", filename="IMG_18.JPG")
    plan = build_plan([rel], state=store, output_root=root, sizes={rel: 100})

    assert plan.candidates[0].rel == "2011/06/IMG_18-1.JPG"
    assert plan.candidates[0].filename == "IMG_18.JPG"   # iCloud's name, not the disk's


# --- blast radius -------------------------------------------------------------


def test_guard_refuses_an_implausibly_large_plan():
    plan = DeletionPlan(candidates=[
        Candidate(rel=f"f{i}", asset_id=f"a{i}", filename="x", capture_dt=None,
                  expected_size=1, local_size=1) for i in range(300)])

    assert plan.guard_refusal(completed_rows=25691, max_delete=500) is None
    assert "100-per-run" in plan.guard_refusal(completed_rows=25691, max_delete=100)
    # 300 of a 400-asset library is not a cleanup, whatever the per-run limit says
    assert "25%" in plan.guard_refusal(completed_rows=400, max_delete=500)


# --- gate_remote: what iCloud says now ----------------------------------------


def _candidate(**kw):
    base = dict(rel="2024/03/IMG_1.MOV", asset_id="a1", filename="IMG_1.MOV",
                capture_dt=CAPTURE.isoformat(), expected_size=100, local_size=100)
    base.update(kw)
    return Candidate(**base)


def test_gate_passes_when_everything_agrees():
    assert gate_remote(_candidate(), FakeRemote())[0] == GATE_OK


def test_gate_refuses_a_filename_that_disagrees():
    verdict, why = gate_remote(_candidate(), FakeRemote(filename="SOMETHING_ELSE.MOV"))
    assert verdict == GATE_REFUSE and "SOMETHING_ELSE.MOV" in why


def test_gate_refuses_a_size_that_disagrees():
    verdict, why = gate_remote(_candidate(), FakeRemote(size=999))
    assert verdict == GATE_REFUSE and "999" in why


def test_gate_refuses_a_capture_date_that_disagrees():
    drifted = FakeRemote(capture_dt=CAPTURE + timedelta(hours=3))
    assert gate_remote(_candidate(), drifted)[0] == GATE_REFUSE


def test_gate_tolerates_sub_second_capture_drift():
    close = FakeRemote(capture_dt=CAPTURE + timedelta(milliseconds=400))
    assert gate_remote(_candidate(), close)[0] == GATE_OK


def test_gate_refuses_a_shared_library_asset():
    verdict, why = gate_remote(_candidate(), FakeRemote(in_shared_library=True))
    assert verdict == GATE_REFUSE and "shared library" in why


def test_gate_refuses_an_expunged_asset():
    assert gate_remote(_candidate(), FakeRemote(is_expunged=True))[0] == GATE_REFUSE


def test_gate_treats_an_already_trashed_asset_as_done_not_failed():
    assert gate_remote(_candidate(), FakeRemote(is_deleted=True))[0] == GATE_ALREADY


# --- execute ------------------------------------------------------------------


def _plan(n=3):
    return DeletionPlan(candidates=[
        Candidate(rel=f"2024/03/IMG_{i}.MOV", asset_id=f"a{i}", filename=f"IMG_{i}.MOV",
                  capture_dt=CAPTURE.isoformat(), expected_size=100, local_size=100)
        for i in range(n)])


def test_execute_deletes_and_verifies():
    ops = FakeOps(remotes={f"a{i}": FakeRemote(asset_id=f"a{i}", filename=f"IMG_{i}.MOV")
                           for i in range(3)})
    report = execute(_plan(), ops)

    assert [c.asset_id for c in report.deleted] == ["a0", "a1", "a2"]
    assert report.exit_code() == 0


def test_execute_stops_when_a_delete_cannot_be_verified():
    """The API said fine; the asset says otherwise. Stop, do not continue."""
    remotes = {f"a{i}": FakeRemote(asset_id=f"a{i}", filename=f"IMG_{i}.MOV")
               for i in range(6)}
    ops = FakeOps(remotes=remotes, verified={"a0": False})
    report = execute(_plan(6), ops, batch_size=2)

    assert [c.asset_id for c in report.unverified] == ["a0"]
    assert len(ops.deleted_batches) == 1          # never went on to batch two
    assert report.exit_code() == 5                # its own exit code


def test_execute_reports_a_per_asset_failure_without_stopping():
    remotes = {f"a{i}": FakeRemote(asset_id=f"a{i}", filename=f"IMG_{i}.MOV")
               for i in range(4)}
    ops = FakeOps(remotes=remotes,
                  results={"a1": FakeResult("a1", ok=False, error="CONFLICT")})
    report = execute(_plan(4), ops, batch_size=2)

    assert [c.asset_id for c, _ in report.failed] == ["a1"]
    assert len(report.deleted) == 3               # the rest still went through
    assert report.exit_code() == 1


def test_execute_counts_a_vanished_asset_as_already_gone():
    ops = FakeOps(remotes={"a0": FakeRemote(asset_id="a0", filename="IMG_0.MOV")},
                  missing=["a1", "a2"])
    report = execute(_plan(), ops)

    assert len(report.deleted) == 1
    assert [c.asset_id for c in report.already] == ["a1", "a2"]
    assert report.exit_code() == 0


def test_execute_refuses_assets_the_gate_rejects():
    remotes = {"a0": FakeRemote(asset_id="a0", filename="IMG_0.MOV"),
               "a1": FakeRemote(asset_id="a1", filename="WRONG.MOV"),
               "a2": FakeRemote(asset_id="a2", filename="IMG_2.MOV")}
    ops = FakeOps(remotes=remotes)
    report = execute(_plan(), ops)

    assert [c.asset_id for c, _ in report.refused] == ["a1"]
    assert ops.deleted_batches == [["a0", "a2"]]   # the refused one never sent
    assert len(report.deleted) == 2


def test_execute_cancels_between_batches_never_mid_batch():
    cancel = Event()
    remotes = {f"a{i}": FakeRemote(asset_id=f"a{i}", filename=f"IMG_{i}.MOV")
               for i in range(6)}
    ops = FakeOps(remotes=remotes, batch_hook=lambda ids: cancel.set())

    report = execute(_plan(6), ops, batch_size=2, cancel=cancel)

    assert report.cancelled is True
    assert len(ops.deleted_batches) == 1           # the in-flight batch finished
    assert len(report.deleted) == 2                # and was fully accounted for
    assert report.exit_code() == 130


def test_execute_reports_progress_per_batch():
    seen = []
    remotes = {f"a{i}": FakeRemote(asset_id=f"a{i}", filename=f"IMG_{i}.MOV")
               for i in range(5)}
    execute(_plan(5), FakeOps(remotes=remotes), batch_size=2,
            on_progress=seen.append)

    assert seen == [2, 2, 1]


def test_execute_resolves_every_candidate_exactly_once():
    """The receipt callback is how deletions get recorded; it must not miss one."""
    resolved = []
    remotes = {"a0": FakeRemote(asset_id="a0", filename="IMG_0.MOV"),
               "a1": FakeRemote(asset_id="a1", filename="WRONG.MOV"),
               "a2": FakeRemote(asset_id="a2", filename="IMG_2.MOV", is_deleted=True)}
    execute(_plan(), FakeOps(remotes=remotes),
            on_resolved=lambda c, status, detail: resolved.append((c.asset_id, status)))

    assert sorted(resolved) == [("a0", "deleted"), ("a1", "refused"), ("a2", "already")]


def test_execute_of_an_empty_plan_does_nothing():
    ops = FakeOps()
    report = execute(DeletionPlan(), ops)
    assert ops.deleted_batches == [] and report.exit_code() == 0


# --- audit trail --------------------------------------------------------------


def test_manifest_round_trip(tmp_path):
    plan = _plan(2)
    plan.skipped.append(Skip("x.jpg", SKIP_NO_ROW))
    path = write_manifest(tmp_path / "m.json", plan,
                          {"apple_id": "me@example.com", "output_root": "/photos"})

    candidates, meta = read_manifest(path)
    assert [c.asset_id for c in candidates] == ["a0", "a1"]
    assert meta["apple_id"] == "me@example.com"
    assert json.loads(path.read_text())["skipped"][0]["reason"] == SKIP_NO_ROW


def test_latest_manifest_picks_the_newest_for_this_account(tmp_path):
    (tmp_path / "20260101-000000-local-clean-KEY.json").write_text("{}")
    (tmp_path / "20260202-000000-video-clean-KEY.json").write_text("{}")
    (tmp_path / "20260303-000000-video-clean-OTHER.json").write_text("{}")

    newest = latest_manifest(tmp_path, "KEY")
    assert newest.name == "20260202-000000-video-clean-KEY.json"


def test_latest_manifest_is_none_when_there_are_none(tmp_path):
    assert latest_manifest(tmp_path, "KEY") is None


def test_receipt_records_intent_before_result(tmp_path):
    candidate = _plan(1).candidates[0]
    with Receipt(tmp_path / "r.jsonl") as receipt:
        receipt.intent(candidate)
        receipt.result(candidate, "deleted")
        receipt.trailer(deleted=1, failed=0)

    phases = [e["phase"] for e in iter_receipt(tmp_path / "r.jsonl")]
    assert phases == ["intent", "result", "summary"]


def test_receipt_survives_a_hard_kill(tmp_path):
    """Each line is fsynced, so an unclosed receipt is still readable."""
    receipt = Receipt(tmp_path / "r.jsonl")
    receipt.intent(_plan(1).candidates[0])
    # deliberately not closed
    assert [e["phase"] for e in iter_receipt(tmp_path / "r.jsonl")] == ["intent"]


def test_fraction_guard_does_not_fire_on_a_small_library():
    """Clearing half of a 20-photo folder is ordinary; half of 25,000 is a bug."""
    plan = DeletionPlan(candidates=[
        Candidate(rel=f"f{i}", asset_id=f"a{i}", filename="x", capture_dt=None,
                  expected_size=1, local_size=1) for i in range(10)])

    assert plan.guard_refusal(completed_rows=12, max_delete=500) is None
    assert plan.guard_refusal(completed_rows=0, max_delete=500) is None
