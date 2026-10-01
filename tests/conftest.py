import json
import os
import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest

_TMP = Path(tempfile.mkdtemp(prefix="acoustic-tests-"))
os.environ["ACOUSTIC_ENV_FILE"] = ""
os.environ["ACOUSTIC_DB_PATH"] = str(_TMP / "test.db")
os.environ["ACOUSTIC_LOG_LEVEL"] = "WARNING"
os.environ["ACOUSTIC_HARDWARE_POLL_SECONDS"] = "0"
os.environ.pop("ACOUSTIC_BACKEND_URL", None)
os.environ.pop("ACOUSTIC_API_TOKEN", None)
if os.environ.get("ACOUSTIC_TEST_VERIFIER") != "1":
    os.environ["ACOUSTIC_VERIFIER_MODEL"] = ""

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "speech"


def models_present() -> bool:
    from app.config import get_settings

    m = get_settings().models_dir
    return (m / "vad" / "silero_vad.onnx").exists() and (m / "whisper" / get_settings().whisper_model / "model.bin").exists()


def pytest_collection_modifyitems(config, items):
    skip_models = pytest.mark.skip(reason="models not downloaded - run scripts/fetch_models.py")
    have = models_present()
    for item in items:
        if ("models" in item.keywords or "speech" in item.keywords) and not have:
            item.add_marker(skip_models)
        if "verifier" in item.keywords and os.environ.get("ACOUSTIC_TEST_VERIFIER") != "1":
            item.add_marker(pytest.mark.skip(reason="set ACOUSTIC_TEST_VERIFIER=1 to run the second-opinion model"))


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0


def speech_manifest() -> list[dict]:
    path = FIXTURES / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


@pytest.fixture
def tmp_store(tmp_path):
    from app.storage import AlertStore

    return AlertStore(db_path=tmp_path / "alerts.db")


@pytest.fixture
def use_store(monkeypatch, tmp_store):
    import app.storage as storage

    monkeypatch.setattr(storage, "_store", tmp_store)
    return tmp_store
