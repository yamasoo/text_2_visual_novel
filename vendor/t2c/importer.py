# -*- coding: utf-8 -*-
"""整本小說匯入 → 自動切章，寫成專案的 `source/chNNN.txt`（N2）。

切章邏輯移植自 Novel Studio 的 `studio/importer.py`（唯讀參考，只抄邏輯）：
`split_text()`／`normalize()`／`plan()`／`preview()`／`commit()`。差別只有這幾點：

- 吃 `Path`（或一批 Path），不再用「檔案內容是 base64」的約定；支援 .txt／.md／.docx。
  .docx 用標準函式庫的 zipfile＋ElementTree 讀 `word/document.xml`，不加新依賴。
- 落地格式是 t2c 的 `<專案>/source/chNNN.txt`（一章一個檔），不是 Novel Studio 的書庫。
  重複匯入可選 `conflict="skip"`（預設）或 `"overwrite"`；覆寫前先把舊檔備份到
  `<專案>/build/import/backup/`（`build/` 不進版控）。
- 簡轉繁（opencc）是選用：沒安裝就跳過並在 warnings 說明，不會讓匯入失敗。
- Novel Studio 的 `fix_punct`（它的標點檢查器）沒有搬過來，t2c 不做標點改寫。

只用標準函式庫（CLAUDE.md 第 3 節）；錯誤丟 `ImporterError`，訊息是給人看的繁體中文。
"""
import io
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_TITLE = 30
MAX_CHAPTER = 999
BACKUP_KEEP = 10
TEXT_EXTS = (".txt", ".md", ".markdown", ".text", "")
MODE_CHOICES = ("auto", "chapter", "markdown")
NUMBERING_CHOICES = ("auto", "titles", "sequential")
CONFLICT_CHOICES = ("skip", "overwrite")

NUM = r"[0-9０-９一二三四五六七八九十百千零〇兩]+"
SEP = r"(?:\s*$|[\s:：、.．·\-—　]+(.*?)\s*$)"
CHAPTER_RE = re.compile(r"^\s*(?:#{1,4}\s*)?第\s*(" + NUM + r")\s*[章回節话話]" + SEP)
EN_RE = re.compile(r"^\s*(?:#{1,4}\s*)?chapter\s*(\d+|[IVXLC]+)" + SEP, re.I)   # 阿拉伯數字或羅馬數字（CHAPTER VI）
# 英文的「PART II」單獨一行：卷名，不當成章節，也不放進正文（下一行若是全大寫的卷名也一起略過）
EN_PART_RE = re.compile(r"^\s*(?:PART|Part)\s+([IVXLC]+|\d+)\s*$")
# 序章、尾聲這類特殊章：中文、英文（Prologue 等，不分大小寫）、日文（プロローグ 等）
SPECIAL_RE = re.compile(r"^\s*(?:#{1,4}\s*)?(序章|序幕|楔子|引子|序|尾聲|終章|後記|番外(?:\s*" + NUM + r")?"
                        r"|(?i:prologue|epilogue|interlude|afterword)"
                        r"|プロローグ|エピローグ|幕間|(?:閑話|番外編)(?:\s*" + NUM + r")?)" + SEP)
VOLUME_RE = re.compile(r"^\s*(?:#{1,4}\s*)?第\s*" + NUM + r"\s*[卷部集篇]\b.*$|^\s*(?:#{1,4}\s*)?第\s*" + NUM + r"\s*[卷部集篇](\s|$|[:：])")
MD_HEAD_RE = re.compile(r"^\s*#{1,3}\s+(.+?)\s*$")

_CN = {c: i for i, c in enumerate("零一二三四五六七八九")}
_CN.update({"〇": 0, "兩": 2})
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff]")
CH_FILE_RE = re.compile(r"^ch(\d{3})\.(?:txt|md)$")

# Novel Studio `_simplified_set()` 的內建後備清單：只拿來「提醒」有簡體字，不用來改寫文字。
# 真正的簡轉繁仍交給 opencc；這裡刻意不用外部字表，避免變成硬依賴。
_SIMP_FALLBACK = set("们这说时发来对会过还没么样经现进动后着里为国")
_SIMP = None
_T2S = None


class ImporterError(Exception):
    """給人看的匯入錯誤（檔案讀不到、格式不支援、章號不能用…）。"""


