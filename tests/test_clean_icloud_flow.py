"""The terminal flow: arm → review → plan → confirm → delete → report.

Every prompt, print and session is injected, so nothing here logs in or reaches
the network. What is under test is the sequencing — in particular that nothing
irreversible happens before the manifest is written and the count is typed back.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
import typer

from icloud_photo_sync import clean_icloud
from icloud_photo_sync.config import ICloudDeleteConfig
from icloud_photo_sync.errors import (
    ICloudSyncError,
    ManifestMismatchError,
    ManifestMissingError,
    SessionExpiredError,
)
from icloud_photo_sync.models import AssetRef
from icloud_photo_sync.review import TrashOutcome
from icloud_photo_sync.state import StateStore

CAPTURE = datetime(2024, 3, 11, 9, 14, tzinfo=timezone.utc)


# --- fakes --------------------------------------------------------------------


@dataclass
class FakeRemote:
    asset_id: str
    filename: str
    size: int = 100
    capture_dt: datetime = CAPTURE
    is_deleted: bool = False
    is_expunged: bool = False
    in_shared_library: bool = False


@dataclass
class FakeResult:
    asset_id: str
    ok: bool = True
    error: str | None = None
    already_deleted: bool = False


class FakeClient:
    def __init__(self, *, remotes=None, can_delete=True, results=None, verified=None):
        self.remotes = remotes or {}
        self.can_delete = can_delete
        self.results = results or {}
        self.verified = verified
        self.deleted: list[str] = []

    def supports_delete(self):
        return self.can_delete

    def lookup_assets(self, asset_ids):
        return ({i: self.remotes[i] for i in asset_ids if i in self.remotes},
                [i for i in asset_ids if i not in self.remotes])

    def delete_assets(self, assets):
        ids = [a.asset_id for a in assets]
        self.deleted.extend(ids)
        return [self.results.get(i, FakeResult(i)) for i in ids]

    def verify_deleted(self, asset_ids):
        return {i: (self.verified or {}).get(i, True) for i in asset_ids}


class FakeService:
    data = {"dsInfo": {"dsid": "4821"}}
    account_name = "Test Person"


class FakeSession:
    """Stands in for SessionManager; raises whatever the test scripts."""

    client = None
    raises = None

    def __init__(self, app_config):
        self.app_config = app_config

    def resume(self):
        if type(self).raises is not None:
            raise type(self).raises
        return FakeService(), type(self).client


@pytest.fixture(autouse=True)
def _reset_fake_session():
    FakeSession.raises = None
    yield
    FakeSession.raises = None


def make_config(tmp_path, **overrides):
    return ICloudDeleteConfig.create("me@example.com", tmp_path / "photos",
                                     config_root=tmp_path / "cfg", **overrides)


def seed_manifest(config, rels_and_ids, *, size=100):
    (config.app.output_root).mkdir(parents=True, exist_ok=True)
    with StateStore(config.app.state_db) as state:
        for rel, asset_id in rels_and_ids:
            state.register(AssetRef(id=asset_id, filename=rel.split("/")[-1],
                                    capture_dt=CAPTURE, added_dt=CAPTURE, size=size),
                           rel)
            state.mark_completed(asset_id, size)


def outcome_for(rels, *, size=100):
    return TrashOutcome(moved=list(rels), icloud=list(rels),
                        sizes={rel: size for rel in rels})


def collect(lines):
    return lambda msg="", **kw: lines.append(str(msg))


# --- arm ----------------------------------------------------------------------


def test_arm_reports_the_account_and_library_size(tmp_path):
    config = make_config(tmp_path)
    seed_manifest(config, [("2024/03/a.MOV", "a1")])
    FakeSession.client = FakeClient()
    lines = []

    armed = clean_icloud.arm(config, session_factory=FakeSession, echo=collect(lines))

    assert armed.dsid == "4821"
    assert armed.tracked_assets == 1
    assert any("armed for Test Person <me@example.com>" in ln for ln in lines)


def test_arm_refuses_when_there_is_no_manifest_for_this_folder(tmp_path):
    """An auto-created empty DB would silently match nothing; say it out loud."""
    config = make_config(tmp_path)
    FakeSession.client = FakeClient()

    with pytest.raises(ManifestMissingError):
        clean_icloud.arm(config, session_factory=FakeSession, echo=lambda *a, **k: None)


def test_arm_refuses_a_build_that_cannot_delete(tmp_path):
    config = make_config(tmp_path)
    seed_manifest(config, [("2024/03/a.MOV", "a1")])
    FakeSession.client = FakeClient(can_delete=False)

    with pytest.raises(ICloudSyncError, match="cannot delete"):
        clean_icloud.arm(config, session_factory=FakeSession, echo=lambda *a, **k: None)


def test_arm_refuses_a_manifest_stamped_for_another_account(tmp_path):
    config = make_config(tmp_path)
    seed_manifest(config, [("2024/03/a.MOV", "a1")])
    with StateStore(config.app.state_db) as state:
        state.stamp_identity(apple_id="someone-else@example.com")
    FakeSession.client = FakeClient()

    with pytest.raises(ManifestMismatchError):
        clean_icloud.arm(config, session_factory=FakeSession, echo=lambda *a, **k: None)


def test_arm_stamps_identity_for_next_time(tmp_path):
    config = make_config(tmp_path)
    seed_manifest(config, [("2024/03/a.MOV", "a1")])
    FakeSession.client = FakeClient()

    clean_icloud.arm(config, session_factory=FakeSession, echo=lambda *a, **k: None)

    with StateStore(config.app.state_db, read_only=True) as state:
        assert state.identity()["dsid"] == "4821"
        assert state.identity()["account_name"] == "Test Person"


def test_arm_lets_an_expired_session_surface_before_any_work(tmp_path):
    config = make_config(tmp_path)
    seed_manifest(config, [("2024/03/a.MOV", "a1")])
    FakeSession.client = FakeClient()
    FakeSession.raises = SessionExpiredError("run login")

    with pytest.raises(SessionExpiredError):
        clean_icloud.arm(config, session_factory=FakeSession, echo=lambda *a, **k: None)


# --- finish_and_report --------------------------------------------------------


def _armed(tmp_path, rels_and_ids, **cfg_kw):
    config = make_config(tmp_path, **cfg_kw)
    seed_manifest(config, rels_and_ids)
    return clean_icloud.arm(config, session_factory=FakeSession,
                            echo=lambda *a, **k: None)


def test_nothing_happens_without_an_armed_session(tmp_path):
    client = FakeClient()
    FakeSession.client = client
    code = clean_icloud.finish_and_report(None, outcome_for(["2024/03/a.MOV"]),
                                          source="video-clean")
    assert code == 0 and client.deleted == []


def test_nothing_happens_when_the_page_opted_out(tmp_path):
    FakeSession.client = FakeClient()
    armed = _armed(tmp_path, [("2024/03/a.MOV", "a1")])
    outcome = TrashOutcome(moved=["2024/03/a.MOV"], icloud=[], sizes={})

    assert clean_icloud.finish_and_report(armed, outcome, source="video-clean") == 0
    assert FakeSession.client.deleted == []


def test_full_run_deletes_verifies_and_records(tmp_path):
    client = FakeClient(remotes={"a1": FakeRemote("a1", "a.MOV")})
    FakeSession.client = client
    armed = _armed(tmp_path, [("2024/03/a.MOV", "a1")])
    lines = []

    code = clean_icloud.finish_and_report(
        armed, outcome_for(["2024/03/a.MOV"]), source="video-clean",
        session_factory=FakeSession, confirm=lambda n: True, echo=collect(lines))

    assert code == 0
    assert client.deleted == ["a1"]
    with StateStore(armed.config.app.state_db, read_only=True) as state:
        assert state.remote_deleted_ids(["a1"]) == {"a1"}   # idempotent next time
    assert any("Recently Deleted" in ln for ln in lines)
    assert any("still in the macOS Trash" in ln for ln in lines)


def test_declining_the_confirmation_deletes_nothing(tmp_path):
    client = FakeClient(remotes={"a1": FakeRemote("a1", "a.MOV")})
    FakeSession.client = client
    armed = _armed(tmp_path, [("2024/03/a.MOV", "a1")])
    lines = []

    code = clean_icloud.finish_and_report(
        armed, outcome_for(["2024/03/a.MOV"]), source="video-clean",
        session_factory=FakeSession, confirm=lambda n: False, echo=collect(lines))

    assert code == 0 and client.deleted == []
    assert any("Not confirmed" in ln for ln in lines)


def test_the_manifest_is_written_before_the_confirmation(tmp_path):
    """So a refusal, a crash or an expired token still leaves something usable."""
    client = FakeClient(remotes={"a1": FakeRemote("a1", "a.MOV")})
    FakeSession.client = client
    armed = _armed(tmp_path, [("2024/03/a.MOV", "a1")])
    seen = {}

    def confirm(n):
        seen["manifests"] = list(armed.config.manifest_dir.glob("*.json"))
        return False

    clean_icloud.finish_and_report(armed, outcome_for(["2024/03/a.MOV"]),
                                   source="video-clean", session_factory=FakeSession,
                                   confirm=confirm, echo=lambda *a, **k: None)

    assert len(seen["manifests"]) == 1
    written = json.loads(seen["manifests"][0].read_text())
    assert written["candidates"][0]["asset_id"] == "a1"
    assert written["apple_id"] == "me@example.com"


def test_dry_run_plans_but_never_deletes(tmp_path):
    client = FakeClient(remotes={"a1": FakeRemote("a1", "a.MOV")})
    FakeSession.client = client
    armed = _armed(tmp_path, [("2024/03/a.MOV", "a1")], dry_run=True)
    lines = []

    code = clean_icloud.finish_and_report(
        armed, outcome_for(["2024/03/a.MOV"]), source="video-clean",
        session_factory=FakeSession, confirm=lambda n: True, echo=collect(lines))

    assert code == 0 and client.deleted == []
    assert any("Dry run" in ln for ln in lines)


def test_untracked_and_ambiguous_files_are_named_not_silently_dropped(tmp_path):
    client = FakeClient(remotes={"a1": FakeRemote("a1", "a.MOV")})
    FakeSession.client = client
    config = make_config(tmp_path)
    seed_manifest(config, [("2024/03/a.MOV", "a1"),
                           ("2024/01/dup.HEIC", "d1"), ("2024/01/dup.HEIC", "d2")])
    armed = clean_icloud.arm(config, session_factory=FakeSession, echo=lambda *a, **k: None)
    lines = []

    clean_icloud.finish_and_report(
        armed, outcome_for(["2024/03/a.MOV", "2024/01/dup.HEIC", "2019/07/loose.jpg"]),
        source="local-clean", session_factory=FakeSession,
        confirm=lambda n: True, echo=collect(lines))

    text = "\n".join(lines)
    assert "eligible                 1" in text
    assert "2024/01/dup.HEIC" in text and "more than one iCloud asset" in text
    assert "2019/07/loose.jpg" in text and "not tracked" in text
    assert client.deleted == ["a1"]


def test_an_implausibly_large_plan_is_refused_outright(tmp_path):
    pairs = [(f"2024/03/f{i}.MOV", f"a{i}") for i in range(10)]
    client = FakeClient(remotes={f"a{i}": FakeRemote(f"a{i}", f"f{i}.MOV")
                                 for i in range(10)})
    FakeSession.client = client
    armed = _armed(tmp_path, pairs, max_delete=3)
    lines = []

    code = clean_icloud.finish_and_report(
        armed, outcome_for([rel for rel, _ in pairs]), source="video-clean",
        session_factory=FakeSession, confirm=lambda n: True, echo=collect(lines))

    assert code == 1 and client.deleted == []
    assert any("3-per-run" in ln for ln in lines)


def test_an_expired_session_at_delete_time_points_at_the_retry_command(tmp_path):
    client = FakeClient(remotes={"a1": FakeRemote("a1", "a.MOV")})
    FakeSession.client = client
    armed = _armed(tmp_path, [("2024/03/a.MOV", "a1")])
    lines = []
    FakeSession.raises = SessionExpiredError("session expired")

    code = clean_icloud.finish_and_report(
        armed, outcome_for(["2024/03/a.MOV"]), source="video-clean",
        session_factory=FakeSession, confirm=lambda n: True, echo=collect(lines))

    assert code == 3 and client.deleted == []
    assert any("icloud-delete --last" in ln for ln in lines)


def test_an_unverified_delete_reports_its_own_exit_code(tmp_path):
    client = FakeClient(remotes={"a1": FakeRemote("a1", "a.MOV")}, verified={"a1": False})
    FakeSession.client = client
    armed = _armed(tmp_path, [("2024/03/a.MOV", "a1")])
    lines = []

    code = clean_icloud.finish_and_report(
        armed, outcome_for(["2024/03/a.MOV"]), source="video-clean",
        session_factory=FakeSession, confirm=lambda n: True, echo=collect(lines))

    assert code == 5
    assert any("STOPPED" in ln for ln in lines)
    with StateStore(armed.config.app.state_db, read_only=True) as state:
        assert state.remote_deleted_ids(["a1"]) == set()   # never recorded as done


def test_a_receipt_is_written_next_to_the_manifest(tmp_path):
    client = FakeClient(remotes={"a1": FakeRemote("a1", "a.MOV")})
    FakeSession.client = client
    armed = _armed(tmp_path, [("2024/03/a.MOV", "a1")])

    clean_icloud.finish_and_report(
        armed, outcome_for(["2024/03/a.MOV"]), source="video-clean",
        session_factory=FakeSession, confirm=lambda n: True, echo=lambda *a, **k: None)

    receipts = list(armed.config.manifest_dir.glob("*.receipt.jsonl"))
    assert len(receipts) == 1
    phases = [json.loads(ln)["phase"] for ln in receipts[0].read_text().splitlines()]
    assert phases == ["intent", "result", "summary"]


# --- run_from_manifest --------------------------------------------------------


def test_rerunning_a_manifest_skips_what_already_went(tmp_path):
    client = FakeClient(remotes={"a1": FakeRemote("a1", "a.MOV"),
                                 "a2": FakeRemote("a2", "b.MOV")})
    FakeSession.client = client
    armed = _armed(tmp_path, [("2024/03/a.MOV", "a1"), ("2024/03/b.MOV", "a2")])

    clean_icloud.finish_and_report(
        armed, outcome_for(["2024/03/a.MOV", "2024/03/b.MOV"]), source="video-clean",
        session_factory=FakeSession, confirm=lambda n: True, echo=lambda *a, **k: None)
    assert sorted(client.deleted) == ["a1", "a2"]

    client.deleted.clear()
    code = clean_icloud.run_from_manifest(
        armed.config, None, session_factory=FakeSession,
        confirm=lambda n: True, echo=lambda *a, **k: None)

    assert code == 0 and client.deleted == []      # nothing left to do


def test_run_from_manifest_without_a_manifest_says_so(tmp_path):
    FakeSession.client = FakeClient()
    config = make_config(tmp_path)
    lines = []

    code = clean_icloud.run_from_manifest(config, None, session_factory=FakeSession,
                                          echo=collect(lines))

    assert code == 2
    assert any("No deletion manifest" in ln for ln in lines)


def test_run_from_manifest_refuses_a_manifest_for_another_folder(tmp_path):
    client = FakeClient(remotes={"a1": FakeRemote("a1", "a.MOV")})
    FakeSession.client = client
    armed = _armed(tmp_path, [("2024/03/a.MOV", "a1")])
    clean_icloud.finish_and_report(
        armed, outcome_for(["2024/03/a.MOV"]), source="video-clean",
        session_factory=FakeSession, confirm=lambda n: False, echo=lambda *a, **k: None)
    manifest = next(armed.config.manifest_dir.glob("*.json"))

    elsewhere = make_config(tmp_path / "other")
    lines = []
    code = clean_icloud.run_from_manifest(elsewhere, manifest,
                                          session_factory=FakeSession, echo=collect(lines))

    assert code == 2
    assert any("Refusing" in ln for ln in lines)


# --- the credential-free contract ---------------------------------------------


def test_clean_commands_do_not_import_auth_or_the_icloud_client():
    """Both commands must stay usable with no Apple ID; the glue is the only door."""
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "icloud_photo_sync"
    for name in ("local_clean.py", "video_clean.py", "review.py", "video_review.py"):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert "auth" not in imported, f"{name} imports auth"
        assert "icloud_client" not in imported, f"{name} imports icloud_client"


# --- the retrospective flow ---------------------------------------------------
#
# Files trashed by a session that never armed iCloud deletion, reconstructed
# after the fact. The evidence is weaker by construction, so these tests are
# mostly about what makes the run refuse.


PASS_AT = "2026-07-27T08:20:10.339666+00:00"


def seed_retro(tmp_path, rels_and_ids, *, size=100, trash_round=True,
               log_start="2026-07-05 23:15:24", **cfg_kw):
    """A folder whose manifest rows are all completed and all absent from disk."""
    config = make_config(tmp_path, **cfg_kw)
    seed_manifest(config, rels_and_ids, size=size)
    with StateStore(config.app.state_db) as state:
        state.set_meta("last_full_pass_at", PASS_AT)
        state.flush()
        state._conn.execute("UPDATE assets SET updated_at = ?",
                            ("2026-07-25T00:00:00+00:00",))
        state._conn.commit()

    config.app.logs_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"{log_start} DEBUG   keyring.backend: Loading Gnome"]
    if trash_round:
        lines.append('2026-08-10 11:04:33 DEBUG   icloud_photo_sync.review: '
                     'review: "POST /trash HTTP/1.1" 200 -')
    (config.app.logs_dir / "icloud-photo-sync.log").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return config


def retro_pair(n):
    return (f"2024/03/IMG_{n}.JPG", f"a{n}")


def test_retro_deletes_what_the_reconstruction_supports(tmp_path):
    config = seed_retro(tmp_path, [retro_pair(0), retro_pair(1)])
    client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG"),
                                 "a1": FakeRemote("a1", "IMG_1.JPG")})
    FakeSession.client = client
    lines = []

    code = clean_icloud.run_retro(
        config, session_factory=FakeSession, no_review=True,
        confirm=lambda n: True, echo=collect(lines))

    assert code == 0
    assert sorted(client.deleted) == ["a0", "a1"]
    assert any("EVIDENCE: retrospective" in ln for ln in lines)


def test_retro_records_its_evidence_class_in_the_manifest_and_receipt(tmp_path):
    config = seed_retro(tmp_path, [retro_pair(0)])
    FakeSession.client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG")})

    clean_icloud.run_retro(config, session_factory=FakeSession, no_review=True,
                           confirm=lambda n: True, echo=lambda *a, **k: None)

    [manifest] = list(config.manifest_dir.glob("*.json"))
    written = json.loads(manifest.read_text())
    assert written["evidence"] == "retrospective"
    assert written["verified_present_at"] == PASS_AT
    assert written["candidates"][0]["evidence"] == "retrospective"
    assert written["candidates"][0]["corroboration"]

    [receipt] = list(config.manifest_dir.glob("*.receipt.jsonl"))
    intents = [json.loads(ln) for ln in receipt.read_text().splitlines()]
    assert intents[0]["evidence"] == "retrospective"


def test_retro_writes_its_manifest_before_the_confirmation(tmp_path):
    config = seed_retro(tmp_path, [retro_pair(0)])
    FakeSession.client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG")})
    seen = {}

    def confirm(n):
        seen["manifests"] = list(config.manifest_dir.glob("*.json"))
        return False

    clean_icloud.run_retro(config, session_factory=FakeSession, no_review=True,
                           confirm=confirm, echo=lambda *a, **k: None)
    assert len(seen["manifests"]) == 1


def test_retro_dry_run_deletes_nothing_and_skips_the_review(tmp_path):
    config = seed_retro(tmp_path, [retro_pair(0)], dry_run=True)
    client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG")})
    FakeSession.client = client
    reviewed = []

    code = clean_icloud.run_retro(
        config, session_factory=FakeSession,
        review=lambda armed, cands: reviewed.append(cands) or frozenset(),
        confirm=lambda n: True, echo=lambda *a, **k: None)

    assert code == 0 and client.deleted == [] and reviewed == []


def test_retro_deletes_only_what_the_review_selected(tmp_path):
    config = seed_retro(tmp_path, [retro_pair(0), retro_pair(1)])
    client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG"),
                                 "a1": FakeRemote("a1", "IMG_1.JPG")})
    FakeSession.client = client

    code = clean_icloud.run_retro(
        config, session_factory=FakeSession,
        review=lambda armed, cands: frozenset({"2024/03/IMG_1.JPG"}),
        confirm=lambda n: True, echo=lambda *a, **k: None)

    assert code == 0 and client.deleted == ["a1"]


def test_an_empty_review_selection_deletes_nothing(tmp_path):
    config = seed_retro(tmp_path, [retro_pair(0)])
    client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG")})
    FakeSession.client = client

    code = clean_icloud.run_retro(
        config, session_factory=FakeSession,
        review=lambda armed, cands: frozenset(),
        confirm=lambda n: True, echo=lambda *a, **k: None)

    assert code == 0 and client.deleted == []


def test_retro_refuses_outright_when_a_missing_file_is_out_of_envelope(tmp_path):
    """One hand-deleted HEIC means something other than a clean session removed
    files, so the whole reconstruction stops."""
    config = seed_retro(tmp_path, [retro_pair(0), ("2024/03/IMG_9.HEIC", "a9")])
    client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG"),
                                 "a9": FakeRemote("a9", "IMG_9.HEIC")})
    FakeSession.client = client
    lines = []

    code = clean_icloud.run_retro(config, session_factory=FakeSession,
                                  no_review=True, confirm=lambda n: True,
                                  echo=collect(lines))

    assert code == 2 and client.deleted == []
    assert any("outside every clean" in ln for ln in lines)
    assert list(config.manifest_dir.glob("*.json")) == []


def test_retro_refuses_when_no_trash_round_is_logged(tmp_path):
    config = seed_retro(tmp_path, [retro_pair(0)], trash_round=False)
    client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG")})
    FakeSession.client = client
    lines = []

    code = clean_icloud.run_retro(config, session_factory=FakeSession,
                                  no_review=True, confirm=lambda n: True,
                                  echo=collect(lines))

    assert code == 2 and client.deleted == []
    assert any("no trash round is logged" in ln for ln in lines)


def test_retro_refuses_when_the_log_does_not_cover_the_window(tmp_path):
    config = seed_retro(tmp_path, [retro_pair(0)], log_start="2026-08-09 00:00:00")
    FakeSession.client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG")})
    lines = []

    code = clean_icloud.run_retro(config, session_factory=FakeSession,
                                  no_review=True, confirm=lambda n: True,
                                  echo=collect(lines))

    assert code == 2
    assert any("unobserved gap" in ln for ln in lines)


def test_retro_reports_a_whole_tree_and_deletes_nothing(tmp_path):
    config = seed_retro(tmp_path, [retro_pair(0)])
    (config.app.output_root / "2024/03").mkdir(parents=True, exist_ok=True)
    (config.app.output_root / "2024/03/IMG_0.JPG").write_bytes(b"x" * 100)
    client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG")})
    FakeSession.client = client
    lines = []

    code = clean_icloud.run_retro(config, session_factory=FakeSession,
                                  no_review=True, confirm=lambda n: True,
                                  echo=collect(lines))

    assert code == 0 and client.deleted == []
    assert any("Every tracked file is present" in ln for ln in lines)


# --- slicing ------------------------------------------------------------------


def test_a_large_plan_is_deleted_in_separately_confirmed_batches(tmp_path):
    pairs = [retro_pair(n) for n in range(7)]
    config = seed_retro(tmp_path, pairs, max_delete=3)
    client = FakeClient(remotes={aid: FakeRemote(aid, rel.split("/")[-1])
                                 for rel, aid in pairs})
    FakeSession.client = client
    asked = []

    code = clean_icloud.run_retro(
        config, session_factory=FakeSession, no_review=True,
        confirm=lambda n: asked.append(n) or True, echo=lambda *a, **k: None)

    assert code == 0
    assert asked == [3, 3, 1]                       # 7 assets, cap 3
    assert len(client.deleted) == 7
    assert len(list(config.manifest_dir.glob("*.json"))) == 1
    assert len(list(config.manifest_dir.glob("*.receipt.jsonl"))) == 1


def test_declining_a_later_batch_keeps_what_already_went(tmp_path):
    pairs = [retro_pair(n) for n in range(5)]
    config = seed_retro(tmp_path, pairs, max_delete=2)
    client = FakeClient(remotes={aid: FakeRemote(aid, rel.split("/")[-1])
                                 for rel, aid in pairs})
    FakeSession.client = client
    answers = iter([True, False])

    code = clean_icloud.run_retro(
        config, session_factory=FakeSession, no_review=True,
        confirm=lambda n: next(answers), echo=lambda *a, **k: None)

    assert code == 0 and len(client.deleted) == 2


def test_a_file_restored_between_batches_drops_out(tmp_path):
    """A concurrent sync putting a file back is a retraction, and the next batch
    has to see it."""
    pairs = [retro_pair(n) for n in range(4)]
    config = seed_retro(tmp_path, pairs, max_delete=2)
    client = FakeClient(remotes={aid: FakeRemote(aid, rel.split("/")[-1])
                                 for rel, aid in pairs})
    FakeSession.client = client
    calls = []

    def confirm(n):
        calls.append(n)
        if len(calls) == 1:                       # restore one before batch 2
            path = config.app.output_root / "2024/03/IMG_3.JPG"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 100)
        return True

    code = clean_icloud.run_retro(
        config, session_factory=FakeSession, no_review=True,
        confirm=confirm, echo=lambda *a, **k: None)

    assert code == 0
    assert "a3" not in client.deleted and len(client.deleted) == 3


# --- resuming a retrospective manifest ----------------------------------------


def test_rerunning_a_retro_manifest_keeps_its_evidence_class(tmp_path):
    """Regression: replaying the recorded sizes would re-bless bytes_done as a
    measurement, and passing None would dead-end at "nothing left to delete"."""
    config = seed_retro(tmp_path, [retro_pair(0), retro_pair(1)])
    client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG"),
                                 "a1": FakeRemote("a1", "IMG_1.JPG")})
    FakeSession.client = client
    clean_icloud.run_retro(config, session_factory=FakeSession, no_review=True,
                           confirm=lambda n: False, echo=lambda *a, **k: None)
    [manifest] = list(config.manifest_dir.glob("*.json"))
    assert client.deleted == []

    lines = []
    code = clean_icloud.run_from_manifest(
        config, manifest, session_factory=FakeSession,
        confirm=lambda n: True, echo=collect(lines))

    assert code == 0 and sorted(client.deleted) == ["a0", "a1"]
    assert any("evidence   : retrospective" in ln for ln in lines)


def test_rerunning_a_retro_manifest_skips_what_already_went(tmp_path):
    config = seed_retro(tmp_path, [retro_pair(0), retro_pair(1)])
    client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG"),
                                 "a1": FakeRemote("a1", "IMG_1.JPG")})
    FakeSession.client = client
    clean_icloud.run_retro(config, session_factory=FakeSession, no_review=True,
                           confirm=lambda n: True, echo=lambda *a, **k: None)
    [manifest] = list(config.manifest_dir.glob("*.json"))
    assert sorted(client.deleted) == ["a0", "a1"]

    client.deleted.clear()
    lines = []
    code = clean_icloud.run_from_manifest(config, manifest,
                                          session_factory=FakeSession,
                                          confirm=lambda n: True,
                                          echo=collect(lines))

    assert code == 0 and client.deleted == []
    assert any("Nothing left to delete" in ln for ln in lines)


def test_a_retro_manifest_refuses_if_the_reconstruction_no_longer_holds(tmp_path):
    config = seed_retro(tmp_path, [retro_pair(0)])
    client = FakeClient(remotes={"a0": FakeRemote("a0", "IMG_0.JPG")})
    FakeSession.client = client
    clean_icloud.run_retro(config, session_factory=FakeSession, no_review=True,
                           confirm=lambda n: False, echo=lambda *a, **k: None)
    [manifest] = list(config.manifest_dir.glob("*.json"))

    # Something removed a HEIC in the meantime: the premise is broken now.
    seed_manifest(config, [("2024/03/IMG_9.HEIC", "a9")])
    with StateStore(config.app.state_db) as state:
        state.flush()
        state._conn.execute("UPDATE assets SET updated_at = ?",
                            ("2026-07-25T00:00:00+00:00",))
        state._conn.commit()

    code = clean_icloud.run_from_manifest(config, manifest,
                                          session_factory=FakeSession,
                                          confirm=lambda n: True,
                                          echo=lambda *a, **k: None)
    assert code == 2 and client.deleted == []


# --- the confirmation phrase --------------------------------------------------


def test_the_retrospective_confirmation_demands_the_evidence_class(monkeypatch):
    """A bare count recalled from a measured run must not work here."""
    monkeypatch.setattr(clean_icloud.sys.stdin, "isatty", lambda: True, raising=False)

    assert not clean_icloud._confirm_retrospective(500, prompt=lambda *a, **k: "500")
    assert clean_icloud._confirm_retrospective(
        500, prompt=lambda *a, **k: "delete 500 retrospective")
    assert not clean_icloud._confirm_retrospective(
        500, prompt=lambda *a, **k: "delete 499 retrospective")


def test_the_measured_confirmation_is_unchanged(monkeypatch):
    monkeypatch.setattr(clean_icloud.sys.stdin, "isatty", lambda: True, raising=False)
    assert clean_icloud._confirm_by_count(42, prompt=lambda *a, **k: "42")
    assert not clean_icloud._confirm_by_count(42, prompt=lambda *a, **k: "41")


def test_retro_clean_stays_credential_free():
    """The scan half must remain unable to reach the network."""
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "icloud_photo_sync"
    tree = ast.parse((package / "retro_clean.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert "auth" not in imported and "icloud_client" not in imported


def test_resuming_a_retro_manifest_slices_instead_of_refusing_at_the_cap(tmp_path):
    """Regression: --last used to hit guard_refusal and tell the user to "trash
    fewer files at a time", which is impossible once the files are gone."""
    pairs = [retro_pair(n) for n in range(5)]
    config = seed_retro(tmp_path, pairs, max_delete=2)
    client = FakeClient(remotes={aid: FakeRemote(aid, rel.split("/")[-1])
                                 for rel, aid in pairs})
    FakeSession.client = client

    # Write the manifest without deleting, exactly as a dry run or a decline does.
    clean_icloud.run_retro(config, session_factory=FakeSession, no_review=True,
                           confirm=lambda n: False, echo=lambda *a, **k: None)
    [manifest] = list(config.manifest_dir.glob("*.json"))
    assert client.deleted == []

    asked = []
    lines = []
    code = clean_icloud.run_from_manifest(
        config, manifest, session_factory=FakeSession,
        confirm=lambda n: asked.append(n) or True, echo=collect(lines))

    assert code == 0
    assert asked == [2, 2, 1]                      # sliced, not refused
    assert len(client.deleted) == 5
    assert not any("trash fewer files" in ln for ln in lines)


def test_resuming_a_measured_manifest_still_refuses_at_the_cap(tmp_path):
    """The strong path keeps its single confirmation and its per-run cap."""
    rels = [f"2024/03/m{n}.MOV" for n in range(5)]
    armed = _armed(tmp_path, [(rel, f"m{n}") for n, rel in enumerate(rels)],
                   max_delete=2)
    client = FakeClient(remotes={f"m{n}": FakeRemote(f"m{n}", f"m{n}.MOV")
                                 for n in range(5)})
    FakeSession.client = client
    lines = []

    code = clean_icloud.finish_and_report(
        armed, outcome_for(rels), source="local-clean",
        session_factory=FakeSession, confirm=lambda n: True, echo=collect(lines))

    assert code == 1 and client.deleted == []
    assert any("exceeds the 2-per-run limit" in ln for ln in lines)
