"""音檔掃描登記（S7）：對照 manifest 的 bgm／ambience 項目，找出 ``assets/`` 底下實際有的音檔。

純函式，只讀資料夾、不寫任何檔案。呼叫端拿結果去更新 manifest 的狀態與授權。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

AUDIO_KINDS = ("bgm", "ambience")
FOLDERS = {"bgm": "bgm", "ambience": "ambience"}
EXTENSIONS = ("opus", "ogg", "mp3", "flac", "wav")
SIDECAR_SUFFIX = ".json"
UTF8 = "utf-8"


def _find_files(folder: Path, asset_id: str) -> list[Path]:
    """資料夾裡檔名是 ``<asset_id>.<副檔名>`` 的檔案（副檔名在 :data:`EXTENSIONS` 內），依優先序排列。"""
    found: list[Path] = []
    for extension in EXTENSIONS:
        path = folder / f"{asset_id}.{extension}"
        if path.is_file():
            found.append(path)
    return found


def read_sidecar(folder: Path, asset_id: str) -> dict[str, str]:
    """讀 ``<asset_id>.json``（旁註檔），回傳 ``{"license", "source_url"}``（缺的給空字串）。

    檔案不存在、壞掉或不是物件 → 兩個欄位都是空字串；欄位不是字串也當空字串。
    """
    path = folder / f"{asset_id}{SIDECAR_SUFFIX}"
    empty = {"license": "", "source_url": ""}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding=UTF8))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    result = {}
    for key in ("license", "source_url"):
        value = data.get(key)
        result[key] = value.strip() if isinstance(value, str) else ""
    return result


def scan_audio(
    assets_root: Path, assets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """掃描 manifest 裡 kind 為 bgm／ambience 的項目，依 manifest 順序回傳結果清單。

    每項：``{"asset_id", "kind", "present", "file", "extras", "license", "source_url"}``。
    ``file`` 是優先序最高的音檔（相對 ``assets_root`` 的 POSIX 路徑），沒有檔案是 ``None``；
    ``extras`` 是同一個 id 的其他音檔；``license``、``source_url`` 來自旁註檔。其他 kind 與
    缺少 ``asset_id`` 的項目略過。
    """
    results: list[dict[str, Any]] = []
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        kind = asset.get("kind")
        asset_id = asset.get("asset_id")
        if kind not in AUDIO_KINDS or not isinstance(asset_id, str) or not asset_id:
            continue
        folder = Path(assets_root) / FOLDERS[kind]
        files = _find_files(folder, asset_id)
        sidecar = read_sidecar(folder, asset_id)
        results.append(
            {
                "asset_id": asset_id,
                "kind": kind,
                "present": bool(files),
                "file": (
                    files[0].relative_to(assets_root).as_posix() if files else None
                ),
                "extras": [
                    path.relative_to(assets_root).as_posix() for path in files[1:]
                ],
                "license": sidecar["license"],
                "source_url": sidecar["source_url"],
            }
        )
    return results


def find_orphans(
    assets_root: Path, assets: list[dict[str, Any]]
) -> list[str]:
    """``bgm/``、``ambience/`` 底下有音檔、但檔名（不含副檔名）不是 manifest 該種類任何項目 id 的檔案。

    只看副檔名在 :data:`EXTENSIONS` 內的檔案（旁註 ``.json`` 不算）；回傳相對
    ``assets_root`` 的 POSIX 路徑，排序後的清單；資料夾不存在就略過。
    """
    known: dict[str, set[str]] = {kind: set() for kind in AUDIO_KINDS}
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        kind = asset.get("kind")
        asset_id = asset.get("asset_id")
        if kind in AUDIO_KINDS and isinstance(asset_id, str) and asset_id:
            known[kind].add(asset_id)

    orphans: list[str] = []
    for kind in AUDIO_KINDS:
        folder = Path(assets_root) / FOLDERS[kind]
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if not path.is_file() or path.suffix.lstrip(".").lower() not in EXTENSIONS:
                continue
            if path.stem in known[kind]:
                continue
            orphans.append(path.relative_to(assets_root).as_posix())
    return sorted(orphans)


def apply_scan(
    assets: list[dict[str, Any]], results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """依掃描結果回傳**新的** assets 清單（深拷貝，不改傳入的物件）。

    - 有音檔且 ``status`` 是 ``planned`` → ``generated``；沒有音檔且 ``status`` 是
      ``generated`` → ``planned``；其他狀態（``selected``／``processed``／``final``）不動。
    - ``license`` 目前是空字串（或缺少）且旁註有授權 → 填入；已有授權不覆蓋。
    - ``results`` 裡沒有的項目原樣保留。
    """
    updated = copy.deepcopy(assets)
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in results or []:
        if not isinstance(item, dict):
            continue
        asset_id = item.get("asset_id")
        kind = item.get("kind")
        if isinstance(asset_id, str) and isinstance(kind, str):
            index[(asset_id, kind)] = item

    for item in updated:
        if not isinstance(item, dict):
            continue
        result = index.get((item.get("asset_id"), item.get("kind")))
        if result is None:
            continue
        if result.get("present"):
            if item.get("status") == "planned":
                item["status"] = "generated"
        elif item.get("status") == "generated":
            item["status"] = "planned"
        if not item.get("license") and result.get("license"):
            item["license"] = result["license"]
    return updated
