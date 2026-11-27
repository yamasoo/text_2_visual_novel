"""頁面模板、格子尺寸、MOSS-Image 的 bbox 換算。

座標約定（整個專案都一樣）：
- 分鏡裡的位置一律是「比例」[x0, y0, x1, y1]，左上 (0,0)、右下 (1,1)。給 LLM 與人寫的都是這個。
- 只有送進 MOSS-Image 的那一刻才換成它的格式：像素、原點在畫布中心、順序 [y_min, x_min, y_max, x_max]
  （bbox_coord_space = "abs_centralized"、bbox_format = "yxyx"）。換算只在 to_moss_bbox() 做。

版型（N4）：預設讀套件自帶的 t2c/data/pages.json；專案可以在 project.yaml 用 layout 指定外部版型檔，
mode: merge（預設，疊加：同名的模板換掉、其他的留著）或 mode: replace（只用專案檔的模板）。

斜格（V3，決策 D19）：模板可以多一個 shapes，和 panels 一一對應；每一項是 null（直角格）或
3–8 個頂點的凸多邊形 [[x, y], …]（同樣是頁面內框的比例）。panels 的框必須是這個多邊形的外接矩形——
出圖、生圖尺寸、MOSS bbox 都照外接矩形走，只有組版與嵌字用多邊形把多出來的部分裁掉。
"""
import json
import math
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"      # 套件自帶的資料：整包搬走也讀得到（M1 修正 M-3）
PAGES_FILE = DATA / "pages.json"
BUILTIN_LABEL = "t2c/data/pages.json（內建）"

MOSS_COORD_SPACE = "abs_centralized"
MOSS_BBOX_FORMAT = "yxyx"

TEMPLATE_KEY = re.compile(r"^[a-z0-9_-]+$")
LAYOUT_MODES = {"merge": "疊加（同名的模板換掉，其他的留著）", "replace": "覆寫（只用專案檔的模板）"}


class LayoutError(Exception):
    """給人看的錯誤訊息（寫出是哪個檔案、哪個模板、第幾格）。"""


