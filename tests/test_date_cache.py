"""Date-recovery resume cache: what it remembers, and what it deliberately
refuses to remember."""

from __future__ import annotations

import sqlite3

import pytest

from icloud_photo_sync import date_cache as dc


@pytest.fixture
def cache(tmp_path):
    c = dc.DateCache(tmp_path / "dates.db")
    yield c
    c.close()


def _put(cache, rel="a.jpg", size=100, mtime_ns=1234,
         outcome=dc.OUTCOME_ALREADY_PRESENT):
    cache.put(rel, size, mtime_ns, outcome)
    cache.flush()


# --- schema / lifecycle -------------------------------------------------------


def test_schema_and_version_stamped_on_open(cache):
    assert cache.get_meta("schema_version") == dc.SCHEMA_VERSION


def test_reopen_keeps_existing_rows(tmp_path):
    with dc.DateCache(tmp_path / "dates.db") as c:
        _put(c)
    with dc.DateCache(tmp_path / "dates.db") as c:
        assert c.get("a.jpg", 100, 1234) == dc.OUTCOME_ALREADY_PRESENT


def test_creates_the_parent_directory(tmp_path):
    with dc.DateCache(tmp_path / "nested" / "deeper" / "dates.db") as c:
        assert c.count() == 0


# --- hit / miss ----------------------------------------------------------------


def test_hit_when_nothing_changed(cache):
    _put(cache)
    assert cache.get("a.jpg", 100, 1234) == dc.OUTCOME_ALREADY_PRESENT


def test_miss_on_unknown_path(cache):
    _put(cache)
    assert cache.get("other.jpg", 100, 1234) is None


def test_miss_when_size_changed(cache):
    _put(cache)
    assert cache.get("a.jpg", 101, 1234) is None


def test_miss_when_mtime_changed(cache):
    _put(cache)
    assert cache.get("a.jpg", 100, 9999) is None


def test_case_insensitive_path_is_one_row(cache):
    # The photo volume is case-insensitive: IMG_1.HEIC and IMG_1.heic are one
    # file and must never hold two different verdicts.
    _put(cache, rel="IMG_1.HEIC")
    assert cache.get("img_1.heic", 100, 1234) == dc.OUTCOME_ALREADY_PRESENT
    cache.put("img_1.heic", 100, 1234, dc.OUTCOME_UNKNOWN)
    cache.flush()
    assert cache.count() == 1


def test_reput_updates_in_place(cache):
    _put(cache)
    cache.put("a.jpg", 200, 5678, dc.OUTCOME_STAMPED_FOLDER)
    cache.flush()
    assert cache.count() == 1
    assert cache.get("a.jpg", 100, 1234) is None            # old fingerprint gone
    assert cache.get("a.jpg", 200, 5678) == dc.OUTCOME_STAMPED_FOLDER


# --- which outcomes are remembered ---------------------------------------------


@pytest.mark.parametrize("outcome", sorted(dc.TERMINAL))
def test_terminal_outcomes_are_remembered(cache, outcome):
    _put(cache, outcome=outcome)
    assert cache.get("a.jpg", 100, 1234) == outcome


def test_unknown_is_terminal_on_purpose(cache):
    # All three inputs to "unknown" (embedded date, filename, folder) are
    # properties of a file that has not changed, so re-asking cannot differ.
    assert dc.OUTCOME_UNKNOWN in dc.TERMINAL


@pytest.mark.parametrize("outcome", ["failed", "tool_unavailable", "", "banana"])
def test_non_terminal_outcomes_are_silently_not_remembered(cache, outcome):
    cache.put("a.jpg", 100, 1234, outcome)
    cache.flush()
    assert cache.count() == 0
    assert cache.get("a.jpg", 100, 1234) is None


def test_the_check_constraint_rejects_a_bad_outcome_written_directly(tmp_path):
    # put() filters, but the table refuses too — so a future caller reaching
    # past the API cannot poison the cache with an unknown verdict.
    with dc.DateCache(tmp_path / "dates.db") as c:
        with pytest.raises(sqlite3.IntegrityError):
            c._conn.execute(
                "INSERT INTO date_checks (rel, size, mtime_ns, outcome) "
                "VALUES ('a.jpg', 1, 1, 'nonsense')"
            )


# --- durability ------------------------------------------------------------------


def test_put_alone_does_not_commit_but_flush_does(tmp_path):
    c = dc.DateCache(tmp_path / "dates.db")
    c.put("a.jpg", 100, 1234, dc.OUTCOME_UNKNOWN)
    # A separate connection cannot see it until the batch is flushed.
    other = sqlite3.connect(str(tmp_path / "dates.db"))
    assert other.execute("SELECT COUNT(*) FROM date_checks").fetchone()[0] == 0
    c.flush()
    assert other.execute("SELECT COUNT(*) FROM date_checks").fetchone()[0] == 1
    other.close()
    c.close()


def test_close_commits_anything_still_staged(tmp_path):
    c = dc.DateCache(tmp_path / "dates.db")
    c.put("a.jpg", 100, 1234, dc.OUTCOME_UNKNOWN)
    c.close()
    with dc.DateCache(tmp_path / "dates.db") as reopened:
        assert reopened.count() == 1


# --- clear (--recheck-dates) -----------------------------------------------------


def test_clear_forgets_everything(cache):
    _put(cache, rel="a.jpg")
    _put(cache, rel="b.jpg")
    assert cache.count() == 2
    cache.clear()
    assert cache.count() == 0
    assert cache.get("a.jpg", 100, 1234) is None


def test_clear_keeps_the_schema_usable(cache):
    _put(cache)
    cache.clear()
    _put(cache, rel="c.jpg")
    assert cache.count() == 1
