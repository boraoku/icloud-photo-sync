"""Tests for photo-optimise-external: Phase A (date recovery) and its
hand-off into Phase B (the existing local-clean flow, called as-is).

Phase A reads embedded dates in bulk, so these stub
``metadata.read_embedded_capture_dates`` (the one subprocess seam) rather
than the per-file reader — no exiftool is needed to run this file.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from icloud_photo_sync import date_cache as dc
from icloud_photo_sync import photo_optimise_external as poe
from icloud_photo_sync.config import LocalCleanConfig
from icloud_photo_sync.metadata import MetadataOutcome

DT = datetime(2019, 10, 25, 12, 0, 0, tzinfo=timezone.utc)


def _write(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _config(tmp_path, **overrides):
    root = tmp_path / "tree"
    root.mkdir(exist_ok=True)
    return LocalCleanConfig.create(root, config_root=tmp_path / "cfg", **overrides)


def _embedded(monkeypatch, mapping=None, default=None, calls=None):
    """Stub the bulk read. ``mapping`` is by filename; anything absent gets
    ``default``. ``calls`` records each batch, so a test can assert batching
    happened at all rather than only that the answers were right."""
    mapping = mapping or {}

    def read(paths, **kw):
        if calls is not None:
            calls.append(list(paths))
        return {p: mapping.get(p.name, default) for p in paths}

    monkeypatch.setattr(poe.md, "read_embedded_capture_dates", read)


def _stamps(monkeypatch, outcome=MetadataOutcome.STAMPED, recorded=None):
    def ensure(path, capture_dt, **kw):
        if recorded is not None:
            recorded.append((path, capture_dt, kw))
        return outcome

    monkeypatch.setattr(poe.md, "ensure_capture_date", ensure)


# --- scan_photos ---------------------------------------------------------------


def test_scan_finds_every_image_suffix_regardless_of_size(tmp_path):
    _write(tmp_path / "a.heic")
    _write(tmp_path / "b.HEIF")
    _write(tmp_path / "c.jpg", b"x" * 5_000_000)   # large — still included
    _write(tmp_path / "d.png")
    _write(tmp_path / "e.tiff")
    _write(tmp_path / "movie.mov")                  # excluded: not an image

    found = poe.scan_photos(tmp_path)
    assert sorted(ph.rel for ph in found) == ["a.heic", "b.HEIF", "c.jpg", "d.png", "e.tiff"]


def test_scan_returns_sorted_by_rel(tmp_path):
    _write(tmp_path / "z.jpg")
    _write(tmp_path / "a.jpg")
    assert [ph.rel for ph in poe.scan_photos(tmp_path)] == ["a.jpg", "z.jpg"]


def test_scan_records_the_fingerprint_the_cache_keys_on(tmp_path):
    _write(tmp_path / "a.jpg", b"x" * 17)
    photo = poe.scan_photos(tmp_path)[0]
    st = os.stat(tmp_path / "a.jpg")
    assert photo.size == 17 == st.st_size
    assert photo.mtime_ns == st.st_mtime_ns


def test_scan_empty_folder(tmp_path):
    assert poe.scan_photos(tmp_path) == []


# --- recover_dates ---------------------------------------------------------------


class TestRecoverDates:
    def test_already_has_date_is_never_written(self, tmp_path, monkeypatch):
        _write(tmp_path / "photo.jpg")
        _embedded(monkeypatch, {"photo.jpg": datetime(2019, 1, 1, tzinfo=timezone.utc)})
        recorded = []
        _stamps(monkeypatch, recorded=recorded)

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert summary.already_had_date == 1
        assert summary.stamped == 0
        assert recorded == []

    def test_filename_inferred_date_is_stamped(self, tmp_path, monkeypatch):
        _write(tmp_path / "IMG-20191025-WA0007.jpg")
        _embedded(monkeypatch)                       # nothing embedded
        recorded = []
        _stamps(monkeypatch, recorded=recorded)

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert summary.stamped_from_filename == 1
        assert summary.stamped_from_folder == 0
        path, dt, _kw = recorded[0]
        assert path == tmp_path / "IMG-20191025-WA0007.jpg"
        assert dt == DT

    def test_folder_inferred_date_is_stamped(self, tmp_path, monkeypatch):
        _write(tmp_path / "2019" / "10" / "holiday.jpg")
        _embedded(monkeypatch)
        recorded = []
        _stamps(monkeypatch, recorded=recorded)

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert summary.stamped_from_folder == 1
        assert summary.stamped_from_filename == 0
        assert recorded[0][1] == datetime(2019, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_stamping_declares_the_date_already_known_absent(self, tmp_path, monkeypatch):
        # The batch read IS the read-before-write check; re-reading the same
        # file inside ensure_capture_date would be the per-file cost this
        # whole change exists to remove.
        _write(tmp_path / "IMG-20191025-WA0007.jpg")
        _embedded(monkeypatch)
        recorded = []
        _stamps(monkeypatch, recorded=recorded)

        poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert recorded[0][2].get("known_absent") is True

    def test_nothing_found_is_unknown_and_never_written(self, tmp_path, monkeypatch):
        _write(tmp_path / "photo.jpg")
        _embedded(monkeypatch)
        recorded = []
        _stamps(monkeypatch, recorded=recorded)

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert summary.unknown == 1
        assert recorded == []

    def test_dry_run_counts_without_writing(self, tmp_path, monkeypatch):
        _write(tmp_path / "IMG-20191025-WA0007.jpg")
        _embedded(monkeypatch)
        recorded = []
        _stamps(monkeypatch, recorded=recorded)

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None, dry_run=True)

        assert summary.stamped_from_filename == 1
        assert recorded == []

    def test_tool_unavailable_is_tracked_and_warned_once(self, tmp_path, monkeypatch):
        _write(tmp_path / "IMG-20191025-WA0001.jpg")
        _write(tmp_path / "IMG-20191025-WA0002.jpg")
        _embedded(monkeypatch)
        _stamps(monkeypatch, outcome=MetadataOutcome.TOOL_UNAVAILABLE)
        warnings = []

        summary = poe.recover_dates(tmp_path, echo=lambda t, **k: warnings.append(t))

        assert summary.tool_unavailable == 2
        assert summary.stamped == 0
        assert len([w for w in warnings if "not found" in w]) == 1

    def test_failed_outcome_is_tracked(self, tmp_path, monkeypatch):
        _write(tmp_path / "IMG-20191025-WA0001.jpg")
        _embedded(monkeypatch)
        _stamps(monkeypatch, outcome=MetadataOutcome.FAILED)

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert summary.failed == 1
        assert summary.stamped == 0

    def test_empty_folder_returns_zeroed_summary(self, tmp_path):
        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)
        assert summary.total == 0

    def test_progress_bar_is_driven_when_a_factory_is_given(self, tmp_path, monkeypatch):
        # The original bug: the bar existed but the CLI never passed a
        # factory, so a 47k-photo run printed nothing for over an hour.
        for i in range(3):
            _write(tmp_path / f"p{i}.jpg")
        _embedded(monkeypatch, default=DT)
        updates = []

        class Bar:
            def __init__(self, **kw):
                self.kw = kw
                bars.append(self)

            def update(self, n):
                updates.append(n)

            def close(self):
                self.closed = True

        bars = []
        poe.recover_dates(tmp_path, echo=lambda *a, **k: None, progress=Bar)

        assert bars and bars[0].kw["total"] == 3
        assert updates == [1, 1, 1]
        assert bars[0].closed is True

    def test_reads_are_batched_not_one_call_per_photo(self, tmp_path, monkeypatch):
        for i in range(5):
            _write(tmp_path / f"p{i}.jpg")
        calls = []
        _embedded(monkeypatch, default=DT, calls=calls)

        poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert len(calls) == 1          # one batch, not five
        assert len(calls[0]) == 5

    def test_batches_are_chunked_at_the_configured_size(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poe.md, "EXIFTOOL_BATCH", 2)
        for i in range(5):
            _write(tmp_path / f"p{i}.jpg")
        calls = []
        _embedded(monkeypatch, default=DT, calls=calls)

        poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert [len(c) for c in calls] == [2, 2, 1]


# --- resume cache ---------------------------------------------------------------


class TestResume:
    def _cache(self, tmp_path):
        return dc.DateCache(tmp_path / "dates.db")

    def test_second_run_skips_everything_already_settled(self, tmp_path, monkeypatch):
        root = tmp_path / "tree"
        for i in range(3):
            _write(root / f"p{i}.jpg")
        _embedded(monkeypatch, default=DT)

        with self._cache(tmp_path) as cache:
            first = poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)
        assert first.already_had_date == 3
        assert first.skipped_cached == 0

        calls = []
        _embedded(monkeypatch, default=DT, calls=calls)
        with self._cache(tmp_path) as cache:
            second = poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)

        assert calls == []                       # exiftool never invoked at all
        assert second.skipped_cached == 3
        assert second.already_had_date == 3      # totals still describe the library

    def test_a_resumed_run_counts_each_photo_exactly_once(self, tmp_path, monkeypatch):
        # skipped_cached annotates the buckets above it rather than being a
        # bucket itself; counting it again in the total made every resumed
        # run report double the photos that exist.
        root = tmp_path / "tree"
        for i in range(3):
            _write(root / f"p{i}.jpg")
        _embedded(monkeypatch, default=DT)
        with self._cache(tmp_path) as cache:
            poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)
        with self._cache(tmp_path) as cache:
            second = poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)

        assert second.skipped_cached == 3
        assert second.total == 3

    def test_a_partly_cached_run_still_totals_correctly(self, tmp_path, monkeypatch):
        root = tmp_path / "tree"
        for i in range(3):
            _write(root / f"p{i}.jpg")
        _embedded(monkeypatch, default=DT)
        with self._cache(tmp_path) as cache:
            poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)

        _write(root / "p3.jpg")                  # one genuinely new photo
        with self._cache(tmp_path) as cache:
            mixed = poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)

        assert mixed.skipped_cached == 3
        assert mixed.total == 4

    def test_an_edited_photo_is_rechecked(self, tmp_path, monkeypatch):
        root = tmp_path / "tree"
        _write(root / "p.jpg")
        _embedded(monkeypatch, default=DT)
        with self._cache(tmp_path) as cache:
            poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)

        _write(root / "p.jpg", b"different bytes entirely")   # size + mtime change
        calls = []
        _embedded(monkeypatch, default=DT, calls=calls)
        with self._cache(tmp_path) as cache:
            again = poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)

        assert len(calls) == 1
        assert again.skipped_cached == 0

    def test_a_stamped_photo_is_not_reread_next_run(self, tmp_path, monkeypatch):
        # The trap: stamping rewrites the file and resets its mtime, so
        # caching the PRE-write fingerprint would miss every single time and
        # re-read exactly the files the run just fixed.
        root = tmp_path / "tree"
        photo = root / "IMG-20191025-WA0007.jpg"
        _write(photo)
        _embedded(monkeypatch)

        def ensure(path, capture_dt, **kw):
            path.write_bytes(b"rewritten, different length")   # as a real stamp does
            ts = capture_dt.timestamp()
            os.utime(path, (ts, ts))
            return MetadataOutcome.STAMPED

        monkeypatch.setattr(poe.md, "ensure_capture_date", ensure)
        with self._cache(tmp_path) as cache:
            first = poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)
        assert first.stamped_from_filename == 1

        calls = []
        _embedded(monkeypatch, calls=calls)
        with self._cache(tmp_path) as cache:
            second = poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)

        assert calls == []
        assert second.skipped_cached == 1
        assert second.stamped_from_filename == 1

    def test_failures_are_always_retried(self, tmp_path, monkeypatch):
        root = tmp_path / "tree"
        _write(root / "IMG-20191025-WA0007.jpg")
        _embedded(monkeypatch)
        _stamps(monkeypatch, outcome=MetadataOutcome.TOOL_UNAVAILABLE)
        with self._cache(tmp_path) as cache:
            poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)

        calls = []
        _embedded(monkeypatch, calls=calls)
        _stamps(monkeypatch, outcome=MetadataOutcome.STAMPED)
        with self._cache(tmp_path) as cache:
            second = poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)

        assert len(calls) == 1                   # re-read, not written off
        assert second.stamped_from_filename == 1

    def test_dry_run_records_nothing(self, tmp_path, monkeypatch):
        root = tmp_path / "tree"
        _write(root / "p.jpg")
        _embedded(monkeypatch, default=DT)
        with self._cache(tmp_path) as cache:
            poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache, dry_run=True)
            assert cache.count() == 0

    def test_an_interrupt_keeps_completed_batches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poe.md, "EXIFTOOL_BATCH", 2)
        root = tmp_path / "tree"
        for i in range(6):
            _write(root / f"p{i}.jpg")

        seen = []

        def read(paths, **kw):
            seen.append(list(paths))
            if len(seen) == 3:                   # die during the third batch
                raise KeyboardInterrupt
            return {p: DT for p in paths}

        monkeypatch.setattr(poe.md, "read_embedded_capture_dates", read)
        with self._cache(tmp_path) as cache:
            summary = poe.recover_dates(root, echo=lambda *a, **k: None, cache=cache)

        assert summary.interrupted is True
        assert summary.already_had_date == 4     # the two completed batches
        with self._cache(tmp_path) as cache:
            assert cache.count() == 4            # and they survived the interrupt


# --- run_photo_optimise_external: the two-phase hand-off ----------------------


class TestRunPhotoOptimiseExternal:
    def test_dry_run_never_calls_local_clean(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _write(config.output_root / "photo.jpg")
        _embedded(monkeypatch)
        calls = []
        monkeypatch.setattr(poe, "run_local_clean", lambda cfg: calls.append(cfg) or 0)

        code = poe.run_photo_optimise_external(config, echo=lambda *a, **k: None, dry_run=True)

        assert code == 0
        assert calls == []

    def test_real_run_calls_local_clean_after_phase_a(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _write(config.output_root / "IMG-20191025-WA0007.jpg")
        _embedded(monkeypatch)
        recorded = []
        _stamps(monkeypatch, recorded=recorded)
        calls = []
        monkeypatch.setattr(poe, "run_local_clean", lambda cfg: calls.append(cfg) or 3)

        code = poe.run_photo_optimise_external(config, echo=lambda *a, **k: None)

        assert recorded[0][0] == config.output_root / "IMG-20191025-WA0007.jpg"
        assert calls == [config]
        assert code == 3                 # local-clean's own exit code passes through

    def test_recheck_dates_clears_the_cache_first(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _write(config.output_root / "p.jpg")
        _embedded(monkeypatch, default=DT)
        monkeypatch.setattr(poe, "run_local_clean", lambda cfg: 0)
        poe.run_photo_optimise_external(config, echo=lambda *a, **k: None)
        with dc.DateCache(config.date_cache_db) as cache:
            assert cache.count() == 1

        calls = []
        _embedded(monkeypatch, default=DT, calls=calls)
        rechecking = _config(tmp_path, recheck_dates=True)
        poe.run_photo_optimise_external(rechecking, echo=lambda *a, **k: None)

        assert len(calls) == 1           # re-read despite the earlier verdict

    def test_an_interrupt_skips_local_clean_and_exits_130(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _write(config.output_root / "p.jpg")

        def read(paths, **kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(poe.md, "read_embedded_capture_dates", read)
        calls = []
        monkeypatch.setattr(poe, "run_local_clean", lambda cfg: calls.append(cfg) or 0)

        code = poe.run_photo_optimise_external(config, echo=lambda *a, **k: None)

        assert code == 130
        assert calls == []               # never fell through into phase B

    def test_never_resolves_an_apple_id(self):
        # Structural guarantee: neither phase in this module's own source ever
        # constructs an Apple-ID-bearing object or calls iCloud auth.
        import inspect
        src = inspect.getsource(poe)
        for forbidden in ("arm(", "ArmedICloud", "SessionManager", "PyiCloudService"):
            assert forbidden not in src, f"{forbidden!r} must never appear in photo_optimise_external"
