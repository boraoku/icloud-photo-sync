"""Review page + server tests."""

import threading
from pathlib import Path

import requests

from icloud_photo_sync.review import FlaggedItem, ReviewServer, render_page
from icloud_photo_sync.trash import TrashResult


def _items(tmp_path, n=3):
    items = []
    for i in range(n):
        p = tmp_path / f"img{i}.jpg"
        p.write_text("x")
        items.append(FlaggedItem(
            index=i, path=p, rel=f"2023/img{i}.jpg",
            category="meme", confidence=0.9, reason="joke", size=1234,
        ))
    return items


def test_render_page_embeds_token_and_escapes(tmp_path):
    items = _items(tmp_path, 1)
    items[0] = FlaggedItem(
        index=0, path=items[0].path, rel="2023/</script>evil.jpg",
        category="meme", confidence=0.5, reason="x", size=10,
    )
    html = render_page(items, token="SECRET123")
    assert "SECRET123" in html
    assert "</script>evil" not in html      # broken up
    assert "<\\/script>evil" in html        # escaped form present


def _make_server(tmp_path, items, recorded, port=0):
    # Pre-generate thumbnails the server will serve.
    for it in items:
        (tmp_path / f"{it.index}.jpg").write_bytes(b"JPEGDATA")

    def trash_fn(paths):
        recorded.append(list(paths))
        return [TrashResult(path=p, ok=True) for p in paths]

    return ReviewServer(
        items=items, thumbs_dir=tmp_path, trash_fn=trash_fn,
        token="tok", port=port,
    )


def test_get_root_and_thumb(tmp_path):
    items = _items(tmp_path)
    srv = _make_server(tmp_path, items, recorded=[])
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    try:
        base = srv.url.rstrip("/")
        r = requests.get(base + "/", timeout=5)
        assert r.status_code == 200 and "text/html" in r.headers["Content-Type"]
        r = requests.get(base + "/thumb/0", timeout=5)
        assert r.status_code == 200 and r.content == b"JPEGDATA"
        r = requests.get(base + "/thumb/../etc", timeout=5)
        assert r.status_code == 404
    finally:
        srv._httpd.shutdown()
        t.join(timeout=5)


def test_post_requires_token(tmp_path):
    items = _items(tmp_path)
    recorded = []
    srv = _make_server(tmp_path, items, recorded)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    try:
        base = srv.url.rstrip("/")
        r = requests.post(base + "/trash", json={"ids": [0]}, timeout=5)
        assert r.status_code == 403
        assert recorded == []  # trash_fn never called
    finally:
        srv._httpd.shutdown()
        t.join(timeout=5)


def test_post_trashes_selected_and_returns_outcome(tmp_path):
    items = _items(tmp_path)
    recorded = []
    srv = _make_server(tmp_path, items, recorded)
    result_box = {}

    def run():
        result_box["outcome"] = srv.serve()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        base = srv.url.rstrip("/")
        r = requests.post(
            base + "/trash",
            json={"ids": [0, 2]},
            headers={"X-Clean-Token": "tok"},
            timeout=5,
        )
        assert r.status_code == 200
        data = r.json()
        assert sorted(data["moved"]) == ["2023/img0.jpg", "2023/img2.jpg"]
        assert data["failed"] == []
    finally:
        t.join(timeout=5)

    # Server shut itself down after the POST; serve() returned the outcome.
    assert recorded == [[items[0].path, items[2].path]]
    outcome = result_box["outcome"]
    assert sorted(outcome.moved) == ["2023/img0.jpg", "2023/img2.jpg"]


def test_serve_returns_none_on_no_action(tmp_path):
    items = _items(tmp_path)
    srv = _make_server(tmp_path, items, recorded=[])
    box = {}

    def run():
        box["outcome"] = srv.serve()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    srv._httpd.shutdown()  # simulate external stop without a POST
    t.join(timeout=5)
    assert box["outcome"] is None
