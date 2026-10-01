import pytest

pytestmark = pytest.mark.models


@pytest.fixture(scope="module")
def scorer():
    from app.semantic import SemanticScorer

    return SemanticScorer()


def test_empty_text_scores_zero(scorer):
    assert scorer.score("") == 0.0
    assert scorer.score("   ") == 0.0


def test_score_is_bounded(scorer):
    for text in ["hello", "this is a very sensitive exfiltration attempt", "a" * 500]:
        assert 0.0 <= scorer.score(text) <= 1.0


LEAKS = [
    "I'm going to quietly copy the whole client list onto my pen drive before I resign",
    "کمپنی چھوڑنے سے پہلے میں یہ ساری فائلیں اپنے ذاتی ای میل پر بھیج دوں گا",
    "resign karne se pehle saara data apne email pe bhej do",
    "मैं यह गोपनीय फ़ाइल अपने निजी ईमेल पर भेज दूँगा",
]
BENIGN = [
    "let's grab lunch after the meeting",
    "please send the meeting notes to the whole team",
    "آج موسم بہت اچھا ہے، چائے پیتے ہیں",
    "कल हम सब मिलकर क्रिकेट मैच देखेंगे",
]


def test_leak_intent_beats_benign_in_every_language(scorer):
    worst_leak = min(scorer.score(t) for t in LEAKS)
    best_benign = max(scorer.score(t) for t in BENIGN)
    assert worst_leak > best_benign


def test_routine_sharing_is_not_flagged(scorer):
    assert scorer.score("I'll copy the slides to the shared drive for the presentation") < 0.3


def test_explain_reports_closest_reference(scorer):
    info = scorer.explain(LEAKS[0])
    assert info["closest_reference"] and info["margin"] > 0


def test_threat_engine_wires_semantic_scorer_end_to_end(scorer):
    from app.threat_engine import ThreatEngine

    engine = ThreatEngine(semantic_scorer=scorer)
    benign = engine.evaluate("let's grab lunch after the meeting")
    leak = engine.evaluate("save this to a usb drive quietly, don't tell IT")
    assert leak.threat_score > benign.threat_score
    assert benign.severity == "NONE"
