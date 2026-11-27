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

from . import annotate, history, ingest
from .atomic import write_atomic
from .config import Config
from .errors import ConfigError, SourceError, T2vnError
from .schema_check import PLAYER_ID, check_scene_links, validate_data

STATUS_VALUES = ("draft", "reviewed")
# 新書預設的語音引擎（見 Studio._ensure_cast_voice）：speaker_id 走 sherpa-onnx。
NEW_BOOK_ENGINE = "sherpa-onnx"
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
        write_atomic(target, text)

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
                source_dir = str((book.get("source") or {}).get("dir_name") or "")
                books.append({
                    "book_id": book_id, "lang": lang,
                    "display_name": (book.get("display_name") or {}).get(lang, book_id),
                    "source_kind": "library" if self.library_dir(book_id) else "import",
                    "source_dir": source_dir,
                    "chapters": len(chapters),
                    "annotated": sum(1 for c in chapters if c["scenes_state"] != "none"),
                    "reviewed": sum(1 for c in chapters if c["scenes_state"] == "reviewed"),
                })
        return books

    def suggest_book_id(self, hint: str = "") -> str:
        """依書名／資料夾名產生合法且不撞名的 ``book_id``（``^[a-z][a-z0-9_]{1,30}$``）。"""
        import hashlib

        raw = str(hint or "").strip()
        slug = re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_")
        if slug and slug[0].isalpha() and len(slug) >= 2:
            base = slug[:24].rstrip("_")
        elif slug:
            base = f"b_{slug}"[:24].rstrip("_")
        else:
            digest = hashlib.sha1((raw or "book").encode("utf-8")).hexdigest()[:6]
            base = f"book_{digest}"
        if len(base) < 2:
            base = f"{base}_"
        candidate = base
        suffix = 2
        while (self.works_dir / candidate).exists():
            tail = f"_{suffix}"
            candidate = base[: 31 - len(tail)].rstrip("_") + tail
            suffix += 1
        return candidate

    def library_candidates(self) -> list[dict[str, Any]]:
        """書庫裡還沒匯入的書（支援 novel-studio v3 與舊 v2 格式）。"""
        from .source_reader import detect_layout, load_field_map, read_book_meta, source_paths_for

        library = self.config.book_library
        if not library.is_dir():
            return []
        fm = load_field_map()
        taken = set()
        for book in self.list_books():
            name = (self.book_json(book["book_id"]).get("source") or {}).get("dir_name")
            if name:
                taken.add(name)
        found = []
        for child in sorted(library.iterdir()):
            if not child.is_dir() or child.name in taken:
                continue
            paths = source_paths_for(child, fm)
            text_dir = child / paths["chapter_text_dir"]
            if not text_dir.is_dir():
                continue
            meta = read_book_meta(child)
            vc_dir = child / paths.get("visual_card_dir", "bible/visual-cards")
            title = str(meta.get("title") or "").strip()
            found.append({
                "dir_name": child.name,
                "chapters": sum(1 for _ in text_dir.glob("*.md")),
                "layout": detect_layout(child, fm),
                "title": title,
                "lang": str(meta.get("lang") or "").strip(),
                "visual_cards": sum(1 for _ in vc_dir.glob("*.md")) if vc_dir.is_dir() else 0,
                "suggested_id": self.suggest_book_id(child.name or title),
            })
        return found

    # ------------------------------------------------------------ 匯入

    def preview_import(self, payload: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
        return ingest.preview(payload, opts)

    def _init_default_comic(self, book_id: str, title: str) -> str | None:
        """匯入書時順手建立預設漫畫專案（``main``）並同步角色卡。失敗不阻斷匯入。"""
        from . import comic

        try:
            comic.create_project(book_id, "main", title or book_id, config=self.config)
            comic.bible_cards(book_id, "main", overwrite=False, config=self.config)
            return "main"
        except Exception:  # noqa: BLE001
            return None

    def commit_import(self, book_id: str, display_name: str, payload: dict[str, Any], opts: dict[str, Any],
                      *, init_comic: bool = False) -> dict[str, Any]:
        lang = opts.get("lang") or "zh-TW"
        plan = ingest.plan_chapters(payload, opts)
        skip = {int(n) for n in opts.get("skip") or []}
        chosen = [c for c in plan["chapters"] if c["no"] not in skip]
        source_names = [s["name"] for s in plan["sources"]]
        resolved_id = str(book_id or "").strip() or self.suggest_book_id(display_name or (source_names[0] if source_names else ""))
        ingest.validate_book_id(resolved_id)
        work = self.config.assert_writable(self.works_dir / resolved_id)
        source = {"kind": "import", "files": source_names}
        titles = {int(k): v for k, v in (opts.get("titles") or {}).items()}
        for c in chosen:
            c["title"] = titles.get(c["no"], c["title"])
            c["source_file"] = ", ".join(source_names)[:200]
        resolved_name = (display_name or "").strip() or (Path(source_names[0]).stem if source_names else resolved_id)
        index = ingest.write_book(work, resolved_id, resolved_name, lang, chosen, source)
        self._ensure_cast_voice(resolved_id, work)
        out: dict[str, Any] = {"book_id": resolved_id, "chapters": len(index["chapters"])}
        if init_comic:
            out["comic_project"] = self._init_default_comic(resolved_id, resolved_name)
        return out

    def _ensure_cast_voice(self, book_id: str, work: Path) -> None:
        """建書時先寫一份最小 cast_voice.json（引擎預設 sherpa-onnx）。

        沒有這個檔時語音頁的 roster／lines 算得出來（見 voice_gen.overview），但「套用模型」的存檔
        會因為 characters 是空的被擋下；先放一個空殼，使用者就能直接配聲線。
        """
        from . import voice_lines

        if voice_lines.cast_voice_path(work).exists():
            return
        doc = {"schema_version": voice_lines.SCHEMA_VERSION, "book_id": book_id,
               "engine": NEW_BOOK_ENGINE, "characters": {}}
        voice_lines.save_cast_voice(self.config, work, doc)

    def import_library_book(self, dir_name: str, book_id: str = "", display_name: str = "", lang: str = "",
                            *, init_comic: bool = False) -> dict[str, Any]:
        """把書庫（唯讀）的書正規化進 works/<book_id>/；若書庫有視覺卡會自動同步。

        ``book_id`` 留空時自動依資料夾名／書名產生；``init_comic=True`` 時同步建立預設漫畫專案 ``main``。
        """
        from tools.normalize_book import NormalizeError, normalize_book

        from . import source_reader, visual_card_import

        if "/" in dir_name or "\\" in dir_name or dir_name in (".", "..") or not dir_name:
            raise StudioError("invalid library folder name")
        book_dir = self.config.book_library / dir_name
        if not book_dir.is_dir():
            raise StudioError(f"library folder not found: {dir_name}")
        meta = source_reader.read_book_meta(book_dir)
        resolved_lang = (lang or "").strip() or str(meta.get("lang") or "zh-TW")
        resolved_name = (display_name or "").strip() or str(meta.get("title") or "").strip() or dir_name
        resolved_id = str(book_id or "").strip() or self.suggest_book_id(dir_name or resolved_name)
        ingest.validate_book_id(resolved_id)
        work = self.config.assert_writable(self.works_dir / resolved_id)
        if (work / "book.json").exists():
            raise StudioError(f"works/{resolved_id} already exists")
        try:
            index = normalize_book(book_dir, resolved_id, work)
        except NormalizeError as exc:
            raise StudioError(str(exc)) from exc
        book = {"schema_version": "0.1", "book_id": resolved_id, "display_name": {resolved_lang: resolved_name}, "lang": resolved_lang,
                "source": {"kind": "library", "dir_name": dir_name}}
        self._write(work / "book.json", _dump(book))
        if not (work / "cast.json").exists():
            cast = {"schema_version": "0.1", "book_id": resolved_id, "characters": {
                PLAYER_ID: {"names": {resolved_lang: "主角（內心）"}, "aliases": [], "role": "player"}}}
            self._write(work / "cast.json", _dump(cast))
        self._ensure_cast_voice(resolved_id, work)
        vc_report = visual_card_import.sync_visual_cards(
            book_dir, work, self.config.assert_writable, lang=resolved_lang, overwrite=False
        )
        out: dict[str, Any] = {"book_id": resolved_id, "chapters": len(index["chapters"]), "visual_cards": vc_report}
        if init_comic:
            out["comic_project"] = self._init_default_comic(resolved_id, resolved_name)
        return out

    def _sync_comic_bibles(self, book_id: str) -> list[str]:
        """若這本書已有漫畫專案，自動重跑 ``bible_cards(overwrite=True)`` 同步角色卡與地點卡。"""
        from . import comic

        synced: list[str] = []
        try:
            projects = comic.list_projects(book_id, self.config)
        except Exception:  # noqa: BLE001
            return synced
        for proj in projects:
            if proj.get("broken"):
                continue
            pid = str(proj.get("project_id") or "").strip()
            if not pid:
                continue
            try:
                comic.bible_cards(book_id, pid, overwrite=True, config=self.config)
                synced.append(pid)
            except Exception:  # noqa: BLE001
                continue
        return synced

    def sync_library_visual_cards(self, book_id: str, *, overwrite: bool = False) -> dict[str, Any]:
        """從這本書對應的唯讀書庫同步 ``bible/visual-cards/*.md``，並同步更新既有漫畫專案角色卡。"""
        from . import visual_card_import

        work = self.work(book_id)
        book_dir = self.library_dir(book_id)
        if book_dir is None:
            raise StudioError("這本書沒有對應的書庫資料夾，無法同步視覺卡")
        lang = self.book_json(book_id).get("lang", "zh-TW")
        report = visual_card_import.sync_visual_cards(
            book_dir, work, self.config.assert_writable, lang=lang, overwrite=overwrite
        )
        report["comic_projects_synced"] = self._sync_comic_bibles(book_id)
        return report

    def sync_library_locations(self, book_id: str, *, overwrite: bool = False) -> dict[str, Any]:
        """從這本書對應的唯讀書庫同步世界觀地點到 ``bible.json`` 與 ``art/style.json``，並同步更新既有漫畫專案地點卡。"""
        from . import visual_card_import

        work = self.work(book_id)
        book_dir = self.library_dir(book_id)
        if book_dir is None:
            raise StudioError("這本書沒有對應的書庫資料夾，無法同步地點")
        report = visual_card_import.sync_world_locations(
            book_dir, work, self.config.assert_writable, overwrite=overwrite
        )
        report["comic_projects_synced"] = self._sync_comic_bibles(book_id)
        return report

    def check_library_chapters(self, book_id: str) -> dict[str, Any]:
        """唯讀比對書庫正文與已匯入的 ``chapters/``，回傳新增與已修訂章節清單。"""
        from tools import normalize_book as nb

        work = self.work(book_id)
        book_dir = self.library_dir(book_id)
        if book_dir is None:
            return {"has_library": False, "added": [], "modified": [], "unchanged": 0}
        try:
            rules = nb._load_field_map(nb.DEFAULT_FIELD_MAP, book_root=book_dir)
            sources = nb._list_chapter_files(book_dir / rules.chapter_text_dir, rules, book_dir)
        except nb.NormalizeError as exc:
            raise StudioError(str(exc)) from exc

        chapters_dir = work / nb.CHAPTERS_DIR_NAME
        added: list[dict[str, Any]] = []
        modified: list[dict[str, Any]] = []
        unchanged = 0
        for source in sources:
            cid = f"ch{source.chapter_no:03d}"
            existing_path = chapters_dir / f"{cid}.json"
            text, digest = nb._read_chapter_text(source)
            parsed = nb._parse_chapter(source, rules, book_id, text, digest)
            if not existing_path.is_file():
                added.append({
                    "chapter_no": source.chapter_no,
                    "chapter_id": cid,
                    "title": parsed["title"],
                    "char_count": parsed["char_count"],
                })
                continue
            try:
                old = _read_json(existing_path)
            except StudioError:
                old = {}
            if old.get("sha256") != digest:
                modified.append({
                    "chapter_no": source.chapter_no,
                    "chapter_id": cid,
                    "title": parsed["title"],
                    "char_count": parsed["char_count"],
                    "has_scenes": self._scene_path(work, cid) is not None,
                })
            else:
                unchanged += 1
        return {"has_library": True, "added": added, "modified": modified, "unchanged": unchanged}

    def sync_library_chapters(self, book_id: str, *, include_modified: bool = False) -> dict[str, Any]:
        """從書庫增量同步新章節（選填同步原稿已修訂舊章），絕不改動既有 ``scenes/`` 與 ``gold/``。"""
        from tools import normalize_book as nb

        work = self.config.assert_writable(self.work(book_id))
        book_dir = self.library_dir(book_id)
        if book_dir is None:
            raise StudioError("這本書沒有對應的書庫資料夾，無法同步章節")
        try:
            rules = nb._load_field_map(nb.DEFAULT_FIELD_MAP, book_root=book_dir)
            sources = nb._list_chapter_files(book_dir / rules.chapter_text_dir, rules, book_dir)
        except nb.NormalizeError as exc:
            raise StudioError(str(exc)) from exc

        chapters_dir = work / nb.CHAPTERS_DIR_NAME
        chapters_dir.mkdir(parents=True, exist_ok=True)
        index_path = chapters_dir / nb.INDEX_FILE_NAME
        existing_index = _read_json(index_path) if index_path.is_file() else {"chapters": []}
        by_no: dict[int, dict[str, Any]] = {
            int(c["chapter_no"]): dict(c) for c in (existing_index.get("chapters") or []) if "chapter_no" in c
        }

        added: list[int] = []
        updated: list[int] = []
        skipped_modified: list[int] = []

        for source in sources:
            cid = f"ch{source.chapter_no:03d}"
            existing_path = chapters_dir / f"{cid}.json"
            text, digest = nb._read_chapter_text(source)
            is_new = not existing_path.is_file()
            if not is_new:
                try:
                    old = _read_json(existing_path)
                except StudioError:
                    old = {}
                if old.get("sha256") == digest:
                    continue
                if not include_modified:
                    skipped_modified.append(source.chapter_no)
                    continue
            chapter = nb._parse_chapter(source, rules, book_id, text, digest)
            self._write(existing_path, _dump(chapter))
            by_no[source.chapter_no] = {
                "chapter_id": chapter["chapter_id"],
                "chapter_no": chapter["chapter_no"],
                "title": chapter["title"],
                "char_count": chapter["char_count"],
                "paragraph_count": len(chapter["paragraphs"]),
                "file": nb.CHAPTER_FILE_FORMAT.format(chapter_id=chapter["chapter_id"]),
            }
            if is_new:
                added.append(source.chapter_no)
            else:
                updated.append(source.chapter_no)

        if added or updated:
            new_index = {
                "schema_version": nb.SCHEMA_VERSION,
                "book_id": book_id,
                "chapters": [by_no[n] for n in sorted(by_no)],
            }
            self._write(index_path, _dump(new_index))

        return {
            "added": added,
            "updated": updated,
            "skipped_modified": skipped_modified,
            "total": len(by_no),
        }

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

    def _backup(self, path: Path, history_dir: Path, keep: int = history.DEFAULT_MAX_HISTORY) -> None:
        if path.is_file():
            stamp = time.strftime("%Y%m%d%H%M%S")
            self._write(history_dir / f"{path.stem}_{stamp}{path.suffix}", path.read_text(encoding="utf-8"))
            if history_dir == path.parent / history.HISTORY_DIR_NAME:
                history.prune_history(path, keep)

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
