"""把書匯入 ``works/<book_id>/``：TXT／Markdown／docx／EPUB／貼上的文字 → chapters/chNNN.json。

- 拆章與編碼判斷重用 ``vendor/novel_studio/importer.py``（TXT／MD／docx）；EPUB 由本檔讀取（每個內文檔當一章，或合併後依標題拆）。
- 只寫 ``works/<book_id>/``，從不寫書目錄。
- 輸出格式與 ``tools/normalize_book.py`` 相同（chapter.schema.json），所以後面的標註、校對、原型流程不分來源。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .errors import T2vnError

SCHEMA_VERSION = "0.1"
BOOK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
SCENE_BREAK_RE = re.compile(r"^(＊＊＊|\* \* \*|\*\*\*)$")
EPUB_MIN_CHARS = 200  # 內文檔少於這個字數視為封面／目錄／版權頁，預設不匯入
SUPPORTED_EXTS = (".txt", ".md", ".markdown", ".text", ".docx", ".epub")


class IngestError(T2vnError):
    """匯入失敗（格式、檔案、參數）。"""


# ---------------------------------------------------------------- EPUB


class _TextExtractor(HTMLParser):
    """XHTML → 段落清單。區塊標籤換段，標題另外記下來。"""

    BLOCK = {"p", "div", "br", "li", "blockquote", "section", "tr"}
    HEADINGS = {"h1", "h2", "h3", "h4"}
    SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self.headings: list[str] = []
        self._buf: list[str] = []
        self._skip = 0
        self._in_heading = False

    def _flush(self) -> None:
        text = "".join(self._buf).strip()
        self._buf = []
        if text:
            self.paragraphs.append(re.sub(r"\s+", " ", text) if re.search(r"[A-Za-z]", text) else re.sub(r"[ \t\r\n　]+", "", text))

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.HEADINGS:
            self._flush()
            self._in_heading = True
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.HEADINGS:
            text = "".join(self._buf).strip()
            if text:
                self.headings.append(re.sub(r"\s+", " ", text))
            self._buf = []
            self._in_heading = False
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def read_epub(data: bytes) -> list[dict[str, str]]:
    """回傳 [{"title", "text"}]，依 spine 順序；text 是一段一行（空行分隔）。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise IngestError("this .epub cannot be opened (damaged or not an EPUB)") from exc
    with archive:
        try:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            opf_path = next(el.attrib["full-path"] for el in container.iter() if el.tag.endswith("rootfile"))
            opf = ET.fromstring(archive.read(opf_path))
        except (KeyError, StopIteration, ET.ParseError) as exc:
            raise IngestError("this .epub has no readable package file (container.xml / .opf)") from exc
        base = posixpath.dirname(opf_path)
        manifest = {el.attrib["id"]: el.attrib for el in opf.iter() if el.tag.endswith("}item") or el.tag == "item"}
        spine = [el.attrib["idref"] for el in opf.iter() if el.tag.endswith("itemref")]
        out: list[dict[str, str]] = []
        for idref in spine:
            item = manifest.get(idref)
            if not item or "html" not in item.get("media-type", ""):
                continue
            if "nav" in item.get("properties", ""):
                continue
            path = posixpath.normpath(posixpath.join(base, item["href"].split("#")[0]))
            try:
                raw = archive.read(path)
            except KeyError:
                continue
            parser = _TextExtractor()
            parser.feed(raw.decode("utf-8", "replace"))
            parser.close()
            paragraphs = parser.paragraphs
            title = parser.headings[0] if parser.headings else ""
            if title and paragraphs and paragraphs[0] == title:
                paragraphs = paragraphs[1:]
            out.append({"title": title, "text": "\n\n".join(paragraphs)})
    if not out:
        raise IngestError("this .epub has no text documents in its spine")
    return out


# ---------------------------------------------------------------- 預覽（拆章）


def _importer():
    from vendor.novel_studio import importer
    return importer


