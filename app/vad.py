import numpy as np
import onnxruntime as ort

from app.config import get_settings


class SileroVAD:
    def __init__(self):
        settings = get_settings()
        model_path = settings.models_dir / "vad" / "silero_vad.onnx"
        if not model_path.exists():
            raise FileNotFoundError(
                f"VAD model missing at {model_path}. Run scripts/fetch_models.py first."
            )

        self.chunk_samples = settings.vad_chunk_samples
        self.sample_rate = settings.sample_rate

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._sr = np.array(self.sample_rate, dtype=np.int64)
        self.context_size = 64 if self.sample_rate == 16000 else 32
        self.reset()

    def reset(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.context_size), dtype=np.float32)

    def process_chunk(self, chunk: np.ndarray) -> float:
        if chunk.shape[-1] != self.chunk_samples:
            raise ValueError(
                f"Silero VAD requires exactly {self.chunk_samples} samples per chunk, "
                f"got {chunk.shape[-1]}"
            )

        chunk = chunk.reshape(1, -1).astype(np.float32)
        input_tensor = np.concatenate([self._context, chunk], axis=1)
        prob, self._state = self.session.run(
            None, {"input": input_tensor, "state": self._state, "sr": self._sr}
        )
        self._context = input_tensor[:, -self.context_size:]
        return float(prob[0][0])


_vad: SileroVAD | None = None


def get_vad() -> SileroVAD:
    global _vad
    if _vad is None:
        _vad = SileroVAD()
    return _vad
