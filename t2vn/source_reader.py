"""唯讀讀取 novel-studio 格式的書資料（章綱、角色卡、名詞表）。

規則：
- 程式裡沒有中文欄位名；所有中文 key／標題／enum 值都來自 ``config/source_field_map.json``。
- frontmatter 出現沒有對應的 key、章節標題或 enum 值時一律丟 SourceError，不默默略過。
- 這個模組只讀不寫。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SourceError, T2vnError

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIELD_MAP = REPO_ROOT / "config" / "source_field_map.json"

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)\s*$")


# ---------------------------------------------------------------- 欄位對照


def load_field_map(path: str | Path | None = None) -> dict[str, Any]:
    """載入欄位對照表。"""
    p = Path(path) if path is not None else DEFAULT_FIELD_MAP
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SourceError(f"cannot read field map {p}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"field map is not valid JSON: {p}: {exc}") from exc


# ---------------------------------------------------------------- 資料類別


@dataclass(frozen=True)
class ChapterOutline:
    chapter_no: int
    title: str
    volume_title: str
    arc_title: str
    status: str
    sections: dict[str, str]  # 英文 section key -> 原文內容
    cast_names: list[str]  # 出場角色（原文名稱）
    source_file: str


@dataclass(frozen=True)
class CharacterCard:
    name: str
    aliases: list[str]
    role: str  # 英文 enum
    first_appearance: int | None
    status: str
    sections: dict[str, str]
    source_file: str


@dataclass(frozen=True)
class GlossaryTerm:
    term: str
    category: str  # 英文 enum
    description: str
    note: str
    first_appearance: int | None


@dataclass(frozen=True)
class Glossary:
    terms: list[GlossaryTerm] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)  # 重複出現的名詞（原文）


# ---------------------------------------------------------------- 低階解析


def split_frontmatter(text: str, where: str) -> tuple[dict[str, str], str]:
    """切出 frontmatter（單層 ``key: value``）與正文。沒有 frontmatter 丟 SourceError。"""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise SourceError(f"{where}: missing frontmatter")
    meta: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1 :])
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise SourceError(f"{where}: malformed frontmatter line {index + 1}")
        meta[key.strip()] = value.strip()
    raise SourceError(f"{where}: frontmatter is not closed")


def map_frontmatter(meta: dict[str, str], key_map: dict[str, str], where: str) -> dict[str, str]:
    """把來源 key 轉成英文 key；沒有對應的 key 丟 SourceError。"""
    result: dict[str, str] = {}
    for key, value in meta.items():
        if key not in key_map:
            raise SourceError(f"{where}: unmapped frontmatter key {key!r}; add it to source_field_map.json")
        result[key_map[key]] = value
    return result


def split_sections(body: str, heading_map: dict[str, str], where: str) -> dict[str, str]:
    """依 ``##`` 標題切段，標題轉成英文 key。``#`` 一級標題與標題之前的內容忽略。"""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is not None:
            sections[current] = "\n".join(buffer).strip()

    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            title = match.group(1)
            if title not in heading_map:
                raise SourceError(f"{where}: unmapped section heading {title!r}; add it to source_field_map.json")
            current = heading_map[title]
            if current in sections:
                raise SourceError(f"{where}: duplicate section {title!r}")
            buffer = []
        elif current is not None:
            buffer.append(line)
    flush()
    return sections


def bullet_items(text: str) -> list[str]:
    """取出清單項目（``-``、``*`` 或 ``1.``）的內容。"""
    items = []
    for line in text.splitlines():
        match = _BULLET_RE.match(line)
        if match:
            items.append(match.group(1))
    return items


