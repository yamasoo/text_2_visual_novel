"""素材清單：由場景算出全書需要的背景、立繪、音訊（S6）。

掃過所有章節的 scene graph，列出全書需要的素材寫成 ``manifest.json``，之後的出圖
工具依這份清單逐項製作。重新掃描時會保留既有的製作進度（狀態、提示詞、種子、授權等），
只更新「這次算出來的」欄位。

用法::

    python -m t2vn.asset_manifest --book-dir works/sample_book [--lang zh-TW]
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1"
KINDS = ("bg", "sprite", "headshot", "bgm", "ambience")
SPEC = {
    "bg": {"width": 1920, "height": 1080, "format": "png"},
    "sprite": {"width": 800, "height": 1600, "format": "png"},
    "headshot": {"width": 864, "height": 1152, "format": "png"},
}
TIME_LABELS = {
    "dawn": "清晨",
    "morning": "上午",
    "day": "白天",
    "dusk": "黃昏",
    "night": "夜晚",
}
KEEP_FIELDS = (
    "status",
    "prompt_en",
    "negative_prompt",
    "seed",
    "model",
    "loras",
    "license",
    "notes",
    "deprecated_ids",
)
ASSET_KIND_SPEC = {"bg": SPEC["bg"], "sprite": SPEC["sprite"], "headshot": SPEC["headshot"]}
FILE_FOLDERS = {"bgm": "bgm", "ambience": "ambience"}
ASSET_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
BOOK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
FILE_PATTERN = re.compile(r"^assets/[a-z0-9_]+/[a-z0-9_]+\.(png|ogg|opus)$")
SCENE_ID_PATTERN = re.compile(r"^ch[0-9]{3}_sc[0-9]{2}$")
CHAPTER_FILE_PATTERN = re.compile(r"^ch([0-9]+)$")
SCENE_FOLDER = "scenes"
GOLD_FOLDER = "gold"
CAST_FILE = "cast.json"
STYLE_FILE = "prototype/style.json"
MANIFEST_FILE = "manifest.json"
DEFAULT_LANG = "zh-TW"
NAME_SEPARATOR = "・"
SPRITE_NAME_TEMPLATE = "{name}（{expr}）"
UTF8 = "utf-8"


# ------------------------------------------------------------------------ 小工具


def _dump(data: Any) -> str:
    """統一序列化格式：不跳脫中文、縮排 2、結尾換行。"""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _table(value: Any) -> dict[str, Any]:
    """是 dict 就用，否則空 dict。"""
    return value if isinstance(value, dict) else {}


def _location_label(location_id: str, style: dict[str, Any]) -> str:
    """地點顯示名：style 有就用，否則用 id。"""
    entry = _table(style.get("locations")).get(location_id)
    label = entry.get("label") if isinstance(entry, dict) else None
    return label if isinstance(label, str) and label else location_id


def _time_label(time_of_day: str, style: dict[str, Any]) -> str:
    """時段顯示名：style 有就用，其次內建名稱，最後原字串。"""
    entry = _table(style.get("times")).get(time_of_day)
    label = entry.get("label") if isinstance(entry, dict) else None
    if isinstance(label, str) and label:
        return label
    return TIME_LABELS.get(time_of_day, time_of_day)


# 依角色重要度決定要出哪些表情圖：主角＝立繪與頭像都有各種表情；主要配角（main、antagonist）＝頭像有各種表情、立繪只有 neutral；
# 其餘（supporting、minor 等）＝立繪與頭像都只有 neutral。缺圖時匯出器退回 neutral。
SPRITE_EXPRESSION_ROLES = ("protagonist",)
HEADSHOT_EXPRESSION_ROLES = ("protagonist", "main", "antagonist")


def _role_of(characters: dict[str, Any], char_id: str) -> str:
    entry = characters.get(char_id)
    role = entry.get("role") if isinstance(entry, dict) else None
    return role if isinstance(role, str) else ""


def sprite_expr_for(role: str, expr: str) -> str:
    """這個角色的立繪要用哪個表情：主角保留原表情，其他一律 neutral。"""
    return expr if role in SPRITE_EXPRESSION_ROLES else "neutral"


def headshot_exprs_for(role: str, wanted: list[str]) -> list[str]:
    """這個角色的頭像要出哪些表情：主角與主要配角用外型卡勾選的，其他只有 neutral。"""
    return list(wanted) if role in HEADSHOT_EXPRESSION_ROLES else ["neutral"]


def _character_label(char_id: str, characters: dict[str, Any], lang: str) -> str:
    """角色顯示名：指定語言 → 第一個語言 → id。"""
    entry = characters.get(char_id)
    names = _table(entry.get("names")) if isinstance(entry, dict) else {}
    value = names.get(lang)
    if isinstance(value, str) and value:
        return value
    for candidate in names.values():
        if isinstance(candidate, str) and candidate:
            return candidate
    return char_id


def _add_used(item: dict[str, Any], scene_id: str) -> None:
    """記下這個素材被哪一場用到（去重、保留順序）。"""
    if not scene_id:
        return
    if scene_id not in item["used_in"]:
        item["used_in"].append(scene_id)


# ------------------------------------------------------------------ 需求蒐集


def collect_needs(
    scene_docs: list[dict[str, Any]],
    cast_doc: dict[str, Any],
    style: dict[str, Any] | None = None,
    lang: str = DEFAULT_LANG,
) -> list[dict[str, Any]]:
    """掃過所有場景，回傳需要的素材（依第一次出現的順序）。

    每項是 ``{asset_id, kind, names, file, spec?, used_in}``；背景名稱是
    「地點・時段」，立繪名稱是「角色（expr）」。立繪來源是場景的 ``cast``（expr 缺
    就是 neutral）與帶 ``expr`` 的 dialogue 行（speaker 不是 player）。BGM／環境音
    各一項、沒有 ``spec``。
    """
    style_table = _table(style)
    characters = _table(_table(cast_doc).get("characters"))
    needs: dict[str, dict[str, Any]] = {}

    def touch(asset_id: str, kind: str, file: str, name: str) -> dict[str, Any]:
        item = needs.get(asset_id)
        if item is None:
            item = {
                "asset_id": asset_id,
                "kind": kind,
                "names": {lang: name},
                "file": file,
            }
            if kind in ASSET_KIND_SPEC:
                item["spec"] = copy.deepcopy(ASSET_KIND_SPEC[kind])
            item["used_in"] = []
            needs[asset_id] = item
        return item

    def add_sprite(char_id: str, expr: str) -> None:
        expr = sprite_expr_for(_role_of(characters, char_id), expr)
        asset_id = f"sprite_{char_id}_{expr}"
        item = touch(
            asset_id,
            "sprite",
            f"assets/sprite/{char_id}_{expr}.png",
            SPRITE_NAME_TEMPLATE.format(
                name=_character_label(char_id, characters, lang), expr=expr
            ),
        )
        _add_used(item, current_scene_id[0])

    current_scene_id = [""]

    for doc in scene_docs or []:
        if not isinstance(doc, dict):
            continue
        scenes = doc.get("scenes")
        if not isinstance(scenes, list):
            continue
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            scene_id = scene.get("scene_id")
            current_scene_id[0] = scene_id if isinstance(scene_id, str) else ""

            bg = scene.get("bg")
            bg_id = bg.get("asset_id") if isinstance(bg, dict) else None
            if isinstance(bg_id, str) and bg_id:
                location_id = str(scene.get("location_id") or "")
                time_of_day = str(scene.get("time_of_day") or "")
                name = NAME_SEPARATOR.join(
                    (_location_label(location_id, style_table), _time_label(time_of_day, style_table))
                )
                item = touch(bg_id, "bg", f"assets/bg/{bg_id}.png", name)
                _add_used(item, current_scene_id[0])

            for entry in scene.get("cast") or []:
                if not isinstance(entry, dict):
                    continue
                char_id = entry.get("char_id")
                if not isinstance(char_id, str) or not char_id:
                    continue
                expr = entry.get("expr") or "neutral"
                add_sprite(char_id, str(expr))

            for line in scene.get("lines") or []:
                if not isinstance(line, dict) or line.get("type") != "dialogue":
                    continue
                expr = line.get("expr")
                speaker = line.get("speaker")
                if not isinstance(expr, str) or not expr:
                    continue
                if not isinstance(speaker, str) or not speaker or speaker == "player":
                    continue
                add_sprite(speaker, expr)

            for kind in ("bgm", "ambience"):
                value = scene.get(kind)
                if not isinstance(value, str) or not value:
                    continue
                item = touch(
                    value,
                    kind,
                    f"assets/{FILE_FOLDERS[kind]}/{value}.opus",
                    value,
                )
                _add_used(item, current_scene_id[0])

    return list(needs.values())


# ------------------------------------------------------------------ 合併與輸出


TITLE_BG_ID = "bg_title"
TITLE_BGM_ID = "bgm_title"
TITLE_LOCATION_ID = "title_screen"   # 標題圖的提示詞存在 art.locations.title_screen
TITLE_TIME_OF_DAY = "day"
TITLE_NAMES = {"zh-TW": ("標題圖", "標題音樂"), "en": ("Title screen", "Title music")}


def title_needs(lang: str = DEFAULT_LANG, taken_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """標題畫面的兩項素材：背景 ``bg_title``、音樂 ``bgm_title``（不屬於任何場景，``used_in`` 是空的）。

    格式同 :func:`collect_needs`；``taken_ids`` 已有的 id 不重複補。
    """
    bg_name, bgm_name = TITLE_NAMES.get(lang) or TITLE_NAMES[DEFAULT_LANG]
    taken = set(taken_ids or ())
    needs: list[dict[str, Any]] = []
    if TITLE_BG_ID not in taken:
        needs.append({"asset_id": TITLE_BG_ID, "kind": "bg", "names": {lang: bg_name},
                      "file": f"assets/bg/{TITLE_BG_ID}.png", "spec": copy.deepcopy(ASSET_KIND_SPEC["bg"]), "used_in": []})
    if TITLE_BGM_ID not in taken:
        needs.append({"asset_id": TITLE_BGM_ID, "kind": "bgm", "names": {lang: bgm_name},
                      "file": f"assets/{FILE_FOLDERS['bgm']}/{TITLE_BGM_ID}.opus", "used_in": []})
    return needs


def _validate_book_id(book_id: Any) -> str:
    """book_id 必須符合 schema 的 pattern。"""
    if not isinstance(book_id, str) or BOOK_ID_PATTERN.match(book_id) is None:
        raise ValueError(f"book_id 不合法：{book_id!r}")
    return book_id


def _validate_need(need: Any) -> None:
    """檢查一個「需求」項目能寫進合法的 manifest。"""
    if not isinstance(need, dict):
        raise ValueError("素材項目必須是物件")
    asset_id = need.get("asset_id")
    if not isinstance(asset_id, str) or ASSET_ID_PATTERN.match(asset_id) is None:
        raise ValueError(f"asset_id 不合法：{asset_id!r}")
    kind = need.get("kind")
    if kind not in KINDS:
        raise ValueError(f"{asset_id} 的 kind 不合法：{kind!r}")
    file = need.get("file")
    if not isinstance(file, str) or FILE_PATTERN.match(file) is None:
        raise ValueError(f"{asset_id} 的 file 不合法：{file!r}")
    names = need.get("names")
    if not isinstance(names, dict) or not names:
        raise ValueError(f"{asset_id} 缺少 names")
    used_in = need.get("used_in")
    if not isinstance(used_in, list):
        raise ValueError(f"{asset_id} 的 used_in 必須是清單")
    for scene_id in used_in:
        if not isinstance(scene_id, str) or SCENE_ID_PATTERN.match(scene_id) is None:
            raise ValueError(f"{asset_id} 的 used_in 有不合法場景 id：{scene_id!r}")


def _merge_item(need: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """把這次算出的需求與舊項目合併：這次算的優先，製作進度（KEEP_FIELDS）保留。"""
    item: dict[str, Any] = {
        "asset_id": need["asset_id"],
        "kind": need["kind"],
        "names": copy.deepcopy(need["names"]),
        "file": need["file"],
    }
    if "spec" in need:
        item["spec"] = copy.deepcopy(need["spec"])

    item["status"] = previous.get("status", "planned") if previous else "planned"
    item["license"] = previous.get("license", "") if previous else ""
    for field in KEEP_FIELDS:
        if field in ("status", "license"):
            continue
        if previous is not None and field in previous:
            item[field] = copy.deepcopy(previous[field])
    item["used_in"] = list(need["used_in"])
    return item


def build_manifest(
    book_id: str, needs: list[dict[str, Any]], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    """合併「需要的素材」與既有 manifest，回傳新的 manifest（不改傳入的物件）。

    順序：先照 ``needs`` 的順序；既有但這次沒用到的項目接在後面、``used_in`` 清成
    ``[]``（不刪，製作進度還在）。既有項目的 :data:`KEEP_FIELDS` 原樣保留，其餘欄位
    以這次算出的為準。新項目的 ``status`` 是 ``"planned"``、``license`` 是 ``""``，
    其他可選欄位不寫。``existing`` 的 ``book_id`` 不同 → ``ValueError``。
    """
    _validate_book_id(book_id)
    existing_assets: list[Any] = []
    if existing is not None:
        if not isinstance(existing, dict):
            raise ValueError("existing manifest 必須是物件")
        existing_book = existing.get("book_id")
        if existing_book is not None and existing_book != book_id:
            raise ValueError(
                f"existing manifest 的 book_id 不同：{existing_book!r} != {book_id!r}"
            )
        raw_assets = existing.get("assets")
        if raw_assets is not None and not isinstance(raw_assets, list):
            raise ValueError("existing manifest 的 assets 必須是清單")
        existing_assets = raw_assets or []

    previous: dict[str, dict[str, Any]] = {}
    for item in existing_assets:
        if not isinstance(item, dict):
            continue
        asset_id = item.get("asset_id")
        if isinstance(asset_id, str) and asset_id not in previous:
            previous[asset_id] = item

    assets: list[dict[str, Any]] = []
    used: set[str] = set()
    for need in needs or []:
        _validate_need(need)
        asset_id = need["asset_id"]
        if asset_id in used:
            continue
        used.add(asset_id)
        assets.append(_merge_item(need, previous.get(asset_id)))

    for item in existing_assets:
        if not isinstance(item, dict):
            continue
        asset_id = item.get("asset_id")
        if not isinstance(asset_id, str) or asset_id in used:
            continue
        used.add(asset_id)
        carried = copy.deepcopy(item)
        carried["used_in"] = []
        assets.append(carried)

    return {"schema_version": SCHEMA_VERSION, "book_id": book_id, "assets": assets}


# -------------------------------------------------------------------------- CLI


def _chapter_files(book_dir: Path) -> list[Path]:
    """章節檔：``scenes/chNNN.json`` 優先於 ``gold/``，依檔名排序。"""
    found: dict[str, Path] = {}
    for folder_name in (SCENE_FOLDER, GOLD_FOLDER):
        folder = book_dir / folder_name
        try:
            paths = sorted(folder.glob("ch*.json"), key=lambda item: item.name)
        except OSError:
            continue
        for path in paths:
            if CHAPTER_FILE_PATTERN.match(path.stem) is None:
                continue
            found.setdefault(path.stem, path)
    return [found[key] for key in sorted(found)]


def _load_json_file(path: Path, default: Any) -> Any:
    """讀 JSON；檔案不存在回傳 default，壞掉則丟 ValueError。"""
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding=UTF8))
    except OSError as exc:
        raise ValueError(f"讀不了 {path}：{exc}") from exc
    except ValueError as exc:
        raise ValueError(f"{path} 不是合法 JSON：{exc}") from exc


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.asset_manifest --book-dir works/sample_book [--lang zh-TW]``。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="asset_manifest.py",
        description="掃描章節的 scene graph，更新素材清單 manifest.json。",
    )
    parser.add_argument("--book-dir", required=True, help="作品資料夾（例如 works/sample_book）")
    parser.add_argument("--lang", default=DEFAULT_LANG, help="names 的語言標籤")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return 0 if code == 0 else 1

    book_dir = Path(args.book_dir)
    try:
        chapter_files = _chapter_files(book_dir)
        if not chapter_files:
            raise ValueError(f"{book_dir} 找不到任何章節檔（scenes/ 或 gold/ 下的 chNNN.json）")
        scene_docs = [_load_json_file(path, {}) for path in chapter_files]
        cast_doc = _load_json_file(book_dir / CAST_FILE, {})
        style = _load_json_file(book_dir / STYLE_FILE, None)
        existing = _load_json_file(book_dir / MANIFEST_FILE, None)

        book_id = cast_doc.get("book_id") if isinstance(cast_doc, dict) else None
        if not isinstance(book_id, str) or not book_id:
            book_id = book_dir.resolve().name

        needs = collect_needs(
            [doc for doc in scene_docs if isinstance(doc, dict)],
            cast_doc if isinstance(cast_doc, dict) else {},
            style if isinstance(style, dict) else None,
            lang=args.lang,
        )
        manifest = build_manifest(book_id, needs, existing if isinstance(existing, dict) else None)
        (book_dir / MANIFEST_FILE).write_text(_dump(manifest), encoding=UTF8)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    counts = {
        kind: sum(1 for item in manifest["assets"] if item.get("kind") == kind)
        for kind in KINDS
    }
    summary = ", ".join(f"{kind} {counts[kind]}" for kind in KINDS)
    print(f"ok: {len(manifest['assets'])} asset(s): {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
