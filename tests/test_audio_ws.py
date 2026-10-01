import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

import app.pipeline as pipeline_module
from app.audio_stream import get_engine
from app.config import get_settings
from app.main import app
from app.transcriber import Transcription


class ScriptedVAD:
    """speech for the first `speech` chunks, silence afterwards"""

    def __init__(self, speech: int):
        self.left = speech

    def process_chunk(self, chunk):
        self.left -= 1
        return 0.9 if self.left >= 0 else 0.0

    def reset(self):
        pass


class FakeTranscriber:
    def __init__(self):
        self.lengths = []

    def transcribe(self, audio, translate=None, language=None, translate_if=None):
        self.lengths.append(len(audio))
        return Transcription(text="میرا ڈیٹا بیس کا پاس ورڈ یہ ہے", language="ur",
                             language_probability=0.97, duration=len(audio) / 16000)


@pytest.fixture
def client(use_store, monkeypatch):
    fake = FakeTranscriber()
    monkeypatch.setattr(pipeline_module, "get_transcriber", lambda: fake)
    monkeypatch.setattr(pipeline_module, "get_semantic_scorer", lambda: None)
    monkeypatch.setattr(get_settings(), "kws_enabled", False)
    monkeypatch.setattr(get_settings(), "verifier_model", "")
    engine = get_engine()
    monkeypatch.setattr(engine, "kws", None)
    monkeypatch.setattr(engine, "_warm_models", lambda: None)
    engine.environment.enabled = False
    engine.noise.mode = "off"
    engine.vad = ScriptedVAD(speech=40)
    with TestClient(app) as c:
        c.fake = fake
        yield c
    if engine.is_running:
        engine.stop()
    engine.vad = None


def pcm(seconds: float) -> bytes:
    t = np.arange(int(16000 * seconds)) / 16000
    return (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes()


def test_browser_stream_produces_alert(client, use_store):
    engine = get_engine()
    with client.websocket_connect("/api/v1/audio") as ws:
        ws.send_json({"type": "start", "consent_acknowledged": True, "operator": "tester", "label": "USB Mic"})
        reply = ws.receive_json()
        assert reply["type"] == "started" and "USB Mic" in reply["device"]
        assert engine.source == "browser"
        data = pcm(3.0)
        for i in range(0, len(data), 3000):      # odd block size on purpose
            ws.send_bytes(data[i:i + 3000])
        deadline = time.time() + 10
        while time.time() < deadline and engine.metrics["transcribed"] == 0:
            time.sleep(0.1)
        ws.send_json({"type": "stop"})
    assert client.fake.lengths, "no segment reached the speech model"
    assert use_store.list_alerts()[0] == 1
    deadline = time.time() + 5
    while time.time() < deadline and engine.is_running:
        time.sleep(0.1)
    assert not engine.is_running
    assert [r["action"] for r in use_store.consent_history()] == ["stream_stop", "stream_start:browser"]


def test_browser_stream_requires_consent(client):
    with client.websocket_connect("/api/v1/audio") as ws:
        ws.send_json({"type": "start", "consent_acknowledged": False})
        reply = ws.receive_json()
        assert reply["type"] == "error" and reply["status"] == "consent_required"
    assert not get_engine().is_running


def test_browser_stream_needs_token(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(get_settings(), "api_token", "s3cret")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/v1/audio") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_disconnect_stops_monitoring(client):
    engine = get_engine()
    with client.websocket_connect("/api/v1/audio") as ws:
        ws.send_json({"type": "start", "consent_acknowledged": True})
        assert ws.receive_json()["type"] == "started"
    deadline = time.time() + 5
    while time.time() < deadline and engine.is_running:
        time.sleep(0.1)
    assert not engine.is_running
