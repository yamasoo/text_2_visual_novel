"""把書目錄的章節正文正規化成帶段落編號的 JSON。

這是 text_2_visual_novel 管線的第一步（任務卡 t_s1_normalize_book）：

- 輸入：書目錄底下的章節正文 Markdown（唯讀，本模組絕不寫入書目錄）。
- 輸出：``<out_dir>/chapters/chNNN.json`` 與 ``<out_dir>/chapters/index.json``，
  可選輸出給 ruxi ``scan_scenes.py`` 使用的 ``<out_dir>/chapters/book.txt``。

章節正文資料夾名稱、檔名／標題／分場符號的正規表達式、``book.txt`` 的章標題格式，
一律從 ``config/source_field_map.json`` 讀取；程式碼裡不寫死任何中文路徑或格式。

用法::

    python tools/normalize_book.py --book-dir <書目錄> --book-id sample_book \\
        --out <輸出目錄> [--chapters 1-5] [--txt] [--field-map <path>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Pattern, Sequence

# <repo>/tools/normalize_book.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIELD_MAP: Path = REPO_ROOT / "config" / "source_field_map.json"

SCHEMA_VERSION = "0.1"
CHAPTERS_DIR_NAME = "chapters"
INDEX_FILE_NAME = "index.json"
TXT_FILE_NAME = "book.txt"
CHAPTER_ID_FORMAT = "ch{chapter_no:03d}"
CHAPTER_FILE_FORMAT = "chapters/{chapter_id}.json"
MARKDOWN_SUFFIX = ".md"
MIN_CHAPTER_NO = 1
MAX_CHAPTER_NO = 999

BOOK_ID_PATTERN = r"^[a-z][a-z0-9_]{1,30}$"
CHAPTER_RANGE_PATTERN = re.compile(r"^([0-9]+)-([0-9]+)$")
_BOOK_ID_RE = re.compile(BOOK_ID_PATTERN)


class NormalizeError(Exception):
    """所有可預期的錯誤都丟這個例外，訊息要寫明是哪個檔案、什麼問題。"""


@dataclass(frozen=True)
class _Rules:
    """從 ``source_field_map.json`` 讀出來、這個模組會用到的規則。"""

    chapter_text_dir: str
    title_pattern: Pattern[str]
    file_pattern: Pattern[str]
    scene_break_pattern: Pattern[str]
    txt_chapter_heading: str


@dataclass(frozen=True)
class _ChapterSource:
    """一個待處理的章節檔（還沒解析內容）。"""

    chapter_no: int
    path: Path
    rel_path: str
    file_title: str


# --------------------------------------------------------------------- field map


def _require_str(mapping: Mapping[str, Any], key: str, field_map_path: Path) -> str:
    """從 mapping 取一個非空字串欄位，缺少或型別不對就丟 NormalizeError。"""
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise NormalizeError(f"欄位對照檔 {field_map_path} 缺少字串欄位 {key!r}")
    return value


def _compile(
    pattern: str, key: str, field_map_path: Path, min_groups: int = 1
) -> Pattern[str]:
    """編譯 field map 裡的正規表達式，格式錯誤時轉成 NormalizeError。"""
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise NormalizeError(
            f"欄位對照檔 {field_map_path} 的 {key!r} 不是合法的正規表達式：{exc}"
        ) from exc
    if compiled.groups < min_groups:
        raise NormalizeError(
            f"欄位對照檔 {field_map_path} 的 {key!r} 至少需要 {min_groups} 個 capture group"
        )
    return compiled


def _load_field_map(field_map_path: str | Path) -> _Rules:
    """讀取並驗證欄位對照檔，回傳本模組需要的規則。"""
    path = Path(field_map_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NormalizeError(f"無法讀取欄位對照檔 {path}：{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NormalizeError(
            f"欄位對照檔 {path} 不是合法 JSON（第 {exc.lineno} 行）：{exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise NormalizeError(f"欄位對照檔 {path} 的最外層必須是 JSON 物件")

    source_paths = data.get("source_paths")
    if not isinstance(source_paths, dict):
        raise NormalizeError(f"欄位對照檔 {path} 缺少物件欄位 'source_paths'")
    chapter_text_dir = _require_str(source_paths, "chapter_text_dir", path)

    title_pattern = _compile(
        _require_str(data, "chapter_text_title_pattern", path),
        "chapter_text_title_pattern",
        path,
        min_groups=2,
    )
    file_pattern = _compile(
        _require_str(data, "chapter_text_file_pattern", path),
        "chapter_text_file_pattern",
        path,
        min_groups=2,
    )
    scene_break_pattern = _compile(
        _require_str(data, "scene_break_pattern", path),
        "scene_break_pattern",
        path,
        min_groups=1,
    )
    txt_chapter_heading = _require_str(data, "txt_chapter_heading", path)
    if "{chapter_no" not in txt_chapter_heading:
        raise NormalizeError(
            f"欄位對照檔 {path} 的 'txt_chapter_heading' 必須含 {{chapter_no}} 佔位符"
        )

    return _Rules(
        chapter_text_dir=chapter_text_dir,
        title_pattern=title_pattern,
        file_pattern=file_pattern,
        scene_break_pattern=scene_break_pattern,
        txt_chapter_heading=txt_chapter_heading,
    )


# ------------------------------------------------------------------------ helpers


def _chapter_id(chapter_no: int) -> str:
    """章號轉章節 ID，例如 7 -> ``ch007``。"""
    return CHAPTER_ID_FORMAT.format(chapter_no=chapter_no)


def _group(match: re.Match[str], index: int) -> str:
    """取 capture group，沒有這個 group 或沒抓到時回傳空字串。"""
    if match.re.groups < index:
        return ""
    value = match.group(index)
    return value if value is not None else ""


def _group_int(match: re.Match[str], index: int, source: str) -> int:
    """取 capture group 並轉成整數，抓到的不是數字時丟 NormalizeError。"""
    raw = _group(match, index)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise NormalizeError(
            f"{source}：章號 capture group 抓到非數字內容 {raw!r}（請檢查欄位對照檔的格式）"
        ) from exc


def _parse_chapter_range(spec: str) -> tuple[int, int]:
    """把 CLI 的 ``起-迄`` 字串解析成 (起, 迄)，格式錯誤丟 NormalizeError。"""
    text = spec.strip()
    match = CHAPTER_RANGE_PATTERN.match(text)
    if match is None:
        raise NormalizeError(f"--chapters 格式必須是 起-迄（例如 1-5），收到：{spec!r}")
    start = int(match.group(1))
    end = int(match.group(2))
    if start > end:
        raise NormalizeError(f"--chapters 的起章號不可大於迄章號，收到：{spec!r}")
    return start, end


def _check_chapters(chapters: Any) -> tuple[int, int] | None:
    """驗證 API 傳進來的章號範圍（含頭含尾），回傳 (起, 迄) 或 None。"""
    if chapters is None:
        return None
    if not isinstance(chapters, (tuple, list)) or len(chapters) != 2:
        raise NormalizeError(f"chapters 必須是 (起, 迄) 兩個整數，收到：{chapters!r}")
    start, end = chapters
    if not isinstance(start, int) or not isinstance(end, int):
        raise NormalizeError(f"chapters 的起迄必須是整數，收到：{chapters!r}")
    if start > end:
        raise NormalizeError(f"chapters 的起章號不可大於迄章號，收到：{chapters!r}")
    return start, end


def _validate_book_id(book_id: Any) -> None:
    """檢查 book_id 是否符合 ``^[a-z][a-z0-9_]{1,30}$``。"""
    if not isinstance(book_id, str) or _BOOK_ID_RE.match(book_id) is None:
        raise NormalizeError(
            f"book_id 不合法：{book_id!r}（必須符合 {BOOK_ID_PATTERN}）"
        )


def _dump_json(data: Any) -> str:
    """統一的 JSON 序列化格式：不跳脫中文、縮排 2、結尾換行。"""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _write_text_file(path: Path, content: str) -> None:
    """以 UTF-8、強制 LF 換行寫入文字檔。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


