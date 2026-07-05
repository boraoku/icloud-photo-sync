from datetime import datetime, timezone

import pytest

from icloud_photo_sync.models import AssetRef
from icloud_photo_sync.state import StateStore


def _asset(id="a1", filename="IMG.HEIC", size=100):
    return AssetRef(
        id=id,
        filename=filename,
        capture_dt=datetime(2026, 7, 1, tzinfo=timezone.utc),
        added_dt=datetime(2026, 7, 2, tzinfo=timezone.utc),
        size=size,
    )


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


def test_register_and_get(store):
    store.register(_asset(), "2026/07/IMG.HEIC")
    row = store.get("a1")
    assert row["status"] == "pending"
    assert row["dest_path"] == "2026/07/IMG.HEIC"
    assert row["expected_size"] == 100
    assert row["bytes_done"] == 0


def test_register_preserves_dest_and_status(store):
    store.register(_asset(), "2026/07/IMG.HEIC")
    store.mark_completed("a1", 100)
    # Re-register with a different filename (metadata refresh) must NOT reset.
    store.register(_asset(filename="RENAMED.HEIC"), "9999/99/RENAMED.HEIC")
    row = store.get("a1")
    assert row["status"] == "completed"           # not reset to pending
    assert row["dest_path"] == "2026/07/IMG.HEIC"  # original destination kept
    assert row["filename"] == "RENAMED.HEIC"        # metadata did refresh


def test_partial_then_completed(store):
    store.register(_asset(), "p")
    store.record_partial("a1", 40)
    assert store.get("a1")["bytes_done"] == 40
    store.mark_completed("a1", 100)
    row = store.get("a1")
    assert row["status"] == "completed"
    assert row["bytes_done"] == 100


def test_mark_failed(store):
    store.register(_asset(), "p")
    store.mark_failed("a1", "boom")
    row = store.get("a1")
    assert row["status"] == "failed"
    assert row["error"] == "boom"


def test_path_owner(store):
    store.register(_asset(id="a1"), "2026/07/IMG.HEIC")
    assert store.path_owner("2026/07/IMG.HEIC") == "a1"
    assert store.path_owner("2026/07/OTHER.HEIC") is None


def test_counts_and_bytes(store):
    store.register(_asset(id="a1"), "p1")
    store.register(_asset(id="a2"), "p2")
    store.register(_asset(id="a3"), "p3")
    store.mark_completed("a1", 100)
    store.mark_completed("a2", 50)
    store.mark_failed("a3", "x")
    counts = store.counts()
    assert counts["completed"] == 2
    assert counts["failed"] == 1
    assert counts["pending"] == 0
    assert counts["total"] == 3
    assert store.total_bytes_completed() == 150
    assert len(store.iter_failed()) == 1


def test_meta_roundtrip(store):
    assert store.get_meta("missing") is None
    store.set_meta("last_full_pass_at", "2026-06-28T00:00:00+00:00")
    assert store.get_meta("last_full_pass_at") == "2026-06-28T00:00:00+00:00"
    assert store.get_meta("schema_version") == "1"


def test_register_null_metadata_does_not_clobber(store):
    """A later enumeration that fails to read size/dates must never erase
    known-good values — the stored size is the integrity ground truth."""
    store.register(_asset(size=100), "p")
    degraded = AssetRef(id="a1", filename="IMG.HEIC", capture_dt=None,
                        added_dt=None, size=None)
    store.register(degraded, "ignored")
    row = store.get("a1")
    assert row["expected_size"] == 100
    assert row["capture_dt"] is not None
    assert row["added_dt"] is not None


def test_update_dest(store):
    store.register(_asset(), "old/path.HEIC")
    store.update_dest("a1", "new/path.HEIC")
    assert store.get("a1")["dest_path"] == "new/path.HEIC"
