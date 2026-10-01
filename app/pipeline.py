import logging
import os
import threading
import time

import numpy as np

from app.config import get_settings
from app.forwarder import build_alert_event, get_forwarder
from app.semantic import get_semantic_scorer
from app.storage import get_alert_store
from app.threat_engine import ThreatResult, get_threat_engine, severity_rank
from app.transcriber import Transcriber, Transcription, get_speech_model, get_transcriber

log = logging.getLogger("acoustic.pipeline")


def total_ram_gb() -> float | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (AttributeError, ValueError, OSError):
        pass
    try:  # Windows
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong),
                        ("avail_page", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong),
                        ("avail_virtual", ctypes.c_ulonglong), ("avail_extended", ctypes.c_ulonglong)]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.total / 1024 ** 3
    except Exception:
        pass
    return None


class Pipeline:
    """speech segment -> transcript (+ English translation) -> threat verdict -> audit/backend."""

    def __init__(self):
        self.settings = get_settings()
        self._verifier: Transcriber | None = None
        self._verifier_failed = False
        self._lock = threading.Lock()

    @property
    def transcriber(self) -> Transcriber:
        return get_transcriber()

    @property
    def threat_engine(self):
        engine = get_threat_engine()
        if engine.semantic_scorer is None:
            engine.set_semantic_scorer(get_semantic_scorer())
        return engine

    def verifier(self) -> Transcriber | None:
        name = self.settings.verifier_model
        if not name or name == self.settings.whisper_model or self._verifier_failed:
            return None
        ram = total_ram_gb()
        if ram is not None and ram < self.settings.verifier_min_ram_gb:
            log.warning("not enough memory for the second-opinion model, running without it",
                        extra={"model": name, "ram_gb": round(ram, 1), "needed_gb": self.settings.verifier_min_ram_gb})
            self._verifier_failed = True
            return None
        with self._lock:
            if self._verifier is None:
                try:
                    self._verifier = Transcriber(primary=get_speech_model(name, cpu_threads=2))
                except FileNotFoundError:
                    log.info("verifier model not installed, second-opinion pass disabled", extra={"model": name})
                    self._verifier_failed = True
                    return None
        return self._verifier

    def verifier_ready(self) -> bool:
        return self._verifier is not None

    def evaluate(self, tr: Transcription, extra_matches: list[dict] | None = None,
                 track_history: bool = False) -> ThreatResult:
        return self.threat_engine.evaluate(
            tr.text, translation=tr.translation, language=tr.language,
            extra_matches=extra_matches, track_history=track_history, duration=tr.duration,
        )

    def build_event(self, tr: Transcription, result: ThreatResult, source: str, latency_ms: int | None,
                    preprocessing: dict | None = None) -> dict:
        return {
            "type": "transcript",
            "source": source,
            "timestamp": time.time(),
            "transcription": tr.as_dict(),
            **result.as_dict(),
            "latency_ms": latency_ms,
            "preprocessing": preprocessing,
            "alert_id": None,
        }

    def persist(self, event: dict, result: ThreatResult, tr: Transcription, source: str) -> int | None:
        if not result.is_violation:
            return None
        store = get_alert_store()
        alert_id = store.insert_alert(
            transcript=result.transcript,
            translation=result.translation,
            language=result.language,
            language_probability=tr.language_probability,
            threat_score=result.threat_score,
            severity=result.severity,
            keyword_score=result.keyword_score,
            semantic_score=result.semantic_score,
            pattern_score=result.pattern_score,
            matches=result.matches,
            sources=result.sources,
            categories=result.categories,
            risk_factors=result.risk_factors,
            review_only=result.review_only,
            store_transcript=self.settings.store_transcript,
            source=source,
            latency_ms=event.get("latency_ms"),
        )
        get_forwarder().enqueue(build_alert_event(alert_id, result, tr.as_dict(), event.get("latency_ms")))
        event["alert_id"] = alert_id
        return alert_id

    def process(self, audio: np.ndarray, source: str = "stream", speech_end: float | None = None,
                extra_matches: list[dict] | None = None, preprocessing: dict | None = None,
                track_history: bool = False, store: bool = True,
                language: str | None = None) -> tuple[dict | None, ThreatResult | None, Transcription]:
        tr = self.transcriber.transcribe(audio, language=language, translate_if=self._translation_can_help)
        if not tr.text:
            return None, None, tr
        result = self.evaluate(tr, extra_matches=extra_matches, track_history=track_history)
        latency_ms = int((time.monotonic() - speech_end) * 1000) if speech_end else None
        event = self.build_event(tr, result, source, latency_ms, preprocessing)
        if store:
            self.persist(event, result, tr, source)
        log.info(
            "segment analysed",
            extra={
                "source": source, "language": tr.language, "severity": result.severity,
                "score": result.threat_score, "asr_s": round(tr.elapsed, 2), "latency_ms": latency_ms,
                **({"transcript": tr.text} if self.settings.store_transcript else {}),
            },
        )
        return event, result, tr

    def _translation_can_help(self, native_text: str) -> bool:
        # an English rendering can only raise the verdict; skip it when the native text is
        # already CRITICAL so the alert goes out one decode pass sooner
        return self.threat_engine.evaluate(native_text).severity != "CRITICAL"

    def needs_verification(self, result: ThreatResult | None) -> bool:
        if result is None or result.severity == "CRITICAL" or self.verifier() is None:
            return False
        return result.threat_score >= self.settings.verify_min_score or bool(result.matches)

    def verify(self, audio: np.ndarray, first: ThreatResult, first_tr: Transcription,
               source: str = "stream", first_alert_id: int | None = None) -> dict | None:
        verifier = self.verifier()
        if verifier is None:
            return None
        tr = verifier.transcribe(audio, translate=False, language=first_tr.language)
        if not tr.text:
            return None
        tr.translation = first_tr.translation
        result = self.threat_engine.evaluate(
            tr.text, translation=tr.translation, language=tr.language,
            extra_matches=[m for m in first.matches if m.get("match") == "kws"], duration=tr.duration,
        )
        upgraded = severity_rank(result.severity) > severity_rank(first.severity)
        event = {
            "type": "verified",
            "source": source,
            "timestamp": time.time(),
            "model": tr.model,
            "transcription": tr.as_dict(),
            **result.as_dict(),
            "previous_severity": first.severity,
            "upgraded": upgraded,
            "confirms_alert_id": first_alert_id,
            "alert_id": None,
        }
        if upgraded and result.is_violation:
            result.risk_factors.append(f"raised by {tr.model} second-opinion pass")
            self.persist(event, result, tr, f"{source}:verified")
        return event


_pipeline: Pipeline | None = None


def get_pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline()
    return _pipeline


def reset_pipeline() -> None:
    global _pipeline
    _pipeline = None
