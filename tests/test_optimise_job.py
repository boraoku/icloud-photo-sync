import sqlite3

import pytest

from icloud_photo_sync.optimise_job import (
    ALL_STATUSES,
    SCHEMA_VERSION,
    STATUS_CONVERTED,
    STATUS_SELECTED,
    STATUS_SWAPPED,
    OptimiseJob,
    out_probe_of,
    plan_of,
    probe_of,
)


def _probe(**over):
    base = {"width": 1920, "height": 1080, "fps": 30.0, "duration": 10.0}
    base.update(over)
    return base


def _plan(**over):
    base = {"width": 1080, "height": 1920, "fps": 30.0, "bitrate": 6_000_000}
    base.update(over)
    return base


@pytest.fixture
def job(tmp_path):
    j = OptimiseJob(tmp_path / "job.db")
    yield j
    j.close()


def _add(job, rel="a.mov", **over):
    kwargs = dict(
        asset_id="asset-1",
        src_bytes=1000,
        src_probe=_probe(),
        plan=_plan(),
    )
    kwargs.update(over)
    job.add(rel, **kwargs)


# --- schema / identity -----------------------------------------------------


def test_schema_created_on_open(job):
    row = job.get("nope")
    assert row is None  # table exists, just empty
    assert job.get_meta("schema_version") == SCHEMA_VERSION


def test_reopen_does_not_wipe_existing_rows(tmp_path):
    db = tmp_path / "job.db"
    j1 = OptimiseJob(db)
    _add(j1)
    j1.close()

    j2 = OptimiseJob(db)
    try:
        row = j2.get("a.mov")
        assert row is not None
        assert row["status"] == "selected"
        assert j2.get_meta("schema_version") == SCHEMA_VERSION
    finally:
        j2.close()


def test_identity_round_trip(job):
    assert job.identity() == {}
    job.stamp_identity(apple_id="me@example.com", output_root="/Volumes/B0/x")
    assert job.identity() == {
        "apple_id": "me@example.com",
        "output_root": "/Volumes/B0/x",
    }


def test_identity_rejects_unknown_fields(job):
    with pytest.raises(KeyError):
        job.stamp_identity(dsid="12345")


def test_meta_roundtrip(job):
    assert job.get_meta("missing") is None
    job.set_meta("k", "v")
    assert job.get_meta("k") == "v"


# --- add() ------------------------------------------------------------------


def test_add_inserts_selected_row_with_json_roundtrip(job):
    _add(job, src_probe=_probe(width=42), plan=_plan(bitrate=123))
    row = job.get("a.mov")
    assert row["status"] == "selected"
    assert row["asset_id"] == "asset-1"
    assert row["src_bytes"] == 1000
    assert probe_of(row) == _probe(width=42)
    assert plan_of(row) == _plan(bitrate=123)
    assert out_probe_of(row) is None


def test_add_on_converted_row_does_not_reset_it(job):
    _add(job)
    job.mark_converted(
        "a.mov", out_rel="out/a.mov", out_bytes=100, out_probe=_probe(width=10)
    )
    _add(job, src_bytes=9999)  # a later selection re-scan sees it again
    row = job.get("a.mov")
    assert row["status"] == "converted"
    assert row["out_rel"] == "out/a.mov"
    assert row["out_bytes"] == 100


@pytest.mark.parametrize(
    "status_setup",
    [
        "rejected",
        "not_worth_it",
        "colour_mismatch",
        "uploaded",
        "swapped",
    ],
)
def test_add_does_not_reset_other_terminal_states(job, status_setup):
    _add(job)
    if status_setup == "rejected":
        job.mark_rejected("a.mov")
    elif status_setup in ("not_worth_it", "colour_mismatch"):
        job.mark_skipped("a.mov", status_setup, error="reason")
    elif status_setup == "uploaded":
        job.mark_converted("a.mov", out_rel="o", out_bytes=1, out_probe=None)
        job.mark_uploaded("a.mov", "new-asset")
    elif status_setup == "swapped":
        job.mark_converted("a.mov", out_rel="o", out_bytes=1, out_probe=None)
        job.mark_uploaded("a.mov", "new-asset")
        job.mark_swapped("a.mov")

    _add(job)
    assert job.get("a.mov")["status"] == status_setup


