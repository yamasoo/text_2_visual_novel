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
    assert re.search(r"PAGES\.audio\s*=\s*async\s+function\s*\(\s*bookId", js)
    assert "crumbs(" in js and "音樂" in js


def test_no_unsafe_apis(js):
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "fetch("):
        assert bad not in js, bad


def test_no_hardcoded_host(js):
    for bad in ("127.0.0.1", "localhost", "http://", "https://"):
        assert bad not in js, bad


def test_api_routes_used(js):
    assert "/art/audio" in js
    for part in ("/file?path=", "/draft", "/prompt", "/generate", "/select", "/scan", "/art/status", "/api/jobs/", "/stop"):
        assert part in js, part
    assert "encodeURIComponent" in js
    assert re.search(r'method:\s*"PUT"', js)


def test_payloads(js):
    for key in ("only", "candidates", "regenerate", "asset_id", "style", "lyrics", "loop", "status", '"final"', "license"):
        assert key in js, key
    assert "AiPanel.poll" in js and "AiPanel.executor" in js


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
    assert "prompt(" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="沒有 node")
def test_syntax_ok_with_node():
    result = subprocess.run(["node", "--check", str(SRC)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
