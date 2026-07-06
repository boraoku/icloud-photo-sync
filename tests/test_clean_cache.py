"""Classification cache tests."""

import pytest

from icloud_photo_sync.classifier import Classification
from icloud_photo_sync.clean_cache import CleanCache
from icloud_photo_sync.local_clean import ImageFile


def _img(rel="2023/a.jpg", size=100, mtime_ns=111, path=None):
    return ImageFile(path=path or f"/root/{rel}", rel=rel, size=size, mtime_ns=mtime_ns)


@pytest.fixture
def cache(tmp_path):
    c = CleanCache(tmp_path / "clean.db")
    yield c
    c.close()


def test_put_get_roundtrip(cache):
    img = _img()
    cache.put(img, "m1", Classification("meme", 0.9, "funny"))
    got = cache.get(img, "m1")
    assert got == Classification("meme", 0.9, "funny")


def test_miss_on_size_change(cache):
    cache.put(_img(size=100), "m1", Classification("photo", 0.8, "r"))
    assert cache.get(_img(size=200), "m1") is None


def test_miss_on_mtime_change(cache):
    cache.put(_img(mtime_ns=111), "m1", Classification("photo", 0.8, "r"))
    assert cache.get(_img(mtime_ns=222), "m1") is None


def test_miss_on_model_change(cache):
    cache.put(_img(), "m1", Classification("photo", 0.8, "r"))
    assert cache.get(_img(), "m2") is None


def test_upsert_overwrites(cache):
    img = _img()
    cache.put(img, "m1", Classification("photo", 0.8, "first"))
    cache.put(img, "m1", Classification("meme", 0.7, "second"))
    got = cache.get(img, "m1")
    assert got.category == "meme"
    assert got.reason == "second"


def test_remove(cache):
    img = _img()
    cache.put(img, "m1", Classification("meme", 0.9, "x"))
    cache.remove([img.rel])
    assert cache.get(img, "m1") is None


def test_persist_across_reopen(tmp_path):
    db = tmp_path / "clean.db"
    img = _img()
    c1 = CleanCache(db)
    c1.put(img, "m1", Classification("screenshot", 0.95, "ui"))
    c1.close()

    c2 = CleanCache(db)
    try:
        assert c2.get(img, "m1").category == "screenshot"
    finally:
        c2.close()
