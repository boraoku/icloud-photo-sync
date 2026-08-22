"""Tests for the convert and swap engines in :mod:`icloud_photo_sync.optimise`.

The swap group is the important one. Every test in it asserts what happened to
the *original* — because the failure this feature has to be incapable of is
deleting a video from iCloud when its replacement is not there. Several of the
tests below would pass just as well if the code simply never deleted anything,
which is deliberate: that is the safe direction, and the one happy-path test is
what stops the suite being satisfied by a no-op.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from icloud_photo_sync import optimise as op
from icloud_photo_sync import optimise_job as oj
from icloud_photo_sync import video_optimise as vo
from icloud_photo_sync.config import VideoOptimiseConfig
from icloud_photo_sync.errors import ICloudSyncError
from icloud_photo_sync.icloud_client import DeleteResult, RemoteAsset
from icloud_photo_sync.state import StateStore
from icloud_photo_sync.transcode import ConvertResult


# --- fixtures ----------------------------------------------------------------


def make_probe(**kw) -> vo.VideoProbe:
    base = dict(rel="2024/05/IMG_1.MOV", size=300 * 1024 * 1024, width=2160,
                height=3840, fps=60.0, duration=36.0, codec="hevc",
                pix_fmt="yuv420p10le", transfer="arib-std-b67",
                primaries="bt2020", colorspace="bt2020nc", has_audio=True)
    base.update(kw)
    return vo.VideoProbe(**base)


@pytest.fixture
def config(tmp_path) -> VideoOptimiseConfig:
    root = (tmp_path / "tree").resolve()
    root.mkdir()
    return VideoOptimiseConfig.create(root, config_root=tmp_path / "cfg")


@pytest.fixture
def job(config):
    with oj.OptimiseJob(config.job_db) as store:
        yield store


def seed_converted(job, config, *, rel="2024/05/IMG_1.MOV", asset_id="OLD",
                   src_bytes=300 * 1024 * 1024, out_bytes=25 * 1024 * 1024):
    """A row that has been converted and is waiting to be swapped."""
    source = make_probe(rel=rel, size=src_bytes)
    encode = vo.choose_encode(source)
    job.add(rel, asset_id=asset_id, src_bytes=src_bytes,
            src_probe=op._probe_dict(source), plan=op._encode_dict(encode))
    src = config.output_root / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"o" * 32)
    name = vo.flat_name(rel)
    out = config.work_path(name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"x" * out_bytes)
    output = make_probe(rel=rel, size=out_bytes, width=1080, height=1920, fps=30.0)
    job.mark_converted(rel, out_rel=name, out_bytes=out_bytes,
                       out_probe=op._probe_dict(output))
    return rel


def remote(asset_id="OLD", *, deleted=False, zone=None) -> RemoteAsset:
    return RemoteAsset(
        asset_id=asset_id, record_type="CPLAsset", change_tag="tag",
        zone=zone or {"zoneName": "PrimarySync"}, filename="IMG_1.MOV",
        size=300 * 1024 * 1024, capture_dt=datetime(2024, 5, 3, tzinfo=timezone.utc),
        is_deleted=deleted, is_expunged=False,
    )


_UNSET = object()      # so a test can pass present=None and mean it


class FakeAsset:
    """What ``iter_added_desc`` yields: a recently-added iCloud asset."""

    def __init__(self, asset_id, filename, size, added=None):
        self.id = asset_id
        self.filename = filename
        self.size = size
        self.added_dt = added or datetime.now(timezone.utc)


class FakeClient:
    """Records everything, so a test can assert what was *not* done."""

    def __init__(self, *, present=_UNSET, delete_ok=True, verified=True,
                 old=_UNSET, arrivals=None, added_desc_none=False):
        self.deleted: list[str] = []
        self._present = remote("NEW") if present is _UNSET else present
        self._delete_ok = delete_ok
        self._verified = verified
        self._old = remote("OLD") if old is _UNSET else old
        self._arrivals = list(arrivals or [])
        self._added_desc_none = added_desc_none

    def iter_added_desc(self):
        if self._added_desc_none:
            return None
        return iter(self._arrivals)

    def verify_present(self, asset_id):
        return self._present

    def lookup_assets(self, ids):
        if self._old is None:
            return {}, list(ids)
        return {self._old.asset_id: self._old}, []

    def delete_assets(self, assets):
        self.deleted.extend(a.asset_id for a in assets)
        return [DeleteResult(asset_id=a.asset_id, ok=self._delete_ok,
                             error=None if self._delete_ok else "SERVER_SAYS_NO")
                for a in assets]

    def verify_deleted(self, ids):
        return {i: self._verified for i in ids}


def swap(row, *, client, job, config, state=None):
    return op._swap_one(row, client=client, state=state, job=job, config=config,
                        echo=lambda *a, **k: None)


def reconcile(job, config, *, arrivals, **kw):
    client = FakeClient(arrivals=arrivals, **kw)
    return client, op.reconcile(job, client, config, echo=lambda *a, **k: None)


# --- the swap fence ----------------------------------------------------------


# --- conversion --------------------------------------------------------------


def seed_selected(job, config, *, rel="2024/05/IMG_1.MOV", size=300 * 1024 * 1024):
    source = make_probe(rel=rel, size=size)
    job.add(rel, asset_id="OLD", src_bytes=size, src_probe=op._probe_dict(source),
            plan=op._encode_dict(vo.choose_encode(source)))
    src = config.output_root / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"o" * 64)
    return rel, source


def fake_convert(size: int):
    def convert(src, dest, encode, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * size)
        return ConvertResult(ok=True, size=size)
    return convert


def convert_all(job, config, *, convert_fn, probe_fn, cancel=None):
    return op._convert_all(job, config, echo=lambda *a, **k: None,
                           cancel=cancel, convert_fn=convert_fn, probe_fn=probe_fn)


class TestConversionGate:
    def test_a_good_output_is_kept(self, job, config):
        rel, source = seed_selected(job, config)
        out = make_probe(rel=rel, size=25 * 1024 * 1024, width=1080, height=1920)
        totals = convert_all(job, config, convert_fn=fake_convert(25 * 1024 * 1024),
                             probe_fn=lambda p, r: out)
        assert totals.converted == 1
        assert job.get(rel)["status"] == oj.STATUS_CONVERTED

    def test_an_output_that_lost_its_colour_is_discarded(self, job, config):
        rel, _ = seed_selected(job, config)
        washed = make_probe(rel=rel, size=25 * 1024 * 1024, width=1080, height=1920,
                            transfer="bt709", primaries="bt709",
                            colorspace="bt709", pix_fmt="yuv420p")
        convert_all(job, config, convert_fn=fake_convert(25 * 1024 * 1024),
                    probe_fn=lambda p, r: washed)
        assert job.get(rel)["status"] == oj.STATUS_COLOUR_MISMATCH
        assert not config.work_path(rel).exists()      # the bad file is gone
        assert (config.output_root / rel).exists()     # the original is not

    def test_an_output_that_barely_shrank_is_discarded(self, job, config):
        rel, _ = seed_selected(job, config)
        big = 290 * 1024 * 1024
        out = make_probe(rel=rel, size=big, width=1080, height=1920)
        convert_all(job, config, convert_fn=fake_convert(big), probe_fn=lambda p, r: out)
        assert job.get(rel)["status"] == oj.STATUS_NOT_WORTH_IT
        assert not config.work_path(rel).exists()

    def test_an_output_that_cannot_be_read_back_is_a_failure(self, job, config):
        rel, _ = seed_selected(job, config)
        convert_all(job, config, convert_fn=fake_convert(1024), probe_fn=lambda p, r: None)
        assert job.get(rel)["status"] == oj.STATUS_CONVERT_FAILED
        assert not config.work_path(rel).exists()

    def test_a_failed_encode_leaves_the_original(self, job, config):
        rel, _ = seed_selected(job, config)

        def failing(src, dest, encode, **kw):
            return ConvertResult(ok=False, error="ffmpeg exploded")

        totals = convert_all(job, config, convert_fn=failing, probe_fn=lambda p, r: None)
        assert totals.failed == 1
        assert job.get(rel)["status"] == oj.STATUS_CONVERT_FAILED
        assert (config.output_root / rel).exists()

    def test_a_cancel_leaves_the_rest_selected_for_next_time(self, job, config):
        import threading
        first, _ = seed_selected(job, config, rel="2024/05/A.MOV", size=400 * 1024 * 1024)
        second, _ = seed_selected(job, config, rel="2024/05/B.MOV", size=300 * 1024 * 1024)
        cancel = threading.Event()

        def convert(src, dest, encode, **kw):
            cancel.set()                       # stop after the first one starts
            return ConvertResult(ok=False, cancelled=True)

        convert_all(job, config, convert_fn=convert, probe_fn=lambda p, r: None,
                    cancel=cancel)
        assert job.get(first)["status"] == oj.STATUS_SELECTED
        assert job.get(second)["status"] == oj.STATUS_SELECTED


# --- confirmation ------------------------------------------------------------


class TestConfirmDelete:
    def _answers(self, *replies):
        queue = list(replies)
        return lambda *a, **k: queue.pop(0)

    def test_both_phrases_are_required(self, monkeypatch):
        monkeypatch.setattr(op.sys.stdin, "isatty", lambda: True, raising=False)
        assert op.confirm_delete(10, prompt=self._answers("delete 10 originals", "YES I AM SURE")) is True

    def test_the_count_must_match(self, monkeypatch):
        monkeypatch.setattr(op.sys.stdin, "isatty", lambda: True, raising=False)
        assert op.confirm_delete(10, prompt=self._answers("delete 9 originals")) is False

    def test_the_second_phrase_must_match(self, monkeypatch):
        monkeypatch.setattr(op.sys.stdin, "isatty", lambda: True, raising=False)
        assert op.confirm_delete(10, prompt=self._answers("delete 10 originals", "yes")) is False

    def test_case_and_spacing_are_forgiven(self, monkeypatch):
        monkeypatch.setattr(op.sys.stdin, "isatty", lambda: True, raising=False)
        assert op.confirm_delete(3, prompt=self._answers("  Delete 3   Originals ", "yes i am sure")) is True

    def test_a_non_tty_refuses_outright(self, monkeypatch):
        monkeypatch.setattr(op.sys.stdin, "isatty", lambda: False, raising=False)
        with pytest.raises(ICloudSyncError, match="interactive"):
            op.confirm_delete(1, prompt=self._answers("delete 1 originals"))


# --- local cleanup -----------------------------------------------------------


class TestCleanup:
    def _trash(self, seen):
        from icloud_photo_sync.trash import TrashResult

        def fn(paths):
            seen.extend(paths)
            for p in paths:
                p.unlink(missing_ok=True)
            return [TrashResult(path=p, ok=True) for p in paths]
        return fn

    def test_only_swapped_originals_are_offered(self, job, config):
        swapped = seed_converted(job, config, rel="2024/05/A.MOV")
        job.mark_uploaded(swapped, "NEW")
        job.mark_swapped(swapped)
        kept = seed_converted(job, config, rel="2024/05/B.MOV")   # still 'converted'
        seen: list[Path] = []
        op._cleanup_locals(job, config, echo=lambda *a, **k: None,
                           confirm=lambda *a, **k: True, trash_fn=self._trash(seen))
        assert [p.name for p in seen] == ["A.MOV"]
        assert (config.output_root / kept).exists()

    def test_declining_keeps_everything(self, job, config):
        rel = seed_converted(job, config)
        job.mark_uploaded(rel, "NEW")
        job.mark_swapped(rel)
        seen: list[Path] = []
        op._cleanup_locals(job, config, echo=lambda *a, **k: None,
                           confirm=lambda *a, **k: False, trash_fn=self._trash(seen))
        assert seen == [] and (config.output_root / rel).exists()

    def test_the_converted_copy_takes_the_original_place(self, job, config):
        rel = seed_converted(job, config)
        job.mark_uploaded(rel, "NEW")
        job.mark_swapped(rel)
        op._cleanup_locals(job, config, echo=lambda *a, **k: None,
                           confirm=lambda *a, **k: True, trash_fn=self._trash([]))
        final = (config.output_root / rel).with_suffix(".mov")
        assert final.exists() and not config.work_path(vo.flat_name(rel)).exists()

    def test_rejected_conversions_are_discarded_only_on_confirmation(self, job, config):
        rel = seed_converted(job, config)
        job.mark_rejected(rel)
        conv = config.work_path(vo.flat_name(rel))
        op._discard_rejected(job, config, echo=lambda *a, **k: None,
                             confirm=lambda *a, **k: False)
        assert conv.exists()
        op._discard_rejected(job, config, echo=lambda *a, **k: None,
                             confirm=lambda *a, **k: True)
        assert not conv.exists()


# --- resuming an offline job online ------------------------------------------


class TestBackfillAssetIds:
    """A ``--no-upload`` run records no asset ids; resuming online must fix that.

    Without this the rows are stranded: they are already ``converted``, and
    ``add()`` deliberately refuses to reset a converted row, so the id has no
    other route in.
    """

    def _manifest(self, tmp_path, rel, asset_id="OLD"):
        from icloud_photo_sync.models import AssetRef
        db = tmp_path / "state.db"
        with StateStore(db) as state:
            state.register(AssetRef(id=asset_id, filename=Path(rel).name,
                                    capture_dt=None, added_dt=None, size=10), rel)
            state.mark_completed(asset_id, 10)
        return db

    def test_a_converted_row_with_no_id_gets_one(self, tmp_path, job, config):
        rel = seed_converted(job, config, asset_id=None)
        assert job.get(rel)["asset_id"] is None
        assert op._backfill_asset_ids(job, self._manifest(tmp_path, rel)) == 1
        assert job.get(rel)["asset_id"] == "OLD"

    def test_an_existing_id_is_never_overwritten(self, tmp_path, job, config):
        # An id already on the row came from a run that had the manifest open;
        # a later guess must not be allowed to replace it.
        rel = seed_converted(job, config, asset_id="ORIGINAL")
        op._backfill_asset_ids(job, self._manifest(tmp_path, rel, asset_id="OTHER"))
        assert job.get(rel)["asset_id"] == "ORIGINAL"

    def test_a_missing_manifest_is_not_an_error(self, tmp_path, job, config):
        seed_converted(job, config, asset_id=None)
        assert op._backfill_asset_ids(job, tmp_path / "nope.db") == 0

    def test_a_swapped_row_is_left_alone(self, tmp_path, job, config):
        rel = seed_converted(job, config, asset_id="OLD")
        job.mark_uploaded(rel, "NEW")
        job.mark_swapped(rel)
        assert op._backfill_asset_ids(job, self._manifest(tmp_path, rel)) == 0

    def test_the_backfilled_row_can_then_be_deleted(self, tmp_path, job, config):
        rel = seed_converted(job, config, asset_id=None)
        op._backfill_asset_ids(job, self._manifest(tmp_path, rel))
        job.mark_uploaded(rel, "NEW")
        client = FakeClient()
        ok, _ = swap(job.get(rel), client=client, job=job, config=config)
        assert ok is True and client.deleted == ["OLD"]


# --- retrying failed swaps ----------------------------------------------------


class TestRetryFailedDeletes:
    """A failed swap must be retryable by re-running — the failure message says
    "Re-run to retry", and without ``reset_swap_failed`` that promise was false:
    the convert phase only looks at ``selected``, the swap phase at ``converted``
    and ``uploaded``, so 12 ``swap_failed`` rows dead-ended the whole command
    (observed live: "Resuming … 0 still to convert, 0 converted" → exit).
    """

    def test_reset_sends_failed_rows_back_to_converted(self, job, config):
        rel = seed_converted(job, config)
        job.mark_swap_failed(rel, "upload failed: HTTP 410")
        assert job.converted() == []            # the stranding, demonstrated
        assert job.reset_swap_failed() == 1
        assert job.get(rel)["status"] == oj.STATUS_CONVERTED

    def test_reset_is_a_noop_with_nothing_failed(self, job, config):
        seed_converted(job, config)
        assert job.reset_swap_failed() == 0

    def test_a_reset_row_deletes_on_the_next_attempt(self, job, config):
        rel = seed_converted(job, config)
        job.mark_swap_failed(rel, "upload failed: HTTP 410")
        job.reset_swap_failed()
        job.mark_uploaded(rel, "NEW")
        client = FakeClient()
        ok, _ = swap(job.get(rel), client=client, job=job, config=config)
        assert ok is True and client.deleted == ["OLD"]

    def test_a_verified_replacement_survives_the_reset(self, job, config):
        # The delete was refused, not the upload: the replacement is still in
        # iCloud, so the retry must go straight to the delete and not ask the
        # user to upload the same file again.
        rel = seed_converted(job, config)
        job.mark_uploaded(rel, "NEW")
        job.mark_swap_failed(rel, "delete refused: SERVER_SAYS_NO")
        job.reset_swap_failed()
        assert job.get(rel)["new_asset_id"] == "NEW"
        client = FakeClient()
        ok, _ = swap(job.get(rel), client=client, job=job, config=config)
        assert ok is True and client.deleted == ["OLD"]


# --- reconciliation -----------------------------------------------------------


class TestReconcile:
    """Reconciliation is the ONLY source of a verified ``new_asset_id`` now, and
    therefore the only thing that can ever authorise deleting an original. Every
    test here is really asking the same question: can this be made to point at
    the wrong asset, or at an asset that is not really there?
    """

    OUT = 25 * 1024 * 1024

    def _seed(self, job, config, rel="2024/05/IMG_1.MOV"):
        seed_converted(job, config, rel=rel, out_bytes=self.OUT)
        return vo.flat_name(rel)

    def test_an_exact_match_is_accepted(self, job, config):
        name = self._seed(job, config)
        client, (found, waiting) = reconcile(
            job, config, arrivals=[FakeAsset("NEW", name, self.OUT)])
        assert (found, waiting) == (1, 0)
        row = job.get("2024/05/IMG_1.MOV")
        assert row["status"] == oj.STATUS_UPLOADED and row["new_asset_id"] == "NEW"

    def test_filename_case_is_forgiven(self, job, config):
        # The photo volume is case-insensitive; Photos may echo a different case.
        name = self._seed(job, config)
        _, (found, _) = reconcile(
            job, config, arrivals=[FakeAsset("NEW", name.upper(), self.OUT)])
        assert found == 1

    def test_a_wrong_size_is_not_a_match(self, job, config):
        name = self._seed(job, config)
        _, (found, waiting) = reconcile(
            job, config, arrivals=[FakeAsset("NEW", name, self.OUT + 1)])
        assert (found, waiting) == (0, 1)
        assert job.get("2024/05/IMG_1.MOV")["new_asset_id"] is None

    def test_a_wrong_name_is_not_a_match(self, job, config):
        self._seed(job, config)
        _, (found, waiting) = reconcile(
            job, config, arrivals=[FakeAsset("NEW", "SOMETHING_ELSE.mov", self.OUT)])
        assert (found, waiting) == (0, 1)

    def test_the_original_itself_can_never_be_the_match(self, job, config):
        # Same name and size as the conversion would still not do: matching the
        # row's own original would authorise deleting it in favour of itself.
        name = self._seed(job, config)
        _, (found, _) = reconcile(
            job, config, arrivals=[FakeAsset("OLD", name, self.OUT)])
        assert found == 0

    def test_two_candidates_are_refused_not_guessed(self, job, config):
        name = self._seed(job, config)
        _, (found, waiting) = reconcile(job, config, arrivals=[
            FakeAsset("NEW1", name, self.OUT), FakeAsset("NEW2", name, self.OUT)])
        assert (found, waiting) == (0, 1)
        assert job.get("2024/05/IMG_1.MOV")["new_asset_id"] is None

    def test_nothing_uploaded_yet_is_not_an_error(self, job, config):
        self._seed(job, config)
        _, (found, waiting) = reconcile(job, config, arrivals=[])
        assert (found, waiting) == (0, 1)

    def test_an_unavailable_listing_changes_nothing(self, job, config):
        name = self._seed(job, config)
        client = FakeClient(arrivals=[FakeAsset("NEW", name, self.OUT)],
                            added_desc_none=True)
        found, waiting = op.reconcile(job, client, config, echo=lambda *a, **k: None)
        assert (found, waiting) == (0, 1)
        assert job.get("2024/05/IMG_1.MOV")["new_asset_id"] is None

    def test_a_missing_conversion_file_is_not_pending(self, job, config):
        name = self._seed(job, config)
        config.work_path(name).unlink()
        _, (found, waiting) = reconcile(
            job, config, arrivals=[FakeAsset("NEW", name, self.OUT)])
        assert (found, waiting) == (0, 0)

    def test_assets_older_than_the_conversion_stop_the_scan(self, job, config):
        name = self._seed(job, config)
        ancient = datetime.now(timezone.utc) - timedelta(days=400)
        _, (found, _) = reconcile(
            job, config, arrivals=[FakeAsset("NEW", name, self.OUT, added=ancient)])
        assert found == 0

    def test_two_rows_wanting_one_asset_are_both_refused(self, job, config):
        # Two conversions that happen to share a name and size (defended against
        # by flat_name, but the matcher must not rely on that).
        seed_converted(job, config, rel="a/b/IMG_1.MOV", out_bytes=self.OUT)
        job.set_out_rel("a/b/IMG_1.MOV", "SAME.mov")
        seed_converted(job, config, rel="c/d/IMG_2.MOV", out_bytes=self.OUT)
        job.set_out_rel("c/d/IMG_2.MOV", "SAME.mov")
        (config.work_dir / "SAME.mov").write_bytes(b"x" * self.OUT)
        _, (found, _) = reconcile(
            job, config, arrivals=[FakeAsset("NEW", "SAME.mov", self.OUT)])
        assert found == 0

    def test_a_reconciled_row_then_deletes_its_original(self, job, config):
        name = self._seed(job, config)
        reconcile(job, config, arrivals=[FakeAsset("NEW", name, self.OUT)])
        client = FakeClient()
        ok, _ = swap(job.get("2024/05/IMG_1.MOV"), client=client, job=job,
                     config=config)
        assert ok is True and client.deleted == ["OLD"]


class TestDeleteStillRefusesWithoutAReplacement:
    def test_a_row_with_no_verified_replacement_raises(self, job, config):
        rel = seed_converted(job, config)          # converted, never reconciled
        client = FakeClient()
        with pytest.raises(ValueError, match="no verified replacement"):
            swap(job.get(rel), client=client, job=job, config=config)
        assert client.deleted == []

    def test_a_replacement_that_vanished_since_reconcile_stops_the_delete(self, job, config):
        rel = seed_converted(job, config)
        job.mark_uploaded(rel, "NEW")
        client = FakeClient(present=None)          # user deleted the upload again
        ok, _ = swap(job.get(rel), client=client, job=job, config=config)
        assert ok is False and client.deleted == []
        assert job.get(rel)["status"] == oj.STATUS_SWAP_FAILED

    def test_a_missing_converted_file_deletes_nothing(self, job, config):
        rel = seed_converted(job, config)
        job.mark_uploaded(rel, "NEW")
        (config.work_dir / job.get(rel)["out_rel"]).unlink()
        client = FakeClient()
        ok, _ = swap(job.get(rel), client=client, job=job, config=config)
        assert ok is False and client.deleted == []

    def test_a_shared_library_original_is_refused(self, job, config):
        rel = seed_converted(job, config)
        job.mark_uploaded(rel, "NEW")
        client = FakeClient(old=remote("OLD", zone={"zoneName": "SharedSync-ABC"}))
        ok, _ = swap(job.get(rel), client=client, job=job, config=config)
        assert ok is False and client.deleted == []

    def test_a_delete_iCloud_did_not_really_apply_is_a_failure(self, job, config):
        rel = seed_converted(job, config)
        job.mark_uploaded(rel, "NEW")
        client = FakeClient(verified=False)
        ok, _ = swap(job.get(rel), client=client, job=job, config=config)
        assert ok is False
        assert job.get(rel)["status"] == oj.STATUS_SWAP_FAILED

    def test_the_sync_manifest_learns_about_the_replacement(self, tmp_path, job, config):
        rel = seed_converted(job, config)
        job.mark_uploaded(rel, "NEW")
        db = tmp_path / "state.db"
        client = FakeClient()
        with StateStore(db) as state:
            ok, _ = swap(job.get(rel), client=client, job=job, config=config,
                         state=state)
            assert ok is True
            assert state.get("NEW")["status"] == "completed"
            assert state.remote_deleted_ids(["OLD"]) == {"OLD"}


class TestWorkDirMigration:
    def test_nested_conversions_are_flattened_not_re_encoded(self, job, config):
        rel = "2024/05/IMG_1.MOV"
        seed_converted(job, config, rel=rel)
        # Rewrite the row into the old nested shape and move the file there.
        old_rel = "2024/05/IMG_1.mov"
        legacy = config.legacy_work_dir / old_rel
        legacy.parent.mkdir(parents=True, exist_ok=True)
        config.work_path(vo.flat_name(rel)).replace(legacy)
        job.set_out_rel(rel, old_rel)

        assert op._migrate_work_dir(job, config, lambda *a, **k: None) == 1
        assert job.get(rel)["out_rel"] == "IMG_1.mov"
        assert config.work_path("IMG_1.mov").is_file()
        assert not legacy.exists()

    def test_migration_is_idempotent(self, job, config):
        seed_converted(job, config)
        assert op._migrate_work_dir(job, config, lambda *a, **k: None) == 0

    def test_a_failed_run_still_gets_its_files_migrated(self, job, config):
        # The reset-then-migrate ordering: a job whose last run ended in delete
        # failures must still have its conversions moved to the new folder,
        # or those 12 files sit in the old hidden directory forever.
        rel = "2024/05/IMG_1.MOV"
        seed_converted(job, config, rel=rel)
        legacy = config.legacy_work_dir / "2024/05/IMG_1.mov"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        config.work_path(vo.flat_name(rel)).replace(legacy)
        job.set_out_rel(rel, "2024/05/IMG_1.mov")
        job.mark_swap_failed(rel, "upload failed: HTTP 410")

        job.reset_swap_failed()
        assert op._migrate_work_dir(job, config, lambda *a, **k: None) == 1
        assert config.work_path("IMG_1.mov").is_file()
