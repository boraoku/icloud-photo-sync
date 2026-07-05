"""Downloader resume/restart tests.

The HTTP-level tests run a real local server (range-capable and range-ignoring)
through the real ICloudClient.open_stream + requests, so the actual Range
negotiation is exercised. Cancellation, transient-retry and integrity tests use
a deterministic fake client where timing matters.
"""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

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


def make_asset(blob, url=None, id="a1", filename="IMG.HEIC"):
    return AssetRef(
        id=id, filename=filename, capture_dt=None, added_dt=None,
        size=len(blob), raw=FakeRaw(url) if url else None,
    )


def start_server(blob, support_range):
    log = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range")
            log.append(rng)
            if support_range and rng and rng.startswith("bytes="):
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
                if support_range:
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
    srv, port, _ = start_server(blob, support_range=True)
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


def test_skip_when_already_complete(tmp_path):
    blob = os.urandom(5000)
    srv, port, log = start_server(blob, support_range=True)
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
    srv, port, log = start_server(blob, support_range=True)
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
    srv, port, log = start_server(blob, support_range=False)  # ignores Range
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
