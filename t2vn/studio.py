"""校對／編輯介面的後端邏輯（不含 HTTP）。``t2vn/server.py`` 只是它的薄外殼，方便單元測試。

- 書：``works/<book_id>/book.json`` 是唯一的書清單來源（TXT／EPUB 匯入的書沒有書庫資料夾，也不在 config.yaml）。
- 書庫（唯讀）只用來匯入與讀章綱／角色卡；所有寫入都經 ``Config.assert_writable``。
- 編輯的場景檔：``scenes/chNNN.json``，沒有的話用 ``gold/chNNN.json``；存檔前先備份到 ``_history/``。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import annotate, ingest
from .config import Config
from .errors import ConfigError, SourceError, T2vnError
from .schema_check import PLAYER_ID, check_scene_links, validate_data

STATUS_VALUES = ("draft", "reviewed")
_REVIEW_ROW = re.compile(r"^\|\s*sc(\d+)-(\d+)\s*\|.*\|\s*\?\s*\|\s*$")
_POINTER_LINE = re.compile(r"^/scenes/(\d+)(?:/lines/(\d+))?")


class StudioError(T2vnError):
    """UI 操作失敗（找不到書／章、資料不合格式等）。訊息會直接顯示給使用者。"""


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StudioError(f"cannot read {path.name}: {exc}") from exc
    except ValueError as exc:
        raise StudioError(f"{path.name} is not valid JSON: {exc}") from exc


class Studio:
    def __init__(self, config: Config):
        self.config = config
        self.works_dir = config.works_dir

    # ------------------------------------------------------------ 寫入與路徑

    def _write(self, path: Path, text: str) -> None:
        target = self.config.assert_writable(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def work(self, book_id: str) -> Path:
        ingest.validate_book_id(book_id)
        path = self.works_dir / book_id
        if not (path / "book.json").is_file():
            raise StudioError(f"book {book_id!r} not found under works/")
        return path

    def book_json(self, book_id: str) -> dict[str, Any]:
        return _read_json(self.work(book_id) / "book.json")

    def library_dir(self, book_id: str) -> Path | None:
        """書庫裡這本書的資料夾（唯讀）；匯入檔案的書回傳 None。"""
        source = self.book_json(book_id).get("source") or {}
        name = source.get("dir_name")
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return None
        path = self.config.book_library / name
        return path if path.is_dir() else None

    # ------------------------------------------------------------ 書架

    def list_books(self) -> list[dict[str, Any]]:
        books = []
        if self.works_dir.is_dir():
            for book_file in sorted(self.works_dir.glob("*/book.json")):
                try:
                    book = _read_json(book_file)
                except StudioError:
                    continue
                book_id = book.get("book_id", book_file.parent.name)
                chapters = self.chapters(book_id)
                lang = book.get("lang", "zh-TW")
                books.append({
                    "book_id": book_id, "lang": lang,
                    "display_name": (book.get("display_name") or {}).get(lang, book_id),
                    "source_kind": "library" if self.library_dir(book_id) else "import",
                    "chapters": len(chapters),
                    "annotated": sum(1 for c in chapters if c["scenes_state"] != "none"),
                    "reviewed": sum(1 for c in chapters if c["scenes_state"] == "reviewed"),
                })
        return books

    def library_candidates(self) -> list[dict[str, str]]:
        """書庫裡還沒匯入的書（資料夾裡有章節資料夾的才算）。"""
        from .source_reader import load_field_map

        library = self.config.book_library
        if not library.is_dir():
            return []
        text_dir = load_field_map()["source_paths"]["chapter_text_dir"]
        taken = set()
        for book in self.list_books():
            name = (self.book_json(book["book_id"]).get("source") or {}).get("dir_name")
            if name:
                taken.add(name)
        found = []
        for child in sorted(library.iterdir()):
            if child.is_dir() and (child / text_dir).is_dir() and child.name not in taken:
                found.append({"dir_name": child.name, "chapters": sum(1 for _ in (child / text_dir).glob("*.md"))})
        return found

    # ------------------------------------------------------------ 匯入

    def preview_import(self, payload: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
        return ingest.preview(payload, opts)

    def commit_import(self, book_id: str, display_name: str, payload: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
        ingest.validate_book_id(book_id)
        lang = opts.get("lang") or "zh-TW"
        plan = ingest.plan_chapters(payload, opts)
        skip = {int(n) for n in opts.get("skip") or []}
        chosen = [c for c in plan["chapters"] if c["no"] not in skip]
        source_names = [s["name"] for s in plan["sources"]]
        work = self.config.assert_writable(self.works_dir / book_id)
        source = {"kind": "import", "files": source_names}
        titles = {int(k): v for k, v in (opts.get("titles") or {}).items()}
        for c in chosen:
            c["title"] = titles.get(c["no"], c["title"])
            c["source_file"] = ", ".join(source_names)[:200]
        index = ingest.write_book(work, book_id, display_name or book_id, lang, chosen, source)
        return {"book_id": book_id, "chapters": len(index["chapters"])}

    def import_library_book(self, dir_name: str, book_id: str, display_name: str, lang: str = "zh-TW") -> dict[str, Any]:
        """把書庫（唯讀）的書正規化進 works/<book_id>/。"""
        from tools.normalize_book import NormalizeError, normalize_book

        ingest.validate_book_id(book_id)
        if "/" in dir_name or "\\" in dir_name or dir_name in (".", ".."):
            raise StudioError("invalid library folder name")
        book_dir = self.config.book_library / dir_name
        if not book_dir.is_dir():
            raise StudioError(f"library folder not found: {dir_name}")
        work = self.config.assert_writable(self.works_dir / book_id)
        if (work / "book.json").exists():
            raise StudioError(f"works/{book_id} already exists")
        try:
            index = normalize_book(book_dir, book_id, work)
        except NormalizeError as exc:
            raise StudioError(str(exc)) from exc
        book = {"schema_version": "0.1", "book_id": book_id, "display_name": {lang: display_name or dir_name}, "lang": lang,
                "source": {"kind": "library", "dir_name": dir_name}}
        self._write(work / "book.json", _dump(book))
        if not (work / "cast.json").exists():
            cast = {"schema_version": "0.1", "book_id": book_id, "characters": {
                PLAYER_ID: {"names": {lang: "主角（內心）"}, "aliases": [], "role": "player"}}}
            self._write(work / "cast.json", _dump(cast))
        return {"book_id": book_id, "chapters": len(index["chapters"])}

    # ------------------------------------------------------------ 章節

    def _scene_path(self, work: Path, chapter_id: str) -> Path | None:
        for folder in ("scenes", "gold"):
            path = work / folder / f"{chapter_id}.json"
            if path.is_file():
                return path
        return None

    def _status_map(self, work: Path) -> dict[str, str]:
        path = work / "review_state.json"
        if not path.is_file():
            return {}
        try:
            return {k: v.get("status", "draft") for k, v in _read_json(path).get("chapters", {}).items()}
        except (StudioError, AttributeError):
            return {}

    def _outline_origins(self, book_id: str, work: Path) -> dict[int, str]:
        origins: dict[int, str] = {}
        library = self.library_dir(book_id)
        if library:
            from .source_reader import read_all_outlines
            try:
                for outline in read_all_outlines(library):
                    origins[outline.chapter_no] = "library"
            except SourceError:
                pass
        for number in annotate.load_work_outlines(work):
            origins.setdefault(number, "extract")
        return origins

    def chapters(self, book_id: str) -> list[dict[str, Any]]:
        work = self.work(book_id)
        index_path = work / "chapters" / "index.json"
        if not index_path.is_file():
            return []
        statuses = self._status_map(work)
        origins = self._outline_origins(book_id, work)
        rows = []
        for entry in _read_json(index_path)["chapters"]:
            chapter_id = entry["chapter_id"]
            path = self._scene_path(work, chapter_id)
            if path is None:
                state, count = "none", 0
            else:
                state = statuses.get(chapter_id) or ("reviewed" if path.parent.name == "gold" else "draft")
                try:
                    count = len(_read_json(path)["scenes"])
                except (StudioError, KeyError):
                    count = 0
            rows.append({
                "chapter_id": chapter_id, "chapter_no": entry["chapter_no"], "title": entry["title"],
                "char_count": entry["char_count"], "paragraph_count": entry["paragraph_count"],
                "scenes_state": state, "scene_count": count, "outline": origins.get(entry["chapter_no"], "none"),
            })
        return rows

    def chapter_doc(self, book_id: str, number: int) -> dict[str, Any]:
        path = self.work(book_id) / "chapters" / f"ch{number:03d}.json"
        if not path.is_file():
            raise StudioError(f"chapter {number} not found")
        return _read_json(path)

    def get_chapter(self, book_id: str, number: int) -> dict[str, Any]:
        work = self.work(book_id)
        chapter = self.chapter_doc(book_id, number)
        chapter_id = chapter["chapter_id"]
        path = self._scene_path(work, chapter_id)
        doc = _read_json(path) if path else None
        statuses = self._status_map(work)
        status = "none" if path is None else (statuses.get(chapter_id) or ("reviewed" if path.parent.name == "gold" else "draft"))
        flags: list[list[Any]] = []
        review = work / "scenes" / f"{chapter_id}_review.md"
        if doc and review.is_file():
            flags = inferred_flags(review.read_text(encoding="utf-8"), doc)
        locations, bgm = annotate.known_ids(sorted((work / "scenes").glob("ch*.json")) + sorted((work / "gold").glob("ch*.json")))
        outline = annotate.load_work_outlines(work).get(number)
        result = {
            "chapter": chapter, "scenes": doc, "status": status, "file": f"{path.parent.name}/{path.name}" if path else None,
            "flags": flags, "known_locations": locations, "known_bgm": bgm, "outline": outline,
            "issues": self.check_scenes(book_id, number, doc)["issues"] if doc else [],
        }
        return result

    # ------------------------------------------------------------ 角色

    def get_cast(self, book_id: str) -> dict[str, Any]:
        return _read_json(self.work(book_id) / "cast.json")

    def save_cast(self, book_id: str, cast_doc: dict[str, Any]) -> dict[str, Any]:
        work = self.work(book_id)
        issues = validate_data(cast_doc, "cast")
        if issues:
            raise StudioError("cast.json: " + "; ".join(f"{i.path or '/'} {i.message}" for i in issues[:5]))
        if cast_doc["book_id"] != book_id:
            raise StudioError("cast book_id does not match")
        self._backup(work / "cast.json", work / "_history")
        self._write(work / "cast.json", _dump(cast_doc))
        return cast_doc

    # ------------------------------------------------------------ 場景檢查與存檔

    def check_scenes(self, book_id: str, number: int, doc: dict[str, Any]) -> dict[str, Any]:
        """整理後的 doc ＋問題清單。issues：{level: error|warning, scene, line, message}（scene、line 為 0 起的索引或 None）。"""
        chapter = self.chapter_doc(book_id, number)
        cast = self.get_cast(book_id)
        normalized = normalize_scenes(doc, chapter["chapter_id"])
        issues: list[dict[str, Any]] = []
        for issue in validate_data(normalized, "scene"):
            issues.append(pointer_issue("error", issue.path, issue.message))
        if not any(i["level"] == "error" for i in issues):
            for issue in check_scene_links(normalized, cast):
                issues.append(pointer_issue("error", issue.path, issue.message))
        issues += fidelity_issues(normalized, chapter)
        return {"doc": normalized, "issues": issues}

    def save_scenes(self, book_id: str, number: int, doc: dict[str, Any], status: str | None = None) -> dict[str, Any]:
        work = self.work(book_id)
        checked = self.check_scenes(book_id, number, doc)
        errors = [i for i in checked["issues"] if i["level"] == "error"]
        if errors:
            raise StudioError("cannot save: " + "; ".join(i["message"] for i in errors[:3]))
        chapter_id = f"ch{number:03d}"
        path = self._scene_path(work, chapter_id) or (work / "scenes" / f"{chapter_id}.json")
        self._backup(path, path.parent / "_history")
        self._write(path, _dump(checked["doc"]))
        if status:
            self.set_status(book_id, number, status)
        return checked

    def _backup(self, path: Path, history_dir: Path) -> None:
        if path.is_file():
            stamp = time.strftime("%Y%m%d%H%M%S")
            self._write(history_dir / f"{path.stem}_{stamp}{path.suffix}", path.read_text(encoding="utf-8"))

    def set_status(self, book_id: str, number: int, status: str) -> None:
        if status not in STATUS_VALUES:
            raise StudioError(f"status must be one of {STATUS_VALUES}")
        work = self.work(book_id)
        path = work / "review_state.json"
        state = _read_json(path) if path.is_file() else {"schema_version": "0.1", "book_id": book_id, "chapters": {}}
        state["chapters"][f"ch{number:03d}"] = {"status": status, "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._write(path, _dump(state))

    # ------------------------------------------------------------ 章綱

    def save_outline(self, book_id: str, number: int, summary: str, plot_points: str, cast_names: str) -> dict[str, Any]:
        work = self.work(book_id)
        chapter = self.chapter_doc(book_id, number)
        sections = {"plot_points": plot_points.strip()}
        if cast_names.strip():
            sections["cast_names"] = cast_names.strip()
        doc = {"schema_version": "0.1", "book_id": book_id, "chapter_id": chapter["chapter_id"], "chapter_no": number,
               "summary": summary.strip() or "-", "sections": sections, "origin": "manual"}
        issues = validate_data(doc, "outline")
        if issues:
            raise StudioError("outline: " + issues[0].message)
        self._write(work / "outlines" / f"{chapter['chapter_id']}.json", _dump(doc))
        return doc

    # ------------------------------------------------------------ 標註用的上下文

    def annotation_context(self, book_id: str) -> tuple[dict[int, dict[str, str]], dict[str, str], dict[str, str]]:
        """(章綱 sections, 角色備註, 角色定位)：書庫的設定優先，缺的用 works/ 裡抽取或手寫的。"""
        work = self.work(book_id)
        outlines: dict[int, dict[str, str]] = {}
        notes: dict[str, str] = {}
        roles: dict[str, str] = {}
        library = self.library_dir(book_id)
        if library:
            from .source_reader import read_all_outlines, read_characters
            try:
                outlines = {o.chapter_no: o.sections for o in read_all_outlines(library)}
            except SourceError:
                pass
            try:
                for card in read_characters(library):
                    notes[card.name] = card.sections.get("one_line", "")
                    roles[card.name] = card.role
            except SourceError:
                pass
        for number, sections in annotate.load_work_outlines(work).items():
            outlines.setdefault(number, sections)
        lang = self.book_json(book_id).get("lang", "zh-TW")
        for name, text in annotate.cast_notes(self.get_cast(book_id), lang).items():
            notes.setdefault(name, text)
        return outlines, notes, roles

    # ------------------------------------------------------------ 原型

    def build_prototype(self, book_id: str, numbers: list[int] | None = None) -> dict[str, Any]:
        from . import prototype

        work = self.work(book_id)
        wanted = set(numbers or [])
        paths = []
        for entry in self.chapters(book_id):
            if wanted and entry["chapter_no"] not in wanted:
                continue
            path = self._scene_path(work, entry["chapter_id"])
            if path:
                paths.append(path)
        if not paths:
            raise StudioError("no annotated chapters to build")
        out = work / "prototype"
        self.config.assert_writable(out / "x")
        style_file = out / "style.json"
        book = self.book_json(book_id)
        cast = self.get_cast(book_id)
        style = _read_json(style_file) if style_file.is_file() else prototype.default_style(book, cast)
        docs = [_read_json(p) for p in paths]
        added = prototype.complete_style(style, docs)
        if added or not style_file.is_file():
            self._write(style_file, _dump(style))
        try:
            summaries = prototype.build_book_prototype(paths, work, out, style_file)
        except prototype.PrototypeError as exc:
            raise StudioError(str(exc)) from exc
        self.config.assert_writable(out / summaries[0]["html"])
        return {"files": [s["html"] for s in summaries], "scenes": summaries[0]["scenes"], "lines": summaries[0]["lines"],
                "warnings": summaries[0]["warnings"], "style_added": added}


# ---------------------------------------------------------------- 場景整理與檢查（純函式）


def normalize_scenes(doc: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    """依目前順序重編 scene_id、修正 next、補背景 id 與出場名單，去掉 UI 的暫存欄位。"""
    result = json.loads(json.dumps(doc))
    scenes = result.get("scenes", [])
    old_ids = [s.get("scene_id") for s in scenes]
    new_ids = [f"{chapter_id}_sc{i:02d}" for i in range(1, len(scenes) + 1)]
    remap = dict(zip(old_ids, new_ids))
    for index, scene in enumerate(scenes):
        old_next = scene.get("next")
        following_old = old_ids[index + 1] if index + 1 < len(old_ids) else None
        following_new = new_ids[index + 1] if index + 1 < len(new_ids) else None
        same_chapter_gone = isinstance(old_next, str) and old_next.startswith(chapter_id + "_") and old_next not in remap
        scene["scene_id"] = new_ids[index]
        if old_next is None or old_next == following_old or same_chapter_gone:
            scene["next"] = following_new
        else:
            scene["next"] = remap.get(old_next, old_next)
        for key in [k for k in scene if k.startswith("_")]:
            del scene[key]
        location, time_of_day = scene.get("location_id", ""), scene.get("time_of_day", "")
        asset = (scene.get("bg") or {}).get("asset_id", "")
        if not asset or asset.startswith("bg_"):
            scene["bg"] = {"asset_id": f"bg_{location}_{time_of_day}"}
        for key in ("weather", "bgm", "ambience"):
            if scene.get(key) in ("", None) and key in scene:
                del scene[key]
        scene.setdefault("cast", [])
        scene.setdefault("lines", [])
        for line in scene["lines"]:
            for key in [k for k in line if k.startswith("_")]:
                del line[key]
            for option in line.get("options", []) if line.get("type") == "choice" else []:
                if option.get("goto") in remap:
                    option["goto"] = remap[option["goto"]]
        if scene["lines"]:
            annotate._add_missing_speakers(scene)
    paras = [line["source_para"] for s in scenes for line in s["lines"] if "source_para" in line]
    result["chapter_id"] = chapter_id
    result["source"] = {"file_key": "chapter_text", "paragraphs": [min(paras), max(paras)] if paras else [1, 1]}
    return result


def pointer_issue(level: str, pointer: str, message: str) -> dict[str, Any]:
    match = _POINTER_LINE.match(pointer or "")
    scene = int(match.group(1)) if match else None
    line = int(match.group(2)) if match and match.group(2) is not None else None
    return {"level": level, "scene": scene, "line": line, "message": f"{pointer or '/'}: {message}" if not match else message}


def fidelity_issues(doc: dict[str, Any], chapter: dict[str, Any]) -> list[dict[str, Any]]:
    """警告（不擋存檔）：旁白／台詞找不到原文、段落沒被用到、段落順序倒退。"""
    paragraphs = {p["para_no"]: p["text"] for p in chapter["paragraphs"]}
    issues: list[dict[str, Any]] = []
    used: set[int] = set()
    previous = 0
    for s_index, scene in enumerate(doc.get("scenes", [])):
        for l_index, line in enumerate(scene.get("lines", [])):
            number = line.get("source_para")
            if line.get("type") not in ("narration", "dialogue") or number is None:
                continue
            if number not in paragraphs:
                issues.append({"level": "warning", "scene": s_index, "line": l_index, "message": f"source paragraph {number} does not exist"})
                continue
            used.add(number)
            if number < previous:
                issues.append({"level": "warning", "scene": s_index, "line": l_index, "message": f"paragraph order goes back ({number} after {previous})"})
            previous = max(previous, number)
            bad = annotate.unfaithful_clauses(line.get("text", ""), paragraphs[number])
            if bad:
                issues.append({"level": "warning", "scene": s_index, "line": l_index,
                               "message": "not found in the source paragraph: " + " / ".join(bad[:2])})
    missing = sorted(set(paragraphs) - used)
    if missing:
        issues.append({"level": "warning", "scene": None, "line": None, "message": f"{len(missing)} paragraph(s) unused: " + annotate._ranges(missing)})
    return issues


def inferred_flags(review_md: str, doc: dict[str, Any]) -> list[list[Any]]:
    """從校對表（`| scNN-NN | ... | ? |`）取出推斷說話者的行，回傳 [scene_index, line_index]（0 起）。"""
    ids = {s["scene_id"].split("_")[-1]: i for i, s in enumerate(doc.get("scenes", []))}
    flags = []
    for row in review_md.splitlines():
        match = _REVIEW_ROW.match(row)
        if match:
            scene_index = ids.get(f"sc{match.group(1)}")
            if scene_index is not None:
                flags.append([scene_index, int(match.group(2)) - 1])
    return flags
