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


def test_folder_picker_goes_through_fekit():
    # 檔案／資料夾選擇改走 fekit 的選擇器，shared.js 不再自己打 /api/fs/list。
    text = (UI / "shared.js").read_text(encoding="utf-8")
    assert "FE.filepicker.open(" in text
    assert "api/fs/list" not in text
