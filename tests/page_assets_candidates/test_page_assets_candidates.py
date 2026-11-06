"""ui/page_assets.js 候選圖勾選刪除的靜態檢查（DS 端沒有瀏覽器；畫面由 Claude 用 Playwright 驗收）。"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "ui" / "page_assets.js"


@pytest.fixture(scope="module")
def js():
    assert SRC.is_file(), "ui/page_assets.js 不存在"
    return SRC.read_text(encoding="utf-8")


def test_existing_structure_kept(js):
    assert re.search(r'FE\.PAGES\["book/:id/assets"\]\s*=\s*async\s*\(ctx\)\s*=>', js)
    for name in ("listPane", "lookPane", "stylePane", "AppearanceCards.render", "/select", "/generate", "/refresh", "/status", "/appearance"):
        assert name in js, name


def test_no_unsafe_apis(js):
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "fetch("):
        assert bad not in js, bad


def test_delete_candidates_ui(js):
    assert "/candidates/delete" in js
    for label in ("刪除所選", "全選", "全不選", "已選"):
        assert label in js, label
    assert re.search(r'type:\s*"checkbox"', js)
    assert "asset_id" in js and "names" in js
    assert "confirm(" in js


def test_headshot_kind_shown(js):
    assert re.search(r"headshot\s*:\s*\"頭像\"", js)
    assert re.search(r'\["headshot",\s*"頭像"\]', js), "種類篩選要有頭像"


def test_no_hardcoded_host(js):
    assert "127.0.0.1" not in js and "localhost" not in js and "http://" not in js and "https://" not in js


@pytest.mark.skipif(shutil.which("node") is None, reason="沒有 node")
def test_syntax_ok_with_node():
    result = subprocess.run(["node", "--check", str(SRC)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_bulk_buttons(js):
    for text in ("一括定稿", "全部退回定稿前", "未出圖的全部各出 1 張", "bulkFinalize", "bulkUnfinalize", "candidates: 1"):
        assert text in js, text
