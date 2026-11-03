"""下載 sherpa-onnx 的預訓練 TTS 模型到 ``models/sherpa``。

用法：
    .\\venv\\Scripts\\python.exe tools\\sherpa_get_model.py --list
    .\\venv\\Scripts\\python.exe tools\\sherpa_get_model.py vits-cantonese-hf-xiaomaiiwn
    .\\venv\\Scripts\\python.exe tools\\sherpa_get_model.py vits-piper-zh_CN-huayan-medium
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t2vn import sherpa_tts  # noqa: E402

BASE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"


def download(model_id: str, root: Path) -> Path:
    if not model_id or "/" in model_id or "\\" in model_id or model_id.startswith("."):
        raise SystemExit(f"invalid model id: {model_id!r}")
    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"{model_id}.tar.bz2"
    url = f"{BASE_URL}/{model_id}.tar.bz2"
    print(f"downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=600) as response, archive.open("wb") as out:
        shutil.copyfileobj(response, out)
    print(f"extracting {archive.name}", flush=True)
    with tarfile.open(archive, "r:bz2") as tar:
        for member in tar.getmembers():
            target = (root / member.name).resolve()
            if root.resolve() not in target.parents and target != root.resolve():
                raise SystemExit(f"unsafe archive member: {member.name}")
        tar.extractall(root)
    archive.unlink()
    folder = root / model_id
    if not folder.is_dir():
        raise SystemExit(f"extracted folder not found: {folder}")
    print(f"done: {folder}", flush=True)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_id", nargs="?")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--all", action="store_true", help="download all models in the catalog")
    args = parser.parse_args()
    if args.list or (not args.model_id and not args.all):
        print("installed:")
        for model in sherpa_tts.list_models():
            print(f"  {model['path']}  {model['label']}  speakers={model.get('speakers')}")
        print("\ncatalog:")
        for name, info in sherpa_tts.MODEL_CATALOG.items():
            print(f"  {name}  {info.get('label')}")
        return 0
    targets = list(sherpa_tts.MODEL_CATALOG) if args.all else [args.model_id]
    for name in targets:
        download(name, sherpa_tts.MODELS_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
