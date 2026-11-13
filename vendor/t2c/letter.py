"""嵌字：對話框、尾巴、直排繁中、避開臉部、狀聲詞。

規則與資料格式見 docs/02_資料格式.md 第 10 節：
- 分鏡的 bubbles／sfx／characters 座標相對於「生成的圖」；組版時圖被裁切（picks 的 focus、zoom），
  這裡先用 compose.page_panels() 的裁切範圍（window）換算到格子上。
- 人的微調放 letters/chNNN.json，座標相對於「裁切後看到的格子」。
- 流程：plan_page()（算位置，結果可以轉 JSON 給介面）→ draw_page()（畫上去）。
- 只用標準函式庫與 Pillow（核心規則：不依賴 FastAPI、fekit）。
"""
import glob
import hashlib
import json
import math
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import compose, layout

LETTERS_VERSION = 1
KINDS = ("speech", "thought", "shout", "whisper", "radio", "narration")
ROLE_OF_KIND = {"speech": "dialogue", "thought": "dialogue", "whisper": "dialogue", "radio": "dialogue", "shout": "shout",
                "narration": "narration"}
SCALE_MIN, SCALE_MAX = 0.5, 2.0
ROWS_MIN, ROWS_MAX = 1, 40
ANGLE_LIMIT = 45.0
SS = 3                                  # 對話框先放大 3 倍畫，再縮回來（反鋸齒）
FIT_SCALES = (1.0, 0.9, 0.8, 0.7, 0.6)  # 放不下時逐步縮小字級
SHORT = 6                               # 這麼短的台詞優先排成一欄

# 鏡頭 → 臉大約佔角色框高度的比例（自動推估臉框用；不準時在介面拖臉框）
HEAD = {"extreme_wide": 0.16, "wide": 0.17, "full": 0.15, "medium": 0.3, "close": 0.45, "extreme_close": 0.7}


class LetterError(Exception):
    """給人看的錯誤訊息（會寫出是哪一頁哪一格）。"""


# ---- 字型 ---------------------------------------------------------------------------

FONT_DIRS = [
    "C:/Windows/Fonts",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft/Windows/Fonts") if os.environ.get("LOCALAPPDATA") else "",
    "/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"), os.path.expanduser("~/.local/share/fonts"),
    "/Library/Fonts", "/System/Library/Fonts", os.path.expanduser("~/Library/Fonts"),
]
FONT_PATTERNS = {
    "dialogue": ["NotoSansCJK-Medium.ttc", "NotoSansCJKtc-Medium.otf", "NotoSansTC-Medium.*", "NotoSansCJK-Regular.ttc",
                 "NotoSansCJKtc-Regular.otf", "NotoSansTC-Regular.*", "NotoSansTC-VariableFont*", "SourceHanSansTC-Medium.*",
                 "SourceHanSans-Medium.ttc", "SourceHanSansTC-Regular.*", "msjh.ttc"],
    "shout": ["NotoSansCJK-Black.ttc", "NotoSansCJK-Bold.ttc", "NotoSansCJKtc-Black.otf", "NotoSansCJKtc-Bold.otf",
              "NotoSansTC-Black.*", "NotoSansTC-Bold.*", "SourceHanSansTC-Heavy.*", "SourceHanSansTC-Bold.*", "msjhbd.ttc"],
    "narration": ["NotoSerifCJK-Medium.ttc", "NotoSerifCJK-Regular.ttc", "NotoSerifCJKtc-Medium.otf",
                  "NotoSerifCJKtc-Regular.otf", "NotoSerifTC-Medium.*", "NotoSerifTC-Regular.*", "SourceHanSerifTC-Medium.*",
                  "SourceHanSerif-Medium.ttc", "mingliu.ttc"],
    "sfx": ["NotoSansCJK-Black.ttc", "NotoSansCJKtc-Black.otf", "NotoSansTC-Black.*", "SourceHanSansTC-Heavy.*",
            "msjhbd.ttc"],
}
FALLBACK = {"shout": "dialogue", "narration": "dialogue", "sfx": "shout"}
SYSTEM_FONTS = {"msjh.ttc", "msjhbd.ttc", "mingliu.ttc"}      # Windows 附的字型：自己看可以，發佈要換開源字型
TC_FACE = re.compile(r"\bTC\b|Traditional|JhengHei|正黑|MingLiU|明", re.I)
CONFIG_KEY = {"dialogue": "dialogue_font", "shout": "shout_font", "narration": "narration_font", "sfx": "sfx_font"}


def _split_spec(spec):
    """'路徑#索引' → (路徑, 索引或 None)。"""
    text = str(spec).strip()
    match = re.match(r"^(.*)#(\d+)$", text)
    return (match.group(1), int(match.group(2))) if match else (text, None)


def _ttc_index(path):
    """.ttc 沒指定索引時，挑名稱像繁中的那一套；找不到就 0。"""
    for index in range(32):
        try:
            name = " ".join(ImageFont.truetype(path, 16, index=index).getname())
        except OSError:
            break
        if TC_FACE.search(name):
            return index
    return 0


def resolve_font(spec):
    """設定值 → (路徑, 索引)。檔案不存在或讀不了就丟 LetterError。"""
    path, index = _split_spec(spec)
    if not Path(path).is_file():
        raise LetterError("字型檔不存在：%s" % path)
    if index is None:
        index = _ttc_index(path) if path.lower().endswith(".ttc") else 0
    try:
        ImageFont.truetype(path, 16, index=index)
    except OSError as error:
        raise LetterError("讀不了字型 %s（索引 %d）：%s" % (path, index, error))
    return path, index


def find_font(role, dirs=None):
    """依 FONT_PATTERNS 在常見字型資料夾找；回傳路徑或 None。"""
    for pattern in FONT_PATTERNS[role]:
        for folder in dirs if dirs is not None else FONT_DIRS:
            if not folder or not os.path.isdir(folder):
                continue
            hits = sorted(glob.glob(os.path.join(folder, "**", pattern), recursive=True))
            if hits:
                return hits[0]
    return None


class Settings:
    """嵌字設定：字型、直排、字級。由 config.json 的 lettering 建立（from_config）。"""

    def __init__(self, fonts, vertical=True, dialogue_pt=9.0, narration_pt=8.5, faux_bold=(), notes=()):
        self.fonts = dict(fonts)                 # role → (路徑, 索引)
        self.vertical = bool(vertical)
        self.dialogue_pt = float(dialogue_pt)
        self.narration_pt = float(narration_pt)
        self.faux_bold = set(faux_bold)          # 沒有粗體字型、用描邊加粗的 role
        self.notes = list(notes)                 # 給人看的提示（暫用系統字型等）
        self._cache = {}

    @classmethod
    def from_config(cls, cfg=None, dirs=None):
        cfg = dict(cfg or {})
        fonts, notes, faux = {}, [], set()
        for role in ("dialogue", "shout", "narration", "sfx"):
            spec = str(cfg.get(CONFIG_KEY[role]) or "").strip()
            if spec:
                try:
                    fonts[role] = resolve_font(spec)
                except LetterError as error:
                    raise LetterError("config.json 的 lettering.%s：%s" % (CONFIG_KEY[role], error))
                continue
            found = find_font(role, dirs)
            if found:
                fonts[role] = resolve_font(found)
                name = Path(found).name
                note = "沒有設定 lettering.%s，自動使用 %s" % (CONFIG_KEY[role], name)
                if name.lower() in SYSTEM_FONTS:
                    note += "（系統字型：自己看可以，要發佈請換成開源字型）"
                notes.append(note)
            elif role in FALLBACK and FALLBACK[role] in fonts:
                fonts[role] = fonts[FALLBACK[role]]
                if role in ("shout", "sfx"):
                    faux.add(role)
            elif role == "dialogue":
                raise LetterError("找不到中文字型：請在 config.json 的 lettering.dialogue_font 填字型檔路徑"
                                  "（例如 Noto Sans CJK TC，見 docs/03_建置與環境.md 第 7 節）")
        if "shout" in faux and "sfx" not in fonts:
            fonts["sfx"] = fonts["shout"]
            faux.add("sfx")
        for key in ("dialogue_pt", "narration_pt"):
            if key in cfg:
                try:
                    value = float(cfg[key])
                except (TypeError, ValueError):
                    raise LetterError("config.json 的 lettering.%s 要是數字，目前是 %r" % (key, cfg[key]))
                if not 4 <= value <= 30:
                    raise LetterError("config.json 的 lettering.%s 要在 4–30 之間，目前是 %g" % (key, value))
        return cls(fonts, cfg.get("vertical", True), cfg.get("dialogue_pt", 9), cfg.get("narration_pt", 8.5), faux, notes)

    def font(self, role, size):
        size = max(4, int(round(size)))
        key = (role, size)
        if key not in self._cache:
            path, index = self.fonts[role]
            self._cache[key] = ImageFont.truetype(path, size, index=index)
        return self._cache[key]


