"""ui/page_appearance.js（v2）的靜態檢查（DS 端沒有瀏覽器；真正的畫面由 Claude 用 Playwright 驗收）。"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "ui" / "page_appearance.js"


@pytest.fixture(scope="module")
def js():
    assert SRC.is_file(), "ui/page_appearance.js 不存在"
    return SRC.read_text(encoding="utf-8")


def test_registers_page_and_component(js):
    # 頁面改由 fekit 註冊：路由字串 + ctx（原本是 PAGES.appearance = async function (bookId)）。
    assert re.search(r'FE\.PAGES\["book/:id/appearance"\]\s*=\s*async\s*\(\s*ctx\s*\)', js)
    assert re.search(r"window\.AppearanceCards\s*=", js)
    assert re.search(r"\brender\b", js)


def test_wrapped_in_iife_so_no_global_collisions(js):
    body = re.sub(r"/\*.*?\*/|//[^\n]*", "", js, flags=re.S).strip()
    body = body.removeprefix('"use strict";').strip()
    assert re.match(r"\(\s*(async\s+)?(function\s*\(\s*\)|\(\s*\)\s*=>)\s*\{", body), "最外層要用 IIFE 包起來"
    assert body.rstrip().endswith("})();")


def test_no_unsafe_apis(js):
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "fetch("):
        assert bad not in js, bad


def test_uses_every_endpoint(js):
    for piece in ("/cards", "/cards/roll", "/cards/generate", "/draft", "/wildcards", "/wildcards/enabled",
                  "/styles", "/styles/upload", "/styles/apply", "/styles/clear", "/file?path="):
        assert piece in js, piece
    # 畫風表上傳改走 fekit 的 FE.upload（原本是頁面自己的 uploadFile()）。
    assert "FE.upload(" in js
    assert "/styles/import" not in js, "畫風表改成選檔上傳，不再輸入路徑"


def test_two_prompt_fields_and_no_old_fields(js):
    assert "headshot_prompt" in js and "sprite_prompt" in js and "expressions" in js
    for old in ("hair_style", "skin_tone", "hair_ornament", "插入 wildcard", "/headshot/select"):
        assert old not in js, old


def test_expression_checkboxes(js):
    for value in ("neutral", "smile", "angry", "sad", "serious", "worried", "surprised"):
        assert f'"{value}"' in js, value
    for label in ("平靜", "喜", "怒", "哀", "認真", "擔心", "驚訝"):
        assert f'"{label}"' in js, label


def test_wildcard_autocomplete_keys(js):
    for key in ("ArrowDown", "ArrowUp", "Tab", "Enter", "Escape"):
        assert key in js, key
    assert "keydown" in js or "onkeydown" in js
    assert "selectionStart" in js


def test_per_character_generate_block(js):
    assert "sprite_scenes" in js
    assert re.search(r'type:\s*"number"', js) and re.search(r"min:\s*\"?1\"?", js) and re.search(r"max:\s*\"?16\"?", js)
    assert re.search(r'type:\s*"checkbox"', js)
    for label in ("頭像", "立繪", "張數", "出圖", "停止", "擲骰", "AI 草擬", "存草稿", "確認", "畫風", "套用", "清除"):
        assert label in js, label
    assert "char_id" in js
    assert "全選角色" not in js, "全域出圖列已移除"


def test_uses_helpers_from_app(js):
    for helper in ("h(", "api(", "toast(", "fail(", "badge(", "crumbs("):
        assert helper in js, helper
    # AiPanel.executor → Executors.value、AiPanel.poll → Panel.attach。
    assert "Executors.value" in js and "Panel.attach" in js


def test_no_hardcoded_book_or_host(js):
    assert "127.0.0.1" not in js and "localhost" not in js and "http://" not in js and "https://" not in js


@pytest.mark.skipif(shutil.which("node") is None, reason="沒有 node")
def test_syntax_ok_with_node():
    result = subprocess.run(["node", "--check", str(SRC)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