# ---------------------------------------------------------------------------
# 讀檔：編碼判斷、docx、簡轉繁
# ---------------------------------------------------------------------------
def decode(data):
    """bytes → (文字, 編碼名稱)。UTF-8／UTF-16 先試，再在 Big5 與 GB18030 之間挑比較像中文的。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "UTF-8"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "UTF-16"
    try:
        return data.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        pass
    cands = []
    for enc, label in (("cp950", "Big5（繁體）"), ("gb18030", "GB18030（簡體）")):
        try:
            text = data.decode(enc)
            cands.append((text, label))
        except UnicodeDecodeError:
            continue
    if len(cands) == 2:
        # 兩種都解得開時：看哪個結果比較像正常中文（常用字比例高、罕用區塊少）
        def score(text):
            common = len(re.findall(r"[的一是不了人我在有他這中大來上個們到說時你地出就那要以會可也著她沒過]", text))
            odd = len(re.findall(r"[\u3400-\u4dbf\ue000-\uf8ff]", text))
            return common - 5 * odd
        cands.sort(key=lambda x: -score(x[0]))
    if cands:
        return cands[0]
    return data.decode("utf-8", "replace"), "UTF-8（有無法辨識的字元）"


def read_docx(source):
    """.docx → 文字。source 可以是 Path，也可以是檔案內容（bytes）。段落之間空一行。

    只用 zipfile＋ElementTree 讀 `word/document.xml`；不支援舊版 .doc。
    """
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        name = "這個 .docx"
    else:
        path = Path(source)
        name = "「%s」" % path.name
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ImporterError("讀不到檔案%s：%s" % (name, error))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise ImporterError("%s打不開，可能已損毀，或其實是舊版 .doc 格式（請在 Word 另存成 .docx）" % name)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise ImporterError("%s的 XML 讀不懂：%s" % (name, error))
    W = "{%s}" % ns["w"]
    paras = []
    for p in root.iter(W + "p"):
        buf = []
        for node in p.iter():
            if node.tag == W + "t":
                buf.append(node.text or "")
            elif node.tag == W + "tab":
                buf.append("　")
            elif node.tag in (W + "br", W + "cr"):
                buf.append("\n")
        paras.append("".join(buf))
    return "\n\n".join(paras)


def read_source(path):
    """Path → (文字, 編碼名稱)。支援 .txt／.md／.docx，副檔名不分大小寫。"""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ImporterError("讀不到檔案「%s」：%s" % (path.name, error))
    ext = path.suffix.lower()
    if ext == ".docx":
        return read_docx(data), "Word"
    if ext in TEXT_EXTS:
        return decode(data)
    raise ImporterError("不支援「%s」的格式；可以匯入 .txt、.md、.docx" % path.name)


def _as_paths(files):
    if isinstance(files, (str, os.PathLike)):
        files = [files]
    paths = [Path(item) for item in (files or []) if item is not None]
    if not paths:
        raise ImporterError("請選擇要匯入的檔案（.txt、.md、.docx）")
    paths.sort(key=lambda p: _natural_key(p.name))
    return paths


def load_sources(files):
    """讀一批檔案 → [{"path", "name", "text", "encoding"}]（依檔名自然排序）。"""
    out = []
    for path in _as_paths(files):
        text, encoding = read_source(path)
        out.append({"path": str(path), "name": path.name, "text": text, "encoding": encoding})
    return out


def has_opencc():
    try:
        import opencc  # noqa: F401
        return True
    except ImportError:
        return False


def to_traditional(text):
    """簡 → 台灣繁體（opencc 的 s2twp）。沒有安裝就丟 ImporterError（plan 會先檢查、跳過）。"""
    try:
        from opencc import OpenCC
    except ImportError:
        raise ImporterError("簡轉繁需要 OpenCC 套件（pip install opencc-python-reimplemented），"
                            "或把「轉成台灣繁體」關掉")
    return OpenCC("s2twp").convert(text)


def _simplified_set():
    global _SIMP
    if _SIMP is None:
        _SIMP = set(_SIMP_FALLBACK)
    return _SIMP


def count_simplified(text):
    """算簡體字數量（只用來提醒；字表是內建的小清單，不是完整的簡繁對照）。"""
    simp = _simplified_set()
    return sum(1 for c in text or "" if c in simp)


# ---------------------------------------------------------------------------
# 章號
# ---------------------------------------------------------------------------
def roman_to_int(s):
    s = s.upper()
    total = 0
    for i, ch in enumerate(s):
        v = _ROMAN.get(ch, 0)
        nxt = _ROMAN.get(s[i + 1], 0) if i + 1 < len(s) else 0
        total += -v if v < nxt else v
    return total


def en_num(s):
    return int(s) if s.isdigit() else roman_to_int(s)


def cn_to_int(s):
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if s.isdigit():
        return int(s)
    total, section, num = 0, 0, 0
    units = {"十": 10, "百": 100, "千": 1000}
    for ch in s:
        if ch in _CN:
            num = _CN[ch]
        elif ch in units:
            section += (num or 1) * units[ch]
            num = 0
        elif ch == "萬":
            total += (section + num) * 10000
            section, num = 0, 0
    return total + section + num


# ---------------------------------------------------------------------------
# 拆章（邏輯與 Novel Studio 相同）
# ---------------------------------------------------------------------------
def _natural_key(name):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name)]


def _heading(line, mode):
    """判斷一行是不是章節標題。mode：chapter（第X章、Chapter N、楔子等）或 markdown（# 標題）。
    回傳 (標題上的章號或 None, 章名, 原文) 或 None。"""
    s = line.strip()
    if not s or len(s) > 60:
        return None
    if mode == "chapter":
        for rx, kind in ((CHAPTER_RE, "cn"), (EN_RE, "en"), (SPECIAL_RE, "sp")):
            m = rx.match(s)
            if not m:
                continue
            rest = (m.group(2) or "").strip("#　 ")
            if len(rest) > MAX_TITLE or re.search(r"[。！？!?]", rest[:-1] if rest else ""):
                return None  # 後面接的是句子，不是章名
            if kind == "cn":
                return cn_to_int(m.group(1)), rest, s
            if kind == "en":
                return en_num(m.group(1)), rest, s
            joiner = " " if re.match(r"[A-Za-z]", m.group(1)) else "　"      # 英文的 Prologue 後面接半形空白
            return None, (m.group(1) + (joiner + rest if rest else ""))[:MAX_TITLE], s
        return None
    m = MD_HEAD_RE.match(s)
    if m:
        h = _heading(m.group(1), "chapter")
        return h if h else (None, m.group(1)[:MAX_TITLE], s)
    return None


def _t2s_table():
    """繁→簡逐字對照（只取一字對一字），做法與 Novel Studio 的 `search._table()` 相同。

    只有裝了 opencc 才拿得到它的字典；沒裝就回傳空表（比對時只去掉標點與英文大小寫）。
    """
    global _T2S
    if _T2S is not None:
        return _T2S
    table = {}
    try:
        import opencc
        folder = Path(opencc.__file__).parent / "dictionary"
        path = folder / "TSCharacters.txt"
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 2:
                        continue
                    src, dst = parts[0], parts[1].split(" ")[0]
                    if len(src) == 1 and len(dst) == 1 and src != dst:
                        table.setdefault(ord(src), ord(dst))
    except Exception:
        table = {}
    _T2S = table
    return table


def _title_key(t):
    """比對章名用：去掉 #、書名號、引號、標點與空白，英文不分大小寫。
    有裝 opencc 時順便把繁簡逐字對齊（「風雪夜歸」和「风雪夜归」算同一個章名）。"""
    key = re.sub(r"[#《》〈〉「」『』\"'“”‘’()（）\[\]【】:：、,，.．·\-—\s　]", "", t or "")
    return key.translate(_t2s_table()).casefold()


def _merge_duplicate_headings(chunks):
    """同一章的標題連寫兩次（例如「第 7 章　夜襲」下一行又有「# 第七章 夜襲」）：
    章號相同、中間只有空行、章名相同或其中一邊沒寫，就當成同一章。"""
    out, merged = [], 0
    for c in chunks:
        p = out[-1] if out else None
        if (p and p["num"] is not None and p["num"] == c["num"]
                and len(p["lines"]) <= 3 and not any(x.strip() for x in p["lines"])
                and (not _title_key(p["title"]) or not _title_key(c["title"]) or _title_key(p["title"]) == _title_key(c["title"]))):
            p["title"] = p["title"] or c["title"]
            p["lines"] = c["lines"]
            merged += 1
            continue
        out.append(c)
    return out, merged


def _drop_leading_toc(chunks):
    """書開頭的目錄：一串間距很近的章名，後面正文又照同樣順序出現 → 丟掉開頭那串。
    回傳 (章節, 丟掉幾條)。"""
    if len(chunks) < 6:
        return chunks, 0
    for k in range(min(len(chunks) // 2, 500), 2, -1):
        pre = chunks[:k]
        if pre[0]["line"] > 200:
            continue
        gaps = sorted(pre[i]["line"] - pre[i - 1]["line"] for i in range(1, k))
        if gaps[len(gaps) // 2] > 3:
            continue
        sig = [(x["num"], _title_key(x["title"])) for x in pre[:min(8, k)]]
        for j in range(k, len(chunks) - len(sig) + 1):
            if [(x["num"], _title_key(x["title"])) for x in chunks[j:j + len(sig)]] == sig and chunks[j]["line"] - pre[-1]["line"] > 3:
                return chunks[j:], j
    return chunks, 0


def _split(lines, mode, stats=None):
    chunks, cur, preface = [], None, []
    want_title, skip_part_title = False, False   # want_title：英文「CHAPTER VI」後面下一行是章名；skip_part_title：PART 後面的卷名
    for i, ln in enumerate(lines):
        s = ln.strip()
        if skip_part_title and s:
            skip_part_title = False
            if len(s) <= 60 and s == s.upper() and not re.search(r"[.!?]$", s):
                continue   # 「PART II」下一行的卷名（THE SEA COOK）
        if len(s) <= 40 and VOLUME_RE.match(s):
            continue  # 卷名：不當成章節，也不放進正文
        if EN_PART_RE.match(s):
            skip_part_title = True
            continue
        if want_title and s:
            want_title = False
            if cur is not None and not cur["title"] and len(s) <= 100 and not re.search(r"[.!?,;:]$", s):
                cur["title"] = s[:60]     # 「CHAPTER VI」的下一行「THE CAPTAIN'S PAPERS」是章名
                continue
        h = _heading(ln, mode)
        if h:
            cur = {"num": h[0], "title": h[1], "heading": h[2], "lines": [], "line": i + 1}
            chunks.append(cur)
            want_title = bool(EN_RE.match(s)) and not h[1]
        elif cur is None:
            preface.append(ln)
        else:
            cur["lines"].append(ln)
    chunks, toc = _drop_leading_toc(chunks)
    chunks, merged = _merge_duplicate_headings(chunks)
    if stats is not None:
        stats["toc"] = stats.get("toc", 0) + toc
        stats["merged"] = stats.get("merged", 0) + merged
    for c in chunks:
        c["body"] = "\n".join(c.pop("lines"))
        c.pop("line", None)
    return "\n".join(preface).strip(), chunks


def split_text(text, mode="auto", stats=None):
    """回傳 (前言, [{"num": 標題上的章號或 None, "title", "heading", "body"}])。
    auto：先找「第X章」這類標題，找不到再用 Markdown 的 # 標題。stats 會記下合併與丟掉目錄的數量。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    lines = text.split("\n")
    if mode in ("auto", "chapter"):
        pre, chunks = _split(lines, "chapter", stats)
        if chunks or mode == "chapter":
            return pre, chunks
    return _split(lines, "markdown", stats)


