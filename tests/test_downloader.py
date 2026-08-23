"""Downloader resume/restart tests.

The HTTP-level tests run a real local server (range-capable and range-ignoring)
through the real ICloudClient.open_stream + requests, so the actual Range
negotiation is exercised. Cancellation, transient-retry and integrity tests use
a deterministic fake client where timing matters.
"""

import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from icloud_photo_sync import downloader as dlmod
from icloud_photo_sync import metadata as md
from icloud_photo_sync.config import AppConfig
from icloud_photo_sync.downloader import Downloader
from icloud_photo_sync.errors import OperationCancelled, TransientError
from icloud_photo_sync.icloud_client import ICloudClient
from icloud_photo_sync.models import AssetRef, DownloadOutcome
from icloud_photo_sync.state import StateStore
from threading import Event


# --- helpers -----------------------------------------------------------------


def build_cfg(tmp_path, **kw):
    return AppConfig.create(
        "t@e.com",
        tmp_path / "out",
        config_root=tmp_path / "cfg",
        chunk_size=4096,
        max_retries=2,
        backoff_base=1.0,
        backoff_cap=0.01,
        show_progress=False,
        **kw,
    )


class FakeService:
    def __init__(self, session):
        self.session = session


class FakeRaw:
    def __init__(self, url):
        self._url = url
        self._resources = None

    def download_url(self, version="original"):
        return self._url

    def _refresh_from_library(self):
        return True


def make_asset(blob, url=None, id="a1", filename="IMG.HEIC", size="auto", capture_dt=None):
    return AssetRef(
        id=id, filename=filename, capture_dt=capture_dt, added_dt=None,
        size=(len(blob) if size == "auto" else size),
        raw=FakeRaw(url) if url else None,
    )


def start_server(blob, mode):
    """mode: 'range' honours Range; 'no-range' always 200; 'lying-206'
    answers Range requests with a 206 that actually starts at byte 0;
    'always-503' returns 503."""
    log = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range")
            log.append(rng)
            if mode == "always-503":
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if mode == "lying-206" and rng:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes 0-{len(blob)-1}/{len(blob)}")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
            if mode == "range" and rng and rng.startswith("bytes="):
                start = int(rng[len("bytes="):].split("-")[0])
                chunk = blob[start:]
                self.send_response(206)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{len(blob)-1}/{len(blob)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
            else:
                self.send_response(200)
                if mode == "range":
                    self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], log


def http_downloader(tmp_path):
    cfg = build_cfg(tmp_path)
    state = StateStore(cfg.state_db)
    client = ICloudClient(FakeService(requests.Session()), cfg)
    return Downloader(client, state, cfg), state


# --- HTTP-level tests --------------------------------------------------------


def test_full_download(tmp_path):
    blob = os.urandom(20000)
    srv, port, _ = start_server(blob, mode="range")
    dl, state = http_downloader(tmp_path)
    try:
        asset = make_asset(blob, url=f"http://127.0.0.1:{port}/f")
        dest = tmp_path / "out.bin"
        state.register(asset, dest.name)
        assert dl.download(asset, dest) == DownloadOutcome.DOWNLOADED
        assert dest.read_bytes() == blob
        assert not dest.with_name("out.bin.part").exists()
        assert state.get("a1")["status"] == "completed"
    finally:
        srv.shutdown(); state.close()


