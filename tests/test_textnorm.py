from app.textnorm import dominant_script, is_rtl, normalize, phonetic_skeleton


def test_latin_case_and_punctuation():
    assert normalize("Root  Password!!") == "root password"


def test_arabic_diacritics_and_letter_forms_unified():
    # Arabic yeh/kaf/heh and harakat collapse onto the Urdu forms
    assert normalize("كِتَابٌ") == normalize("کتاب")
    assert normalize("يه") == normalize("یہ")


def test_eastern_digits_become_ascii():
    assert normalize("۴۲۱٠١") == "42101"
    assert normalize("४२१") == "421"


def test_devanagari_vowel_signs_survive_punctuation_stripping():
    assert normalize("पासवर्ड, बताओ!") == "पासवर्ड बताओ"


def test_urdu_full_stop_removed():
    assert normalize("پاس ورڈ یہ ہے۔") == "پاس ورڈ یہ ہے"


def test_script_detection():
    assert dominant_script("hello there") == "latin"
    assert dominant_script("میرا نام") == "arabic"
    assert dominant_script("मेरा नाम") == "devanagari"
    assert is_rtl("پاس ورڈ") and not is_rtl("password")


def test_phonetic_skeleton_folds_whisper_misspellings():
    assert phonetic_skeleton("پاس ورڈ") == phonetic_skeleton("پاس ورد")
    assert phonetic_skeleton("ٹوکن") == phonetic_skeleton("توکن")
    assert phonetic_skeleton("ڈیٹا بیس") == phonetic_skeleton("دیتا بیس")
