import ahocorasick
from rapidfuzz import fuzz

from app.config import load_threat_registry
from app.textnorm import normalize, phonetic_skeleton

INFLECTION_SUFFIXES = {
    "s", "es", "ed", "d", "ing", "er", "ers",
    "ں", "وں", "یں", "ے", "ی",
    "ों", "ें", "े",
}

BASE_LANGUAGE = "en"

FUZZY_MIN_CHARS = 6
FUZZY_DISCOUNT = 0.9


def _fuzzy_threshold(length: int) -> float:
    return 86.0 if length >= 10 else 90.0


def _iter_phrases(data: dict, default_weight: float):
    for entry in data.get("phrases", []) or []:
        yield BASE_LANGUAGE, entry, default_weight
    for lang, entries in (data.get("variants") or {}).items():
        for entry in entries or []:
            yield lang, entry, default_weight


def _unpack(entry, default_weight: float) -> tuple[str, float]:
    if isinstance(entry, dict):
        return str(entry.get("text", "")), float(entry.get("weight", default_weight))
    return str(entry), default_weight


class KeywordEngine:
    def __init__(self, registry: dict | None = None):
        registry = registry or load_threat_registry()
        self.category_weights: dict[str, float] = {}
        self.phrase_count = 0
        self.languages: set[str] = set()
        self.automaton = ahocorasick.Automaton()
        self.fuzzy_entries: list[tuple[str, str, str, float, str]] = []
        self.fuzzy_enabled = True

        for category, data in registry["categories"].items():
            weight = float(data["weight"])
            self.category_weights[category] = weight
            for lang, entry, default_w in _iter_phrases(data, weight):
                text, w = _unpack(entry, default_w)
                key = normalize(text)
                if not key:
                    continue
                payload = (category, text, w, lang)
                if key in self.automaton:
                    existing = self.automaton.get(key)
                    if existing[2] >= w:
                        continue
                else:
                    self.phrase_count += 1
                self.automaton.add_word(key, payload)
                self.languages.add(lang)
                skeleton = phonetic_skeleton(text)
                if len(skeleton) >= FUZZY_MIN_CHARS:
                    self.fuzzy_entries.append((skeleton, category, text, w, lang))

        if self.phrase_count:
            self.automaton.make_automaton()

    @staticmethod
    def _bounded(padded: str, start: int, end: int) -> bool:
        if padded[start - 1] != " ":
            return False
        nxt = end + 1
        if padded[nxt] == " ":
            return True
        token_end = padded.find(" ", nxt)
        suffix = padded[nxt:token_end]
        return suffix in INFLECTION_SUFFIXES

    def scan(self, text: str) -> dict:
        if not self.phrase_count:
            return {"score": 0.0, "matches": []}

        normalized = normalize(text)
        padded = f" {normalized} "
        best: dict[tuple[str, str], dict] = {}

        for end, (category, phrase, weight, lang) in self.automaton.iter(padded):
            key_len = len(normalize(phrase))
            start = end - key_len + 1
            if start < 1 or not self._bounded(padded, start, end):
                continue
            k = (category, phrase)
            if k not in best:
                best[k] = {"category": category, "phrase": phrase, "weight": weight, "lang": lang}

        for m in best.values():
            m["match"] = "exact"

        if self.fuzzy_enabled and self.fuzzy_entries:
            text_skeleton = phonetic_skeleton(text)
            if len(text_skeleton) >= FUZZY_MIN_CHARS:
                for skeleton, category, phrase, weight, lang in self.fuzzy_entries:
                    if (category, phrase) in best:
                        continue
                    similarity = fuzz.partial_ratio(skeleton, text_skeleton)
                    if similarity >= _fuzzy_threshold(len(skeleton)):
                        best[(category, phrase)] = {
                            "category": category, "phrase": phrase, "lang": lang,
                            "weight": round(weight * FUZZY_DISCOUNT, 4),
                            "match": "fuzzy", "similarity": round(similarity, 1),
                        }

        exact_best: dict[str, float] = {}
        for m in best.values():
            if m["match"] == "exact":
                exact_best[m["category"]] = max(exact_best.get(m["category"], 0.0), m["weight"])
        candidates = [
            m for m in best.values()
            if m["match"] == "exact" or m["weight"] > exact_best.get(m["category"], 0.0)
        ]
        matches = self._drop_contained(candidates)
        matches.sort(key=lambda m: m["weight"], reverse=True)
        score = matches[0]["weight"] if matches else 0.0
        return {"score": score, "matches": matches}

    @staticmethod
    def _drop_contained(matches: list[dict]) -> list[dict]:
        keys = [(m, f" {normalize(m['phrase'])} ") for m in matches]
        kept = []
        for m, key in keys:
            covered = any(
                other is not m and other["category"] == m["category"]
                and key != okey and key in okey and other["weight"] >= m["weight"]
                for other, okey in keys
            )
            if not covered:
                kept.append(m)
        return kept

    def summary(self) -> dict:
        return {
            "phrases": self.phrase_count,
            "categories": len(self.category_weights),
            "languages": sorted(self.languages),
        }


_engine: KeywordEngine | None = None


def get_keyword_engine() -> KeywordEngine:
    global _engine
    if _engine is None:
        _engine = KeywordEngine()
    return _engine


def reset_keyword_engine() -> None:
    global _engine
    _engine = None
