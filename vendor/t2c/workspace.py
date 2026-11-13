"""介面與命令列共用的專案操作（S7）：總覽、新建專案、讀寫原著／規劃／劇本／分鏡、每格的出圖狀態、加畫用的新 seed。

只用標準函式庫、PyYAML、Pillow（CLAUDE.md 第 3 節）；錯誤丟 WorkspaceError（給人看的繁體中文）。
人改的檔案只擋「讀不懂、編譯不了」的錯，其他檢查回傳成 issues 讓人決定（docs/05_介面設計.md 原則 5）。
"""
import json
import re
import shutil
from pathlib import Path

from . import bible, cast, check, compose, layout, letter, script as sc, styles
from .compile import CompileError, _seed, compile_chapter
from .project import FRONT, Project, ProjectError

CH_RE = re.compile(r"^ch(\d{3})\.(?:json|md|txt)$")
MAX_CHAPTER = 999

DEFAULT_PRESET = styles.DEFAULT_PRESET        # 新建專案的畫風預設集（mono＝日式黑白頁漫）
PROJECT_TEMPLATE = """# 漫畫專案設定。格式說明見 text2comics 的 docs/02_資料格式.md。
title: {title}
language: zh-TW
reading: rtl                 # 日式右翻：格子由右到左、由上到下
{style_block}
megapixels: 1.0              # 每格生圖約 1 MP
negative: nsfw, worst quality, bad anatomy, watermark, jpeg artifact, text, speech bubble, letters
sfx_mode: overlay            # overlay＝狀聲詞由嵌字疊上；moss＝交給 MOSS 畫
candidates: 3                # 每格產生幾張候選
seed_base: 0
"""
# 範例專案複製時一定帶的；sample_renders=True 時再加上出圖與人的選擇
SAMPLE_BASE = ("project.yaml", "bible", "source", "script", "storyboard")
SAMPLE_RENDERS = ("renders/ch001", "picks", "letters")


class WorkspaceError(Exception):
    """給人看的錯誤訊息。"""


def issue_dicts(issues):
    """script.Issue 清單 → [{"level", "where", "message"}]（介面顯示用）。"""
    return [{"level": i.level, "where": i.where, "message": i.message} for i in issues]


