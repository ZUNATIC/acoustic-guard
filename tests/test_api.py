import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from tests.conftest import FIXTURES


@pytest.fixture
def client(use_store):
    with TestClient(app) as c:
        yield c


def test_health(client):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok" and body["version"].startswith("2.")
    assert body["models_loaded"]["keyword_engine"] is True
    assert body["module_state"] in ("ready", "disabled_no_hardware", "active")


def test_capabilities_and_languages(client):
    caps = client.get("/api/v1/capabilities").json()
    assert caps["module"] == "acoustic_guard"
    assert caps["features"]["multilingual_speech"] is True
    assert "ur" in caps["keyword_registry"]["languages"]
    langs = client.get("/api/v1/languages").json()
    assert langs["remap"] == {"hi": "ur"}


def test_status(client):
    body = client.get("/api/v1/status").json()
    assert body["monitoring_active"] is False
    assert body["models"]["speech"] == get_settings().whisper_model


def test_analyze_benign(client):
    body = client.post("/api/v1/analyze", json={"text": "let's grab lunch after the meeting"}).json()
    assert body["is_violation"] is False and body["severity"] == "NONE"


def test_analyze_credential_leak(client):
    body = client.post("/api/v1/analyze", json={"text": "this is the top secret database root password"}).json()
    assert body["is_violation"] is True and body["severity"] == "CRITICAL"
    assert any(m["category"] == "credentials" for m in body["matches"])


def test_analyze_urdu_and_translation(client):
    body = client.post("/api/v1/analyze", json={"text": "میرا ڈیٹا بیس کا پاس ورڈ یہ ہے", "language": "ur"}).json()
    assert body["severity"] == "CRITICAL" and body["language"] == "ur"
    body = client.post("/api/v1/analyze", json={"text": "کچھ اور", "translation": "share the admin password"}).json()
    assert body["is_violation"] and "translation" in body["sources"]


def test_analyze_rejects_empty_text(client):
    assert client.post("/api/v1/analyze", json={"text": ""}).status_code == 422


def test_alerts_empty_after_purge(client):
    client.delete("/api/v1/alerts")
    body = client.get("/api/v1/alerts").json()
    assert body["total"] == 0


def test_privacy_manifest(client):
    body = client.get("/api/v1/privacy/manifest").json()
    assert body["raw_audio_ever_stored"] is False and body["raw_audio_ever_transmitted"] is False
    assert body["processing_location"] == "local-only"
    assert "transcript" not in body["fields_sent_to_backend"]


def test_monitored_keywords_are_disclosed(client):
    body = client.get("/api/v1/policy/keywords").json()
    creds = body["categories"]["credentials"]
    assert "root password" in creds["en"] and creds["ur"]
    assert "cnic" in body["patterns"]


def test_stream_start_requires_consent(client, monkeypatch):
    import app.hardware as hardware

    monitor = hardware.get_hardware_monitor()
    monkeypatch.setattr(monitor, "devices", [{"index": 0, "name": "Test Mic", "channels": 1, "is_default": True}])
    r = client.post("/api/v1/stream/start", json={"consent_acknowledged": False})
    assert r.status_code == 428 and r.json()["status"] == "consent_required"


def test_stream_start_without_microphone(client, monkeypatch):
    import app.hardware as hardware

    monkeypatch.setattr(get_settings(), "simulate_no_microphone", True)
    monkeypatch.setattr(hardware.get_hardware_monitor(), "devices", [])
    r = client.post("/api/v1/stream/start", json={"consent_acknowledged": True, "operator": "tester"})
    assert r.status_code == 409 and r.json()["status"] == "disabled_no_hardware"


def test_audit_verify_and_consent(client):
    assert client.get("/api/v1/audit/verify").json()["valid"] is True
    assert "records" in client.get("/api/v1/consent").json()


def test_csv_export(client, use_store):
    use_store.insert_alert("root password", 0.95, "CRITICAL", 0.95, 0.0,
                           [{"category": "credentials", "phrase": "root password", "weight": 0.95}],
                           language="en", categories=["credentials"])
    r = client.get("/api/v1/alerts/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].lstrip("﻿").startswith("id,time,severity")
    assert "CRITICAL" in lines[1]


def test_stats(client):
    body = client.get("/api/v1/stats").json()
    assert {"total_alerts", "by_severity", "by_language", "session"} <= set(body)


def test_environment_endpoint(client):
    body = client.get("/api/v1/environment").json()
    assert "live" in body and "events" in body


def test_policy_validation_rejects_bad_registry(client):
    r = client.put("/api/v1/policy", json={"categories": {}})
    assert r.status_code == 422


def test_policy_roundtrip(client, tmp_path, monkeypatch):
    from app.config import load_threat_registry
    from app.keyword_engine import reset_keyword_engine
    from app.threat_engine import reset_threat_engine

    registry = load_threat_registry()
    monkeypatch.setattr(get_settings(), "threats_config_path", tmp_path / "threats.yaml")
    try:
        assert client.put("/api/v1/policy", json=registry).json()["status"] == "policy_updated"
        assert (tmp_path / "threats.yaml").exists()
        assert client.post("/api/v1/analyze", json={"text": "root password"}).json()["is_violation"] is True
    finally:
        load_threat_registry.cache_clear()
        reset_keyword_engine()
        reset_threat_engine()


def test_upload_rejects_non_wav(client):
    r = client.post("/api/v1/analyze/audio", files={"file": ("x.wav", b"nope", "audio/wav")})
    assert r.status_code == 415


@pytest.mark.models
def test_upload_urdu_clip(client, use_store):
    data = (FIXTURES / "ur_leak.wav").read_bytes()
    r = client.post("/api/v1/analyze/audio", files={"file": ("ur_leak.wav", data, "audio/wav")}, data={"store": "true"})
    body = r.json()
    assert r.status_code == 200
    assert body["language"] == "ur" and body["severity"] in ("HIGH", "CRITICAL")
    assert body["alert_id"] and use_store.list_alerts()[0] == 1


def test_websocket_hello(client):
    with client.websocket_connect("/api/v1/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and "monitoring_active" in hello