def normalize(body, lang="zh-TW"):
    """整理段落：一段一行、段落之間空一行，去掉段首空白。場景分隔線：中文統一成「＊＊＊」，
    英文統一成「* * *」，日文保留作者原本的寫法（◇、＊、——…各家習慣不同）。"""
    body = body.replace("\t", "").strip("\n")
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    has_blank = re.search(r"\n[ \t　]*\n", body) is not None
    lines = [ln.strip(" 　\xa0") for ln in body.split("\n")]
    if has_blank:
        paras, buf = [], []
        for ln in lines:
            if ln:
                buf.append(ln)
            elif buf:
                paras.append(buf)
                buf = []
        if buf:
            paras.append(buf)
        # 段內換行：中文直接接起來
        paras = ["".join(p) if all(re.search(r"[\u4e00-\u9fff，。！？」』…—]$", x) for x in p[:-1]) else " ".join(p)
                 for p in paras]
    else:
        paras = [ln for ln in lines if ln]
    # 場景分隔線（***、＊＊＊、——— 等）：中文統一成「＊＊＊」、英文統一成「* * *」，日文不動
    sep = {"en": "* * *", "ja": None}.get(lang, "＊＊＊")
    if sep:
        paras = [sep if re.fullmatch(r"[-*_=＝—~～·•＊※◆◇○●\s]{3,}", p) else p for p in paras]
    return "\n\n".join(paras)


