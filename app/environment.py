import time
from collections import deque

import numpy as np

from app.config import get_settings
from app.preprocess import rms_db

MUTED_DB = -80.0
SUDDEN_MIN_DB = -30.0
SUDDEN_CONFIRM_CHUNKS = 8
RAISED_VOICE_OVER_SPEECH_DB = 12.0
SPEECH_LEVEL_ALPHA = 0.02
SPEECH_WARMUP_CHUNKS = 60
SHORT_WINDOW_CHUNKS = 10
MUTED_SECONDS = 8.0
SHIFT_DB = 10.0
SHIFT_REFERENCE_SECONDS = 60.0


def spectral_centroid(chunk: np.ndarray, sample_rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(chunk * np.hanning(len(chunk))))
    total = spectrum.sum()
    if total <= 1e-9:
        return 0.0
    freqs = np.fft.rfftfreq(len(chunk), 1.0 / sample_rate)
    return float((freqs * spectrum).sum() / total)


class EnvironmentMonitor:
    """Environment Signature Analysis. Tracks the ambient noise floor (EMA over non-speech
    audio only, so talking does not raise it) and raises events for:

    - sudden_sound   a non-speech burst well above the floor and loud in absolute terms
                     (door slam, drop, knock)
    - raised_voice   speech well above how loudly people normally talk in this room
                     (shouting / speaking right into the mic)
    - ambient_shift  the floor itself moved by >10 dB versus the last minute (room change,
                     machinery switched on, window opened)
    - input_silent   the input has been digitally silent for several seconds - almost always
                     a muted or disconnected microphone
    """

    def __init__(self):
        s = get_settings()
        self.enabled = s.environment_analysis
        self.sample_rate = s.sample_rate
        self.alpha = s.env_baseline_alpha
        self.spike_db = s.env_spike_db
        self.cooldown = s.env_event_cooldown_seconds
        self.chunk_seconds = s.vad_chunk_samples / s.sample_rate
        self.reset()

    def reset(self) -> None:
        self.baseline_db: float | None = None
        self.level_db = -90.0
        self.centroid_hz = 0.0
        self._last_event: dict[str, float] = {}
        self._silent_run = 0.0
        self._muted = False
        self._history: deque[tuple[float, float]] = deque(maxlen=int(SHIFT_REFERENCE_SECONDS * 2))
        self._last_history = 0.0
        self._pending_spike: dict | None = None
        self._pending_left = 0
        self.speech_db: float | None = None
        self._speech_chunks = 0
        self._recent_speech: deque[float] = deque(maxlen=SHORT_WINDOW_CHUNKS)
        self.recent: deque[dict] = deque(maxlen=20)

    def _ready(self, kind: str, now: float) -> bool:
        if now - self._last_event.get(kind, 0.0) < self.cooldown:
            return False
        self._last_event[kind] = now
        return True

    def _snapshot_event(self, kind: str, now: float, **extra) -> dict:
        return {
            "kind": kind,
            "level_db": round(self.level_db, 1),
            "baseline_db": round(self.baseline_db if self.baseline_db is not None else self.level_db, 1),
            "delta_db": round(self.level_db - (self.baseline_db or self.level_db), 1),
            "centroid_hz": round(self.centroid_hz),
            "timestamp": now,
            **extra,
        }

    def _event(self, kind: str, now: float, **extra) -> dict:
        event = self._snapshot_event(kind, now, **extra)
        self.recent.appendleft(event)
        return event

    def update(self, chunk: np.ndarray, is_speech: bool, now: float | None = None,
               in_segment: bool = False) -> dict | None:
        """is_speech: the VAD's verdict for this chunk. in_segment: the chunk falls inside an
        utterance (e.g. a pause between words) - it must not move the noise floor, and it is not
        speech either."""
        if not self.enabled:
            return None
        now = now or time.time()
        self.level_db = rms_db(chunk)

        if self.level_db <= MUTED_DB:
            self._silent_run += self.chunk_seconds
            if self._silent_run >= MUTED_SECONDS and not self._muted:
                self._muted = True
                return self._event("input_silent", now)
            return None
        self._silent_run = 0.0
        if self._muted:
            self._muted = False
            self._last_event.pop("input_silent", None)

        if self.baseline_db is None:
            self.baseline_db = self.level_db
            return None

        delta = self.level_db - self.baseline_db
        event = None

        # a loud non-speech burst is only reported if speech does not follow straight away;
        # otherwise it was just the first syllable arriving before the VAD caught up
        if self._pending_spike is not None:
            if is_speech or in_segment:
                self._pending_spike = None
            else:
                self._pending_left -= 1
                if self._pending_left <= 0:
                    spike, self._pending_spike = self._pending_spike, None
                    if self._ready("sudden_sound", now):
                        self.recent.appendleft(spike)
                        event = spike

        if in_segment and not is_speech:
            pass
        elif not is_speech:
            if delta >= self.spike_db and self.level_db >= SUDDEN_MIN_DB:
                if self._pending_spike is None and now - self._last_event.get("sudden_sound", 0.0) >= self.cooldown:
                    self.centroid_hz = spectral_centroid(chunk, self.sample_rate)
                    self._pending_spike = self._snapshot_event("sudden_sound", now)
                    self._pending_left = SUDDEN_CONFIRM_CHUNKS
            else:
                self.baseline_db += self.alpha * (self.level_db - self.baseline_db)
        else:
            self._speech_chunks += 1
            if self.speech_db is None:
                self.speech_db = self.level_db
            # compare ~0.3 s of speech (not single 32 ms chunks, which swing a lot between
            # syllables) with this room's long-term speaking level
            self._recent_speech.append(10 ** (self.level_db / 10))
            short_db = 10 * np.log10(sum(self._recent_speech) / len(self._recent_speech))
            louder = short_db - self.speech_db
            if (self._speech_chunks > SPEECH_WARMUP_CHUNKS and len(self._recent_speech) == SHORT_WINDOW_CHUNKS
                    and louder >= RAISED_VOICE_OVER_SPEECH_DB
                    and delta >= self.spike_db and self._ready("raised_voice", now)):
                self.centroid_hz = spectral_centroid(chunk, self.sample_rate)
                event = self._event("raised_voice", now, over_speech_db=round(louder, 1))
            self.speech_db += SPEECH_LEVEL_ALPHA * (self.level_db - self.speech_db)

        if now - self._last_history >= 0.5:
            self._last_history = now
            self._history.append((now, self.baseline_db))
            if len(self._history) == self._history.maxlen and event is None:
                reference = self._history[0][1]
                if abs(self.baseline_db - reference) >= SHIFT_DB and self._ready("ambient_shift", now):
                    event = self._event("ambient_shift", now, previous_baseline_db=round(reference, 1))
                    self._history.clear()
        return event

    @property
    def muted(self) -> bool:
        return self._muted

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "level_db": round(self.level_db, 1),
            "baseline_db": round(self.baseline_db, 1) if self.baseline_db is not None else None,
            "input_silent": self._muted,
            "speech_level_db": round(self.speech_db, 1) if self.speech_db is not None else None,
            "recent_events": list(self.recent)[:10],
        }
