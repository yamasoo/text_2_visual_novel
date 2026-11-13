"""組版：把每格選好的圖裁進格子、畫框、排成一整頁 PNG。

資料與規則見 docs/02_資料格式.md 第 8、9 節：
- picks/chNNN.json 記每格選哪張候選（image）與裁切焦點（focus、zoom）。
- 格子位置來自 t2c/data/pages.json 的模板（比例），換算成像素只在 layout.panel_rect_px() 做。
- 只用標準函式庫與 Pillow（核心規則：不依賴 FastAPI、fekit）。
"""
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import layout

PICKS_VERSION = 1
CANDIDATE_RE = re.compile(r"^(?P<id>.+)_s\d+(?:_[A-Za-z0-9-]+)?(?:__[A-Za-z0-9-]+)?\.png$")
ZOOM_MIN, ZOOM_MAX = 1.0, 4.0
PLACEHOLDER = 200      # 沒有圖的格子的灰底


class ComposeError(Exception):
    """給人看的錯誤訊息（會寫出是哪一頁哪一格）。"""


# ---- 選圖紀錄 ----------------------------------------------------------------------

def chapter_name(chapter):
    return "ch%03d" % int(chapter)


def picks_path(project, chapter):
    return project.root / "picks" / (chapter_name(chapter) + ".json")


def renders_dir(project, chapter):
    return project.root / "renders" / chapter_name(chapter)


def pages_dir(project, chapter):
    return project.root / "pages" / chapter_name(chapter)


def check_pick(panel_id, entry):
    """驗證並整理一筆選圖：回傳 {"image", "focus", "zoom"}（focus、zoom 補預設）。"""
    where = "picks 的「%s」" % panel_id
    if not isinstance(entry, dict):
        raise ComposeError("%s 要是 {image, focus, zoom} 的形式" % where)
    image = entry.get("image")
    if not isinstance(image, str) or not image or Path(image).name != image:
        raise ComposeError("%s：image 要是 renders/ 底下的純檔名（不含路徑），目前是 %r" % (where, image))
    focus = entry.get("focus", [0.5, 0.5])
    try:
        fx, fy = (float(v) for v in focus)
    except (TypeError, ValueError):
        raise ComposeError("%s：focus 要是 [x, y] 兩個 0–1 的數字，目前是 %r" % (where, focus))
    if not (0 <= fx <= 1 and 0 <= fy <= 1):
        raise ComposeError("%s：focus 要在 0–1 之間，目前是 %r" % (where, focus))
    try:
        zoom = float(entry.get("zoom", 1.0))
    except (TypeError, ValueError):
        raise ComposeError("%s：zoom 要是數字，目前是 %r" % (where, entry.get("zoom")))
    if not (ZOOM_MIN <= zoom <= ZOOM_MAX):
        raise ComposeError("%s：zoom 要在 %g–%g 之間，目前是 %g" % (where, ZOOM_MIN, ZOOM_MAX, zoom))
    return {"image": image, "focus": [fx, fy], "zoom": zoom}


def load_picks(project, chapter):
    """讀 picks/chNNN.json；檔案不存在就回傳空的 {}。"""
    path = picks_path(project, chapter)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ComposeError("%s 不是合法的 JSON：%s" % (path, error))
    if not isinstance(data, dict) or not isinstance(data.get("picks", {}), dict):
        raise ComposeError("%s 要是 {\"version\": 1, \"picks\": {格 id: {...}}}" % path)
    if data.get("version", PICKS_VERSION) != PICKS_VERSION:
        raise ComposeError("%s：不認得的格式版本 %r（目前是 %d）" % (path, data.get("version"), PICKS_VERSION))
    return {pid: check_pick(pid, entry) for pid, entry in (data.get("picks") or {}).items()}