def _number(value, label, positive=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise LayoutError("%s 要是數字：%r" % (label, value))
    if number < 0 or (positive and number <= 0):
        raise LayoutError("%s 要是%s：%r" % (label, "大於 0 的數字" if positive else "大於等於 0 的數字", value))
    return number


def _check_page(page, label):
    """page 區塊的檢查：尺寸、頁邊、白邊。"""
    if not isinstance(page, dict):
        raise LayoutError("%s：page 要是物件（width_mm、height_mm、width_px…），目前是 %s" % (label, type(page).__name__))
    for key in ("width_mm", "height_mm", "width_px"):
        if key in page:
            _number(page[key], "%s：page.%s" % (label, key), positive=True)
    for key in ("gutter_px", "frame_px"):
        if key in page:
            _number(page[key], "%s：page.%s" % (label, key))
    if "margin" in page:
        margin = page["margin"]
        if not isinstance(margin, (list, tuple)) or len(margin) != 4:
            raise LayoutError("%s：page.margin 要是 [左, 上, 右, 下] 四個比例：%r" % (label, margin))
        left, top, right, bottom = (_number(v, "%s：page.margin" % label) for v in margin)
        if any(v >= 1 for v in (left, top, right, bottom)) or left + right >= 1 or top + bottom >= 1:
            raise LayoutError("%s：page.margin 要是 0–1 的比例，左右、上下加起來要小於 1：%r" % (label, margin))


def _check_templates(templates, label):
    if not isinstance(templates, dict) or not templates:
        raise LayoutError("%s 缺少 templates（至少要有一組模板；格式同 t2c/data/pages.json）" % label)
    for key, tpl in templates.items():
        if str(key).startswith("_"):
            continue
        if not TEMPLATE_KEY.match(str(key)):
            raise LayoutError("%s：模板「%s」的名稱只能用 a-z、0-9、底線、連字號" % (label, key))
        if not isinstance(tpl, dict):
            raise LayoutError("%s：模板「%s」要是物件（name、panels）：目前是 %s" % (label, key, type(tpl).__name__))
        if "name" in tpl and not isinstance(tpl["name"], str):
            raise LayoutError("%s：模板「%s」的 name 要是字串：%r" % (label, key, tpl["name"]))
        panels = tpl.get("panels")
        if not isinstance(panels, list) or not panels:
            raise LayoutError("%s：模板「%s」要有 panels（一串格子框，至少一個）" % (label, key))
        for i, box in enumerate(panels, 1):
            check_box(box, "%s：模板「%s」第 %d 格" % (label, key, i))
        if "mood" in tpl and not isinstance(tpl["mood"], str):
            raise LayoutError("%s：模板「%s」的 mood 要是字串（例如 \"action\"）：%r" % (label, key, tpl["mood"]))
        shapes = tpl.get("shapes")
        if shapes is not None:
            if not isinstance(shapes, list) or len(shapes) != len(panels):
                raise LayoutError("%s：模板「%s」的 shapes 要和 panels 一樣有 %d 項（直角格寫 null）"
                                  % (label, key, len(panels)))
            for i, (box, shape) in enumerate(zip(panels, shapes), 1):
                if shape is not None:
                    check_shape(shape, box, "%s：模板「%s」第 %d 格的 shape" % (label, key, i))


def validate_pages(data, label=BUILTIN_LABEL):
    """一份版型檔的檢查。回傳只留 page 與 templates 的 dict（_ 開頭的說明鍵不傳下去）。"""
    if not isinstance(data, dict):
        raise LayoutError("%s 最外層要是物件（{page, templates}）：目前是 %s" % (label, type(data).__name__))
    page = data.get("page") or {}
    _check_page(page, label)
    templates = data.get("templates")
    _check_templates(templates, label)
    return {"page": dict(page), "templates": {k: v for k, v in templates.items() if not str(k).startswith("_")}}


def load_pages(path=None):
    """讀一份版型檔（沒給就是套件內建的）並檢查。錯誤訊息會寫出檔案與欄位。"""
    path = Path(path or PAGES_FILE)
    label = BUILTIN_LABEL if path.resolve() == PAGES_FILE.resolve() else str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise LayoutError("找不到版型檔：%s" % path)
    except OSError as error:
        raise LayoutError("讀不到版型檔 %s：%s" % (path, error))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise LayoutError("%s 不是合法的 JSON：第 %d 行第 %d 欄：%s" % (label, error.lineno, error.colno, error.msg))
    return validate_pages(data, label)


def merge_pages(base, overlay, label=BUILTIN_LABEL, mode="merge"):
    """把 overlay 疊到 base 上：page 一層合併（overlay 有給的欄位才蓋掉）；merge 時模板同名換掉、
    replace 時只用 overlay 的模板。回傳新的 dict（兩個來源都不會被改到）。"""
    page = {**base["page"], **(overlay.get("page") or {})}
    if mode == "replace":
        templates = dict(overlay["templates"])
    else:
        templates = {**base["templates"], **overlay["templates"]}
    return validate_pages({"page": page, "templates": templates}, label)


def layout_spec(project):
    """project.yaml 的 layout 設定 → (路徑, 模式, 說明文字)；沒有設定回傳 (None, None, "")。"""
    spec = (getattr(project, "config", None) or {}).get("layout")
    if not spec:
        return None, None, ""
    if isinstance(spec, str):
        spec = {"file": spec}
    if not isinstance(spec, dict):
        raise LayoutError("project.yaml 的 layout 要是版型檔的路徑，或 {file, mode} 物件：%r" % (spec,))
    name = str(spec.get("file") or "").strip()
    if not name:
        raise LayoutError("project.yaml 的 layout 少了 file（專案裡的版型檔路徑，例如 pages.json）")
    mode = str(spec.get("mode") or "merge").strip().lower()
    if mode not in LAYOUT_MODES:
        raise LayoutError("project.yaml 的 layout.mode 只能是 merge 或 replace（merge＝疊加、replace＝覆寫），目前是「%s」"
                          % (spec.get("mode"),))
    path = Path(name).expanduser()
    if not path.is_absolute():
        path = Path(project.root) / path
    return path, mode, name


def load_project_pages(project, info=None):
    """專案的版型：內建的 + project.yaml layout 指定的外部檔。info 有的話會被填入
    {"file": 檔案路徑字串或 "", "mode": "merge"／"replace" 或 "", "templates": 專案檔提供的模板名稱}。"""
    if info is not None:
        info.clear()
        info.update({"file": "", "mode": "", "templates": []})
    path, mode, name = layout_spec(project)
    base = load_pages()
    if path is None:
        return base
    if not path.is_file():
        raise LayoutError("找不到版型檔：%s（project.yaml 的 layout.file；這個檔案要放在專案資料夾裡）" % path)
    overlay = load_pages(path)
    if info is not None:
        info.update({"file": str(path), "mode": mode, "templates": sorted(overlay["templates"])})
    return merge_pages(base, overlay, "%s（project.yaml 的 layout.file）" % path, mode)


def check_box(box, label="位置"):
    """比例框：四個 0–1 的數字，而且 x0 < x1、y0 < y1。"""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise LayoutError("%s 要是 [x0, y0, x1, y1] 四個數字：%r" % (label, box))
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        raise LayoutError("%s 只能是數字：%r" % (label, box))
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise LayoutError("%s 要在 0–1 之間，而且左上小於右下：%r" % (label, box))
    return x0, y0, x1, y1


SHAPE_TOLERANCE = 0.002     # 外接矩形與 panels 的框允許的誤差（比例）


def _signed_area(points):
    return sum(points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1]
               for i in range(len(points))) / 2.0


