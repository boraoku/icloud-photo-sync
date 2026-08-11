"""Arming the review UIs for iCloud deletion.

The security property under test is one-directional: the terminal decides, and
the page can only ever narrow that decision. A page that has the loopback token
must not be able to queue an iCloud deletion the terminal never authorised.
"""

import re
from pathlib import Path

import pytest
import requests

from icloud_photo_sync.review import FlaggedItem, ReviewServer, render_page
from icloud_photo_sync.trash import TrashResult
from icloud_photo_sync.video_review import (
    VideoItem,
    VideoReviewServer,
    render_video_page,
)

HDR = {"X-Clean-Token": "tok"}


def _trash_fn(paths):
    for p in paths:
        p.unlink()                      # actually remove, so `moved` is real
    return [TrashResult(path=p, ok=True) for p in paths]


def _image_server(tmp_path, *, armed, n=2):
    srv = ReviewServer(thumbs_dir=tmp_path, trash_fn=_trash_fn, token="tok",
                       port=0, icloud_armed=armed)
    for i in range(n):
        path = tmp_path / f"img{i}.jpg"
        path.write_bytes(b"x" * (10 + i))
        srv.publish(FlaggedItem(index=i, path=path, rel=f"2026/07/img{i}.jpg",
                                category="screenshot", confidence=0.9,
                                reason="r", size=10 + i))
    return srv


def _video_server(tmp_path, *, armed, n=2):
    items = []
    for i in range(n):
        path = tmp_path / f"vid{i}.mov"
        path.write_bytes(b"x" * (10 + i))
        st = path.stat()
        items.append(VideoItem(index=i, path=path, rel=f"2026/07/vid{i}.mov",
                               size=st.st_size, mtime_ns=st.st_mtime_ns))
    return VideoReviewServer(items=items, trash_fn=_trash_fn, token="tok",
                             port=0, icloud_armed=armed)


# --- the narrowing rule -------------------------------------------------------


def test_armed_session_queues_what_the_page_opted_into(tmp_path):
    srv = _image_server(tmp_path, armed=True)
    srv.start()
    try:
        requests.post(srv.url + "trash", headers=HDR, json={"ids": [0], "icloud": True})
        assert srv.outcome.icloud == ["2026/07/img0.jpg"]
    finally:
        srv.close()


def test_page_can_decline_icloud_for_a_round(tmp_path):
    srv = _image_server(tmp_path, armed=True)
    srv.start()
    try:
        requests.post(srv.url + "trash", headers=HDR, json={"ids": [0], "icloud": False})
        requests.post(srv.url + "trash", headers=HDR, json={"ids": [1], "icloud": True})

        assert srv.outcome.moved == ["2026/07/img0.jpg", "2026/07/img1.jpg"]
        assert srv.outcome.icloud == ["2026/07/img1.jpg"]     # only the opted-in round
    finally:
        srv.close()


def test_an_unarmed_session_ignores_a_page_that_asks_for_icloud(tmp_path):
    """The whole point: the page cannot widen the terminal's authorisation."""
    srv = _image_server(tmp_path, armed=False)
    srv.start()
    try:
        requests.post(srv.url + "trash", headers=HDR, json={"ids": [0], "icloud": True})

        assert srv.outcome.moved == ["2026/07/img0.jpg"]
        assert srv.outcome.icloud == []
    finally:
        srv.close()


def test_video_review_follows_the_same_rule(tmp_path):
    srv = _video_server(tmp_path, armed=False)
    srv.start()
    try:
        requests.post(srv.url + "trash", headers=HDR, json={"ids": [0], "icloud": True})
        assert srv.outcome.icloud == []
    finally:
        srv.close()

    srv = _video_server(tmp_path, armed=True)
    srv.start()
    try:
        requests.post(srv.url + "trash", headers=HDR, json={"ids": [1], "icloud": True})
        assert srv.outcome.icloud == ["2026/07/vid1.mov"]
    finally:
        srv.close()