def save_picks(project, chapter, picks):
    path = picks_path(project, chapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": PICKS_VERSION, "chapter": int(chapter),
            "picks": {pid: picks[pid] for pid in sorted(picks)}}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def candidates(project, chapter, panel_id):
    """renders/chNNN/ 裡這一格的候選檔名（<格 id>_s<數字>.png，含草稿後綴的），依檔名排序。"""
    folder = renders_dir(project, chapter)
    if not folder.is_dir():
        return []
    prefix = panel_id + "_s"
    names = []
    for path in folder.glob(prefix + "*.png"):
        match = CANDIDATE_RE.match(path.name)
        if match and match.group("id") == panel_id:
            names.append(path.name)
    return sorted(names)


def default_candidate(project, chapter, panel_id):
    """沒有選圖時暫用的候選：不帶預設後綴的（定稿）優先，其次依檔名第一個。"""
    names = candidates(project, chapter, panel_id)
    plain = [n for n in names if re.fullmatch(r".+_s\d+\.png", n)]
    return (plain or names or [None])[0]


# ---- 裁切 --------------------------------------------------------------------------

def crop_box(src_size, size, focus=(0.5, 0.5), zoom=1.0):
    """cover 裁切在原圖上取的範圍 (left, top, right, bottom)，像素。
    先蓋滿格子（取寬、高縮放比的較大者）、再乘 zoom；框盡量以 focus 為中心，不超出原圖。"""
    w, h = int(size[0]), int(size[1])
    if w <= 0 or h <= 0:
        raise ComposeError("格子尺寸不合理：%r" % (size,))
    src_w, src_h = src_size
    scale = max(w / src_w, h / src_h) * float(zoom)      # 原圖 → 格子的縮放倍率
    crop_w, crop_h = w / scale, h / scale                 # 在原圖上要取的範圍
    left = min(max(focus[0] * src_w - crop_w / 2.0, 0.0), src_w - crop_w)
    top = min(max(focus[1] * src_h - crop_h / 2.0, 0.0), src_h - crop_h)
    return (left, top, left + crop_w, top + crop_h)


def crop_window(src_size, size, focus=(0.5, 0.5), zoom=1.0):
    """同 crop_box，但換成原圖的比例 [x0, y0, x1, y1]（嵌字用來把分鏡座標換到格子上）。"""
    left, top, right, bottom = crop_box(src_size, size, focus, zoom)
    return [left / src_size[0], top / src_size[1], right / src_size[0], bottom / src_size[1]]


def crop_cover(img, size, focus=(0.5, 0.5), zoom=1.0):
    """把 img 裁成 size=(w, h)（cover 裁切，見 crop_box）。"""
    box = crop_box(img.size, size, focus, zoom)
    return img.resize((int(size[0]), int(size[1])), Image.LANCZOS, box=box)


# ---- 組版 --------------------------------------------------------------------------

def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                                     # Pillow < 10.1
        return ImageFont.load_default()


def _label(draw, rect, text, fill, size):
    """在格子右上角寫一個編號／標記（只用 ASCII，不依賴 CJK 字型）。"""
    font = _font(size)
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    x1, y0 = rect[2] - 14, rect[1] + 10
    pad = size // 4
    draw.rectangle((x1 - tw - 2 * pad, y0, x1, y0 + th + 2 * pad), fill=255)
    draw.text((x1 - tw - pad - box[0], y0 + pad - box[1]), text, fill=fill, font=font)


