from datetime import datetime, timezone

import pytest

from icloud_photo_sync.models import AssetRef
from icloud_photo_sync.state import SCHEMA_VERSION, StateStore


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
    assert store.get_meta("schema_version") == SCHEMA_VERSION


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


def test_batched_commits_persist_across_reopen(tmp_path):
    """Mutations are batched; close() must flush them durably."""
    db = tmp_path / "reopen.db"
    s1 = StateStore(db)
    s1.register(_asset(), "2026/07/IMG.HEIC")
    s1.mark_completed("a1", 100)
    s1.close()
    s2 = StateStore(db)
    try:
        row = s2.get("a1")
        assert row["status"] == "completed"
        assert row["bytes_done"] == 100
    finally:
        s2.close()


# --- deletion bookkeeping (icloud-delete) ------------------------------------


def test_rows_for_dest_exposes_every_claimant(store):
    """path_owner's LIMIT 1 hides duplicates; deletion has to see them.

    Two assets claiming one file is real, not hypothetical: the live manifest
    for this tree has exactly one such pair.
    """
    store.register(_asset(id="a1"), "2024/01/IMG_3691.HEIC")
    store.register(_asset(id="a2"), "2024/01/IMG_3691.HEIC")

    assert store.path_owner("2024/01/IMG_3691.HEIC") in {"a1", "a2"}   # picks one
    rows = store.rows_for_dest("2024/01/IMG_3691.HEIC")
    assert sorted(r["id"] for r in rows) == ["a1", "a2"]               # sees both


def test_rows_for_dest_matches_across_unicode_normalisation(store):
    composed = "2026/07/café.HEIC"          # NFC
    decomposed = "2026/07/café.HEIC"       # NFD — same filename to macOS
    store.register(_asset(), decomposed)

    assert [r["id"] for r in store.rows_for_dest(composed)] == ["a1"]
    assert [r["id"] for r in store.rows_for_dest(decomposed)] == ["a1"]


def test_rows_for_dest_is_empty_for_untracked_paths(store):
    assert store.rows_for_dest("2026/07/never-downloaded.jpg") == []


def test_colliding_dest_paths_folds_case_and_normalisation(store):
    store.register(_asset(id="a1"), "2026/07/IMG.HEIC")
    store.register(_asset(id="a2"), "2026/07/img.heic")   # one file on APFS
    store.register(_asset(id="a3"), "2026/07/other.HEIC")

    collisions = store.colliding_dest_paths()
    assert "2026/07/img.heic" in collisions
    assert "2026/07/other.heic" not in collisions


def test_remote_deletions_are_recorded_and_idempotent(store):
    store.record_remote_deletion(
        asset_id="a1", dest_path="2026/07/IMG.HEIC", filename="IMG.HEIC",
        capture_dt="2026-07-01T00:00:00+00:00", expected_size=100,
        receipt_path="/tmp/receipt.jsonl", verified_at="2026-07-01T01:00:00+00:00",
    )
    assert store.remote_deleted_ids(["a1", "a2"]) == {"a1"}
    assert store.remote_deletion_count() == 1

    store.record_remote_deletion(          # re-run must not double-count
        asset_id="a1", dest_path="2026/07/IMG.HEIC", filename="IMG.HEIC",
        capture_dt=None, expected_size=100, receipt_path=None, verified_at=None,
    )
    assert store.remote_deletion_count() == 1


def test_remote_deletion_survives_a_crash_without_close(tmp_path):
    """The only local record of an irreversible remote effect is never batched."""
    db = tmp_path / "receipts.db"
    s1 = StateStore(db)
    s1.record_remote_deletion(
        asset_id="a1", dest_path="p", filename="f", capture_dt=None,
        expected_size=None, receipt_path=None, verified_at=None,
    )
    # deliberately no close()/flush() — simulate a hard kill
    s2 = StateStore(db)
    try:
        assert s2.remote_deleted_ids(["a1"]) == {"a1"}
    finally:
        s2.close()
        s1.close()


