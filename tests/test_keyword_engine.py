import pytest

from app.keyword_engine import KeywordEngine, normalize


@pytest.fixture(scope="module")
def engine():
    return KeywordEngine()


def categories(result):
    return {m["category"] for m in result["matches"]}


def test_normalize_strips_punctuation_and_case():
    assert normalize("Root Password!!") == "root password"
    assert normalize("  multiple   spaces  ") == "multiple spaces"


def test_matches_known_phrase(engine):
    result = engine.scan("can you send me the root password please")
    assert result["score"] > 0.9
    assert "credentials" in categories(result)


def test_no_match_on_benign_text(engine):
    result = engine.scan("the weather is nice today, let's grab lunch")
    assert result["score"] == 0
    assert result["matches"] == []


def test_multiple_categories(engine):
    result = engine.scan("I need the database password and also the bank account number")
    assert {"credentials", "financial"} <= categories(result)


def test_case_and_punctuation_do_not_evade_detection(engine):
    assert engine.scan("ROOT PASSWORD!!! give it now.")["score"] > 0


def test_no_duplicate_matches_for_repeated_phrase(engine):
    result = engine.scan("root password root password root password")
    assert len([m for m in result["matches"] if m["phrase"] == "root password"]) == 1


def test_word_boundaries(engine):
    assert engine.scan("we moved to Bombay last year")["matches"] == []
    assert "safety_threats" in categories(engine.scan("they found a bomb in the car"))


def test_inflections_still_match(engine):
    assert engine.scan("he reset all the api keys")["score"] > 0


def test_per_phrase_weight_override(engine):
    bare = engine.scan("I forgot my password")
    assert 0 < bare["score"] < 0.65
    assert engine.scan("the password is hunter two")["score"] >= 0.95


@pytest.mark.parametrize("text,lang", [
    ("میرا ڈیٹا بیس کا پاس ورڈ یہ ہے", "ur"),
    ("yaar password bata do jaldi", "roman_ur"),
    ("रूट पासवर्ड क्या है", "hi"),
    ("كلمة المرور هي", "ar"),
    ("رمز عبور سرور", "fa"),
])
def test_multilingual_variants(engine, text, lang):
    result = engine.scan(text)
    assert result["score"] > 0
    assert any(m["lang"] == lang for m in result["matches"])


def test_security_incident_category(engine):
    result = engine.scan("My system is compromised, there is a ransomware attack")
    assert "security_incident" in categories(result)


def test_fuzzy_catches_transcription_misspelling(engine):
    # Whisper writes password as پاس وڑٹ here - exact matching misses it, phonetic matching does not
    result = engine.scan("یار سرور کا پاس وڑٹ بتا دو")
    fuzzy = [m for m in result["matches"] if m["match"] == "fuzzy"]
    assert fuzzy and result["score"] >= 0.8


def test_fuzzy_does_not_fire_on_ordinary_speech(engine):
    for text in [
        "please send the meeting notes to the whole team",
        "آج موسم بہت اچھا ہے چلو شام کو چائے پیتے ہیں",
        "कल हम सब मिलकर क्रिकेट मैच देखने चलेंगे",
        "الطقس جميل اليوم هل تريد أن نشرب القهوة معا",
    ]:
        assert engine.scan(text)["matches"] == [], text


def test_summary_lists_languages(engine):
    summary = engine.summary()
    assert summary["phrases"] > 250
    assert {"en", "ur", "roman_ur", "hi", "ar", "fa"} <= set(summary["languages"])
