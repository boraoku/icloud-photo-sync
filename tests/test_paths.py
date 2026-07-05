from datetime import datetime, timedelta, timezone
from pathlib import Path

from icloud_photo_sync.models import AssetRef
from icloud_photo_sync.paths import PathResolver, safe_filename, year_month

UNICODE_NAME = "café.heic"  # café.heic (NFC)


def _asset(filename="IMG_0001.HEIC", capture=None, added=None, size=10):
    return AssetRef(id="a1", filename=filename, capture_dt=capture, added_dt=added, size=size)


def test_year_month_zero_padded():
    dt = datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc)
    assert year_month(dt) == ("2026", "03")


def test_year_month_uses_utc():
    # 00:30 at +02:00 is the previous day (22:30) in UTC → previous month.
    dt = datetime(2026, 7, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert year_month(dt) == ("2026", "06")


def test_relative_dest_by_capture_date():
    r = PathResolver(Path("/root"))
    a = _asset(capture=datetime(2026, 7, 4, tzinfo=timezone.utc))
    assert r.relative_dest(a) == Path("2026/07/IMG_0001.HEIC")


def test_relative_dest_falls_back_to_added_then_unknown():
    r = PathResolver(Path("/root"))
    a = _asset(capture=None, added=datetime(2025, 12, 31, tzinfo=timezone.utc))
    assert r.relative_dest(a) == Path("2025/12/IMG_0001.HEIC")
    b = _asset(capture=None, added=None)
    assert r.relative_dest(b) == Path("unknown-date/IMG_0001.HEIC")


def test_safe_filename():
    assert safe_filename("a/b\\c.jpg") == "a_b_c.jpg"
    assert safe_filename(UNICODE_NAME) == UNICODE_NAME  # unicode preserved
    assert safe_filename("ctrl\x01name.heic") == "ctrl_name.heic"  # control stripped
    assert safe_filename("   ") == "unnamed"
    assert safe_filename("..") == "unnamed"


def test_disambiguate_appends_suffix():
    taken = {Path("2026/07/IMG.HEIC"), Path("2026/07/IMG-1.HEIC")}
    out = PathResolver.disambiguate(Path("2026/07/IMG.HEIC"), lambda p: p in taken)
    assert out == Path("2026/07/IMG-2.HEIC")


def test_disambiguate_noop_when_free():
    out = PathResolver.disambiguate(Path("2026/07/IMG.HEIC"), lambda p: False)
    assert out == Path("2026/07/IMG.HEIC")
