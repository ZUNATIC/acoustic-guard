import pytest

from app.threat_engine import ThreatEngine


class FakeSemanticScorer:
    def __init__(self, fixed_score=0.0):
        self.fixed_score = fixed_score

    def score(self, text: str) -> float:
        return self.fixed_score


@pytest.fixture
def engine():
    return ThreatEngine(semantic_scorer=FakeSemanticScorer(0.0))


def test_empty_transcript_is_none_severity():
    result = ThreatEngine().evaluate("")
    assert result.severity == "NONE" and result.is_violation is False


def test_benign_text_no_violation(engine):
    result = engine.evaluate("let's grab coffee after the meeting")
    assert result.is_violation is False
    assert result.severity == "NONE"


def test_credential_leak_triggers_high_or_critical(engine):
    result = engine.evaluate("this is the top secret database root password")
    assert result.is_violation is True
    assert result.severity == "CRITICAL"
    assert "keyword" in result.sources


def test_semantic_score_influences_fusion():
    result = ThreatEngine(semantic_scorer=FakeSemanticScorer(0.9)).evaluate("nothing sensitive here at all")
    assert result.semantic_score == 0.9
    assert result.threat_score > 0


def test_severity_bands_are_monotonic():
    b = ThreatEngine().bands
    assert b["critical"] > b["high"] > b["medium"] > b["low"]


def test_threat_score_never_exceeds_one():
    result = ThreatEngine(semantic_scorer=FakeSemanticScorer(1.0)).evaluate("root password api key secret key private key")
    assert result.threat_score <= 1.0


def test_no_semantic_scorer_falls_back_to_keyword_only():
    result = ThreatEngine(semantic_scorer=None).evaluate("root password")
    assert result.semantic_score == 0.0 and result.threat_score > 0


def test_strong_keyword_alone_is_not_diluted_by_weak_semantic(engine):
    result = engine.evaluate("root password")
    assert result.threat_score >= 0.9 and result.severity == "CRITICAL"


def test_strong_semantic_alone_can_still_raise_severity():
    result = ThreatEngine(semantic_scorer=FakeSemanticScorer(0.95)).evaluate("nothing on the surface looks sensitive here")
    assert result.keyword_score == 0
    assert result.severity in ("HIGH", "MEDIUM")


def test_translation_is_scored_too(engine):
    # native text the registry does not cover, English rendering that it does
    result = engine.evaluate("xyz abc", translation="give me the root password")
    assert result.severity == "CRITICAL"
    assert "translation" in result.sources


def test_spoken_number_pattern_contributes(engine):
    result = engine.evaluate("my number is four two one zero one five five six seven eight nine one two")
    assert "pattern" in result.sources
    assert result.is_violation


def test_keyword_spotting_matches_are_fused(engine):
    kws = [{"category": "credentials", "phrase": "root password", "weight": 0.95, "match": "kws", "lang": "en"}]
    result = engine.evaluate("some unclear words", extra_matches=kws)
    assert "kws" in result.sources and result.severity == "CRITICAL"


def test_repeated_disclosure_escalates(engine):
    first = engine.evaluate("tell me the account number", track_history=True)
    second = engine.evaluate("what is the account number again", track_history=True)
    third = engine.evaluate("the account number please", track_history=True)
    assert first.severity == "HIGH"
    assert third.severity == "CRITICAL"
    assert any("repeated" in f for f in third.risk_factors)
    assert not first.risk_factors


def test_history_not_tracked_by_default(engine):
    for _ in range(4):
        result = engine.evaluate("tell me the account number")
    assert result.severity == "HIGH"


def test_safety_category_marked_review_only(engine):
    result = engine.evaluate("there is a bomb threat in the building")
    assert result.review_only is True


def test_result_serialises(engine):
    d = engine.evaluate("root password").as_dict()
    assert {"severity", "threat_score", "matches", "sources", "categories"} <= set(d)


def test_second_opinion_skips_critical(monkeypatch):
    from app.pipeline import Pipeline

    p = Pipeline()
    monkeypatch.setattr(p, "verifier", lambda: object())
    critical = ThreatEngine(semantic_scorer=FakeSemanticScorer(0.0)).evaluate("root password")
    low = ThreatEngine(semantic_scorer=FakeSemanticScorer(0.4)).evaluate("something odd")
    assert p.needs_verification(critical) is False
    assert p.needs_verification(low) is True


def test_second_opinion_skipped_on_low_memory(monkeypatch):
    import app.pipeline as pipeline_module
    from app.pipeline import Pipeline

    p = Pipeline()
    monkeypatch.setattr(p.settings, "verifier_model", "large-v3-turbo")
    monkeypatch.setattr(pipeline_module, "total_ram_gb", lambda: 4.0)
    assert p.verifier() is None
