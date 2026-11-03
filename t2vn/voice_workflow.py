"""MOSS-TTS workflow（ComfyUI API 格式）的填值與輸出解析。

模板：``workflows/moss_tts_clone_api.json``（Voice Clone／一般 TTS）與
``workflows/moss_tts_design_api.json``（Voice Design）。純函式，不連 ComfyUI、不讀寫檔案。
規格見 ``docs/33_語音管線規格.md``。
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from .audio_workflow import output_files

MODEL_LOCAL = "MOSS-TTS (Local 1.7B)"
MODEL_DELAY = "MOSS-TTS (Delay 8B)"
MODEL_DESIGN = "MOSS-VoiceGenerator"
ENGINE_LOCAL = "moss-tts-local-1.7b"
ENGINE_DELAY = "moss-tts-delay-8b"
ENGINE_DESIGN = "moss-voicegenerator-1.7b"

CLASS_LOADER = "MossTTSModelLoader"
CLASS_LOAD_AUDIO = "LoadAudio"
CLASS_GENERATE = "MossTTSGenerate"
CLASS_DESIGN = "MossTTSVoiceDesign"
CLASS_SAVE = "SaveAudioAdvanced"
MAX_SEED = 2**31 - 1
LANGS = ("auto", "zh", "en", "ja", "ko")

# 模型建議取樣參數（節點包的 tips）
DEFAULT_PARAMS = {
    ENGINE_LOCAL: {"temperature": 1.0, "top_p": 0.95, "top_k": 50, "repetition_penalty": 1.1},
    ENGINE_DESIGN: {"temperature": 1.5, "top_p": 0.6, "top_k": 50, "repetition_penalty": 1.1},
    ENGINE_DELAY: {"temperature": 1.7, "top_p": 0.8, "top_k": 25, "repetition_penalty": 1.0},
}
MODEL_FOR_ENGINE = {
    ENGINE_LOCAL: MODEL_LOCAL,
    ENGINE_DELAY: MODEL_DELAY,
    ENGINE_DESIGN: MODEL_DESIGN,
}


def _find_nodes(workflow: dict[str, Any], class_type: str) -> list[str]:
    if not isinstance(workflow, dict):
        return []
    return [node_id for node_id, node in workflow.items()
            if isinstance(node, dict) and node.get("class_type") == class_type]


def _one(workflow: dict[str, Any], class_type: str, required: bool = True) -> str | None:
    nodes = _find_nodes(workflow, class_type)
    if not nodes:
        if required:
            raise ValueError(f"template is missing a {class_type} node")
        return None
    if len(nodes) > 1:
        raise ValueError(f"more than one {class_type} node: {', '.join(map(str, nodes))}")
    return nodes[0]


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _inputs(workflow: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = workflow[node_id]
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
        node["inputs"] = inputs
    return inputs


def _check_seed(seed: int) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
        raise ValueError(f"seed must be an integer between 0 and {MAX_SEED}: {seed!r}")
    return seed


def _params(engine: str, overrides: dict[str, Any] | None) -> dict[str, Any]:
    params = dict(DEFAULT_PARAMS.get(engine, DEFAULT_PARAMS[ENGINE_LOCAL]))
    for key in ("temperature", "top_p", "top_k", "repetition_penalty", "max_new_tokens"):
        value = (overrides or {}).get(key)
        if value is not None:
            params[key] = value
    return params


def estimate_max_tokens(text: str, floor: int = 64, cap: int = 4096) -> int:
    """依台詞長度估一個安全上限（1 秒 ≈ 12.5 個音訊 frame、中文約 4–5 字／秒）。

    MOSS 的 ``max_new_tokens`` 預設 4096 對單句太寬；若模型沒吐出 EOS，會一路生成到
    上限（實測 13 字跑 6 分鐘還沒停）。用 3.5 token／字＋40 的餘量，通常足够念完且不會
    無限拉長。
    """
    length = len(str(text or ""))
    return max(floor, min(cap, int(length * 3.5) + 40))


def _filename_prefix(value: str | None, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    if text.startswith("/") or "\\" in text or ".." in text.split("/"):
        raise ValueError(f"invalid filename_prefix: {text!r}")
    return text


def _save(workflow: dict[str, Any], save_id: str, source_id: str, prefix: str, audio_format: str) -> None:
    save = _inputs(workflow, save_id)
    save["audio"] = [source_id, 0]
    save["filename_prefix"] = _filename_prefix(prefix, "voice/line")
    save["format"] = _text(audio_format, "audio_format")


def fill_clone(
    template: dict[str, Any],
    *,
    text: str,
    seed: int,
    reference_audio: str | None = None,
    engine: str = ENGINE_LOCAL,
    language: str = "zh",
    params: dict[str, Any] | None = None,
    filename_prefix: str | None = None,
    audio_format: str = "flac",
    local_model_path: str = "",
    codec_local_path: str = "",
) -> dict[str, Any]:
    """Voice Clone（有參考音）或一般 TTS（``reference_audio=None``）的 workflow。"""
    if not isinstance(template, dict):
        raise ValueError("template must be a dict (ComfyUI API format)")
    if engine not in (ENGINE_LOCAL, ENGINE_DELAY):
        raise ValueError(f"clone workflow does not support engine {engine!r}")
    if language not in LANGS:
        raise ValueError(f"language must be one of {', '.join(LANGS)}: {language!r}")
    clean_text = _text(text, "text")
    _check_seed(seed)
    load_id = _one(template, CLASS_LOAD_AUDIO, required=reference_audio is not None)
    generate_id = _one(template, CLASS_GENERATE)
    save_id = _one(template, CLASS_SAVE)
    loader_id = _one(template, CLASS_LOADER)

    workflow = copy.deepcopy(template)
    loader = _inputs(workflow, loader_id)
    loader["model_variant"] = MODEL_FOR_ENGINE[engine]
    if local_model_path:
        loader["local_model_path"] = _text(local_model_path, "local_model_path")
    if codec_local_path:
        loader["codec_local_path"] = _text(codec_local_path, "codec_local_path")

    values = _params(engine, params)
    generate = _inputs(workflow, generate_id)
    generate["language"] = language
    generate["text"] = clean_text
    generate["seed"] = seed
    generate.update(values)
    if values.get("max_new_tokens") is None:
        generate["max_new_tokens"] = estimate_max_tokens(clean_text)
    generate["enable_duration_control"] = False
    generate["duration_tokens"] = 325
    generate["head_handle"] = 0.05
    generate["tail_handle"] = 0.05
    if reference_audio is not None:
        name = _text(reference_audio, "reference_audio")
        _inputs(workflow, load_id)["audio"] = name
        generate["reference_audio"] = [load_id, 0]
    else:
        generate.pop("reference_audio", None)
        if load_id is not None:
            workflow.pop(load_id, None)

    _save(workflow, save_id, generate_id, filename_prefix or "voice/line", audio_format)
    return workflow


def fill_design(
    template: dict[str, Any],
    *,
    text: str,
    instruction: str,
    seed: int,
    language: str = "zh",
    params: dict[str, Any] | None = None,
    filename_prefix: str | None = None,
    audio_format: str = "flac",
    local_model_path: str = "",
    codec_local_path: str = "",
) -> dict[str, Any]:
    """Voice Design workflow（MOSS-VoiceGenerator）。"""
    if not isinstance(template, dict):
        raise ValueError("template must be a dict (ComfyUI API format)")
    if language not in LANGS:
        raise ValueError(f"language must be one of {', '.join(LANGS)}: {language!r}")
    clean_text = _text(text, "text")
    clean_instruction = _text(instruction, "instruction")
    _check_seed(seed)
    design_id = _one(template, CLASS_DESIGN)
    save_id = _one(template, CLASS_SAVE)
    loader_id = _one(template, CLASS_LOADER)

    workflow = copy.deepcopy(template)
    loader = _inputs(workflow, loader_id)
    loader["model_variant"] = MODEL_DESIGN
    if local_model_path:
        loader["local_model_path"] = _text(local_model_path, "local_model_path")
    if codec_local_path:
        loader["codec_local_path"] = _text(codec_local_path, "codec_local_path")

    design = _inputs(workflow, design_id)
    design["language"] = language
    design["text"] = clean_text
    design["instruction"] = clean_instruction
    design["seed"] = seed
    values = _params(ENGINE_DESIGN, params)
    design.update(values)
    if values.get("max_new_tokens") is None:
        design["max_new_tokens"] = estimate_max_tokens(clean_text)
    design["head_handle"] = 0.05
    design["tail_handle"] = 0.05

    _save(workflow, save_id, design_id, filename_prefix or "voice/design", audio_format)
    return workflow


def reference_filename(book_id: str, char_id: str, path: str | Path) -> str:
    """ComfyUI input 資料夾用的參考音檔名；同名不同內容不會互相覆蓋。"""
    source = Path(path)
    digest = hashlib.sha256(f"{book_id}/{char_id}".encode("utf-8")).hexdigest()[:8]
    safe_book = "".join(ch for ch in str(book_id) if ch.isalnum() or ch == "_") or "book"
    safe_char = "".join(ch for ch in str(char_id) if ch.isalnum() or ch == "_") or "char"
    return f"{safe_book}_{safe_char}_{digest}{source.suffix.lower() or '.wav'}"


def output_audio_paths(outputs: Any) -> list[dict[str, str]]:
    """從 ComfyUI 歷史紀錄的 ``outputs`` 取音檔（沿用 ``audio_workflow`` 的解析）。"""
    return output_files(outputs)
