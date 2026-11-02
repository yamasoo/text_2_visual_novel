"""場景與 BGM 清單、註冊表（設定集頁用）。

從所有場景檔彙整「已經用過的」地點 id 與 BGM id，加上使用者自己維護的註冊表
``work/bible.json``（中文名、備註、提示詞），再併入原型 ``style.json`` 的地點，
給網頁的「設定集」頁與編輯器的下拉選單使用。這個模組不改任何場景檔或 style.json。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCENES_DIR = "scenes"
GOLD_DIR = "gold"
STYLE_FILE = "prototype/style.json"
REGISTRY_FILE = "bible.json"
SCHEMA_VERSION = "0.1"
REGISTRY_TOP_KEYS = ("schema_version", "locations", "bgm")
SECTION_NAMES = ("locations", "bgm")
ENTRY_FIELDS = ("label", "note", "prompt")
SCENE_GLOB = "ch*.json"

ENTRY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
CHAPTER_ID_PATTERN = re.compile(r"^ch([0-9]+)$")
SOURCE_SCENES = "scenes"
SOURCE_REGISTRY = "registry"
SOURCE_STYLE = "style"
UTF8 = "utf-8"


# ------------------------------------------------------------------------ 讀檔


def _load_json(path: Path) -> Any:
    """讀 JSON；讀不到或不是合法 JSON 都回傳 None。"""
    try:
        return json.loads(path.read_text(encoding=UTF8))
    except (OSError, ValueError):
        return None


def _dump(doc: Any) -> str:
    """統一序列化格式：不跳脫中文、縮排 2、結尾換行。"""
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def _chapter_sort_key(chapter_id: str) -> tuple[int, str]:
    """章 id 的排序鍵：先比數字、再比字串。"""
    match = CHAPTER_ID_PATTERN.match(chapter_id)
    number = int(match.group(1)) if match is not None else 0
    return number, chapter_id


def _scene_files(work: Path) -> dict[str, Path]:
    """章 id → 場景檔路徑；同一章兩邊都有時只用 ``scenes/`` 的。"""
    found: dict[str, Path] = {}
    for folder_name in (SCENES_DIR, GOLD_DIR):
        folder = work / folder_name
        try:
            paths = sorted(folder.glob(SCENE_GLOB), key=lambda item: item.name)
        except OSError:
            continue
        for path in paths:
            if CHAPTER_ID_PATTERN.match(path.stem) is None:
                continue
            found.setdefault(path.stem, path)
    return found


# ---------------------------------------------------------------- 用過的 id 統計


def _record_use(
    table: dict[str, dict[str, Any]], raw_id: Any, chapter_id: str, scene_id: str
) -> None:
    """把一次使用記進統計表（保留第一次出現的順序）。"""
    if not isinstance(raw_id, str) or not raw_id:
        return
    entry = table.get(raw_id)
    if entry is None:
        entry = {"count": 0, "chapters": [], "scenes": []}
        table[raw_id] = entry
    entry["count"] += 1
    if chapter_id not in entry["chapters"]:
        entry["chapters"].append(chapter_id)
    entry["scenes"].append(scene_id)


def _rows(table: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """統計表 → 輸出用的清單（依第一次出現排序）。"""
    return [
        {
            "id": entry_id,
            "count": entry["count"],
            "chapters": list(entry["chapters"]),
            "scenes": list(entry["scenes"]),
        }
        for entry_id, entry in table.items()
    ]


def collect_ids(work: Path) -> dict[str, list[dict[str, Any]]]:
    """掃描場景檔，回傳 ``{"locations": [...], "bgm": [...]}``。

    每筆是 ``{"id", "count", "chapters", "scenes"}``：``count`` 是使用該 id 的場景數，
    ``chapters`` 是出現過的章 id（去重、依序），``scenes`` 是場景 id 清單（依序）。
    ``scenes/`` 與 ``gold/`` 同一章都有時只用 ``scenes/`` 的；章依檔名排序、場景依檔內
    順序、清單依第一次出現排序。壞檔與沒有 ``scenes`` 清單的檔案直接略過；
    ``bgm`` 為 null／缺少／空字串的場景不計入 BGM，``location_id`` 缺少的場景不計入地點。
    """
    locations: dict[str, dict[str, Any]] = {}
    bgm: dict[str, dict[str, Any]] = {}

    files = _scene_files(work)
    for chapter_id in sorted(files, key=_chapter_sort_key):
        data = _load_json(files[chapter_id])
        if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
            continue
        for scene in data["scenes"]:
            if not isinstance(scene, dict):
                continue
            raw_scene_id = scene.get("scene_id")
            scene_id = raw_scene_id if isinstance(raw_scene_id, str) else ""
            _record_use(locations, scene.get("location_id"), chapter_id, scene_id)
            _record_use(bgm, scene.get("bgm"), chapter_id, scene_id)

    return {"locations": _rows(locations), "bgm": _rows(bgm)}


# ------------------------------------------------------------------------ 註冊表


def _empty_registry() -> dict[str, Any]:
    """空註冊表。"""
    return {"schema_version": SCHEMA_VERSION, "locations": {}, "bgm": {}}


def _validate_entry(section: str, entry_id: Any, entry: Any) -> list[str]:
    """檢查註冊表的單筆（id 格式、欄位白名單、值型別）。"""
    errors: list[str] = []
    if not isinstance(entry_id, str) or ENTRY_ID_PATTERN.match(entry_id) is None:
        errors.append(f"{section}.{entry_id}：id 必須符合 [a-z][a-z0-9_]*")
    if not isinstance(entry, dict):
        errors.append(
            f"{section}.{entry_id}：必須是物件（現在是 {type(entry).__name__}）"
        )
        return errors
    for field, value in entry.items():
        if field not in ENTRY_FIELDS:
            errors.append(
                f"{section}.{entry_id}.{field}："
                f"不允許的欄位（只允許 {'／'.join(ENTRY_FIELDS)}）"
            )
        elif not isinstance(value, str):
            errors.append(
                f"{section}.{entry_id}.{field}：必須是字串（現在是 {value!r}）"
            )
    return errors


def validate_registry(doc: Any) -> list[str]:
    """回傳錯誤訊息清單（空＝通過）；每則訊息都會帶出問題的 id 或欄位名。

    規則：頂層必須是 dict；``schema_version`` 必須是 ``"0.1"``；頂層只允許
    ``schema_version``、``locations``、``bgm``；兩個清單若存在必須是 dict；每個 id
    要符合 ``[a-z][a-z0-9_]*``；每筆必須是 dict，只允許 ``label``、``note``、
    ``prompt``，且值必須是字串。
    """
    if not isinstance(doc, dict):
        return [f"{REGISTRY_FILE} 最外層必須是物件（現在是 {type(doc).__name__}）"]

    errors: list[str] = []
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version：必須是 {SCHEMA_VERSION!r}"
            f"（現在是 {doc.get('schema_version')!r}）"
        )
    for key in doc:
        if key not in REGISTRY_TOP_KEYS:
            errors.append(
                f"{key}：不允許的頂層欄位"
                f"（只允許 {'、'.join(REGISTRY_TOP_KEYS)}）"
            )
    for section in SECTION_NAMES:
        if section not in doc:
            continue
        table = doc[section]
        if not isinstance(table, dict):
            errors.append(
                f"{section}：必須是物件（現在是 {type(table).__name__}）"
            )
            continue
        for entry_id, entry in table.items():
            errors.extend(_validate_entry(section, entry_id, entry))
    return errors


def load_registry(work: Path) -> dict[str, Any]:
    """讀 ``work/bible.json``。

    檔案不存在 → ``{"schema_version": "0.1", "locations": {}, "bgm": {}}``；
    不是合法 JSON（或頂層不是物件）→ ``ValueError``。
    """
    path = work / REGISTRY_FILE
    if not path.is_file():
        return _empty_registry()
    try:
        text = path.read_text(encoding=UTF8)
    except OSError as exc:
        raise ValueError(f"讀不到 {REGISTRY_FILE}：{exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{REGISTRY_FILE} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{REGISTRY_FILE} 最外層必須是物件")
    return data


def save_registry(work: Path, doc: dict[str, Any]) -> None:
    """先 :func:`validate_registry`；有錯 → ``ValueError``（訊息用 ``"; "`` 接起來），且不動檔案。

    通過才寫 ``work/bible.json``（必要時建立資料夾）：JSON ``indent=2``、
    ``ensure_ascii=False``、結尾換行。
    """
    errors = validate_registry(doc)
    if errors:
        raise ValueError("; ".join(errors))
    path = work / REGISTRY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(doc), encoding=UTF8)


def _check_kind(kind: Any) -> str:
    """檢查 ``kind`` 只能是 locations／bgm。"""
    if kind not in SECTION_NAMES:
        raise ValueError(f"kind 必須是 {'／'.join(SECTION_NAMES)} 之一：{kind!r}")
    return kind


def _check_fields(fields: Any) -> dict[str, str]:
    """檢查要寫入的欄位：只允許 label／note／prompt，值必須是字串。"""
    if not isinstance(fields, dict):
        raise ValueError("fields 必須是 dict")
    for field, value in fields.items():
        if field not in ENTRY_FIELDS:
            raise ValueError(
                f"不允許的欄位：{field!r}（只允許 {'／'.join(ENTRY_FIELDS)}）"
            )
        if not isinstance(value, str):
            raise ValueError(f"{field} 必須是字串（現在是 {value!r}）")
    return dict(fields)


def set_entry(
    work: Path, kind: str, entry_id: str, fields: dict[str, str]
) -> dict[str, str]:
    """新增或合併註冊表的單筆，存檔後回傳該筆的最新內容。

    ``kind`` 只能是 ``"locations"`` 或 ``"bgm"``；``entry_id`` 要符合
    ``[a-z][a-z0-9_]*``；``fields`` 只允許 label／note／prompt 且值必須是字串，
    否則 ``ValueError`` 且不會留下檔案。新筆的預設值是 ``{"label": "", "note": ""}``；
    已有的筆只更新 ``fields`` 給的欄位。
    """
    _check_kind(kind)
    if not isinstance(entry_id, str) or ENTRY_ID_PATTERN.match(entry_id) is None:
        raise ValueError(f"id 不合法：{entry_id!r}（必須符合 [a-z][a-z0-9_]*)")
    clean_fields = _check_fields(fields)

    doc = load_registry(work)
    table = doc.get(kind)
    if not isinstance(table, dict):
        table = {}
        doc[kind] = table

    current = table.get(entry_id)
    entry: dict[str, str] = (
        {key: value for key, value in current.items()}
        if isinstance(current, dict) and current
        else {"label": "", "note": ""}
    )
    entry.update(clean_fields)
    table[entry_id] = entry
    save_registry(work, doc)
    return entry


def remove_entry(work: Path, kind: str, entry_id: str) -> bool:
    """刪除註冊表的單筆：有刪到回傳 True，本來就沒有回傳 False。

    ``kind`` 不合法 → ``ValueError``。不碰場景檔。
    """
    _check_kind(kind)
    doc = load_registry(work)
    table = doc.get(kind)
    if not isinstance(table, dict) or entry_id not in table:
        return False
    del table[entry_id]
    save_registry(work, doc)
    return True


# -------------------------------------------------------------------- 合併給網頁


def _style_locations(work: Path) -> dict[str, str]:
    """``style.json`` 的 locations → ``{id: label}``（label 可能為空字串）。

    檔案不存在或壞掉都當作沒有，不丟例外。
    """
    path = work / STYLE_FILE
    if not path.is_file():
        return {}
    data = _load_json(path)
    if not isinstance(data, dict):
        return {}
    locations = data.get("locations")
    if not isinstance(locations, dict):
        return {}

    labels: dict[str, str] = {}
    for place_id, value in locations.items():
        if not isinstance(place_id, str):
            continue
        label = value.get("label") if isinstance(value, dict) else None
        labels[place_id] = label if isinstance(label, str) else ""
    return labels


def _build_row(
    section: str,
    entry_id: str,
    used: dict[str, Any] | None,
    registry_table: dict[str, Any],
    style_labels: dict[str, str],
) -> dict[str, Any]:
    """組出設定集清單的一列。"""
    usage = used or {"count": 0, "chapters": [], "scenes": []}
    registry_entry = registry_table.get(entry_id)
    if not isinstance(registry_entry, dict):
        registry_entry = {}

    sources: list[str] = []
    if usage["count"] > 0:
        sources.append(SOURCE_SCENES)
    if entry_id in registry_table:
        sources.append(SOURCE_REGISTRY)
    if section == "locations" and entry_id in style_labels:
        sources.append(SOURCE_STYLE)

    label = registry_entry.get("label")
    if not isinstance(label, str) or not label:
        label = style_labels.get(entry_id, "") if section == "locations" else ""
    note = registry_entry.get("note")
    if not isinstance(note, str):
        note = ""

    return {
        "id": entry_id,
        "label": label,
        "note": note,
        "count": usage["count"],
        "chapters": list(usage["chapters"]),
        "scenes": list(usage["scenes"]),
        "used": usage["count"] > 0,
        "sources": sources,
    }


def _merge_section(
    section: str,
    used_rows: list[dict[str, Any]],
    registry_table: dict[str, Any],
    style_labels: dict[str, str],
) -> list[dict[str, Any]]:
    """場景用到的 → 只在註冊表的 → 只在 style.json 的（只有 locations 有第三類）。"""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for used in used_rows:
        entry_id = used["id"]
        rows.append(_build_row(section, entry_id, used, registry_table, style_labels))
        seen.add(entry_id)
    for entry_id in registry_table:
        if entry_id in seen:
            continue
        rows.append(_build_row(section, entry_id, None, registry_table, style_labels))
        seen.add(entry_id)
    if section == "locations":
        for entry_id in style_labels:
            if entry_id in seen:
                continue
            rows.append(
                _build_row(section, entry_id, None, registry_table, style_labels)
            )
            seen.add(entry_id)
    return rows


def merged_view(work: Path) -> dict[str, list[dict[str, Any]]]:
    """把場景用到的、註冊表的、style.json 的地點合併成給網頁用的清單。

    每筆是 ``{"id", "label", "note", "count", "chapters", "scenes", "used",
    "sources"}``。順序：場景裡用到的（依第一次出現）→ 只在註冊表的（依註冊表順序）
    → 只在 style.json 的（依檔內順序，只有 locations）。``label`` 以註冊表非空值優先，
    其次才是 ``style.json`` 的 label。``style.json`` 不存在或壞掉都當作沒有。
    """
    counts = collect_ids(work)
    registry = load_registry(work)
    style_labels = _style_locations(work)

    def table_of(section: str) -> dict[str, Any]:
        table = registry.get(section)
        return table if isinstance(table, dict) else {}

    return {
        "locations": _merge_section(
            "locations", counts["locations"], table_of("locations"), style_labels
        ),
        "bgm": _merge_section("bgm", counts["bgm"], table_of("bgm"), {}),
    }
