"""Downloads every model the Acoustic Guard needs, once. After this the module runs offline.

    python scripts/fetch_models.py                 # standard: speech + keyword spotting + verifier
    python scripts/fetch_models.py --profile light # no large verifier model (~0.7 GB instead of ~2.3 GB)
    python scripts/fetch_models.py --check         # only report what is present

Profiles
    light     small (speech), tiny (keyword spotting), MiniLM-L12 (intent), Silero VAD
    standard  light + large-v3-turbo second-opinion model   [default]
"""
import argparse
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from huggingface_hub import hf_hub_download, snapshot_download  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.semantic import EMBEDDING_MODELS  # noqa: E402

VAD_ONNX_URL = "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx"

WHISPER_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def whisper_models(profile: str) -> list[str]:
    s = get_settings()
    names = [s.whisper_model]
    if s.kws_enabled:
        names.append(s.kws_model)
    if s.translation_model:
        names.append(s.translation_model)
    if profile == "standard" and s.verifier_model:
        names.append(s.verifier_model)
    return list(dict.fromkeys(n for n in names if n))


def fetch_whisper(name: str) -> None:
    s = get_settings()
    target = s.models_dir / "whisper" / name
    if (target / "model.bin").exists():
        print(f"[fetch_models] whisper/{name} already present")
        return
    repo = WHISPER_REPOS.get(name)
    if repo is None:
        raise SystemExit(f"unknown whisper model '{name}' (choose from {', '.join(WHISPER_REPOS)})")
    print(f"[fetch_models] downloading {repo}")
    snapshot_download(repo_id=repo, local_dir=target,
                      allow_patterns=["model.bin", "config.json", "tokenizer.json", "vocabulary.*", "preprocessor_config.json"])
    print(f"[fetch_models] whisper/{name} ready")


def fetch_semantic() -> None:
    s = get_settings()
    spec = EMBEDDING_MODELS[s.semantic_model]
    target = s.models_dir / "semantic" / s.semantic_model
    for filename in (spec.onnx_file, spec.tokenizer_file):
        if (target / filename).exists():
            continue
        print(f"[fetch_models] downloading {spec.repo}/{filename}")
        hf_hub_download(repo_id=spec.repo, filename=filename, local_dir=target)
    print(f"[fetch_models] semantic/{s.semantic_model} ready")


def fetch_vad() -> None:
    target = get_settings().models_dir / "vad" / "silero_vad.onnx"
    if target.exists():
        print("[fetch_models] vad/silero_vad.onnx already present")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"[fetch_models] downloading {VAD_ONNX_URL}")
    urllib.request.urlretrieve(VAD_ONNX_URL, target)
    print("[fetch_models] vad ready")


def check(profile: str) -> bool:
    s = get_settings()
    spec = EMBEDDING_MODELS[s.semantic_model]
    items = {"vad/silero_vad.onnx": s.models_dir / "vad" / "silero_vad.onnx",
             f"semantic/{s.semantic_model}": s.models_dir / "semantic" / s.semantic_model / spec.onnx_file}
    for name in whisper_models(profile):
        items[f"whisper/{name}"] = s.models_dir / "whisper" / name / "model.bin"
    ok = True
    for label, path in items.items():
        present = path.exists()
        ok &= present
        print(f"  {'ok     ' if present else 'MISSING'}  {label}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", choices=["light", "standard"], default="standard")
    parser.add_argument("--check", action="store_true", help="only report which models are present")
    args = parser.parse_args()

    if args.check:
        sys.exit(0 if check(args.profile) else 1)

    fetch_vad()
    fetch_semantic()
    for name in whisper_models(args.profile):
        fetch_whisper(name)
    print(f"[fetch_models] models dir: {get_settings().models_dir}")
    if check(args.profile):
        print("[fetch_models] all models present - the module can now run offline")
    if args.profile == "light":
        print("[fetch_models] light profile: set ACOUSTIC_VERIFIER_MODEL= (empty) in .env")


if __name__ == "__main__":
    main()
