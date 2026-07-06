"""Classifier tests against a local stub of the LM Studio endpoint.

Follows the real-local-server pattern from test_downloader.py so the actual
requests call, request body, and response parsing are exercised end to end.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from icloud_photo_sync.classifier import (
    Classification,
    LMStudioClassifier,
    prepare_image,
)
from icloud_photo_sync.errors import ClassificationError, ClassifierUnavailableError


def start_stub(reply):
    """reply: dict controlling the /v1/chat/completions response.

    Keys: 'status' (int), 'content' (str returned as message content), or
    'raw' (full JSON body string). Records each POST body into `bodies`.
    """
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/v1/models":
                self._send(200, json.dumps({"data": [{"id": "m"}]}))
            else:
                self._send(404, "{}")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            bodies.append(json.loads(self.rfile.read(length)))
            status = reply.get("status", 200)
            if "raw" in reply:
                self._send(status, reply["raw"])
            else:
                content = reply.get("content", "")
                self._send(status, json.dumps(
                    {"choices": [{"message": {"content": content}}]}
                ))

        def _send(self, code, body):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], bodies


def _classifier(port, tmp_path):
    return LMStudioClassifier(
        base_url=f"http://127.0.0.1:{port}",
        model="test-model",
        timeout=(5, 5),
        max_dim=512,
        work_dir=tmp_path,
    )


# A minimal but valid PNG so prepare_image's raw fallback has real bytes.
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _png(tmp_path, name="x.png"):
    p = tmp_path / name
    p.write_bytes(_PNG)
    return p


def test_classify_happy_path_and_request_shape(tmp_path):
    content = json.dumps({"category": "meme", "confidence": 0.93, "reason": "joke"})
    srv, port, bodies = start_stub({"content": content})
    try:
        c = _classifier(port, tmp_path).classify(_png(tmp_path))
        assert c == Classification("meme", 0.93, "joke")
        body = bodies[0]
        assert body["reasoning_effort"] == "none"
        assert body["response_format"]["json_schema"]["strict"] is True
        parts = body["messages"][0]["content"]
        assert any(p.get("type") == "image_url"
                   and p["image_url"]["url"].startswith("data:image/")
                   for p in parts)
    finally:
        srv.shutdown()


def test_classify_clamps_confidence(tmp_path):
    content = json.dumps({"category": "photo", "confidence": 5, "reason": "r"})
    srv, port, _ = start_stub({"content": content})
    try:
        c = _classifier(port, tmp_path).classify(_png(tmp_path))
        assert c.confidence == 1.0
    finally:
        srv.shutdown()


def test_classify_bad_json_raises(tmp_path):
    srv, port, _ = start_stub({"content": "not json at all"})
    try:
        with pytest.raises(ClassificationError):
            _classifier(port, tmp_path).classify(_png(tmp_path))
    finally:
        srv.shutdown()


def test_classify_unknown_category_raises(tmp_path):
    content = json.dumps({"category": "banana", "confidence": 0.5, "reason": "r"})
    srv, port, _ = start_stub({"content": content})
    try:
        with pytest.raises(ClassificationError):
            _classifier(port, tmp_path).classify(_png(tmp_path))
    finally:
        srv.shutdown()


def test_classify_http_500_raises(tmp_path):
    srv, port, _ = start_stub({"status": 500, "raw": "{}"})
    try:
        with pytest.raises(ClassificationError):
            _classifier(port, tmp_path).classify(_png(tmp_path))
    finally:
        srv.shutdown()


def test_check_available_ok(tmp_path):
    srv, port, _ = start_stub({})
    try:
        _classifier(port, tmp_path).check_available()  # no raise
    finally:
        srv.shutdown()


def test_check_available_down_names_url(tmp_path):
    # Nothing listening on this port.
    c = LMStudioClassifier(
        base_url="http://127.0.0.1:9",
        model="m", timeout=(1, 1), max_dim=512, work_dir=tmp_path,
    )
    with pytest.raises(ClassifierUnavailableError) as exc:
        c.check_available()
    assert "127.0.0.1:9" in str(exc.value)


def test_prepare_image_raw_fallback(tmp_path, monkeypatch):
    import icloud_photo_sync.classifier as clf

    def boom(*a, **k):
        raise FileNotFoundError("no sips")

    monkeypatch.setattr(clf.subprocess, "run", boom)
    data, mime = prepare_image(_png(tmp_path), 512, tmp_path)
    assert data == _PNG
    assert mime == "image/png"