def count_text(text, lang="zh-TW"):
    """字數：中日文算字（漢字與假名）、英文算詞，兩者取大的（和 Novel Studio 的算法相同）。
    lang 保留只為相容舊呼叫。"""
    body = text or ""
    if body.startswith("---"):
        lines = body.split("\n")
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body = "\n".join(lines[i + 1:])
                break
    body = re.sub(r"^#.*$", "", body, flags=re.M)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    cjk = len(CJK.findall(body))
    words = len(re.findall(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*", body))
    return words if words > cjk else cjk


def _unit(lang):
    return "詞" if str(lang or "").startswith("en") else "字"


# ---------------------------------------------------------------------------
# 規劃與預覽
# ---------------------------------------------------------------------------
def _start_no(value):
    try:
        start = int(value if value is not None else 1)
    except (TypeError, ValueError):
        raise ImporterError("起始章號要是整數，目前是 %r" % (value,))
    if not 1 <= start <= MAX_CHAPTER:
        raise ImporterError("起始章號要在 1–%d 之間，目前是 %d" % (MAX_CHAPTER, start))
    return start


def plan(files, opts=None):
    """依選項拆章，回傳預覽資料（不寫檔）。

    files：Path 或 Path 清單。opts：
    mode（auto／chapter／markdown）、lang（zh-TW／en／ja）、convert（簡轉繁，需 opencc）、
    preface_as_chapter、preface_title、numbering（auto／titles／sequential）、start。
    """
    opts = dict(opts or {})
    mode = str(opts.get("mode") or "auto")
    if mode not in MODE_CHOICES:
        raise ImporterError("切章模式只能是 %s（目前是 %r）" % ("、".join(MODE_CHOICES), mode))
    numbering = str(opts.get("numbering") or "auto")
    if numbering not in NUMBERING_CHOICES:
        raise ImporterError("編號方式只能是 %s（目前是 %r）" % ("、".join(NUMBERING_CHOICES), numbering))
    start = _start_no(opts.get("start"))
    lang = str(opts.get("lang") or "zh-TW")
    # 只有繁體中文稿才做簡體偵測與簡轉繁；日文的新字體（伝、戦、竜…）在簡體字表裡，會被誤判
    zh = lang == "zh-TW"
    convert = bool(opts.get("convert")) and zh
    warnings = []
    if convert and not has_opencc():
        warnings.append("沒有安裝 OpenCC，這次略過簡轉繁（要轉請執行 pip install opencc-python-reimplemented）。")
        convert = False
    sources = load_sources(files)
    infos, chapters, preface_all = [], [], []
    simp_total = 0
    stats = {}
    for src in sources:
        text = src["text"]
        simp = count_simplified(text) if zh else 0
        simp_total += simp
        if convert:
            text = to_traditional(text)
        pre, chunks = split_text(text, mode, stats)
        if not chunks:
            # 整個檔案當成一章（一個檔案一章的情況），章號和章名從檔名猜
            stem = Path(src["path"]).stem
            m = re.match(r"^(\d+)[\s._\-、]*(.*)$", stem)
            h = _heading(stem, "chapter") or ((int(m.group(1)), (m.group(2) or stem)[:MAX_TITLE], stem) if m
                                              else (None, stem[:MAX_TITLE], stem))
            chunks = [{"num": h[0], "title": h[1], "heading": "", "body": text}]
            pre = ""
        if pre and count_text(pre, lang) >= 1:
            preface_all.append((src["name"], pre))
        infos.append({"name": src["name"], "encoding": src["encoding"], "simplified": simp, "chapters": len(chunks)})
        chapters += chunks
    if opts.get("preface_as_chapter") and preface_all:
        body = "\n\n".join(p for _, p in preface_all)
        chapters.insert(0, {"num": None, "title": str(opts.get("preface_title") or "序章"), "heading": "", "body": body})
        preface_all = []
    # 編號：標題上的章號遞增且不重複就沿用，否則依順序重新編號
    nums = [c["num"] for c in chapters]
    use_titles = numbering != "sequential" and all(n is not None for n in nums) \
        and all(b > a for a, b in zip(nums, nums[1:]))
    if use_titles:
        bad = [n for n in nums if not (isinstance(n, int) and 1 <= n <= MAX_CHAPTER)]
        if bad:
            if numbering == "titles":
                raise ImporterError("標題上的章號有 0 或超過 %d，不能當成 source/chNNN.txt 的檔名；"
                                    "請改用「依順序重新編號」（--numbering sequential）" % MAX_CHAPTER)
            use_titles = False
            warnings.append("標題上的章號有 0 或超過 %d，改成依順序重新編號。" % MAX_CHAPTER)
    if numbering == "titles" and not use_titles:
        raise ImporterError("標題上的章號不連續、重複或有缺（例如有楔子、番外），請改用「依順序重新編號」")
    for i, c in enumerate(chapters):
        c["no"] = c["num"] if use_titles else start + i
        c["text"] = normalize(c["body"], lang)
        c["chars"] = count_text(c["text"], lang)
    chapters = [c for c in chapters if c["chars"] > 0 or c["title"]]
    for c in chapters:
        if not 1 <= c["no"] <= MAX_CHAPTER:
            raise ImporterError("章號 %d 超過 %d，不能用 source/ch%03d.txt 的形式命名；"
                                "請減少章數或調整起始章號" % (c["no"], MAX_CHAPTER, c["no"]))
    if stats.get("merged"):
        warnings.append("有 %d 章的章名連寫了兩次（寫法不同），已經合併成同一章。" % stats["merged"])
    if stats.get("toc"):
        warnings.append("開頭有一份目錄（%d 條章名），已經略過，不當成章節。" % stats["toc"])
    uz = _unit(lang)        # 英文書算詞：訊息寫「詞」，其他寫「字」
    _flag_chapters(chapters, convert, warnings, zh, uz)
    if zh and simp_total and not convert:
        warnings.append("偵測到約 %d 個簡體字。可以勾選「轉成台灣繁體」再預覽。" % simp_total)
    if preface_all:
        n = sum(count_text(p, lang) for _, p in preface_all)
        warnings.append("第一個章節標題之前有 %d %s（例如書名、簡介、楔子），預設不會匯入；"
                        "需要的話勾選「把開頭的文字當成一章」。" % (n, uz))
    return {"sources": infos, "chapters": chapters, "warnings": warnings, "numbered_by_title": use_titles,
            "preface": [{"name": n, "chars": count_text(p, lang), "preview": p.strip()[:80]} for n, p in preface_all]}


def _nos(cs):
    nos = [c["no"] for c in cs]
    s = "、".join("第%d章" % n for n in nos[:12])
    return s + ("……等 %d 章" % len(nos) if len(nos) > 12 else "")


def _flag_chapters(chapters, convert, warnings, zh=True, unit="字"):
    """逐章標出可疑的地方（給預覽表上色），並彙整成提醒。"""
    sizes = sorted(c["chars"] for c in chapters if c["chars"] > 0)
    median = sizes[len(sizes) // 2] if sizes else 0
    prev = None
    for c in chapters:
        f = []
        if c["chars"] < 50:
            f.append("空章")
        elif median >= 500 and c["chars"] < median * 0.2:
            f.append("過短")
        if c["chars"] > 20000 or (median >= 500 and c["chars"] > median * 3):
            f.append("過長")
        n = c.get("num")
        if n is not None and prev is not None:
            if n == prev:
                f.append("重複章號")
            elif n > prev + 1:
                f.append("跳號（缺第%s章）" % ("%d" % (prev + 1) if n == prev + 2 else "%d～%d" % (prev + 1, n - 1)))
            elif n < prev:
                f.append("章號倒退")
        if n is not None:
            prev = n
        simp = count_simplified(c["text"]) if zh else 0
        if simp >= 5:
            f.append(("轉繁後仍有簡體字 %d" if convert else "簡體字 %d") % simp)
        c["flags"] = f
    groups = [("空章", "內容不到 50 %s（可能是目錄或標題誤判）" % unit), ("過短", "比一般章節短很多"),
              ("過長", "比一般章節長很多（可能有章節標題沒被認出來）"), ("重複章號", "章號重複"),
              ("跳號", "章號不連續"), ("章號倒退", "章號比上一章小"), ("簡體字", "含簡體字"), ("轉繁後", "轉繁後仍有簡體字")]
    for key, text in groups:
        cs = [c for c in chapters if any(x.startswith(key) for x in c["flags"])]
        if cs:
            warnings.append("%s：%s。" % (text, _nos(cs)))


def preview(files, opts=None):
    """plan() 的輕量版：章節只留表格要用的欄位（不帶整章正文）。"""
    r = plan(files, opts)
    r["chapters"] = [{"no": c["no"], "title": c["title"], "heading": c["heading"], "chars": c["chars"],
                      "flags": c.get("flags") or [], "head": re.sub(r"\s+", " ", c["text"][:60])} for c in r["chapters"]]
    return r


# ---------------------------------------------------------------------------
# 寫檔
# ---------------------------------------------------------------------------
def _write_atomic(path, text):
    """先寫同目錄的暫存檔再換名；寫到一半失敗時原檔維持舊內容。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def existing_chapters(folder):
    """回傳 {章號: [Path…]}。同一個章號可能同時有 .txt 與 .md（舊專案相容）。"""
    found = {}
    folder = Path(folder)
    if not folder.is_dir():
        return found
    for path in sorted(folder.iterdir()):
        m = CH_FILE_RE.match(path.name)
        if m and path.is_file():
            found.setdefault(int(m.group(1)), []).append(path)
    return found


def _backup(old, root, stamp):
    """覆寫前把舊檔複製到 build/import/backup/，回傳備份路徑（每個章節最多留 BACKUP_KEEP 份）。"""
    folder = Path(root) / "build" / "import" / "backup"
    folder.mkdir(parents=True, exist_ok=True)
    stem = "%s.%s" % (old.name, stamp)
    target = folder / (stem + ".bak")
    n = 2
    while target.exists():
        target = folder / ("%s-%d.bak" % (stem, n))
        n += 1
    shutil.copy2(old, target)
    olds = sorted(folder.glob(old.name + ".*.bak"), key=lambda p: p.name, reverse=True)
    for extra in olds[BACKUP_KEEP:]:
        try:
            extra.unlink()
        except OSError:
            pass
    return target


def _rel(path, root):
    return str(Path(path).relative_to(root)).replace("\\", "/")


def commit(project, files, opts=None):
    """把 plan() 的章節寫進專案的 source/chNNN.txt。

    project：漫畫專案資料夾（Path）或 `t2c.project.Project`。
    opts：plan() 的選項＋conflict（skip／overwrite）。
    回傳 {"project", "written", "skipped", "replaced", "backups", "chars", "warnings"}。
    """
    opts = dict(opts or {})
    conflict = str(opts.get("conflict") or "skip")
    if conflict not in CONFLICT_CHOICES:
        raise ImporterError("重複匯入的處理方式只能是 %s（目前是 %r）" % ("、".join(CONFLICT_CHOICES), conflict))
    # Path 在 Windows 上也有 .root（磁碟機代號），所以先判斷是不是路徑本身
    if isinstance(project, (str, os.PathLike)):
        root = Path(project)
    else:
        root = Path(getattr(project, "root", project))
    root = root.resolve()
    if not (root / "project.yaml").is_file():
        raise ImporterError("「%s」不是漫畫專案資料夾（找不到 project.yaml）" % root)
    result = plan(files, opts)
    if not result["chapters"]:
        raise ImporterError("沒有可寫入的章節；請確認檔案裡有文字與章節標題")
    tdir = root / "source"
    tdir.mkdir(parents=True, exist_ok=True)
    existing = existing_chapters(tdir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    written, skipped, replaced, backups = [], [], [], []
    for c in result["chapters"]:
        no = int(c["no"])
        title = c["title"] or ""
        text = "# 第%d章%s\n\n%s\n" % (no, (" " + title) if title else "", c["text"].strip())
        olds = existing.get(no) or []
        if olds and conflict != "overwrite":
            skipped.append(no)
            continue
        target = tdir / ("ch%03d.txt" % no)
        made = []
        for old in olds:
            bak = _backup(old, root, stamp)
            made.append(_rel(bak, root))
        _write_atomic(target, text)
        for old in olds:
            if old != target:
                try:
                    old.unlink()
                except OSError as error:
                    raise ImporterError("新檔已寫入，但刪不掉舊檔「%s」：%s（備份在 %s）"
                                        % (old.name, error, made[0] if made else "build/import/backup"))
        if olds:
            replaced.append(no)
            backups.append({"no": no, "files": made})
        written.append({"no": no, "file": _rel(target, root), "chars": c["chars"]})
    return {"project": str(root), "written": written, "skipped": skipped, "replaced": replaced,
            "backups": backups, "chars": sum(w["chars"] for w in written), "warnings": result["warnings"]}
