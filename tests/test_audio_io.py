import io
import wave

import numpy as np
import pytest

from app.audio_io import AudioFormatError, decode_wav


def wav_bytes(audio, rate=16000, channels=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((audio * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def test_resamples_stereo_44k_to_mono_16k():
    t = np.arange(44100) / 44100
    mono = 0.5 * np.sin(2 * np.pi * 440 * t)
    stereo = np.stack([mono, mono], axis=1).reshape(-1)
    out = decode_wav(wav_bytes(stereo, 44100, 2))
    assert out.dtype == np.float32
    assert abs(len(out) - 16000) <= 2
    assert 0.45 < np.max(np.abs(out)) < 0.55


def test_rejects_non_wav():
    with pytest.raises(AudioFormatError):
        decode_wav(b"definitely not audio")


def test_rejects_long_clips():
    with pytest.raises(AudioFormatError):
        decode_wav(wav_bytes(np.zeros(16000 * 61)))
