import os
import socket
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ACOUSTIC_",
        # the test suite sets ACOUSTIC_ENV_FILE to an empty value so a local .env cannot change results
        env_file=None if os.environ.get("ACOUSTIC_ENV_FILE") == "" else BASE_DIR / ".env",
        extra="ignore",
    )

    # service
    host: str = "127.0.0.1"
    port: int = 8001
    api_token: str | None = None
    ssl_certfile: Path | None = None
    ssl_keyfile: Path | None = None
    endpoint_id: str = socket.gethostname()
    log_level: str = "INFO"
    log_json: bool = True

    # capture + VAD
    sample_rate: int = 16000
    vad_chunk_samples: int = 512
    vad_threshold: float = 0.5
    max_speech_seconds: float = 12.0
    silence_hangover_chunks: int = 18
    min_speech_chunks: int = 8
    audio_device: int | str | None = None
    hardware_poll_seconds: float = 10.0
    simulate_no_microphone: bool = False

    # speech recognition
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_beam_size: int = 1
    whisper_cpu_threads: int = 4
    whisper_language: str | None = None
    language_remap: dict[str, str] = {"hi": "ur"}
    translate_non_english: bool = True
    translation_model: str | None = None

    # second-opinion pass: a larger model re-checks any utterance that shows a risk signal
    verifier_model: str | None = "large-v3-turbo"
    verify_min_score: float = 0.2
    verify_max_backlog: int = 3
    # the second-opinion model needs ~1.6 GB on top of the rest; below this much RAM it is skipped (0 = never skip)
    verifier_min_ram_gb: float = 7.0

    # low-latency keyword spotting path
    kws_enabled: bool = True
    kws_model: str = "tiny"
    kws_window_seconds: float = 2.5
    kws_hop_seconds: float = 1.0
    kws_confirm_windows: int = 2

    # preprocessing / environment
    noise_reduction: str = "auto"
    noise_reduction_snr_db: float = 3.0
    noise_profile_seconds: float = 1.5
    noise_prop_decrease: float = 0.85
    environment_analysis: bool = True
    env_spike_db: float = 18.0
    env_baseline_alpha: float = 0.01
    env_event_cooldown_seconds: float = 5.0

    # semantic intent model
    semantic_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    semantic_max_tokens: int = 96

    # storage / privacy
    models_dir: Path = BASE_DIR / "models"
    threats_config_path: Path = BASE_DIR / "config" / "threats.yaml"
    db_path: Path = BASE_DIR / "data" / "acoustic_guard.db"
    store_transcript: bool = True
    retention_days: int = 30
    require_consent: bool = True

    # backend integration (ExfilGuard central server)
    backend_url: str | None = None
    backend_token: str | None = None
    backend_hmac_secret: str | None = None
    backend_timeout_seconds: float = 5.0
    backend_verify_tls: bool = True
    backend_min_severity: str = "HIGH"
    backend_include_transcript: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def load_threat_registry() -> dict:
    settings = get_settings()
    with open(settings.threats_config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)