def test_identity_round_trip(store):
    assert store.identity() == {}
    store.stamp_identity(apple_id="me@example.com", output_root="/Volumes/B0/x",
                         dsid="12345", account_name="Me")
    assert store.identity() == {
        "apple_id": "me@example.com", "output_root": "/Volumes/B0/x",
        "dsid": "12345", "account_name": "Me",
    }


def test_identity_rejects_unknown_fields(store):
    with pytest.raises(KeyError):
        store.stamp_identity(password="hunter2")


def test_read_only_refuses_to_create_a_manifest(tmp_path):
    """An auto-created empty DB means wrong account or wrong folder — say so."""
    from icloud_photo_sync.errors import ManifestMissingError

    missing = tmp_path / "nope.db"
    with pytest.raises(ManifestMissingError):
        StateStore(missing, read_only=True)
    assert not missing.exists()


def test_read_only_reads_but_cannot_write(tmp_path):
    import sqlite3

    db = tmp_path / "ro.db"
    writer = StateStore(db)
    writer.register(_asset(), "2026/07/IMG.HEIC")
    writer.mark_completed("a1", 100)
    writer.close()

    reader = StateStore(db, read_only=True)
    try:
        assert reader.get("a1")["status"] == "completed"
        with pytest.raises(sqlite3.OperationalError):
            reader.mark_failed("a1", "should not be possible")
    finally:
        reader.close()


def test_v1_manifest_gains_the_new_table_on_open(tmp_path):
    """A database written before this feature must open and upgrade in place."""
    import sqlite3

    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE assets (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, capture_dt TEXT,
            added_dt TEXT, dest_path TEXT, expected_size INTEGER,
            bytes_done INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','completed','failed')),
            error TEXT, updated_at TEXT
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '1');
        INSERT INTO assets (id, filename, dest_path, expected_size, status)
             VALUES ('old1', 'IMG.HEIC', '2020/01/IMG.HEIC', 42, 'completed');
        """
    )
    conn.commit()
    conn.close()

    store = StateStore(db)
    try:
        assert store.get_meta("schema_version") == "2"
        assert [r["id"] for r in store.rows_for_dest("2020/01/IMG.HEIC")] == ["old1"]
        assert store.remote_deletion_count() == 0          # table now exists
    finally:
        store.close()


def test_read_only_opens_a_wal_manifest_that_lost_its_sidecars(tmp_path):
    """A reboot or a restore leaves the .db without -wal/-shm.

    SQLite cannot open that with mode=ro at all (it must create the shared
    memory file), so the read-only path has to cope — while still refusing
    writes.
    """
    import shutil
    import sqlite3

    original = tmp_path / "orig.db"
    writer = StateStore(original)
    writer.register(_asset(), "2026/07/IMG.HEIC")
    writer.mark_completed("a1", 100)
    writer.close()

    bare = tmp_path / "restored.db"
    shutil.copy(original, bare)          # the .db alone, as a backup would hold it
    assert not (tmp_path / "restored.db-shm").exists()

    reader = StateStore(bare, read_only=True)
    try:
        assert reader.get("a1")["status"] == "completed"
        with pytest.raises(sqlite3.OperationalError):
            reader.mark_failed("a1", "still refused")
    finally:
        reader.close()


def test_a_v1_manifest_read_only_reports_no_deletions(tmp_path):
    """Planning opens the manifest read-only, so it cannot create the table."""
    import sqlite3

    db = tmp_path / "v1ro.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE assets (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, capture_dt TEXT,
            added_dt TEXT, dest_path TEXT, expected_size INTEGER,
            bytes_done INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','completed','failed')),
            error TEXT, updated_at TEXT
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO assets (id, filename, dest_path, expected_size, status)
             VALUES ('old1', 'IMG.HEIC', '2020/01/IMG.HEIC', 42, 'completed');
        """
    )
    conn.commit()
    conn.close()

    store = StateStore(db, read_only=True)
    try:
        assert store.remote_deleted_ids(["old1"]) == set()
        assert store.remote_deletion_count() == 0
    finally:
        store.close()
