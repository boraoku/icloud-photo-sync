"""Poster extraction + on-demand cache tests."""

import shutil
import threading
import time

import pytest

from icloud_photo_sync.poster import (
    PosterCache,
    extract_poster,
    format_duration,
    probe_durations,
)
from icloud_photo_sync.video_review import VideoItem

JPEG = b"\xff\xd8\xff" + b"fake jpeg body"


def _item(tmp_path, name="a.mov", content=b"video bytes", index=0):
    p = tmp_path / name
    p.write_bytes(content)
    st = p.stat()
    return VideoItem(index=index, path=p, rel=name, size=st.st_size,
                     mtime_ns=st.st_mtime_ns)


def _stub(payload=JPEG, calls=None, delay=0.0):
    """Extractor double: writes ``payload``, or fails when payload is None."""
    def extract(src, dest, max_dim, timeout):
        if calls is not None:
            calls.append(src)
        if delay:
            time.sleep(delay)
        if payload is None:
            return False
        dest.write_bytes(payload)
        return True
    return extract


# --- caching ------------------------------------------------------------------


def test_renders_once_then_serves_from_cache(tmp_path):
    calls = []
    cache = PosterCache(tmp_path / "cache", extract=_stub(calls=calls))
    item = _item(tmp_path)

    assert cache.get(item) == JPEG
    assert cache.get(item) == JPEG
    assert len(calls) == 1                      # second call came off disk


def test_failure_is_cached_so_it_is_not_retried(tmp_path):
    calls = []
    cache = PosterCache(tmp_path / "cache", extract=_stub(payload=None, calls=calls))
    item = _item(tmp_path)

    assert cache.get(item) is None
    assert cache.get(item) is None
    assert len(calls) == 1
    # the negative result is an empty file, not a missing one
    assert cache.path_for(item).stat().st_size == 0


def test_edited_file_is_re_rendered(tmp_path):
    calls = []
    cache = PosterCache(tmp_path / "cache", extract=_stub(calls=calls))
    item = _item(tmp_path)
    cache.get(item)

    item.path.write_bytes(b"different, longer content")
    st = item.path.stat()
    edited = VideoItem(index=0, path=item.path, rel=item.rel, size=st.st_size,
                       mtime_ns=st.st_mtime_ns)

    cache.get(edited)
    assert len(calls) == 2                      # size+mtime are part of the key
    assert cache.path_for(edited) != cache.path_for(item)


def test_a_failed_render_leaves_no_partial_file(tmp_path):
    def writes_then_fails(src, dest, max_dim, timeout):
        dest.write_bytes(b"truncated garbage")
        return False

    cache = PosterCache(tmp_path / "cache", extract=writes_then_fails)
    item = _item(tmp_path)

    assert cache.get(item) is None
    assert list((tmp_path / "cache").glob("*.part")) == []


# --- concurrency --------------------------------------------------------------


def test_concurrent_requests_share_one_render(tmp_path):
    calls = []
    cache = PosterCache(tmp_path / "cache", extract=_stub(calls=calls, delay=0.15))
    item = _item(tmp_path)
    results = []

    threads = [threading.Thread(target=lambda: results.append(cache.get(item)))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [JPEG] * 8
    assert len(calls) == 1                      # eight scrolls, one extraction


def test_extractions_are_capped_at_worker_count(tmp_path):
    live = 0
    peak = 0
    lock = threading.Lock()

    def slow(src, dest, max_dim, timeout):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.1)
        with lock:
            live -= 1
        dest.write_bytes(JPEG)
        return True

    cache = PosterCache(tmp_path / "cache", workers=2, extract=slow)
    items = [_item(tmp_path, name=f"v{i}.mov", content=b"x" * (i + 1), index=i)
             for i in range(6)]

    threads = [threading.Thread(target=cache.get, args=(it,)) for it in items]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak <= 2                            # a fast scroll can't fork six ffmpegs


# --- background rendering -----------------------------------------------------