def parse_list_value(value: str) -> list[str]:
    """解析 frontmatter 的清單值：``[]``、``[a, b]`` 或單一字串。"""
    value = value.strip()
    if value in ("", "[]"):
        return []
    if value.startswith("[") and value.endswith("]"):
        return [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
    return [value]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceError(f"cannot read {path}: {exc}") from exc


def _chapter_no(raw: str, where: str) -> int:
    if not raw.isdigit():
        raise SourceError(f"{where}: chapter number must be digits, got {raw!r}")
    return int(raw)


def _enum(value: str, mapping: dict[str, str], what: str, where: str) -> str:
    if value not in mapping:
        raise SourceError(f"{where}: unmapped {what} value {value!r}; add it to source_field_map.json")
    return mapping[value]


def _check_doc_type(meta: dict[str, str], expected: str, field_map: dict[str, Any], where: str) -> None:
    doc_type = _enum(meta.get("doc_type", ""), field_map["doc_type_values"], "doc_type", where)
    if doc_type != expected:
        raise SourceError(f"{where}: expected doc_type {expected!r}, got {doc_type!r}")


# ---------------------------------------------------------------- 章綱


def read_chapter_outline(path: str | Path, field_map: dict[str, Any] | None = None) -> ChapterOutline:
    """讀一份章綱。"""
    fm = field_map or load_field_map()
    p = Path(path)
    where = p.name
    meta_raw, body = split_frontmatter(_read(p), where)
    meta = map_frontmatter(meta_raw, fm["chapter_outline"], where)
    _check_doc_type(meta, "chapter_outline", fm, where)
    sections = split_sections(body, fm["outline_sections"], where)
    return ChapterOutline(
        chapter_no=_chapter_no(meta.get("chapter_no", ""), where),
        title=meta.get("chapter_title", ""),
        volume_title=meta.get("volume_title", ""),
        arc_title=meta.get("arc_title", ""),
        status=_enum(meta.get("status", ""), fm["status_values"], "status", where),
        sections=sections,
        cast_names=bullet_items(sections.get("cast_names", "")),
        source_file=p.name,
    )


def read_all_outlines(book_dir: str | Path, field_map: dict[str, Any] | None = None) -> list[ChapterOutline]:
    """讀全部章綱，依章號排序。檔名不符 ``chapter_outline_file_pattern`` 的檔案忽略。"""
    fm = field_map or load_field_map()
    directory = Path(book_dir) / fm["source_paths"]["chapter_outline_dir"]
    if not directory.is_dir():
        raise SourceError(f"chapter outline folder not found: {directory}")
    pattern = re.compile(fm["chapter_outline_file_pattern"])
    outlines = [read_chapter_outline(p, fm) for p in sorted(directory.iterdir()) if p.is_file() and pattern.match(p.name)]
    seen: dict[int, str] = {}
    for outline in outlines:
        if outline.chapter_no in seen:
            raise SourceError(f"duplicate chapter number {outline.chapter_no}: {seen[outline.chapter_no]} and {outline.source_file}")
        seen[outline.chapter_no] = outline.source_file
    return sorted(outlines, key=lambda o: o.chapter_no)


# ---------------------------------------------------------------- 角色卡


def read_character(path: str | Path, field_map: dict[str, Any] | None = None) -> CharacterCard:
    """讀一張角色卡。"""
    fm = field_map or load_field_map()
    p = Path(path)
    where = p.name
    meta_raw, body = split_frontmatter(_read(p), where)
    meta = map_frontmatter(meta_raw, fm["character_card"], where)
    _check_doc_type(meta, "character_card", fm, where)
    first = meta.get("first_appearance", "")
    first_no: int | None = None
    if first:
        match = re.match(fm["first_appearance_pattern"], first)
        if not match:
            raise SourceError(f"{where}: unrecognized first appearance {first!r}")
        first_no = int(match.group(1))
    return CharacterCard(
        name=meta.get("name", ""),
        aliases=parse_list_value(meta.get("aliases", "")),
        role=_enum(meta.get("role", ""), fm["role_values"], "role", where),
        first_appearance=first_no,
        status=_enum(meta.get("status", ""), fm["status_values"], "status", where),
        sections=split_sections(body, fm["character_sections"], where),
        source_file=p.name,
    )


def read_characters(book_dir: str | Path, field_map: dict[str, Any] | None = None) -> list[CharacterCard]:
    """讀全部角色卡，依檔名排序。名稱重複丟 SourceError。"""
    fm = field_map or load_field_map()
    directory = Path(book_dir) / fm["source_paths"]["character_dir"]
    if not directory.is_dir():
        raise SourceError(f"character folder not found: {directory}")
    cards = [read_character(p, fm) for p in sorted(directory.glob("*.md"))]
    seen: set[str] = set()
    for card in cards:
        if card.name in seen:
            raise SourceError(f"duplicate character name {card.name!r}")
        seen.add(card.name)
    return cards


# ---------------------------------------------------------------- 名詞表


def read_glossary(book_dir: str | Path, field_map: dict[str, Any] | None = None) -> Glossary:
    """讀名詞表（Markdown 表格）。重複名詞不丟錯，列在 ``duplicates``，保留第一筆。"""
    fm = field_map or load_field_map()
    path = Path(book_dir) / fm["source_paths"]["glossary_file"]
    where = path.name
    meta_raw, body = split_frontmatter(_read(path), where)
    meta = map_frontmatter(meta_raw, fm["glossary_doc"], where)
    _check_doc_type(meta, "glossary", fm, where)

    columns = fm["glossary_columns"]
    first_re = re.compile(fm["glossary_first_appearance_pattern"])
    header: list[str] | None = None
    terms: list[GlossaryTerm] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for line in body.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            if all(c in columns for c in cells) and len(cells) == len(columns):
                header = [columns[c] for c in cells]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        if len(cells) != len(header):
            raise SourceError(f"{where}: table row has {len(cells)} cells, expected {len(header)}: {line.strip()[:40]}")
        row = dict(zip(header, cells))
        note = row.get("note", "")
        match = first_re.search(note)
        term = GlossaryTerm(
            term=row["term"],
            category=_enum(row["category"], fm["glossary_category_values"], "glossary category", where),
            description=row.get("description", ""),
            note=note,
            first_appearance=int(match.group(1)) if match else None,
        )
        if term.term in seen:
            duplicates.append(term.term)
            continue
        seen.add(term.term)
        terms.append(term)
    if header is None:
        raise SourceError(f"{where}: glossary table header not found")
    return Glossary(terms=terms, duplicates=duplicates)


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.source_reader BOOK_DIR``：讀一本書並印出摘要。"""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Read a book folder (read only) and print a summary.")
    parser.add_argument("book_dir")
    parser.add_argument("--field-map", default=None)
    args = parser.parse_args(argv)
    try:
        fm = load_field_map(args.field_map)
        outlines = read_all_outlines(args.book_dir, fm)
        cards = read_characters(args.book_dir, fm)
        glossary = read_glossary(args.book_dir, fm)
    except T2vnError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"outlines   : {len(outlines)}")
    print(f"characters : {len(cards)}")
    print(f"glossary   : {len(glossary.terms)} terms, {len(glossary.duplicates)} duplicates")
    for name in glossary.duplicates:
        print(f"  duplicate: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
