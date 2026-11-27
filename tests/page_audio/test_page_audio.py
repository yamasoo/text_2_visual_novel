"""ui/page_audio.js 的靜態檢查（DS 端沒有瀏覽器；畫面由 Claude 用 Playwright 驗收）。"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "ui" / "page_audio.js"


@pytest.fixture(scope="module")
def js():
    assert SRC.is_file(), "ui/page_audio.js 不存在"
    return SRC.read_text(encoding="utf-8")


def test_page_registered(js):
    assert js.lstrip().startswith("/*")
    assert '"use strict"' in js
    # 頁面改由 fekit 註冊：路由字串 + ctx（原本是 PAGES.audio = async function (bookId)。）
    assert re.search(r'FE\.PAGES\["book/:id/audio"\]\s*=\s*async\s*\(\s*ctx\s*\)', js)
    assert "crumbs(" in js and "音樂" in js


def test_no_unsafe_apis(js):
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "fetch("):
        assert bad not in js, bad


def test_no_hardcoded_host(js):
    for bad in ("127.0.0.1", "localhost", "http://", "https://"):
        assert bad not in js, bad


def test_api_routes_used(js):
    assert "/art/audio" in js
    # 任務端點改走 fekit 的 /api/fe-jobs，停止鈕改呼叫 tracker.stop()（原本打 /api/jobs/{id} 與 /api/jobs/{id}/stop）。
    for part in ("/file?path=", "/draft", "/prompt", "/generate", "/select", "/scan", "/art/status", "/api/fe-jobs", "tracker.stop"):
        assert part in js, part
    assert "encodeURIComponent" in js
    assert re.search(r'method:\s*"PUT"', js)


def test_payloads(js):
    for key in ("only", "candidates", "regenerate", "asset_id", "style", "lyrics", "loop", "status", '"final"', "license"):
        assert key in js, key
    # 任務掛載與模型挑選改走 fekit：AiPanel.poll → Panel.attach、AiPanel.executor → Executors.value。
    assert "Panel.attach" in js and "Executors.value" in js


def test_ui_labels(js):
    for label in ("AI 擬稿", "存提示詞", "選這首", "再出 1 首", "掃描手動放入的音檔", "定稿", "停止", "BGM", "環境音", "出曲"):
        assert label in js, label
    assert re.search(r"<audio|h\(\s*\"audio\"", js)
    assert "controls" in js
    assert "textarea" in js


def test_status_labels(js):
    for key in ("planned", "generated", "selected", "processed", "final"):
        assert re.search(rf"\b{key}\s*:", js), key


def test_confirm_license_before_final(js):
    # 原生 prompt() 已移除，授權改由 fekit 的 ask() 取得，且確實寫進 final 的 payload。
    assert re.search(r"const license\s*=\s*await\s+ask\(", js)
    assert re.search(r'status:\s*"final",\s*license', js)


@pytest.mark.skipif(shutil.which("node") is None, reason="沒有 node")
def test_syntax_ok_with_node():
    result = subprocess.run(["node", "--check", str(SRC)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