def page_panels(project, chapter, page, pages_data, picks):
    """一頁每一格的組版資訊（依 slot 排序），組版與嵌字共用：
    [{"slot", "id", "where", "panel"（分鏡的格）, "rect"（整頁像素）, "source"（Path 或 None）,
      "focus", "zoom", "window"（裁切範圍，原圖比例；沒有圖時是 [0, 0, 1, 1]）}]、warnings。"""
    tpl = pages_data["templates"].get(str(page.get("template")))
    if tpl is None:
        raise ComposeError("第 %s 頁：沒有這個格子模板「%s」（見 t2c/data/pages.json）" % (page.get("page"), page.get("template")))
    boxes = tpl["panels"]
    panels = {p.get("slot"): p for p in page.get("panels") or []}
    if sorted(panels) != list(range(1, len(boxes) + 1)):
        raise ComposeError("第 %s 頁：模板「%s」有 %d 格，分鏡的 slot 要剛好是 1–%d，目前是 %s"
                           % (page["page"], page["template"], len(boxes), len(boxes), sorted(panels)))
    cfg = pages_data["page"]
    result, warnings = [], []
    for slot, box in enumerate(boxes, 1):
        panel = panels[slot]
        pid = panel.get("id") or "p%03d_k%d" % (page["page"], slot)
        where = "第 %s 頁第 %d 格（%s）" % (page["page"], slot, pid)
        try:
            rect = layout.panel_rect_px(cfg, box)
        except layout.LayoutError as error:
            raise ComposeError("%s：%s" % (where, error))
        size = (rect[2] - rect[0], rect[3] - rect[1])

        pick = picks.get(pid)
        name = pick["image"] if pick else None
        if pick is None:
            name = default_candidate(project, chapter, pid)
            if name:
                warnings.append("%s：沒有選圖紀錄，暫用 %s" % (where, name))
        source = renders_dir(project, chapter) / name if name else None
        if source is not None and not source.is_file():
            raise ComposeError("%s：選的圖不存在：%s" % (where, source))
        focus = pick["focus"] if pick else [0.5, 0.5]
        zoom = pick["zoom"] if pick else 1.0
        window = [0.0, 0.0, 1.0, 1.0]
        if source is None:
            warnings.append("%s：沒有任何候選圖（renders/%s/ 裡找不到 %s_s*.png），先放灰底" % (where, chapter_name(chapter), pid))
        else:
            try:
                with Image.open(source) as img:               # 只讀檔頭拿尺寸
                    window = crop_window(img.size, size, focus, zoom)
            except OSError as error:
                raise ComposeError("%s：讀不了圖 %s（%s）" % (where, source.name, error))
        result.append({"slot": slot, "id": pid, "where": where, "panel": panel, "rect": rect, "source": source,
                       "focus": focus, "zoom": zoom, "window": window})
    return result, warnings


def auto_gray(project):
    """組版要不要轉灰階：預設保留原色（所見即所出），只有 project.yaml 明寫 output.gray: true 才轉。

    不依畫風預設集自動判斷：1.12.1 試過「mono 就轉灰階」，但沒寫 style_preset 的舊專案會被當成 mono，
    出圖彩色、頁面卻變黑白（1.12.2 改掉）。畫風預設集的 gray 只是那個畫風的建議值，要不要套用由人決定。"""
    output = project.config.get("output") if isinstance(project.config.get("output"), dict) else {}
    return output.get("gray") is True


