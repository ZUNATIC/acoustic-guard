import queue

import numpy as np
import pytest

import app.pipeline as pipeline_module
from app.audio_stream import AcousticGuardEngine
from app.pipeline import Pipeline
from app.transcriber import Transcription

CHUNK = 512


class FakeVAD:
    def __init__(self, probs):
        self.probs = list(probs)
        self.calls = 0

    def process_chunk(self, chunk):
        self.calls += 1
        return self.probs.pop(0) if self.probs else 0.0

    def reset(self):
        pass


class FakeTranscriber:
    def __init__(self, text, language="en", translation=None):
        self.text, self.language, self.translation = text, language, translation
        self.calls = []

    def transcribe(self, audio, translate=None, language=None, translate_if=None):
        self.calls.append(len(audio))
        return Transcription(text=self.text, language=self.language, language_probability=0.98,
                             translation=self.translation, duration=len(audio) / 16000, elapsed=0.01)


class FakeKwsModel:
    def __init__(self, text):
        self.text = text

    def analyze(self, audio, **kw):
        return {"text": self.text}


@pytest.fixture
def make_engine(monkeypatch, use_store):
    monkeypatch.setattr(pipeline_module, "get_semantic_scorer", lambda: None)

    def build(probs, text="", language="en", translation=None, kws_text=None, **settings):
        transcriber = FakeTranscriber(text, language, translation)
        monkeypatch.setattr(pipeline_module, "get_transcriber", lambda: transcriber)
        engine = AcousticGuardEngine(pipeline=Pipeline())
        for key, value in {"min_speech_chunks": 3, "silence_hangover_chunks": 2, "max_speech_seconds": 10.0, **settings}.items():
            monkeypatch.setattr(engine.settings, key, value)
        engine.vad = FakeVAD(probs)
        engine.synchronous = True
        engine.noise.mode = "off"
        engine.environment.enabled = False
        if kws_text is None:
            engine.kws = None
        else:
            engine.kws._model = FakeKwsModel(kws_text)
            engine.kws.window_samples = CHUNK * 4
            engine.kws.hop_samples = CHUNK * 2
        events = []
        engine.on_telemetry(events.append)
        engine.is_running = True
        return engine, transcriber, events

    return build


def feed(engine, n, value=0.05):
    for _ in range(n):
        engine.process_chunk(np.full(CHUNK, value, dtype=np.float32))


def of_type(events, kind):
    return [e for e in events if e.get("type") == kind]


def test_silence_never_triggers_transcription(make_engine):
    engine, transcriber, _ = make_engine([0.0] * 20, "should not be called")
    feed(engine, 20)
    assert transcriber.calls == []


def test_speech_then_silence_flushes_and_alerts(make_engine, use_store):
    engine, transcriber, events = make_engine([0.9] * 4 + [0.0] * 3, "this contains the root password")
    feed(engine, 7)
    assert len(transcriber.calls) == 1
    transcripts = of_type(events, "transcript")
    assert transcripts[-1]["severity"] == "CRITICAL" and transcripts[-1]["alert_id"]
    assert use_store.list_alerts()[0] == 1
    assert use_store.verify_chain()["valid"]
    assert engine.metrics["alerts"] == 1


def test_preroll_audio_is_kept(make_engine):
    engine, transcriber, _ = make_engine([0.0] * 5 + [0.9] * 4 + [0.0] * 3, "hello there friend")
    feed(engine, 12)
    # 5 pre-speech chunks, 4 speech chunks, 2 hangover chunks
    assert transcriber.calls[0] == CHUNK * 11


def test_short_blip_is_discarded(make_engine):
    engine, transcriber, _ = make_engine([0.9, 0.9, 0.0, 0.0, 0.0], "too short")
    feed(engine, 5)
    assert transcriber.calls == []
    assert engine.metrics["discarded_segments"] == 1


def test_long_speech_is_split_at_max_length(make_engine):
    engine, transcriber, _ = make_engine([0.9] * 70, "long talk", max_speech_seconds=1.0)
    feed(engine, 70)
    assert len(transcriber.calls) == 2


def test_benign_speech_updates_telemetry_without_alert(make_engine, use_store):
    engine, _, events = make_engine([0.9] * 4 + [0.0] * 3, "let's grab lunch later")
    feed(engine, 7)
    assert engine.latest_telemetry["transcript"] == "let's grab lunch later"
    assert engine.latest_telemetry["severity"] == "NONE"
    assert use_store.list_alerts()[0] == 0


def test_urdu_segment_with_translation(make_engine):
    engine, _, events = make_engine(
        [0.9] * 4 + [0.0] * 3, "میرا ڈیٹا بیس کا پاس ورڈ یہ ہے", language="ur",
        translation="My database password is this",
    )
    feed(engine, 7)
    event = of_type(events, "transcript")[-1]
    assert event["language"] == "ur" and event["severity"] == "CRITICAL"
    assert engine.session_language == "ur"


def test_keyword_spotting_raises_early_warning_before_flush(make_engine):
    engine, _, events = make_engine([0.9] * 12 + [0.0] * 3, "unclear words", kws_text="give me the root password")
    feed(engine, 10)
    warnings = of_type(events, "early_warning")
    assert warnings and warnings[0]["category"] == "credentials"
    assert not of_type(events, "transcript")
    feed(engine, 5)
    final = of_type(events, "transcript")[-1]
    assert "kws" in final["sources"] and final["severity"] == "CRITICAL"


def test_level_telemetry_emitted(make_engine):
    engine, _, events = make_engine([0.0] * 3)
    feed(engine, 3)
    level = of_type(events, "level")[0]
    assert {"db", "speech_prob", "speech", "muted"} <= set(level)


def test_full_capture_queue_counts_dropped_chunks(make_engine):
    engine, _, _ = make_engine([])
    engine.audio_queue = queue.Queue(maxsize=1)
    block = np.zeros((CHUNK, 1), dtype=np.float32)
    engine._audio_callback(block, CHUNK, None, None)
    engine._audio_callback(block, CHUNK, None, None)
    assert engine.metrics["dropped_chunks"] == 1


def test_start_requires_consent(monkeypatch, use_store):
    import app.hardware as hardware
    from app.hardware import HardwareMonitor

    monitor = HardwareMonitor.__new__(HardwareMonitor)
    HardwareMonitor.__init__(monitor)
    monitor.devices = [{"index": 0, "name": "Test Mic", "channels": 1, "is_default": True}]
    monkeypatch.setattr(hardware, "_monitor", monitor)
    result = AcousticGuardEngine().start(consent_acknowledged=False)
    assert result["status"] == "consent_required"
    assert use_store.consent_history() == []
