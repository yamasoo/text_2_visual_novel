"""一本書的進度與統計（總覽頁用）。

讀作品資料夾 ``works/<book>/`` 的檔案，算出「幾章、幾章已標註、幾章已校對、
共幾場、幾句台詞、誰說最多話」，一次回傳全書統計與逐章列表。

- ``chapters/index.json``：章節索引（沒有或壞掉 → 全零的結果）。
- ``scenes/<chapter_id>.json`` 優先，找不到才用 ``gold/<chapter_id>.json``。
- ``review_state.json``：每章的校對狀態（只認 draft／reviewed）。

這個模組不改任何檔案，也不驗證 schema：壞檔一律當作沒有。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

INDEX_DIR = "chapters"
INDEX_FILE = "index.json"
SCENES_DIR = "scenes"
GOLD_DIR = "gold"
REVIEW_STATE_FILE = "review_state.json"
STATUS_NONE = "none"
STATUS_DRAFT = "draft"
STATUS_REVIEWED = "reviewed"
KNOWN_STATUSES = (STATUS_DRAFT, STATUS_REVIEWED)
LINE_TYPES = ("narration", "dialogue", "direction")
UTF8 = "utf-8"


def _load_json(path: Path) -> Any:
    """讀 JSON；讀不到或不是合法 JSON 都回傳 None。"""
    try:
        return json.loads(path.read_text(encoding=UTF8))
    except (OSError, ValueError):
        return None


def _as_int(value: Any) -> int:
    """把值當整數用；bool 或轉不動的值一律當 0（壞資料不讓統計爆掉）。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return 0
    return 0


def _is_safe_name(name: Any) -> bool:
    """檔名用的字串不含路徑分隔或上層參照。"""
    if not isinstance(name, str) or not name:
        return False
    if "/" in name or "\\" in name or name in {".", ".."}:
        return False
    return ".." not in name


def _empty_overview() -> dict[str, Any]:
    """全零的結果（索引不存在或壞掉時用）。"""
    return {
        "chapters": 0,
        "chars": 0,
        "status_counts": {STATUS_NONE: 0, STATUS_DRAFT: 0, STATUS_REVIEWED: 0},
        "scenes": 0,
        "lines": 0,
        "narration": 0,
        "dialogue": 0,
        "direction": 0,
        "speakers": [],
        "per_chapter": [],
    }


def _load_scene_scenes(work: Path, chapter_id: str) -> tuple[list[Any], str] | None:
    """找這章的場景清單，回傳 (scenes, 來源資料夾)；沒有有效檔案回傳 None。

    ``scenes/`` 優先；只要 ``scenes/`` 有這個檔案就以它為準（壞掉也不再退回 ``gold/``）。
    """
    if not _is_safe_name(chapter_id):
        return None
    for folder_name in (SCENES_DIR, GOLD_DIR):
        path = work / folder_name / f"{chapter_id}.json"
        if not path.is_file():
            continue
        data = _load_json(path)
        if isinstance(data, dict) and isinstance(data.get("scenes"), list):
            return data["scenes"], folder_name
        return None
    return None


def _count_lines(scenes: list[Any]) -> tuple[int, dict[str, int], dict[str, int]]:
    """回傳 (總行數、各 type 的行數、每個說話者的台詞數)。"""
    total = 0
    by_type = {name: 0 for name in LINE_TYPES}
    speakers: dict[str, int] = {}
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        raw_lines = scene.get("lines")
        if not isinstance(raw_lines, list):
            continue
        for item in raw_lines:
            total += 1
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind not in by_type:
                continue
            by_type[kind] += 1
            if kind == "dialogue":
                speaker = item.get("speaker")
                if isinstance(speaker, str) and speaker:
                    speakers[speaker] = speakers.get(speaker, 0) + 1
    return total, by_type, speakers


def _status_for(source: str | None, state_status: Any) -> str:
    """依場景來源與 review_state 決定章節狀態。"""
    if source is None:
        return STATUS_NONE
    if state_status in KNOWN_STATUSES:
        return state_status
    return STATUS_REVIEWED if source == GOLD_DIR else STATUS_DRAFT


def _state_statuses(work: Path) -> dict[str, Any]:
    """讀 review_state.json 的 ``chapters`` 對照表（壞掉就是空表）。"""
    data = _load_json(work / REVIEW_STATE_FILE)
    if not isinstance(data, dict):
        return {}
    chapters = data.get("chapters")
    return chapters if isinstance(chapters, dict) else {}


def _index_chapters(work: Path) -> list[Any] | None:
    """讀章節索引的 ``chapters`` 清單；沒有或格式不對回傳 None。"""
    data = _load_json(work / INDEX_DIR / INDEX_FILE)
    if not isinstance(data, dict):
        return None
    chapters = data.get("chapters")
    return chapters if isinstance(chapters, list) else None


def book_overview(work: Path) -> dict[str, Any]:
    """讀 ``work`` 底下的檔案並回傳統計（總覽頁要的完整結果）。

    任何檔案不存在或壞掉都不丟例外，當作沒有；索引不存在時回傳全零的結果。
    """
    index_chapters = _index_chapters(work)
    if index_chapters is None:
        return _empty_overview()

    state = _state_statuses(work)
    overview = _empty_overview()
    speaker_totals: dict[str, int] = {}

    for entry in index_chapters:
        if not isinstance(entry, dict):
            continue
        chapter_id = entry.get("chapter_id")
        if not isinstance(chapter_id, str) or not chapter_id:
            continue
        char_count = _as_int(entry.get("char_count"))
        found = _load_scene_scenes(work, chapter_id)
        source = found[1] if found is not None else None
        scenes = found[0] if found is not None else []

        state_entry = state.get(chapter_id)
        state_status = state_entry.get("status") if isinstance(state_entry, dict) else None
        status = _status_for(source, state_status)

        total, by_type, speakers = _count_lines(scenes)
        for speaker, count in speakers.items():
            speaker_totals[speaker] = speaker_totals.get(speaker, 0) + count

        overview["chapters"] += 1
        overview["chars"] += char_count
        overview["status_counts"][status] += 1
        overview["scenes"] += len(scenes)
        overview["lines"] += total
        overview["narration"] += by_type["narration"]
        overview["dialogue"] += by_type["dialogue"]
        overview["direction"] += by_type["direction"]
        overview["per_chapter"].append(
            {
                "chapter_id": chapter_id,
                "chapter_no": entry.get("chapter_no"),
                "title": entry.get("title", ""),
                "char_count": char_count,
                "status": status,
                "scenes": len(scenes),
                "lines": total,
                "dialogue": by_type["dialogue"],
                "narration": by_type["narration"],
            }
        )

    overview["speakers"] = [
        {"id": speaker, "lines": count}
        for speaker, count in sorted(
            speaker_totals.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return overview
