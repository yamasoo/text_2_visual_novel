"""漫畫劇本 `script/chNNN.md` 的解析與檢查（S5）。

劇本是「人看、人改」的版本，也是分鏡階段的唯一依據：分鏡的台詞、旁白、狀聲詞必須和劇本逐字相同
（見 `check.check_against_script`）。所以人只要改劇本，再重跑分鏡階段即可。格式見 docs/02_資料格式.md 第 4 節::

    ## 第 1 頁（模板 5a：上二、中一、下二）

    **1 格**｜遠景、俯視｜畫面描述。
    - 旁白：十二點四十分。
    - 曉雨：……又不接。
    - 曉雨（心聲）：……記得這種事幹嘛。
    - 阿哲（通訊）：收到。          ← 透過機器傳來的聲音：radio，說話的人不在這一格
    - 阿哲（畫面外、喊）：快跑！    ← 說話的人不在這一格的畫面裡：offscreen
    - 狀聲詞：碰！

只用標準函式庫與 PyYAML，不依賴 FastAPI／fekit。
"""
import re
from dataclasses import dataclass, field

import yaml

from . import cast

FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)

# 劇本用的中文鏡頭詞 → 分鏡的英文列舉值（compile.SHOT／ANGLE 的 key）
SHOT_WORDS = {"大遠景": "extreme_wide", "遠景": "wide", "全身": "full", "中景": "medium", "近景": "close", "特寫": "extreme_close"}
ANGLE_WORDS = {"平視": "eye", "俯視": "high", "仰視": "low", "鳥瞰": "bird", "斜角": "dutch", "過肩": "over_shoulder"}
# 台詞後面括號裡的語氣 → 對話框種類
MARKS = {"心聲": "thought", "喊": "shout", "喊叫": "shout", "小聲": "whisper", "悄聲": "whisper"}
# 聲音來源（M1 修正）：透過機器傳來 → radio；人不在這一格的畫面裡 → offscreen。可以和語氣用「、」併寫
RADIO_MARKS = ("通訊", "電話", "廣播", "無線電")
OFFSCREEN_MARKS = ("畫面外",)
MARK_SPLIT = re.compile(r"[、，,／/\s]+")
NARRATOR = "旁白"
SFX_LABELS = ("狀聲詞", "音效")
NO_LINES = {"無台詞", "無對白", "沒有台詞"}

MAX_BUBBLES = 3          # 一格最多幾個對話框（旁白也算，狀聲詞不算）
MAX_LINE_CHARS = 45      # 單一對話框最多幾個字，再長就讀不下去了

PAGE_RE = re.compile(r"^\s*#{1,4}\s*第\s*(\d+)\s*頁\s*(?:[（(]\s*模板\s*([0-9A-Za-z]+)\s*(?:[：:][^）)]*)?[）)])?\s*$")
PANEL_RE = re.compile(r"^\s*\*\*\s*(\d+)\s*格\s*\*\*\s*[｜|]\s*(.*?)\s*$")
BULLET_RE = re.compile(r"^\s*[-*•]\s*(.*?)\s*$")
LABEL_RE = re.compile(r"^(.+?)\s*[（(]\s*(.+?)\s*[）)]\s*$")
SPLIT_WORDS = re.compile(r"[、，,/／\s]+")


@dataclass
class Issue:
    """一個檢查結果。error 會觸發 LLM 自動回修；warn 只列在報告裡。"""
    level: str          # "error" | "warn"
    where: str          # 例如「第 2 頁第 3 格」
    message: str

    def __str__(self):
        return "%s：%s" % (self.where, self.message) if self.where else self.message


def errors(issues):
    return [i for i in issues if i.level == "error"]


@dataclass
class ScriptLine:
    kind: str                   # narration | speech | thought | shout | whisper | radio | sfx
    speaker: str = ""           # 劇本裡寫的名字（旁白、狀聲詞為空）
    text: str = ""
    offscreen: bool = False     # （畫面外）：說話的人不在這一格的畫面裡（radio 由分鏡決定，見資料格式第 5 節）


@dataclass
class ScriptPanel:
    slot: int
    shots: list = field(default_factory=list)       # 鏡頭詞（遠景…）
    angles: list = field(default_factory=list)      # 角度詞（俯視…）
    unknown_words: list = field(default_factory=list)
    description: str = ""
    lines: list = field(default_factory=list)       # ScriptLine


