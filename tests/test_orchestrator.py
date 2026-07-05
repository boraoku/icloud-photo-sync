"""Destination-resolution policy tests: adoption, collision, re-pointing.

These exercise Orchestrator._resolve_dest / _on_disk_ok against a real
StateStore and filesystem; the iCloud client and downloader are inert stubs
because resolution never touches them.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from icloud_photo_sync.config import AppConfig
from icloud_photo_sync.models import AssetRef
from icloud_photo_sync.orchestrator import Orchestrator
from icloud_photo_sync.paths import PathResolver
from icloud_photo_sync.state import StateStore

CAPTURE = datetime(2026, 7, 4, tzinfo=timezone.utc)


def make_asset(id="a1", filename="IMG_0001.HEIC", size=100):
    return AssetRef(id=id, filename=filename, capture_dt=CAPTURE,
                    added_dt=CAPTURE, size=size)


@pytest.fixture
def orch(tmp_path):
    cfg = AppConfig.create("t@e.com", tmp_path / "out",
                           config_root=tmp_path / "cfg", show_progress=False)
    state = StateStore(cfg.state_db)
    o = Orchestrator(
        client=object(), state=state, paths=PathResolver(cfg.output_root),
        downloader=object(), config=cfg,
    )
    yield o
    state.close()


def test_adopts_existing_file_with_matching_size(orch):
    """Lost DB / pre-populated folder: an identical-size file at the canonical
    path is adopted instead of re-downloaded under a -N suffix."""
    asset = make_asset(size=100)
    abs_dest = orch.paths.absolute(Path("2026/07/IMG_0001.HEIC"))
    abs_dest.parent.mkdir(parents=True, exist_ok=True)
    abs_dest.write_bytes(b"x" * 100)

    rel = orch._resolve_dest(asset)

    assert rel == Path("2026/07/IMG_0001.HEIC")
    row = orch.state.get("a1")
    assert row["status"] == "completed"


def test_nonmatching_existing_file_gets_suffix(orch):
    """A different-size file at the canonical path is left alone; the asset
    goes to a suffixed name."""
    asset = make_asset(size=100)
    abs_dest = orch.paths.absolute(Path("2026/07/IMG_0001.HEIC"))
    abs_dest.parent.mkdir(parents=True, exist_ok=True)
    abs_dest.write_bytes(b"x" * 55)  # wrong size — not ours to touch

    rel = orch._resolve_dest(asset)

    assert rel == Path("2026/07/IMG_0001-1.HEIC")
    assert abs_dest.read_bytes() == b"x" * 55


def test_repoints_when_foreign_file_occupies_recorded_dest(orch):
    """A pending asset whose recorded destination got occupied by a foreign
    file is re-pointed, and its .part progress moves along."""
    asset = make_asset(size=100)
    rel1 = orch._resolve_dest(asset)  # registers 2026/07/IMG_0001.HEIC
    abs1 = orch.paths.absolute(rel1)
    abs1.parent.mkdir(parents=True, exist_ok=True)
    part1 = abs1.with_name(abs1.name + ".part")
    part1.write_bytes(b"p" * 40)      # in-flight progress
    abs1.write_bytes(b"u" * 77)       # user drops their own file at our dest

    rel2 = orch._resolve_dest(asset)

    assert rel2 != rel1
    assert orch.state.get("a1")["dest_path"] == rel2.as_posix()
    assert abs1.read_bytes() == b"u" * 77  # untouched
    assert not part1.exists()
    part2 = orch.paths.absolute(rel2).with_name(rel2.name + ".part")
    assert part2.read_bytes() == b"p" * 40  # progress carried over


def test_completed_dest_is_reused_verbatim(orch):
    asset = make_asset(size=100)
    rel1 = orch._resolve_dest(asset)
    orch.state.mark_completed("a1", 100)
    assert orch._resolve_dest(asset) == rel1


def test_on_disk_ok_falls_back_to_recorded_size(orch):
    """asset.size=None must not bless a truncated file when the manifest
    knows the real size."""
    asset = make_asset(size=100)
    rel = orch._resolve_dest(asset)
    abs_dest = orch.paths.absolute(rel)
    abs_dest.parent.mkdir(parents=True, exist_ok=True)
    abs_dest.write_bytes(b"t" * 40)  # truncated
    orch.state.mark_completed("a1", 100)

    degraded = AssetRef(id="a1", filename="IMG_0001.HEIC", capture_dt=CAPTURE,
                        added_dt=CAPTURE, size=None)
    row = orch.state.get("a1")
    assert orch._on_disk_ok(degraded, row) is False

    abs_dest.write_bytes(b"g" * 100)  # full size again
    assert orch._on_disk_ok(degraded, row) is True
