import re
import threading
import time
from dataclasses import dataclass, field

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.audio import pad_or_trim
from faster_whisper.tokenizer import Tokenizer
from faster_whisper.transcribe import get_compression_ratio, get_suppressed_tokens

from app.config import get_settings

LANGUAGE_NAMES = {
    "en": "English", "ur": "Urdu", "hi": "Hindi", "ar": "Arabic", "fa": "Persian",
    "pa": "Punjabi", "ps": "Pashto", "sd": "Sindhi", "bn": "Bengali", "tr": "Turkish",
    "zh": "Chinese", "fr": "French", "de": "German", "es": "Spanish", "ru": "Russian",
    "ja": "Japanese", "ko": "Korean", "it": "Italian", "pt": "Portuguese", "id": "Indonesian",
    "ms": "Malay", "nl": "Dutch", "sv": "Swedish", "pl": "Polish", "uk": "Ukrainian",
}

# what Whisper tends to produce on silence, breathing or very short noise bursts
_HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching", "bye",
    "subtitles by the amara org community", "please subscribe", "so", "okay",
    "شکریہ", "धन्यवाद", "ترجمہ", "موسیقی", "music",
}

_WORDS = re.compile(r"\w+", re.UNICODE)

WINDOW_SAMPLES = 16000 * 30
MAX_TOKENS = 224


def collapse_repetitions(text: str, max_ngram: int = 6) -> str:
    words = text.split()
    if len(words) < 4:
        return text
    changed = True
    while changed:
        changed = False
        for n in range(1, max_ngram + 1):
            i = 0
            out: list[str] = []
            while i < len(words):
                gram = words[i:i + n]
                j = i + n
                repeats = 1
                while len(gram) == n and words[j:j + n] == gram:
                    repeats += 1
                    j += n
                if repeats >= 3:
                    out.extend(gram)
                    i = j
                    changed = True
                else:
                    out.append(words[i])
                    i += 1
            words = out
    return " ".join(words)


def is_hallucination(text: str) -> bool:
    key = " ".join(_WORDS.findall(text.casefold()))
    return not key or key in _HALLUCINATIONS


@dataclass
class Transcription:
    text: str = ""
    language: str | None = None
    language_name: str | None = None
    language_probability: float = 0.0
    detected_as: str | None = None
    translation: str | None = None
    duration: float = 0.0
    elapsed: float = 0.0
    model: str | None = None
    passes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "language": self.language,
            "language_name": self.language_name,
            "language_probability": round(self.language_probability, 3),
            "detected_as": self.detected_as,
            "translation": self.translation,
            "audio_seconds": round(self.duration, 2),
            "asr_seconds": round(self.elapsed, 2),
            "model": self.model,
        }


class SpeechModel:
    """One Whisper model. Each audio window is encoded once; language detection, the native
    transcript and the English translation are all decoded from that same encoder output."""

    def __init__(self, name: str, cpu_threads: int | None = None):
        settings = get_settings()
        model_dir = settings.models_dir / "whisper" / name
        if not (model_dir / "model.bin").exists():
            raise FileNotFoundError(
                f"Whisper model '{name}' missing at {model_dir}. Run scripts/fetch_models.py first."
            )
        self.name = name
        self.model = WhisperModel(
            str(model_dir),
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            cpu_threads=cpu_threads or settings.whisper_cpu_threads,
        )
        self.multilingual = self.model.model.is_multilingual
        self._base_tokenizer = Tokenizer(self.model.hf_tokenizer, self.multilingual, task="transcribe", language="en")
        self._suppress = list(get_suppressed_tokens(self._base_tokenizer, [-1]))
        self._lock = threading.Lock()

    def _windows(self, audio: np.ndarray) -> list[np.ndarray]:
        audio = np.asarray(audio, dtype=np.float32)
        return [audio[i:i + WINDOW_SAMPLES] for i in range(0, max(len(audio), 1), WINDOW_SAMPLES)]

    def _encode(self, window: np.ndarray):
        features = self.model.feature_extractor(window)
        features = pad_or_trim(features[:, :3000])
        return self.model.encode(features)

    def detect_language(self, encoded, allowed: list[str] | None = None) -> tuple[str, float, dict]:
        if not self.multilingual:
            return "en", 1.0, {"en": 1.0}
        pairs = self.model.model.detect_language(encoded)[0]
        probs = {tok.strip("<|>"): float(p) for tok, p in pairs}
        pool = {k: v for k, v in probs.items() if not allowed or k in allowed} or probs
        lang = max(pool, key=pool.get)
        return lang, pool[lang], probs

    def _decode(self, encoded, language: str, task: str, beam_size: int) -> tuple[str, float, float]:
        tok = Tokenizer(
            self.model.hf_tokenizer, self.multilingual,
            task=task if self.multilingual else "transcribe",
            language=language if self.multilingual else "en",
        )
        prompt = list(tok.sot_sequence) + [tok.no_timestamps]

        def run(**extra):
            r = self.model.model.generate(
                encoded, [prompt],
                beam_size=beam_size, max_length=MAX_TOKENS,
                return_scores=True, return_no_speech_prob=True,
                suppress_blank=True, suppress_tokens=self._suppress,
                **extra,
            )[0]
            ids = [i for i in r.sequences_ids[0] if i < tok.eot]
            text = tok.decode(ids).strip()
            n = len(ids)
            avg_lp = (r.scores[0] * n) / (n + 1) if n else -10.0
            return text, avg_lp, float(r.no_speech_prob)

        text, avg_lp, no_speech = run()
        if text and (get_compression_ratio(text) > 2.4 or avg_lp < -1.0):
            text2, avg_lp2, _ = run(repetition_penalty=1.25, no_repeat_ngram_size=3)
            if text2 and avg_lp2 >= avg_lp - 0.2:
                text, avg_lp = text2, avg_lp2
        if no_speech > 0.6 and avg_lp < -1.0:
            return "", avg_lp, no_speech
        return collapse_repetitions(text), avg_lp, no_speech

    def analyze(self, audio: np.ndarray, language: str | None = None, translate: bool = False,
                allowed: list[str] | None = None, remap: dict | None = None,
                beam_size: int = 1, translate_if=None) -> dict:
        native_parts, english_parts = [], []
        lang, prob, detected = language, 1.0 if language else 0.0, None

        with self._lock:
            for idx, window in enumerate(self._windows(audio)):
                encoded = self._encode(window)
                if lang is None or (idx == 0 and not language):
                    detected, prob, _ = self.detect_language(encoded, allowed)
                    lang = (remap or {}).get(detected, detected)
                text, _, _ = self._decode(encoded, lang, "transcribe", beam_size)
                native_parts.append(text)
                wanted = translate_if is None or translate_if(" ".join(native_parts))
                if translate and wanted and self.multilingual and lang != "en" and text:
                    eng, _, _ = self._decode(encoded, lang, "translate", beam_size)
                    english_parts.append(eng)

        return {
            "text": " ".join(p for p in native_parts if p).strip(),
            "language": lang,
            "language_probability": prob,
            "detected_as": detected,
            "translation": " ".join(p for p in english_parts if p).strip() or None,
        }