def test_request_returns_immediately_and_renders_in_the_background(tmp_path):
    cache = PosterCache(tmp_path / "cache", extract=_stub(delay=0.3))
    item = _item(tmp_path)
    try:
        t0 = time.perf_counter()
        cache.request(item)
        assert time.perf_counter() - t0 < 0.1     # never waits on the render
        assert cache.get_cached(item) is None     # not there yet

        deadline = time.time() + 5
        while cache.get_cached(item) is None and time.time() < deadline:
            time.sleep(0.05)
        assert cache.get_cached(item) == JPEG
    finally:
        cache.close()


def test_newest_request_is_rendered_first(tmp_path):
    order = []
    started = threading.Event()

    def record(src, dest, max_dim, timeout):
        order.append(src.name)
        started.set()
        time.sleep(0.05)
        dest.write_bytes(JPEG)
        return True

    # One worker, so the queue order is exactly the render order.
    cache = PosterCache(tmp_path / "cache", workers=1, extract=record)
    items = [_item(tmp_path, name=f"v{i}.mov", content=b"x" * (i + 1), index=i)
             for i in range(5)]
    try:
        cache.request(items[0])
        started.wait(2)                # occupy the worker with the first item
        for it in items[1:]:
            cache.request(it)
        deadline = time.time() + 10
        while len(order) < 5 and time.time() < deadline:
            time.sleep(0.05)

        # v0 was already running; the rest are served newest-first, so the card
        # the user just scrolled to beats the backlog behind it.
        assert order[0] == "v0.mov"
        assert order[1:] == ["v4.mov", "v3.mov", "v2.mov", "v1.mov"]
    finally:
        cache.close()


def test_request_is_idempotent_while_queued(tmp_path):
    calls = []
    cache = PosterCache(tmp_path / "cache", workers=1, extract=_stub(calls=calls, delay=0.2))
    item = _item(tmp_path)
    try:
        for _ in range(10):            # ten retries from the page
            cache.request(item)
        deadline = time.time() + 5
        while cache.get_cached(item) is None and time.time() < deadline:
            time.sleep(0.05)
        assert len(calls) == 1
    finally:
        cache.close()


def test_close_stops_workers(tmp_path):
    cache = PosterCache(tmp_path / "cache", extract=_stub())
    cache.request(_item(tmp_path))
    cache.close()
    cache.close()                      # idempotent
    assert not any(t.is_alive() for t in cache._threads)


# --- durations ----------------------------------------------------------------


def test_format_duration_is_hh_mm_ss():
    assert format_duration(0) == ""
    assert format_duration(None) == ""
    assert format_duration(9.4) == "00:00:09"
    assert format_duration(75) == "00:01:15"
    assert format_duration(3671.4) == "01:01:11"
    assert format_duration(36000) == "10:00:00"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_probe_durations_reads_real_files(tmp_path):
    import subprocess

    src = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=3:size=320x240:rate=10", str(src)],
        check=True, capture_output=True,
    )
    missing = tmp_path / "nope.mov"
    missing.write_bytes(b"not a video")

    found = probe_durations([src, missing])
    assert 2.8 < found[src] < 3.2
    assert missing not in found        # unknown length is an absence, not a crash


# --- real extraction ----------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_extract_poster_produces_a_bounded_jpeg(tmp_path):
    import subprocess

    src = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=2:size=640x480:rate=10", str(src)],
        check=True, capture_output=True,
    )
    dest = tmp_path / "poster.jpg"

    assert extract_poster(src, dest, max_dim=160) is True
    assert dest.read_bytes()[:3] == b"\xff\xd8\xff"          # JPEG magic
    assert dest.stat().st_size < 100_000                     # a tile, not a frame


def test_extract_poster_reports_failure_for_a_non_video(tmp_path):
    src = tmp_path / "not-a-video.mov"
    src.write_bytes(b"definitely not a container")
    dest = tmp_path / "poster.jpg"

    assert extract_poster(src, dest, max_dim=160) is False
    assert not dest.exists()
