"""分塊標註：長章節切成幾塊各自標註，每塊成功就存檢查點；失敗後重跑只做還沒完成的塊。

為什麼要分塊：
- 本機模型的上下文放不下整章（見 ``executors.check_fit``）。
- 一塊出錯只重做那一塊，不用整章從頭來；修正回合的提示詞也小很多。

做法：``plan_chunks`` 在段落邊界把章節切成幾塊（盡量不切在對話中間）→ 每塊用 ``annotate.annotate_chapter``
標註（提示詞告知「只標註第 a–b 段」並帶上一塊最後一場的地點與時間）→ 成功就把這塊存成檢查點 →
``merge_chunks`` 把各塊的場接起來（交界處地點、時段、BGM 相同的兩場會合併成一場）。
檢查點放在 ``scenes/_chunks/chNNN/``；章節標註成功就刪掉。段落文字或切法改變了，舊檢查點自動作廢。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .annotate import (MAX_SCENE_PARAGRAPHS, AnnotateError, AnnotateResult, ChapterContext, annotate_chapter)
from .errors import JobStopped
from .llm import LlmFn, RunHooks
from .schema_check import check_scene_links, validate_data

MIN_CHUNK_PARAGRAPHS = 3
SCHEMA_VERSION = "0.1"


def plan_chunks(paragraphs: list[dict[str, Any]], chunk_chars: int) -> list[list[dict[str, Any]]]:
    """把段落（有 ``para_no``、``text``）依字數切成幾塊；``chunk_chars`` <= 0 或整章放得下就只有一塊。

    在段落邊界切；接近上限時優先選「下一段不是以引號開頭」的邊界（避免把一串對話切開）。
    """
    total = sum(len(p["text"]) for p in paragraphs)
    if chunk_chars <= 0 or total <= chunk_chars or len(paragraphs) < 2 * MIN_CHUNK_PARAGRAPHS:
        return [list(paragraphs)]
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    index = 0
    while index < len(paragraphs):
        para = paragraphs[index]
        current.append(para)
        size += len(para["text"])
        index += 1
        remaining = len(paragraphs) - index
        if size >= chunk_chars and remaining >= MIN_CHUNK_PARAGRAPHS and len(current) >= MIN_CHUNK_PARAGRAPHS:
            # 往回找最近 25% 內一個「下一段不是對話開頭」的邊界
            cut = len(current)
            window = max(MIN_CHUNK_PARAGRAPHS, int(len(current) * 0.75))
            for back in range(len(current), window - 1, -1):
                nxt = current[back] if back < len(current) else paragraphs[index]
                if not nxt["text"].lstrip().startswith(("「", "『", "\"", "“")):
                    cut = back
                    break
            if cut < len(current):
                index -= len(current) - cut
                current = current[:cut]
            chunks.append(current)
            current, size = [], 0
    if current:
        if chunks and len(current) < MIN_CHUNK_PARAGRAPHS:
            chunks[-1].extend(current)
        else:
            chunks.append(current)
    return chunks


def plan_hash(chunks: list[list[dict[str, Any]]]) -> str:
    """切法與段落文字的指紋；任何一個變了，舊檢查點就不能用。"""
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(json.dumps([[p["para_no"], p["text"]] for p in chunk], ensure_ascii=False).encode("utf-8"))
        digest.update(b"|")
    return digest.hexdigest()[:16]


def auto_chunk_chars(prov: dict[str, Any] | None, cap: int = 15000) -> int:
    """依執行者的上下文長度算一塊最多放幾個字：要留空間給角色表、規則、輸出。沒給執行者就只用上限 ``cap``。"""
    if not prov:
        return cap
    from vendor.novel_studio import providers

    context = int(prov.get("context_tokens") or 0)
    if not context:
        return cap
    ratio = providers.cjk_ratio(prov) or 1.0
    output = int(prov.get("max_output") or 0)
    # 輸出（op 清單）大約是輸入字數的一半 token；提示詞固定部分約 5000 tokens
    usable = context - 5000 - min(output, context // 3)
    fit = int(usable / (ratio * 1.6))
    return max(1500, min(cap, fit))


def chunk_chars_for(config: Any, executor: str | None = None) -> int:
    """這次標註一塊最多幾個字：config 的 ``annotate.chunk_chars`` 當上限（沒設用 15000），再依執行者的上下文長度往下調。"""
    from . import executors

    try:
        prov = executors.resolve_executor(executors.build_providers(config.llm_overrides), executor or config.llm_default)
    except Exception:  # noqa: BLE001 — 執行者設定有問題時，別讓分塊計算擋住；真正的錯誤會在呼叫模型時報
        prov = None
    return auto_chunk_chars(prov, int(getattr(config, "annotate_chunk_chars", 0) or 15000))


# ---------------------------------------------------------------- 檢查點


@dataclass
class ChunkStore:
    """一章的檢查點資料夾。所有寫檔都經 ``writable``（呼叫端傳入 ``config.assert_writable``）。"""

    folder: Path
    writable: Callable[[Path], Path] = lambda p: p

    def _path(self, name: str) -> Path:
        return self.writable(self.folder / name)

    def reset(self) -> None:
        if self.folder.is_dir():
            for path in self.folder.glob("*.json"):
                self.writable(path).unlink()

    def load_plan(self) -> str | None:
        try:
            return json.loads((self.folder / "plan.json").read_text(encoding="utf-8")).get("hash")
        except (OSError, ValueError, AttributeError):
            return None

    def save_plan(self, digest: str, ranges: list[list[int]]) -> None:
        self._write("plan.json", {"hash": digest, "ranges": ranges})

    def load(self, index: int) -> dict[str, Any] | None:
        try:
            data = json.loads((self.folder / f"c{index:02d}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("scenes"), list) else None

    def save(self, index: int, data: dict[str, Any]) -> None:
        self._write(f"c{index:02d}.json", data)

    def _write(self, name: str, data: Any) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 合併


def _para_count(scene: dict[str, Any]) -> int:
    return len({line["source_para"] for line in scene["lines"] if "source_para" in line})


def _joinable(left: dict[str, Any], right: dict[str, Any]) -> bool:
    same = all(left.get(k) == right.get(k) for k in ("location_id", "time_of_day", "bgm", "weather"))
    return same and _para_count(left) + _para_count(right) <= MAX_SCENE_PARAGRAPHS


def merge_chunks(chapter_id: str, book_id: str, lang: str, last_para: int,
                 parts: list[tuple[list[dict[str, Any]], set[tuple[str, int]]]]) -> tuple[dict[str, Any], set[tuple[str, int]], list[str]]:
    """把各塊的場接起來，重新編號。回傳 (scene_doc, flagged, 提醒)。"""
    out: list[dict[str, Any]] = []
    marks: list[set[int]] = []
    notes: list[str] = []
    for chunk_index, (scenes, flagged) in enumerate(parts):
        by_sid: dict[str, set[int]] = {}
        for sid, idx in flagged:
            by_sid.setdefault(sid, set()).add(idx)
        for scene_index, original in enumerate(scenes):
            scene = copy.deepcopy(original)
            idxs = by_sid.get(original["scene_id"], set())
            if chunk_index > 0 and scene_index == 0 and out and _joinable(out[-1], scene):
                offset = len(out[-1]["lines"])
                out[-1]["lines"] += scene["lines"]
                marks[-1] |= {i + offset for i in idxs}
                have = {m["char_id"] for m in out[-1]["cast"]}
                out[-1]["cast"] += [m for m in scene["cast"] if m["char_id"] not in have]
                notes.append(f"第 {chunk_index}、{chunk_index + 1} 塊交界的兩場地點與時段相同，已合併成一場")
            else:
                out.append(scene)
                marks.append(set(idxs))
    for n, scene in enumerate(out, 1):
        scene["scene_id"] = f"{chapter_id}_sc{n:02d}"
        scene["next"] = f"{chapter_id}_sc{n + 1:02d}" if n < len(out) else None
    flagged = {(scene["scene_id"], i) for scene, idxs in zip(out, marks) for i in idxs}
    doc = {"schema_version": SCHEMA_VERSION, "book_id": book_id, "chapter_id": chapter_id, "lang": lang,
           "source": {"file_key": "chapter_text", "paragraphs": [1, last_para]}, "scenes": out}
    return doc, flagged, notes


# ---------------------------------------------------------------- 一章（分塊）


@dataclass
class ChunkedRun:
    """分塊標註的結果＋過程資訊（給紀錄用）。"""

    result: AnnotateResult
    chunks: int
    resumed: int  # 這次從檢查點直接用掉幾塊


class ChunkFailure(AnnotateError):
    """某一塊失敗。前面完成的塊已存檢查點，重跑會從這一塊繼續。"""

    def __init__(self, message: str, chunk: int, total: int, issues: list[str], raw: list[str], history: list[list[str]]):
        super().__init__(message, issues, raw, history)
        self.chunk, self.total = chunk, total


def annotate_chunked(ctx: ChapterContext, llm: LlmFn, book_id: str, lang: str, chunks: list[list[dict[str, Any]]],
                     store: ChunkStore, max_repairs: int = 2, card_roles: dict[str, str] | None = None,
                     hooks: RunHooks | None = None, log: Callable[[str], None] | None = None,
                     save_raw: Callable[[int, list[str]], None] | None = None, fresh: bool = False) -> ChunkedRun:
    """逐塊標註並存檢查點。``fresh`` 為真就忽略舊檢查點。失敗丟 ``ChunkFailure``；使用者中斷丟 ``JobStopped``。"""
    say = log or (lambda text: None)
    chapter = ctx.chapter
    chapter_id = chapter["chapter_id"]
    total = len(chunks)
    digest = plan_hash(chunks)
    if fresh or store.load_plan() != digest:
        store.reset()
        store.save_plan(digest, [[c[0]["para_no"], c[-1]["para_no"]] for c in chunks])

    cast_doc = copy.deepcopy(ctx.cast_doc)
    locations, bgm = list(ctx.known_locations), list(ctx.known_bgm)
    previous = ctx.previous_scene
    parts: list[tuple[list[dict[str, Any]], set[tuple[str, int]]]] = []
    added: list[str] = []
    warnings: list[str] = []
    raw_all: list[str] = []
    usage: list[dict[str, Any]] = []
    rounds = 0
    resumed = 0

    for index, paras in enumerate(chunks, 1):
        first, last = paras[0]["para_no"], paras[-1]["para_no"]
        saved = store.load(index)
        if saved is not None:
            for char_id, definition in (saved.get("added_defs") or {}).items():
                cast_doc["characters"].setdefault(char_id, definition)
            scenes, flagged = saved["scenes"], {(sid, int(i)) for sid, i in saved.get("flagged", [])}
            added += [c for c in saved.get("added", []) if c not in added]
            warnings += saved.get("warnings", [])
            usage += saved.get("usage", [])
            resumed += 1
            say(f"{chapter_id}: 第 {index}/{total} 塊（第 {first}–{last} 段）已有檢查點，直接沿用")
        else:
            if hooks:
                hooks.on_event("chunk", {"index": index, "total": total, "first": first, "last": last})
            say(f"{chapter_id}: 標註第 {index}/{total} 塊（第 {first}–{last} 段，{len(paras)} 段）...")
            part = {"index": index, "total": total, "first": first, "last": last, "all": len(chapter["paragraphs"])}
            sub = {**chapter, "paragraphs": paras}
            sub_ctx = ChapterContext(sub, cast_doc, ctx.outline_sections, ctx.character_notes, locations, bgm, previous, part)
            try:
                result = annotate_chapter(sub_ctx, llm, book_id, lang, max_repairs, card_roles, hooks=hooks)
            except JobStopped:
                raise
            except AnnotateError as exc:
                if save_raw:
                    save_raw(index, exc.raw)
                raise ChunkFailure(f"chunk {index}/{total} (paragraphs {first}–{last}): {exc}", index, total,
                                   exc.issues, exc.raw, exc.history) from exc
            if save_raw:
                save_raw(index, result.raw)
            cast_doc = result.cast_doc
            scenes, flagged = result.scene_doc["scenes"], set(result.flagged)
            new_ids = [c for c in result.added_characters if c not in added]
            added += new_ids
            warnings += result.warnings
            usage += result.usage
            rounds += result.rounds
            raw_all += result.raw
            store.save(index, {"range": [first, last], "scenes": scenes, "flagged": sorted([sid, i] for sid, i in flagged),
                               "added": result.added_characters, "added_defs": {c: cast_doc["characters"][c] for c in result.added_characters},
                               "warnings": result.warnings, "usage": result.usage})
        parts.append((scenes, flagged))
        if scenes:
            tail = scenes[-1]
            previous = {"location_id": tail["location_id"], "time_of_day": tail["time_of_day"]}
            for scene in scenes:
                if scene["location_id"] not in locations:
                    locations.append(scene["location_id"])
                if scene.get("bgm") and scene["bgm"] not in bgm:
                    bgm.append(scene["bgm"])

    doc, flagged, notes = merge_chunks(chapter_id, book_id, lang, max(p["para_no"] for p in chapter["paragraphs"]), parts)
    problems = [f"schema {i.path or '/'}: {i.message}" for i in validate_data(doc, "scene")]
    if not problems:
        problems = [f"links {i.path}: {i.message}" for i in check_scene_links(doc, cast_doc)]
    if problems:
        raise AnnotateError("merged chunks are invalid", problems, raw_all)
    result = AnnotateResult(doc, cast_doc, added, flagged, warnings + notes, rounds or total, raw_all, usage, [])
    return ChunkedRun(result, total, resumed)