# ---- 微調紀錄 letters/chNNN.json ---------------------------------------------------------

def letters_path(project, chapter):
    return project.root / "letters" / (compose.chapter_name(chapter) + ".json")


def _point(value, label):
    try:
        x, y = (float(v) for v in value)
    except (TypeError, ValueError):
        raise LetterError("%s 要是 [x, y] 兩個 0–1 的數字，目前是 %r" % (label, value))
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise LetterError("%s 要在 0–1 之間，目前是 %r" % (label, value))
    return [x, y]


def _box(value, label):
    try:
        return list(layout.check_box(value, label))
    except layout.LayoutError as error:
        raise LetterError(str(error))


def _entries(value, label, check):
    if value is None:
        return []
    if not isinstance(value, list):
        raise LetterError("%s 要是清單（依分鏡的順序；不改的寫 {}）" % label)
    return [check(item, "%s 第 %d 個" % (label, i + 1)) for i, item in enumerate(value)]


def _bubble_override(item, label):
    if item is None:
        return {}
    if not isinstance(item, dict):
        raise LetterError("%s 要是 {center, tail, scale, rows} 的形式" % label)
    allowed = {"text", "center", "tail", "scale", "rows"}
    out = {}
    for key, value in item.items():
        if key.startswith("_"):
            continue
        if key not in allowed:
            raise LetterError("%s：不認得的欄位「%s」（可用：%s）" % (label, key, "、".join(sorted(allowed))))
        if key == "text":
            out["text"] = str(value)
        elif key == "center":
            out["center"] = _point(value, label + " 的 center")
        elif key == "tail":
            out["tail"] = None if value is None else _point(value, label + " 的 tail")
        elif key == "scale":
            try:
                scale = float(value)
            except (TypeError, ValueError):
                raise LetterError("%s 的 scale 要是數字，目前是 %r" % (label, value))
            if not SCALE_MIN <= scale <= SCALE_MAX:
                raise LetterError("%s 的 scale 要在 %g–%g 之間，目前是 %g" % (label, SCALE_MIN, SCALE_MAX, scale))
            out["scale"] = scale
        elif key == "rows":
            if isinstance(value, bool) or not isinstance(value, int) or not ROWS_MIN <= value <= ROWS_MAX:
                raise LetterError("%s 的 rows 要是 %d–%d 的整數，目前是 %r" % (label, ROWS_MIN, ROWS_MAX, value))
            out["rows"] = value
    return out


def _sfx_override(item, label):
    if item is None:
        return {}
    if not isinstance(item, dict):
        raise LetterError("%s 要是 {box, angle, vertical} 的形式" % label)
    allowed = {"box", "angle", "vertical"}
    out = {}
    for key, value in item.items():
        if key.startswith("_"):
            continue
        if key not in allowed:
            raise LetterError("%s：不認得的欄位「%s」（可用：%s）" % (label, key, "、".join(sorted(allowed))))
        if key == "box":
            out["box"] = _box(value, label + " 的 box")
        elif key == "angle":
            try:
                angle = float(value)
            except (TypeError, ValueError):
                raise LetterError("%s 的 angle 要是數字，目前是 %r" % (label, value))
            if abs(angle) > ANGLE_LIMIT:
                raise LetterError("%s 的 angle 要在 ±%g 度之間，目前是 %g" % (label, ANGLE_LIMIT, angle))
            out["angle"] = angle
        elif key == "vertical":
            if not isinstance(value, bool):
                raise LetterError("%s 的 vertical 要是 true 或 false，目前是 %r" % (label, value))
            out["vertical"] = value
    return out


def check_panel_letters(panel_id, entry):
    """驗證並整理一格的微調：{"faces": [框] 或 None, "bubbles": [...], "sfx": [...]}。"""
    where = "letters 的「%s」" % panel_id
    if not isinstance(entry, dict):
        raise LetterError("%s 要是 {faces, bubbles, sfx} 的形式" % where)
    for key in entry:
        if not key.startswith("_") and key not in ("faces", "bubbles", "sfx"):
            raise LetterError("%s：不認得的欄位「%s」（可用：faces、bubbles、sfx）" % (where, key))
    faces = entry.get("faces")
    if faces is not None:
        if not isinstance(faces, list):
            raise LetterError("%s：faces 要是框的清單（[] 表示不避開任何東西）" % where)
        faces = [_box(box, "%s 的 faces 第 %d 個" % (where, i + 1)) for i, box in enumerate(faces)]
    return {"faces": faces,
            "bubbles": _entries(entry.get("bubbles"), where + " 的 bubbles", _bubble_override),
            "sfx": _entries(entry.get("sfx"), where + " 的 sfx", _sfx_override)}


def check_letters(data, label="letters"):
    if not isinstance(data, dict) or not isinstance(data.get("panels", {}), dict):
        raise LetterError("%s 要是 {\"version\": 1, \"panels\": {格 id: {...}}}" % label)
    if data.get("version", LETTERS_VERSION) != LETTERS_VERSION:
        raise LetterError("%s：不認得的格式版本 %r（目前是 %d）" % (label, data.get("version"), LETTERS_VERSION))
    return {pid: check_panel_letters(pid, entry) for pid, entry in (data.get("panels") or {}).items()}


def load_letters(project, chapter):
    """讀 letters/chNNN.json；檔案不存在就回傳空的 {}。"""
    path = letters_path(project, chapter)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LetterError("%s 不是合法的 JSON：%s" % (path, error))
    return check_letters(data, str(path))


def clean_entry(entry):
    """存檔時去掉預設值，只留和自動不同的部分。"""
    out = {}
    if entry.get("faces") is not None:
        out["faces"] = [[round(v, 4) for v in box] for box in entry["faces"]]
    for key in ("bubbles", "sfx"):
        items = [{k: ([round(x, 4) for x in v] if isinstance(v, list) else (round(v, 4) if isinstance(v, float) else v))
                  for k, v in (item or {}).items()} for item in entry.get(key) or []]
        while items and not items[-1]:
            items.pop()
        if items:
            out[key] = items
    return out


NUMBER_LIST = re.compile(r"\[\s*(-?[\d.]+(?:,\s*-?[\d.]+)*)\s*\]")      # 存檔時把座標寫成一行


