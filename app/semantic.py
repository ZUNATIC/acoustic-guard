from dataclasses import dataclass

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from app.config import get_settings, load_threat_registry


@dataclass(frozen=True)
class EmbeddingModel:
    repo: str
    onnx_file: str
    tokenizer_file: str
    prefix: str = ""
    # margin (closest exfiltration phrase minus closest benign phrase) mapped to 0..1,
    # tuned per model on held-out leak / benign sentences in five languages
    floor: float = 0.0
    ceiling: float = 1.0


EMBEDDING_MODELS: dict[str, EmbeddingModel] = {
    "paraphrase-multilingual-MiniLM-L12-v2": EmbeddingModel(
        repo="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        onnx_file="onnx/model_quint8_avx2.onnx",
        tokenizer_file="tokenizer.json",
        floor=0.04,
        ceiling=0.30,
    ),
    "paraphrase-multilingual-mpnet-base-v2": EmbeddingModel(
        repo="sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
        onnx_file="onnx/model_quint8_avx2.onnx",
        tokenizer_file="tokenizer.json",
        floor=0.10,
        ceiling=0.45,
    ),
    "multilingual-e5-small": EmbeddingModel(
        repo="intfloat/multilingual-e5-small",
        onnx_file="onnx/model_qint8_avx512_vnni.onnx",
        tokenizer_file="onnx/tokenizer.json",
        prefix="query: ",
        floor=0.015,
        ceiling=0.07,
    ),
}


def _mean_pool(last_hidden_state: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    mask = attention_mask[..., None].astype(np.float32)
    summed = (last_hidden_state * mask).sum(axis=1)
    counts = np.clip(mask.sum(axis=1), a_min=1e-9, a_max=None)
    return summed / counts


def _l2_normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms = np.clip(norms, a_min=1e-9, a_max=None)
    return vectors / norms


class SemanticScorer:
    def __init__(self, model_name: str | None = None, reference_phrases: list[str] | None = None,
                 benign_phrases: list[str] | None = None):
        settings = get_settings()
        self.model_name = model_name or settings.semantic_model
        if self.model_name not in EMBEDDING_MODELS:
            raise ValueError(f"unknown semantic model '{self.model_name}'")
        self.spec = EMBEDDING_MODELS[self.model_name]

        model_dir = settings.models_dir / "semantic" / self.model_name
        onnx_path = model_dir / self.spec.onnx_file
        tokenizer_path = model_dir / self.spec.tokenizer_file
        if not onnx_path.exists() or not tokenizer_path.exists():
            raise FileNotFoundError(
                f"semantic model files missing under {model_dir}. Run scripts/fetch_models.py first."
            )

        self.max_tokens = settings.semantic_max_tokens
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(onnx_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=self.max_tokens)
        self.tokenizer.enable_padding()
        self._input_names = {i.name for i in self.session.get_inputs()}

        registry = load_threat_registry() if reference_phrases is None or benign_phrases is None else {}
        if reference_phrases is None:
            reference_phrases = registry["semantic_reference_phrases"]
        if benign_phrases is None:
            benign_phrases = registry.get("semantic_benign_phrases", [])
        self.reference_phrases = list(reference_phrases)
        self.benign_phrases = list(benign_phrases)
        self.reference_embeddings = self.embed(self.reference_phrases)
        self.benign_embeddings = self.embed(self.benign_phrases) if self.benign_phrases else None

    def embed(self, texts: list[str]) -> np.ndarray:
        texts = [self.spec.prefix + t for t in texts]
        encodings = self.tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feed = {"input_ids": input_ids, "attention_mask": attention_mask}
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros_like(input_ids)
        last_hidden_state = self.session.run(None, feed)[0]
        return _l2_normalize(_mean_pool(last_hidden_state, attention_mask))

    def similarities(self, text: str) -> tuple[float, str, float]:
        embedding = self.embed([text])[0]
        sims = self.reference_embeddings @ embedding
        idx = int(np.argmax(sims))
        benign = float(np.max(self.benign_embeddings @ embedding)) if self.benign_embeddings is not None else 0.0
        return float(sims[idx]), self.reference_phrases[idx], benign

    def raw_similarity(self, text: str) -> tuple[float, str]:
        sim, ref, _ = self.similarities(text)
        return sim, ref

    def margin(self, text: str) -> tuple[float, str]:
        sim, ref, benign = self.similarities(text)
        return sim - benign, ref

    def calibrate(self, similarity: float) -> float:
        span = self.spec.ceiling - self.spec.floor
        return float(min(max((similarity - self.spec.floor) / span, 0.0), 1.0))

    def score(self, text: str) -> float:
        if not text or not text.strip():
            return 0.0
        margin, _ = self.margin(text)
        return self.calibrate(margin)

    def explain(self, text: str) -> dict:
        if not text or not text.strip():
            return {"score": 0.0, "similarity": 0.0, "margin": 0.0, "closest_reference": None}
        sim, ref, benign = self.similarities(text)
        return {
            "score": round(self.calibrate(sim - benign), 4),
            "similarity": round(sim, 4),
            "margin": round(sim - benign, 4),
            "closest_reference": ref,
        }


_scorer: SemanticScorer | None = None
_load_failed = False


def get_semantic_scorer() -> SemanticScorer | None:
    global _scorer, _load_failed
    if _scorer is not None:
        return _scorer
    if _load_failed:
        return None
    try:
        _scorer = SemanticScorer()
        return _scorer
    except FileNotFoundError:
        _load_failed = True
        return None


def reset_semantic_scorer() -> None:
    global _scorer, _load_failed
    _scorer = None
    _load_failed = False
