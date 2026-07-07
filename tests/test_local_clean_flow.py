"""End-to-end flow test for run_local_clean.

Drives the real main-thread/server-thread handshake: classification runs on the
main thread while a driver thread polls /items, trashes the flagged item, and
clicks Finish — with the vision model and Finder trashing stubbed out.
"""

import queue
import re
import threading

import requests

from icloud_photo_sync import local_clean
from icloud_photo_sync.classifier import Classification
from icloud_photo_sync.clean_cache import CleanCache
from icloud_photo_sync.config import LocalCleanConfig
from icloud_photo_sync.trash import TrashResult


def _seed(root):
    (root / "2023").mkdir(parents=True)
    (root / "2023" / "meme_thing.jpg").write_text("x")
    (root / "2023" / "real_photo.jpg").write_text("x")


def test_stream_classify_trash_finish(tmp_path, monkeypatch):
    photo_root = tmp_path / "photos"
    photo_root.mkdir()
    _seed(photo_root)

    # Stub the vision model: flag anything with "meme" in the name.
    monkeypatch.setattr(local_clean.LMStudioClassifier, "check_available", lambda self: None)

    def fake_classify(self, path):
        if "meme" in path.name:
            return Classification("meme", 0.95, "stub meme")
        return Classification("photo", 0.95, "stub photo")

    monkeypatch.setattr(local_clean.LMStudioClassifier, "classify", fake_classify)

    # Stub trashing: actually unlink so a re-scan won't find the file.
    trashed = []

    def fake_trash(paths):
        out = []
        for p in paths:
            try:
                p.unlink()
            except OSError:
                pass
            trashed.append(str(p))
            out.append(TrashResult(path=p, ok=True))
        return out

    monkeypatch.setattr(local_clean, "move_to_trash", fake_trash)

    # Capture the review URL via the browser-open hook.
    url_q: queue.Queue = queue.Queue()
    monkeypatch.setattr(local_clean.webbrowser, "open", lambda u: url_q.put(u))

    config = LocalCleanConfig.create(
        photo_root,
        config_root=tmp_path / "cfg",
        lm_model="test-model",
        open_browser=True,
        port=0,
    )

    driver_err: queue.Queue = queue.Queue()

    def driver():
        try:
            base = url_q.get(timeout=10).rstrip("/")
            token = re.search(r'const TOKEN = "([^"]+)"',
                              requests.get(base + "/", timeout=5).text).group(1)
            hdr = {"X-Clean-Token": token}
            # Wait until classification is done and the meme has streamed in.
            items = []
            for _ in range(100):
                data = requests.get(base + "/items?since=0", timeout=5).json()
                if data["done"]:
                    items = data["items"]
                    break
                threading.Event().wait(0.05)
            assert [it["rel"] for it in items] == ["2023/meme_thing.jpg"]
            ids = [it["index"] for it in items]
            r = requests.post(base + "/trash", json={"ids": ids}, headers=hdr, timeout=10)
            assert r.json()["moved"] == ["2023/meme_thing.jpg"]
            requests.post(base + "/finish", json={}, headers=hdr, timeout=5)
        except Exception as exc:  # surface to the main assertion
            driver_err.put(exc)

    rc_box = {}

    def run():
        rc_box["rc"] = local_clean.run_local_clean(config)

    rt = threading.Thread(target=run)
    dt = threading.Thread(target=driver)
    rt.start()
    dt.start()
    dt.join(timeout=25)
    rt.join(timeout=25)

    assert driver_err.empty(), driver_err.get()
    assert rc_box.get("rc") == 0
    assert trashed and trashed[0].endswith("meme_thing.jpg")
    assert not (photo_root / "2023" / "meme_thing.jpg").exists()

    # Cache: the trashed row was removed; the kept photo's row remains.
    c = CleanCache(config.cache_db)
    try:
        meme_row = c._conn.execute(
            "SELECT 1 FROM classifications WHERE path=?", ("2023/meme_thing.jpg",)
        ).fetchone()
        photo_row = c._conn.execute(
            "SELECT 1 FROM classifications WHERE path=?", ("2023/real_photo.jpg",)
        ).fetchone()
        assert meme_row is None
        assert photo_row is not None
    finally:
        c.close()


def test_all_cached_nothing_flagged_exits_zero(tmp_path, monkeypatch):
    photo_root = tmp_path / "photos"
    photo_root.mkdir()
    (photo_root / "2023").mkdir(parents=True)
    (photo_root / "2023" / "real_photo.jpg").write_text("x")

    monkeypatch.setattr(local_clean.LMStudioClassifier, "check_available", lambda self: None)
    monkeypatch.setattr(
        local_clean.LMStudioClassifier, "classify",
        lambda self, path: Classification("photo", 0.9, "stub"),
    )
    monkeypatch.setattr(local_clean, "move_to_trash", lambda paths: [])
    monkeypatch.setattr(local_clean.webbrowser, "open", lambda u: None)
    # Speed up the zero-flagged grace wait.
    monkeypatch.setattr(local_clean, "ZERO_FLAGGED_GRACE", 0.1)

    config = LocalCleanConfig.create(
        photo_root, config_root=tmp_path / "cfg",
        lm_model="test-model", open_browser=False, port=0,
    )

    # First run classifies the photo (not flagged) → exits 0 without any driver.
    assert local_clean.run_local_clean(config) == 0
    # Second run: fully cached, still nothing flagged → exits 0.
    assert local_clean.run_local_clean(config) == 0