class Transcriber:
    def __init__(self, primary: "SpeechModel | None" = None, translator: "SpeechModel | None" = None):
        self.settings = get_settings()
        self.primary = primary or get_speech_model(self.settings.whisper_model)
        self._translator = translator

    @property
    def translator(self) -> SpeechModel | None:
        name = self.settings.translation_model
        if self._translator is None and name and name != self.primary.name:
            self._translator = get_speech_model(name)
        return self._translator

    def transcribe(self, audio: np.ndarray, translate: bool | None = None,
                   language: str | None = None, translate_if=None) -> Transcription:
        """translate_if(native_text) -> bool lets the caller skip the translation pass when the
        native transcript alone already settles the verdict."""
        s = self.settings
        started = time.perf_counter()
        result = Transcription(duration=len(audio) / s.sample_rate, model=self.primary.name)
        do_translate = s.translate_non_english if translate is None else translate
        inline_translate = do_translate and self.translator is None

        out = self.primary.analyze(
            audio,
            language=language or s.whisper_language,
            translate=inline_translate,
            remap=s.language_remap,
            beam_size=s.whisper_beam_size,
            translate_if=translate_if,
        )
        result.passes.append(f"{self.primary.name}:transcribe")

        if is_hallucination(out["text"]):
            result.elapsed = time.perf_counter() - started
            return result

        result.text = out["text"]
        result.language = out["language"]
        result.language_name = LANGUAGE_NAMES.get(out["language"] or "", out["language"])
        result.language_probability = out["language_probability"]
        result.detected_as = out["detected_as"]
        result.translation = out["translation"]
        if inline_translate and result.translation:
            result.passes.append(f"{self.primary.name}:translate")

        if do_translate and not inline_translate and result.language != "en":
            extra = self.translator.analyze(audio, language=result.language, translate=True, beam_size=s.whisper_beam_size)
            result.translation = extra["translation"]
            result.passes.append(f"{self.translator.name}:translate")

        if result.translation and is_hallucination(result.translation):
            result.translation = None

        result.elapsed = time.perf_counter() - started
        return result


_models: dict[str, SpeechModel] = {}
_models_lock = threading.Lock()


def get_speech_model(name: str, cpu_threads: int | None = None) -> SpeechModel:
    with _models_lock:
        if name not in _models:
            _models[name] = SpeechModel(name, cpu_threads=cpu_threads)
        return _models[name]


def loaded_speech_models() -> list[str]:
    return sorted(_models)


_transcriber: Transcriber | None = None


def get_transcriber() -> Transcriber:
    global _transcriber
    if _transcriber is None:
        _transcriber = Transcriber()
    return _transcriber


def reset_transcriber() -> None:
    global _transcriber
    _transcriber = None
