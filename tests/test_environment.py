import numpy as np
import pytest

from app.environment import EnvironmentMonitor

rng = np.random.default_rng(0)


def noise(level):
    return (rng.standard_normal(512) * level).astype(np.float32)


@pytest.fixture
def env():
    e = EnvironmentMonitor()
    e.cooldown = 0
    return e


def settle(env, level=0.003, n=200, t0=0.0):
    for i in range(n):
        env.update(noise(level), is_speech=False, now=t0 + i * 0.032)
    return t0 + n * 0.032


def test_baseline_tracks_quiet_room(env):
    settle(env)
    assert -55 < env.baseline_db < -45


def test_sudden_sound_detected(env):
    t = settle(env)
    events = [env.update(noise(0.3), is_speech=False, now=t)]
    events += [env.update(noise(0.003), is_speech=False, now=t + (i + 1) * 0.032) for i in range(10)]
    hits = [e for e in events if e]
    assert len(hits) == 1 and hits[0]["kind"] == "sudden_sound"
    assert hits[0]["delta_db"] >= 18


def test_speech_onset_is_not_a_sudden_sound(env):
    # the first loud syllable often arrives a chunk or two before the VAD says "speech"
    t = settle(env)
    events = [env.update(noise(0.3), is_speech=False, now=t)]
    events += [env.update(noise(0.05), is_speech=True, now=t + (i + 1) * 0.032) for i in range(20)]
    assert [e for e in events if e] == []


def test_speech_does_not_raise_baseline(env):
    t = settle(env)
    before = env.baseline_db
    for i in range(100):
        env.update(noise(0.05), is_speech=True, now=t + i * 0.032)
    assert abs(env.baseline_db - before) < 0.5


def _talk(env, level, n, t0):
    return [env.update(noise(level), is_speech=True, now=t0 + i * 0.032) for i in range(n)]


def test_raised_voice(env):
    t = settle(env)
    _talk(env, 0.03, 80, t)
    events = [e for e in _talk(env, 0.3, 5, t + 3) if e]
    assert events and events[0]["kind"] == "raised_voice"
    assert events[0]["over_speech_db"] >= 12


def test_consistently_loud_speaker_is_not_raised_voice(env):
    # a recording or a naturally loud talker: loud, but no louder than their own normal level
    t = settle(env)
    assert [e for e in _talk(env, 0.3, 200, t) if e] == []


def test_muted_microphone_detected(env):
    events = [env.update(np.zeros(512, dtype=np.float32), is_speech=False, now=i * 0.032) for i in range(300)]
    kinds = [e["kind"] for e in events if e]
    assert kinds == ["input_silent"]
    assert env.muted
    env.update(noise(0.01), is_speech=False, now=20.0)
    assert not env.muted


def test_snapshot(env):
    settle(env)
    snap = env.snapshot()
    assert snap["enabled"] and snap["baseline_db"] is not None


def test_digitally_silent_room_does_not_flag_normal_speech(env):
    # virtual / very quiet inputs sit near -90 dB; ordinary speech must not count as shouting
    t = settle(env, level=0.00004)
    event = env.update(noise(0.03), is_speech=True, now=t)
    assert event is None


def test_pauses_inside_speech_do_not_lower_speaking_level(env):
    t = settle(env)
    for i in range(200):
        speaking = i % 3 != 0
        env.update(noise(0.05 if speaking else 0.0005), is_speech=speaking, now=t + i * 0.032, in_segment=True)
    assert env.speech_db > -32
    assert -55 < env.baseline_db < -45
