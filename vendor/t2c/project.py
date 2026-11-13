"""漫畫專案資料夾的讀取：project.yaml、視覺聖經（角色、地點）、分鏡 JSON。

漫畫專案資料夾（例：examples/sample_comic/）：
    project.yaml               專案設定：畫風、閱讀方向、生圖參數
    bible/characters/*.md      角色：YAML frontmatter（id、look_en、outfits…）＋人看的說明
    bible/locations/*.md       地點：同上（id、look_en）
    script/chNNN.md            漫畫劇本（人看、人改）
    storyboard/chNNN.json      分鏡（機器讀；格式見 docs/02_資料格式.md）
    pages.json                 選填：這一部作品自己的版型（project.yaml 的 layout 指到它）
    build/、renders/、pages/   產出（不進版控）

版型（N4）：預設用套件內建的 t2c/data/pages.json；project.yaml 寫 layout: {file, mode} 就能
疊加（mode: merge，預設）或整份取代（mode: replace）模板。出圖 profile（N4）用 comfy_profile 指定
config.json 的 comfy.profiles 其中一組。
"""
import json
import re
from pathlib import Path

import yaml

from . import layout, look

FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
ID_RE = re.compile(r"^[a-z0-9_-]+$")


class ProjectError(Exception):
    """給人看的錯誤訊息。"""


def read_front_matter(path):
    """回傳 (frontmatter dict, 本文)。沒有 frontmatter 就丟錯。"""
    text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")
    match = FRONT.match(text)
    if not match:
        raise ProjectError("%s 開頭沒有 YAML frontmatter（--- 包起來的區塊）" % path)
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as error:
        raise ProjectError("%s 的 frontmatter 不是合法的 YAML：%s" % (path, error))
    if not isinstance(meta, dict):
        raise ProjectError("%s 的 frontmatter 要是 key: value 的形式" % path)
    return meta, text[match.end():]


def _load_cards(folder, kind):
    cards = {}
    for path in sorted(Path(folder).glob("*.md")):
        meta, _ = read_front_matter(path)
        cid = str(meta.get("id") or "")
        if not ID_RE.match(cid):
            raise ProjectError("%s：id 只能用 a-z、0-9、底線、連字號（目前是 %r）" % (path, cid))
        if cid in cards:
            raise ProjectError("%s：%s id「%s」重複了" % (path, kind, cid))
        if not look.look_en(meta):                # 新的卡片用 sections，舊的用 look_en（D4 的相容層）
            raise ProjectError("%s：缺少 look_en 或 sections（固定的英文外觀描述，生圖時逐字使用）" % path)
        meta["_path"] = str(path)
        cards[cid] = meta
    return cards


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        cfg = self.root / "project.yaml"
        if not cfg.is_file():
            raise ProjectError("%s 不是漫畫專案資料夾（找不到 project.yaml）" % self.root)
        self.config = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        if not isinstance(self.config, dict):
            raise ProjectError("%s 要是 key: value 的形式" % cfg)
        self.characters = _load_cards(self.root / "bible" / "characters", "角色")
        self.locations = _load_cards(self.root / "bible" / "locations", "地點")
        self._pages = None
        self._layout_info = None

    # --- 版型與出圖 profile（N4）---------------------------------------------
    def pages(self):
        """這一部作品用的版型：內建的 + project.yaml 的 layout 指定的外部檔（merge 疊加／replace 覆寫）。
        讀一次就快取；錯誤訊息會寫出是哪個檔案、哪個模板、第幾格。"""
        if self._pages is None:
            info = {}
            try:
                pages = layout.load_project_pages(self, info)
            except layout.LayoutError as error:
                raise ProjectError(str(error))
            self._layout_info, self._pages = info, pages
        return self._pages

    def layout_info(self):
        """專案的版型檔設定：{"file": 路徑或 "", "mode": "merge"／"replace" 或 "", "templates": [專案檔的模板]}。"""
        if self._layout_info is None:
            self.pages()
        return self._layout_info

    def comfy_profile(self):
        """project.yaml 的 comfy_profile：用 config.json 的 comfy.profiles 哪一組出圖；沒寫回傳 None。"""
        value = self.config.get("comfy_profile")
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ProjectError("project.yaml 的 comfy_profile 要是 config.json 裡 comfy.profiles 的名稱（字串）：%r" % (value,))
        return value.strip()

    def storyboard_path(self, chapter):
        return self.root / "storyboard" / ("ch%03d.json" % int(chapter))

    def load_storyboard(self, chapter):
        path = self.storyboard_path(chapter)
        if not path.is_file():
            raise ProjectError("找不到分鏡：%s" % path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ProjectError("%s 不是合法的 JSON：%s" % (path, error))

    # --- S5：文字端的檔案 ---------------------------------------------------
    def source_path(self, chapter):
        """原著章節：source/chNNN.txt 或 .md；沒有就回傳 None。"""
        for ext in (".txt", ".md"):
            path = self.root / "source" / ("ch%03d%s" % (int(chapter), ext))
            if path.is_file():
                return path
        return None

    def load_source(self, chapter):
        path = self.source_path(chapter)
        if path is None:
            raise ProjectError("找不到原著：請把第 %d 章放在 %s（.txt 或 .md）" % (int(chapter), self.root / "source" / ("ch%03d.txt" % int(chapter))))
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n").lstrip("﻿")
        match = FRONT.match(text)          # 允許原著檔開頭有 frontmatter（例如從 Novel Studio 匯出的），略過
        return (text[match.end():] if match else text).strip()

    def plan_path(self, chapter):
        return self.root / "plan" / ("ch%03d.json" % int(chapter))

    def scenes_path(self, chapter):
        """切對話（N3）：場 → 旁白／對話／演出，每一句帶 source_para 回指原著段落。"""
        return self.root / "scenes" / ("ch%03d.json" % int(chapter))

    def load_scenes(self, chapter):
        path = self.scenes_path(chapter)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ProjectError("%s 不是合法的 JSON：%s" % (path, error))

    def script_path(self, chapter):
        return self.root / "script" / ("ch%03d.md" % int(chapter))

    def load_plan(self, chapter):
        path = self.plan_path(chapter)
        if not path.is_file():
            raise ProjectError("找不到改編規劃：%s（先執行 plan 階段）" % path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ProjectError("%s 不是合法的 JSON：%s" % (path, error))

    def out_dir(self, *parts):
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path