def test_only_files_that_actually_moved_are_queued(tmp_path):
    def refuse(paths):
        return [TrashResult(path=p, ok=False, error="nope") for p in paths]

    srv = ReviewServer(thumbs_dir=tmp_path, trash_fn=refuse, token="tok",
                       port=0, icloud_armed=True)
    path = tmp_path / "img0.jpg"
    path.write_bytes(b"x" * 10)
    srv.publish(FlaggedItem(index=0, path=path, rel="2026/07/img0.jpg",
                            category="meme", confidence=0.9, reason="r", size=10))
    srv.start()
    try:
        requests.post(srv.url + "trash", headers=HDR, json={"ids": [0], "icloud": True})
        assert srv.outcome.icloud == []          # it never left the disk
    finally:
        srv.close()


# --- the size evidence --------------------------------------------------------


def test_size_is_measured_at_trash_time_not_scan_time(tmp_path):
    """A local-clean session runs for hours; the scan-time size can be stale."""
    srv = _image_server(tmp_path, armed=True, n=1)
    (tmp_path / "img0.jpg").write_bytes(b"y" * 999)      # grew since the scan
    srv.start()
    try:
        requests.post(srv.url + "trash", headers=HDR, json={"ids": [0], "icloud": True})
        assert srv.outcome.sizes["2026/07/img0.jpg"] == 999
    finally:
        srv.close()


def test_sizes_are_recorded_for_videos_too(tmp_path):
    srv = _video_server(tmp_path, armed=True, n=1)
    srv.start()
    try:
        requests.post(srv.url + "trash", headers=HDR, json={"ids": [0], "icloud": True})
        assert srv.outcome.sizes["2026/07/vid0.mov"] == 10
    finally:
        srv.close()


# --- what the page and the finish payload say ---------------------------------


def test_finish_payload_reports_the_icloud_queue(tmp_path):
    srv = _image_server(tmp_path, armed=True)
    srv.start()
    try:
        requests.post(srv.url + "trash", headers=HDR, json={"ids": [0], "icloud": True})
        payload = requests.post(srv.url + "finish", headers=HDR, json={}).json()

        assert payload["icloud_armed"] is True
        assert payload["icloud"] == 1
    finally:
        srv.close()


def test_unarmed_pages_show_nothing_about_icloud():
    for html in (render_page("tok"), render_video_page("tok", [])):
        assert "const ICLOUD_ARMED = false;" in html
        assert "__ICLOUD__" not in html


def test_armed_pages_show_the_banner_and_the_opt_out():
    for html in (render_page("tok", True), render_video_page("tok", [], True)):
        assert "const ICLOUD_ARMED = true;" in html
        assert 'id="icloudBanner"' in html
        assert 'id="icloudBox"' in html
        # the destructive confirm must name both effects
        assert "AND queue them for deletion" in html
        assert "confirm the iCloud part in the terminal" in html


def _script_of(html: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(blocks) == 1, f"expected one script block, got {len(blocks)}"
    return blocks[0]


@pytest.mark.parametrize("armed", [False, True])
def test_page_script_has_no_string_literal_split_across_lines(armed):
    """The templates are plain (non-raw) Python strings, so a ``\\n`` written for
    JavaScript becomes a real newline and splits the JS string it sits in. That
    is a syntax error for the *whole* script block, so the page renders nothing
    and sits on "Loading…" forever — write ``\\\\n`` in the template instead.

    JavaScript has no multi-line plain string, so an odd number of unescaped
    ``"`` (or backtick) on a line means exactly that mistake.
    """
    items = [VideoItem(index=0, path=Path("a.mp4"), rel="a.mp4",
                       size=1, mtime_ns=0)]     # never opened; only rendered
    for html in (render_page("tok", armed), render_video_page("tok", items, armed)):
        offenders = [
            (n, line) for n, line in enumerate(_script_of(html).splitlines(), 1)
            # drop escaped chars first: \" and \` are not delimiters
            if (stripped := re.sub(r"\\.", "", line)).count('"') % 2
            or stripped.count("`") % 2
        ]
        assert not offenders, f"unterminated JS string literal: {offenders}"


def test_default_do_trash_call_still_works_unarmed(tmp_path):
    """Callers that predate this feature keep their behaviour."""
    srv = _image_server(tmp_path, armed=False, n=1)
    outcome = srv.do_trash([0])
    assert outcome.moved == ["2026/07/img0.jpg"]
    assert srv.outcome.icloud == []
    srv.close()
