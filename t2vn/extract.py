"""從已匯入的章節抽取章綱與角色（給沒有 novel-studio 設定的書，例如 TXT／EPUB 匯入）。

做法參考 novel-studio 的「從正文建立設定」：逐章呼叫、帶著跨章狀態（目前角色表、上一章摘要）、
每章結果立刻存檔，中斷後從斷的地方接著跑。輸出全部是英文 key：

- ``works/<book>/outlines/chNNN.json``（outline.schema.json）：摘要、情節點、出場角色。
- ``works/<book>/cast.json``：新角色加進去（含 ``one_line``）。

標註（``t2vn.annotate``）會自動讀這兩樣當分場與說話者的參考。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .annotate import AnnotateError, merge_new_characters, parse_response
from .errors import JobStopped, T2vnError
from .llm import LlmError, LlmFn, RunHooks, call_llm

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT_FILE = REPO_ROOT / "prompts" / "extract_system.md"
PLAYER_ID = "player"


class ExtractError(T2vnError):
    """抽取失敗（含原始回應）。"""

    def __init__(self, message: str, issues: list[str] | None = None, raw: list[str] | None = None):
        super().__init__(message)
        self.issues = issues or []
        self.raw = raw or []


@dataclass
class ExtractResult:
    outline: dict[str, Any]
    cast_doc: dict[str, Any]
    added: list[str]
    rounds: int
    raw: list[str] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)


def build_messages(chapter: dict[str, Any], cast_doc: dict[str, Any], previous_summary: str = "",
                   system_prompt_file: str | Path = SYSTEM_PROMPT_FILE) -> list[dict[str, str]]:
    system = Path(system_prompt_file).read_text(encoding="utf-8")
    cast_lines = []
    for char_id, char in cast_doc["characters"].items():
        if char_id == PLAYER_ID:
            continue
        entry = {"id": char_id, "name": next(iter(char["names"].values())), "aliases": char.get("aliases", []), "role": char["role"]}
        cast_lines.append(json.dumps(entry, ensure_ascii=False))
    parts = [
        f"# 本章\nchapter_id: {chapter['chapter_id']}\n章名：{chapter['title']}\n段落數：{len(chapter['paragraphs'])}",
        "# 角色表（已知的人，不要重複新增）\n" + ("\n".join(cast_lines) or "（尚無）"),
    ]
    if previous_summary:
        parts.append("# 上一章摘要\n" + previous_summary)
    parts.append("# 正文\n" + "\n".join(f"[{p['para_no']}] {p['text']}" for p in chapter["paragraphs"]))
    parts.append("請依規則輸出 JSON。")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


def check_response(data: dict[str, Any], paragraph_count: int) -> list[str]:
    """回傳問題清單（空＝可用）。"""
    issues: list[str] = []
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        issues.append("summary must be a non-empty string")
    points = data.get("plot_points")
    if not isinstance(points, list) or not points or not all(isinstance(p, str) and p.strip() for p in points):
        issues.append("plot_points must be a non-empty list of strings")
    names = data.get("cast_names", [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        issues.append("cast_names must be a list of strings")
    return issues


def extract_chapter(chapter: dict[str, Any], cast_doc: dict[str, Any], llm: LlmFn, book_id: str, lang: str,
                    previous_summary: str = "", max_repairs: int = 2, hooks: RunHooks | None = None) -> ExtractResult:
    messages = build_messages(chapter, cast_doc, previous_summary)
    raw: list[str] = []
    usage: list[dict[str, Any]] = []
    issues: list[str] = []
    for round_no in range(1, max_repairs + 2):
        try:
            result = call_llm(llm, messages, hooks, round_no)
        except LlmError as exc:
            raise ExtractError(f"LLM call failed: {exc}", issues, raw) from exc
        raw.append(result.text)
        usage.append(dict(result.usage))
        try:
            data = parse_response(result.text)
        except AnnotateError as exc:
            issues = [str(exc)]
        else:
            issues = check_response(data, len(chapter["paragraphs"]))
            added: list[str] = []
            new_cast = cast_doc
            if not issues:
                new_cast, added, issues = merge_new_characters(cast_doc, data.get("new_characters"), chapter["chapter_id"],
                                                               lang, None)
            if not issues:
                for entry in data.get("new_characters") or []:
                    one_line = entry.get("one_line") if isinstance(entry, dict) else None
                    if entry.get("id") in added and isinstance(one_line, str) and one_line.strip():
                        new_cast["characters"][entry["id"]]["one_line"] = {lang: one_line.strip()}
                sections = {"plot_points": "\n".join(f"- {p.strip()}" for p in data["plot_points"])}
                if data.get("cast_names"):
                    sections["cast_names"] = "、".join(n.strip() for n in data["cast_names"] if n.strip())
                outline = {
                    "schema_version": "0.1", "book_id": book_id, "chapter_id": chapter["chapter_id"],
                    "chapter_no": chapter["chapter_no"], "summary": data["summary"].strip(), "sections": sections,
                    "origin": "llm_extract",
                }
                return ExtractResult(outline, new_cast, added, round_no, raw, usage)
        if hooks:
            hooks.on_event("repair", {"round": round_no, "issues": issues[:5], "count": len(issues)})
        messages = messages + [
            {"role": "assistant", "content": result.text},
            {"role": "user", "content": "上面的輸出有以下問題。請逐條修正並輸出完整 JSON：\n" + "\n".join(f"- {i}" for i in issues[:20])},
        ]
    raise ExtractError(f"still invalid after {max_repairs} repair round(s)", issues, raw)


def extract_book(work: Path, numbers: list[int], llm: LlmFn, book_id: str, lang: str, force: bool = False,
                 log: Callable[[str], None] = lambda text: None, should_stop: Callable[[], bool] = lambda: False,
                 max_repairs: int = 2, hooks: RunHooks | None = None,
                 on_chapter: Callable[[int, str, str], None] = lambda number, status, note: None) -> dict[str, Any]:
    """逐章抽取。已有 outlines/chNNN.json 且沒有 force 的章跳過（可續跑）。回傳摘要。"""
    from .schema_check import validate_data

    cast_path = work / "cast.json"
    cast_doc = json.loads(cast_path.read_text(encoding="utf-8"))
    done, failed, skipped, tokens = [], [], [], 0
    previous = ""
    hooks = hooks or RunHooks(should_stop=should_stop)
    for number in sorted(numbers):
        if hooks.should_stop():
            log("stopped")
            break
        on_chapter(number, "running", "")
        chapter_id = f"ch{number:03d}"
        out_path = work / "outlines" / f"{chapter_id}.json"
        if out_path.exists() and not force:
            try:
                previous = json.loads(out_path.read_text(encoding="utf-8")).get("summary", previous)
            except (OSError, ValueError):
                pass
            skipped.append(number)
            on_chapter(number, "skipped", "outline exists")
            log(f"{chapter_id}: skip (outline exists)")
            continue
        chapter_path = work / "chapters" / f"{chapter_id}.json"
        if not chapter_path.is_file():
            failed.append(number)
            on_chapter(number, "error", "chapter file not found")
            log(f"{chapter_id}: error — chapter file not found")
            continue
        chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
        log(f"{chapter_id}: extracting {len(chapter['paragraphs'])} paragraphs ...")
        try:
            result = extract_chapter(chapter, cast_doc, llm, book_id, lang, previous, max_repairs, hooks)
        except JobStopped:
            on_chapter(number, "stopped", "")
            log(f"{chapter_id}: stopped")
            break
        except ExtractError as exc:
            raw_dir = work / "outlines" / "_raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            for index, text in enumerate(exc.raw, 1):
                (raw_dir / f"{chapter_id}_r{index}.json").write_text(text, encoding="utf-8")
            failed.append(number)
            on_chapter(number, "error", str(exc))
            log(f"{chapter_id}: error — {exc}")
            for issue in exc.issues[:5]:
                log(f"  - {issue}")
            continue
        problems = validate_data(result.outline, "outline") + validate_data(result.cast_doc, "cast")
        if problems:
            failed.append(number)
            on_chapter(number, "error", "schema check failed")
            log(f"{chapter_id}: error — schema check: {problems[0].path} {problems[0].message}")
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result.outline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if result.added:
            cast_doc = result.cast_doc
            cast_path.write_text(json.dumps(cast_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        previous = result.outline["summary"]
        used = sum(u.get("prompt_tokens", 0) + u.get("completion_tokens", 0) for u in result.usage)
        tokens += used
        done.append(number)
        on_chapter(number, "done", f"{len(result.added)} new characters")
        log(f"{chapter_id}: ok — {len(result.outline['sections']['plot_points'].splitlines())} plot points, "
            f"{len(result.added)} new character(s), rounds {result.rounds}, tokens {used}")
    return {"done": done, "failed": failed, "skipped": skipped, "tokens": tokens}
