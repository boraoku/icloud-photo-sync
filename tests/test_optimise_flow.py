"""Tests for the convert and swap engines in :mod:`icloud_photo_sync.optimise`.

The swap group is the important one. Every test in it asserts what happened to
the *original* — because the failure this feature has to be incapable of is
deleting a video from iCloud when its replacement is not there. Several of the
tests below would pass just as well if the code simply never deleted anything,
which is deliberate: that is the safe direction, and the one happy-path test is
what stops the suite being satisfied by a no-op.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from icloud_photo_sync import optimise as op
from icloud_photo_sync import optimise_job as oj
from icloud_photo_sync import optimise_review as orv
from icloud_photo_sync import video_optimise as vo
from icloud_photo_sync.config import VideoOptimiseConfig
from icloud_photo_sync.errors import ICloudSyncError
from icloud_photo_sync.icloud_client import DeleteResult, RemoteAsset
from icloud_photo_sync.metadata import MetadataOutcome
from icloud_photo_sync.models import AssetRef
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


def _sparse(path: Path, size: int) -> None:
    """A file that really is ``size`` bytes, without occupying them."""
    with open(path, "wb") as fh:
        fh.truncate(size)


def seed_converted(job, config, *, rel="2024/05/IMG_1.MOV", asset_id="OLD",
                   src_bytes=300 * 1024 * 1024, out_bytes=25 * 1024 * 1024):
    """A row that has been converted and is waiting to be swapped."""
    source = make_probe(rel=rel, size=src_bytes)
    encode = vo.choose_encode(source)
    job.add(rel, asset_id=asset_id, src_bytes=src_bytes,
            src_probe=op._probe_dict(source), plan=op._encode_dict(encode))
    src = config.output_root / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    _sparse(src, src_bytes)          # real recorded size, no real bytes on disk
    name = vo.flat_name(rel)
    out = config.work_path(name)
    out.parent.mkdir(parents=True, exist_ok=True)
    _sparse(out, out_bytes)
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
    _sparse(src, size)
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

    def test_a_row_with_no_stored_plan_gets_one_recomputed_from_its_probe(self, job, config):
        # The regression this pins down happened live: retry_colour_mismatch()
        # flipped a row back to 'selected' but its stored plan still carried
        # the pre-fix decision (main10/p010le for an 8-bit HDR source). The
        # very next real run reused that stale plan verbatim and reproduced
        # the exact colour mismatch the retry existed to fix. A missing plan
        # must be recomputed from the retained probe under CURRENT policy, not
        # treated as a hard failure and not silently trusted from cache.
        rel = "2024/05/IMG_1.MOV"
        source = make_probe(rel=rel, pix_fmt="yuv420p",     # 8-bit HDR: the real shape
                            size=140 * 1024 * 1024, duration=20.0)
        stale_plan = op._encode_dict(vo.Encode(               # the pre-fix, wrong decision
            width=1080, height=1920, fps=30.0, bitrate=8_000_000,
            profile=vo.PROFILE_10BIT, pix_fmt=vo.PIX_FMT_10BIT,
            transfer="arib-std-b67", primaries="bt2020", colorspace="bt2020nc"))
        job.add(rel, asset_id="OLD", src_bytes=source.size,
                src_probe=op._probe_dict(source), plan=stale_plan)
        job.mark_skipped(rel, oj.STATUS_COLOUR_MISMATCH, "stale plan, pre-fix")
        job.retry_colour_mismatch()
        assert job.get(rel)["plan"] is None            # the fix under test

        seen_encodes = []

        def convert(src, dest, encode, **kw):
            seen_encodes.append(encode)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x" * (30 * 1024 * 1024))
            return ConvertResult(ok=True, size=30 * 1024 * 1024)

        output = make_probe(rel=rel, size=30 * 1024 * 1024, width=1080, height=1920,
                            pix_fmt="yuv420p", transfer="arib-std-b67",
                            primaries="bt2020", colorspace="bt2020nc")
        convert_all(job, config, convert_fn=convert, probe_fn=lambda p, r: output)

        assert len(seen_encodes) == 1
        # The recomputed plan must follow the CURRENT policy (8-bit, matching
        # the source), not the stale cached one -- this is what a real
        # ffmpeg invocation would have received.
        assert seen_encodes[0].pix_fmt == vo.PIX_FMT_8BIT
        assert seen_encodes[0].profile == vo.PROFILE_8BIT
        assert job.get(rel)["status"] == oj.STATUS_CONVERTED


class TestCaptureDateStamping:
    """_convert_all consults the sync manifest for the source's true capture
    date and stamps the converted output — the video-optimise half of the
    WhatsApp-strips-metadata fix; see icloud_photo_sync.metadata."""

    def _state_db(self, tmp_path):
        from icloud_photo_sync.config import AppConfig
        app = AppConfig.create("t@e.com", tmp_path / "tree", config_root=tmp_path / "cfg")
        return app.state_db

    def _register(self, state_db, asset_id, capture_dt, rel):
        with StateStore(state_db) as state:
            asset = AssetRef(id=asset_id, filename=Path(rel).name,
                             capture_dt=capture_dt, added_dt=None, size=None)
            state.register(asset, rel)

    def _convert_with_state(self, job, config, state_db, *, monkeypatch, calls, rel):
        def fake_ensure(path, capture_dt, **kw):
            calls.append((path, capture_dt))
            return MetadataOutcome.STAMPED

        monkeypatch.setattr(op.md, "ensure_capture_date", fake_ensure)
        return op._convert_all(
            job, config, echo=lambda *a, **k: None,
            convert_fn=fake_convert(25 * 1024 * 1024),
            probe_fn=lambda p, r: make_probe(rel=rel, size=25 * 1024 * 1024,
                                             width=1080, height=1920),
            state_db=state_db,
        )

    def test_stamps_the_output_with_the_manifest_capture_date(self, job, config, monkeypatch, tmp_path):
        rel, _ = seed_selected(job, config)
        state_db = self._state_db(tmp_path)
        capture_dt = datetime(2019, 10, 25, 15, 7, 50, tzinfo=timezone.utc)
        self._register(state_db, "OLD", capture_dt, rel)
        calls = []

        totals = self._convert_with_state(job, config, state_db, monkeypatch=monkeypatch,
                                          calls=calls, rel=rel)

        assert totals.converted == 1
        assert len(calls) == 1
        stamped_path, stamped_dt = calls[0]
        assert stamped_path == config.work_path(vo.flat_name(rel))
        assert stamped_dt == capture_dt

    def test_no_stamp_call_when_manifest_has_no_row_for_the_asset(self, job, config, monkeypatch, tmp_path):
        rel, _ = seed_selected(job, config)
        state_db = self._state_db(tmp_path)
        with StateStore(state_db):
            pass                            # DB created, but no row for "OLD"
        calls = []

        self._convert_with_state(job, config, state_db, monkeypatch=monkeypatch,
                                 calls=calls, rel=rel)

        assert calls == []

    def test_no_stamp_call_when_state_db_not_given(self, job, config, monkeypatch):
        rel, _ = seed_selected(job, config)
        calls = []

        def fake_ensure(path, capture_dt, **kw):
            calls.append(1)

        monkeypatch.setattr(op.md, "ensure_capture_date", fake_ensure)
        totals = op._convert_all(
            job, config, echo=lambda *a, **k: None,
            convert_fn=fake_convert(25 * 1024 * 1024),
            probe_fn=lambda p, r: make_probe(rel=rel, size=25 * 1024 * 1024,
                                             width=1080, height=1920),
        )

        assert totals.converted == 1
        assert calls == []

    def test_conversion_still_succeeds_when_stamping_reports_failed(self, job, config, monkeypatch, tmp_path):
        rel, _ = seed_selected(job, config)
        state_db = self._state_db(tmp_path)
        self._register(state_db, "OLD", datetime(2019, 10, 25, tzinfo=timezone.utc), rel)

        monkeypatch.setattr(op.md, "ensure_capture_date",
                            lambda *a, **kw: MetadataOutcome.FAILED)
        totals = op._convert_all(
            job, config, echo=lambda *a, **k: None,
            convert_fn=fake_convert(25 * 1024 * 1024),
            probe_fn=lambda p, r: make_probe(rel=rel, size=25 * 1024 * 1024,
                                             width=1080, height=1920),
            state_db=state_db,
        )

        assert totals.converted == 1
        assert job.get(rel)["status"] == oj.STATUS_CONVERTED


class TestInferDateFn:
    """_convert_all's other date source — video-optimise-external has no sync
    manifest at all, so it passes infer_date_fn (the filename/folder ladder)
    instead of state_db. The two are independent: video-optimise never sets
    infer_date_fn, video-optimise-external never sets state_db."""

    def test_infer_date_fn_used_when_no_manifest(self, job, config, monkeypatch):
        rel, _ = seed_selected(job, config)
        calls = []

        def fake_ensure(path, capture_dt, **kw):
            calls.append((path, capture_dt))
            return MetadataOutcome.STAMPED

        monkeypatch.setattr(op.md, "ensure_capture_date", fake_ensure)
        inferred = datetime(2019, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

        totals = op._convert_all(
            job, config, echo=lambda *a, **k: None,
            convert_fn=fake_convert(25 * 1024 * 1024),
            probe_fn=lambda p, r: make_probe(rel=rel, size=25 * 1024 * 1024,
                                             width=1080, height=1920),
            infer_date_fn=lambda src, rel: inferred,
        )

        assert totals.converted == 1
        assert len(calls) == 1
        stamped_path, stamped_dt = calls[0]
        assert stamped_path == config.work_path(vo.flat_name(rel))
        assert stamped_dt == inferred

    def test_infer_date_fn_receives_the_source_path_and_rel(self, job, config, monkeypatch):
        rel, _ = seed_selected(job, config)
        seen = []
        monkeypatch.setattr(op.md, "ensure_capture_date",
                            lambda *a, **kw: MetadataOutcome.STAMPED)

        def infer(src, r):
            seen.append((src, r))
            return datetime(2019, 10, 25, tzinfo=timezone.utc)

        op._convert_all(
            job, config, echo=lambda *a, **k: None,
            convert_fn=fake_convert(25 * 1024 * 1024),
            probe_fn=lambda p, r: make_probe(rel=rel, size=25 * 1024 * 1024,
                                             width=1080, height=1920),
            infer_date_fn=infer,
        )

        assert seen == [(config.output_root / rel, rel)]

    def test_no_stamp_call_when_infer_date_fn_finds_nothing(self, job, config, monkeypatch):
        rel, _ = seed_selected(job, config)
        calls = []
        monkeypatch.setattr(op.md, "ensure_capture_date",
                            lambda *a, **kw: calls.append(1) or MetadataOutcome.STAMPED)

        op._convert_all(
            job, config, echo=lambda *a, **k: None,
            convert_fn=fake_convert(25 * 1024 * 1024),
            probe_fn=lambda p, r: make_probe(rel=rel, size=25 * 1024 * 1024,
                                             width=1080, height=1920),
            infer_date_fn=lambda src, rel: None,
        )

        assert calls == []

    def test_manifest_takes_priority_over_infer_date_fn_when_both_given(self, job, config, monkeypatch, tmp_path):
        # Not a real combination any current caller produces, but the
        # priority order is deliberate: a manifest's recorded capture_dt is
        # authoritative where it exists, an inferred one is a fallback guess.
        rel, _ = seed_selected(job, config)
        from icloud_photo_sync.config import AppConfig
        from icloud_photo_sync.models import AssetRef
        from icloud_photo_sync.state import StateStore

        app = AppConfig.create("t@e.com", tmp_path / "tree", config_root=tmp_path / "cfg")
        manifest_dt = datetime(2019, 10, 25, tzinfo=timezone.utc)
        with StateStore(app.state_db) as state:
            state.register(AssetRef(id="OLD", filename=Path(rel).name,
                                    capture_dt=manifest_dt, added_dt=None, size=None), rel)

        calls = []
        monkeypatch.setattr(op.md, "ensure_capture_date",
                            lambda path, dt, **kw: calls.append(dt) or MetadataOutcome.STAMPED)

        op._convert_all(
            job, config, echo=lambda *a, **k: None,
            convert_fn=fake_convert(25 * 1024 * 1024),
            probe_fn=lambda p, r: make_probe(rel=rel, size=25 * 1024 * 1024,
                                             width=1080, height=1920),
            state_db=app.state_db,
            infer_date_fn=lambda src, rel: datetime(2000, 1, 1, tzinfo=timezone.utc),
        )

        assert calls == [manifest_dt]


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


class TestRetryColourMismatch:
    """Unlike a failed delete, a colour mismatch is never retried automatically
    — it usually means the file has a genuine, repeatable problem, and blindly
    re-attempting it every run would burn hours re-encoding the same 4K clip
    forever. ``--retry-colour-mismatch`` exists for the one case retrying is
    right: a fix to the encoding policy itself. Without this, classify() being
    fixed is inert for a real file, because job.add() refuses to move a
    terminal-state row back to 'selected' — colour_mismatch is terminal.
    """

    def _seed_mismatch(self, job, config, rel="2024/05/IMG_1.MOV"):
        source = make_probe(rel=rel, size=140 * 1024 * 1024)
        job.add(rel, asset_id="OLD", src_bytes=source.size,
                src_probe=op._probe_dict(source),
                plan=op._encode_dict(vo.choose_encode(source)))
        job.mark_skipped(rel, oj.STATUS_COLOUR_MISMATCH,
                         "HLG HDR 8-bit → HLG HDR 10-bit")
        return rel

    def test_retry_sends_mismatched_rows_back_to_selected(self, job, config):
        rel = self._seed_mismatch(job, config)
        assert job.pending_conversion() == []          # the stranding, demonstrated
        assert job.retry_colour_mismatch() == 1
        row = job.get(rel)
        assert row["status"] == oj.STATUS_SELECTED
        assert row["out_rel"] is None and row["error"] is None

    def test_retry_clears_the_stale_plan_but_keeps_the_probe(self, job, config):
        # Observed live: without this, the row bounced straight back to
        # colour_mismatch on the very next real run, still carrying the
        # pre-fix plan (main10/p010le) that caused the original failure — the
        # retry looked like it worked (status flipped) but silently reproduced
        # the exact bug it existed to fix.
        rel = self._seed_mismatch(job, config)
        job.retry_colour_mismatch()
        row = job.get(rel)
        assert row["plan"] is None
        assert oj.probe_of(row) is not None

    def test_retry_is_a_noop_with_nothing_mismatched(self, job, config):
        seed_selected(job, config)
        assert job.retry_colour_mismatch() == 0

    def test_a_retried_row_can_convert_on_the_next_attempt(self, job, config):
        rel = self._seed_mismatch(job, config)
        job.retry_colour_mismatch()
        assert [r["rel"] for r in job.pending_conversion()] == [rel]

    def test_other_terminal_statuses_are_untouched(self, job, config):
        # Scoped narrowly: a run declining to retry not_worth_it/rejected rows
        # is a design choice, not an oversight this test should let slip.
        mismatch = self._seed_mismatch(job, config, rel="2024/05/A.MOV")
        other = seed_selected(job, config, rel="2024/05/B.MOV")[0]
        job.mark_skipped(other, oj.STATUS_NOT_WORTH_IT, "too small a gain")
        assert job.retry_colour_mismatch() == 1
        assert job.get(mismatch)["status"] == oj.STATUS_SELECTED
        assert job.get(other)["status"] == oj.STATUS_NOT_WORTH_IT

    def test_not_wired_into_the_ordinary_swap_failed_reset(self, job, config):
        # The two resets are independent: fixing failed deletes must never
        # silently also retry colour mismatches.
        rel = self._seed_mismatch(job, config)
        job.reset_swap_failed()
        assert job.get(rel)["status"] == oj.STATUS_COLOUR_MISMATCH


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


# --- local cleanup is asked once, and never eats the replacement --------------


class TestLocalCleanupIsIdempotent:
    """The bug this pins down destroyed real files.

    After a successful cleanup the original is in the Trash and the conversion
    has taken its place — and on a case-insensitive volume that is the *same
    path*. A second run re-offered every swapped row, found a file at the
    original's path, and trashed it: the optimised video, not the original.
    """

    def _trash(self, seen):
        from icloud_photo_sync.trash import TrashResult

        def fn(paths):
            seen.extend(paths)
            for p in paths:
                p.unlink(missing_ok=True)
            return [TrashResult(path=p, ok=True) for p in paths]
        return fn

    def _swapped(self, job, config, rel="2024/05/IMG_1.MOV"):
        seed_converted(job, config, rel=rel)
        job.mark_uploaded(rel, "NEW")
        job.mark_swapped(rel)
        return rel

    def _cleanup(self, job, config, seen, *, yes=True):
        return op._cleanup_locals(job, config, echo=lambda *a, **k: None,
                                  confirm=lambda *a, **k: yes,
                                  trash_fn=self._trash(seen))

    def test_a_second_run_does_not_trash_the_replacement(self, job, config):
        rel = self._swapped(job, config)
        first: list[Path] = []
        self._cleanup(job, config, first)
        assert [p.name for p in first] == ["IMG_1.MOV"]
        placed = (config.output_root / rel).with_suffix(".mov")
        assert placed.is_file()

        second: list[Path] = []
        self._cleanup(job, config, second)
        assert second == []                    # nothing offered, nothing trashed
        assert placed.is_file()                # the optimised file survives

    def test_the_row_is_stamped_so_it_is_never_asked_again(self, job, config):
        rel = self._swapped(job, config)
        self._cleanup(job, config, [])
        assert job.get(rel)["local_done"]
        assert job.needs_local_cleanup() == []

    def test_declining_leaves_the_row_to_ask_again(self, job, config):
        rel = self._swapped(job, config)
        seen: list[Path] = []
        self._cleanup(job, config, seen, yes=False)
        assert seen == []
        assert job.get(rel)["local_done"] is None
        assert len(job.needs_local_cleanup()) == 1

    def test_a_file_the_size_of_the_conversion_is_never_trashed(self, job, config):
        # The size guard on its own, with the marker deliberately cleared: even
        # a row that somehow came back round must not eat the replacement.
        rel = self._swapped(job, config)
        self._cleanup(job, config, [])
        job._conn.execute("UPDATE jobs SET local_done = NULL WHERE rel = ?", (rel,))
        job.flush()
        seen: list[Path] = []
        self._cleanup(job, config, seen)
        assert seen == []
        assert (config.output_root / rel).with_suffix(".mov").is_file()

    def test_an_already_missing_original_is_stamped_not_re_offered(self, job, config):
        rel = self._swapped(job, config)
        (config.output_root / rel).unlink()
        seen: list[Path] = []
        self._cleanup(job, config, seen)
        assert seen == [] and job.get(rel)["local_done"]

    def test_an_unexpected_size_is_left_alone_and_re_offered(self, job, config):
        # Neither trashed nor stamped: the run says so and moves on, and a
        # later run can still deal with it once the user knows.
        rel = seed_converted(job, config)
        job.mark_uploaded(rel, "NEW")
        job.mark_swapped(rel)
        with open(config.output_root / rel, "wb") as fh:
            fh.truncate(12345)                 # neither recorded size
        seen: list[Path] = []
        from icloud_photo_sync.trash import TrashResult

        def trash(paths):
            seen.extend(paths)
            return [TrashResult(path=p, ok=True) for p in paths]

        op._cleanup_locals(job, config, echo=lambda *a, **k: None,
                           confirm=lambda *a, **k: True, trash_fn=trash)
        assert seen == []
        assert job.get(rel)["local_done"] is None
        assert (config.output_root / rel).is_file()

    def test_the_original_is_still_trashed_when_it_really_is_the_original(self, job, config):
        rel = self._swapped(job, config)
        seen: list[Path] = []
        assert self._cleanup(job, config, seen) == 0
        assert [p.name for p in seen] == ["IMG_1.MOV"]


class TestLocalState:
    """Which of the two files is sitting at the original's path."""

    def _row(self, src, out):
        return {"src_bytes": src, "out_bytes": out}

    def _file(self, tmp_path, size):
        p = tmp_path / "a.mov"
        with open(p, "wb") as fh:
            fh.truncate(size)
        return p

    def test_the_original_is_recognised(self, tmp_path):
        assert op._local_state(self._row(100, 10),
                               self._file(tmp_path, 100)) == "original"

    def test_the_replacement_is_recognised_and_never_offered(self, tmp_path):
        assert op._local_state(self._row(100, 10),
                               self._file(tmp_path, 10)) == "replaced"

    def test_a_missing_file_means_the_local_side_is_finished(self, tmp_path):
        assert op._local_state(self._row(100, 10), tmp_path / "no.mov") == "gone"

    def test_an_unexpected_size_is_neither(self, tmp_path):
        # Something outside this tool changed it: not safe to trash, not
        # honest to call finished.
        assert op._local_state(self._row(100, 10),
                               self._file(tmp_path, 55)) == "unknown"