def test_add_on_convert_failed_row_resets_to_selected(job):
    _add(job)
    job.mark_skipped("a.mov", "convert_failed", error="ffmpeg exploded")
    _add(job, src_bytes=2222)
    row = job.get("a.mov")
    assert row["status"] == "selected"
    assert row["src_bytes"] == 2222
    assert row["error"] is None


def test_add_after_convert_failed_clears_stale_output_columns(job):
    _add(job)
    job.mark_converted("a.mov", out_rel="o", out_bytes=1, out_probe=_probe())
    job.mark_skipped("a.mov", "convert_failed", error="retry me")
    _add(job)
    row = job.get("a.mov")
    assert row["status"] == "selected"
    assert row["out_rel"] is None
    assert row["out_bytes"] is None
    assert row["out_probe"] is None


# --- mark_converted / mark_skipped / mark_rejected --------------------------


def test_mark_converted(job):
    _add(job)
    job.mark_converted("a.mov", out_rel="out/a.mov", out_bytes=500, out_probe=_probe(width=7))
    row = job.get("a.mov")
    assert row["status"] == "converted"
    assert row["out_rel"] == "out/a.mov"
    assert row["out_bytes"] == 500
    assert out_probe_of(row) == _probe(width=7)


@pytest.mark.parametrize("status", ["not_worth_it", "colour_mismatch", "convert_failed"])
def test_mark_skipped_accepts_known_statuses(job, status):
    _add(job)
    job.mark_skipped("a.mov", status, error="why")
    row = job.get("a.mov")
    assert row["status"] == status
    assert row["error"] == "why"


def test_mark_skipped_rejects_unknown_status(job):
    _add(job)
    with pytest.raises(ValueError):
        job.mark_skipped("a.mov", "swapped")


def test_mark_rejected(job):
    _add(job)
    job.mark_rejected("a.mov")
    assert job.get("a.mov")["status"] == "rejected"


# --- mark_uploaded / mark_swapped / mark_swap_failed ------------------------


def test_mark_uploaded_empty_id_raises(job):
    _add(job)
    job.mark_converted("a.mov", out_rel="o", out_bytes=1, out_probe=None)
    with pytest.raises(ValueError):
        job.mark_uploaded("a.mov", "")


def test_mark_uploaded_sets_status_and_asset_id(job):
    _add(job)
    job.mark_converted("a.mov", out_rel="o", out_bytes=1, out_probe=None)
    job.mark_uploaded("a.mov", "new-asset-123")
    row = job.get("a.mov")
    assert row["status"] == "uploaded"
    assert row["new_asset_id"] == "new-asset-123"


def test_mark_swapped_without_new_asset_id_raises(job):
    _add(job)
    job.mark_converted("a.mov", out_rel="o", out_bytes=1, out_probe=None)
    with pytest.raises(ValueError):
        job.mark_swapped("a.mov")
    # status must be unchanged by the failed attempt
    assert job.get("a.mov")["status"] == "converted"


def test_mark_swapped_for_unknown_rel_raises(job):
    with pytest.raises(ValueError):
        job.mark_swapped("does-not-exist.mov")


def test_mark_swapped_after_upload_succeeds(job):
    _add(job)
    job.mark_converted("a.mov", out_rel="o", out_bytes=1, out_probe=None)
    job.mark_uploaded("a.mov", "new-asset-123")
    job.mark_swapped("a.mov")
    row = job.get("a.mov")
    assert row["status"] == "swapped"
    assert row["new_asset_id"] == "new-asset-123"