def check_shape(shape, box, label="shape"):
    """斜格的多邊形：3–8 個 [x, y]（0–1 的比例）、凸多邊形、有面積，外接矩形要等於 panels 的框。
    回傳 [(x, y), …]（照原本的頂點順序）。"""
    if not isinstance(shape, (list, tuple)) or not 3 <= len(shape) <= 8:
        raise LayoutError("%s 要是 3–8 個頂點 [[x, y], …]：%r" % (label, shape))
    points = []
    for i, point in enumerate(shape, 1):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise LayoutError("%s 第 %d 個頂點要是 [x, y]：%r" % (label, i, point))
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            raise LayoutError("%s 第 %d 個頂點只能是數字：%r" % (label, i, point))
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise LayoutError("%s 第 %d 個頂點要在 0–1 之間：%r" % (label, i, point))
        points.append((x, y))
    if abs(_signed_area(points)) < 1e-4:
        raise LayoutError("%s 的面積是 0（頂點重疊或排成一直線）：%r" % (label, shape))
    signs = set()
    n = len(points)
    for i in range(n):
        ax, ay = points[i]
        bx, by = points[(i + 1) % n]
        cx, cy = points[(i + 2) % n]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if abs(cross) > 1e-9:
            signs.add(cross > 0)
    if len(signs) > 1:
        raise LayoutError("%s 要是凸多邊形，頂點照順時針或逆時針排（目前有凹角或交叉）：%r" % (label, shape))
    x0, y0, x1, y1 = check_box(box, label)
    bound = (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))
    if any(abs(a - b) > SHAPE_TOLERANCE for a, b in zip(bound, (x0, y0, x1, y1))):
        raise LayoutError("%s 的外接矩形是 %s，跟 panels 的框 %s 不一致（panels 要寫外接矩形）"
                          % (label, [round(v, 4) for v in bound], [x0, y0, x1, y1]))
    return points


def template_shapes(tpl):
    """模板每一格的多邊形（沒有 shapes 或那一格是 null 時給 None）。"""
    shapes = tpl.get("shapes") or [None] * len(tpl["panels"])
    return [None if s is None else [tuple(float(v) for v in p) for p in s] for s in shapes]


def local_shape(box, shape):
    """多邊形換成「這一格外接矩形」裡的比例（生成的圖就是這個矩形）。"""
    x0, y0, x1, y1 = (float(v) for v in box)
    return [((x - x0) / (x1 - x0), (y - y0) / (y1 - y0)) for x, y in shape]


def point_in_polygon(x, y, points):
    """凸或凹都可以（射線法）；邊上算在裡面。"""
    inside = False
    n = len(points)
    for i in range(n):
        ax, ay = points[i]
        bx, by = points[(i + 1) % n]
        if (ax - x) * (by - y) - (bx - x) * (ay - y) == 0 and min(ax, bx) <= x <= max(ax, bx) and min(ay, by) <= y <= max(ay, by):
            return True
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            inside = not inside
    return inside


def cut_corners(box, shape, probe=0.06):
    """斜格被切掉的角（給提示詞與檢查用）：回傳 ["左上", …]。直角格回傳 []。"""
    if shape is None:
        return []
    local = local_shape(box, shape)
    corners = (("左上", probe, probe), ("右上", 1 - probe, probe), ("左下", probe, 1 - probe), ("右下", 1 - probe, 1 - probe))
    return [name for name, x, y in corners if not point_in_polygon(x, y, local)]


