import pytest

from app.patterns import PatternDetector, mask, spoken_digit_runs, _luhn_ok


@pytest.fixture(scope="module")
def detector():
    return PatternDetector()


def kinds(result):
    return {m["pattern"] for m in result["matches"]}


def test_spoken_english_digits_with_repeaters():
    assert spoken_digit_runs("four two one zero one double five six") == ["42101556"]


def test_spoken_urdu_digits():
    assert spoken_digit_runs("چار دو ایک صفر ایک پانچ") == ["421015"]


def test_ambiguous_words_alone_are_not_numbers():
    assert spoken_digit_runs("do you know no one") == []


def test_cnic_english(detector):
    assert "cnic" in kinds(detector.scan("my cnic is four two one zero one five five six seven eight nine one two"))


def test_cnic_urdu(detector):
    text = "میرا شناختی کارڈ نمبر چار دو ایک صفر ایک پانچ پانچ چھ سات آٹھ نو ایک دو ہے"
    assert "cnic" in kinds(detector.scan(text))


def test_cnic_written_with_dashes(detector):
    assert "cnic" in kinds(detector.scan("42101-5567891-2"))


def test_payment_card_requires_luhn(detector):
    assert _luhn_ok("4111111111111111") and not _luhn_ok("4111111111111112")
    assert "payment_card" in kinds(detector.scan("card 4111 1111 1111 1111"))
    assert "payment_card" not in kinds(detector.scan("card 4111 1111 1111 1112 0"))


def test_phone_ip_iban(detector):
    assert "phone" in kinds(detector.scan("call zero three zero zero one two three four five six seven"))
    assert "ipv4" in kinds(detector.scan("the server is at 192.168.10.5"))
    assert "ipv4" in kinds(detector.scan("one nine two dot one six eight dot one dot five"))
    assert "iban" in kinds(detector.scan("iban PK36 SCBL 0000 0011 2345 6702"))


def test_values_are_masked(detector):
    result = detector.scan("card 4111 1111 1111 1111")
    assert "4111111111111111" not in str(result)
    assert mask("4111111111111111") == "411**********111"


def test_ordinary_numbers_ignored(detector):
    assert detector.scan("we have three meetings and two calls today")["matches"] == []
    assert detector.scan("the year 2026 was busy")["matches"] == []
