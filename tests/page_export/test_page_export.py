"""ui/page_export.js 的靜態檢查（DS 端沒有瀏覽器；畫面由 Claude 用 Playwright 驗收）。"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "ui" / "page_export.js"


@pytest.fixture(scope="module")
def js():
    assert SRC.is_file(), "ui/page_export.js 不存在"
    return SRC.read_text(encoding="utf-8")


def test_page_registered(js):
    assert js.lstrip().startswith("/*")
    assert '"use strict"' in js
    # 頁面改由 fekit 註冊：路由字串 + ctx（原本是 PAGES.export = async function (bookId)）。
    assert re.search(r'FE\.PAGES\["book/:id/export"\]\s*=\s*async\s*\(\s*ctx\s*\)', js)
    assert "crumbs(" in js and "輸出" in js


def test_no_unsafe_apis(js):
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "fetch("):
        assert bad not in js, bad


def test_no_hardcoded_host(js):
    for bad in ("127.0.0.1", "localhost", "http://", "https://"):
        assert bad not in js, bad


def test_api_routes_used(js):
    assert "/export" in js
    # 任務端點改走 fekit 的 /api/fe-jobs，停止鈕改呼叫 tracker.stop()（原本打 /api/jobs/{id} 與 /api/jobs/{id}/stop）。
    for part in ("/export/settings", "/export/title", "/export/run", "/export/verify", "/export/report", "/export/shot", "/art/refresh", "/api/fe-jobs", "tracker.stop"):
        assert part in js, part
    assert "encodeURIComponent" in js
    assert len(re.findall(r'method:\s*"PUT"', js)) >= 2


def test_payload_keys(js):
    for key in ("sdk", "project", "tags", "description", "chapters", "placeholders", "clean", "report", "summary", "cases", "preflight_errors"):
        assert key in js, key
    # AiPanel.poll → Panel.attach。
    assert "Panel.attach" in js


def test_ui_labels(js):
    for label in ("標題畫面", "標題圖", "標題音樂", "存標題圖提示詞", "加入素材清單", "Ren'Py 設定", "SDK", "匯出", "補佔位圖", "先清掉舊檔",
                  "全選", "自動驗收", "跑驗收", "驗收報告", "看報告", "停止"):
        assert label in js, label
    assert "textarea" in js and "checkbox" in js and "<img" not in js and "img" in js


def test_case_labels_and_job_states(js):
    for key in ("launch", "screens", "input", "main_loop_ending", "restart"):
        assert key in js, key
    for text in ("啟動", "畫面", "輸入", "主循環與結局", "重開", "通過", "失敗", "未知"):
        assert text in js, text
    for state in ("running", "done", "error", "stopped"):
        assert state in js, state


def test_links_to_other_pages(js):
    assert "#/book/" in js and "/assets" in js and "/audio" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="沒有 node")
def test_syntax_ok_with_node():
    result = subprocess.run(["node", "--check", str(SRC)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
