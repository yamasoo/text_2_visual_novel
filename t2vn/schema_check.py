"""用 schema/*.schema.json 驗證輸出資料，另外做不需要跨檔案的連結檢查。

對應 docs/05_資料格式規格.md 第 7 節：
- V1 JSON Schema：``validate_data`` / ``validate_file``
- V2 角色存在、V6 跳轉可達（單一檔案範圍）：``check_scene_links``
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from .errors import SchemaCheckError, T2vnError

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "schema"

SCHEMA_FILES = {
    "scene": "scene.schema.json",
    "cast": "cast.schema.json",
    "manifest": "manifest.schema.json",
    "chapter": "chapter.schema.json",
    "chapter_index": "chapter_index.schema.json",
    "outline": "outline.schema.json",
}

PLAYER_ID = "player"
_CHAPTER_FILE_RE = re.compile(r"^ch[0-9]{3}\.json$")
_SCENE_CHAPTER_RE = re.compile(r"^(ch[0-9]{3})_sc[0-9]{2}$")


@dataclass(frozen=True)
class Issue:
    """一個驗證問題。``path`` 是 JSON Pointer（根為空字串），``message`` 是說明。"""

    path: str
    message: str


# ---------------------------------------------------------------- schema 驗證


def load_schema(kind: str, schema_dir: str | Path | None = None) -> dict[str, Any]:
    """載入某種資料的 schema；未知種類或檔案缺漏丟 SchemaCheckError。"""
    if kind not in SCHEMA_FILES:
        raise SchemaCheckError(f"unknown schema kind {kind!r}; choose from: {', '.join(sorted(SCHEMA_FILES))}")
    path = Path(schema_dir) / SCHEMA_FILES[kind] if schema_dir is not None else SCHEMA_DIR / SCHEMA_FILES[kind]
    if not path.is_file():
        raise SchemaCheckError(f"schema file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaCheckError(f"schema file is not valid JSON: {path}: {exc}") from exc


def validate_data(data: Any, kind: str, schema_dir: str | Path | None = None) -> list[Issue]:
    """驗證一份已解析的資料，回傳依路徑排序的問題清單（空清單＝通過）。"""
    validator = Draft202012Validator(load_schema(kind, schema_dir))
    issues: list[Issue] = []
    for error in validator.iter_errors(data):
        # oneOf / anyOf 的錯誤訊息很籠統，改挑最相關的子錯誤。
        shown = best_match(error.context) if error.context else error
        pointer = _pointer(shown.absolute_path)
        issues.append(Issue(pointer, shown.message))
    return sorted(issues, key=lambda issue: (issue.path, issue.message))


def guess_kind(path: str | Path) -> str | None:
    """依檔案位置猜資料種類：scenes/*.json、chapters/*.json、cast.json、manifest.json。"""
    p = Path(path)
    if p.name == "cast.json":
        return "cast"
    if p.name == "manifest.json":
        return "manifest"
    if p.parent.name == "scenes" and p.suffix == ".json":
        return "scene"
    if p.parent.name == "outlines" and p.suffix == ".json":
        return "outline"
    if p.parent.name == "chapters":
        if p.name == "index.json":
            return "chapter_index"
        if _CHAPTER_FILE_RE.match(p.name):
            return "chapter"
    return None


def validate_file(path: str | Path, kind: str | None = None, schema_dir: str | Path | None = None) -> list[Issue]:
    """驗證一個 JSON 檔。``kind`` 省略時依位置猜；猜不到丟 SchemaCheckError。"""
    p = Path(path)
    resolved_kind = kind or guess_kind(p)
    if resolved_kind is None:
        raise SchemaCheckError(f"cannot tell which schema applies to {p}; pass kind explicitly")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [Issue("", f"invalid JSON: {exc}")]
    return validate_data(data, resolved_kind, schema_dir)


# ---------------------------------------------------------------- 單檔連結檢查


def check_scene_links(scene_doc: dict[str, Any], cast_doc: dict[str, Any] | None = None) -> list[Issue]:
    """檢查 scene 檔內部的連結（假設已通過 schema 驗證）。

    - scene_id 不可重複。
    - ``next`` 與選項的 ``goto``：指向本檔的場景、``end_`` 結局節點，或其他章的場景（無法在單檔內驗證，放行）。
    - 有提供 ``cast_doc`` 時，``speaker`` 與 ``cast[].char_id`` 必須在角色表內（``player`` 例外）。
    """
    issues: list[Issue] = []
    chapter_id = scene_doc.get("chapter_id", "")
    scenes = scene_doc.get("scenes", [])

    seen: dict[str, int] = {}
    for index, scene in enumerate(scenes):
        scene_id = scene.get("scene_id")
        if scene_id in seen:
            issues.append(Issue(f"/scenes/{index}/scene_id", f"duplicate scene_id {scene_id!r} (first at index {seen[scene_id]})"))
        else:
            seen[scene_id] = index
    local_ids = set(seen)

    known_chars: set[str] | None = None
    if cast_doc is not None:
        known_chars = set(cast_doc.get("characters", {})) | {PLAYER_ID}

    def check_target(target: Any, pointer: str) -> None:
        if target is None or target in local_ids or str(target).startswith("end_"):
            return
        match = _SCENE_CHAPTER_RE.match(str(target))
        if match and match.group(1) != chapter_id:
            return
        issues.append(Issue(pointer, f"unknown target {target!r}"))

    def check_char(char_id: Any, pointer: str) -> None:
        if known_chars is not None and char_id not in known_chars:
            issues.append(Issue(pointer, f"unknown character {char_id!r}"))

    for s_index, scene in enumerate(scenes):
        base = f"/scenes/{s_index}"
        check_target(scene.get("next"), f"{base}/next")
        for c_index, member in enumerate(scene.get("cast", [])):
            check_char(member.get("char_id"), f"{base}/cast/{c_index}/char_id")
        for l_index, line in enumerate(scene.get("lines", [])):
            line_base = f"{base}/lines/{l_index}"
            if line.get("type") == "dialogue":
                check_char(line.get("speaker"), f"{line_base}/speaker")
            elif line.get("type") == "choice":
                for o_index, option in enumerate(line.get("options", [])):
                    check_target(option.get("goto"), f"{line_base}/options/{o_index}/goto")
    return sorted(issues, key=lambda issue: (issue.path, issue.message))


# ---------------------------------------------------------------- 內部工具


def _pointer(parts: Any) -> str:
    items = [str(part) for part in parts]
    return "/" + "/".join(items) if items else ""


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.schema_check FILE... [--kind K] [--cast cast.json]``。"""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Validate generated JSON files against schema/*.schema.json.")
    parser.add_argument("files", nargs="+")
    parser.add_argument("--kind", choices=sorted(SCHEMA_FILES), default=None)
    parser.add_argument("--cast", default=None, help="cast.json; enables character and link checks for scene files")
    args = parser.parse_args(argv)

    try:
        cast_doc = json.loads(Path(args.cast).read_text(encoding="utf-8")) if args.cast else None
        total = 0
        for name in args.files:
            kind = args.kind or guess_kind(name)
            issues = validate_file(name, kind)
            if kind == "scene" and not issues:
                issues = check_scene_links(json.loads(Path(name).read_text(encoding="utf-8")), cast_doc)
            for issue in issues:
                print(f"{name}: {issue.path or '/'}: {issue.message}")
            total += len(issues)
            if not issues:
                print(f"{name}: ok")
    except (T2vnError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
