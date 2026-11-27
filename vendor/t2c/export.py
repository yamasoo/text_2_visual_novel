"""輸出：把 pages/chNNN/pNNN.png 打包成 PDF 或 CBZ（日式右翻）。整章或單頁。

規則與格式見 docs/02_資料格式.md 第 15 節：
- 只讀已經輸出的頁面 PNG，不重新組版；PNG 不存在就報錯，叫人先輸出頁面。
- PDF：自己寫（Pillow 的 PDF 輸出不能設閱讀方向）。`/ViewerPreferences << /Direction /R2L >>`，
  預設 `/PageLayout /TwoPageRight`（第 1 頁在右、單獨一頁，之後左右成對）。
- CBZ：ZIP 內放頁面 PNG（原檔不重新壓縮）＋ ComicInfo.xml（`Manga` = `YesAndRightToLeft`）。
- 輸出到專案的 out/（不進版控）；同樣的輸入產生同樣的位元組（不寫時間戳）。
- 只用標準函式庫與 Pillow（核心規則：不依賴 FastAPI、fekit）。
"""
import re
import zipfile
import zlib
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image

from . import compose

FORMATS = ("pdf", "cbz")
DEFAULT_QUALITY = 90               # PDF 內頁的 JPEG 品質；None 表示無損（Flate）
DEFAULT_DPI = 254
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
CBZ_COMMENT_NAME = "ComicInfo.xml"


class ExportError(Exception):
    """給人看的錯誤訊息（寫出是哪一頁）。"""


# ---- 要輸出哪些頁 ---------------------------------------------------------------------

def page_path(project, chapter, page):
    return compose.pages_dir(project, chapter) / ("p%03d.png" % int(page))


def chapter_pages(project, chapter, page=None):
    """依分鏡的頁碼，回傳 ([(頁碼, PNG 路徑)], 警告)。PNG 缺了就丟 ExportError，一次列出全部缺的頁。"""
    storyboard = project.load_storyboard(chapter)
    numbers = sorted(int(p["page"]) for p in storyboard.get("pages") or [])
    if not numbers:
        raise ExportError("第 %d 章的分鏡沒有任何頁" % int(chapter))
    if page is not None:
        if int(page) not in numbers:
            raise ExportError("分鏡裡沒有第 %s 頁" % page)
        numbers = [int(page)]
    missing = [n for n in numbers if not page_path(project, chapter, n).is_file()]
    if missing:
        raise ExportError("第 %s 頁還沒有輸出頁面（找不到 pages/%s/p%03d.png）：請先輸出頁面（介面的「頁面」分頁，或 python tools/pages.py）"
                          % ("、".join(str(n) for n in missing), compose.chapter_name(chapter), missing[0]))
    pages = [(n, page_path(project, chapter, n)) for n in numbers]

    warnings = []
    sources = [project.storyboard_path(chapter), compose.picks_path(project, chapter),
               project.root / "letters" / (compose.chapter_name(chapter) + ".json")]
    newest = max((p.stat().st_mtime for p in sources if p.is_file()), default=0)
    stale = [n for n, path in pages if path.stat().st_mtime < newest]
    if stale:
        warnings.append("第 %s 頁的 PNG 比分鏡、選圖或嵌字微調舊，可能不是最新的；要的話先重新輸出頁面"
                        % "、".join(str(n) for n in stale))
    return pages, warnings


def default_meta(project, chapter, page=None):
    """書名、章名、語言（取自 project.yaml 與分鏡）。"""
    storyboard = project.load_storyboard(chapter)
    work = str(project.config.get("title") or "").strip()
    chapter_title = str(storyboard.get("title") or "").strip()
    language = str(project.config.get("language") or "zh-TW")
    if not LANG_RE.match(language):
        language = "zh-TW"
    parts = [work, "第 %d 章" % int(chapter)] + ([chapter_title] if chapter_title else [])
    if page is not None:
        parts.append("第 %d 頁" % int(page))
    return {"work": work, "chapter": int(chapter), "chapter_title": chapter_title, "language": language,
            "title": "　".join(p for p in parts if p)}


# ---- PDF ---------------------------------------------------------------------------

