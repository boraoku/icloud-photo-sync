"""Review streaming-server tests."""

import threading

import requests

from icloud_photo_sync.review import FlaggedItem, ReviewServer, render_page
from icloud_photo_sync.trash import TrashResult

HDR = {"X-Clean-Token": "tok"}


def _make_server(tmp_path, recorded, fail_paths=None, token="tok"):
    fail_paths = fail_paths if fail_paths is not None else set()

    def trash_fn(paths):
        recorded.append(list(paths))
        return [
            TrashResult(
                path=p,
                ok=str(p) not in fail_paths,
                error=None if str(p) not in fail_paths else "boom",
            )
            for p in paths
        ]

    return ReviewServer(thumbs_dir=tmp_path, trash_fn=trash_fn, token=token, port=0)


def _publish(srv, tmp_path, n=3):
    items = []
    for i in range(n):
        src = tmp_path / f"src{i}.jpg"
        src.write_text("x")
        (tmp_path / f"{i}.jpg").write_bytes(b"JPEGDATA")  # thumbnail the server serves
        it = FlaggedItem(
            index=i, path=src, rel=f"2023/img{i}.jpg",
            category="meme", confidence=0.9, reason="joke", size=1234,
        )
        srv.publish(it)
        items.append(it)
    return items


# --- page ---------------------------------------------------------------------


def test_render_page_has_token_and_no_item_data():
    html = render_page("SECRET123")
    assert "SECRET123" in html
    assert "__TOKEN__" not in html
    assert "__ITEMS_JSON__" not in html  # item data now travels via /items, not embedded


# --- snapshot / cursor (no server thread needed) ------------------------------


def test_items_cursor(tmp_path):
    srv = _make_server(tmp_path, [])
    _publish(srv, tmp_path, 3)
    assert len(srv.snapshot(0)["items"]) == 3
    assert srv.snapshot(0)["next"] == 3
    assert len(srv.snapshot(2)["items"]) == 1
    assert srv.snapshot(3)["items"] == []


def test_progress_and_done_reflected(tmp_path):
    srv = _make_server(tmp_path, [])
    srv.set_progress(4, 10)
    snap = srv.snapshot(0)
    assert snap["classified"] == 4 and snap["total"] == 10
    assert snap["done"] is False
    srv.mark_done()
    assert srv.snapshot(0)["done"] is True


def test_trashed_excluded_from_snapshot(tmp_path):
    recorded = []
    srv = _make_server(tmp_path, recorded)
    _publish(srv, tmp_path, 3)
    srv.do_trash([1])
    rels = [it["rel"] for it in srv.snapshot(0)["items"]]
    assert rels == ["2023/img0.jpg", "2023/img2.jpg"]
    assert srv.snapshot(0)["next"] == 3  # cursor unchanged; indices never reused


def test_repeat_trash_is_noop(tmp_path):
    recorded = []
    srv = _make_server(tmp_path, recorded)
    _publish(srv, tmp_path, 3)
    srv.do_trash([0])
    assert len(recorded) == 1 and len(recorded[0]) == 1
    srv.do_trash([0])  # already trashed → resolves to nothing
    assert recorded[1] == []


def test_failed_trash_unmarks_and_is_retryable(tmp_path):
    recorded = []
    fail_paths = set()
    srv = _make_server(tmp_path, recorded, fail_paths=fail_paths)
    items = _publish(srv, tmp_path, 3)
    fail_paths.add(str(items[1].path))

    out = srv.do_trash([1])
    assert out.moved == []
    assert out.failed and out.failed[0][0] == "2023/img1.jpg"
    # not marked trashed → still visible, retryable
    assert "2023/img1.jpg" in [it["rel"] for it in srv.snapshot(0)["items"]]

    fail_paths.discard(str(items[1].path))
    out2 = srv.do_trash([1])
    assert out2.moved == ["2023/img1.jpg"]
    # accumulated outcome carries both the earlier failure and the success
    assert srv.outcome.moved == ["2023/img1.jpg"]
    assert len(srv.outcome.failed) == 1


# --- HTTP surface (server thread) ---------------------------------------------