def inset_polygon(points, distances):
    """把凸多邊形的每一條邊往內推 distances[i]（像素；第 i 條邊是 points[i]→points[i+1]），回傳新頂點。"""
    n = len(points)
    ccw = _signed_area(points) > 0             # 影像座標（y 往下）：面積為正＝順時針看起來的那個方向
    lines = []
    for i in range(n):
        (ax, ay), (bx, by) = points[i], points[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy) or 1.0
        nx, ny = (-dy / length, dx / length) if ccw else (dy / length, -dx / length)   # 指向多邊形內側的法向量
        d = distances[i]
        lines.append(((ax + nx * d, ay + ny * d), (dx, dy)))
    out = []
    for i in range(n):
        (p1, d1), (p2, d2) = lines[i - 1], lines[i]
        det = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(det) < 1e-9:                      # 平行（不會發生在凸多邊形相鄰邊上）：取平移後的點
            out.append(p2)
            continue
        t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / det
        out.append((p1[0] + d1[0] * t, p1[1] + d1[1] * t))
    return out


def panel_poly_px(page, box, shape):
    """斜格在整頁上的像素多邊形（已扣掉一半的 gutter：貼著頁面內框的邊不扣，和 panel_rect_px 同規則），
    與它的外接矩形 (left, top, right, bottom)（四捨五入成整數，組版裁圖用）。"""
    points = check_shape(shape, box)
    left, top, right, bottom = inner_rect(page)
    half = page.get("gutter_px", 0) / 2
    w, h = right - left, bottom - top
    eps = 1e-6
    distances = []
    for i in range(len(points)):
        (ax, ay), (bx, by) = points[i], points[(i + 1) % len(points)]
        on_border = ((ax < eps and bx < eps) or (ax > 1 - eps and bx > 1 - eps)
                     or (ay < eps and by < eps) or (ay > 1 - eps and by > 1 - eps))
        distances.append(0.0 if on_border else half)
    px = [(left + x * w, top + y * h) for x, y in points]
    poly = inset_polygon(px, distances)
    rect = (round(min(p[0] for p in poly)), round(min(p[1] for p in poly)),
            round(max(p[0] for p in poly)), round(max(p[1] for p in poly)))
    return poly, rect


def page_size(page):
    """回傳組版用的整頁像素 (寬, 高)。"""
    width = int(page["width_px"])
    return width, int(round(width * page["height_mm"] / page["width_mm"]))


def inner_rect(page):
    """頁面內框的像素範圍 (left, top, right, bottom)，margin 是 [左, 上, 右, 下] 的比例。"""
    width, height = page_size(page)
    left, top, right, bottom = page.get("margin", [0, 0, 0, 0])
    return (width * left, height * top, width * (1 - right), height * (1 - bottom))


def panel_rect_px(page, box):
    """格子在整頁上的像素框，已扣掉一半的 gutter（相鄰兩格加起來剛好一個 gutter）。"""
    x0, y0, x1, y1 = check_box(box)
    left, top, right, bottom = inner_rect(page)
    half = page.get("gutter_px", 0) / 2
    w, h = right - left, bottom - top
    px0 = left + x0 * w + (half if x0 > 0 else 0)
    py0 = top + y0 * h + (half if y0 > 0 else 0)
    px1 = left + x1 * w - (half if x1 < 1 else 0)
    py1 = top + y1 * h - (half if y1 < 1 else 0)
    return (round(px0), round(py0), round(px1), round(py1))


def _round16(value):
    return max(16, int(round(value / 16.0)) * 16)


def gen_size(aspect, megapixels=1.0, min_aspect=1 / 3, max_aspect=3.0):
    """依格子的寬高比算出生圖尺寸：總像素約 megapixels、寬高都是 16 的倍數。
    太細長的格子先夾到 [min_aspect, max_aspect]，組版時再裁切成格子的形狀。"""
    if aspect <= 0:
        raise LayoutError("寬高比要大於 0：%r" % aspect)
    aspect = min(max(aspect, min_aspect), max_aspect)
    pixels = float(megapixels) * 1_000_000
    width = math.sqrt(pixels * aspect)
    return _round16(width), _round16(width / aspect)


def to_moss_bbox(box, width, height):
    """比例框 [x0, y0, x1, y1] → MOSS 的 [y_min, x_min, y_max, x_max]（像素、原點在中心）。"""
    x0, y0, x1, y1 = check_box(box)
    half_w, half_h = width / 2.0, height / 2.0
    return [
        int(round(y0 * height - half_h)),
        int(round(x0 * width - half_w)),
        int(round(y1 * height - half_h)),
        int(round(x1 * width - half_w)),
    ]


def from_moss_bbox(bbox, width, height):
    """to_moss_bbox 的反向（除錯、預覽框線用）。"""
    y0, x0, y1, x1 = bbox
    return [(x0 + width / 2.0) / width, (y0 + height / 2.0) / height,
            (x1 + width / 2.0) / width, (y1 + height / 2.0) / height]
