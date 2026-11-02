"""前端靜態檔：index.html 引用的每個檔案都要存在並可由伺服器取得；每個頁面檔都要註冊到 PAGES。"""
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
    refs = re.findall(r'(?:src|href)="(/ui/[^"]+)"', html)
    assert "/ui/shell.js" in refs and "/ui/app.js" in refs
    for ref in refs:
        assert http.get(ref).status_code == 200, ref
    # shell.js 必須最後載入（它會啟動路由）
    scripts = re.findall(r'<script src="([^"]+)"', html)
    assert scripts[0] == "/ui/app.js" and scripts[-1] == "/ui/shell.js"


def test_every_page_file_registers_itself():
    for path in sorted(UI.glob("page_*.js")):
        name = path.stem.removeprefix("page_")
        assert re.search(rf"PAGES\.{name}\s*=", path.read_text(encoding="utf-8")), path.name