def _pdf_text(text):
    """PDF 文字字串：UTF-16BE（含 BOM）十六進位。"""
    return "<FEFF" + str(text).encode("utf-16-be").hex().upper() + ">"


def _image_stream(img, quality):
    """內頁的影像：回傳 (色彩空間, 濾鏡, bytes)。灰階與 RGB 之外一律轉灰階。"""
    if img.mode not in ("L", "RGB"):
        img = img.convert("L")
    space = "/DeviceGray" if img.mode == "L" else "/DeviceRGB"
    if quality is None:
        return img, space, "/FlateDecode", zlib.compress(img.tobytes(), 6)
    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=int(quality), optimize=True)
    return img, space, "/DCTDecode", buffer.getvalue()


def _dpi(img):
    dpi = img.info.get("dpi")
    try:
        value = float(dpi[0])
    except (TypeError, ValueError, IndexError):
        value = 0.0
    return value if value > 1 else float(DEFAULT_DPI)


def build_pdf(paths, out, meta=None, quality=DEFAULT_QUALITY, spread=True):
    """paths：頁面圖檔（依閱讀順序，第 1 頁在最前面）。右翻：Direction R2L。回傳 Path。

    spread=True：用雙頁檢視開啟（第 1 頁單獨在右，之後左右成對）；False：單頁。只有一頁時一律單頁。"""
    meta = meta or {}
    paths = [Path(p) for p in paths]
    if not paths:
        raise ExportError("沒有任何頁面可以輸出")
    if quality is not None and not 1 <= int(quality) <= 95:
        raise ExportError("JPEG 品質要在 1–95 之間（要無損請不要給品質），目前是 %r" % (quality,))

    objects = []                                    # objects[i] 是第 i+1 號物件的 bytes（不含 "n 0 obj"）

    def add(body):
        objects.append(body)
        return len(objects)

    catalog = add(b"")                              # 1：先佔位，最後才知道 Pages 的編號
    pages_root = add(b"")                           # 2
    info = add(b"")                                 # 3
    kids = []
    for path in paths:
        try:
            with Image.open(path) as opened:
                opened.load()
                dpi = _dpi(opened)
                img, space, filt, data = _image_stream(opened, quality)
        except OSError as error:
            raise ExportError("讀不了頁面圖 %s（%s）" % (path.name, error))
        width, height = img.size
        pt_w, pt_h = width / dpi * 72.0, height / dpi * 72.0
        image_id = len(objects) + 3                 # 頁、內容、影像依序是 +1、+2、+3（先算好，才能互相引用）
        page_id = add(("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources << /XObject << /Im0 %d 0 R >> >> "
                       "/Contents %d 0 R >>" % (pages_root, pt_w, pt_h, image_id, image_id - 1)).encode("ascii"))
        content = ("q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (pt_w, pt_h)).encode("ascii")
        add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        head = ("<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s /BitsPerComponent 8 /Filter %s /Length %d >>"
                % (width, height, space, filt, len(data))).encode("ascii")
        add(head + b"\nstream\n" + data + b"\nendstream")
        kids.append(page_id)

    language = str(meta.get("language") or "zh-TW")
    if not LANG_RE.match(language):
        language = "zh-TW"
    layout = "/TwoPageRight" if spread and len(paths) > 1 else "/SinglePage"
    objects[catalog - 1] = ("<< /Type /Catalog /Pages %d 0 R /ViewerPreferences << /Direction /R2L >> /PageLayout %s /Lang (%s) >>"
                            % (pages_root, layout, language)).encode("ascii")
    objects[pages_root - 1] = ("<< /Type /Pages /Count %d /Kids [%s] >>"
                               % (len(kids), " ".join("%d 0 R" % k for k in kids))).encode("ascii")
    fields = ["/Producer (text2comics)"]
    if meta.get("title"):
        fields.insert(0, "/Title " + _pdf_text(meta["title"]))
    objects[info - 1] = ("<< %s >>" % " ".join(fields)).encode("ascii")

    buffer = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(buffer))
        buffer += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(buffer)
    buffer += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        buffer += b"%010d 00000 n \n" % offset
    buffer += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
               % (len(objects) + 1, catalog, info, xref))
    return _write(out, bytes(buffer))


# ---- CBZ ---------------------------------------------------------------------------

