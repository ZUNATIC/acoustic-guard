import re
import unicodedata

_ARABIC_MARKS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")

_LETTER_MAP = str.maketrans(
    {
        "ي": "ی",  # ي -> ی
        "ى": "ی",  # ى -> ی
        "ك": "ک",  # ك -> ک
        "ه": "ہ",  # ه -> ہ
        "ۀ": "ہ",  # ۀ -> ہ
        "ة": "ہ",  # ة -> ہ
        "أ": "ا",  # أ -> ا
        "إ": "ا",  # إ -> ا
        "ٱ": "ا",  # ٱ -> ا
        "़": None,      # devanagari nukta
        "ँ": "ं",  # chandrabindu -> anusvara
        "‌": None,      # ZWNJ
        "‍": None,      # ZWJ
        "‏": None,      # RLM
        "‎": None,      # LRM
    }
)

_DIGIT_MAP = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹०१२३४५६७८९",
    "012345678901234567890123456789",
)

_WS = re.compile(r"\s+")


def _strip_punct(text: str) -> str:
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] in ("P", "S") or cat == "Cc":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = _ARABIC_MARKS.sub("", text)
    text = text.translate(_LETTER_MAP).translate(_DIGIT_MAP)
    text = text.replace("'", "").replace("’", "")
    text = _strip_punct(text)
    return _WS.sub(" ", text).strip()


# Folds letters that Whisper confuses in Urdu/Arabic/Hindi output (retroflex vs dental,
# the three s/z families, aspiration, nasal marks) so near-spellings share one skeleton.
_PHONETIC_MAP = str.maketrans(
    {
        "ڑ": "ر", "ڈ": "د", "ٹ": "ت", "ط": "ت", "ث": "س", "ص": "س",
        "ذ": "ز", "ض": "ز", "ظ": "ز", "ح": "ہ", "ۃ": "ہ", "ھ": None,
        "ع": "ا", "آ": "ا", "ں": "ن", "ے": "ی", "ئ": "ی", "ؤ": "و", "ق": "ک",
        "ड": "द", "ट": "त", "ठ": "थ", "ढ": "ध", "ण": "न", "ष": "श", "ं": "न",
        "ॉ": "ो",
    }
)


def phonetic_skeleton(text: str) -> str:
    return normalize(text).translate(_PHONETIC_MAP).replace(" ", "")


_SCRIPT_RANGES = (
    ("arabic", 0x0600, 0x06FF),
    ("arabic", 0x0750, 0x077F),
    ("arabic", 0xFB50, 0xFDFF),
    ("arabic", 0xFE70, 0xFEFF),
    ("devanagari", 0x0900, 0x097F),
    ("gurmukhi", 0x0A00, 0x0A7F),
    ("bengali", 0x0980, 0x09FF),
    ("cyrillic", 0x0400, 0x04FF),
    ("cjk", 0x4E00, 0x9FFF),
)


def dominant_script(text: str) -> str:
    counts: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        name = "latin" if cp < 0x0250 else "other"
        for script, lo, hi in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                name = script
                break
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return "none"
    return max(counts, key=counts.get)


RTL_SCRIPTS = {"arabic"}


def is_rtl(text: str) -> bool:
    return dominant_script(text) in RTL_SCRIPTS