def plan_chapters(payload: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
    """依選項拆章，回傳 importer.plan 相同結構（含每章 text），不寫檔。

    payload：{"files": [{"name", "data"(base64)}], "text": "..."}。
    opts：mode(auto|chapter|markdown|spine)、lang、convert、numbering、start、preface_as_chapter。
    """
    importer = _importer()
    files = payload.get("files") or []
    epubs = [f for f in files if str(f.get("name", "")).lower().endswith(".epub")]
    if epubs and (len(epubs) != len(files) or (payload.get("text") or "").strip()):
        raise IngestError("EPUB cannot be mixed with other files or pasted text; import them separately")
    try:
        if not epubs:
            return importer.plan(payload, opts)
        return _plan_epub(epubs, opts)
    except importer.ImportError_ as exc:
        raise IngestError(str(exc)) from exc


def _plan_epub(epubs: list[dict[str, Any]], opts: dict[str, Any]) -> dict[str, Any]:
    importer = _importer()
    lang = opts.get("lang") or "zh-TW"
    convert = bool(opts.get("convert")) and lang == "zh-TW"
    docs: list[dict[str, str]] = []
    for f in sorted(epubs, key=lambda x: importer._natural_key(x.get("name", ""))):
        try:
            data = base64.b64decode(f.get("data") or "")
        except Exception as exc:  # noqa: BLE001
            raise IngestError(f"cannot read file {f.get('name')!r}") from exc
        docs += read_epub(data)

    mode = opts.get("mode") or "auto"
    if mode in ("auto", "spine"):
        body = [d for d in docs if importer.count_text(d["text"], lang) >= EPUB_MIN_CHARS]
        use_spine = mode == "spine" or len(body) >= 3
    else:
        use_spine = False
    warnings: list[str] = []
    if use_spine:
        skipped = len(docs) - len(body)
        if skipped:
            warnings.append(f"{skipped} spine document(s) under {EPUB_MIN_CHARS} chars skipped (cover / contents / copyright).")
        chunks = [{"num": None, "title": (d["title"] or "")[: importer.MAX_TITLE], "heading": d["title"], "body": d["text"]} for d in body]
    else:
        text = "\n\n".join((f"{d['title']}\n\n" if d["title"] else "") + d["text"] for d in docs)
        if convert:
            text = importer.to_traditional(text)
        _pre, chunks = importer.split_text(text, "auto" if mode == "spine" else mode)
        if not chunks:
            chunks = [{"num": None, "title": "", "heading": "", "body": text}]
    if convert and use_spine:
        for c in chunks:
            c["body"] = importer.to_traditional(c["body"])
            c["title"] = importer.to_traditional(c["title"])
    start = int(opts.get("start") or 1)
    nums = [c["num"] for c in chunks]
    use_titles = opts.get("numbering", "auto") != "sequential" and all(n is not None for n in nums) \
        and all(b > a for a, b in zip(nums, nums[1:]))
    for i, c in enumerate(chunks):
        c["no"] = c["num"] if use_titles else start + i
        c["text"] = importer.normalize(c["body"], lang)
        c["chars"] = importer.count_text(c["text"], lang)
        c["flags"] = []
    chunks = [c for c in chunks if c["chars"] > 0]
    importer._flag_chapters(chunks, convert, warnings, lang == "zh-TW", importer.lang_mod.unit_zh(lang))
    return {"sources": [{"name": f.get("name", ""), "encoding": "EPUB", "simplified": 0, "chapters": len(chunks)} for f in epubs],
            "chapters": chunks, "warnings": warnings, "numbered_by_title": use_titles, "preface": []}


def preview(payload: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
    """給 UI 的預覽：每章只留編號、章名、字數、旗標與開頭一小段。"""
    result = plan_chapters(payload, opts)
    result["chapters"] = [
        {"no": c["no"], "title": c["title"], "chars": c["chars"], "flags": c.get("flags") or [],
         "head": re.sub(r"\s+", " ", c["text"][:60])}
        for c in result["chapters"]
    ]
    return result


# ---------------------------------------------------------------- 轉成 chapter 檔


def chapter_doc(book_id: str, no: int, title: str, text: str, source_file: str) -> dict[str, Any]:
    """一章的文字（段落以空行分隔）→ chapter.schema.json 的資料。分場線（＊＊＊）記進 scene_breaks。"""
    paragraphs: list[dict[str, Any]] = []
    scene_breaks: list[int] = []
    pending = False
    for raw in re.split(r"\n\s*\n", text.strip()):
        line = raw.strip()
        if not line:
            continue
        if SCENE_BREAK_RE.match(line):
            pending = True
            continue
        if pending and paragraphs:
            scene_breaks.append(len(paragraphs) + 1)
        pending = False
        paragraphs.append({"para_no": len(paragraphs) + 1, "text": line})
    return {
        "schema_version": SCHEMA_VERSION, "book_id": book_id, "chapter_id": f"ch{no:03d}", "chapter_no": no,
        "title": title or f"{no}", "source_file": source_file,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "char_count": sum(len("".join(p["text"].split())) for p in paragraphs),
        "paragraphs": paragraphs, "scene_breaks": scene_breaks,
    }


def validate_book_id(book_id: Any) -> str:
    if not isinstance(book_id, str) or not BOOK_ID_RE.match(book_id):
        raise IngestError(f"book_id must match {BOOK_ID_RE.pattern} (ASCII lowercase, digits, underscore), got {book_id!r}")
    return book_id


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_book(work_dir: str | Path, book_id: str, display_name: str, lang: str, chapters: list[dict[str, Any]],
               source: dict[str, Any]) -> dict[str, Any]:
    """寫 book.json、cast.json（只有 player）、chapters/*.json、chapters/index.json。已有章節的資料夾不覆蓋。"""
    from .schema_check import validate_data

    validate_book_id(book_id)
    work = Path(work_dir)
    if (work / "chapters" / "index.json").exists():
        raise IngestError(f"works/{book_id} already has chapters; choose another book_id or remove it first")
    nos = [c["no"] for c in chapters]
    if not nos:
        raise IngestError("no chapters to import")
    if len(set(nos)) != len(nos) or min(nos) < 1 or max(nos) > 999:
        raise IngestError("chapter numbers must be unique integers in 1..999")
    docs = []
    for c in sorted(chapters, key=lambda x: x["no"]):
        doc = chapter_doc(book_id, c["no"], c["title"], c["text"], c.get("source_file", source.get("name", "")))
        issues = validate_data(doc, "chapter")
        if issues:
            raise IngestError(f"chapter {c['no']} failed schema check: {issues[0].path} {issues[0].message}")
        docs.append(doc)

    (work / "chapters").mkdir(parents=True, exist_ok=True)
    for doc in docs:
        (work / "chapters" / f"{doc['chapter_id']}.json").write_text(_dump(doc), encoding="utf-8")
    index = {"schema_version": SCHEMA_VERSION, "book_id": book_id, "chapters": [
        {"chapter_id": d["chapter_id"], "chapter_no": d["chapter_no"], "title": d["title"], "char_count": d["char_count"],
         "paragraph_count": len(d["paragraphs"]), "file": f"chapters/{d['chapter_id']}.json"} for d in docs]}
    (work / "chapters" / "index.json").write_text(_dump(index), encoding="utf-8")
    book = {"schema_version": SCHEMA_VERSION, "book_id": book_id, "display_name": {lang: display_name}, "lang": lang,
            "source": source}
    (work / "book.json").write_text(_dump(book), encoding="utf-8")
    cast_path = work / "cast.json"
    if not cast_path.exists():
        cast = {"schema_version": SCHEMA_VERSION, "book_id": book_id, "characters": {
            "player": {"names": {lang: "主角（內心）"}, "aliases": [], "role": "player"}}}
        cast_path.write_text(_dump(cast), encoding="utf-8")
    return index