def test_get_root_thumb_and_items(tmp_path):
    srv = _make_server(tmp_path, [])
    _publish(srv, tmp_path, 3)
    srv.set_progress(3, 10)
    srv.start()
    try:
        base = srv.url.rstrip("/")
        r = requests.get(base + "/", timeout=5)
        assert r.status_code == 200 and "text/html" in r.headers["Content-Type"]
        r = requests.get(base + "/thumb/0", timeout=5)
        assert r.status_code == 200 and r.content == b"JPEGDATA"
        assert requests.get(base + "/thumb/../etc", timeout=5).status_code == 404
        data = requests.get(base + "/items?since=0", timeout=5).json()
        assert len(data["items"]) == 3 and data["next"] == 3
        assert data["classified"] == 3 and data["total"] == 10 and data["done"] is False
        assert requests.get(base + "/items?since=abc", timeout=5).status_code == 400
    finally:
        srv.close()


def test_publish_after_start_is_visible(tmp_path):
    srv = _make_server(tmp_path, [])
    srv.start()
    try:
        base = srv.url.rstrip("/")
        assert requests.get(base + "/items?since=0", timeout=5).json()["items"] == []
        _publish(srv, tmp_path, 1)
        data = requests.get(base + "/items?since=0", timeout=5).json()
        assert len(data["items"]) == 1 and data["next"] == 1
    finally:
        srv.close()


def test_post_requires_token(tmp_path):
    recorded = []
    srv = _make_server(tmp_path, recorded)
    _publish(srv, tmp_path, 3)
    srv.start()
    try:
        base = srv.url.rstrip("/")
        assert requests.post(base + "/trash", json={"ids": [0]}, timeout=5).status_code == 403
        assert recorded == []
        assert requests.post(base + "/finish", json={}, timeout=5).status_code == 403
        assert srv.finish_requested is False
    finally:
        srv.close()


def test_trash_keeps_server_alive_and_accumulates(tmp_path):
    recorded = []
    srv = _make_server(tmp_path, recorded)
    _publish(srv, tmp_path, 3)
    srv.start()
    try:
        base = srv.url.rstrip("/")
        r = requests.post(base + "/trash", json={"ids": [0]}, headers=HDR, timeout=5)
        assert r.status_code == 200 and r.json()["moved"] == ["2023/img0.jpg"]
        # still alive: a follow-up request succeeds
        assert requests.get(base + "/items?since=0", timeout=5).status_code == 200
        r = requests.post(base + "/trash", json={"ids": [2]}, headers=HDR, timeout=5)
        assert r.json()["moved"] == ["2023/img2.jpg"]
        assert sorted(srv.outcome.moved) == ["2023/img0.jpg", "2023/img2.jpg"]
    finally:
        srv.close()
    assert len(recorded) == 2


def test_finish_sets_event_and_returns_accumulated_outcome(tmp_path):
    recorded = []
    srv = _make_server(tmp_path, recorded)
    _publish(srv, tmp_path, 2)
    srv.do_trash([0])
    srv.start()
    try:
        base = srv.url.rstrip("/")
        done = threading.Event()
        threading.Thread(
            target=lambda: (srv.wait_finished(), done.set()), daemon=True
        ).start()
        r = requests.post(base + "/finish", json={}, headers=HDR, timeout=5)
        assert r.status_code == 200 and r.json()["moved"] == ["2023/img0.jpg"]
        assert srv.finish_requested is True
        assert done.wait(5) is True
    finally:
        srv.close()


def test_close_is_idempotent_and_outcome_starts_empty(tmp_path):
    srv = _make_server(tmp_path, [])
    assert srv.outcome.moved == [] and srv.outcome.failed == []
    srv.start()
    srv.close()
    srv.close()  # must not raise


def test_close_without_start_does_not_hang(tmp_path):
    srv = _make_server(tmp_path, [])
    srv.close()  # never started


def test_client_disconnect_is_quiet_but_bugs_are_reported(tmp_path, capfd):
    srv = _make_server(tmp_path, [])
    client = ("127.0.0.1", 54321)
    try:
        # handle_error() reads the live exception, so raise it for real.
        try:
            raise ConnectionResetError(54, "Connection reset by peer")
        except ConnectionResetError:
            srv._httpd.handle_error(None, client)
        assert capfd.readouterr().err == ""

        try:
            raise ValueError("handler bug")
        except ValueError:
            srv._httpd.handle_error(None, client)
        assert "handler bug" in capfd.readouterr().err
    finally:
        srv.close()