def test_stamps_capture_date_on_a_completed_download(tmp_path, monkeypatch):
    """The hook this session added: a fresh download with a known capture
    date gets it stamped in, using the asset's own timezone offset when the
    engine exposes one."""
    blob = os.urandom(2000)
    srv, port, _ = start_server(blob, mode="range")
    dl, state = http_downloader(tmp_path)
    calls = []

    def fake_ensure(path, capture_dt, *, tz_offset_seconds=None):
        calls.append((path, capture_dt, tz_offset_seconds))
        return md.MetadataOutcome.STAMPED

    monkeypatch.setattr(dlmod.md, "ensure_capture_date", fake_ensure)
    monkeypatch.setattr(dlmod, "asset_timezone_offset", lambda raw: 3 * 3600)
    try:
        capture_dt = datetime(2019, 10, 25, tzinfo=timezone.utc)
        asset = make_asset(blob, url=f"http://127.0.0.1:{port}/f", capture_dt=capture_dt)
        dest = tmp_path / "out.bin"
        state.register(asset, dest.name)

        assert dl.download(asset, dest) == DownloadOutcome.DOWNLOADED

        assert len(calls) == 1
        stamped_path, stamped_dt, tz = calls[0]
        assert stamped_path == dest
        assert stamped_dt == capture_dt
        assert tz == 3 * 3600
    finally:
        srv.shutdown(); state.close()


def test_no_stamp_call_when_capture_date_unknown(tmp_path, monkeypatch):
    blob = os.urandom(2000)
    srv, port, _ = start_server(blob, mode="range")
    dl, state = http_downloader(tmp_path)
    calls = []
    monkeypatch.setattr(
        dlmod.md, "ensure_capture_date",
        lambda *a, **kw: calls.append(1) or md.MetadataOutcome.STAMPED,
    )
    try:
        asset = make_asset(blob, url=f"http://127.0.0.1:{port}/f", capture_dt=None)
        dest = tmp_path / "out.bin"
        state.register(asset, dest.name)

        assert dl.download(asset, dest) == DownloadOutcome.DOWNLOADED
        assert calls == []
    finally:
        srv.shutdown(); state.close()


def test_download_still_succeeds_when_stamping_reports_failed(tmp_path, monkeypatch):
    """Best-effort boundary: a stamping FAILED/TOOL_UNAVAILABLE outcome must
    never turn a successful, byte-verified download into a failure."""
    blob = os.urandom(2000)
    srv, port, _ = start_server(blob, mode="range")
    dl, state = http_downloader(tmp_path)
    monkeypatch.setattr(
        dlmod.md, "ensure_capture_date",
        lambda *a, **kw: md.MetadataOutcome.TOOL_UNAVAILABLE,
    )
    try:
        capture_dt = datetime(2019, 10, 25, tzinfo=timezone.utc)
        asset = make_asset(blob, url=f"http://127.0.0.1:{port}/f", capture_dt=capture_dt)
        dest = tmp_path / "out.bin"
        state.register(asset, dest.name)

        assert dl.download(asset, dest) == DownloadOutcome.DOWNLOADED
        assert dest.read_bytes() == blob
        assert state.get("a1")["status"] == "completed"
    finally:
        srv.shutdown(); state.close()


def test_skip_when_already_complete(tmp_path):
    blob = os.urandom(5000)
    srv, port, log = start_server(blob, mode="range")
    dl, state = http_downloader(tmp_path)
    try:
        asset = make_asset(blob, url=f"http://127.0.0.1:{port}/f")
        dest = tmp_path / "out.bin"
        dest.write_bytes(blob)  # already there, correct size
        state.register(asset, dest.name)
        assert dl.download(asset, dest) == DownloadOutcome.SKIPPED
        assert log == []  # never hit the network
    finally:
        srv.shutdown(); state.close()


def test_resume_with_range(tmp_path):
    blob = os.urandom(20000)
    half = 8000
    srv, port, log = start_server(blob, mode="range")
    dl, state = http_downloader(tmp_path)
    try:
        asset = make_asset(blob, url=f"http://127.0.0.1:{port}/f")
        dest = tmp_path / "out.bin"
        dest.with_name("out.bin.part").write_bytes(blob[:half])  # interrupted transfer
        state.register(asset, dest.name)
        assert dl.download(asset, dest) == DownloadOutcome.DOWNLOADED
        assert dest.read_bytes() == blob
        assert any(r and r.startswith(f"bytes={half}-") for r in log)  # resumed
        assert not dest.with_name("out.bin.part").exists()
    finally:
        srv.shutdown(); state.close()


