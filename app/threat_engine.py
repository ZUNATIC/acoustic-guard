import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from app.config import load_threat_registry
from app.keyword_engine import get_keyword_engine
from app.patterns import get_pattern_detector

SEVERITY_ORDER = ["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
REVIEW_ONLY_CATEGORIES = {"safety_threats"}


class SemanticScorer(Protocol):
    def score(self, text: str) -> float: ...


@dataclass
class ThreatResult:
    transcript: str
    keyword_score: float
    semantic_score: float
    threat_score: float
    severity: str
    is_violation: bool
    matches: list[dict] = field(default_factory=list)
    pattern_score: float = 0.0
    translation: str | None = None
    language: str | None = None
    sources: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    risk_factors: list[str] = field(default_factory=list)
    semantic_reference: str | None = None
    review_only: bool = False

    def as_dict(self) -> dict:
        return {
            "transcript": self.transcript,
            "translation": self.translation,
            "language": self.language,
            "keyword_score": round(self.keyword_score, 4),
            "semantic_score": round(self.semantic_score, 4),
            "pattern_score": round(self.pattern_score, 4),
            "threat_score": self.threat_score,
            "severity": self.severity,
            "is_violation": self.is_violation,
            "matches": self.matches,
            "sources": self.sources,
            "categories": self.categories,
            "risk_factors": self.risk_factors,
            "semantic_reference": self.semantic_reference,
            "review_only": self.review_only,
        }


def severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else 0


class ThreatEngine:
    def __init__(self, semantic_scorer: SemanticScorer | None = None):
        registry = load_threat_registry()
        scoring = registry["scoring"]
        self.semantic_weight = float(scoring["semantic_weight"])
        self.pattern_weight = float(scoring.get("pattern_weight", 1.0))
        self.bands = scoring["severity_bands"]
        self.repeat_window = float(scoring.get("repeat_window_seconds", 600))
        self.repeat_count = int(scoring.get("repeat_escalation_count", 2))
        self.keyword_engine = get_keyword_engine()
        self.pattern_detector = get_pattern_detector()
        self.semantic_scorer = semantic_scorer
        self._history: deque[tuple[float, str]] = deque(maxlen=500)
        self._lock = threading.Lock()

    def set_semantic_scorer(self, scorer: SemanticScorer | None) -> None:
        self.semantic_scorer = scorer

    def _severity(self, score: float) -> str:
        if score >= self.bands["critical"]:
            return "CRITICAL"
        if score >= self.bands["high"]:
            return "HIGH"
        if score >= self.bands["medium"]:
            return "MEDIUM"
        if score >= self.bands["low"]:
            return "LOW"
        return "NONE"

    def _band_floor(self, severity: str) -> float:
        return {"CRITICAL": self.bands["critical"], "HIGH": self.bands["high"],
                "MEDIUM": self.bands["medium"], "LOW": self.bands["low"]}.get(severity, 0.0)

    def _semantic(self, texts: list[str]) -> tuple[float, str | None]:
        if self.semantic_scorer is None:
            return 0.0, None
        best, ref = 0.0, None
        for t in texts:
            if hasattr(self.semantic_scorer, "explain"):
                info = self.semantic_scorer.explain(t)
                s, r = info["score"], info["closest_reference"]
            else:
                s, r = self.semantic_scorer.score(t), None
            if s > best:
                best, ref = s, r
        return best, ref

    def _escalate(self, categories: list[str], severity: str, now: float) -> tuple[str, list[str]]:
        factors: list[str] = []
        if severity_rank(severity) < severity_rank("MEDIUM") or not categories:
            return severity, factors
        with self._lock:
            while self._history and now - self._history[0][0] > self.repeat_window:
                self._history.popleft()
            repeats = max(sum(1 for _, c in self._history if c == cat) for cat in categories)
            for cat in categories:
                self._history.append((now, cat))
        if repeats >= self.repeat_count:
            factors.append(f"repeated disclosure ({repeats + 1}x in {int(self.repeat_window / 60)} min)")
            idx = min(severity_rank(severity) + 1, len(SEVERITY_ORDER) - 1)
            severity = SEVERITY_ORDER[idx]
        return severity, factors

    def evaluate(self, transcript: str, translation: str | None = None, language: str | None = None,
                 extra_matches: list[dict] | None = None, track_history: bool = False,
                 duration: float | None = None) -> ThreatResult:
        transcript = (transcript or "").strip()
        translation = (translation or "").strip() or None
        if not transcript and not translation:
            return ThreatResult(transcript="", keyword_score=0.0, semantic_score=0.0,
                                threat_score=0.0, severity="NONE", is_violation=False)

        texts = [t for t in (transcript, translation) if t]
        matches: dict[tuple[str, str], dict] = {}
        kw_score = 0.0
        for t in texts:
            r = self.keyword_engine.scan(t)
            kw_score = max(kw_score, r["score"])
            for m in r["matches"]:
                matches.setdefault((m["category"], m["phrase"]), m)

        pat_score = 0.0
        for t in texts:
            r = self.pattern_detector.scan(t)
            pat_score = max(pat_score, r["score"])
            for m in r["matches"]:
                matches.setdefault((m["category"], m["phrase"]), m)

        sources: list[str] = []
        for m in extra_matches or []:
            key = (m["category"], m["phrase"])
            if key not in matches:
                matches[key] = m
                kw_score = max(kw_score, m["weight"])
            if "kws" not in sources:
                sources.append("kws")

        sem_score, sem_ref = self._semantic(texts)

        fused = 1 - (1 - kw_score) * (1 - self.semantic_weight * sem_score) * (1 - self.pattern_weight * pat_score)
        fused = round(min(max(fused, 0.0), 1.0), 4)
        severity = self._severity(fused)

        ordered = sorted(matches.values(), key=lambda m: m["weight"], reverse=True)
        categories = list(dict.fromkeys(m["category"] for m in ordered))

        if any(m.get("match", "exact") == "exact" and m["category"] != "sensitive_pattern" for m in ordered):
            sources.append("keyword")
        if any(m.get("match") == "fuzzy" for m in ordered):
            sources.append("phonetic")
        if pat_score > 0:
            sources.append("pattern")
        if sem_score >= 0.5:
            sources.append("semantic")
        if translation and any(self.keyword_engine.scan(translation)["matches"]):
            sources.append("translation")

        risk_factors: list[str] = []
        if duration and duration >= 8 and severity_rank(severity) >= severity_rank("MEDIUM"):
            risk_factors.append(f"sustained disclosure ({duration:.0f}s of speech)")
        if track_history:
            new_severity, factors = self._escalate(categories, severity, time.time())
            if new_severity != severity:
                fused = max(fused, self._band_floor(new_severity))
                severity = new_severity
            risk_factors.extend(factors)

        review_only = bool(categories) and categories[0] in REVIEW_ONLY_CATEGORIES

        return ThreatResult(
            transcript=transcript,
            translation=translation,
            language=language,
            keyword_score=kw_score,
            semantic_score=sem_score,
            pattern_score=pat_score,
            threat_score=fused,
            severity=severity,
            is_violation=severity in ("HIGH", "CRITICAL"),
            matches=ordered,
            sources=list(dict.fromkeys(sources)),
            categories=categories,
            risk_factors=risk_factors,
            semantic_reference=sem_ref,
            review_only=review_only,
        )


_engine: ThreatEngine | None = None


def get_threat_engine() -> ThreatEngine:
    global _engine
    if _engine is None:
        _engine = ThreatEngine()
    return _engine


def reset_threat_engine() -> None:
    global _engine
    _engine = None
