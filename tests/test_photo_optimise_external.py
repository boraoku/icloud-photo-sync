"""Tests for photo-optimise-external: Phase A (date recovery) and its
hand-off into Phase B (the existing local-clean flow, called as-is).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from icloud_photo_sync import photo_optimise_external as poe
from icloud_photo_sync.config import LocalCleanConfig
from icloud_photo_sync.metadata import MetadataOutcome, SOURCE_EMBEDDED, SOURCE_FILENAME, SOURCE_FOLDER


def _write(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _config(tmp_path, **overrides):
    root = tmp_path / "tree"
    root.mkdir(exist_ok=True)
    return LocalCleanConfig.create(root, config_root=tmp_path / "cfg", **overrides)


# --- scan_photos ---------------------------------------------------------------


def test_scan_finds_every_image_suffix_regardless_of_size(tmp_path):
    _write(tmp_path / "a.heic")
    _write(tmp_path / "b.HEIF")
    _write(tmp_path / "c.jpg", b"x" * 5_000_000)   # large — still included
    _write(tmp_path / "d.png")
    _write(tmp_path / "e.tiff")
    _write(tmp_path / "movie.mov")                  # excluded: not an image

    found = poe.scan_photos(tmp_path)
    rels = sorted(rel for _, rel in found)
    assert rels == ["a.heic", "b.HEIF", "c.jpg", "d.png", "e.tiff"]


def test_scan_returns_sorted_by_rel(tmp_path):
    _write(tmp_path / "z.jpg")
    _write(tmp_path / "a.jpg")
    found = poe.scan_photos(tmp_path)
    assert [rel for _, rel in found] == ["a.jpg", "z.jpg"]


def test_scan_empty_folder(tmp_path):
    assert poe.scan_photos(tmp_path) == []


# --- recover_dates ---------------------------------------------------------------


class TestRecoverDates:
    def test_already_has_date_is_never_written(self, tmp_path, monkeypatch):
        _write(tmp_path / "photo.jpg")
        calls = []
        monkeypatch.setattr(
            poe.md, "infer_capture_date",
            lambda path, rel: (datetime(2019, 1, 1, tzinfo=timezone.utc), SOURCE_EMBEDDED),
        )
        monkeypatch.setattr(poe.md, "ensure_capture_date",
                            lambda *a, **kw: calls.append(1) or MetadataOutcome.STAMPED)

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert summary.already_had_date == 1
        assert summary.stamped == 0
        assert calls == []

    def test_filename_inferred_date_is_stamped(self, tmp_path, monkeypatch):
        _write(tmp_path / "IMG-20191025-WA0007.jpg")
        stamped = []
        inferred = datetime(2019, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(poe.md, "infer_capture_date",
                            lambda path, rel: (inferred, SOURCE_FILENAME))
        monkeypatch.setattr(
            poe.md, "ensure_capture_date",
            lambda path, dt, **kw: stamped.append((path, dt)) or MetadataOutcome.STAMPED,
        )

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert summary.stamped_from_filename == 1
        assert summary.stamped_from_folder == 0
        assert stamped == [(tmp_path / "IMG-20191025-WA0007.jpg", inferred)]

    def test_folder_inferred_date_is_stamped(self, tmp_path, monkeypatch):
        _write(tmp_path / "2019" / "10" / "holiday.jpg")
        inferred = datetime(2019, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(poe.md, "infer_capture_date",
                            lambda path, rel: (inferred, SOURCE_FOLDER))
        monkeypatch.setattr(poe.md, "ensure_capture_date",
                            lambda *a, **kw: MetadataOutcome.STAMPED)

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert summary.stamped_from_folder == 1
        assert summary.stamped_from_filename == 0

    def test_nothing_found_is_unknown_and_never_written(self, tmp_path, monkeypatch):
        _write(tmp_path / "photo.jpg")
        calls = []
        monkeypatch.setattr(poe.md, "infer_capture_date",
                            lambda path, rel: (None, poe.md.SOURCE_UNKNOWN))
        monkeypatch.setattr(poe.md, "ensure_capture_date",
                            lambda *a, **kw: calls.append(1) or MetadataOutcome.STAMPED)

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert summary.unknown == 1
        assert calls == []

    def test_dry_run_counts_without_writing(self, tmp_path, monkeypatch):
        _write(tmp_path / "IMG-20191025-WA0007.jpg")
        calls = []
        monkeypatch.setattr(
            poe.md, "infer_capture_date",
            lambda path, rel: (datetime(2019, 10, 25, tzinfo=timezone.utc), SOURCE_FILENAME),
        )
        monkeypatch.setattr(poe.md, "ensure_capture_date",
                            lambda *a, **kw: calls.append(1) or MetadataOutcome.STAMPED)

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None, dry_run=True)

        assert summary.stamped_from_filename == 1
        assert calls == []               # counted, but nothing was actually written

    def test_tool_unavailable_is_tracked_and_warned_once(self, tmp_path, monkeypatch):
        _write(tmp_path / "a.jpg")
        _write(tmp_path / "b.jpg")
        warnings = []
        monkeypatch.setattr(
            poe.md, "infer_capture_date",
            lambda path, rel: (datetime(2019, 10, 25, tzinfo=timezone.utc), SOURCE_FILENAME),
        )
        monkeypatch.setattr(poe.md, "ensure_capture_date",
                            lambda *a, **kw: MetadataOutcome.TOOL_UNAVAILABLE)

        def echo(text, **kw):
            warnings.append(text)

        summary = poe.recover_dates(tmp_path, echo=echo)

        assert summary.tool_unavailable == 2
        assert summary.stamped == 0
        tool_lines = [w for w in warnings if "not found" in w]
        assert len(tool_lines) == 1        # one notice, not one per file

    def test_failed_outcome_is_tracked(self, tmp_path, monkeypatch):
        _write(tmp_path / "a.jpg")
        monkeypatch.setattr(
            poe.md, "infer_capture_date",
            lambda path, rel: (datetime(2019, 10, 25, tzinfo=timezone.utc), SOURCE_FILENAME),
        )
        monkeypatch.setattr(poe.md, "ensure_capture_date",
                            lambda *a, **kw: MetadataOutcome.FAILED)

        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)

        assert summary.failed == 1
        assert summary.stamped == 0

    def test_empty_folder_returns_zeroed_summary(self, tmp_path):
        summary = poe.recover_dates(tmp_path, echo=lambda *a, **k: None)
        assert summary.total == 0


# --- run_photo_optimise_external: the two-phase hand-off ----------------------


class TestRunPhotoOptimiseExternal:
    def test_dry_run_never_calls_local_clean(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _write(config.output_root / "photo.jpg")
        monkeypatch.setattr(poe.md, "infer_capture_date",
                            lambda path, rel: (None, poe.md.SOURCE_UNKNOWN))
        calls = []
        monkeypatch.setattr(poe, "run_local_clean", lambda cfg: calls.append(cfg) or 0)

        code = poe.run_photo_optimise_external(config, echo=lambda *a, **k: None, dry_run=True)

        assert code == 0
        assert calls == []

    def test_real_run_calls_local_clean_after_phase_a(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _write(config.output_root / "IMG-20191025-WA0007.jpg")
        stamped = []
        monkeypatch.setattr(
            poe.md, "infer_capture_date",
            lambda path, rel: (datetime(2019, 10, 25, tzinfo=timezone.utc), SOURCE_FILENAME),
        )
        monkeypatch.setattr(
            poe.md, "ensure_capture_date",
            lambda path, dt, **kw: stamped.append(path) or MetadataOutcome.STAMPED,
        )
        calls = []
        monkeypatch.setattr(poe, "run_local_clean", lambda cfg: calls.append(cfg) or 3)

        code = poe.run_photo_optimise_external(config, echo=lambda *a, **k: None)

        assert stamped == [config.output_root / "IMG-20191025-WA0007.jpg"]
        assert calls == [config]
        assert code == 3                 # local-clean's own exit code passes through

    def test_never_resolves_an_apple_id(self):
        # Structural guarantee: neither phase in this module's own source ever
        # constructs an Apple-ID-bearing object or calls iCloud auth.
        import inspect
        src = inspect.getsource(poe)
        for forbidden in ("arm(", "ArmedICloud", "SessionManager", "PyiCloudService"):
            assert forbidden not in src, f"{forbidden!r} must never appear in photo_optimise_external"
