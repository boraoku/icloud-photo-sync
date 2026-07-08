"""Video review server + page tests."""

import requests

from icloud_photo_sync.trash import TrashResult
from icloud_photo_sync.video_review import (
    VideoItem,
    VideoReviewServer,
    _parse_range,
    render_video_page,
)

HDR = {"X-Clean-Token": "tok"}


def _items(tmp_path, specs):
    """specs: list of (rel, content-bytes). Returns VideoItems on disk."""
    items = []
    for i, (rel, content) in enumerate(specs):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        st = p.stat()
        items.append(VideoItem(index=i, path=p, rel=rel, size=st.st_size,
                               mtime_ns=st.st_mtime_ns))
    return items


def _make_server(tmp_path, items, recorded=None, fail_paths=None, token="tok"):
    recorded = recorded if recorded is not None else []
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

    return VideoReviewServer(items=items, trash_fn=trash_fn, token=token, port=0)


# --- range parsing (pure) -----------------------------------------------------


def test_parse_range_variants():
    assert _parse_range(None, 10) is None
    assert _parse_range("bytes=2-5", 10) == (2, 5)
    assert _parse_range("bytes=4-", 10) == (4, 9)
    assert _parse_range("bytes=-3", 10) == (7, 9)       # suffix range
    assert _parse_range("bytes=8-100", 10) == (8, 9)    # clamped to end
    assert _parse_range("items=0-1", 10) is None        # non-byte unit → full
    assert _parse_range("bytes=10-12", 10) == "unsatisfiable"  # start past EOF
    assert _parse_range("bytes=5-2", 10) == "unsatisfiable"    # inverted


# --- page ---------------------------------------------------------------------


def test_render_page_has_token_and_items_largest_first():
    items = [
        VideoItem(index=0, path=None, rel="big.mov", size=900, mtime_ns=0),
        VideoItem(index=1, path=None, rel="small.mov", size=100, mtime_ns=0),
    ]
    html = render_video_page("SECRET123", items)
    assert "SECRET123" in html
    assert "__TOKEN__" not in html
    assert "__ITEMS_JSON__" not in html
    # order is preserved as given (caller passes largest-first)
    assert html.index("big.mov") < html.index("small.mov")


# --- video serving (live server) ----------------------------------------------


def test_video_full_body(tmp_path):
    srv = _make_server(tmp_path, _items(tmp_path, [("a.mov", b"0123456789")]))
    srv.start()
    try:
        r = requests.get(srv.url + "video/0")
        assert r.status_code == 200
        assert r.content == b"0123456789"
        assert r.headers["Accept-Ranges"] == "bytes"
    finally:
        srv.close()


def test_video_range_slice(tmp_path):
    srv = _make_server(tmp_path, _items(tmp_path, [("a.mov", b"0123456789")]))
    srv.start()
    try:
        r = requests.get(srv.url + "video/0", headers={"Range": "bytes=2-5"})
        assert r.status_code == 206
        assert r.content == b"2345"
        assert r.headers["Content-Range"] == "bytes 2-5/10"
        assert r.headers["Content-Length"] == "4"
    finally:
        srv.close()


def test_video_range_open_ended(tmp_path):
    srv = _make_server(tmp_path, _items(tmp_path, [("a.mov", b"0123456789")]))
    srv.start()
    try:
        r = requests.get(srv.url + "video/0", headers={"Range": "bytes=4-"})
        assert r.status_code == 206
        assert r.content == b"456789"
        assert r.headers["Content-Range"] == "bytes 4-9/10"
    finally:
        srv.close()


def test_video_range_unsatisfiable(tmp_path):
    srv = _make_server(tmp_path, _items(tmp_path, [("a.mov", b"0123456789")]))
    srv.start()
    try:
        r = requests.get(srv.url + "video/0", headers={"Range": "bytes=99-100"})
        assert r.status_code == 416
        assert r.headers["Content-Range"] == "bytes */10"
    finally:
        srv.close()


def test_video_unknown_index_404(tmp_path):
    srv = _make_server(tmp_path, _items(tmp_path, [("a.mov", b"x")]))
    srv.start()
    try:
        assert requests.get(srv.url + "video/9").status_code == 404
        assert requests.get(srv.url + "video/abc").status_code == 404
    finally:
        srv.close()


# --- trash / finish -----------------------------------------------------------


def test_trash_requires_token(tmp_path):
    srv = _make_server(tmp_path, _items(tmp_path, [("a.mov", b"x")]))
    srv.start()
    try:
        r = requests.post(srv.url + "trash", json={"ids": [0]})  # no token header
        assert r.status_code == 403
    finally:
        srv.close()


def test_trash_moves_and_accumulates(tmp_path):
    recorded = []
    items = _items(tmp_path, [("a.mov", b"x"), ("b.mov", b"y")])
    srv = _make_server(tmp_path, items, recorded=recorded)
    srv.start()
    try:
        r = requests.post(srv.url + "trash", headers=HDR, json={"ids": [0, 1]})
        data = r.json()
        assert sorted(data["moved"]) == ["a.mov", "b.mov"]
        assert data["failed"] == []
        assert sorted(srv.outcome.moved) == ["a.mov", "b.mov"]
        assert recorded == [[items[0].path, items[1].path]]
    finally:
        srv.close()


def test_trash_failure_is_retryable(tmp_path):
    items = _items(tmp_path, [("a.mov", b"x")])
    srv = _make_server(tmp_path, items, fail_paths={str(items[0].path)})
    srv.start()
    try:
        r = requests.post(srv.url + "trash", headers=HDR, json={"ids": [0]})
        data = r.json()
        assert data["moved"] == []
        assert data["failed"] == [{"rel": "a.mov", "error": "boom"}]
        # failed items are un-marked, so index 0 can be retried
        assert 0 not in srv._trashed
    finally:
        srv.close()


def test_finish_sets_event(tmp_path):
    srv = _make_server(tmp_path, _items(tmp_path, [("a.mov", b"x")]))
    srv.start()
    try:
        assert not srv.finish_requested
        r = requests.post(srv.url + "finish", headers=HDR, json={})
        assert r.status_code == 200
        assert srv.finish_requested
    finally:
        srv.close()
