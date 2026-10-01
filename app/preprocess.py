import threading
from collections import deque

import numpy as np

from app.config import get_settings

SILENCE_DB = -90.0


def rms_db(audio: np.ndarray) -> float:
    if audio.size == 0:
        return SILENCE_DB
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    return max(20.0 * np.log10(rms + 1e-12), SILENCE_DB)


def peak(audio: np.ndarray) -> float:
    return float(np.max(np.abs(audio))) if audio.size else 0.0


_nr_module = None
_nr_lock = threading.Lock()


def _noisereduce():
    global _nr_module
    with _nr_lock:
        if _nr_module is None:
            import noisereduce

            _nr_module = noisereduce
        return _nr_module


def warm_up() -> None:
    _noisereduce()


class NoiseReducer:
    """Keeps a rolling profile of recent non-speech audio and applies stationary spectral
    gating to a speech segment only when its estimated SNR is low.

    Measured on this pipeline: gating improves Whisper at ~0 dB SNR with steady noise (fans,
    hum) but makes it worse at 5-10 dB, so the default "auto" mode gates only noisy segments."""

    def __init__(self):
        s = get_settings()
        self.mode = (s.noise_reduction or "off").lower()
        self.snr_threshold = s.noise_reduction_snr_db
        self.prop_decrease = s.noise_prop_decrease
        self.sample_rate = s.sample_rate
        max_chunks = max(1, int(s.noise_profile_seconds * s.sample_rate / s.vad_chunk_samples))
        self._noise: deque[np.ndarray] = deque(maxlen=max_chunks)
        self._lock = threading.Lock()
        self.applied = 0
        self.skipped = 0

    def observe_noise(self, chunk: np.ndarray) -> None:
        if self.mode == "off":
            return
        with self._lock:
            self._noise.append(chunk)

    def noise_profile(self) -> np.ndarray | None:
        with self._lock:
            if len(self._noise) < max(4, self._noise.maxlen // 3):
                return None
            return np.concatenate(list(self._noise))

    def estimate_snr(self, audio: np.ndarray) -> float | None:
        noise = self.noise_profile()
        if noise is None:
            return None
        signal_power = np.mean(np.square(audio, dtype=np.float64))
        noise_power = np.mean(np.square(noise, dtype=np.float64)) + 1e-12
        speech_power = max(signal_power - noise_power, 1e-12)
        return float(10 * np.log10(speech_power / noise_power))

    def apply(self, audio: np.ndarray) -> tuple[np.ndarray, dict]:
        info = {"mode": self.mode, "applied": False, "snr_db": None}
        if self.mode == "off":
            return audio, info
        snr = self.estimate_snr(audio)
        info["snr_db"] = round(snr, 1) if snr is not None else None
        noise = self.noise_profile()
        if noise is None or (self.mode == "auto" and (snr is None or snr >= self.snr_threshold)):
            self.skipped += 1
            return audio, info
        nr = _noisereduce()
        cleaned = nr.reduce_noise(
            y=audio, sr=self.sample_rate, y_noise=noise,
            stationary=True, prop_decrease=self.prop_decrease,
        ).astype(np.float32)
        self.applied += 1
        info["applied"] = True
        return cleaned, info