def compose_page(project, chapter, page, pages_data, picks, gray=None, numbers=False):
    """組一頁（不含嵌字）。page 是分鏡裡的一頁。回傳 (PIL.Image, warnings)。"""
    infos, warnings = page_panels(project, chapter, page, pages_data, picks)
    cfg = pages_data["page"]
    width, height = layout.page_size(cfg)
    gray = auto_gray(project) if gray is None else bool(gray)
    mode, white, black = ("L", 255, 0) if gray else ("RGB", (255, 255, 255), (0, 0, 0))
    canvas = Image.new(mode, (width, height), white)
    draw = ImageDraw.Draw(canvas)
    frame = int(cfg.get("frame_px", 0))

    for info in infos:
        slot, rect, source, where = info["slot"], info["rect"], info["source"], info["where"]
        size = (rect[2] - rect[0], rect[3] - rect[1])
        if source is None:
            tile = Image.new(mode, size, PLACEHOLDER if gray else (PLACEHOLDER,) * 3)
        else:
            try:
                with Image.open(source) as img:
                    img.load()
                    tile = crop_cover(img.convert("L" if gray else "RGB"), size, info["focus"], info["zoom"])
            except OSError as error:
                raise ComposeError("%s：讀不了圖 %s（%s）" % (where, source.name, error))
        canvas.paste(tile, (rect[0], rect[1]))
        if frame > 0:
            draw.rectangle((rect[0], rect[1], rect[2] - 1, rect[3] - 1), outline=black, width=frame)
        if numbers:
            _label(draw, rect, str(slot), 0, max(36, size[1] // 10 if size[1] < 600 else 60))
        if source is None:
            _label(draw, (rect[0], rect[1], rect[2], rect[3]), "no image", 0, 36)
    return canvas, warnings


def page_dpi(cfg):
    width, _ = layout.page_size(cfg)
    return round(width / (float(cfg["width_mm"]) / 25.4))


def find_page(storyboard, page_no):
    for page in storyboard.get("pages") or []:
        if int(page["page"]) == int(page_no):
            return page
    raise ComposeError("分鏡裡沒有第 %s 頁" % page_no)


def _lettering(lettering):
    from . import letter                                   # 延後載入：只組版時不需要字型
    return lettering if isinstance(lettering, letter.Settings) else letter.Settings.from_config(lettering)


def render_page(project, chapter, page, pages_data=None, picks=None, gray=None, numbers=False, lettering=None,
                letters=None, guides=False, art=None):
    """一頁：組版（＋嵌字）。回傳 (PIL.Image, plan 或 None, warnings)。
    art 是已經組好的頁面（介面拖曳預覽時重用，避免每次重新裁圖）；會複製一份，不會改到它。"""
    pages_data = pages_data or project.pages()
    picks = load_picks(project, chapter) if picks is None else picks
    if art is None:
        canvas, warnings = compose_page(project, chapter, page, pages_data, picks, gray=gray, numbers=numbers)
    else:
        canvas, warnings = art.copy(), []
    plan = None
    if lettering is not None:
        from . import letter
        settings = _lettering(lettering)
        letters = letter.load_letters(project, chapter) if letters is None else letters
        plan = letter.plan_page(project, chapter, page, pages_data, picks, letters, settings)
        letter.draw_page(canvas, plan, settings, guides=guides)
        warnings = warnings + plan["warnings"]
    return canvas, plan, warnings


def save_page(canvas, path, pages_data):
    dpi = page_dpi(pages_data["page"])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, dpi=(dpi, dpi))
    return Path(path)


def compose_chapter(project, chapter, storyboard, pages_data=None, only_page=None, picks=None, gray=None,
                    numbers=False, out_dir=None, lettering=None, letters=None, guides=False):
    """整章（或 only_page）→ pages/chNNN/pNNN.png。回傳 [(頁碼, 路徑)]、警告清單。

    lettering 是 letter.Settings（或 config.json 的 lettering dict）時，組版後接著嵌字（t2c/letter.py）；
    None 表示只組版。letters 是嵌字微調（None 就讀 letters/chNNN.json）。guides 畫臉框與 anchor（檢查用）。"""
    pages_data = pages_data or project.pages()
    picks = load_picks(project, chapter) if picks is None else picks
    warnings = []
    if lettering is not None:
        from . import letter
        lettering = _lettering(lettering)
        letters = letter.load_letters(project, chapter) if letters is None else letters
        warnings += lettering.notes
    out = Path(out_dir) if out_dir else pages_dir(project, chapter)
    out.mkdir(parents=True, exist_ok=True)

    written, known = [], set()
    for page in storyboard.get("pages") or []:
        for panel in page.get("panels") or []:
            known.add(panel.get("id") or "p%03d_k%d" % (page["page"], panel.get("slot")))
        if only_page is not None and int(page["page"]) != int(only_page):
            continue
        canvas, _, warns = render_page(project, chapter, page, pages_data, picks, gray=gray, numbers=numbers,
                                       lettering=lettering, letters=letters, guides=guides)
        warnings += warns
        written.append((int(page["page"]), save_page(canvas, out / ("p%03d.png" % int(page["page"])), pages_data)))
    if only_page is not None and not written:
        raise ComposeError("分鏡裡沒有第 %s 頁" % only_page)
    for pid in sorted(set(picks) - known):
        warnings.append("picks 裡的「%s」不在這一章的分鏡裡，已忽略" % pid)
    if lettering is not None:
        for pid in sorted(set(letters) - known):
            warnings.append("letters 裡的「%s」不在這一章的分鏡裡，已忽略" % pid)
    return written, warnings