def test_restart_when_range_ignored(tmp_path):
    blob = os.urandom(20000)
    half = 8000
    srv, port, log = start_server(blob, mode="no-range")  # ignores Range
    dl, state = http_downloader(tmp_path)
    try:
        asset = make_asset(blob, url=f"http://127.0.0.1:{port}/f")
        dest = tmp_path / "out.bin"
        # Pre-existing .part has GARBAGE — a correct restart must overwrite it.
        dest.with_name("out.bin.part").write_bytes(b"X" * half)
        state.register(asset, dest.name)
        assert dl.download(asset, dest) == DownloadOutcome.DOWNLOADED
        assert dest.read_bytes() == blob  # clean, not contaminated by the X's
        assert not dest.with_name("out.bin.part").exists()
    finally:
        srv.shutdown(); state.close()


# --- deterministic fake-client tests -----------------------------------------


class FakeResp:
    def __init__(self, chunks, total=None):
        self._chunks = chunks
        self.headers = {"Content-Length": str(total)} if total is not None else {}
        self.status_code = 200

    def iter_content(self, n):
        for c in self._chunks:
            yield c

    def close(self):
        pass


class FakeClient:
    def __init__(self, factory):
        self._factory = factory
        self.calls = 0

    def open_stream(self, asset, byte_offset=0):
        self.calls += 1
        return self._factory(self.calls, byte_offset)

    def refresh_asset(self, asset):
        return True


def fake_downloader(tmp_path, factory, cancel=None):
    cfg = build_cfg(tmp_path)
    state = StateStore(cfg.state_db)
    return Downloader(FakeClient(factory), state, cfg, cancel), state


def test_cancel_midstream_leaves_resumable_part(tmp_path):
    c0, c1 = os.urandom(3000), os.urandom(3000)
    blob = c0 + c1
    cancel = Event()

    class CancelResp(FakeResp):
        def iter_content(self, n):
            yield c0
            cancel.set()  # fire between chunks
            yield c1

    def factory(calls, offset):
        return CancelResp([], total=len(blob)), False, len(blob)

    dl, state = fake_downloader(tmp_path, factory, cancel=cancel)
    try:
        asset = make_asset(blob)
        dest = tmp_path / "out.bin"
        state.register(asset, dest.name)
        with pytest.raises(OperationCancelled):
            dl.download(asset, dest)
        part = dest.with_name("out.bin.part")
        assert part.exists() and part.read_bytes() == c0
        assert not dest.exists()
        assert state.get("a1")["bytes_done"] == len(c0)
    finally:
        state.close()


def test_transient_retry_then_success(tmp_path):
    blob = os.urandom(5000)

    def factory(calls, offset):
        if calls == 1:
            raise TransientError("boom")
        return FakeResp([blob], total=len(blob)), False, len(blob)

    dl, state = fake_downloader(tmp_path, factory)
    try:
        asset = make_asset(blob)
        dest = tmp_path / "out.bin"
        state.register(asset, dest.name)
        assert dl.download(asset, dest) == DownloadOutcome.DOWNLOADED
        assert dest.read_bytes() == blob
        assert dl.client.calls >= 2
    finally:
        state.close()


def test_integrity_mismatch_fails(tmp_path):
    blob = os.urandom(5000)

    def factory(calls, offset):
        # Always 5 bytes short of the expected size → integrity failure.
        return FakeResp([blob[:-5]], total=len(blob)), False, len(blob)

    dl, state = fake_downloader(tmp_path, factory)
    try:
        asset = make_asset(blob)
        dest = tmp_path / "out.bin"
        state.register(asset, dest.name)
        assert dl.download(asset, dest) == DownloadOutcome.FAILED
        assert state.get("a1")["status"] == "failed"
        assert not dest.exists()
        assert not dest.with_name("out.bin.part").exists()
    finally:
        state.close()


# --- fix regressions: overwrite refusal, range validation, raw-status path ----