def comic_info(meta, sizes, gray=True):
    """ComicInfo.xml（Anansi ComicInfo 2.0）。sizes：[(頁碼, 寬, 高)]。Manga = YesAndRightToLeft。"""
    meta = meta or {}
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">']
    title = meta.get("chapter_title") or meta.get("title") or ""
    if title:
        lines.append("  <Title>%s</Title>" % escape(title))
    if meta.get("work"):
        lines.append("  <Series>%s</Series>" % escape(meta["work"]))
    if meta.get("chapter"):
        lines.append("  <Number>%d</Number>" % int(meta["chapter"]))
    lines.append("  <PageCount>%d</PageCount>" % len(sizes))
    language = str(meta.get("language") or "zh-TW")
    lines.append("  <LanguageISO>%s</LanguageISO>" % escape(language if LANG_RE.match(language) else "zh-TW"))
    lines.append("  <BlackAndWhite>%s</BlackAndWhite>" % ("Yes" if gray else "No"))
    lines.append("  <Manga>YesAndRightToLeft</Manga>")
    lines.append("  <Pages>")
    for index, (_, width, height) in enumerate(sizes):
        kind = ' Type="FrontCover"' if index == 0 else ""
        lines.append('    <Page Image="%d"%s ImageWidth="%d" ImageHeight="%d" />' % (index, kind, width, height))
    lines.append("  </Pages>")
    lines.append("</ComicInfo>")
    return "\n".join(lines) + "\n"


def build_cbz(pages, out, meta=None):
    """pages：[(頁碼, PNG 路徑)]（依閱讀順序）。ZIP 內放 pNNN.png 與 ComicInfo.xml。回傳 Path。"""
    if not pages:
        raise ExportError("沒有任何頁面可以輸出")
    sizes, gray = [], True
    for number, path in pages:
        try:
            with Image.open(path) as img:
                sizes.append((int(number), img.size[0], img.size[1]))
                gray = gray and img.mode in ("L", "1", "LA")
        except OSError as error:
            raise ExportError("讀不了頁面圖 %s（%s）" % (Path(path).name, error))
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for number, path in pages:
            info = zipfile.ZipInfo("p%03d.png" % int(number))          # 預設時間戳固定（1980-01-01），輸出可重現
            info.compress_type = zipfile.ZIP_STORED                    # PNG 已經壓過
            info.external_attr = 0o644 << 16
            archive.writestr(info, Path(path).read_bytes())
        info = zipfile.ZipInfo(CBZ_COMMENT_NAME)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, comic_info(meta, sizes, gray).encode("utf-8"))
    return _write(out, buffer.getvalue())


# ---- 整章／單頁 ---------------------------------------------------------------------

def _write(out, data):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(out)                                # 寫完才換名，下載到一半不會拿到半個檔
    return out


def out_path(project, chapter, fmt, page=None, out=None):
    """輸出檔路徑：沒給就是 <專案>/out/chNNN.pdf（單頁是 chNNN_pNNN.pdf）；給資料夾就放在裡面。"""
    name = compose.chapter_name(chapter) + ("_p%03d" % int(page) if page is not None else "") + "." + fmt
    if out is None:
        return project.root / "out" / name
    out = Path(out)
    if out.is_dir() or not out.suffix:
        return out / name
    if out.suffix.lower() != "." + fmt:
        raise ExportError("輸出檔的副檔名要是 .%s，目前是 %s" % (fmt, out.name))
    return out


def export_chapter(project, chapter, fmt, page=None, out=None, quality=DEFAULT_QUALITY, spread=True):
    """整章（或只有 page）→ PDF 或 CBZ。回傳 (Path, 警告)。"""
    fmt = str(fmt or "").lower()
    if fmt not in FORMATS:
        raise ExportError("格式要是 %s，目前是 %r" % (" 或 ".join(FORMATS), fmt))
    pages, warnings = chapter_pages(project, chapter, page)
    meta = default_meta(project, chapter, page)
    target = out_path(project, chapter, fmt, page, out)
    if fmt == "pdf":
        path = build_pdf([p for _, p in pages], target, meta, quality=quality, spread=spread)
    else:
        path = build_cbz(pages, target, meta)
    return path, warnings