# --- run_optimise: --dry-run must not mutate anything --------------------------


class TestDryRunTouchesNothing:
    """Discovered while verifying --retry-colour-mismatch: --dry-run resolves no
    Apple ID (same as --offline), so it opens a DIFFERENT, offline-keyed job
    database than an authenticated run of the same command against the same
    folder. Before this fix, the job-store preamble (restart/backfill/
    reset_swap_failed/retry_colour_mismatch/migrate_work_dir) ran unconditionally
    — so "--dry-run --retry-colour-mismatch" silently mutated the wrong database
    while reporting nothing changed, and "--dry-run --restart" would have wiped
    it. Both contradict --dry-run's own contract.
    """

    def _run(self, config, **overrides):
        import types
        kwargs = dict(
            icloud=None, session_factory=lambda app: None,
            echo=lambda *a, **k: None, progress=None,
            confirm=lambda *a, **k: False, prompt=lambda *a, **k: "",
            choose=lambda *a, **k: set(), compare=lambda *a, **k: None,
            cancel=None, probe_fn=lambda p, r: None, convert_fn=lambda *a, **k: None,
        )
        kwargs.update(overrides)
        return op.run_optimise(config, **kwargs)

    def test_dry_run_does_not_reset_colour_mismatch(self, monkeypatch, job, config):
        monkeypatch.setattr(op.tc, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(op.tc, "encoder_available", lambda name=None: True)
        rel = "2024/05/IMG_1.MOV"
        source = make_probe(rel=rel, size=140 * 1024 * 1024)
        job.add(rel, asset_id="OLD", src_bytes=source.size,
                src_probe=op._probe_dict(source),
                plan=op._encode_dict(vo.choose_encode(source)))
        job.mark_skipped(rel, oj.STATUS_COLOUR_MISMATCH, "HLG HDR 8-bit -> 10-bit")
        job.flush()

        dry_config = dataclasses.replace(config, dry_run=True,
                                         retry_colour_mismatch=True)
        self._run(dry_config)
        assert job.get(rel)["status"] == oj.STATUS_COLOUR_MISMATCH

    def test_dry_run_does_not_clear_restart(self, monkeypatch, job, config):
        monkeypatch.setattr(op.tc, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(op.tc, "encoder_available", lambda name=None: True)
        rel = seed_converted(job, config)
        job.flush()

        dry_config = dataclasses.replace(config, dry_run=True, restart=True)
        self._run(dry_config)
        assert job.get(rel) is not None          # --restart did not wipe the store

    def test_a_real_run_does_apply_the_retry(self, monkeypatch, job, config):
        # The other half of the fence: outside --dry-run this must still work,
        # exactly as TestRetryColourMismatch already covers unit-level.
        monkeypatch.setattr(op.tc, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(op.tc, "encoder_available", lambda name=None: True)
        rel = "2024/05/IMG_1.MOV"
        source = make_probe(rel=rel, size=140 * 1024 * 1024)
        job.add(rel, asset_id="OLD", src_bytes=source.size,
                src_probe=op._probe_dict(source),
                plan=op._encode_dict(vo.choose_encode(source)))
        job.mark_skipped(rel, oj.STATUS_COLOUR_MISMATCH, "HLG HDR 8-bit -> 10-bit")
        job.flush()

        real_config = dataclasses.replace(config, dry_run=False,
                                          retry_colour_mismatch=True)
        self._run(real_config)
        assert job.get(rel)["status"] == oj.STATUS_SELECTED


# --- run_optimise_external: the local-only sibling, no iCloud at all -----------


class TestRunOptimiseExternal:
    """The whole point of this command: everything video-optimise does
    locally, none of what it does with iCloud. Every test here either proves
    the local behaviour matches, or proves iCloud is never touched."""

    def _config(self, tmp_path, **overrides):
        root = (tmp_path / "tree").resolve()
        root.mkdir(exist_ok=True)
        return VideoOptimiseConfig.create(
            root, config_root=tmp_path / "cfg",
            db_prefix="video-optimise-external", **overrides,
        )

    def _seed_source(self, config, name, size=300 * 1024 * 1024):
        src = config.output_root / name
        src.parent.mkdir(parents=True, exist_ok=True)
        _sparse(src, size)
        return src

    @staticmethod
    def _default_trash_fn(paths):
        from icloud_photo_sync.trash import TrashResult
        for p in paths:
            p.unlink(missing_ok=True)
        return [TrashResult(path=p, ok=True) for p in paths]

    def _run(self, config, monkeypatch, *, choose=None, compare=None,
             convert_fn=None, probe_fn=None, confirm=None, trash_fn=None):
        monkeypatch.setattr(op.tc, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(op.tc, "encoder_available", lambda name=None: True)
        return op.run_optimise_external(
            config,
            echo=lambda *a, **k: None,
            confirm=confirm or (lambda *a, **k: True),
            choose=choose or (lambda items, **kw: {0}),
            compare=compare or (lambda *a, **k: orv.SelectionOutcome(
                set(), orv.CHOICE_APPROVE_ALL)),
            trash_fn=trash_fn or self._default_trash_fn,
            convert_fn=convert_fn, probe_fn=probe_fn,
        )

    def test_no_videos_found(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        code = self._run(config, monkeypatch)
        assert code == 0

    def test_full_local_flow_converts_stamps_and_offers_trash(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        name = "VID-20191025-WA0003.mp4"
        self._seed_source(config, name)

        # probe_fn serves double duty in the real flow: once to scan the
        # (large) source during the initial plan, again to read back the
        # (small) converted output afterwards — same callable, different
        # paths, so it has to distinguish them by location.
        source_probe = make_probe(rel=name, size=300 * 1024 * 1024,
                                  width=3840, height=2160)
        out = make_probe(rel=name, size=25 * 1024 * 1024, width=1080, height=1920)

        def probe_fn(p, r):
            return out if config.work_dir in p.parents else source_probe

        stamped = []

        def fake_ensure(path, capture_dt, **kw):
            stamped.append((path, capture_dt))
            return MetadataOutcome.STAMPED

        monkeypatch.setattr(op.md, "ensure_capture_date", fake_ensure)

        trashed = []

        def trash_fn(paths):
            trashed.extend(paths)
            from icloud_photo_sync.trash import TrashResult
            for p in paths:
                p.unlink(missing_ok=True)
            return [TrashResult(path=p, ok=True) for p in paths]

        code = self._run(
            config, monkeypatch,
            convert_fn=fake_convert(25 * 1024 * 1024),
            probe_fn=probe_fn,
            trash_fn=trash_fn,
        )

        assert code == 0
        # The source had no embedded date; the filename ladder found one.
        assert len(stamped) == 1
        stamped_path, stamped_dt = stamped[0]
        assert stamped_dt == datetime(2019, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

        # Original moved to Trash, optimised copy took its place.
        assert trashed == [config.output_root / name]
        placed = (config.output_root / name).with_suffix(".mov")
        assert placed.exists()
        assert not (config.output_root / name).exists()

    def test_keep_originals_moves_into_originals_folder_preserving_subpath(self, tmp_path, monkeypatch):
        config = self._config(tmp_path, keep_originals=True)
        rel = "2020/08/clip.mp4"
        self._seed_source(config, rel)

        source_probe = make_probe(rel=rel, size=300 * 1024 * 1024, width=3840, height=2160)
        out = make_probe(rel=rel, size=25 * 1024 * 1024, width=1080, height=1920)

        def probe_fn(p, r):
            return out if config.work_dir in p.parents else source_probe

        monkeypatch.setattr(op.md, "ensure_capture_date",
                            lambda *a, **kw: MetadataOutcome.STAMPED)

        code = self._run(
            config, monkeypatch,
            convert_fn=fake_convert(25 * 1024 * 1024),
            probe_fn=probe_fn,
        )

        assert code == 0
        # The original went to originals/2020/08/clip.mp4, not the Trash --
        # same subpath it had under output_root, just rooted differently.
        kept = config.output_root / "originals" / rel
        assert kept.exists()
        assert not (config.output_root / rel).exists()
        # The optimised copy still took the original's place.
        placed = (config.output_root / rel).with_suffix(".mov")
        assert placed.exists()

    def test_originals_folder_is_excluded_from_a_later_scan(self, tmp_path, monkeypatch):
        # Without this, a re-run would find the retired originals sitting in
        # originals/ and offer to convert them all over again.
        config = self._config(tmp_path)
        kept = config.output_root / "originals" / "2020" / "08" / "old.mov"
        kept.parent.mkdir(parents=True)
        _sparse(kept, 300 * 1024 * 1024)

        found = op.scan_videos(config.output_root)
        assert found == []

    def test_nothing_worth_optimising_is_a_clean_no_op(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        self._seed_source(config, "tiny.mp4", size=1024)  # well under --min-size

        code = self._run(config, monkeypatch)
        assert code == 0

    def test_dry_run_does_not_convert_or_mutate(self, tmp_path, monkeypatch):
        config = self._config(tmp_path, dry_run=True)
        name = "clip.mp4"
        self._seed_source(config, name)

        calls = []
        code = self._run(
            config, monkeypatch,
            convert_fn=lambda *a, **k: calls.append(1),
            probe_fn=lambda p, r: make_probe(rel=name, size=280 * 1024 * 1024,
                                             width=3840, height=2160),
        )

        assert code == 0
        assert calls == []
        assert (config.output_root / name).exists()  # untouched

    def test_never_imports_or_calls_icloud_machinery(self):
        # run_optimise_external's own source: no arm(), no ArmedICloud, no
        # ICloudDeleteConfig ever constructed on this path. A grep-level
        # guarantee, not just "the tests happened not to need one".
        import inspect
        src = inspect.getsource(op.run_optimise_external)
        for forbidden in ("arm(", "ArmedICloud", "ICloudDeleteConfig", "SessionManager"):
            assert forbidden not in src, f"{forbidden!r} must never appear in run_optimise_external"