# --------------------------------------------------------------- reading the book


def _list_chapter_files(text_dir: Path, rules: _Rules, book_root: Path) -> list[_ChapterSource]:
    """列出章節正文資料夾第一層的章節檔，依章號排序。

    副檔名不是 ``.md`` 的檔案忽略；``.md`` 但檔名不符格式、章號重複、沒有任何章節檔，
    都丟 NormalizeError。
    """
    if not text_dir.is_dir():
        raise NormalizeError(f"找不到章節正文資料夾：{text_dir}")

    found: dict[int, _ChapterSource] = {}
    try:
        entries = sorted(text_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise NormalizeError(f"無法列出章節正文資料夾 {text_dir}：{exc}") from exc
    for entry in entries:
        if not entry.is_file() or entry.suffix != MARKDOWN_SUFFIX:
            continue
        match = rules.file_pattern.match(entry.name)
        if match is None:
            raise NormalizeError(
                f"章節檔名不符合 {rules.file_pattern.pattern!r}：{entry}"
            )
        chapter_no = _group_int(match, 1, str(entry))
        if not MIN_CHAPTER_NO <= chapter_no <= MAX_CHAPTER_NO:
            raise NormalizeError(
                f"章節檔 {entry} 的章號 {chapter_no} 超出範圍 "
                f"{MIN_CHAPTER_NO}-{MAX_CHAPTER_NO}"
            )
        if chapter_no in found:
            raise NormalizeError(
                f"章號 {chapter_no} 重複：{found[chapter_no].path.name} 與 {entry.name}"
            )
        found[chapter_no] = _ChapterSource(
            chapter_no=chapter_no,
            path=entry,
            rel_path=entry.relative_to(book_root).as_posix(),
            file_title=_group(match, 2).strip(),
        )

    if not found:
        raise NormalizeError(f"章節正文資料夾裡找不到任何章節檔（.md）：{text_dir}")
    return [found[number] for number in sorted(found)]


def _read_chapter_text(source: _ChapterSource) -> tuple[str, str]:
    """讀章節檔原始 bytes，回傳 (正規化後的文字, sha256)。"""
    try:
        raw = source.path.read_bytes()
    except OSError as exc:
        raise NormalizeError(f"無法讀取章節檔 {source.path}：{exc}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NormalizeError(f"章節檔 {source.path} 不是合法 UTF-8：{exc}") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, digest


def _parse_chapter(
    source: _ChapterSource, rules: _Rules, book_id: str, text: str, digest: str
) -> dict[str, Any]:
    """把單一章節的文字解析成 §5.3 的 chapter dict。"""
    lines = text.split("\n")

    heading_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if heading_index is None:
        raise NormalizeError(f"章節檔 {source.rel_path} 是空的，找不到章標題行")

    heading_line = lines[heading_index].strip()
    heading_match = rules.title_pattern.match(heading_line)
    if heading_match is None:
        raise NormalizeError(
            f"章節檔 {source.rel_path} 的第一個非空行不是章標題"
            f"（需符合 {rules.title_pattern.pattern!r}）：{heading_line!r}"
        )

    heading_no = _group_int(heading_match, 1, source.rel_path)
    if heading_no != source.chapter_no:
        raise NormalizeError(
            f"章節檔 {source.rel_path} 的標題章號 {heading_no} "
            f"與檔名章號 {source.chapter_no} 不一致"
        )
    title = _group(heading_match, 2).strip() or source.file_title

    paragraphs: list[dict[str, Any]] = []
    scene_breaks: list[int] = []
    pending_break = False

    for raw_line in lines[heading_index + 1 :]:
        line = raw_line.strip()
        if not line:
            continue
        if rules.title_pattern.match(line) is not None:
            raise NormalizeError(
                f"章節檔 {source.rel_path} 的正文出現第二個章標題：{line!r}"
            )
        if rules.scene_break_pattern.match(line) is not None:
            pending_break = True
            continue

        paragraph_text = line
        if line.startswith("#"):
            paragraph_text = line.lstrip("#").strip()
            # 章內小標題：當作分場邊界，若去掉 # 後還有文字就繼續當段落。
            pending_break = True
        if not paragraph_text:
            continue

        para_no = len(paragraphs) + 1
        if pending_break and paragraphs:
            scene_breaks.append(para_no)
        pending_break = False
        paragraphs.append({"para_no": para_no, "text": paragraph_text})

    char_count = sum(len("".join(item["text"].split())) for item in paragraphs)

    return {
        "schema_version": SCHEMA_VERSION,
        "book_id": book_id,
        "chapter_id": _chapter_id(source.chapter_no),
        "chapter_no": source.chapter_no,
        "title": title,
        "source_file": source.rel_path,
        "sha256": digest,
        "char_count": char_count,
        "paragraphs": paragraphs,
        "scene_breaks": scene_breaks,
    }


def _render_txt(chapters: Sequence[Mapping[str, Any]], rules: _Rules) -> str:
    """組出給 ruxi ``scan_scenes.py`` 用的 book.txt 內容。"""
    # ruxi 只認換行後的章標題，所以檔案開頭必須先有一個換行。
    parts: list[str] = ["\n"]
    for chapter in chapters:
        parts.append(rules.txt_chapter_heading.format(chapter_no=chapter["chapter_no"]))
        parts.append("\n")
        parts.append(str(chapter["title"]))
        parts.append("\n\n")
        for paragraph in chapter["paragraphs"]:
            parts.append(str(paragraph["text"]))
            parts.append("\n\n")
    return "".join(parts)


# ------------------------------------------------------------------------- API


def normalize_book(
    book_dir: str | Path,
    book_id: str,
    out_dir: str | Path,
    chapters: tuple[int, int] | None = None,
    write_txt: bool = False,
    field_map_path: str | Path = DEFAULT_FIELD_MAP,
) -> dict:
    """處理整本書，回傳 index（和寫入 index.json 的內容相同）。

    Args:
        book_dir: 書目錄（唯讀，不會被寫入）。
        book_id: 書的 ID，需符合 ``^[a-z][a-z0-9_]{1,30}$``。
        out_dir: 輸出目錄；不可等於或在 ``book_dir`` 底下。
        chapters: 可選的章號範圍 ``(起, 迄)``，含頭含尾。
        write_txt: 是否另外輸出 ``chapters/book.txt``。
        field_map_path: 欄位對照檔路徑，預設 ``<repo>/config/source_field_map.json``。

    Raises:
        NormalizeError: 任何可預期的錯誤（參數、檔案、格式）。
    """
    _validate_book_id(book_id)
    rules = _load_field_map(field_map_path)
    chapter_range = _check_chapters(chapters)

    book_root = Path(book_dir).resolve()
    out_root = Path(out_dir).resolve()
    if out_root == book_root or book_root in out_root.parents:
        raise NormalizeError(
            f"out_dir 不可等於書目錄或在書目錄底下（書目錄唯讀）：{out_root}"
        )

    text_dir = book_root / rules.chapter_text_dir
    sources = _list_chapter_files(text_dir, rules, book_root)
    if chapter_range is not None:
        start, end = chapter_range
        sources = [item for item in sources if start <= item.chapter_no <= end]
    if not sources:
        raise NormalizeError(
            f"章號範圍 {chapter_range!r} 內沒有任何章節檔（{text_dir}）"
        )

    chapters_dir = out_root / CHAPTERS_DIR_NAME
    chapters_dir.mkdir(parents=True, exist_ok=True)

    parsed: list[dict[str, Any]] = []
    index_entries: list[dict[str, Any]] = []
    for source in sources:
        text, digest = _read_chapter_text(source)
        chapter = _parse_chapter(source, rules, book_id, text, digest)
        chapter_file = chapters_dir / f"{chapter['chapter_id']}.json"
        _write_text_file(chapter_file, _dump_json(chapter))
        parsed.append(chapter)
        index_entries.append(
            {
                "chapter_id": chapter["chapter_id"],
                "chapter_no": chapter["chapter_no"],
                "title": chapter["title"],
                "char_count": chapter["char_count"],
                "paragraph_count": len(chapter["paragraphs"]),
                "file": CHAPTER_FILE_FORMAT.format(chapter_id=chapter["chapter_id"]),
            }
        )

    index = {
        "schema_version": SCHEMA_VERSION,
        "book_id": book_id,
        "chapters": index_entries,
    }
    _write_text_file(chapters_dir / INDEX_FILE_NAME, _dump_json(index))
    if write_txt:
        _write_text_file(chapters_dir / TXT_FILE_NAME, _render_txt(parsed, rules))
    return index


# ------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。成功回傳 0；NormalizeError 或參數錯誤時把訊息印到 stderr 並回傳 1。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="normalize_book.py",
        description="把小說章節正規化成帶段落編號的 JSON。",
    )
    parser.add_argument("--book-dir", required=True, help="書目錄（唯讀）")
    parser.add_argument("--book-id", required=True, help="書的 ID，例如 sample_book")
    parser.add_argument("--out", required=True, help="輸出目錄")
    parser.add_argument("--chapters", default=None, help="章號範圍，格式 起-迄，例如 1-5")
    parser.add_argument("--txt", action="store_true", help="另外輸出 chapters/book.txt")
    parser.add_argument(
        "--field-map", dest="field_map", default=None, help="欄位對照檔路徑"
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 自己的錯誤與 --help 都走這裡
        code = exc.code if isinstance(exc.code, int) else 1
        return 0 if code == 0 else 1

    try:
        chapter_range = (
            _parse_chapter_range(args.chapters) if args.chapters is not None else None
        )
        index = normalize_book(
            args.book_dir,
            args.book_id,
            args.out,
            chapters=chapter_range,
            write_txt=args.txt,
            field_map_path=args.field_map or DEFAULT_FIELD_MAP,
        )
    except NormalizeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    print(f"完成：{len(index.get('chapters', []))} 章 -> {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
