"""Video review server + page tests."""

import json
import time

import requests

from icloud_photo_sync.poster import PosterCache
from icloud_photo_sync.trash import TrashResult
from icloud_photo_sync.video_review import (
    VideoItem,
    VideoReviewServer,
    _Handler,
    _item_payload,
    _parse_range,
    render_video_page,
)

HDR = {"X-Clean-Token": "tok"}
_POSTER = b"\xff\xd8\xff" + b"fake jpeg"


def _stub_extract(calls, delay=0.0):
    """Extractor double, so tests never shell out to ffmpeg/QuickLook."""
    def extract(src, dest, max_dim, timeout):
        calls.append(src)
        if delay:
            time.sleep(delay)
        dest.write_bytes(_POSTER)
        return True
    return extract


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


def test_grid_uses_posters_and_only_the_modal_holds_a_video():
    html = render_video_page("tok", [])
    # One <video> in the whole page: the modal player. Cards get <img> posters,
    # so the browser keeps no decoder per card.
    assert html.count("<video") == 1
    assert 'createElement("video")' not in html
    assert '"/poster/" + it.index' in html
    assert 'shot.loading = "lazy"' in html
    # the modal reuses one element, so a new source must be loaded explicitly
    # or Safari shows a black frame (see openModal)
    assert "player.load();" in html
    # offscreen cards skip layout/paint
    assert "content-visibility: auto" in html


def test_cards_show_the_duration_beside_the_size(tmp_path):
    items = [VideoItem(index=0, path=None, rel="a.mov", size=900, mtime_ns=0,
                       duration=3671.4),                    # 1h 1m 11s
             VideoItem(index=1, path=None, rel="b.mov", size=100, mtime_ns=0)]
    payload = json.loads(json.dumps([_item_payload(it) for it in items]))

    assert payload[0]["dur"] == "01:01:11"
    assert payload[1]["dur"] == ""          # unknown length: no badge, no error
    assert 'dur.textContent = it.dur || ""' in render_video_page("tok", items)


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


# --- posters ------------------------------------------------------------------


def _poll_poster(url, tries=40):
    """Fetch like the page does: a 404 means "queued", so retry briefly."""
    for _ in range(tries):
        r = requests.get(url)
        if r.status_code == 200:
            return r
        time.sleep(0.05)
    return r


def test_poster_request_returns_at_once_then_serves_the_render(tmp_path):
    calls = []
    posters = PosterCache(tmp_path / "cache", extract=_stub_extract(calls, delay=0.4))
    items = _items(tmp_path, [("a.mov", b"x")])
    srv = _make_server(tmp_path, items)
    srv.posters = posters
    srv.start()
    try:
        # The first request must not wait on the render: a blocked thumbnail
        # holds one of the browser's few connections and stalls video playback.
        t0 = time.perf_counter()
        first = requests.get(srv.url + "poster/0")
        assert first.status_code == 404
        assert time.perf_counter() - t0 < 0.2

        r = _poll_poster(srv.url + "poster/0")
        assert r.status_code == 200
        assert r.headers["Content-Type"] == "image/jpeg"
        assert r.content == _POSTER
        assert "max-age" in r.headers.get("Cache-Control", "")

        assert requests.get(srv.url + "poster/0").content == _POSTER
        assert len(calls) == 1              # served from the cache thereafter
    finally:
        srv.close()


def test_poster_404s_for_unknown_index_and_unrenderable_video(tmp_path):
    items = _items(tmp_path, [("a.mov", b"x")])
    srv = _make_server(tmp_path, items)
    # extractor that always fails: the page falls back to a placeholder tile
    srv.posters = PosterCache(tmp_path / "cache",
                              extract=lambda *a, **k: False)
    srv.start()
    try:
        assert requests.get(srv.url + "poster/0").status_code == 404
        assert requests.get(srv.url + "poster/99").status_code == 404
        assert requests.get(srv.url + "poster/abc").status_code == 404
        # and it stays 404 once the render has been attempted and failed
        assert _poll_poster(srv.url + "poster/0", tries=10).status_code == 404
    finally:
        srv.close()


def test_poster_404s_when_no_cache_is_configured(tmp_path):
    srv = _make_server(tmp_path, _items(tmp_path, [("a.mov", b"x")]))
    srv.start()
    try:
        assert srv.posters is None
        assert requests.get(srv.url + "poster/0").status_code == 404
    finally:
        srv.close()


# --- client aborts (keep-alive) ----------------------------------------------


class _ResetOnRead:
    """rfile whose read fails the way an aborted preload resets the socket."""

    def readline(self, *_args):
        raise ConnectionResetError(54, "Connection reset by peer")


def test_client_abort_ends_keepalive_connection_quietly(capfd):
    # No socket needed: the reset happens on the next-request read, which is
    # all __new__ + rfile exercises.
    handler = _Handler.__new__(_Handler)
    handler.rfile = _ResetOnRead()
    handler.close_connection = False

    handler.handle_one_request()

    assert handler.close_connection is True
    assert capfd.readouterr().err == ""
