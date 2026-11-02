"""S4 劇本轉換管線：正規化章節 → LLM 分場與標註 → 展開成 scene graph → 驗證。

LLM 只輸出精簡的 op（見 ``prompts/annotate_system.md``）：整段旁白用 ``N`` 引用段落號，
由程式原文照抄、自動斷句；程式另外檢查覆蓋率、順序、旁白是否忠於原文、說話者是否存在、schema。
有問題就把問題清單回給 LLM 要求修正（最多 ``max_repairs`` 次）。

CLI：``python -m t2vn.annotate --book sample_book --chapters 2-5``（細節見 ``main``）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import JobStopped, SourceError, T2vnError
from .llm import LlmError, LlmFn, RunHooks, call_llm
from .schema_check import check_scene_links, validate_data

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT_FILE = REPO_ROOT / "prompts" / "annotate_system.md"

SCHEMA_VERSION = "0.1"
SOFT_LIMIT = 60
HARD_LIMIT = 90
PLAYER_ID = "player"

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
EXPRS = {"neutral", "smile", "sad", "angry", "surprised", "worried", "serious", "embarrassed"}
TIMES = {"dawn", "morning", "day", "dusk", "night"}
WEATHERS = {"clear", "cloudy", "rain", "storm", "fog"}
POSITIONS = {"left", "center", "right"}
EFFECTS = {"shake", "flash", "fade_in", "fade_out", "none"}
ROLES = {"protagonist", "main", "supporting", "antagonist", "minor"}
OP_KINDS = {"N", "n", "d", "x"}

_SENTENCE_END = re.compile(r"(?<=[。！？])(?![」』…—])")
# 匯入的書常混用半形標點（, . : ; ! ? ．），原文與模型輸出的標點可能不同，比對時一律當分隔符
_CLAUSE_SPLIT = re.compile(r"[，。？！：；、,.?!:;．]")
_CLAUSE_STRIP = "…—『』「」\"“”‘’' 　"
_PRONOUNS = "她他它我你"
MAX_SCENE_PARAGRAPHS = 35
_QUOTE_RE = re.compile(r"「([^「」]*)」|\"([^\"]*)\"|“([^“”]*)”")
_SPEECH_END = "。？！…—.?!．"
_SHAPES = ("tall", "broad", "small", "bent", "round")
# 只把和分場有關的章綱段落放進提示詞（禁止事項、伏筆等是寫作用的，會干擾標註）。
OUTLINE_FOR_PROMPT = {"plot_points": "情節點", "cast_names": "出場角色"}


class AnnotateError(T2vnError):
    """標註失敗。``issues`` 是最後一輪的問題清單，``raw`` 是每一輪 LLM 的原始回應。"""

    def __init__(self, message: str, issues: list[str] | None = None, raw: list[str] | None = None,
                 history: list[list[str]] | None = None):
        super().__init__(message)
        self.issues = issues or []
        self.raw = raw or []
        self.history = history or []  # 每一輪的完整問題清單（第 1 輪在最前）


# ---------------------------------------------------------------- 斷句


def split_text(text: str) -> list[str]:
    """句末（。！？）斷句，短句合併到約 60 字；超過 90 字的句子在逗號斷，還是太長就硬切。"""
    pieces: list[str] = []
    for sentence in (s for s in _SENTENCE_END.split(text) if s):
        if len(sentence) <= HARD_LIMIT:
            pieces.append(sentence)
            continue
        buffer = ""
        for part in re.split(r"(?<=，)", sentence):
            while len(part) > HARD_LIMIT:
                if buffer:
                    pieces.append(buffer)
                    buffer = ""
                pieces.append(part[:HARD_LIMIT])
                part = part[HARD_LIMIT:]
            if buffer and len(buffer) + len(part) > HARD_LIMIT:
                pieces.append(buffer)
                buffer = ""
            buffer += part
        if buffer:
            pieces.append(buffer)
    out: list[str] = []
    buffer = ""
    for piece in pieces:
        if buffer and len(buffer) + len(piece) > SOFT_LIMIT:
            out.append(buffer)
            buffer = ""
        buffer += piece
    if buffer:
        out.append(buffer)
    return out


def has_quoted_speech(text: str) -> bool:
    """段落裡有像是說話的引號內容（引號內以句末標點結尾）。只是引用名稱（「灰船」）不算。"""
    for m in _QUOTE_RE.finditer(text):
        inner = next(g for g in m.groups() if g is not None)
        if inner.rstrip("」』\"” ").endswith(tuple(_SPEECH_END)):
            return True
    return False


def locate_clause(clause: str, paragraphs: dict[int, str], exclude: int) -> int | None:
    """這個子句出現在哪一段（給修正回饋用）。"""
    for number, source in sorted(paragraphs.items()):
        if number != exclude and clause in source:
            return number
    return None


def unfaithful_clauses(text: str, source: str) -> list[str]:
    """回傳在原段落找不到的子句（忽略標點、引號；允許句首補一個代名詞當主詞）。"""
    bad = []
    for clause in _CLAUSE_SPLIT.split(text):
        clause = clause.strip(_CLAUSE_STRIP)
        if not clause or clause in source:
            continue
        if len(clause) > 1 and clause[0] in _PRONOUNS and clause[1:] in source:
            continue
        bad.append(clause)
    return bad


def _norm(text: str) -> str:
    """去掉標點、引號、空白，只留文字本身，供漏字比對。"""
    return "".join(_CLAUSE_SPLIT.sub("", text).translate({ord(c): None for c in _CLAUSE_STRIP}).split())


# 「她說」「艾莉問」這種純對白標籤：說話的人已經在對白行上，不算漏掉敘述。
_SPEECH_TAG = re.compile(r"^(?:最後|然後|後來)?[^沒不只最又才終再先已也還都沉低輕突後然]{0,6}(?:說|問|道|答|喊|叫|笑道|嘆道|罵|嚷)$")
_OMIT_SPLIT = re.compile(r"[，。？！：；、,.?!:;．「」『』\"“”]")


def omitted_clauses(source: str, line_texts: list[str]) -> list[str]:
    """原段落裡沒有出現在任何一行的子句（忽略標點）。用來抓「整句敘述被刪掉」。"""
    joined = _norm("".join(line_texts))
    missing = []
    for clause in _OMIT_SPLIT.split(source):
        clause = clause.strip(_CLAUSE_STRIP)
        if len(_norm(clause)) < 2 or _SPEECH_TAG.match(clause):
            continue
        if _norm(clause) not in joined:
            missing.append(clause)
    return missing


def paragraph_omissions(lines: list[dict[str, Any]], paragraphs: dict[int, str]) -> dict[int, list[str]]:
    """每個有出現在 lines 的段落 → 被刪掉的子句。"""
    by_para: dict[int, list[str]] = {}
    for line in lines:
        if "source_para" in line and "text" in line:
            by_para.setdefault(line["source_para"], []).append(line["text"])
    out: dict[int, list[str]] = {}
    for para, texts in by_para.items():
        if para in paragraphs:
            missing = omitted_clauses(paragraphs[para], texts)
            if missing:
                out[para] = missing
    return out


def _missing_spans(source: str, line_texts: list[str]) -> list[tuple[int, int]]:
    """原段落中被漏掉的敘述片段（原文偏移 start, end），相鄰的子句合併成一段。"""
    joined = _norm("".join(line_texts))
    spans: list[tuple[int, int]] = []
    start = 0
    parts = list(_OMIT_SPLIT.finditer(source))
    bounds = [(start, m.start(), m.end()) for m in parts]
    cursor = 0
    pieces: list[tuple[int, int, int, str]] = []  # (clause_start, clause_end, next_start, delimiter)
    for m in parts:
        pieces.append((cursor, m.start(), m.end(), m.group()))
        cursor = m.end()
    pieces.append((cursor, len(source), len(source), ""))
    del bounds
    for c_start, c_end, _next, delim in pieces:
        clause = source[c_start:c_end].strip(_CLAUSE_STRIP)
        if len(_norm(clause)) < 2 or _SPEECH_TAG.match(clause) or _norm(clause) in joined:
            continue
        end = c_end + (len(delim) if delim in "，。？！、；：,.?!;:．" and delim else 0)
        if spans and source[spans[-1][1]:c_start].strip(_CLAUSE_STRIP + "，、") == "":
            spans[-1] = (spans[-1][0], end)
        else:
            spans.append((c_start, end))
    return spans


def repair_omissions(doc: dict[str, Any], paragraphs: dict[int, str]) -> int:
    """把被漏掉的敘述原文補回 scene graph（照原文順序插進該段落的行之間）。回傳補了幾行。"""
    added = 0
    for scene in doc.get("scenes", []):
        lines = scene["lines"]
        for para in sorted({ln["source_para"] for ln in lines if "source_para" in ln}):
            source = paragraphs.get(para)
            if source is None:
                continue
            idx = [i for i, ln in enumerate(lines) if ln.get("source_para") == para and "text" in ln]
            spans = _missing_spans(source, [lines[i]["text"] for i in idx])
            if not spans:
                continue
            # 每個現有行在原文中的位置（以去標點後的文字對位）
            norm_src, back = "", []
            for pos, ch in enumerate(source):
                n = _norm(ch)
                if n:
                    norm_src += n
                    back.append(pos)
            positions, cur = [], 0
            for i in idx:
                n = _norm(lines[i]["text"])
                at = norm_src.find(n, cur) if n else -1
                if at < 0:
                    at = norm_src.find(n) if n else -1
                if at >= 0:
                    positions.append(back[at])
                    cur = at + len(n)
                else:
                    positions.append(positions[-1] if positions else 0)
            new_by_slot: dict[int, list[dict[str, Any]]] = {}
            for s_start, s_end in spans:
                text = source[s_start:s_end].strip()
                if text and text[-1] in "，、；：,;:":
                    text = text[:-1] + "。"  # 標點不影響逐字比對；句尾不留逗號
                slot = next((k for k, pos in enumerate(positions) if pos > s_start), len(idx))
                for t in split_text(text):
                    new_by_slot.setdefault(slot, []).append({"type": "narration", "text": t, "source_para": para})
                    added += 1
            rebuilt: list[dict[str, Any]] = []
            first = idx[0]
            slot_at = {i: k for k, i in enumerate(idx)}
            for i, ln in enumerate(lines):
                if i in slot_at:
                    rebuilt += new_by_slot.pop(slot_at[i], [])
                rebuilt.append(ln)
                if i == idx[-1]:
                    rebuilt += new_by_slot.pop(len(idx), [])
            del first
            lines[:] = rebuilt
    return added


# ---------------------------------------------------------------- 提示詞


@dataclass
class ChapterContext:
    """組提示詞需要的資料。``outline_sections`` 是章綱的英文 section key → 原文。"""

    chapter: dict[str, Any]
    cast_doc: dict[str, Any]
    outline_sections: dict[str, str] = field(default_factory=dict)
    character_notes: dict[str, str] = field(default_factory=dict)  # 角色中文名 -> 一句話
    known_locations: list[str] = field(default_factory=list)
    known_bgm: list[str] = field(default_factory=list)
    previous_scene: dict[str, str] | None = None  # {"location_id", "time_of_day"}
    part: dict[str, int] | None = None  # 分塊標註時：{"index", "total", "first", "last", "all"}


def build_messages(ctx: ChapterContext, system_prompt_file: str | Path = SYSTEM_PROMPT_FILE) -> list[dict[str, str]]:
    system = Path(system_prompt_file).read_text(encoding="utf-8")
    chapter = ctx.chapter
    lang_names = []
    for char_id, char in ctx.cast_doc["characters"].items():
        if char_id == PLAYER_ID:
            continue
        name = next(iter(char["names"].values()))
        entry = {"id": char_id, "name": name, "aliases": char.get("aliases", []), "role": char["role"]}
        if ctx.character_notes.get(name):
            entry["note"] = ctx.character_notes[name]
        lang_names.append(json.dumps(entry, ensure_ascii=False))
    count = f"段落數：{len(chapter['paragraphs'])}"
    if ctx.part:
        p = ctx.part
        count = (f"這一章分成 {p['total']} 塊標註，這是第 {p['index']} 塊：只標註第 {p['first']}–{p['last']} 段"
                 f"（共 {len(chapter['paragraphs'])} 段，全章 {p['all']} 段）。段落編號沿用全章編號；範圍內每一段都要用到，範圍外的不要輸出。")
    parts = [
        f"# 本章\nchapter_id: {chapter['chapter_id']}\n章名：{chapter['title']}\n{count}",
        "# 角色表（說話者只能用這些 id，或 player＝主角內心獨白）\n" + "\n".join(lang_names),
    ]
    if ctx.outline_sections:
        outline = "\n\n".join(f"## {label}\n{ctx.outline_sections[key]}"
                                for key, label in OUTLINE_FOR_PROMPT.items() if ctx.outline_sections.get(key))
        if outline:
            parts.append("# 章綱（分場參考）\n" + outline)
    parts.append(
        "# 已用過的 id（沿用）\n"
        f"location_id: {', '.join(ctx.known_locations) or '（無）'}\n"
        f"bgm: {', '.join(ctx.known_bgm) or '（無）'}"
    )
    if ctx.previous_scene:
        parts.append(
            f"# {'上一塊' if ctx.part and ctx.part['index'] > 1 else '前一章'}最後一場\nlocation_id: {ctx.previous_scene['location_id']}，time_of_day: {ctx.previous_scene['time_of_day']}"
        )
    body = "\n".join(f"[{p['para_no']}] {p['text']}" for p in chapter["paragraphs"])
    parts.append("# 正文\n" + body)
    parts.append("請依規則輸出 JSON。")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


# ---------------------------------------------------------------- 解析與新角色


def parse_response(text: str) -> dict[str, Any]:
    """容錯解析 LLM 回傳的 JSON（去掉 ``` 圍欄與前後雜訊）。"""
    cleaned = text.strip().replace("```json", "```")
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
    try:
        data = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise AnnotateError("response is not JSON") from None
        try:
            data = json.loads(cleaned[start : end + 1])
        except ValueError as exc:
            raise AnnotateError(f"response JSON is malformed: {exc}") from None
    if not isinstance(data, dict):
        raise AnnotateError("response JSON must be an object")
    return data


def merge_new_characters(cast_doc: dict[str, Any], new_chars: Any, chapter_id: str, lang: str,
                         card_roles: dict[str, str] | None = None) -> tuple[dict[str, Any], list[str], list[str]]:
    """把 LLM 提的新角色加進角色表副本。回傳 (新角色表, 新增的 id, 問題)。角色卡有同名的就用角色卡的定位。"""
    issues: list[str] = []
    added: list[str] = []
    doc = json.loads(json.dumps(cast_doc))
    characters = doc["characters"]
    if new_chars in (None, []):
        return doc, added, issues
    if not isinstance(new_chars, list):
        return doc, added, ["new_characters must be a list"]
    alias_owner = {alias: cid for cid, char in characters.items() for alias in char.get("aliases", [])}
    for index, entry in enumerate(new_chars):
        where = f"new_characters[{index}]"
        if not isinstance(entry, dict):
            issues.append(f"{where} must be an object")
            continue
        char_id, name = entry.get("id"), entry.get("name")
        if not isinstance(char_id, str) or not ID_RE.match(char_id) or char_id == PLAYER_ID:
            issues.append(f"{where}: id {char_id!r} must match [a-z][a-z0-9_] (ASCII, not Chinese)")
            continue
        if char_id in characters:
            continue  # 已存在：當作沿用
        if not isinstance(name, str) or not name:
            issues.append(f"{where}: name is required")
            continue
        role = (card_roles or {}).get(name) or entry.get("role")
        if role not in ROLES:
            issues.append(f"{where}: role {role!r} must be one of {sorted(ROLES)}")
            continue
        aliases = [a for a in entry.get("aliases", []) if isinstance(a, str) and a]
        clash = [a for a in aliases if alias_owner.get(a, char_id) != char_id]
        if clash:
            issues.append(f"{where}: aliases {clash} already belong to other characters")
            continue
        characters[char_id] = {
            "names": {lang: name}, "aliases": aliases, "role": role, "first_chapter": chapter_id,
            "hue": _free_hue(characters), "shape": _SHAPES[len(characters) % len(_SHAPES)],
        }
        alias_owner.update({a: char_id for a in aliases})
        added.append(char_id)
    return doc, added, issues


def _free_hue(characters: dict[str, Any]) -> int:
    used = [c["hue"] for c in characters.values() if isinstance(c.get("hue"), int)]
    if not used:
        return 0
    return max(range(0, 360, 5), key=lambda h: min(min(abs(h - u), 360 - abs(h - u)) for u in used))


# ---------------------------------------------------------------- 展開


@dataclass
class Expanded:
    scene_doc: dict[str, Any]
    issues: list[str]
    flagged: set[tuple[str, int]]
    warnings: list[str]


def split_long_scenes(raw_scenes: list[Any]) -> tuple[list[Any], list[str]]:
    """段落數超過 MAX_SCENE_PARAGRAPHS 的場，直接在段落邊界切成幾份（不再叫模型重做整章）。
    各份沿用原場的地點、時間、出場名單；標題加「（1/2）」。回傳 (新的場清單, 提醒)。"""
    out: list[Any] = []
    notes: list[str] = []
    for index, raw in enumerate(raw_scenes, 1):
        ops = raw.get("ops") if isinstance(raw, dict) else None
        if not isinstance(ops, list):
            out.append(raw)
            continue
        paras = list(dict.fromkeys(o["p"] for o in ops if isinstance(o, dict) and isinstance(o.get("p"), int)))
        if len(paras) <= MAX_SCENE_PARAGRAPHS:
            out.append(raw)
            continue
        parts = -(-len(paras) // MAX_SCENE_PARAGRAPHS)
        target = -(-len(paras) // parts)
        chunks: list[list[Any]] = [[]]
        seen: set[int] = set()
        for op in ops:
            p = op.get("p") if isinstance(op, dict) else None
            if isinstance(p, int) and p not in seen:
                if len(seen) >= target and len(chunks) < parts:
                    chunks.append([])
                    seen = set()
                seen.add(p)
            chunks[-1].append(op)
        title = raw.get("title") if isinstance(raw.get("title"), str) else ""
        for n, chunk in enumerate(chunks, 1):
            piece = {**raw, "ops": chunk}
            if title:
                piece["title"] = f"{title}（{n}/{len(chunks)}）"
            out.append(piece)
        notes.append(f"scene {index}（第 {paras[0]}–{paras[-1]} 段，{len(paras)} 段）超過 {MAX_SCENE_PARAGRAPHS} 段，"
                     f"已在段落邊界自動切成 {len(chunks)} 場；請校對切點是否合理")
    return out, notes


def expand(ops_doc: dict[str, Any], chapter: dict[str, Any], cast_doc: dict[str, Any], book_id: str,
           lang: str) -> Expanded:
    """把 op 展開成 scene graph，並收集所有問題（空清單＝可以採用）。"""
    issues: list[str] = []
    warnings: list[str] = []
    flagged: set[tuple[str, int]] = set()
    paragraphs = {p["para_no"]: p["text"] for p in chapter["paragraphs"]}
    last_para = max(paragraphs)
    chapter_id = chapter["chapter_id"]
    known = set(cast_doc["characters"]) | {PLAYER_ID}

    raw_scenes = ops_doc.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        return Expanded({}, ["scenes must be a non-empty list"], flagged, warnings)
    raw_scenes, split_notes = split_long_scenes(raw_scenes)
    warnings += split_notes
    if len(raw_scenes) > 99:
        return Expanded({}, ["too many scenes (max 99)"], flagged, warnings)

    used: set[int] = set()
    previous_p = 0
    scenes: list[dict[str, Any]] = []
    for s_index, raw in enumerate(raw_scenes):
        sid = f"{chapter_id}_sc{s_index + 1:02d}"
        where = f"scene {s_index + 1}"
        if not isinstance(raw, dict):
            issues.append(f"{where} must be an object")
            continue
        location, time, bgm = raw.get("location_id"), raw.get("time_of_day"), raw.get("bgm")
        if not isinstance(location, str) or not ID_RE.match(location):
            issues.append(f"{where}: location_id {location!r} must match [a-z][a-z0-9_]")
            location = "unknown"
        if time not in TIMES:
            issues.append(f"{where}: time_of_day {time!r} must be one of {sorted(TIMES)}")
            time = "day"
        if bgm is not None and (not isinstance(bgm, str) or not ID_RE.match(bgm)):
            issues.append(f"{where}: bgm {bgm!r} must match [a-z][a-z0-9_] or be null")
            bgm = None
        scene: dict[str, Any] = {"scene_id": sid}
        if isinstance(raw.get("title"), str) and raw["title"]:
            scene["title"] = {lang: raw["title"]}
        scene.update({"location_id": location, "time_of_day": time})
        if raw.get("weather") is not None:
            if raw["weather"] in WEATHERS:
                scene["weather"] = raw["weather"]
            else:
                issues.append(f"{where}: weather {raw['weather']!r} must be one of {sorted(WEATHERS)} or omitted")
        scene["bg"] = {"asset_id": f"bg_{location}_{time}"}
        scene["bgm"] = bgm
        scene["ambience"] = f"amb_{location}"

        cast = []
        for c_index, member in enumerate(raw.get("cast") or []):
            cwhere = f"{where} cast[{c_index}]"
            if not isinstance(member, dict) or member.get("id") not in known or member.get("id") == PLAYER_ID:
                issues.append(f"{cwhere}: id {member.get('id') if isinstance(member, dict) else member!r} is not in the cast table")
                continue
            entry = {"char_id": member["id"], "expr": member.get("e", "neutral"), "pos": member.get("pos", "center")}
            if entry["expr"] not in EXPRS:
                issues.append(f"{cwhere}: e {entry['expr']!r} must be one of {sorted(EXPRS)}")
                entry["expr"] = "neutral"
            if entry["pos"] not in POSITIONS:
                issues.append(f"{cwhere}: pos {entry['pos']!r} must be left/center/right")
                entry["pos"] = "center"
            cast.append(entry)
        scene["cast"] = cast

        lines: list[dict[str, Any]] = []
        ops = raw.get("ops")
        if not isinstance(ops, list) or not ops:
            issues.append(f"{where}: ops must be a non-empty list")
            ops = []
        for o_index, op in enumerate(ops):
            owhere = f"{where} op[{o_index}]"
            if not isinstance(op, dict) or op.get("k") not in OP_KINDS:
                issues.append(f"{owhere}: k must be one of N/n/d/x")
                continue
            kind = op["k"]
            if kind == "x":
                if op.get("e") not in EFFECTS:
                    issues.append(f"{owhere}: effect {op.get('e')!r} must be one of {sorted(EFFECTS)}")
                else:
                    lines.append({"type": "direction", "effect": op["e"]})
                continue
            para = op.get("p")
            if not isinstance(para, int) or para not in paragraphs:
                issues.append(f"{owhere}: p {para!r} is not a paragraph number (1–{last_para})")
                continue
            if para < previous_p:
                issues.append(f"{owhere}: p {para} goes backwards (previous {previous_p}); keep paragraph order")
            previous_p = max(previous_p, para)
            used.add(para)
            source = paragraphs[para]
            if kind == "N":
                if has_quoted_speech(source):
                    issues.append(
                        f"{owhere}: paragraph {para} contains quoted speech, so N would read the speech as narration. "
                        "Use n ops for the narration parts and d ops for each spoken quote (a paragraph may need several ops, "
                        "all with p = this paragraph). If the quote is written text (a note, a sign), use n and keep the full text.")
                    continue
                lines += [{"type": "narration", "text": t, "source_para": para} for t in split_text(source)]
                continue
            text = op.get("t")
            if not isinstance(text, str) or not text.strip():
                issues.append(f"{owhere}: t (text) is required")
                continue
            bad = unfaithful_clauses(text, source)
            if bad:
                hint = ""
                found = locate_clause(bad[0], paragraphs, para)
                if found is not None:
                    hint = f" — that text is in paragraph {found}; set p to {found}"
                issues.append(f"{owhere}: text is not verbatim from paragraph {para}: {'／'.join(bad[:3])}{hint}")
            if kind == "n":
                lines += [{"type": "narration", "text": t, "source_para": para} for t in split_text(text)]
                continue
            speaker = op.get("s")
            if speaker not in known:
                issues.append(f"{owhere}: speaker {speaker!r} is not in the cast table; add it to new_characters")
                continue
            expr = op.get("e")
            if speaker != PLAYER_ID and expr is None:
                expr = "neutral"
            if expr is not None and expr not in EXPRS:
                issues.append(f"{owhere}: e {expr!r} must be one of {sorted(EXPRS)}")
                expr = "neutral"
            for piece in split_text(text):
                line: dict[str, Any] = {"type": "dialogue", "speaker": speaker}
                if expr is not None and speaker != PLAYER_ID:
                    line["expr"] = expr
                line.update({"text": piece, "source_para": para})
                lines.append(line)
                if op.get("u"):
                    flagged.add((sid, len(lines)))
        if not any(line["type"] != "direction" for line in lines):
            issues.append(f"{where}: no narration or dialogue lines")
        scene_paras = sorted({line["source_para"] for line in lines if "source_para" in line})
        if scene_paras and len(scene_paras) > MAX_SCENE_PARAGRAPHS:
            issues.append(
                f"{where} covers paragraphs {scene_paras[0]}–{scene_paras[-1]} ({len(scene_paras)} paragraphs, limit "
                f"{MAX_SCENE_PARAGRAPHS}): split it into several scenes at a change of place, time of day or focus")
        scene["lines"] = lines
        _add_missing_speakers(scene)
        scenes.append(scene)

    all_lines = [line for scene in scenes for line in scene["lines"]]
    for para, clauses in sorted(paragraph_omissions(all_lines, paragraphs).items()):
        issues.append(
            f"paragraph {para}: narration was dropped (rules: do not cut narration). Missing: "
            f"{'／'.join(clauses[:3])}{'…' if len(clauses) > 3 else ''}. "
            f"Add an n op with p = {para} for it, in source order; or use N if the paragraph has no spoken quotes.")

    missing = sorted(set(paragraphs) - used)
    if missing:
        issues.append(f"paragraphs not used: {_ranges(missing)} (every paragraph must appear)")

    for index, scene in enumerate(scenes):
        scene["next"] = scenes[index + 1]["scene_id"] if index + 1 < len(scenes) else None
        paras = {line["source_para"] for line in scene["lines"] if "source_para" in line}
        if 0 < len(paras) < 4 and index + 1 < len(scenes):
            warnings.append(f"{scene['scene_id']}: only {len(paras)} paragraph(s); consider merging")

    doc = {
        "schema_version": SCHEMA_VERSION, "book_id": book_id, "chapter_id": chapter_id, "lang": lang,
        "source": {"file_key": "chapter_text", "paragraphs": [1, last_para]}, "scenes": scenes,
    }
    if not issues:
        issues += [f"schema {i.path or '/'}: {i.message}" for i in validate_data(doc, "scene")]
    if not issues:
        issues += [f"links {i.path}: {i.message}" for i in check_scene_links(doc, cast_doc)]
    return Expanded(doc, issues, flagged, warnings)


def _add_missing_speakers(scene: dict[str, Any]) -> None:
    """說話的人一定要在該場的出場名單裡（立繪才會出現）；LLM 漏列的由程式補上，用他第一句台詞的表情。"""
    cast = scene["cast"]
    present = {member["char_id"] for member in cast}
    free = [pos for pos in ("left", "right", "center") if pos not in {m.get("pos") for m in cast}]
    for line in scene["lines"]:
        speaker = line.get("speaker")
        if line["type"] != "dialogue" or speaker == PLAYER_ID or speaker in present:
            continue
        cast.append({"char_id": speaker, "expr": line.get("expr", "neutral"), "pos": free.pop(0) if free else "center"})
        present.add(speaker)


def _ranges(numbers: list[int]) -> str:
    out, start, prev = [], None, None
    for n in numbers:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}–{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}–{prev}")
    return ", ".join(out)


# ---------------------------------------------------------------- 一章


@dataclass
class AnnotateResult:
    scene_doc: dict[str, Any]
    cast_doc: dict[str, Any]
    added_characters: list[str]
    flagged: set[tuple[str, int]]
    warnings: list[str]
    rounds: int
    raw: list[str]
    usage: list[dict[str, Any]]
    history: list[list[str]] = field(default_factory=list)  # 成功前每一輪被退回的問題


def annotate_chapter(ctx: ChapterContext, llm: LlmFn, book_id: str, lang: str, max_repairs: int = 2,
                     card_roles: dict[str, str] | None = None,
                     system_prompt_file: str | Path = SYSTEM_PROMPT_FILE, hooks: RunHooks | None = None) -> AnnotateResult:
    """呼叫 LLM 標註一章；有問題就回饋修正。成功回傳結果，失敗丟 AnnotateError（含原始回應）。"""
    messages = build_messages(ctx, system_prompt_file)
    raw: list[str] = []
    usage: list[dict[str, Any]] = []
    issues: list[str] = []
    history: list[list[str]] = []
    for round_no in range(1, max_repairs + 2):
        try:
            result = call_llm(llm, messages, hooks, round_no)
        except LlmError as exc:
            raise AnnotateError(f"LLM call failed: {exc}", issues, raw, history) from exc
        raw.append(result.text)
        usage.append(dict(result.usage))
        try:
            ops_doc = parse_response(result.text)
        except AnnotateError as exc:
            issues = [str(exc)]
        else:
            cast_doc, added, issues = merge_new_characters(
                ctx.cast_doc, ops_doc.get("new_characters"), ctx.chapter["chapter_id"], lang, card_roles)
            if not issues:
                expanded = expand(ops_doc, ctx.chapter, cast_doc, book_id, lang)
                issues = expanded.issues
                if not issues:
                    return AnnotateResult(expanded.scene_doc, cast_doc, added, expanded.flagged,
                                          expanded.warnings, round_no, raw, usage, history)
        history.append(list(issues))
        if hooks:
            hooks.on_event("repair", {"round": round_no, "issues": issues[:5], "count": len(issues)})
        messages = messages + [
            {"role": "assistant", "content": result.text},
            {"role": "user", "content": "上面的輸出有以下問題。請逐條修正，並輸出**完整**的 JSON（不要只輸出修改的部分、不要重複上一輪的內容）：\n"
                                        + "\n".join(f"- {issue}" for issue in issues[:40])},
        ]
    raise AnnotateError(f"still invalid after {max_repairs} repair round(s)", issues, raw, history)


# ---------------------------------------------------------------- 書層級的輔助


def known_ids(scene_files: Iterable[Path]) -> tuple[list[str], list[str]]:
    """從既有的 scene 檔收集用過的 location_id 與 bgm。"""
    locations: set[str] = set()
    bgm: set[str] = set()
    for path in scene_files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for scene in doc.get("scenes", []):
            if scene.get("location_id"):
                locations.add(scene["location_id"])
            if scene.get("bgm"):
                bgm.add(scene["bgm"])
    return sorted(locations), sorted(bgm)


def last_scene_of(path: Path) -> dict[str, str] | None:
    try:
        scenes = json.loads(path.read_text(encoding="utf-8"))["scenes"]
    except (OSError, ValueError, KeyError):
        return None
    return {"location_id": scenes[-1]["location_id"], "time_of_day": scenes[-1]["time_of_day"]} if scenes else None


def parse_chapter_range(spec: str) -> list[int]:
    match = re.match(r"^([0-9]+)(?:-([0-9]+))?$", spec.strip())
    if not match:
        raise T2vnError(f"--chapters must look like 2 or 2-5, got {spec!r}")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start < 1 or end < start:
        raise T2vnError(f"invalid chapter range {spec!r}")
    return list(range(start, end + 1))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass
class DiskResult:
    """``annotate_chapter_to_disk`` 的結果。messages 是 (stream, text)，stream 為 out 或 err。"""

    status: str  # ok | skip | missing | error | dry_run
    messages: list[tuple[str, str]]
    scene_doc: dict[str, Any] | None = None
    tokens: int = 0
    note: str = ""  # 給 UI 章節列的一句話（例如分塊失敗時「第 2/4 塊失敗，重跑會從這裡繼續」）


def load_work_outlines(work: Path) -> dict[int, dict[str, str]]:
    """讀 works/<book>/outlines/chNNN.json（t2vn.extract 產生或手寫），回傳 章號 -> sections。"""
    found: dict[int, dict[str, str]] = {}
    for path in sorted((work / "outlines").glob("ch*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            found[int(doc["chapter_no"])] = dict(doc.get("sections", {}))
        except (OSError, ValueError, KeyError):
            continue
    return found


def cast_notes(cast_doc: dict[str, Any], lang: str) -> dict[str, str]:
    """角色表裡的 one_line -> {顯示名稱: 一句話}，給提示詞當角色備註。"""
    notes: dict[str, str] = {}
    for char in cast_doc.get("characters", {}).values():
        text = (char.get("one_line") or {}).get(lang)
        name = next(iter(char.get("names", {}).values()), "")
        if text and name:
            notes[name] = text
    return notes


def annotate_chapter_to_disk(config: Any, work: Path, number: int, llm: LlmFn | None, book_id: str, lang: str,
                             outlines: dict[int, dict[str, str]], notes: dict[str, str], card_roles: dict[str, str],
                             max_repairs: int = 2, force: bool = False, dry_run: bool = False,
                             log: Callable[[str], None] | None = None, hooks: RunHooks | None = None,
                             chunk_chars: int = 0) -> DiskResult:
    """標註一章並寫檔（scenes/chNNN.json、校對表、_raw）。CLI 與 UI 共用。cast.json 有新角色時一併更新。

    ``chunk_chars`` > 0 且章節比這長，就分塊標註：每塊成功存檢查點（``scenes/_chunks/chNNN/``），失敗後重跑從沒做完的塊繼續；
    ``force`` 會丟掉檢查點從頭來。
    """
    from . import chunked
    from .review import render_review

    messages: list[tuple[str, str]] = []

    def say(text: str, stream: str = "out") -> None:
        messages.append((stream, text))
        if log:
            log(text)

    scenes_dir = work / "scenes"
    cast_path = work / "cast.json"
    cast_doc = json.loads(cast_path.read_text(encoding="utf-8"))
    chapter_id = f"ch{number:03d}"
    chapter_path = work / "chapters" / f"{chapter_id}.json"
    out_path = scenes_dir / f"{chapter_id}.json"
    if not chapter_path.is_file():
        say(f"error: {chapter_path} not found; run tools/normalize_book.py first", "err")
        return DiskResult("missing", messages)
    if out_path.exists() and not force and not dry_run:
        say(f"skip: {out_path} exists (use --force to overwrite)")
        return DiskResult("skip", messages)
    chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
    existing = sorted(scenes_dir.glob("ch*.json")) + sorted((work / "gold").glob("ch*.json"))
    locations, bgm = known_ids(existing)
    previous = last_scene_of(scenes_dir / f"ch{number - 1:03d}.json") or last_scene_of(work / "gold" / f"ch{number - 1:03d}.json")
    ctx = ChapterContext(chapter, cast_doc, outlines.get(number, {}), notes, locations, bgm, previous)

    if dry_run:
        prompt_path = config.assert_writable(scenes_dir / "_prompt" / f"{chapter_id}.md")
        prompt_messages = build_messages(ctx)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("\n\n---\n\n".join(f"## {m['role']}\n\n{m['content']}" for m in prompt_messages), encoding="utf-8")
        say(f"dry-run: prompt written to {prompt_path} ({sum(len(m['content']) for m in prompt_messages)} chars)")
        return DiskResult("dry_run", messages)

    chunks = chunked.plan_chunks(chapter["paragraphs"], chunk_chars)
    say(f"{chapter_id}: annotating {len(chapter['paragraphs'])} paragraphs" + (f" in {len(chunks)} chunks" if len(chunks) > 1 else "") + " ...")
    raw_dir = scenes_dir / "_raw"

    def save_raw(texts: list[str]) -> None:
        for index, text in enumerate(texts, 1):
            path = config.assert_writable(raw_dir / f"{chapter_id}_r{index}.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    log_path = scenes_dir / "_log" / f"{chapter_id}.log"

    def save_log(history: list[list[str]], outcome: str) -> None:
        """每次執行都寫一份完整紀錄（含每一輪被退回的全部問題），網頁關掉也看得到。"""
        import datetime
        head = [f"# {chapter_id} {datetime.datetime.now():%Y-%m-%d %H:%M:%S} — {outcome}"]
        body = [f"## 第 {n} 輪被退回（{len(items)} 項）\n" + "\n".join(f"- {i}" for i in items)
                for n, items in enumerate(history, 1)]
        out = "\n\n".join(head + body + ["## 輸出訊息\n" + "\n".join(text for _, text in messages)])
        path = config.assert_writable(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(out + "\n", encoding="utf-8")

    def save_chunk_raw(index: int, texts: list[str]) -> None:
        for n, text in enumerate(texts, 1):
            path = config.assert_writable(raw_dir / f"{chapter_id}_c{index:02d}_r{n}.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    store = chunked.ChunkStore(scenes_dir / "_chunks" / chapter_id, config.assert_writable)
    try:
        if len(chunks) > 1:
            run = chunked.annotate_chunked(ctx, llm, book_id, lang, chunks, store, max_repairs, card_roles, hooks=hooks,
                                           log=say, save_raw=save_chunk_raw, fresh=force)
            result = run.result
            if run.resumed:
                say(f"{chapter_id}: 沿用了 {run.resumed}/{run.chunks} 塊的檢查點")
        else:
            result = annotate_chapter(ctx, llm, book_id, lang, max_repairs, card_roles, hooks=hooks)
    except JobStopped:
        say(f"{chapter_id}: stopped" + ("（已完成的塊有檢查點，重跑會繼續）" if len(chunks) > 1 else ""))
        return DiskResult("stopped", messages)
    except AnnotateError as exc:
        chunk_note = ""
        if isinstance(exc, chunked.ChunkFailure):
            chunk_note = f"第 {exc.chunk}/{exc.total} 塊失敗；前 {exc.chunk - 1} 塊已存檢查點，重跑（不要勾覆蓋）會從這一塊繼續"
        else:
            save_raw(exc.raw)
        say(f"error: {chapter_id}: {exc}", "err")
        if chunk_note:
            say(chunk_note, "err")
        for issue in exc.issues[:20]:
            say(f"  - {issue}", "err")
        if len(exc.issues) > 20:
            say(f"  ... 另有 {len(exc.issues) - 20} 項", "err")
        say(f"完整紀錄：{log_path}", "err")
        save_log(exc.history + [exc.issues], "failed")
        return DiskResult("error", messages, note=chunk_note)

    if len(chunks) == 1:
        save_raw(result.raw)
    _write_json(config.assert_writable(out_path), result.scene_doc)
    if result.added_characters:
        _write_json(config.assert_writable(cast_path), result.cast_doc)
        cast_doc = result.cast_doc
    notes_lines = [f"新角色（已加入 cast.json，請確認 id 與定位）：{', '.join(result.added_characters)}"] if result.added_characters else []
    notes_lines += [f"提醒：{w}" for w in result.warnings]
    review = render_review(result.scene_doc, cast_doc, result.flagged, f"{chapter_id} 校對表（自動標註）", notes_lines)
    config.assert_writable(scenes_dir / f"{chapter_id}_review.md").write_text(review, encoding="utf-8")
    tokens = sum(u.get("prompt_tokens", 0) + u.get("completion_tokens", 0) for u in result.usage)
    lines = sum(len(s["lines"]) for s in result.scene_doc["scenes"])
    say(f"{chapter_id}: ok — {len(result.scene_doc['scenes'])} scenes, {lines} lines, "
        f"{len(result.flagged)} inferred speaker(s), rounds {result.rounds}, tokens {tokens}")
    for warning in result.warnings:
        say(f"  warning: {warning}")
    save_log(result.history, "ok")
    if len(chunks) > 1:
        store.reset()  # 成功了，檢查點用不到了
        try:
            store.folder.rmdir()
        except OSError:
            pass
    return DiskResult("ok", messages, result.scene_doc, tokens)


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.annotate --book sample_book --chapters 2-5 [--dry-run] [--replay FILE ...] [--force]``"""
    sys.stdout.reconfigure(encoding="utf-8")
    from .config import load_config
    from .llm import ReplayLLM, make_executor
    from .source_reader import read_all_outlines, read_characters

    parser = argparse.ArgumentParser(description="Annotate normalized chapters into scene graphs with an LLM.")
    parser.add_argument("--book", required=True, help="book_id in config.yaml")
    parser.add_argument("--chapters", required=True, help="chapter number or range, e.g. 2 or 2-5")
    parser.add_argument("--config", default=None)
    parser.add_argument("--dry-run", action="store_true", help="write the prompt only; do not call the LLM")
    parser.add_argument("--replay", nargs="+", default=None, help="use saved LLM responses instead of calling the API")
    parser.add_argument("--force", action="store_true", help="overwrite existing scenes/chNNN.json")
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--chunk-chars", type=int, default=None,
                        help="split chapters longer than this many characters into chunks (default: automatic from the executor's context length; 0 = never)")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        book = config.book(args.book)
        work = config.work_dir(book.book_id)
        scenes_dir = work / "scenes"
        cast_path = work / "cast.json"
        cast_doc = json.loads(cast_path.read_text(encoding="utf-8"))
        chapters = parse_chapter_range(args.chapters)

        outlines: dict[int, dict[str, str]] = {}
        notes: dict[str, str] = {}
        card_roles: dict[str, str] = {}
        try:
            outlines = {o.chapter_no: o.sections for o in read_all_outlines(config.book_dir(book.book_id))}
        except SourceError as exc:
            print(f"warning: chapter outlines unavailable: {exc}")
        try:
            for card in read_characters(config.book_dir(book.book_id)):
                notes[card.name] = card.sections.get("one_line", "")
                card_roles[card.name] = card.role
        except SourceError as exc:
            print(f"warning: character cards unavailable: {exc}")

        for number_, sections in load_work_outlines(work).items():
            outlines.setdefault(number_, sections)
        for name_, text_ in cast_notes(cast_doc, book.lang).items():
            notes.setdefault(name_, text_)

        llm = ReplayLLM.from_files(args.replay) if args.replay else None
        if llm is None and not args.dry_run:
            llm = make_executor(config)
    except (T2vnError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from .chunked import chunk_chars_for

    chunk_chars = args.chunk_chars if args.chunk_chars is not None else chunk_chars_for(config)
    failures = 0
    for number in chapters:
        result = annotate_chapter_to_disk(config, work, number, llm, book.book_id, book.lang, outlines, notes, card_roles,
                                          max_repairs=args.max_repairs, force=args.force, dry_run=args.dry_run,
                                          chunk_chars=chunk_chars)
        for stream, text in result.messages:
            print(text, file=sys.stderr if stream == "err" else sys.stdout)
        failures += result.status in ("error", "missing")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
