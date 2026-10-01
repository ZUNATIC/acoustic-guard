import numpy as np

from app.config import get_settings
from app.keyword_engine import KeywordEngine, get_keyword_engine


class KeywordSpotter:
    """Low-latency keyword path that runs while someone is still talking.

    Every `hop` seconds the last `window` seconds of the live speech buffer are decoded with a
    small, fast Whisper model and matched against the threat registry. A phrase is only
    reported once it has appeared in `confirm` consecutive windows, which filters one-off
    mishearings from the small model. Windows overlap, so a spoken phrase normally lands in two
    windows in a row.
    """

    def __init__(self, model=None, keyword_engine: KeywordEngine | None = None):
        s = get_settings()
        self.settings = s
        self._model = model
        self.keyword_engine = keyword_engine or get_keyword_engine()
        self.window_samples = int(s.kws_window_seconds * s.sample_rate)
        self.hop_samples = int(s.kws_hop_seconds * s.sample_rate)
        self.confirm = max(1, s.kws_confirm_windows)
        self.reset()

    @property
    def model(self):
        if self._model is None:
            from app.transcriber import get_speech_model

            self._model = get_speech_model(self.settings.kws_model, cpu_threads=2)
        return self._model

    def reset(self) -> None:
        self._streak: dict[tuple[str, str], int] = {}
        self._reported: set[tuple[str, str]] = set()
        self._last_run_at = 0

    def due(self, buffered_samples: int) -> bool:
        if buffered_samples < self.window_samples:
            return False
        return buffered_samples - self._last_run_at >= self.hop_samples

    def mark_submitted(self, buffered_samples: int) -> None:
        self._last_run_at = buffered_samples

    def hits_for_text(self, text: str) -> list[dict]:
        return [m for m in self.keyword_engine.scan(text)["matches"]]

    def update(self, matches: list[dict]) -> list[dict]:
        seen = {(m["category"], m["phrase"]): m for m in matches}
        for key in list(self._streak):
            if key not in seen:
                self._streak.pop(key)
        confirmed = []
        for key, m in seen.items():
            self._streak[key] = self._streak.get(key, 0) + 1
            if self._streak[key] >= self.confirm and key not in self._reported:
                self._reported.add(key)
                confirmed.append({**m, "match": "kws", "windows": self._streak[key]})
        return confirmed

    def process(self, window: np.ndarray, language: str | None = None) -> tuple[str, list[dict]]:
        out = self.model.analyze(
            window, language=language, translate=False,
            remap=self.settings.language_remap, beam_size=1,
        )
        text = out["text"]
        return text, self.update(self.hits_for_text(text) if text else [])