def test_mark_swap_failed(job):
    _add(job)
    job.mark_converted("a.mov", out_rel="o", out_bytes=1, out_probe=None)
    job.mark_uploaded("a.mov", "new-asset-123")
    job.mark_swap_failed("a.mov", "delete timed out")
    row = job.get("a.mov")
    assert row["status"] == "swap_failed"
    assert row["error"] == "delete timed out"


# --- queries -----------------------------------------------------------


def test_pending_conversion_returns_only_selected_largest_first(job):
    _add(job, rel="small.mov", src_bytes=100)
    _add(job, rel="big.mov", src_bytes=900)
    _add(job, rel="mid.mov", src_bytes=500)
    job.mark_rejected("mid.mov")
    rels = [r["rel"] for r in job.pending_conversion()]
    assert rels == ["big.mov", "small.mov"]


def test_converted_returns_only_converted_largest_first(job):
    _add(job, rel="x.mov", src_bytes=100)
    _add(job, rel="y.mov", src_bytes=900)
    job.mark_converted("x.mov", out_rel="o1", out_bytes=1, out_probe=None)
    job.mark_converted("y.mov", out_rel="o2", out_bytes=1, out_probe=None)
    rels = [r["rel"] for r in job.converted()]
    assert rels == ["y.mov", "x.mov"]


def test_approved_includes_converted_and_uploaded(job):
    _add(job, rel="c.mov")
    _add(job, rel="u.mov")
    _add(job, rel="r.mov")
    job.mark_converted("c.mov", out_rel="o", out_bytes=1, out_probe=None)
    job.mark_converted("u.mov", out_rel="o", out_bytes=1, out_probe=None)
    job.mark_uploaded("u.mov", "asset-x")
    job.mark_rejected("r.mov")
    rels = {r["rel"] for r in job.approved()}
    assert rels == {"c.mov", "u.mov"}


def test_swapped_query(job):
    _add(job, rel="s.mov")
    job.mark_converted("s.mov", out_rel="o", out_bytes=1, out_probe=None)
    job.mark_uploaded("s.mov", "asset-x")
    job.mark_swapped("s.mov")
    _add(job, rel="other.mov")
    rels = [r["rel"] for r in job.swapped()]
    assert rels == ["s.mov"]


def test_by_status_multiple(job):
    _add(job, rel="a.mov")
    _add(job, rel="b.mov")
    job.mark_skipped("a.mov", "not_worth_it")
    job.mark_skipped("b.mov", "colour_mismatch")
    rels = {r["rel"] for r in job.by_status("not_worth_it", "colour_mismatch")}
    assert rels == {"a.mov", "b.mov"}


def test_by_status_empty_args_returns_empty(job):
    _add(job)
    assert job.by_status() == []


# --- reporting -----------------------------------------------------------


def test_counts_includes_every_known_status_zeroed(job):
    _add(job)
    counts = job.counts()
    assert set(counts) == set(ALL_STATUSES)
    assert counts["selected"] == 1
    for status in ALL_STATUSES:
        if status != "selected":
            assert counts[status] == 0


def test_totals_sums_only_swapped_rows(job):
    _add(job, rel="s.mov", src_bytes=1000)
    job.mark_converted("s.mov", out_rel="o", out_bytes=300, out_probe=None)
    job.mark_uploaded("s.mov", "asset-x")
    job.mark_swapped("s.mov")

    _add(job, rel="c.mov", src_bytes=5000)
    job.mark_converted("c.mov", out_rel="o2", out_bytes=1000, out_probe=None)

    totals = job.totals()
    assert totals == {"src_bytes": 1000, "out_bytes": 300, "freed": 700}


def test_totals_empty_is_zero(job):
    assert job.totals() == {"src_bytes": 0, "out_bytes": 0, "freed": 0}


# --- malformed JSON --------------------------------------------------------


def test_probe_of_tolerates_malformed_json(job):
    _add(job)
    job._conn.execute(
        "UPDATE jobs SET src_probe = ? WHERE rel = ?", ("{not json", "a.mov")
    )
    job.flush()
    row = job.get("a.mov")
    assert probe_of(row) is None


