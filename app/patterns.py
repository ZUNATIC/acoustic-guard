import re

from app.config import load_threat_registry
from app.textnorm import normalize

_DIGIT_WORDS = {
    # English
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    # Roman Urdu / Hindi
    "sifar": "0", "sifr": "0", "ek": "1", "do": "2", "teen": "3", "tin": "3", "char": "4",
    "chaar": "4", "panch": "5", "paanch": "5", "chhe": "6", "chay": "6", "che": "6", "chhah": "6",
    "saat": "7", "sat": "7", "aath": "8", "aat": "8", "nau": "9", "no": "9",
    # Urdu script
    "صفر": "0", "ایک": "1", "دو": "2", "تین": "3", "چار": "4", "پانچ": "5",
    "چھ": "6", "چھے": "6", "سات": "7", "آٹھ": "8", "نو": "9",
    # Devanagari
    "शून्य": "0", "एक": "1", "दो": "2", "तीन": "3", "चार": "4", "पांच": "5",
    "छह": "6", "छः": "6", "सात": "7", "आठ": "8", "नौ": "9",
    # Arabic
    "واحد": "1", "اثنان": "2", "ثلاثہ": "3", "اربعہ": "4", "خمسہ": "5",
    "ستہ": "6", "سبعہ": "7", "ثمانیہ": "8", "تسعہ": "9",
}

_REPEATERS = {"double": 2, "triple": 3, "ڈبل": 2, "ٹرپل": 3, "डबल": 2, "ट्रिपल": 3}
_JOINERS = {"dash", "hyphen", "space", "and"}
_DOT_WORDS = {"dot", "point", "ڈاٹ", "डॉट"}

# words that are also common in ordinary speech; they only count as digits inside a run
_AMBIGUOUS = {"do", "no", "oh", "o", "sat", "che", "tin", "دو", "نو", "दो"}

_IPV4 = re.compile(r"\b((?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})\b")
_IBAN = re.compile(r"\b([a-z]{2}\d{2}(?:\s?[a-z0-9]{4}){2,7}(?:\s?[a-z0-9]{1,4})?)\b")

MIN_RUN_DIGITS = 7


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask(value: str) -> str:
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"


def spoken_digit_runs(text: str) -> list[str]:
    tokens = normalize(text).split()
    runs: list[str] = []
    current: list[str] = []
    pending_repeat = 1
    unambiguous_in_run = 0

    def close():
        nonlocal current, unambiguous_in_run
        joined = "".join(current)
        if joined and unambiguous_in_run and len(joined) >= 3:
            runs.append(joined)
        current = []
        unambiguous_in_run = 0

    for tok in tokens:
        if tok.isdigit():
            current.append(tok * pending_repeat if len(tok) == 1 else tok)
            unambiguous_in_run += 1
            pending_repeat = 1
            continue
        if tok in _DIGIT_WORDS:
            current.append(_DIGIT_WORDS[tok] * pending_repeat)
            if tok not in _AMBIGUOUS:
                unambiguous_in_run += 1
            pending_repeat = 1
            continue
        if tok in _REPEATERS:
            pending_repeat = _REPEATERS[tok]
            continue
        if tok in _JOINERS and current:
            continue
        pending_repeat = 1
        close()
    close()
    return runs


def _spoken_ipv4(text: str) -> list[str]:
    tokens = normalize(text).split()
    out, octets, cur = [], [], ""
    for tok in tokens + ["<end>"]:
        if tok.isdigit() or tok in _DIGIT_WORDS:
            cur += tok if tok.isdigit() else _DIGIT_WORDS[tok]
        elif tok in _DOT_WORDS and cur:
            octets.append(cur)
            cur = ""
        else:
            if cur:
                octets.append(cur)
            if len(octets) == 4 and all(o.isdigit() and int(o) <= 255 for o in octets):
                out.append(".".join(octets))
            octets, cur = [], ""
    return out


class PatternDetector:
    def __init__(self, registry: dict | None = None):
        registry = registry or load_threat_registry()
        self.rules = registry.get("sensitive_patterns", {}) or {}

    def _rule(self, kind: str) -> dict | None:
        return self.rules.get(kind)

    def _hit(self, kind: str, value: str) -> dict | None:
        rule = self._rule(kind)
        if rule is None:
            return None
        return {
            "category": "sensitive_pattern",
            "pattern": kind,
            "phrase": f"{rule.get('label', kind)}: {mask(value)}",
            "weight": float(rule.get("weight", 0.5)),
            "lang": "any",
        }

    def classify_number(self, digits: str) -> str | None:
        n = len(digits)
        if n == 13 and self._rule("cnic"):
            return "cnic"
        if 13 <= n <= 19 and _luhn_ok(digits):
            return "payment_card"
        if (n == 11 and digits.startswith("03")) or (n == 12 and digits.startswith("923")):
            return "phone"
        if n >= 9:
            return "long_number"
        if n >= MIN_RUN_DIGITS and digits.startswith("0"):
            return "phone"
        return None

    def scan(self, text: str) -> dict:
        if not text or not self.rules:
            return {"score": 0.0, "matches": []}

        found: dict[str, dict] = {}
        lowered = normalize(text.replace(".", " dot "))
        raw_lower = text.casefold()

        for ip in set(_IPV4.findall(raw_lower)) | set(_spoken_ipv4(lowered)):
            hit = self._hit("ipv4", ip.replace(".", ""))
            if hit:
                hit["phrase"] = f"{self.rules['ipv4'].get('label', 'IPv4')}: {ip.rsplit('.', 1)[0]}.*"
                found[f"ipv4:{ip}"] = hit

        for iban in _IBAN.findall(raw_lower):
            iban = iban.replace(" ", "")
            if 15 <= len(iban) <= 34 and sum(c.isdigit() for c in iban) >= 10:
                hit = self._hit("iban", iban.upper())
                if hit:
                    found[f"iban:{iban}"] = hit

        residual = _IBAN.sub(" ", _IPV4.sub(" ", raw_lower))
        for run in spoken_digit_runs(residual.replace(".", " dot ")):
            if len(run) < MIN_RUN_DIGITS:
                continue
            kind = self.classify_number(run)
            if kind is None:
                continue
            hit = self._hit(kind, run)
            if hit:
                found[f"{kind}:{run}"] = hit

        matches = sorted(found.values(), key=lambda m: m["weight"], reverse=True)
        return {"score": matches[0]["weight"] if matches else 0.0, "matches": matches}


_detector: PatternDetector | None = None


def get_pattern_detector() -> PatternDetector:
    global _detector
    if _detector is None:
        _detector = PatternDetector()
    return _detector


def reset_pattern_detector() -> None:
    global _detector
    _detector = None
