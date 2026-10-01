"""Privacy-by-design checks: no audio ever reaches disk, and minimisation settings hold."""
import os
import time

import numpy as np
import pytest

from tests.conftest import ROOT

AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".opus", ".raw", ".pcm", ".webm", ".m4a"}


def _audio_files(since: float) -> list[str]:
    found = []
    for base in (ROOT / "data", ROOT / "app", ROOT / "static", ROOT / "config"):
        for dirpath, _, files in os.walk(base):
            for f in files:
                p = os.path.join(dirpath, f)
                if os.path.splitext(f)[1].lower() in AUDIO_EXT and os.path.getmtime(p) >= since:
                    found.append(p)
    return found


def test_engine_never_writes_audio(monkeypatch, use_store, tmp_path):
    import app.pipeline as pipeline_module
    from app.audio_stream import AcousticGuardEngine
    from app.pipeline import Pipeline
    from app.transcriber import Transcription

    class T:
        def transcribe(self, audio, translate=None, language=None, translate_if=None):
            return Transcription(text="the root password is", language="en", duration=1.0)

    class V:
        probs = [0.9] * 10 + [0.0] * 20

        def process_chunk(self, c):
            return self.probs.pop(0) if self.probs else 0.0

        def reset(self):
            pass

    monkeypatch.setattr(pipeline_module, "get_transcriber", lambda: T())
    monkeypatch.setattr(pipeline_module, "get_semantic_scorer", lambda: None)
    monkeypatch.chdir(tmp_path)
    start = time.time() - 1
    engine = AcousticGuardEngine(pipeline=Pipeline())
    engine.vad, engine.synchronous, engine.kws, engine.is_running = V(), True, None, True
    for _ in range(30):
        engine.process_chunk(np.random.default_rng().standard_normal(512).astype(np.float32) * 0.1)

    assert use_store.list_alerts()[0] == 1
    assert _audio_files(start) == []
    assert not [f for f in os.listdir(tmp_path) if os.path.splitext(f)[1] in AUDIO_EXT]


def test_database_holds_no_audio_blobs(use_store):
    import sqlite3

    use_store.insert_alert("root password", 0.95, "CRITICAL", 0.95, 0.0, [])
    with sqlite3.connect(use_store.db_path) as conn:
        for table, in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for col in conn.execute(f"PRAGMA table_info({table})"):
                assert col[2].upper() != "BLOB", f"{table}.{col[1]} is a BLOB column"


def test_transcript_minimisation_end_to_end(monkeypatch, use_store):
    from app.config import get_settings
    from app.pipeline import Pipeline
    from app.threat_engine import ThreatEngine
    from app.transcriber import Transcription

    monkeypatch.setattr(get_settings(), "store_transcript", False)
    p = Pipeline()
    tr = Transcription(text="the root password is swordfish", language="en", translation=None)
    result = ThreatEngine().evaluate(tr.text)
    event = p.build_event(tr, result, "test", None)
    p.persist(event, result, tr, "test")
    alert = use_store.list_alerts()[1][0]
    assert alert["transcript"] is None and "swordfish" not in str(alert)
