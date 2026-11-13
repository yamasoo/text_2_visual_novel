"""前端靜態檔：index.html 引用的每個檔案都要存在並可由伺服器取得；每個頁面檔都要註冊到 FE.PAGES。"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from t2vn.llm import ReplayLLM
from t2vn.server import create_app
from tests.studio.test_studio import imported

UI = Path(__file__).resolve().parents[2] / "ui"


def test_index_scripts_are_served(tmp_path):
    config, _ = imported(tmp_path)
    http = TestClient(create_app(config, llm_factory=lambda: ReplayLLM([])), base_url="http://127.0.0.1:7866")
    html = http.get("/").text
    refs = re.findall(r'(?:src|href)="(/(?:ui|vendor/fekit|fekit)/[^"]+)"', html)
    assert "/ui/shell.js" in refs and "/ui/app.js" in refs and "/ui/shared.js" in refs
    assert "/fekit/fe/core.js" in refs   # fekit 核心檔（路由／API／面板都在裡面）
    for ref in refs:
        assert http.get(ref).status_code == 200, ref
    # 載入順序：fekit 函式庫（含 filepicker）→ shared → 頁面 → shell.js（它會啟動路由）
    scripts = re.findall(r'<script src="([^"]+)"', html)
    assert scripts[0].endswith("/fekit/fe/core.js") and scripts[-1] == "/ui/shell.js", scripts[0]
    assert "/ui/compat.js" not in scripts, scripts
    assert scripts.index("/ui/shared.js") < scripts.index("/ui/app.js")


def test_every_page_file_registers_itself():
    for path in sorted(UI.glob("page_*.js")):
        name = path.stem.removeprefix("page_")
        text = path.read_text(encoding="utf-8")
        # 只認 fekit 的註冊表：FE.PAGES["book/:id/<name>"]，或 FE.PAGES.<name>（settings）。
        assert re.search(rf'FE\.PAGES(?:\.{name}|\[[^\]]*{name}[^\]]*\])\s*=', text), path.name


def test_voice_page_hides_moss_backend():
    text = (UI / "page_voice.js").read_text(encoding="utf-8")
    assert "MOSS" not in text and "voice_design_prompt" not in text


def test_comic_page_composes_before_pdf():
    # F6：出圖之後要有組版入口，PDF 才拿得到東西（不然 /file?kind=pdf 回 400「還沒有頁面圖」）。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    assert "${chapter}/pages`" in text
    assert "組版（產生頁面）" in text
    assert "pages_out" in text
    assert "candidate.file" in text            # /panels 的候選是 {file, seed, ...}，不是字串


def test_comic_page_offers_cbz_and_settings():
    # N1：CBZ 後端早就有，介面也要有入口；N2／N3：漫畫設定與組版 warnings 全列。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    assert '["pdf", "cbz"]' in text
    assert "/settings" in text and "saveSettings" in text and "lettering" in text
    assert "組版提醒" in text and "lastWarn" in text


def test_comic_page_offers_per_panel_render():
    # 第四段：只補某一格／某一頁與候選張數（t2c 的 chapter_jobs 支援，宿主端點原本沒接）。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    assert "補這一格" in text and "補這一頁" in text
    assert "候選張數" in text and "comic_render_candidates" in text
    assert "panel: row.id" in text and "page: row.page" in text


def test_comic_page_offers_letters_tab():
    # 第三段（N5）：第四個分頁「嵌字」，讀 letters 與 plan，存檔後重組版。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    assert '"嵌字"' in text and '"letters"' in text
    assert "/letters" in text and "letters/plan" in text
    assert "saveLetters" in text and "存檔並重組版" in text


def test_comic_settings_card_lists_publishable_fonts():
    # 收尾段（計畫 §5）：可隨成品發佈的字型要寫在設定卡上。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    assert "可隨成品發佈" in text
    assert "Noto Sans TC" in text and "Noto Serif TC" in text
    assert "OFL" in text


def test_comic_settings_card_warns_system_fonts():
    # 微軟正黑體與細明體是 Windows 內建，不能隨成品發佈；字型檔本身也不進 git。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    assert "不可發佈" in text
    assert "msjh" in text and "mingliu" in text
    assert "不進 git" in text
    assert "lettersState" in text


def test_comic_page_letters_drag_and_limits():
    # 拖曳換算成格內 0–1；字級與行數的上下限要與規格一致；faces／sfx 原樣送回。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    assert "pointerdown" in text and "pointermove" in text and "pointerup" in text
    assert "rect[0]" in text
    assert 'min: "0.5", max: "2"' in text
    assert 'min: "1", max: "40"' in text
    assert "panels: lettersState.panels" in text
    assert "faces" in text and "sfx" in text


def test_folder_picker_goes_through_fekit():
    # 檔案／資料夾選擇改走 fekit 的選擇器，shared.js 不再自己打 /api/fs/list。
    text = (UI / "shared.js").read_text(encoding="utf-8")
    assert "FE.filepicker.open(" in text
    assert "api/fs/list" not in text


def test_comic_page_shows_bible_status():
    # 資源共用 D27：GET …/bible 每列有 errors／warnings／placeholder，漫畫頁要逐卡看得見。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    assert "/bible`" in text
    assert "bibleCard" in text and "bibleSummary" in text
    assert "佔位卡" in text and "placeholder" in text


def test_comic_page_edits_t2c_in_place():
    # 漫畫線 D26：角色列就地改 t2c（sections 五段＋outfits.default），存檔走 PUT …/bible/<id>/t2c。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    assert "/bible/${row.id}/t2c" in text
    assert 'method: "PUT"' in text
    assert "編輯 t2c 人工值" in text and "粗拆初稿" in text
    assert "COMIC_T2C_SECTIONS" in text and "COMIC_T2C_OUTFIT_PARTS" in text
    assert "佔位卡沒有外型卡" in text                   # 佔位卡沒有正本，不掛編輯器


def test_assets_page_shows_look_tab_first():
    # VV-1：素材頁改成「外觀 → 素材清單 → 畫風」，預設停在外觀（先定外觀，再挑圖）。
    text = (UI / "page_assets.js").read_text(encoding="utf-8")
    look = text.index('["look", "外觀"]')
    listing = text.index('["list", "素材清單"]')
    styling = text.index('["style", "畫風（這本書）"]')
    assert look < listing < styling
    assert 'FE.store.get(TAB_KEY, "look") || "look"' in text


def test_comic_tabs_follow_the_workflow_order():
    # VV-2：分頁照做事的順序排；章節與分鏡、出圖與頁面各自獨立一目。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    order = [
        '["project", "專案"]', '["chapter", "章節"]', '["storyboard", "分鏡"]',
        '["render", "出圖"]', '["letters", "嵌字"]', '["pages", "頁面與輸出"]',
    ]
    positions = [text.index(needle) for needle in order]
    assert positions == sorted(positions)
    assert '"章節分鏡"' not in text and '"出圖與頁面"' not in text


def test_comic_storyboard_tab_renders_panels():
    # VV-3：分鏡分頁不只是一坨 JSON——逐頁逐格的卡片，讀 storyboard 與 panels 兩支端點。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    assert "async function storyboardPane" in text
    assert "/storyboard" in text and "/panels" in text
    assert "看這一格 JSON" in text and "看整章 JSON" in text
    assert "boardPanel" in text and "SHOT_LABEL" in text and "chip-info" in text


def test_comic_project_is_remembered_per_book_and_stale_id_recovers():
    # 實測踩到：A 書記住的專案 id 會被 B 書沿用（junior 顯示找不到 ch001），而且整頁被錯誤蓋掉。
    text = (UI / "page_comic.js").read_text(encoding="utf-8")
    assert "${TAB_KEY}_project_${bookId}" in text          # 每本書一份，不是一個全域 key
    assert "forgetProject" in text and "known.includes(projectId)" in text
    assert "找不到漫畫專案「${projectId}」" in text        # 讀不到時留著建立入口，不是整頁錯誤


def test_assets_page_picks_profile_and_settings_page_owns_comfyui():
    # QQ-4：模型與 profile 的正本搬到設定頁；素材頁只挑「這本書用哪一組」＋畫風。
    assets = (UI / "page_assets.js").read_text(encoding="utf-8")
    assert 'id: "art_comfy_profile"' in assets and "payload.profile = profileSelect.value" in assets
    assert 'field("unet"' not in assets and 'field("clip"' not in assets and 'field("vae"' not in assets
    settings = (UI / "page_settings.js").read_text(encoding="utf-8")
    assert "ComfyUI（出圖）" in settings and "comfyui/profiles" not in settings
    assert "/api/settings/comfyui/test" in settings and "/api/settings/comfyui/models" in settings
    assert "form.comfyui.profiles" in settings and "default_profile" in settings


def test_settings_page_edits_comfy_presets():
    # QQ-4 補：t2c 設定頁有的 preset 編輯（預設 preset 下拉＋steps／cfg／sampler／scheduler／lora 表格）要一起搬過來。
    text = (UI / "page_settings.js").read_text(encoding="utf-8")
    assert "預設 preset" in text and "新增預設列" in text and "刪除列" in text
    assert "presetTable" in text and "lora_strength" in text and "scheduler" in text
    assert "spec.presets" in text and "default_preset" in text


def test_assets_style_shows_effective_profile():
    # 使用者反映「看不出這個 profile 有沒有作用」：畫面上要寫出實際生效的模型與 preset。
    text = (UI / "page_assets.js").read_text(encoding="utf-8")
    assert "目前實際生效" in text and "s.resolved" in text
    assert "跟隨設定頁的預設 profile" in text and "inherited" in text   # D34：沒選 profile 時跟隨設定頁的預設