import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from app.config import get_settings
from app.environment import EnvironmentMonitor
from app.forwarder import build_status_event, get_forwarder
from app.hardware import get_hardware_monitor, resolve_device, sd
from app.kws import KeywordSpotter
from app.pipeline import get_pipeline
from app.preprocess import NoiseReducer, peak, rms_db, warm_up as warm_noise_reducer
from app.storage import get_alert_store
from app.vad import get_vad

log = logging.getLogger("acoustic.engine")

TelemetryCallback = Callable[[dict], None]
LEVEL_INTERVAL = 0.2
PREROLL_CHUNKS = 6


@dataclass
class SegmentJob:
    segment_id: int
    audio: np.ndarray
    speech_end: float
    speech_chunks: int


@dataclass
class KwsJob:
    segment_id: int
    window: np.ndarray
    started: float


class AcousticGuardEngine:
    """Live capture -> VAD segmentation -> transcription/threat analysis.

    Threads:
      capture   PortAudio callback, only copies 512-sample blocks into a bounded queue
      vad       Silero VAD, environment analysis, segmentation, schedules work
      asr       full-segment transcription + threat engine (never blocks capture)
      kws       rolling-window keyword spotting while speech is still in progress
      verify    second-opinion pass with a larger model on segments that showed risk
    """

    def __init__(self, pipeline=None):
        s = get_settings()
        self.settings = s
        self.pipeline = pipeline or get_pipeline()
        self.chunk_seconds = s.vad_chunk_samples / s.sample_rate
        self.audio_queue: queue.Queue = queue.Queue(maxsize=int(60 / self.chunk_seconds))
        self.environment = EnvironmentMonitor()
        self.noise = NoiseReducer()
        self.kws = KeywordSpotter() if s.kws_enabled else None
        self.synchronous = False

        self.is_running = False
        self.stream = None
        self.device: dict | None = None
        self.source: str | None = None
        self._remote_tail = np.zeros(0, dtype=np.float32)
        self.operator: str | None = None
        self.started_at: float | None = None
        self.session_language: str | None = None

        self._listeners: list[TelemetryCallback] = []
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._asr_q: queue.Queue = queue.Queue()
        self._kws_q: queue.Queue = queue.Queue(maxsize=1)
        self._verify_q: queue.Queue = queue.Queue(maxsize=max(1, s.verify_max_backlog))
        self._kws_matches: dict[int, list[dict]] = {}
        self._kws_segment = -1
        self._asr_busy = False
        self._vad = None
        self._reset_segment_state()

        self.latest_telemetry = {"status": "idle", "transcript": "", "threat_score": 0.0, "severity": "NONE"}
        self.latest_level: dict = {}
        self.metrics = self._fresh_metrics()

    # ---- helpers ------------------------------------------------------------------------

    @staticmethod
    def _fresh_metrics() -> dict:
        return {
            "segments": 0, "transcribed": 0, "discarded_segments": 0, "alerts": 0,
            "early_warnings": 0, "verifications": 0, "verify_upgrades": 0, "verify_skipped": 0,
            "dropped_chunks": 0, "input_overflows": 0, "environment_events": 0,
            "asr_seconds_total": 0.0, "last_latency_ms": None, "max_latency_ms": None,
        }

    def _reset_segment_state(self) -> None:
        self._buffer: list[np.ndarray] = []
        self._speech_chunks = 0
        self._silence_run = 0
        self._segment_id = getattr(self, "_segment_id", 0)
        self._kws_submitted_at = 0
        self._preroll: deque[np.ndarray] = deque(maxlen=PREROLL_CHUNKS)
        self._last_level_emit = 0.0

    @property
    def vad(self):
        if self._vad is None:
            self._vad = get_vad()
        return self._vad

    @vad.setter
    def vad(self, value):
        self._vad = value

    def on_telemetry(self, callback: TelemetryCallback) -> None:
        self._listeners.append(callback)

    def _emit(self, event: dict) -> None:
        event.setdefault("timestamp", time.time())
        kind = event.get("type", "status")
        if kind == "level":
            self.latest_level = event
        elif kind in ("status", "transcript", "verified"):
            with self._lock:
                snapshot = dict(self.latest_telemetry)
                snapshot.update({
                    "status": event.get("status", snapshot.get("status", "listening")),
                    "timestamp": event["timestamp"],
                })
                if kind != "status":
                    snapshot.update({
                        "transcript": event.get("transcript", ""),
                        "translation": event.get("translation"),
                        "language": event.get("language"),
                        "threat_score": event.get("threat_score", 0.0),
                        "severity": event.get("severity", "NONE"),
                        "sources": event.get("sources", []),
                        "matches": event.get("matches", [])[:8],
                        "transcription": event.get("transcription"),
                    })
                self.latest_telemetry = snapshot
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:
                log.exception("telemetry listener failed")

    def publish(self, event: dict) -> None:
        self._emit(event)

    def status(self) -> dict:
        hw = get_hardware_monitor().status(monitoring=self.is_running)
        return {
            "monitoring_active": self.is_running,
            "state": hw["state"],
            "microphone_available": hw["microphone_available"],
            "reason": hw["reason"],
            "device": self.device or hw["selected_device"],
            "source": self.source,
            "operator": self.operator,
            "started_at": self.started_at,
            "session_language": self.session_language,
            "muted_input": self.environment.muted,
            "queues": {"audio": self.audio_queue.qsize(), "asr": self._asr_q.qsize(), "verify": self._verify_q.qsize()},
            "metrics": dict(self.metrics),
        }

    # ---- lifecycle ----------------------------------------------------------------------

    def _prepare(self) -> None:
        self.vad.reset()
        self.environment.reset()
        self._reset_segment_state()
        self.metrics = self._fresh_metrics()
        self._remote_tail = np.zeros(0, dtype=np.float32)
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()

    def _begin(self, device: dict, operator: str | None, source: str) -> dict:
        if self.settings.require_consent:
            get_alert_store().record_consent(f"stream_start:{source}", operator)
        self.device = device
        self.source = source
        self.operator = operator
        self.started_at = time.time()
        self.is_running = True

        self._threads = [
            threading.Thread(target=self._vad_loop, name="vad", daemon=True),
            threading.Thread(target=self._asr_loop, name="asr", daemon=True),
            threading.Thread(target=self._warm_models, name="model-loader", daemon=True),
        ]
        if self.kws is not None:
            self._threads.append(threading.Thread(target=self._kws_loop, name="kws", daemon=True))
        if self.settings.verifier_model:
            self._threads.append(threading.Thread(target=self._verify_loop, name="verify", daemon=True))
        for t in self._threads:
            t.start()

        log.info("monitoring started", extra={"device": device["name"], "source": source, "operator": operator})
        self._emit({"type": "status", "status": "listening", "device": device["name"], "source": source, "operator": operator})
        get_forwarder().enqueue(build_status_event({"state": "active", "device": device["name"], "source": source}))
        return {"status": "monitoring_started", "device": device["name"]}

    def start(self, consent_acknowledged: bool = False, operator: str | None = None,
              device=None) -> dict:
        """Monitor a microphone attached to this endpoint (PortAudio)."""
        if self.is_running:
            return {"status": "already_running"}

        hardware = get_hardware_monitor()
        if not hardware.available:
            hardware.refresh()
        if not hardware.available:
            return {"status": "disabled_no_hardware", "reason": hardware.error}

        if self.settings.require_consent and not consent_acknowledged:
            return {"status": "consent_required"}

        chosen = resolve_device(hardware.devices, device if device is not None else self.settings.audio_device)
        if chosen is None:
            return {"status": "error", "reason": f"audio device '{device}' not found"}

        self._prepare()
        try:
            self.stream = sd.InputStream(
                samplerate=self.settings.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self.settings.vad_chunk_samples,
                device=chosen["index"],
                callback=self._audio_callback,
            )
            self.stream.start()
        except Exception as exc:
            self.stream = None
            log.error("could not open microphone", extra={"device": chosen["name"], "error": str(exc)})
            return {"status": "error", "reason": f"could not open microphone: {exc}"}

        hardware.streaming = True
        return self._begin(chosen, operator, "endpoint")

    def start_remote(self, consent_acknowledged: bool = False, operator: str | None = None,
                     label: str | None = None, client: str | None = None) -> dict:
        """Monitor audio captured by a browser (getUserMedia) and streamed over the audio
        WebSocket. The browser asks the user for microphone permission; audio still only ever
        exists in memory here."""
        if self.is_running:
            return {"status": "already_running"}
        if self.settings.require_consent and not consent_acknowledged:
            return {"status": "consent_required"}
        self._prepare()
        name = f"{label or 'Browser microphone'}" + (f" @ {client}" if client else "")
        return self._begin({"index": None, "name": name, "remote": True, "client": client}, operator, "browser")

    def feed_remote(self, pcm16: bytes) -> None:
        """16 kHz mono little-endian int16 PCM from the browser, any block size."""
        if not self.is_running or self.source != "browser":
            return
        if len(pcm16) % 2:
            pcm16 = pcm16[:-1]
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        buf = np.concatenate([self._remote_tail, samples]) if self._remote_tail.size else samples
        n = self.settings.vad_chunk_samples
        whole = (len(buf) // n) * n
        for i in range(0, whole, n):
            try:
                self.audio_queue.put_nowait(buf[i:i + n].copy())
            except queue.Full:
                self.metrics["dropped_chunks"] += 1
        self._remote_tail = buf[whole:].copy()

    def stop(self, operator: str | None = None) -> dict:
        was_running = self.is_running
        self.is_running = False
        if was_running and self.settings.require_consent:
            get_alert_store().record_consent("stream_stop", operator or self.operator)
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                log.exception("error closing stream")
            self.stream = None
        if self.source == "endpoint":
            get_hardware_monitor().streaming = False
        for t in self._threads:
            if t.name in ("vad", "asr"):
                t.join(timeout=15)
        self._threads = []
        self._buffer = []
        if was_running:
            get_forwarder().enqueue(build_status_event({"state": "ready"}))
            log.info("monitoring stopped", extra={"source": self.source, "metrics": self.metrics})
        self.source = None
        self.device = None
        self._emit({"type": "status", "status": "idle"})
        return {"status": "monitoring_stopped"}

    def _warm_models(self) -> None:
        try:
            from app.semantic import get_semantic_scorer
            from app.transcriber import get_transcriber

            get_transcriber()
            get_semantic_scorer()
            if self.kws is not None:
                _ = self.kws.model
            if self.settings.noise_reduction != "off":
                warm_noise_reducer()
            self._emit({"type": "status", "status": "listening", "models_ready": True})
        except Exception:
            log.exception("model warm-up failed")

    # ---- capture ------------------------------------------------------------------------

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if status and status.input_overflow:
            self.metrics["input_overflows"] += 1
        try:
            self.audio_queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            self.metrics["dropped_chunks"] += 1

    # ---- VAD / segmentation -------------------------------------------------------------

    def _vad_loop(self) -> None:
        while self.is_running:
            try:
                chunk = self.audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.process_chunk(chunk)
            except Exception:
                log.exception("vad step failed")
        if self._speech_chunks >= self.settings.min_speech_chunks:
            self._flush(time.monotonic())

    def process_chunk(self, chunk: np.ndarray, now: float | None = None) -> None:
        s = self.settings
        now = now or time.monotonic()
        prob = self.vad.process_chunk(chunk)
        is_speech = prob >= s.vad_threshold
        in_segment = bool(self._buffer)

        env_event = self.environment.update(chunk, is_speech, in_segment=in_segment)
        if env_event:
            self._on_environment(env_event)

        if is_speech:
            if not in_segment:
                self._segment_id += 1
                self._buffer = list(self._preroll)
                self._preroll.clear()
                self._speech_chunks = 0
                self._kws_submitted_at = 0
            self._buffer.append(chunk)
            self._speech_chunks += 1
            self._silence_run = 0
        elif in_segment:
            self._buffer.append(chunk)
            self._silence_run += 1
        else:
            self._preroll.append(chunk)
            self.noise.observe_noise(chunk)

        wall = time.time()
        if wall - self._last_level_emit >= LEVEL_INTERVAL:
            self._last_level_emit = wall
            self._emit({
                "type": "level", "db": round(rms_db(chunk), 1), "peak": round(peak(chunk), 3),
                "speech_prob": round(prob, 3), "speech": is_speech,
                "baseline_db": self.environment.snapshot()["baseline_db"],
                "muted": self.environment.muted, "segment_active": bool(self._buffer),
            })

        if not self._buffer:
            return

        if self.kws is not None and is_speech:
            buffered = len(self._buffer) * s.vad_chunk_samples
            if buffered >= self.kws.window_samples and buffered - self._kws_submitted_at >= self.kws.hop_samples:
                self._kws_submitted_at = buffered
                window = np.concatenate(self._buffer)[-self.kws.window_samples:]
                self._submit_kws(KwsJob(self._segment_id, window, now))

        max_chunks = int(s.max_speech_seconds / self.chunk_seconds)
        if self._silence_run >= s.silence_hangover_chunks:
            if self._speech_chunks >= s.min_speech_chunks:
                self._flush(now - self._silence_run * self.chunk_seconds)
            else:
                for c in self._buffer:
                    self.noise.observe_noise(c)
                self._buffer = []
                self._speech_chunks = 0
                self.metrics["discarded_segments"] += 1
        elif len(self._buffer) >= max_chunks:
            self._flush(now)

    def _flush(self, speech_end: float) -> None:
        keep = max(0, len(self._buffer) - max(0, self._silence_run - 6))
        audio = np.concatenate(self._buffer[:keep] or self._buffer).astype(np.float32)
        job = SegmentJob(self._segment_id, audio, speech_end, self._speech_chunks)
        self._buffer = []
        self._speech_chunks = 0
        self._silence_run = 0
        self.metrics["segments"] += 1
        self._emit({"type": "status", "status": "analysing", "segment_id": job.segment_id,
                    "audio_seconds": round(len(audio) / self.settings.sample_rate, 2)})
        if self.synchronous:
            self._handle_segment(job)
        else:
            self._asr_q.put(job)

    def _submit_kws(self, job: KwsJob) -> None:
        if self.synchronous:
            self._handle_kws(job)
            return
        try:
            self._kws_q.get_nowait()
        except queue.Empty:
            pass
        try:
            self._kws_q.put_nowait(job)
        except queue.Full:
            pass

    def _on_environment(self, event: dict) -> None:
        self.metrics["environment_events"] += 1
        if event["kind"] != "input_silent":
            try:
                get_alert_store().insert_environment_event(event["kind"], event["level_db"], event["baseline_db"])
            except Exception:
                log.exception("could not store environment event")
        self._emit({"type": "environment", **event})

    # ---- analysis workers ---------------------------------------------------------------

    def _asr_loop(self) -> None:
        while self.is_running or not self._asr_q.empty():
            try:
                job = self._asr_q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._asr_busy = True
            try:
                self._handle_segment(job)
            except Exception:
                log.exception("segment analysis failed")
            finally:
                self._asr_busy = False

    def _handle_segment(self, job: SegmentJob) -> None:
        audio, prep = self.noise.apply(job.audio)
        extra = self._kws_matches.pop(job.segment_id, [])
        event, result, tr = self.pipeline.process(
            audio, source="stream", speech_end=job.speech_end, extra_matches=extra,
            preprocessing=prep, track_history=True,
            language=None,
        )
        self.metrics["asr_seconds_total"] = round(self.metrics["asr_seconds_total"] + tr.elapsed, 2)
        if event is None:
            self._emit({"type": "status", "status": "listening", "segment_id": job.segment_id, "no_speech": True})
            return
        self.metrics["transcribed"] += 1
        if tr.language and tr.language_probability >= 0.6:
            self.session_language = tr.language
        latency = event.get("latency_ms")
        if latency is not None:
            self.metrics["last_latency_ms"] = latency
            self.metrics["max_latency_ms"] = max(latency, self.metrics["max_latency_ms"] or 0)
        if event.get("alert_id"):
            self.metrics["alerts"] += 1
        event["segment_id"] = job.segment_id
        event["status"] = "listening"
        self._emit(event)

        if self.pipeline.needs_verification(result):
            item = (job.segment_id, audio, result, tr, event.get("alert_id"))
            if self.synchronous:
                self._handle_verify(item)
            else:
                try:
                    self._verify_q.put_nowait(item)
                except queue.Full:
                    self.metrics["verify_skipped"] += 1

    def _kws_loop(self) -> None:
        while self.is_running:
            try:
                job = self._kws_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._handle_kws(job)
            except Exception:
                log.exception("keyword spotting failed")

    def _handle_kws(self, job: KwsJob) -> None:
        if job.segment_id != self._kws_segment:
            self.kws.reset()
            self._kws_segment = job.segment_id
        text, confirmed = self.kws.process(job.window)
        if not confirmed:
            return
        self._kws_matches.setdefault(job.segment_id, []).extend(confirmed)
        self.metrics["early_warnings"] += 1
        top = max(confirmed, key=lambda m: m["weight"])
        log.info("early warning", extra={"segment": job.segment_id, "phrases": [m["phrase"] for m in confirmed]})
        self._emit({
            "type": "early_warning",
            "segment_id": job.segment_id,
            "matches": confirmed,
            "category": top["category"],
            "partial_transcript": text,
            "estimated_severity": self.pipeline.threat_engine._severity(top["weight"]),
            "ms_since_window": int((time.monotonic() - job.started) * 1000),
        })

    def _live_work_pending(self) -> bool:
        return bool(self._buffer) or not self._asr_q.empty() or self._asr_busy

    def _verify_loop(self) -> None:
        # the second opinion must never compete with live analysis: drop this thread (and the
        # inference threads it creates) to the lowest scheduling priority before loading the model
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
        except (AttributeError, OSError):
            pass
        try:
            self.pipeline.verifier()
        except Exception:
            log.exception("could not load the second-opinion model")
        while self.is_running or not self._verify_q.empty():
            try:
                item = self._verify_q.get(timeout=0.5)
            except queue.Empty:
                continue
            waited = 0.0
            while self.is_running and self._live_work_pending() and waited < 20.0:
                time.sleep(0.2)
                waited += 0.2
            try:
                self._handle_verify(item)
            except Exception:
                log.exception("verification failed")

    def _handle_verify(self, item) -> None:
        segment_id, audio, result, tr, alert_id = item
        event = self.pipeline.verify(audio, result, tr, source="stream", first_alert_id=alert_id)
        if event is None:
            return
        self.metrics["verifications"] += 1
        if event["upgraded"]:
            self.metrics["verify_upgrades"] += 1
            if event.get("alert_id"):
                self.metrics["alerts"] += 1
        event["segment_id"] = segment_id
        self._emit(event)


_engine: AcousticGuardEngine | None = None


def get_engine() -> AcousticGuardEngine:
    global _engine
    if _engine is None:
        _engine = AcousticGuardEngine()
    return _engine


def reset_engine() -> None:
    global _engine
    if _engine is not None and _engine.is_running:
        _engine.stop()
    _engine = None