def test_plan_of_tolerates_null(job):
    _add(job, plan=None)
    row = job.get("a.mov")
    assert plan_of(row) is None


def test_probe_of_tolerates_non_dict_json(job):
    _add(job)
    job._conn.execute(
        "UPDATE jobs SET src_probe = ? WHERE rel = ?", ("[1, 2, 3]", "a.mov")
    )
    job.flush()
    row = job.get("a.mov")
    assert probe_of(row) is None


# --- clear -----------------------------------------------------------------


def test_clear_empties_jobs_but_leaves_meta(job):
    _add(job)
    job.stamp_identity(apple_id="me@example.com")
    job.clear()
    assert job.get("a.mov") is None
    assert job.counts()["selected"] == 0
    assert job.identity() == {"apple_id": "me@example.com"}
    assert job.get_meta("schema_version") == SCHEMA_VERSION


# --- read-only ---------------------------------------------------------------


def test_read_only_refuses_to_create_a_store(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        OptimiseJob(missing, read_only=True)
    assert not missing.exists()


def test_read_only_reads_but_cannot_write(tmp_path):
    db = tmp_path / "ro.db"
    writer = OptimiseJob(db)
    _add(writer)
    writer.close()

    reader = OptimiseJob(db, read_only=True)
    try:
        assert reader.get("a.mov")["status"] == "selected"
        with pytest.raises(sqlite3.OperationalError):
            reader.mark_rejected("a.mov")
    finally:
        reader.close()


# --- batching / durability --------------------------------------------------


def test_batched_commits_persist_across_reopen(tmp_path):
    db = tmp_path / "reopen.db"
    j1 = OptimiseJob(db)
    _add(j1)
    j1.mark_converted("a.mov", out_rel="o", out_bytes=1, out_probe=None)
    j1.close()

    j2 = OptimiseJob(db)
    try:
        row = j2.get("a.mov")
        assert row["status"] == "converted"
    finally:
        j2.close()


def test_status_check_constraint_rejects_bad_value(job):
    with pytest.raises(sqlite3.IntegrityError):
        job._conn.execute(
            "INSERT INTO jobs (rel, src_bytes, status) VALUES (?, ?, ?)",
            ("bogus.mov", 1, "not-a-real-status"),
        )


# --- case-insensitive keying (schema 2) --------------------------------------


class TestCaseInsensitiveKey:
    """The photo volume is case-insensitive and the output container is always
    QuickTime, so a video converted from ``IMG_1.MOV`` lands back as
    ``IMG_1.mov`` — one file on disk. Keyed case-sensitively those became two
    rows, and a re-run saw the optimised file as an untracked video.
    """

    def _add(self, job, rel, **kw):
        job.add(rel, asset_id=kw.get("asset_id", "A"), src_bytes=kw.get("src_bytes", 100),
                src_probe={"rel": rel}, plan={"width": 1080})

    def test_differing_case_is_the_same_row(self, tmp_path):
        with OptimiseJob(tmp_path / "j.db") as job:
            self._add(job, "2024/05/IMG_1.MOV")
            self._add(job, "2024/05/img_1.mov")
            assert len(job.by_status(STATUS_SELECTED)) == 1

    def test_lookup_ignores_case(self, tmp_path):
        with OptimiseJob(tmp_path / "j.db") as job:
            self._add(job, "2024/05/IMG_1.MOV")
            assert job.get("2024/05/img_1.mov") is not None
            assert job.get("2024/05/IMG_1.mov") is not None

    def test_a_converted_row_is_not_reset_by_a_differently_cased_add(self, tmp_path):
        # The bug this fixes: after the swap the file on disk is IMG_1.mov, so
        # the next scan offers it under that name and the terminal-state guard
        # never fired, because it was looking for IMG_1.MOV.
        with OptimiseJob(tmp_path / "j.db") as job:
            self._add(job, "2024/05/IMG_1.MOV")
            job.mark_converted("2024/05/IMG_1.MOV", out_rel="IMG_1.mov",
                               out_bytes=10, out_probe={})
            self._add(job, "2024/05/IMG_1.mov")
            row = job.get("2024/05/IMG_1.MOV")
            assert row["status"] == STATUS_CONVERTED and row["out_rel"] == "IMG_1.mov"
            assert len(job.by_status(STATUS_SELECTED)) == 0

    def test_marks_apply_through_a_different_case(self, tmp_path):
        with OptimiseJob(tmp_path / "j.db") as job:
            self._add(job, "2024/05/IMG_1.MOV")
            job.mark_converted("2024/05/img_1.MOV", out_rel="IMG_1.mov",
                               out_bytes=10, out_probe={})
            job.mark_uploaded("2024/05/IMG_1.mov", "NEW")
            job.mark_swapped("2024/05/img_1.mov")
            assert job.get("2024/05/IMG_1.MOV")["status"] == STATUS_SWAPPED

    def test_distinct_files_are_still_distinct(self, tmp_path):
        with OptimiseJob(tmp_path / "j.db") as job:
            self._add(job, "2024/05/IMG_1.MOV")
            self._add(job, "2024/06/IMG_1.MOV")      # same name, different month
            assert len(job.by_status(STATUS_SELECTED)) == 2


class TestCollationMigration:
    """A store written before schema 2 must migrate without losing work."""

    def _legacy(self, path, rows):
        conn = sqlite3.connect(str(path))
        conn.execute("""
            CREATE TABLE jobs (
                rel TEXT PRIMARY KEY, asset_id TEXT, src_bytes INTEGER NOT NULL,
                src_probe TEXT, plan TEXT, status TEXT NOT NULL DEFAULT 'selected',
                out_rel TEXT, out_bytes INTEGER, out_probe TEXT,
                new_asset_id TEXT, error TEXT, updated_at TEXT)""")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO jobs (rel, src_bytes, status, new_asset_id, updated_at) "
            "VALUES (?,?,?,?,?)", rows)
        conn.commit(); conn.close()

    def test_a_legacy_store_opens_and_keeps_its_rows(self, tmp_path):
        db = tmp_path / "legacy.db"
        self._legacy(db, [("2024/05/A.MOV", 10, STATUS_SWAPPED, "NEW", "2026-01-01"),
                          ("2024/05/B.MOV", 20, STATUS_CONVERTED, None, "2026-01-01")])
        with OptimiseJob(db) as job:
            assert job.counts()[STATUS_SWAPPED] == 1
            assert job.counts()[STATUS_CONVERTED] == 1
            assert job.get("2024/05/a.mov") is not None      # now case-insensitive
            assert job.get_meta("schema_version") == SCHEMA_VERSION

    def test_case_duplicates_collapse_to_the_furthest_along_row(self, tmp_path):
        db = tmp_path / "dupes.db"
        self._legacy(db, [("2024/05/A.MOV", 10, STATUS_SWAPPED, "NEW", "2026-01-01"),
                          ("2024/05/a.mov", 10, STATUS_SELECTED, None, "2026-02-02")])
        with OptimiseJob(db) as job:
            rows = job.by_status(*ALL_STATUSES)
            assert len(rows) == 1
            # The swapped row wins despite being older: it carries the real work.
            assert rows[0]["status"] == STATUS_SWAPPED
            assert rows[0]["new_asset_id"] == "NEW"

    def test_migration_is_idempotent(self, tmp_path):
        db = tmp_path / "twice.db"
        self._legacy(db, [("2024/05/A.MOV", 10, STATUS_CONVERTED, None, "2026-01-01")])
        with OptimiseJob(db) as job:
            pass
        with OptimiseJob(db) as job:
            assert len(job.by_status(*ALL_STATUSES)) == 1