def test_refuse_overwrite_wrong_size(tmp_path):
    """A pre-existing file at dest that we can't verify is never overwritten."""
    blob = os.urandom(5000)
    srv, port, log = start_server(blob, mode="range")
    dl, state = http_downloader(tmp_path)
    try:
        asset = make_asset(blob, url=f"http://127.0.0.1:{port}/f")
        dest = tmp_path / "out.bin"
        foreign = b"U" * 1234  # user's own file, wrong size
        dest.write_bytes(foreign)
        state.register(asset, dest.name)
        assert dl.download(asset, dest) == DownloadOutcome.FAILED
        assert dest.read_bytes() == foreign  # untouched
        row = state.get("a1")
        assert row["status"] == "failed"
        assert "refusing to overwrite" in row["error"]
        assert log == []  # never even opened a stream
    finally:
        srv.shutdown(); state.close()


def test_lying_206_restarts_from_zero(tmp_path):
    """A 206 whose Content-Range start is not the requested offset must not
    be appended after the existing prefix — restart clean instead."""
    blob = os.urandom(20000)
    half = 8000
    srv, port, log = start_server(blob, mode="lying-206")
    dl, state = http_downloader(tmp_path)
    try:
        asset = make_asset(blob, url=f"http://127.0.0.1:{port}/f")
        dest = tmp_path / "out.bin"
        dest.with_name("out.bin.part").write_bytes(b"X" * half)  # garbage prefix
        state.register(asset, dest.name)
        assert dl.download(asset, dest) == DownloadOutcome.DOWNLOADED
        assert dest.read_bytes() == blob  # not contaminated by the X's
    finally:
        srv.shutdown(); state.close()


class RaisingSession(requests.Session):
    """Mimics PyiCloudSession: plain .get()/.request() raises on non-2xx,
    while request_raw() returns the response untouched."""

    def request(self, method, url, **kw):
        resp = super().request(method, url, **kw)
        resp.raise_for_status()
        return resp

    def request_raw(self, method, url, **kw):
        return super().request(method, url, **kw)


def test_open_stream_sees_raw_status_via_request_raw(tmp_path):
    """With a PyiCloudSession-like session, a 503 must surface as
    ServiceUnavailableError (proving request_raw was used — through .get the
    status would be swallowed into a raise_for_status HTTPError)."""
    from icloud_photo_sync.errors import ServiceUnavailableError

    blob = os.urandom(100)
    srv, port, _ = start_server(blob, mode="always-503")
    cfg = build_cfg(tmp_path)
    client = ICloudClient(FakeService(RaisingSession()), cfg)
    try:
        asset = make_asset(blob, url=f"http://127.0.0.1:{port}/f")
        with pytest.raises(ServiceUnavailableError):
            client.open_stream(asset)
    finally:
        srv.shutdown()


def test_verify_against_server_total_when_size_unknown(tmp_path):
    """When iCloud reports no size, the server-advertised total is enforced."""
    blob = os.urandom(5000)

    def short_factory(calls, offset):
        return FakeResp([blob[:-5]], total=len(blob)), False, len(blob)

    dl, state = fake_downloader(tmp_path, short_factory)
    try:
        asset = make_asset(blob, size=None)
        dest = tmp_path / "out.bin"
        state.register(asset, dest.name)
        assert dl.download(asset, dest) == DownloadOutcome.FAILED
        assert not dest.exists()
    finally:
        state.close()

    def full_factory(calls, offset):
        return FakeResp([blob], total=len(blob)), False, len(blob)

    dl2, state2 = fake_downloader(tmp_path / "b", full_factory)
    try:
        asset = make_asset(blob, size=None, id="a2")
        dest = tmp_path / "b" / "out.bin"
        dest.parent.mkdir(parents=True, exist_ok=True)
        state2.register(asset, dest.name)
        assert dl2.download(asset, dest) == DownloadOutcome.DOWNLOADED
        assert dest.read_bytes() == blob
    finally:
        state2.close()
