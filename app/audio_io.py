import io
import wave
from math import gcd

import numpy as np
from scipy.signal import resample_poly

MAX_SECONDS = 60


class AudioFormatError(ValueError):
    pass


def decode_wav(data: bytes, target_rate: int = 16000) -> np.ndarray:
    """Decode a WAV byte string entirely in memory to mono float32 at `target_rate`."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if frames / max(rate, 1) > MAX_SECONDS:
                raise AudioFormatError(f"clip longer than {MAX_SECONDS}s")
            raw = w.readframes(frames)
    except AudioFormatError:
        raise
    except (wave.Error, EOFError, ValueError) as exc:
        raise AudioFormatError(f"not a PCM WAV file ({exc})") from exc

    if width == 2:
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        audio = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        audio = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise AudioFormatError(f"unsupported sample width {width * 8} bit")

    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != target_rate:
        g = gcd(rate, target_rate)
        audio = resample_poly(audio, target_rate // g, rate // g).astype(np.float32)
    return np.clip(audio, -1.0, 1.0).astype(np.float32)
