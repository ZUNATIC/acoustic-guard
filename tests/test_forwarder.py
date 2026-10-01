import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.config import get_settings
from app.forwarder import BackendForwarder, build_alert_event
from app.threat_engine import ThreatEngine


class _Backend(BaseHTTPRequestHandler):
    received: list = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((dict(self.headers), body))
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def backend():
    _Backend.received = []
    _Backend.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Backend)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture
def forwarder(monkeypatch, backend, use_store):
    s = get_settings()
    monkeypatch.setattr(s, "backend_url", f"http://127.0.0.1:{backend.server_port}/api/events")
    monkeypatch.setattr(s, "backend_token", "tok-123")
    monkeypatch.setattr(s, "backend_hmac_secret", "shared-secret")
    return BackendForwarder()


def _event(severity_text="root password"):
    result = ThreatEngine().evaluate(severity_text)
    return build_alert_event(7, result, {"language_probability": 0.99}, 850)


def test_event_is_metadata_only_by_default():
    event = _event()
    assert event["type"] == "acoustic_alert" and event["module"] == "acoustic_guard"
    assert event["severity"] == "CRITICAL" and "root password" in event["keywords"]
    assert "transcript" not in event


def test_delivery_is_authenticated_and_signed(forwarder):
    forwarder.enqueue(_event())
    assert forwarder.flush_once() == 1
    headers, body = _Backend.received[0]
    headers = {k.lower(): v for k, v in headers.items()}
    assert headers["authorization"] == "Bearer tok-123"
    ts = headers["x-exfilguard-timestamp"]
    expected = hmac.new(b"shared-secret", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert headers["x-exfilguard-signature"] == f"sha256={expected}"
    assert json.loads(body)["alert_id"] == 7


def test_failed_delivery_is_retried_later(forwarder, use_store):
    _Backend.status = 503
    forwarder.enqueue(_event())
    assert forwarder.flush_once() == 0
    stats = use_store.outbox_stats()
    assert stats["pending"] == 1 and stats["last_error"] == "HTTP 503"
    assert use_store.due_events() == []


def test_low_severity_not_forwarded(forwarder, use_store):
    assert forwarder.enqueue(_event("the weather is nice")) is None
    assert use_store.outbox_stats()["pending"] == 0


def test_disabled_without_url(use_store):
    f = BackendForwarder()
    assert not f.enabled
    assert f.enqueue(_event()) is None
