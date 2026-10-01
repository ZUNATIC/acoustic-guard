"""End-to-end: synthetic speech in five languages through the real models."""
import pytest

from app.threat_engine import severity_rank
from tests.conftest import FIXTURES, load_wav, speech_manifest

pytestmark = [pytest.mark.speech, pytest.mark.models]

CASES = speech_manifest()


@pytest.fixture(scope="module")
def pipeline():
    from app.pipeline import Pipeline

    return Pipeline()


def _meets(severity: str, expect: str) -> bool:
    if expect == "NONE":
        return severity == "NONE"
    return severity_rank(severity) >= severity_rank(expect.rstrip("+"))


@pytest.mark.parametrize("case", CASES, ids=[c["file"] for c in CASES])
def test_clip(pipeline, case):
    audio = load_wav(FIXTURES / case["file"])
    event, result, tr = pipeline.process(audio, source="test", store=False)
    severity = result.severity if result else "NONE"

    if case["expect"] == "NONE":
        assert severity == "NONE", (tr.text, tr.translation, result.matches if result else None)
        return

    assert tr.language, "no language detected"
    if _meets(severity, case["expect"]):
        return
    # not caught by the fast model alone: the clip must at least be routed to the second-opinion pass
    assert result is not None and result.threat_score >= pipeline.settings.verify_min_score, (tr.text, tr.translation)


def test_language_identification(pipeline):
    wrong = []
    for case in CASES:
        tr = pipeline.transcriber.transcribe(load_wav(FIXTURES / case["file"]), translate=False)
        expected = "ur" if case["language"] == "hi" else case["language"]
        if tr.language != expected:
            wrong.append((case["file"], tr.language))
    assert len(wrong) <= 1, wrong


def test_latency_budget(pipeline):
    for name in ("en_leak.wav", "ur_leak.wav", "ar_leak.wav"):
        tr = pipeline.transcriber.transcribe(load_wav(FIXTURES / name))
        assert tr.elapsed < 5.0, (name, tr.elapsed)


@pytest.mark.verifier
@pytest.mark.parametrize("case", [c for c in CASES if c["expect"] != "NONE"], ids=lambda c: c["file"])
def test_clip_with_second_opinion(case):
    from app.pipeline import Pipeline

    p = Pipeline()
    audio = load_wav(FIXTURES / case["file"])
    event, result, tr = p.process(audio, source="test", store=False)
    severity = result.severity if result else "NONE"
    if p.needs_verification(result):
        verified = p.verify(audio, result, tr, source="test")
        if verified:
            severity = max(severity, verified["severity"], key=severity_rank)
    assert _meets(severity, case["expect"])