def _chapter(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise WorkspaceError("章節編號要是整數，目前是 %r" % (n,))
    if not 1 <= n <= MAX_CHAPTER:
        raise WorkspaceError("章節編號要在 1–%d 之間，目前是 %d" % (MAX_CHAPTER, n))
    return n


def open_project(path):
    if not str(path or "").strip():
        raise WorkspaceError("請先選擇漫畫專案資料夾")
    try:
        return Project(path)
    except ProjectError as error:
        raise WorkspaceError(str(error))


# ---------------------------------------------------------------------------
# 新建專案
# ---------------------------------------------------------------------------
def _yaml_text(text):
    return json.dumps(str(text), ensure_ascii=False)       # JSON 字串同時是合法的 YAML


def _style_block(preset, style=""):
    """project.yaml 的畫風那一段。預設只用 style_preset；明寫 style 才多一行。"""
    lines = ["style_preset: %s          # 畫風預設集：mono（日式黑白頁漫）／color（彩色）／american（美式）" % preset]
    if style:
        lines += ["style: >-", "  " + style]
    else:
        lines.append('# style: "..."            # 選填：明寫就蓋掉預設集的 style')
    lines.append("# meta: [manga panel]   # 選填：明寫就蓋掉預設集的 meta（[] 就是不帶）")
    return "\n".join(lines)


def sample_has_renders(sample):
    """範例專案有沒有出圖（renders/ 不進版控，新 clone 的範例沒有；M1 修正 L-7）。"""
    folder = Path(sample) / "renders" / "ch001" if sample else None
    return bool(folder and folder.is_dir() and any(folder.glob("*.png")))


def create_project(path, title="", style="", preset=DEFAULT_PRESET, sample=None, sample_renders=False):
    """建立漫畫專案資料夾。資料夾要不存在或是空的。sample：要複製的範例專案資料夾（由呼叫端給，
    獨立工具是 examples/sample_comic；None＝建空專案）；sample_renders：連同範例的出圖、選圖、嵌字微調
    （沒有 GPU 也能把一章走完）。preset：畫風預設集（mono／color／american，見 t2c/data/styles.json）。
    回傳 Project。"""
    root = Path(str(path or "").strip()).expanduser()
    if not str(path or "").strip():
        raise WorkspaceError("請輸入新專案的資料夾路徑")
    if not root.is_absolute():
        raise WorkspaceError("請輸入完整路徑（例如 E:\\comics\\作品名），目前是「%s」" % path)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise WorkspaceError("%s 已經存在而且不是空的資料夾；請換一個新資料夾" % root)
    preset = str(preset or DEFAULT_PRESET).strip()
    try:
        styles.preset(preset)
    except styles.StylesError as error:
        raise WorkspaceError(str(error))
    if sample:
        sample = Path(sample)
        if not (sample / "project.yaml").is_file():
            raise WorkspaceError("範例專案 %s 不完整（沒有 project.yaml）" % sample)
        if sample_renders and not sample_has_renders(sample):
            raise WorkspaceError("範例沒有出圖（renders/ 不進版控，新下載的工具沒有）：請不要勾「連同範例的出圖」，"
                                 "或先用 tools/panels.py 幫範例出圖")
        root.mkdir(parents=True, exist_ok=True)
        for part in SAMPLE_BASE + (SAMPLE_RENDERS if sample_renders else ()):
            src = sample / part
            if src.is_dir():
                shutil.copytree(src, root / part)
            elif src.is_file():
                shutil.copy2(src, root / part)
        if title.strip():
            cfg = root / "project.yaml"
            text = re.sub(r"^title:.*$", "title: %s" % _yaml_text(title.strip()), cfg.read_text(encoding="utf-8"), count=1, flags=re.M)
            cfg.write_text(text, encoding="utf-8")
    else:
        if not title.strip():
            raise WorkspaceError("請輸入作品名")
        root.mkdir(parents=True, exist_ok=True)
        for sub in ("bible/characters", "bible/locations", "source"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        style = " ".join(str(style or "").split())
        (root / "project.yaml").write_text(
            PROJECT_TEMPLATE.format(title=_yaml_text(title.strip()), style_block=_style_block(preset, style)),
            encoding="utf-8")
    return open_project(root)


# ---------------------------------------------------------------------------
# 總覽
# ---------------------------------------------------------------------------
def _numbers(folder):
    out = set()
    if folder.is_dir():
        for path in folder.iterdir():
            m = CH_RE.match(path.name)
            if m:
                out.add(int(m.group(1)))
    return out


def chapter_numbers(project):
    root = project.root
    return sorted(_numbers(root / "source") | _numbers(root / "plan") | _numbers(root / "script") | _numbers(root / "storyboard"))


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def panel_ids(storyboard):
    return [(int(pg["page"]), p.get("id") or "p%03d_k%d" % (int(pg["page"]), p["slot"]))
            for pg in storyboard.get("pages") or [] for p in sorted(pg.get("panels") or [], key=lambda x: x.get("slot", 0))]


def chapter_status(project, n):
    """一章各階段的狀況（檔案有沒有、幾頁幾格、出圖與選圖進度）。讀不懂的檔案記在 problems，不丟錯。"""
    n = _chapter(n)
    row = {"chapter": n, "title": "", "source_chars": None, "plan_pages": None, "script_pages": None,
           "storyboard_pages": None, "panels": 0, "rendered": 0, "picked": 0, "pages_out": 0, "letters": False, "problems": []}
    src = project.source_path(n)
    if src:
        try:
            row["source_chars"] = len(project.load_source(n))
        except (OSError, ProjectError) as error:
            row["problems"].append(str(error))
    plan = _read_json(project.plan_path(n)) if project.plan_path(n).is_file() else None
    if project.plan_path(n).is_file() and not isinstance(plan, dict):
        row["problems"].append("plan/ch%03d.json 不是合法的 JSON" % n)
    if isinstance(plan, dict):
        row["plan_pages"] = len(plan.get("pages") or [])
        row["title"] = str(plan.get("title") or "")
    if project.script_path(n).is_file():
        meta, pages, _ = sc.parse_script(project.script_path(n).read_text(encoding="utf-8"))
        row["script_pages"] = len(pages)
        row["title"] = row["title"] or str(meta.get("title") or "")
    if project.storyboard_path(n).is_file():
        board = _read_json(project.storyboard_path(n))
        if not isinstance(board, dict):
            row["problems"].append("storyboard/ch%03d.json 不是合法的 JSON" % n)
        else:
            row["storyboard_pages"] = len(board.get("pages") or [])
            row["title"] = str(board.get("title") or "") or row["title"]
            ids = panel_ids(board)
            row["panels"] = len(ids)
            row["rendered"] = sum(1 for _, pid in ids if compose.candidates(project, n, pid))
            try:
                picks = compose.load_picks(project, n)
                row["picked"] = sum(1 for _, pid in ids if pid in picks)
            except compose.ComposeError as error:
                row["problems"].append(str(error))
    out = compose.pages_dir(project, n)
    row["pages_out"] = len(list(out.glob("p*.png"))) if out.is_dir() else 0
    row["letters"] = letter.letters_path(project, n).is_file()
    return row


def set_style_preset(project, name):
    """換一部作品的畫風預設集：改 project.yaml 的 style_preset，並拿掉明寫的 style／meta／look／sfx。

    明寫的欄位會蓋掉預設集（t2c/styles.py 的逐欄覆蓋），所以換預設集時一定要一起清掉，
    不然使用者會覺得「選了彩色卻沒變」。註解行（# style: …）不動。
    回傳 {"path", "preset", "dropped"}；名稱不認得丟 WorkspaceError（列出可用的）。
    """
    root = Path(getattr(project, "root", project)).resolve()
    path = root / "project.yaml"
    if not path.is_file():
        raise WorkspaceError("「%s」不是漫畫專案資料夾（找不到 project.yaml）" % root)
    preset = str(name or "").strip() or styles.default_name()
    try:
        styles.preset(preset)
    except styles.StylesError as error:
        raise WorkspaceError(str(error))
    lines = path.read_text(encoding="utf-8").splitlines()
    out, dropped, written = [], [], False
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^style_preset\s*:", line):
            comment = line.split("#", 1)[1].strip() if "#" in line else ""
            out.append("style_preset: %s%s" % (preset, ("          # " + comment) if comment else ""))
            written = True
            i += 1
            continue
        matched = re.match(r"^(style|meta|look|sfx)\s*:", line)
        if matched:
            dropped.append(matched.group(1))
            i += 1
            while i < len(lines) and lines[i].strip() and lines[i][:1] in (" ", "\t"):
                i += 1                              # 區塊純量（>-）或清單的續行一起拿掉
            continue
        out.append(line)
        i += 1
    if not written:
        out.append("style_preset: %s" % preset)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8", newline="\n")
    tmp.replace(path)
    return {"path": str(path), "preset": preset, "dropped": dropped}


def overview(project):
    cfg = project.config
    drafts = {k: sorted(bible.list_drafts(project, k)) for k in ("character", "location")}
    return {
        "root": str(project.root),
        "title": str(cfg.get("title") or project.root.name),
        "config": {k: cfg.get(k) for k in ("title", "style", "style_preset", "candidates", "megapixels", "sfx_mode", "reading")},
        "chapters": [chapter_status(project, n) for n in chapter_numbers(project)],
        "bible": {"characters": [{"id": cid, "name": c.get("name", cid)} for cid, c in project.characters.items()],
                  "locations": [{"id": lid, "name": c.get("name", lid)} for lid, c in project.locations.items()],
                  "drafts": drafts},
    }


# ---------------------------------------------------------------------------
# 原著
# ---------------------------------------------------------------------------
def read_source(project, n):
    n = _chapter(n)
    path = project.source_path(n)
    if path is None:
        return {"path": str(project.root / "source" / ("ch%03d.txt" % n)), "text": "", "exists": False}
    return {"path": str(path), "text": path.read_text(encoding="utf-8").replace("\r\n", "\n").lstrip("\ufeff"), "exists": True}


def save_source(project, n, text):
    """寫 source/chNNN.txt（原本是 .md 就寫回 .md）。"""
    n = _chapter(n)
    text = str(text or "").replace("\r\n", "\n")
    if not text.strip():
        raise WorkspaceError("原著是空的")
    path = project.source_path(n) or project.root / "source" / ("ch%03d.txt" % n)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path


def import_source_file(project, n, file_path):
    """從本機的 .txt／.md 匯入原著（UTF-8；有 BOM 也可以）。"""
    src = Path(str(file_path or "").strip())
    if src.suffix.lower() not in (".txt", ".md"):
        raise WorkspaceError("只能匯入 .txt 或 .md：%s" % src)
    try:
        raw = src.read_bytes()
    except OSError as error:
        raise WorkspaceError("讀不到 %s（%s）" % (src, error))
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise WorkspaceError("%s 不是 UTF-8 編碼；請先另存成 UTF-8" % src.name)
    return save_source(project, n, text)


def source_body(text):
    """去掉開頭 frontmatter 之後的原著（和 Project.load_source 同規則），用來算字數。"""
    text = str(text or "").lstrip("\ufeff")
    m = FRONT.match(text)
    return (text[m.end():] if m else text).strip()


# ---------------------------------------------------------------------------
# 改編規劃
# ---------------------------------------------------------------------------
def read_plan(project, n):
    n = _chapter(n)
    path = project.plan_path(n)
    if not path.is_file():
        return {"path": str(path), "plan": None, "issues": []}
    data = _read_json(path)
    if not isinstance(data, dict):
        return {"path": str(path), "plan": None, "raw": path.read_text(encoding="utf-8"),
                "issues": [{"level": "error", "where": "", "message": "plan/ch%03d.json 不是合法的 JSON" % n}]}
    return {"path": str(path), "plan": data,
            "issues": issue_dicts(check.check_plan(project, data, project.pages(), source=_source_or_none(project, n)))}


def _source_or_none(project, n):
    try:
        return project.load_source(n) or None
    except (OSError, ProjectError):
        return None


def save_plan(project, n, plan):
    """人改過的規劃：check_plan 有錯就擋（劇本階段要靠它）。回傳 (路徑, issues)。"""
    n = _chapter(n)
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except ValueError as error:
            raise WorkspaceError("規劃不是合法的 JSON：%s" % error)
    issues = check.check_plan(project, plan, project.pages(), source=_source_or_none(project, n))
    errs = sc.errors(issues)
    if errs:
        raise WorkspaceError("規劃有 %d 個問題，沒有存檔：\n%s" % (len(errs), "\n".join("  - %s" % e for e in errs)))
    plan = dict(plan, version=1, chapter=n)
    path = project.plan_path(n)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, issue_dicts(issues)


# ---------------------------------------------------------------------------
# 劇本
# ---------------------------------------------------------------------------
def script_issues(project, n, text):
    """劇本的檢查（人改的版本，strict=False）：解析問題＋每頁的格數與台詞。"""
    meta, pages, issues = sc.parse_script(text)
    pages_data = project.pages()
    for page in pages:
        tpl = pages_data["templates"].get(str(page.template))
        if page.template and tpl is None:
            issues.append(sc.Issue("error", "第 %s 頁" % page.page, "沒有這個格子模板「%s」" % page.template))
        issues += sc.check_page(project, page, panel_count=len(tpl["panels"]) if tpl else None, strict=False)
    numbers = [p.page for p in pages]
    if numbers != list(range(1, len(numbers) + 1)):
        issues.append(sc.Issue("error", "", "頁碼要從 1 連號，目前是 %s" % numbers))
    return meta, pages, issues


def read_script(project, n):
    n = _chapter(n)
    path = project.script_path(n)
    fid = project.root / "build" / "adapt" / ("ch%03d_fidelity.md" % n)
    out = {"path": str(path), "text": "", "exists": path.is_file(), "pages": [], "issues": [],
           "fidelity": fid.read_text(encoding="utf-8") if fid.is_file() else ""}
    if path.is_file():
        out["text"] = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        _, pages, issues = script_issues(project, n, out["text"])
        out["pages"] = [{"page": p.page, "template": p.template, "panels": len(p.panels)} for p in pages]
        out["issues"] = issue_dicts(issues)
    return out


def save_script(project, n, text):
    """人改的劇本：照存，回傳檢查結果（分鏡階段才會被擋）。"""
    n = _chapter(n)
    text = str(text or "").replace("\r\n", "\n")
    if not text.strip():
        raise WorkspaceError("劇本是空的")
    _, pages, issues = script_issues(project, n, text)
    if not pages:
        raise WorkspaceError("看不到任何「## 第 N 頁」標題，沒有存檔（格式見 docs/02_資料格式.md 第 4 節）")
    path = project.script_path(n)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path, issue_dicts(issues)


# ---------------------------------------------------------------------------
# 分鏡
# ---------------------------------------------------------------------------
PANEL_ORDER = ("slot", "id", "shot", "angle", "location", "scene", "characters", "objects", "sfx", "bubbles", "seeds")


def normalize_storyboard(doc, n):
    """介面送來的分鏡 → 檔案格式：補 version、chapter、格 id，欄位順序固定、去掉空的選填欄位。"""
    if not isinstance(doc, dict) or not isinstance(doc.get("pages"), list):
        raise WorkspaceError("分鏡要是 {\"pages\": [ … ]}")
    pages = []
    for page in doc["pages"]:
        if not isinstance(page, dict) or not isinstance(page.get("panels"), list):
            raise WorkspaceError("分鏡的每一頁要有 page、template、panels")
        panels = []
        for panel in sorted(page["panels"], key=lambda p: int(p.get("slot") or 0) if isinstance(p, dict) else 0):
            if not isinstance(panel, dict):
                raise WorkspaceError("第 %s 頁：每一格要是物件" % page.get("page"))
            clean = dict(panel)
            if not clean.get("id"):
                clean["id"] = "p%03d_k%d" % (int(page.get("page") or 0), int(clean.get("slot") or 0))
            for key in ("objects", "sfx", "seeds"):
                if key in clean and not clean[key]:
                    del clean[key]
            if not clean.get("location"):
                clean.pop("location", None)
            panels.append({k: clean[k] for k in PANEL_ORDER if k in clean} | {k: v for k, v in clean.items() if k not in PANEL_ORDER})
        pages.append({"page": page.get("page"), "template": page.get("template"), "panels": panels})
    _no_duplicates(pages)
    out = {"version": 1, "chapter": int(n), "title": str(doc.get("title") or "")}
    out["pages"] = pages
    return out


def _no_duplicates(pages):
    """M1 修正 L-2：重複的頁碼或格 id 會讓兩格共用同一個檔名與 seed（同一張圖），編譯卻不會發現。"""
    seen_pages, seen_ids = {}, {}
    for page in pages:
        no = page.get("page")
        if no in seen_pages:
            raise WorkspaceError("第 %s 頁出現兩次，沒有存檔；頁碼不能重複" % no)
        seen_pages[no] = True
        for panel in page["panels"]:
            pid = panel.get("id")
            if pid in seen_ids:
                raise WorkspaceError("格 id「%s」重複（第 %s 頁第 %s 格和第 %s 頁），沒有存檔；重複的格會共用同一張圖。把其中一個的 id 刪掉讓程式重編"
                                     % (pid, no, panel.get("slot"), seen_ids[pid]))
            seen_ids[pid] = no


def storyboard_issues(project, n, board, pages_data=None):
    """編譯以外的檢查（外觀字、每行字數、和劇本是否一致）。全部當提醒回傳。"""
    pages_data = pages_data or project.pages()
    issues = []
    script_pages = {}
    if project.script_path(n).is_file():
        _, sp, _ = sc.parse_script(project.script_path(n).read_text(encoding="utf-8"))
        script_pages = {p.page: p for p in sp}
    for page in board.get("pages") or []:
        issues += check.check_page_panels(project, page.get("page"), page.get("template"), page.get("panels"), pages_data)
        if page.get("page") in script_pages:
            issues += check.check_against_script(project, page["page"], page.get("panels") or [], script_pages[page["page"]])
    return issues


def read_storyboard(project, n):
    n = _chapter(n)
    path = project.storyboard_path(n)
    out = {"path": str(path), "storyboard": None, "issues": [], "compile_error": ""}
    if not path.is_file():
        return out
    try:
        board = project.load_storyboard(n)
    except ProjectError as error:
        out["compile_error"] = str(error)
        return out
    out["storyboard"] = board
    try:
        compile_chapter(project, board)
    except CompileError as error:
        out["compile_error"] = str(error)
    out["issues"] = issue_dicts(storyboard_issues(project, n, board))
    return out


def save_storyboard(project, n, doc):
    """人改的分鏡：先過 compile_chapter（不過就擋，訊息寫出哪一頁哪一格），其他檢查回傳成提醒。回傳 (路徑, 分鏡, issues)。"""
    n = _chapter(n)
    board = normalize_storyboard(doc, n)
    try:
        compile_chapter(project, board)
    except CompileError as error:
        raise WorkspaceError("分鏡無法編譯，沒有存檔：%s" % error)
    issues = storyboard_issues(project, n, board)
    # 探索用（D2／D9）：缺卡不算錯誤，但要講清楚哪幾格、怎麼解。龍套標 extra，名字寫錯就改 id（D11 只給建議）。
    for job in compile_chapter(project, board):
        where = "第 %s 頁第 %s 格" % (job["page"], job["slot"])
        for row in job.get("unresolved") or []:
            hint = ("；你是不是要對應到「%s」？" % "」或「".join(row["suggest"])) if row.get("suggest") else ""
            issues.append(sc.Issue("warn", where,
                                "探索用：角色「%s」還沒有卡片，這一格不進選圖／頁面／過期清單。龍套請標 \"extra\": true；名字寫錯就改 id 或補別名%s" % (row["id"], hint)))
    path = project.storyboard_path(n)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, board, issue_dicts(issues)


# ---------------------------------------------------------------------------
# 角色解析的三個一鍵動作（微5／D9、D10）：只動這一章的分鏡
# ---------------------------------------------------------------------------
def _chapter_panels(board):
    for page in board.get("pages") or []:
        for panel in page.get("panels") or []:
            yield page, panel


def chapters_with_id(project, skip, cid):
    """其他章也用了同一個 id 的章號（D10 的提示；不改它們）。"""
    out = []
    folder = project.root / "storyboard"
    for path in (sorted(folder.glob("ch*.json")) if folder.is_dir() else []):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        no = int(doc.get("chapter") or re.sub(r"\D", "", path.stem) or 0)
        if not no or no == skip:
            continue
        if any(str(entry.get("id") or "") == cid for _page, panel in _chapter_panels(doc)
               for entry in panel.get("characters") or []):
            out.append(no)
    return out


def _character_edit(project, n, mutate):
    """讀這一章的分鏡 → mutate(board) → 走 save_storyboard（同一套驗證與存檔）。"""
    n = _chapter(n)
    try:
        board = project.load_storyboard(n)
    except ProjectError as error:
        raise WorkspaceError(str(error))
    extra = mutate(board) or {}
    path, saved, issues = save_storyboard(project, n, board)
    out = {"path": str(path), "chapter": n, "storyboard": saved, "issues": issues}
    out.update(extra)
    return out


def remap_character(project, n, old, new, remember_alias=False):
    """「對應到既有卡片」：這一章寫成 old 的角色 id 改成 new 那張卡（D10 只動這一章）。
    remember_alias=True 時順便把人按的這個寫法記進卡片的別名（D11）。"""
    old, new = str(old or "").strip(), str(new or "").strip()
    if not old or not new:
        raise WorkspaceError("對應要有「原本的寫法」與「卡片」兩個值")
    cid, how = cast.resolve(project, new)
    if cid is None:
        raise WorkspaceError("「%s」不是任何卡片的 id、姓名或別名；先建卡片，或把這個拼法補進別名" % new)
    if cid == old:
        raise WorkspaceError("「%s」本來就對得上這張卡，不用改" % old)

    def mutate(board):
        changed = []
        for page, panel in _chapter_panels(board):
            hit = False
            for entry in panel.get("characters") or []:
                if str(entry.get("id") or "") != old:
                    continue
                entry["id"] = cid
                entry.pop("extra", None)                  # 有卡了，臨時角色的旗標就沒有意義
                hit = True
            for bubble in panel.get("bubbles") or []:     # 說話的人寫的是卡片 id，一起改（不然編譯不過）
                if str(bubble.get("speaker") or "") == old:
                    bubble["speaker"] = cid
                    hit = True
            if hit:
                changed.append({"page": page.get("page"), "slot": panel.get("slot")})
        if not changed:
            raise WorkspaceError("這一章的分鏡沒有用到「%s」；別章請先切到那一章" % old)
        return {"changed": changed}

    out = _character_edit(project, n, mutate)
    out["card"], out["how"], out["aliases"] = cid, how, []
    if remember_alias:
        path, aliases = bible.add_alias(project, cid, old)
        project.characters[cid]["aliases"] = aliases        # 這一個請求內就生效
        out["aliases"], out["card_path"] = [old], str(path)
    out["other_chapters"] = chapters_with_id(project, n, old)
    return out


def mark_extra(project, n, cid):
    """「標為臨時角色」：這一章把這個 id 標成 extra: true（D9），解除探索用的鎖。龍套不佔聖經的位置。"""
    cid = str(cid or "").strip()
    if not cid:
        raise WorkspaceError("要指定角色 id")
    if cast.resolve(project, cid)[0] is not None:
        raise WorkspaceError("「%s」已經有卡片，不需要標成臨時角色" % cid)

    def mutate(board):
        changed = []
        for page, panel in _chapter_panels(board):
            for entry in panel.get("characters") or []:
                if str(entry.get("id") or "") == cid:
                    entry["extra"] = True
                    changed.append({"page": page.get("page"), "slot": panel.get("slot")})
        if not changed:
            raise WorkspaceError("這一章的分鏡沒有用到「%s」" % cid)
        return {"changed": changed}

    out = _character_edit(project, n, mutate)
    out["other_chapters"] = chapters_with_id(project, n, cid)
    return out


def drop_character(project, n, cid):
    """「從這一格移除」：這一章把這個角色從 characters 拿掉，他說的對話框改成旁白（D9 的第三個動作）。"""
    cid = str(cid or "").strip()
    if not cid:
        raise WorkspaceError("要指定角色 id")

    def mutate(board):
        changed, narration = [], []
        for page, panel in _chapter_panels(board):
            chars = panel.get("characters") or []
            keep = [entry for entry in chars if str(entry.get("id") or "") != cid]
            if len(keep) != len(chars):
                panel["characters"] = keep
                changed.append({"page": page.get("page"), "slot": panel.get("slot")})
            for bubble in panel.get("bubbles") or []:
                if str(bubble.get("speaker") or "") != cid:
                    continue
                bubble["kind"] = "narration"
                bubble.pop("speaker", None)
                bubble.pop("offscreen", None)
                narration.append({"page": page.get("page"), "slot": panel.get("slot")})
        if not changed and not narration:
            raise WorkspaceError("這一章的分鏡沒有用到「%s」" % cid)
        return {"changed": changed, "narration": narration}

    out = _character_edit(project, n, mutate)
    out["other_chapters"] = chapters_with_id(project, n, cid)
    return out


def compile_one(project, n, panel_id, doc=None):
    """編譯一格（doc 是還沒存的分鏡；None 就讀檔）。回傳 compile 的工作（含 prompt、width、height、seeds）。"""
    n = _chapter(n)
    board = normalize_storyboard(doc, n) if doc is not None else project.load_storyboard(n)
    page_no = next((pg for pg, pid in panel_ids(board) if pid == panel_id), None)
    if page_no is None:
        raise WorkspaceError("分鏡裡沒有格「%s」" % panel_id)
    try:
        jobs = compile_chapter(project, board, only_page=page_no)
    except CompileError as error:
        raise WorkspaceError(str(error))
    return next(j for j in jobs if j["id"] == panel_id)


# ---------------------------------------------------------------------------
# 出圖與選圖
# ---------------------------------------------------------------------------
SUFFIX_RE = re.compile(r"_s(\d+)(?:_([A-Za-z0-9-]+))?(?:__([A-Za-z0-9-]+))?\.png$")


def candidate_info(name):
    """候選圖的檔名 → {file, seed, preset, profile}（見 comfy.render_name）。"""
    m = SUFFIX_RE.search(name)
    return {"file": name, "seed": int(m.group(1)) if m else None,
            "preset": (m.group(2) or "") if m else "", "profile": (m.group(3) or "") if m else ""}


def panels_status(project, n):
    """每一格：候選圖、目前的選圖、生圖尺寸、格子尺寸（組版後）。分鏡要能編譯。"""
    n = _chapter(n)
    try:
        board = project.load_storyboard(n)
        jobs = compile_chapter(project, board)
    except (ProjectError, CompileError) as error:
        raise WorkspaceError(str(error))
    try:
        picks = compose.load_picks(project, n)
    except compose.ComposeError as error:
        raise WorkspaceError(str(error))
    by_id = {}
    for page in board.get("pages") or []:
        for panel in page.get("panels") or []:
            by_id[panel.get("id") or "p%03d_k%d" % (page["page"], panel["slot"])] = panel
    rows = []
    for job in jobs:
        rect = job["rect_px"]
        panel = by_id.get(job["id"], {})
        rows.append({
            "id": job["id"], "page": job["page"], "slot": job["slot"],
            "gen_size": [job["width"], job["height"]], "cell_size": [rect[2] - rect[0], rect[3] - rect[1]],
            "seeds": job["seeds"], "scene": panel.get("scene", ""),
            "characters": [c.get("id") for c in panel.get("characters") or []],
            "lines": [b.get("text", "") for b in panel.get("bubbles") or []],
            "candidates": [candidate_info(name) for name in compose.candidates(project, n, job["id"])],
            "pick": picks.get(job["id"]),
        })
    return {"panels": rows, "pages": sorted({r["page"] for r in rows})}


def set_pick(project, n, panel_id, image=None, focus=None, zoom=None, clear=False):
    """改一格的選圖、焦點、放大（立刻寫 picks/）。換圖時焦點、放大回到預設。回傳這一格的新紀錄（clear 時是 None）。"""
    n = _chapter(n)
    try:
        board = project.load_storyboard(n)
        picks = compose.load_picks(project, n)
    except (ProjectError, compose.ComposeError) as error:
        raise WorkspaceError(str(error))
    if panel_id not in {pid for _, pid in panel_ids(board)}:
        raise WorkspaceError("分鏡裡沒有格「%s」" % panel_id)
    if clear:
        picks.pop(panel_id, None)
        compose.save_picks(project, n, picks)
        return None
    entry = dict(picks.get(panel_id) or {})
    if image:
        if image != entry.get("image"):
            entry = {"image": image}
    elif not entry:
        entry = {"image": compose.default_candidate(project, n, panel_id) or ""}
    if focus is not None:
        entry["focus"] = focus
    if zoom is not None:
        entry["zoom"] = zoom
    try:
        entry = compose.check_pick(panel_id, entry)
    except compose.ComposeError as error:
        raise WorkspaceError(str(error))
    if not (compose.renders_dir(project, n) / entry["image"]).is_file():
        raise WorkspaceError("「%s」選的圖不存在：renders/%s/%s" % (panel_id, compose.chapter_name(n), entry["image"]))
    picks[panel_id] = entry
    compose.save_picks(project, n, picks)
    return entry


def next_seeds(job, count, existing, seed_base=0):
    """「加畫」：依序看這一格的 seeds（手動指定的、再來是候選編號 0、1、2… 由格 id 算出的），
    跳過已經畫過的（existing），取 count 個。同一格永遠得到同一串，見 docs/05_介面設計.md 第 3 節。"""
    out, seen = [], set(existing)
    for seed in job.get("seeds") or []:
        if len(out) >= count:
            return out
        if seed not in seen:
            out.append(seed)
            seen.add(seed)
    index = 0
    while len(out) < count and index < 10000:
        seed = _seed(job["id"], index, seed_base)
        if seed not in seen:
            out.append(seed)
            seen.add(seed)
        index += 1
    return out