def save_letters(project, chapter, letters):
    path = letters_path(project, chapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    panels = {}
    for pid in sorted(letters):
        cleaned = clean_entry(letters[pid])
        if cleaned:
            panels[pid] = cleaned
    data = {"version": LETTERS_VERSION, "chapter": int(chapter), "panels": panels}
    text = json.dumps(data, ensure_ascii=False, indent=2)
    text = NUMBER_LIST.sub(lambda m: "[" + ", ".join(v.strip() for v in m.group(1).split(",")) + "]", text)
    path.write_text(text + "\n", encoding="utf-8")
    return path


# ---- 直排／橫排的排字 ---------------------------------------------------------------------

CENTER_PUNCT = set("，。、．・,.")                                   # 放在字格正中（台灣直排習慣）
ROTATE = set("「」『』（）()〈〉《》【】〔〕［］｛｝[]{}：；:;～〜ー—─–-…‥⋯")   # 直排時轉 90 度
RUNS = set("…‥⋯—─ー～〜")                                          # 連續的不拆開
NO_START = set("，。、．・：；！？」』）》〉】〕］｝…‥⋯～〜ー—々,.!?:;)]}")  # 不放欄首
NO_END = set("「『（《〈【〔［｛([{")                                # 不放欄尾
TCY_PAIRS = {"！？", "？！", "！！", "？？", "!?", "?!", "!!", "??"}
ASCII_RUN = re.compile(r"[A-Za-z0-9!?'\"&%#+=*/.,:;@$-]+")


def tokenize(text, vertical=True):
    """台詞 → 排字單位 [kind, 文字, 佔幾格]；kind：c 一般、p 置中標點、r 旋轉、t 縱中橫、br 強制換欄。"""
    units, i, text = [], 0, str(text or "").replace("\r\n", "\n")
    while i < len(text):
        ch = text[i]
        if ch == "\n":
            units.append(["br", "", 0])
            i += 1
            continue
        if not vertical:
            units.append(["c", ch, 0.5 if ord(ch) < 0x2E80 else 1])
            i += 1
            continue
        pair = text[i:i + 2]
        if pair in TCY_PAIRS:
            units.append(["t", pair, 1])
            i += 2
            continue
        if ch in RUNS:
            j = i
            while j < len(text) and text[j] == ch:
                j += 1
            units.append(["r", text[i:j], j - i])
            i = j
            continue
        match = ASCII_RUN.match(text, i)
        if match and ord(ch) < 0x80:
            run = match.group(0)
            if len(run) <= 2 and (run.isdigit() or run in TCY_PAIRS or run in ("!", "?")):
                units.append(["t", run, 1])
            else:
                units.append(["r", run, max(1, math.ceil(len(run) * 0.55))])
            i = match.end()
            continue
        if ch in CENTER_PUNCT:
            units.append(["p", ch, 1])
        elif ch in ROTATE:
            units.append(["r", ch, 1])
        else:
            units.append(["c", ch, 1])
        i += 1
    return units


def _span(line):
    return sum(u[2] for u in line)


BREAK_AFTER = set("，。、！？；：…‥⋯—」』）")                   # 在這些後面換欄比較自然


def _soft_carry(line, rows):
    """欄滿了：往回最多 rows // 3 格找標點，把標點後面的字帶到下一欄（避免把詞拆開）。"""
    slack = max(1, rows // 3)
    moved = 0
    for k in range(len(line) - 1, 0, -1):
        if line[k - 1][1][-1:] in BREAK_AFTER and line[k - 1][0] != "t" and line[k][1][:1] not in NO_START:
            return line[k:] if moved + line[k][2] <= slack else []
        moved += line[k][2]
        if moved > slack:
            break
    return []


def break_lines(units, rows):
    """依每欄最多 rows 格斷欄，套用禁則：NO_START 不放欄首（必要時懸在欄尾一格）、NO_END 不放欄尾；
    欄尾附近有標點時在標點後換欄。"""
    lines, used = [[]], 0
    for unit in units:
        if unit[0] == "br":
            lines.append([])
            used = 0
            continue
        span = unit[2]
        if used + span > rows and lines[-1]:
            head = unit[1][:1]
            if head in NO_START and span <= 1 and used + span <= rows + 1:
                lines[-1].append(unit)                 # 懸在欄尾
                used += span
                continue
            carry = [] if head in NO_START else _soft_carry(lines[-1], rows)
            if carry:
                del lines[-1][len(lines[-1]) - len(carry):]
            while len(lines[-1]) > 1 and lines[-1][-1][1][-1:] in NO_END:
                carry.insert(0, lines[-1].pop())       # 開括號移到下一欄
            if not carry and head in NO_START:            # 往回帶到第一個不是標點的字，標點才不會在欄首
                while len(lines[-1]) > 1:
                    carry.insert(0, lines[-1].pop())
                    if carry[0][1][:1] not in NO_START:
                        break
                if carry and carry[0][1][:1] in NO_START:   # 整串都是標點：寧可超出，也不放欄首
                    lines[-1].extend(carry)
                    lines[-1].append(unit)
                    used += _span(carry) + span
                    continue
            lines.append(carry)
            used = _span(carry)
        lines[-1].append(unit)
        used += span
    return [line for line in lines if line] or [[]]


def block_size(lines, cell, vertical=True):
    """排好的字塊 (寬, 高)，像素。直排：欄距 1.3 格；橫排：行距 1.35 格。"""
    if vertical:
        cols = len(lines)
        return (cols * cell + (cols - 1) * 0.3 * cell, max(_span(l) for l in lines) * cell)
    rows = len(lines)
    return (max(_span(l) for l in lines) * cell, rows * cell + (rows - 1) * 0.35 * cell)


# ---- 對話框的大小 ---------------------------------------------------------------------------

def shape_axes(kind, tw, th, cell):
    """字塊 → 對話框的半徑 (rx, ry)（narration 是方框的半寬、半高）。保證字塊四角在框內。"""
    if kind == "narration":
        return tw / 2 + 0.55 * cell, th / 2 + 0.55 * cell
    pad = {"thought": 0.95, "shout": 0.75, "radio": 0.7}.get(kind, 0.6) * cell
    rx, ry = tw / 2 * 1.2 + pad, th / 2 * 1.12 + pad
    corner = (tw / 2 / rx) ** 2 + (th / 2 / ry) ** 2
    if corner > 0.92:                                   # 角落超出橢圓就等比放大
        grow = math.sqrt(corner / 0.92)
        rx, ry = rx * grow, ry * grow
    return rx, ry


def outer_axes(kind, rx, ry):
    """含外框突出（爆炸的尖角、雲的圓弧）後的範圍半徑，用來找位置。"""
    grow = {"shout": 1.16, "thought": 1.08, "radio": 1.06}.get(kind, 1.0)
    return rx * grow, ry * grow


def layout_text(kind, text, cell, vertical, max_w, max_h, rows=None):
    """選每欄字數：讓對話框接近目標比例、放得進 max_w × max_h。回傳 (lines, rx, ry, fits)。"""
    units = tokenize(text, vertical)
    total = max(1, int(math.ceil(sum(u[2] for u in units if u[0] != "br"))))
    segments = [[]]
    for unit in units:
        if unit[0] == "br":
            segments.append([])
        else:
            segments[-1].append(unit)
    longest = max(int(math.ceil(_span(s))) for s in segments) if any(segments) else 1
    target = 0.7 if vertical else 1.6                   # 寬／高：直排瘦高、橫排扁平
    if kind == "narration":
        target = 0.6 if vertical else 2.5
    if rows:
        choices = [rows]
    elif "\n" in str(text):
        choices = [min(longest, ROWS_MAX)]               # 有手動換行就照作者的分法
    elif total <= SHORT and kind != "narration":
        choices = [total] + list(range(min(2, total), total))   # 短句優先一欄到底（放不下才分欄）
    else:
        choices = range(min(2, total), min(total, ROWS_MAX) + 1)
    best = None
    for r in choices:
        lines = break_lines(units, r)
        tw, th = block_size(lines, cell, vertical)
        rx, ry = shape_axes(kind, tw, th, cell)
        ox, oy = outer_axes(kind, rx, ry)
        fits = 2 * ox <= max_w and 2 * oy <= max_h
        score = abs(math.log((rx / ry) / target))
        if len(lines) > 1:
            last = _span(lines[-1]) / max(_span(l) for l in lines)
            if last < 0.5:
                score += 0.5                             # 最後一欄只剩一兩個字
            score += 0.35 * sum(1 for l in lines[:-1] if l and l[-1][1][-1:] not in BREAK_AFTER)   # 斷在句中
            score += 0.12 * (len(lines) - 1)             # 欄數少一點比較好讀
        if not fits:
            score += 100 + (ox * oy) / (max_w * max_h)
        if r == total and total <= SHORT and kind != "narration" and fits:
            score -= 10
        if best is None or score < best[0]:
            best = (score, lines, rx, ry, fits)
    return best[1], best[2], best[3], best[4]


# ---- 幾何 ------------------------------------------------------------------------------------

def map_box(box, window):
    """分鏡的比例框（生成的圖）→ 裁切後的格子比例；完全在格子外回傳 None。"""
    x0, y0, x1, y1 = box
    w0, h0, w1, h1 = window
    sx, sy = (w1 - w0), (h1 - h0)
    out = [(x0 - w0) / sx, (y0 - h0) / sy, (x1 - w0) / sx, (y1 - h0) / sy]
    if out[2] <= 0 or out[3] <= 0 or out[0] >= 1 or out[1] >= 1:
        return None
    return [min(max(v, 0.0), 1.0) for v in out]


def map_point(point, window):
    w0, h0, w1, h1 = window
    return [min(max((point[0] - w0) / (w1 - w0), 0.0), 1.0), min(max((point[1] - h0) / (h1 - h0), 0.0), 1.0)]


def estimate_face(box_px, shot):
    """角色框（格子像素）→ 推估的臉框：框的上緣、置中，高度依鏡頭。"""
    x0, y0, x1, y1 = box_px
    fh = HEAD.get(shot or "medium", 0.3) * (y1 - y0)
    fw = min(x1 - x0, fh * 0.95)
    cx = (x0 + x1) / 2
    return [cx - fw / 2, y0, cx + fw / 2, y0 + fh]


def _inflate(box, d):
    return [box[0] - d, box[1] - d, box[2] + d, box[3] + d]


def _area(box):
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _inter(a, b):
    return [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]


def shape_overlap(shape, box, samples=7):
    """形狀（{"kind", "cx", "cy", "rx", "ry"}）和矩形重疊的面積（像素²，橢圓用取樣估計）。"""
    bound = [shape["cx"] - shape["rx"], shape["cy"] - shape["ry"], shape["cx"] + shape["rx"], shape["cy"] + shape["ry"]]
    inter = _inter(bound, box)
    area = _area(inter)
    if area <= 0 or shape["kind"] == "narration":
        return area
    inside = 0
    for i in range(samples):
        x = inter[0] + (i + 0.5) / samples * (inter[2] - inter[0])
        for j in range(samples):
            y = inter[1] + (j + 0.5) / samples * (inter[3] - inter[1])
            if ((x - shape["cx"]) / shape["rx"]) ** 2 + ((y - shape["cy"]) / shape["ry"]) ** 2 <= 1:
                inside += 1
    return area * inside / (samples * samples)


def _ray_box_entry(px, py, dx, dy, box):
    """從 (px, py) 沿 (dx, dy) 方向，進入 box 的距離；不會碰到回傳 None。"""
    tmin, tmax = 0.0, float("inf")
    for p, d, lo, hi in ((px, dx, box[0], box[2]), (py, dy, box[1], box[3])):
        if abs(d) < 1e-9:
            if p < lo or p > hi:
                return None
            continue
        t1, t2 = (lo - p) / d, (hi - p) / d
        tmin, tmax = max(tmin, min(t1, t2)), min(tmax, max(t1, t2))
    return tmin if tmin <= tmax else None


def edge_distance(kind, rx, ry, dx, dy):
    """中心沿單位方向 (dx, dy) 到框邊的距離。"""
    if kind == "narration":
        return min(rx / abs(dx) if abs(dx) > 1e-9 else float("inf"), ry / abs(dy) if abs(dy) > 1e-9 else float("inf"))
    return 1.0 / math.sqrt((dx / rx) ** 2 + (dy / ry) ** 2)


def auto_tail(bubble, face, body, cell):
    """尾巴尖端：指向說話者臉框下緣附近，停在臉外。沒有臉框就指向角色框上緣。回傳 [x, y] 或 None。"""
    if face is not None:
        tx, ty = (face[0] + face[2]) / 2, face[1] + 0.75 * (face[3] - face[1])
        stop = _inflate(face, 0.35 * cell)
    elif body is not None:
        tx, ty = (body[0] + body[2]) / 2, body[1] + 0.1 * (body[3] - body[1])
        stop = None
    else:
        return None
    cx, cy = bubble["cx"], bubble["cy"]
    dist = math.hypot(tx - cx, ty - cy)
    if dist < 1e-6:
        return None
    dx, dy = (tx - cx) / dist, (ty - cy) / dist
    edge = edge_distance(bubble["kind"], bubble["rx"], bubble["ry"], dx, dy)
    room = dist - edge
    if stop is not None:
        entry = _ray_box_entry(cx, cy, dx, dy, stop)
        if entry is not None:
            room = min(room, entry - edge)
    length = min(max(room * 0.7, 0.0), 0.9 * min(bubble["rx"], bubble["ry"]) + 1.2 * cell)
    length = max(length, 0.9 * cell)
    return [cx + dx * (edge + length), cy + dy * (edge + length)]


def offscreen_tail(bubble, inner, cell):
    """畫面外的說話者（M1 修正）：尾巴指向離對話框最近的格框邊（左右優先），尖端伸出框外，
    畫的時候被格框切掉，看起來像聲音從畫面外來。inner：扣掉框線的格子內框。"""
    cx, cy, ox, oy = bubble["cx"], bubble["cy"], bubble["ox"], bubble["oy"]
    gaps = {"left": (cx - ox) - inner[0], "right": inner[2] - (cx + ox),
            "top": 1.6 * ((cy - oy) - inner[1]), "bottom": 1.6 * (inner[3] - (cy + oy))}
    side = min(gaps, key=gaps.get)
    out = 1.5 * cell
    if side == "left":
        return [inner[0] - out, cy + 0.3 * oy]
    if side == "right":
        return [inner[2] + out, cy + 0.3 * oy]
    if side == "top":
        return [cx + 0.25 * ox, inner[1] - out]
    return [cx + 0.25 * ox, inner[3] + out]


# ---- 排版計畫 ---------------------------------------------------------------------------------

def _quote(text, limit=8):
    t = str(text or "").replace("\n", "⏎")
    return t if len(t) <= limit else t[:limit] + "…"


def _px(point, rect):
    return [rect[0] + point[0] * (rect[2] - rect[0]), rect[1] + point[1] * (rect[3] - rect[1])]


def _box_px(box, rect):
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    return [rect[0] + box[0] * w, rect[1] + box[1] * h, rect[0] + box[2] * w, rect[1] + box[3] * h]


ORDER_COST = 2.0          # 右翻會先讀到後一個對話框：比移動 2 個對角線還貴，除非要壓到臉


def order_conflict(first, later, w, h):
    """右翻（日漫）閱讀：later（後一個對話框）會不會比 first 先被讀到。first、later 是外框 [x0, y0, x1, y1]，
    w、h 是格子大小。只抓明確的情況（M1 修正 M-2）：
    1. later 整個在 first 上面；
    2. later 起頭比較高、又在右邊（中心右移超過 5%）——「跑到右上方」；
    3. 兩個起頭差不多高（8% 內），later 整個在 first 右邊。
    later 在右下方、起頭明顯比較低的，讀者多半先往左讀，不算。"""
    ty, tx = 0.05 * h, 0.05 * w
    if later[3] <= first[1]:
        return True
    if later[1] < first[1] - ty and (later[0] + later[2]) / 2 > (first[0] + first[2]) / 2 + tx:
        return True
    return abs(later[1] - first[1]) <= 1.6 * ty and later[0] >= first[2] - tx


def _outer_box(item):
    return [item["cx"] - item["ox"], item["cy"] - item["oy"], item["cx"] + item["ox"], item["cy"] + item["oy"]]


def _place(kind, rx, ry, anchor, rect, margin, faces, bodies, placed, cell, objects=()):
    """在格子裡找對話框中心：成本＝離 anchor 的距離＋壓到臉／其他對話框／重要物件／身體＋閱讀順序。
    回傳 (cx, cy, face_hit)。"""
    ox, oy = outer_axes(kind, rx, ry)
    lo_x, hi_x = rect[0] + margin + ox, rect[2] - margin - ox
    lo_y, hi_y = rect[1] + margin + oy, rect[3] - margin - oy
    if lo_x > hi_x:
        lo_x = hi_x = (rect[0] + rect[2]) / 2
    if lo_y > hi_y:
        lo_y = hi_y = (rect[1] + rect[3]) / 2
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    diag = math.hypot(w, h)
    step = max(6.0, min(w, h) / 28.0)
    nx, ny = int((hi_x - lo_x) / step) + 1, int((hi_y - lo_y) / step) + 1
    xs = sorted(set([lo_x + i * (hi_x - lo_x) / max(nx - 1, 1) for i in range(nx)] + [min(max(anchor[0], lo_x), hi_x)]))
    ys = sorted(set([lo_y + j * (hi_y - lo_y) / max(ny - 1, 1) for j in range(ny)] + [min(max(anchor[1], lo_y), hi_y)]))
    face_boxes = [_inflate(f, 0.3 * cell) for f in faces]
    best = None
    for cx in xs:
        for cy in ys:
            shape = {"kind": kind, "cx": cx, "cy": cy, "rx": ox, "ry": oy}
            area = math.pi * ox * oy if kind != "narration" else 4 * ox * oy
            cost = math.hypot(cx - anchor[0], cy - anchor[1]) / diag
            face_hit = sum(shape_overlap(shape, f) / max(_area(f), 1.0) for f in face_boxes)
            cost += (5 + 30 * face_hit) if face_hit > 0 else 0
            mine = [cx - ox, cy - oy, cx + ox, cy + oy]
            for other in placed:
                gap = _inflate(_outer_box(other), 0.4 * cell)
                cost += 30 * shape_overlap(shape, gap) / area
                if order_conflict(_outer_box(other), mine, w, h):
                    cost += ORDER_COST                   # 右翻時會先讀到這一個（順序顛倒）
            cost += 0.4 * sum(shape_overlap(shape, b) for b in bodies) / area
            cost += 3 * sum(shape_overlap(shape, o) / max(_area(o), 1.0) for o in objects)
            if best is None or cost < best[0]:
                best = (cost, cx, cy, face_hit)
    return best[1], best[2], best[3]


def plan_page(project, chapter, page, pages_data, picks, letters, settings):
    """算出一頁所有對話框與狀聲詞的位置。回傳可以轉 JSON 的 dict（座標都是整頁像素）：
    {"page", "size", "dpi", "warnings", "panels": [{"id", "slot", "rect", "window", "faces", "faces_auto",
      "bubbles": [{"index", "kind", "text", "speaker", "offscreen", "center", "rx", "ry", "tail", "anchor", "cell",
                   "lines", "auto_center", "auto_tail", "scale", "rows", "clip"}],
      "sfx": [{"index", "text", "box", "angle", "vertical"}]}]}"""
    cfg = pages_data["page"]
    dpi = compose.page_dpi(cfg)
    frame = int(cfg.get("frame_px", 0))
    sfx_overlay = project.config.get("sfx_mode", "overlay") == "overlay"
    infos, _ = compose.page_panels(project, chapter, page, pages_data, picks)   # 警告已由組版回報
    result = {"page": int(page["page"]), "size": list(layout.page_size(cfg)), "dpi": dpi, "warnings": [], "panels": []}
    warn = result["warnings"].append

    for info in infos:
        panel, rect, window, where = info["panel"], info["rect"], info["window"], info["where"]
        tweak = letters.get(info["id"]) or {"faces": None, "bubbles": [], "sfx": []}
        chars = {}
        for entry in panel.get("characters") or []:
            box = map_box(layout.check_box(entry.get("box", [0.25, 0.1, 0.75, 1])), window)
            chars[entry.get("id")] = _box_px(box, rect) if box else None
        if tweak["faces"] is not None:
            faces = [_box_px(box, rect) for box in tweak["faces"]]
            face_of = {}
            for cid, body in chars.items():           # 說話者的臉＝中心落在角色框裡、最上面的那個臉框
                inside = [f for f in faces if body and body[0] <= (f[0] + f[2]) / 2 <= body[2]
                          and body[1] - 0.1 * (body[3] - body[1]) <= (f[1] + f[3]) / 2 <= body[3]]
                face_of[cid] = min(inside, key=lambda f: f[1]) if inside else None
        else:
            face_of = {cid: estimate_face(body, panel.get("shot")) if body else None for cid, body in chars.items()}
            faces = [f for f in face_of.values() if f]
        bodies = [b for b in chars.values() if b]
        objects = []
        for obj in panel.get("objects") or []:
            mapped = map_box(layout.check_box(obj.get("box", [0, 0, 1, 1])), window)
            if mapped and _area(mapped) < 0.5:                 # 蓋住大半格的物件（特寫）不算
                objects.append(_box_px(mapped, rect))
        inner = [rect[0] + frame, rect[1] + frame, rect[2] - frame, rect[3] - frame]     # 框線以內：尾巴伸出去會被框切掉
        out = {"id": info["id"], "slot": info["slot"], "rect": list(rect), "window": window,
               "faces": [[round(v, 1) for v in f] for f in faces], "faces_auto": tweak["faces"] is None,
               "bubbles": [], "sfx": []}

        bubbles = panel.get("bubbles") or []
        if len(tweak["bubbles"]) > len(bubbles):
            warn("%s：letters 的 bubbles 比分鏡多 %d 個，多的已忽略" % (where, len(tweak["bubbles"]) - len(bubbles)))
        placed = []
        for index, bubble in enumerate(bubbles):
            kind = bubble.get("kind", "speech")
            if kind not in KINDS:
                raise LetterError("%s：對話框種類「%s」不認得" % (where, kind))
            text = str(bubble.get("text") or "")
            over = tweak["bubbles"][index] if index < len(tweak["bubbles"]) else {}
            if "text" in over and over["text"] != text:
                warn("%s 第 %d 個對話框：台詞改過了（微調時是「%s」），微調可能不適用" % (where, index + 1, _quote(over["text"], 20)))
            speaker = bubble.get("speaker") if kind != "narration" else None
            offscreen = bool(bubble.get("offscreen")) and kind != "narration"
            default_anchor = [0.88, 0.1] if kind == "narration" else [0.5, 0.15]
            anchor = _px(map_point(bubble.get("anchor", default_anchor), window), rect)
            role = ROLE_OF_KIND[kind]
            pt = settings.narration_pt if kind == "narration" else settings.dialogue_pt
            margin = frame + (3 if kind == "narration" else 10)
            avail_w, avail_h = rect[2] - rect[0] - 2 * margin, rect[3] - rect[1] - 2 * margin
            scale = over.get("scale", 1.0)
            for fit in FIT_SCALES:
                cell = pt * dpi / 72.0 * scale * fit * (1.08 if kind == "shout" else 1.0)
                lines, rx, ry, fits = layout_text(kind, text, cell, settings.vertical, avail_w, avail_h, over.get("rows"))
                if fits or "scale" in over:
                    break
            if not fits:
                warn("%s 第 %d 個對話框：字太多，縮小後仍超出格子" % (where, index + 1))
            elif fit < 1.0:
                warn("%s 第 %d 個對話框：格子太小，字級自動縮成 %d%%" % (where, index + 1, round(fit * 100)))
            ox, oy = outer_axes(kind, rx, ry)
            if "center" in over:
                cx, cy = _px(over["center"], rect)
                cx = min(max(cx, rect[0] + ox), rect[2] - ox) if rect[2] - rect[0] > 2 * ox else (rect[0] + rect[2]) / 2
                cy = min(max(cy, rect[1] + oy), rect[3] - oy) if rect[3] - rect[1] > 2 * oy else (rect[1] + rect[3]) / 2
                shape = {"kind": kind, "cx": cx, "cy": cy, "rx": ox, "ry": oy}
                face_hit = sum(shape_overlap(shape, f) / max(_area(f), 1.0) for f in faces)
            else:
                cx, cy, face_hit = _place(kind, rx, ry, anchor, rect, margin, faces, bodies, placed, cell, objects)
            if face_hit > 0.02:
                warn("%s 第 %d 個對話框（%s）：壓到臉了，請在介面調整" % (where, index + 1, _quote(text)))
            item = {"kind": kind, "cx": cx, "cy": cy, "rx": rx, "ry": ry, "ox": ox, "oy": oy}
            placed.append(item)
            if kind == "narration":
                tail, auto_tail_flag = None, True
            elif "tail" in over:
                tail, auto_tail_flag = over["tail"] and _px(over["tail"], rect), False
            elif offscreen:
                tail, auto_tail_flag = (None if kind == "radio" else offscreen_tail(item, inner, cell)), True
            elif speaker:
                if speaker not in chars:
                    raise LetterError("%s 第 %d 個對話框：說話的角色「%s」不在這一格（人在畫面外就加 \"offscreen\": true）"
                                      % (where, index + 1, speaker))
                tail, auto_tail_flag = auto_tail(item, face_of.get(speaker), chars.get(speaker), cell), True
            else:
                tail, auto_tail_flag = None, True
            out["bubbles"].append({
                "index": index, "kind": kind, "text": text, "speaker": speaker, "offscreen": offscreen,
                "center": [round(cx, 1), round(cy, 1)], "rx": round(rx, 1), "ry": round(ry, 1),
                "tail": [round(v, 1) for v in tail] if tail else None, "anchor": [round(v, 1) for v in anchor],
                "cell": round(cell, 2), "lines": lines, "vertical": settings.vertical,
                "auto_center": "center" not in over, "auto_tail": auto_tail_flag,
                "scale": scale, "rows": over.get("rows"), "role": role,
                "clip": [round(v, 1) for v in inner], "order_issue": None,
            })

        w, h = rect[2] - rect[0], rect[3] - rect[1]
        for j, later in enumerate(placed):
            for i in range(j):
                if order_conflict(_outer_box(placed[i]), _outer_box(later), w, h):
                    out["bubbles"][j]["order_issue"] = i + 1
                    warn("%s 第 %d 個對話框（%s）會比第 %d 個（%s）先讀到：右翻是右邊、上面的先讀。請改分鏡的 anchor 或對話框順序，或在嵌字頁拖曳"
                         % (where, j + 1, _quote(out["bubbles"][j]["text"]), i + 1, _quote(out["bubbles"][i]["text"])))
                    break

        if sfx_overlay:
            for index, sfx in enumerate(panel.get("sfx") or []):
                over = tweak["sfx"][index] if index < len(tweak["sfx"]) else {}
                if "box" in over:
                    box = _box_px(over["box"], rect)
                else:
                    mapped = map_box(layout.check_box(sfx.get("box", [0.6, 0.05, 0.95, 0.4])), window)
                    if mapped is None:
                        warn("%s 第 %d 個狀聲詞被裁切到格子外了，請在介面調整" % (where, index + 1))
                        continue
                    box = _box_px(mapped, rect)
                vertical = over.get("vertical", (box[3] - box[1]) > (box[2] - box[0]) * 1.1)
                out["sfx"].append({"index": index, "text": str(sfx.get("text") or ""),
                                   "box": [round(v, 1) for v in box], "angle": over.get("angle", -8.0),
                                   "vertical": vertical})
        result["panels"].append(out)
    return result


# ---- 畫 -----------------------------------------------------------------------------------------

def _bezier(p0, c, p1, steps=10):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * c[0] + t * t * p1[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * c[1] + t * t * p1[1]) for t in (i / steps for i in range(steps + 1))]


def tail_shape(kind, cx, cy, rx, ry, tip, cell):
    """尾巴：從框內一點長出、尖端在 tip 的微彎三角形。回傳 (兩條邊, 多邊形)；tip 在框內回傳 None。"""
    dx, dy = tip[0] - cx, tip[1] - cy
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return None
    ux, uy = dx / dist, dy / dist
    edge = edge_distance(kind, rx, ry, ux, uy)
    if dist <= edge + 0.2 * cell:
        return None
    base = edge - min(0.5 * cell, 0.25 * edge)                # 底邊在框內，接縫才會被蓋掉
    bx, by = cx + ux * base, cy + uy * base
    half = min(0.42 * cell, 0.3 * min(rx, ry))
    px, py = -uy, ux
    b1, b2 = (bx + px * half, by + py * half), (bx - px * half, by - py * half)
    length = dist - base
    bend = 0.18 * length
    mid = (bx + ux * length * 0.55 + px * bend, by + uy * length * 0.55 + py * bend)
    e1, e2 = _bezier(b1, (mid[0] + px * half * 0.5, mid[1] + py * half * 0.5), tip), \
        _bezier(b2, (mid[0] - px * half * 0.5, mid[1] - py * half * 0.5), tip)
    return (e1, e2), e1 + e2[::-1]


def burst_points(cx, cy, rx, ry, seed, tip=None):
    """爆炸框：尖角在 1.15–1.3 倍、凹處在 0.95 倍；有 tip 時把最接近的尖角換成 tip。"""
    rng = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    count = max(12, min(22, int((rx + ry) / 22)))
    points = []
    for i in range(count * 2):
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        jitter = (rng % 1000) / 1000.0
        angle = math.pi * 2 * (i + 0.3 * (jitter - 0.5)) / (count * 2)
        r = (1.1 + 0.12 * jitter) if i % 2 == 0 else 0.96
        points.append((cx + math.cos(angle) * rx * r, cy + math.sin(angle) * ry * r))
    if tip is not None:
        aim = math.atan2(tip[1] - cy, tip[0] - cx)
        nearest = min(range(0, len(points), 2),
                      key=lambda k: abs(math.atan2(math.sin(math.atan2(points[k][1] - cy, points[k][0] - cx) - aim),
                                                   math.cos(math.atan2(points[k][1] - cy, points[k][0] - cx) - aim))))
        points[nearest] = tuple(tip)
    return points


def radio_points(cx, cy, rx, ry, cell):
    """電子音框：沿橢圓的規則鋸齒，齒距約半個字。"""
    perim = math.pi * (3 * (rx + ry) - math.sqrt((3 * rx + ry) * (rx + 3 * ry)))
    count = max(16, int(perim / (0.55 * cell)))
    count += count % 2
    points = []
    for i in range(count):
        a = 2 * math.pi * i / count
        r = 1.06 if i % 2 == 0 else 0.98
        points.append((cx + math.cos(a) * rx * r, cy + math.sin(a) * ry * r))
    return points


_INK = {}


def _ink_offset(font, text):
    """用 anchor="mm" 畫在 (0, 0) 時，墨跡中心的位置（getbbox 的水平範圍是字寬，不是墨跡，所以實際畫一次量）。"""
    key = (font.path, font.size, getattr(font, "index", 0), text)
    if key not in _INK:
        side = int(font.size * 3)
        img = Image.new("L", (side, side), 0)
        ImageDraw.Draw(img).text((side / 2, side / 2), text, font=font, fill=255, anchor="mm")
        box = img.getbbox()
        _INK[key] = (0.0, 0.0) if box is None else ((box[0] + box[2]) / 2 - side / 2, (box[1] + box[3]) / 2 - side / 2)
    return _INK[key]


def _paste_unit(ink, mask, unit, x, y, cell, font, stroke=0, sfx=False):
    """在字格中心 (x, y) 畫一個排字單位（直排）。ink：0 黑、255 白；mask：255 不透明。"""
    kind, text, span = unit
    draw_ink, draw_mask = ImageDraw.Draw(ink), ImageDraw.Draw(mask)
    halo = int(round(cell * 0.14)) if sfx else 0                # 狀聲詞的白邊
    if kind in ("c", "p"):
        ax, ay = x, y
        if kind == "p":                                       # 標點：墨跡放在字格正中
            dx, dy = _ink_offset(font, text)
            ax, ay = x - dx, y - dy
        if halo:
            draw_ink.text((ax, ay), text, font=font, fill=255, anchor="mm", stroke_width=halo + stroke, stroke_fill=255)
            draw_mask.text((ax, ay), text, font=font, fill=255, anchor="mm", stroke_width=halo + stroke, stroke_fill=255)
        draw_ink.text((ax, ay), text, font=font, fill=0, anchor="mm", stroke_width=stroke, stroke_fill=0)
        draw_mask.text((ax, ay), text, font=font, fill=255, anchor="mm", stroke_width=stroke, stroke_fill=255)
        return
    pad = int(halo + stroke + cell * 0.2)
    if kind == "r":                                           # 橫著畫在 span 格長的條上，再順時針轉 90 度
        width = int(math.ceil(max(span * cell, font.getlength(text)))) + 2 * pad
        height = int(math.ceil(cell)) + 2 * pad
        glyph = Image.new("L", (width, height), 0)
        halo_img = Image.new("L", (width, height), 0)
        center = (width / 2, height / 2)
        ImageDraw.Draw(glyph).text(center, text, font=font, fill=255, anchor="mm", stroke_width=stroke, stroke_fill=255)
        if halo:
            ImageDraw.Draw(halo_img).text(center, text, font=font, fill=255, anchor="mm", stroke_width=halo + stroke,
                                          stroke_fill=255)
        glyph, halo_img = glyph.rotate(-90, expand=True), halo_img.rotate(-90, expand=True)
    else:                                                     # 縱中橫：橫著畫，太寬就壓窄到一格
        tw = font.getlength(text)
        width, height = int(math.ceil(tw)) + 2 * pad, int(math.ceil(cell)) + 2 * pad
        glyph = Image.new("L", (width, height), 0)
        halo_img = Image.new("L", (width, height), 0)
        center = (width / 2, height / 2)
        ImageDraw.Draw(glyph).text(center, text, font=font, fill=255, anchor="mm", stroke_width=stroke, stroke_fill=255)
        if halo:
            ImageDraw.Draw(halo_img).text(center, text, font=font, fill=255, anchor="mm", stroke_width=halo + stroke,
                                          stroke_fill=255)
        if tw > cell * 0.95:
            new_w = max(1, int(round(width * cell * 0.95 / tw)))
            glyph, halo_img = glyph.resize((new_w, height), Image.LANCZOS), halo_img.resize((new_w, height), Image.LANCZOS)
    left, top = int(round(x - glyph.size[0] / 2)), int(round(y - glyph.size[1] / 2))
    if halo:
        ink.paste(255, (left, top), halo_img)
        mask.paste(255, (left, top), halo_img)
    ink.paste(0, (left, top), glyph)
    mask.paste(255, (left, top), glyph)


def draw_text(ink, mask, lines, cx, cy, cell, font, vertical=True, stroke=0, sfx=False):
    """把排好的 lines 畫在 (cx, cy) 置中。直排：欄由右到左、每欄上下置中。"""
    tw, th = block_size(lines, cell, vertical)
    if vertical:
        pitch = 1.3 * cell
        right = cx + tw / 2
        for col, line in enumerate(lines):
            x = right - cell / 2 - col * pitch
            y = cy - _span(line) * cell / 2
            for unit in line:
                _paste_unit(ink, mask, unit, x, y + unit[2] * cell / 2, cell, font, stroke, sfx)
                y += unit[2] * cell
    else:
        draw_ink, draw_mask = ImageDraw.Draw(ink), ImageDraw.Draw(mask)
        top = cy - th / 2
        halo = int(round(cell * 0.14)) if sfx else 0
        for row, line in enumerate(lines):
            text = "".join(u[1] for u in line)
            y = top + row * 1.35 * cell + cell / 2
            if halo:
                draw_ink.text((cx, y), text, font=font, fill=255, anchor="mm", stroke_width=halo + stroke, stroke_fill=255)
            draw_ink.text((cx, y), text, font=font, fill=0, anchor="mm", stroke_width=stroke, stroke_fill=0)
            draw_mask.text((cx, y), text, font=font, fill=255, anchor="mm", stroke_width=halo + stroke, stroke_fill=255)


def _bubble_bounds(b, cell):
    ox, oy = outer_axes(b["kind"], b["rx"], b["ry"])
    xs = [b["center"][0] - ox, b["center"][0] + ox]
    ys = [b["center"][1] - oy, b["center"][1] + oy]
    if b.get("tail"):
        xs.append(b["tail"][0])
        ys.append(b["tail"][1])
    pad = 0.6 * cell + 6
    return [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]


def render_bubble(canvas, rect, b, settings, seed):
    """畫一個對話框（形狀＋尾巴＋字）。先在放大 SS 倍的小圖上畫，再縮回來貼到 canvas（限制在格子內）。"""
    cell = b["cell"]
    bounds = _inter(_bubble_bounds(b, cell), list(b.get("clip") or rect))
    x0, y0 = int(math.floor(bounds[0])), int(math.floor(bounds[1]))
    x1, y1 = int(math.ceil(bounds[2])), int(math.ceil(bounds[3]))
    if x1 <= x0 or y1 <= y0:
        return
    size = ((x1 - x0) * SS, (y1 - y0) * SS)
    ink, mask = Image.new("L", size, 255), Image.new("L", size, 0)
    di, dm = ImageDraw.Draw(ink), ImageDraw.Draw(mask)

    def pt(x, y):
        return ((x - x0) * SS, (y - y0) * SS)

    kind = b["kind"]
    cx, cy = pt(*b["center"])
    rx, ry, c = b["rx"] * SS, b["ry"] * SS, cell * SS
    w = int(round((2.0 if kind == "whisper" else 3.0 if kind != "shout" else 3.5) * SS))
    tip = pt(*b["tail"]) if b.get("tail") else None
    ell = (cx - rx, cy - ry, cx + rx, cy + ry)

    if kind == "narration":
        dm.rectangle(ell, fill=255)
        di.rectangle(ell, fill=255, outline=0, width=w)
    elif kind == "shout":
        points = burst_points(cx, cy, rx, ry, seed, tip)
        dm.polygon(points, fill=255)
        dm.line(points + [points[0]], fill=255, width=w, joint="curve")
        di.polygon(points, fill=255)
        di.line(points + [points[0]], fill=0, width=w, joint="curve")
    elif kind == "thought":
        perim = math.pi * (3 * (rx + ry) - math.sqrt((3 * rx + ry) * (rx + 3 * ry)))
        count = max(8, int(perim / (1.5 * c)))
        bump = perim / count * 0.62
        ring = [(cx + math.cos(2 * math.pi * k / count) * rx * 0.93, cy + math.sin(2 * math.pi * k / count) * ry * 0.93)
                for k in range(count)]
        dots = []
        if tip:
            dx, dy = tip[0] - cx, tip[1] - cy
            dist = math.hypot(dx, dy)
            ux, uy = dx / dist, dy / dist
            edge = edge_distance("speech", rx, ry, ux, uy) * 1.06
            span = max(dist - edge, c)
            for k, (frac, r) in enumerate(((0.22, 0.42), (0.6, 0.28), (1.0, 0.18))):
                d = edge + span * frac - (r * c if k == 2 else 0)
                dots.append((cx + ux * d, cy + uy * d, r * c))
        for x, y in ring:
            dm.ellipse((x - bump - w, y - bump - w, x + bump + w, y + bump + w), fill=255)
            di.ellipse((x - bump, y - bump, x + bump, y + bump), fill=0)
        for x, y, r in dots:
            dm.ellipse((x - r - w, y - r - w, x + r + w, y + r + w), fill=255)
            di.ellipse((x - r, y - r, x + r, y + r), fill=255, outline=0, width=w)
        for x, y in ring:
            di.ellipse((x - bump + w, y - bump + w, x + bump - w, y + bump - w), fill=255)
        dm.ellipse((cx - rx * 0.93, cy - ry * 0.93, cx + rx * 0.93, cy + ry * 0.93), fill=255)
        di.ellipse((cx - rx * 0.93, cy - ry * 0.93, cx + rx * 0.93, cy + ry * 0.93), fill=255)
    elif kind == "radio":                                     # 電子音：細密、規則的鋸齒（和喊叫的不規則爆炸不同）
        points = radio_points(cx, cy, rx, ry, c)
        tail = tail_shape("speech", cx, cy, rx, ry, tip, c) if tip else None
        dm.polygon(points, fill=255)
        dm.line(points + [points[0]], fill=255, width=w, joint="curve")
        di.polygon(points, fill=255)
        di.line(points + [points[0]], fill=0, width=w, joint="curve")
        if tail:
            edges, poly = tail
            dm.polygon(poly, fill=255)
            for e in edges:
                dm.line(e, fill=255, width=w, joint="curve")
            di.polygon(poly, fill=255)
            for e in edges:
                di.line(e, fill=0, width=w, joint="curve")
            shrink = 1 - w / max(min(rx, ry), 1.0)
            di.polygon([(cx + (x - cx) * shrink, cy + (y - cy) * shrink) for x, y in points], fill=255)
    else:                                                     # speech、whisper
        tail = tail_shape(kind, cx, cy, rx, ry, tip, c) if tip else None
        dm.ellipse(ell, fill=255)
        if kind == "whisper":
            di.ellipse(ell, fill=255)
        else:
            di.ellipse(ell, fill=255, outline=0, width=w)
        if tail:
            edges, poly = tail
            dm.polygon(poly, fill=255)
            for e in edges:
                dm.line(e, fill=255, width=w, joint="curve")
            di.polygon(poly, fill=255)
            for e in edges:
                di.line(e, fill=0, width=w, joint="curve")
            di.ellipse((cx - rx + w, cy - ry + w, cx + rx - w, cy + ry - w), fill=255)
        if kind == "whisper":                                 # 虛線外框；尾巴開口處不畫
            gap = None
            if tail:
                a1 = math.degrees(math.atan2(tail[0][0][0][1] - cy, tail[0][0][0][0] - cx))
                a2 = math.degrees(math.atan2(tail[0][1][0][1] - cy, tail[0][1][0][0] - cx))
                gap = (a1, a2)
            step = max(4.0, 360.0 * 0.45 * c / (math.pi * (rx + ry)))
            angle = 0.0
            while angle < 360:
                mid = angle + step * 0.3
                if gap is None or not _angle_between(mid, *gap):
                    di.arc(ell, angle, angle + step * 0.6, fill=0, width=w)
                angle += step

    role = b.get("role", ROLE_OF_KIND[kind])
    font = settings.font(role, c)
    stroke = int(round(c * 0.035)) if role in settings.faux_bold else 0
    draw_text(ink, mask, b["lines"], cx, cy, c, font, b.get("vertical", True), stroke)
    _paste_layer(canvas, ink, mask, x0, y0, x1 - x0, y1 - y0)


def _angle_between(angle, a1, a2):
    """angle（度）是否落在 a1、a2 之間比較小的那段弧。"""
    def norm(v):
        return (v + 360) % 360
    lo, hi = sorted((norm(a1), norm(a2)))
    angle = norm(angle)
    return lo <= angle <= hi if hi - lo <= 180 else (angle >= hi or angle <= lo)


def _paste_layer(canvas, ink, mask, x, y, w, h):
    if ink.size != (w, h):
        ink, mask = ink.resize((w, h), Image.LANCZOS), mask.resize((w, h), Image.LANCZOS)
    if canvas.mode != "L":
        ink = ink.convert(canvas.mode)
    canvas.paste(ink, (x, y), mask)


def render_sfx(canvas, rect, s, settings):
    """狀聲詞：黑字白邊，填滿框，轉 angle 度。"""
    box = s["box"]
    bw, bh = box[2] - box[0], box[3] - box[1]
    text = s["text"]
    if not text or bw <= 2 or bh <= 2:
        return
    vertical = s["vertical"]
    units = tokenize(text, vertical)
    lines = [[u for u in units if u[0] != "br"]]
    count = max(1.0, _span(lines[0]))
    probe = settings.font("sfx", 100)
    if vertical:
        cell = min(bw * 0.85, bh * 0.85 / count)
    else:
        length = max(1.0, probe.getlength("".join(u[1] for u in lines[0])))
        cell = min(bh * 0.8, bw * 0.85 * 100 / length)
    cell = max(cell, 12.0)
    font = settings.font("sfx", cell)
    stroke = int(round(cell * 0.04)) if "sfx" in settings.faux_bold else 0
    side = int(math.hypot(bw, bh) + cell)
    ink, mask = Image.new("L", (side, side), 255), Image.new("L", (side, side), 0)
    draw_text(ink, mask, lines, side / 2, side / 2, cell, font, vertical, stroke, sfx=True)
    angle = float(s.get("angle", 0.0))
    if angle:
        ink = ink.rotate(angle, resample=Image.BICUBIC, fillcolor=255)
        mask = mask.rotate(angle, resample=Image.BICUBIC, fillcolor=0)
    x = int(round((box[0] + box[2]) / 2 - side / 2))
    y = int(round((box[1] + box[3]) / 2 - side / 2))
    clip = [max(x, rect[0]), max(y, rect[1]), min(x + side, rect[2]), min(y + side, rect[3])]
    if clip[2] <= clip[0] or clip[3] <= clip[1]:
        return
    crop = (clip[0] - x, clip[1] - y, clip[2] - x, clip[3] - y)
    _paste_layer(canvas, ink.crop(crop), mask.crop(crop), clip[0], clip[1], clip[2] - clip[0], clip[3] - clip[1])


def draw_guides(canvas, plan):
    """檢查用：臉框（灰／紅）、anchor（點）、尾巴目標。"""
    draw = ImageDraw.Draw(canvas)
    color = (220, 30, 30) if canvas.mode != "L" else 110
    dot = (30, 90, 220) if canvas.mode != "L" else 60
    for panel in plan["panels"]:
        for face in panel["faces"]:
            draw.rectangle(face, outline=color, width=4)
        for b in panel["bubbles"]:
            x, y = b["anchor"]
            draw.ellipse((x - 9, y - 9, x + 9, y + 9), fill=dot)
            draw.line((x, y, b["center"][0], b["center"][1]), fill=dot, width=2)


def draw_page(canvas, plan, settings, guides=False):
    """把 plan_page() 的結果畫到 canvas（就地修改）。"""
    for panel in plan["panels"]:
        rect = panel["rect"]
        for s in panel["sfx"]:                               # 狀聲詞在下、對話框在上
            render_sfx(canvas, rect, s, settings)
        for b in panel["bubbles"]:
            render_bubble(canvas, rect, b, settings, "%s#%d" % (panel["id"], b["index"]))
    if guides:
        draw_guides(canvas, plan)
    return canvas


def letter_page(canvas, project, chapter, page, pages_data, picks, letters, settings, guides=False):
    """plan_page＋draw_page；回傳 (canvas, plan)。"""
    plan = plan_page(project, chapter, page, pages_data, picks, letters, settings)
    draw_page(canvas, plan, settings, guides)
    return canvas, plan