@dataclass
class ScriptPage:
    page: int
    template: str = ""
    panels: list = field(default_factory=list)
    raw: str = ""                                   # 這一頁的原始文字（含標題行）


def strip_fence(text):
    """去掉模型常加的 ``` 圍欄。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*\n", "", t)
    t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


def split_front(text):
    """(frontmatter dict, 本文)。沒有 frontmatter 回傳 ({}, 原文)。"""
    text = (text or "").replace("\r\n", "\n").lstrip("﻿")
    match = FRONT.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[match.end():]


def _parse_label(label):
    """「曉雨（心聲）」→ ("曉雨", "心聲")。"""
    m = LABEL_RE.match(label.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return label.strip(), ""


def _parse_bullet(content, where, issues):
    """一行 `- 名字：台詞`。回傳 ScriptLine 或 None（沒有台詞的標記）。"""
    plain = content.strip().strip("（）()").strip()
    if plain in NO_LINES:
        return None
    parts = re.split(r"[：:]", content, maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        issues.append(Issue("error", where, "這一行看不懂：「%s」。對白要寫成「- 角色名：台詞」，沒有台詞的格寫「- （無台詞）」" % content[:30]))
        return None
    name, mark = _parse_label(parts[0])
    text = parts[1].strip()
    if name == NARRATOR:
        return ScriptLine("narration", "", text)
    if name in SFX_LABELS:
        return ScriptLine("sfx", "", text)
    kind, offscreen = _parse_marks(mark, where, issues)
    return ScriptLine(kind, name, text, offscreen)


def _parse_marks(mark, where, issues):
    """括號裡的標記 → (kind, offscreen)。「畫面外、喊」這種併寫用「、」分開。"""
    kind, offscreen, tones, radio = "speech", False, [], False
    for word in [w for w in MARK_SPLIT.split(mark or "") if w]:
        if word in MARKS:
            tones.append(word)
        elif word in RADIO_MARKS:
            radio = True
        elif word in OFFSCREEN_MARKS:
            offscreen = True
        else:
            issues.append(Issue("error", where, "不認得的標記「%s」（語氣可用：%s；聲音來源可用：%s）"
                                % (word, "、".join(MARKS), "、".join(RADIO_MARKS + OFFSCREEN_MARKS))))
    if len(tones) > 1:
        issues.append(Issue("error", where, "語氣只能標一個，目前是：%s" % "、".join(tones)))
    if tones:
        kind = MARKS[tones[0]]
    if radio:
        if tones or offscreen:
            issues.append(Issue("error", where, "（通訊）（電話）這類透過機器的聲音一律用電子音對話框，不能再加%s" % "、".join(tones + (["畫面外"] if offscreen else []))))
        return "radio", False
    if offscreen and kind == "thought":
        issues.append(Issue("error", where, "心聲要看得到想的人，不能和（畫面外）併用"))
        offscreen = False
    return kind, offscreen


def split_pages(body):
    """本文 → [(頁碼, 該頁原始文字)]，依出現順序。頁標題之前的文字（章標題等）略過。"""
    pages, current = [], None
    for line in body.split("\n"):
        m = PAGE_RE.match(line)
        if m:
            current = [int(m.group(1)), [line]]
            pages.append(current)
        elif current is not None:
            current[1].append(line)
    return [(no, "\n".join(lines).strip()) for no, lines in pages]


def parse_page(raw):
    """一頁的原始文字 → (ScriptPage, issues)。"""
    issues = []
    lines = raw.strip().split("\n")
    m = PAGE_RE.match(lines[0]) if lines else None
    if not m:
        return ScriptPage(0, raw=raw), [Issue("error", "", "開頭要是「## 第 N 頁（模板 K：名稱）」")]
    page = ScriptPage(int(m.group(1)), m.group(2) or "", raw=raw.strip())
    panel = None
    for line in lines[1:]:
        if not line.strip():
            continue
        pm = PANEL_RE.match(line)
        if pm:
            parts = re.split(r"[｜|]", pm.group(2), maxsplit=1)
            head, desc = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
            panel = ScriptPanel(int(pm.group(1)), description=desc.strip())
            for word in [w for w in SPLIT_WORDS.split(head) if w]:
                if word in SHOT_WORDS:
                    panel.shots.append(word)
                elif word in ANGLE_WORDS:
                    panel.angles.append(word)
                else:
                    panel.unknown_words.append(word)
            page.panels.append(panel)
            continue
        where = "第 %d 頁第 %d 格" % (page.page, panel.slot) if panel else "第 %d 頁" % page.page
        bm = BULLET_RE.match(line)
        if bm and panel is not None:
            item = _parse_bullet(bm.group(1), where, issues)
            if item is not None:
                panel.lines.append(item)
        elif panel is not None and not line.lstrip().startswith("#"):
            panel.description = (panel.description + " " + line.strip()).strip()     # 描述換行：併進描述
        elif panel is None:
            issues.append(Issue("error", where, "格標題之前不該有文字：「%s」。格標題的寫法是「**1 格**｜鏡頭｜畫面描述」" % line.strip()[:30]))
    return page, issues


def parse_script(text):
    """整份劇本 → (meta, [ScriptPage], issues)。"""
    meta, body = split_front(text)
    pages, issues = [], []
    for no, raw in split_pages(body):
        page, found = parse_page(raw)
        pages.append(page)
        issues += found
    return meta, pages, issues


def resolve_speaker(project, name):
    """劇本裡的名字（姓名、別名、id 都可以）→ 角色 id；找不到回傳 None。
    比對只有一份（D12）：這裡只是 cast.resolve() 的薄包裝。"""
    cid, _how = cast.resolve(project, (name or "").strip())
    return cid


def check_page(project, page, expect_page=None, expect_template=None, panel_count=None, strict=True):
    """檢查一頁劇本。strict=True（LLM 剛產生的）要求每格都有鏡頭詞；人改過的劇本用 strict=False，只擋會讓分鏡做不下去的問題。"""
    issues = []
    where_page = "第 %s 頁" % (expect_page or page.page)
    if expect_page is not None and page.page != expect_page:
        issues.append(Issue("error", where_page, "頁碼寫成第 %d 頁，應該是第 %d 頁" % (page.page, expect_page)))
    if expect_template and page.template and page.template != expect_template:
        issues.append(Issue("error", where_page, "模板寫成「%s」，這一頁規劃的是「%s」" % (page.template, expect_template)))
    if panel_count is not None:
        slots = [p.slot for p in page.panels]
        if slots != list(range(1, panel_count + 1)):
            issues.append(Issue("error", where_page, "這一頁的模板有 %d 格，格編號要依序是 1–%d，目前是 %s" % (panel_count, panel_count, slots or "沒有任何格")))
    for panel in page.panels:
        where = "第 %d 頁第 %d 格" % (page.page, panel.slot)
        if not panel.description.strip():
            issues.append(Issue("error", where, "缺少畫面描述"))
        if strict and not panel.shots:
            issues.append(Issue("error", where, "缺少鏡頭詞（可用：%s）" % "、".join(SHOT_WORDS)))
        if strict and len(panel.shots) > 1:
            issues.append(Issue("error", where, "鏡頭詞只能選一個，目前是：%s" % "、".join(panel.shots)))
        if panel.unknown_words:
            issues.append(Issue("error" if strict else "warn", where, "不認得的鏡頭／角度詞：%s（鏡頭可用：%s；角度可用：%s）"
                                % ("、".join(panel.unknown_words), "、".join(SHOT_WORDS), "、".join(ANGLE_WORDS))))
        bubbles = [l for l in panel.lines if l.kind != "sfx"]
        if len(bubbles) > MAX_BUBBLES:
            issues.append(Issue("error", where, "對話框（含旁白）有 %d 個，一格最多 %d 個。請把部分台詞移到下一格或刪減" % (len(bubbles), MAX_BUBBLES)))
        for line in panel.lines:
            if not line.text.strip():
                issues.append(Issue("error", where, "有一行沒有內容"))
            elif len(re.sub(r"\s", "", line.text)) > MAX_LINE_CHARS:
                issues.append(Issue("error", where, "「%s…」太長（%d 字），單一對話框請在 %d 字以內" % (line.text[:12], len(line.text), MAX_LINE_CHARS)))
            if line.speaker and resolve_speaker(project, line.speaker) is None:
                issues.append(Issue("error", where, "說話的人「%s」不在視覺聖經（bible/characters）裡。請用角色卡的姓名或別名；沒有角色卡的人物請改成旁白或刪掉" % line.speaker))
    return issues


# ---------------------------------------------------------------------------
# 忠實度報告（給人看的參考，不擋流程）
# ---------------------------------------------------------------------------
_KEEP = re.compile(r"[^0-9A-Za-z㐀-鿿]")
QUOTE_RE = re.compile(r"[「『“](.+?)[」』”]", re.S)


def normalize(text):
    """只留文字與數字，去掉標點、空白，比對時不受標點改動影響。"""
    return _KEEP.sub("", text or "")


def source_quotes(source):
    """原著裡引號內的句子（至少 3 個字）。"""
    return [q.strip() for q in QUOTE_RE.findall(source or "") if len(normalize(q)) >= 3]


SENTENCE_END = re.compile(r"[。！？!?；;…⋯\n]+")
SHORT_PART = 4           # 比這短的片段（是我、好、等一下）在原著裡到處都是子字串，要和原著某一句「整句相同」才算


def source_sentences(source):
    """原著引號內台詞依句讀拆開後的每一句（已 normalize）。"""
    return {n for q in QUOTE_RE.findall(source or "") for n in (normalize(p) for p in SENTENCE_END.split(q)) if n}


def found_in(src_norm, line, sentences=None):
    """台詞能不能在原著找到：先整句比；找不到再依句讀拆開逐句比（M1 修正 L-3：兩句原文併成一個對話框不算改寫）。
    長片段只要是原著的子字串；短片段要和原著某一句整句相同（sentences，見 source_sentences）。"""
    whole = normalize(line)
    if not whole or whole in src_norm:
        return True
    parts = [p for p in (normalize(x) for x in SENTENCE_END.split(line)) if p]
    if len(parts) < 2:
        return False
    sentences = sentences or set()
    return all((p in src_norm) if len(p) >= SHORT_PART else (p in sentences) for p in parts)


def fidelity(source, pages):
    """劇本對原著的忠實度（粗略，供人工審閱）：
    - 劇本的台詞有幾句能在原著找到（逐字，不計標點）；找不到的列出來，可能是改寫或新增；
    - 原著引號內的台詞有幾句沒進劇本（被刪或被改寫）。"""
    src = normalize(source)
    dialogue = [l for p in pages for pn in p.panels for l in pn.lines if l.kind in ("speech", "thought", "shout", "whisper", "radio")]
    script_text = normalize("".join(l.text for p in pages for pn in p.panels for l in pn.lines))
    sentences = source_sentences(source)
    not_in_source = [l.text for l in dialogue if normalize(l.text) and not found_in(src, l.text, sentences)]
    quotes = source_quotes(source)
    missing = [q for q in quotes if normalize(q) not in script_text]
    return {
        "dialogue_lines": len(dialogue),
        "dialogue_found": len(dialogue) - len(not_in_source),
        "not_in_source": not_in_source,
        "source_quotes": len(quotes),
        "source_quotes_kept": len(quotes) - len(missing),
        "source_quotes_missing": missing,
    }


def fidelity_markdown(report, title=""):
    lines = ["# 改編忠實度報告%s" % ("：" + title if title else ""), "",
             "（程式粗略比對，供人工審閱；改寫過的台詞會被列在「找不到」，不一定是錯。）", "",
             "- 劇本台詞 %d 句，其中 %d 句能在原著逐字找到（不計標點）" % (report["dialogue_lines"], report["dialogue_found"]),
             "- 原著引號內台詞 %d 句，其中 %d 句進了劇本" % (report["source_quotes"], report["source_quotes_kept"]), ""]
    if report["not_in_source"]:
        lines += ["## 劇本有、原著找不到的台詞（改寫或新增）", ""] + ["- %s" % t for t in report["not_in_source"]] + [""]
    if report["source_quotes_missing"]:
        lines += ["## 原著有、劇本沒有的台詞（刪除或改寫）", ""] + ["- %s" % t for t in report["source_quotes_missing"]] + [""]
    return "\n".join(lines).rstrip() + "\n"
