import numpy as np

from app.preprocess import NoiseReducer, rms_db

rng = np.random.default_rng(1)


def test_rms_db():
    assert rms_db(np.zeros(512, dtype=np.float32)) == -90.0
    assert abs(rms_db(np.full(512, 0.1, dtype=np.float32)) - (-20.0)) < 0.01


def _reducer(mode):
    nr = NoiseReducer()
    nr.mode = mode
    for _ in range(nr._noise.maxlen):
        nr.observe_noise((rng.standard_normal(512) * 0.01).astype(np.float32))
    return nr


def _speech(level):
    t = np.arange(16000) / 16000
    return (level * np.sin(2 * np.pi * 220 * t) + rng.standard_normal(16000) * 0.01).astype(np.float32)


def test_auto_mode_skips_clean_speech():
    nr = _reducer("auto")
    out, info = nr.apply(_speech(0.3))
    assert info["applied"] is False and info["snr_db"] > 20


def test_auto_mode_gates_very_noisy_speech():
    nr = _reducer("auto")
    out, info = nr.apply(_speech(0.008))
    assert info["applied"] is True and info["snr_db"] < 3
    assert out.shape == (16000,) and out.dtype == np.float32


def test_off_mode_is_passthrough():
    nr = _reducer("off")
    audio = _speech(0.005)
    out, info = nr.apply(audio)
    assert out is audio and not info["applied"]


def test_needs_a_noise_profile_first():
    nr = NoiseReducer()
    nr.mode = "on"
    _, info = nr.apply(_speech(0.01))
    assert info["applied"] is False
