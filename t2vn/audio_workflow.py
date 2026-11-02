"""YuE2 純配樂 workflow（ComfyUI API 格式）的填值與輸出檔解析（S7）。

``fill_workflow`` 把樣板（``workflows/yue2_bgm_api.json``）複製一份並填入曲風、歌詞、種子、
長度等；``output_files`` 從 ComfyUI 歷史紀錄的 ``outputs`` 取出音檔位置。兩者都是純函式，
不連 ComfyUI、不讀寫檔案。
"""
from __future__ import annotations

import copy
from typing import Any

CLASS_CHECKPOINT = "CheckpointLoaderSimple"
CLASS_MUSIC = "YuE2GenerateMusic"
CLASS_ABC = "YuE2GenerateABC"
CLASS_SAMPLER = "KSampler"
CLASS_SAVE = "SaveAudioAdvanced"
MIN_SECONDS = 10
MAX_SECONDS = 600
MAX_SEED = 2**63 - 1


def _find_nodes(workflow: dict[str, Any], class_type: str) -> list[str]:
    """找出 ``class_type`` 相同的節點 id（依 workflow 的順序）。"""
    if not isinstance(workflow, dict):
        return []
    return [
        node_id
        for node_id, node in workflow.items()
        if isinstance(node, dict) and node.get("class_type") == class_type
    ]


def _one(
    workflow: dict[str, Any], class_type: str, required: bool = True
) -> str | None:
    """找出唯一的節點 id；缺少（且 ``required``）或重複 → ``ValueError``。"""
    nodes = _find_nodes(workflow, class_type)
    if not nodes:
        if required:
            raise ValueError(f"樣板缺少 {class_type} 節點")
        return None
    if len(nodes) > 1:
        raise ValueError(f"{class_type} 節點不只一個：{', '.join(map(str, nodes))}")
    return nodes[0]


def _text(value: Any, name: str) -> str:
    """文字參數：必須是非空字串，回傳去頭尾空白的版本。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必須是非空字串")
    return value.strip()


def _inputs(workflow: dict[str, Any], node_id: str) -> dict[str, Any]:
    """取節點的 ``inputs``（不是 dict 就換成新的）。"""
    node = workflow[node_id]
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
        node["inputs"] = inputs
    return inputs


def fill_workflow(
    template: dict[str, Any],
    *,
    style: str,
    lyrics: str,
    seed: int,
    max_duration: float = 120,
    ckpt_name: str | None = None,
    filename_prefix: str | None = None,
    abc_planning: bool = True,
    audio_format: str | None = None,
    quality: str | None = None,
) -> dict[str, Any]:
    """回傳填好值的新 workflow（不改傳入的 ``template``）。

    - 曲風、歌詞、種子寫進 ``YuE2GenerateMusic``；``abc_planning`` 為真時同步寫進
      ``YuE2GenerateABC`` 並讓音樂節點的 ``abc`` 連到它；為假時移除 ABC 節點、``abc``
      設成 ``""``。
    - ``KSampler.seed`` 同樣用 ``seed``；``max_duration`` 寫進音樂節點（浮點數）。
    - ``ckpt_name``、``filename_prefix``、``audio_format``（``format``）、``quality``
      （``format.quality``）有給才改。
    - 樣板缺少必要節點或同類節點不只一個、參數不合法 → ``ValueError``。
    """
    if not isinstance(template, dict):
        raise ValueError("樣板必須是 dict（ComfyUI API 格式）")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed 必須是 0 到 {MAX_SEED} 的整數：{seed!r}")
    if not 0 <= seed <= MAX_SEED:
        raise ValueError(f"seed 必須是 0 到 {MAX_SEED} 的整數：{seed!r}")
    if isinstance(max_duration, bool) or not isinstance(max_duration, (int, float)):
        raise ValueError(f"max_duration 必須是數字：{max_duration!r}")
    if not MIN_SECONDS <= max_duration <= MAX_SECONDS:
        raise ValueError(
            f"max_duration 必須在 {MIN_SECONDS} 到 {MAX_SECONDS} 秒之間：{max_duration!r}"
        )
    clean_style = _text(style, "style")
    clean_lyrics = _text(lyrics, "lyrics")

    checkpoint_id = _one(template, CLASS_CHECKPOINT)
    music_id = _one(template, CLASS_MUSIC)
    sampler_id = _one(template, CLASS_SAMPLER)
    save_id = _one(template, CLASS_SAVE)
    abc_id = _one(template, CLASS_ABC, required=bool(abc_planning))

    workflow = copy.deepcopy(template)
    music = _inputs(workflow, music_id)
    music["style"] = clean_style
    music["lyrics"] = clean_lyrics
    music["seed"] = seed
    music["max_duration"] = float(max_duration)

    if abc_planning and abc_id is not None:
        music["abc"] = [abc_id, 0]
        abc = _inputs(workflow, abc_id)
        abc["style"] = clean_style
        abc["lyrics"] = clean_lyrics
        abc["seed"] = seed
    else:
        music["abc"] = ""
        if abc_id is not None:
            workflow.pop(abc_id, None)

    _inputs(workflow, sampler_id)["seed"] = seed

    if ckpt_name is not None:
        _inputs(workflow, checkpoint_id)["ckpt_name"] = _text(ckpt_name, "ckpt_name")

    save = _inputs(workflow, save_id)
    if filename_prefix is not None:
        save["filename_prefix"] = _text(filename_prefix, "filename_prefix")
    if audio_format is not None:
        save["format"] = _text(audio_format, "audio_format")
    if quality is not None:
        save["format.quality"] = _text(quality, "quality")

    return workflow


def output_files(outputs: Any) -> list[dict[str, str]]:
    """從 ComfyUI 歷史紀錄的 ``outputs`` 取出音檔。

    回傳 ``[{"node_id", "filename", "subfolder", "type"}]``，節點依 id 字串排序、檔案依
    原順序；``subfolder`` 缺少（或不是字串）當 ``""``、``type`` 缺少（或空字串）當
    ``"output"``；沒有 ``filename`` 的項目與非 dict／非 list 的內容略過；``outputs`` 不是
    dict 回傳空清單。
    """
    if not isinstance(outputs, dict):
        return []

    files: list[dict[str, str]] = []
    for node_id in sorted(outputs, key=lambda key: str(key)):
        node = outputs[node_id]
        if not isinstance(node, dict):
            continue
        audio = node.get("audio")
        if not isinstance(audio, list):
            continue
        for item in audio:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            if not isinstance(filename, str) or not filename:
                continue
            subfolder = item.get("subfolder")
            kind = item.get("type")
            files.append(
                {
                    "node_id": str(node_id),
                    "filename": filename,
                    "subfolder": subfolder if isinstance(subfolder, str) else "",
                    "type": kind if isinstance(kind, str) and kind else "output",
                }
            )
    return files
