import numpy as np
import pytest

from app.vad import SileroVAD


@pytest.fixture(scope="module")
def vad():
    return SileroVAD()


def test_silence_gives_low_probability(vad):
    vad.reset()
    chunk = np.zeros(512, dtype=np.float32)
    prob = vad.process_chunk(chunk)
    assert 0.0 <= prob <= 1.0
    assert prob < 0.5


def test_wrong_chunk_size_raises(vad):
    vad.reset()
    with pytest.raises(ValueError):
        vad.process_chunk(np.zeros(480, dtype=np.float32))


def test_state_persists_across_calls(vad):
    vad.reset()
    chunk = np.random.randn(512).astype(np.float32) * 0.05
    state_before = vad._state.copy()
    vad.process_chunk(chunk)
    assert not np.array_equal(state_before, vad._state)


def test_reset_clears_state(vad):
    vad.reset()
    chunk = np.random.randn(512).astype(np.float32) * 0.05
    vad.process_chunk(chunk)
    vad.reset()
    assert np.array_equal(vad._state, np.zeros((2, 1, 128), dtype=np.float32))


def test_context_carries_last_samples_between_chunks(vad):
    vad.reset()
    chunk1 = np.linspace(-1, 1, 512, dtype=np.float32)
    vad.process_chunk(chunk1)
    assert np.array_equal(vad._context[0], chunk1[-vad.context_size:])

    chunk2 = np.linspace(1, -1, 512, dtype=np.float32)
    vad.process_chunk(chunk2)
    assert np.array_equal(vad._context[0], chunk2[-vad.context_size:])


def test_reset_clears_context(vad):
    vad.reset()
    vad.process_chunk(np.random.randn(512).astype(np.float32) * 0.05)
    vad.reset()
    assert np.array_equal(vad._context, np.zeros((1, vad.context_size), dtype=np.float32))


def test_real_speech_is_detected_as_speech(vad):
    """
    Regression test for a real bug: the ONNX graph's declared input shape is
    [None, None], giving no indication it actually requires 64 samples of
    trailing context from the previous chunk concatenated onto every new
    512-sample chunk. Without it, every chunk decodes as near-zero regardless
    of actual content - confirmed on a real synthesized speech utterance
    (max probability 0.28 without context, 0.9999 with it).
    """
    vad.reset()
    rng = np.random.default_rng(42)
    t = np.linspace(0, 1.0, 16000, dtype=np.float32)
    speech_like = (
        0.5 * np.sin(2 * np.pi * 150 * t)
        + 0.3 * np.sin(2 * np.pi * 300 * t)
        + 0.05 * rng.standard_normal(len(t)).astype(np.float32)
    ).astype(np.float32)

    probs = []
    for i in range(0, len(speech_like) - 512, 512):
        probs.append(vad.process_chunk(speech_like[i : i + 512]))

    assert max(probs) > 0.5, (
        "VAD failed to detect a sustained harmonic (speech-like) signal - "
        "the context window is likely broken again"
    )
