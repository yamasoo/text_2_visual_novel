"""lint_parse 的驗收測試。fixtures：clean.txt 是真實乾淨輸出；其餘為合成資料。"""

from __future__ import annotations

from pathlib import Path

import pytest

from t2vn import lint_parse as m

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_clean_report():
    result = m.parse_lint(read("clean.txt"))
    assert result["version"] == "8.5.3.26051504"
    assert result["statistics"] == {"dialogue_blocks": 541, "words": 556, "characters": 18027, "menus": 0, "images": 71, "screens": 25}
    assert result["issues"] == [] and result["crashed"] is False and result["clean"] is True


def test_problem_report():
    result = m.parse_lint(read("problems.txt"))
    assert result["clean"] is False and result["crashed"] is False
    assert result["statistics"]["menus"] == 2
    issues = result["issues"]
    assert [(i["file"], i["line"]) for i in issues] == [
        ("game/vn_ch001.rpy", 12), ("game/vn_ch002.rpy", 40), ("game/vn_ch002.rpy", 44), ("game/options.rpy", 3)]
    assert issues[0]["message"] == "The image named 'ellie_reed angry' was not found."
    assert issues[1]["message"] == "The character c_ghost is used but is not defined with define."
    assert [i["level"] for i in issues] == ["error", "error", "warning", "error"]


def test_crash_report():
    result = m.parse_lint(read("crash.txt"))
    assert result["crashed"] is True and result["clean"] is False and result["statistics"] == {}
    assert result["version"] == "8.5.3.26051504"


@pytest.mark.parametrize("text", ["", None])
def test_empty_input(text):
    result = m.parse_lint(text)
    assert result == {"version": "", "statistics": {}, "issues": [], "crashed": False, "clean": False}


def test_bom_and_crlf():
    raw = "﻿" + read("problems.txt").replace("\n", "\r\n")
    result = m.parse_lint(raw)
    assert result["version"] == "8.5.3.26051504" and len(result["issues"]) == 4
    assert result["issues"][1]["message"].endswith("with define.")


def test_issue_first_line_with_bom_still_parsed():
    issues = m.parse_issues("﻿game/a.rpy:1 Something is missing.\n")
    assert issues == [{"file": "game/a.rpy", "line": 1, "message": "Something is missing.", "level": "error"}]


def test_continuation_ends_at_blank_line():
    text = "game/a.rpy:1 First thing\n    continues here\n\n    not a continuation\ngame/a.rpy:2 Second\n"
    issues = m.parse_issues(text)
    assert [i["message"] for i in issues] == ["First thing continues here", "Second"]


def test_non_issue_lines_ignored():
    assert m.parse_issues("Statistics:\n\nThe game contains 1 menus.\nsomething: 12 not an issue\n") == []


def test_classify():
    assert m.classify("The image 'x' was not found.") == "error"
    assert m.classify("UNKNOWN property") == "error"
    assert m.classify("Strange text looks odd") == "warning"


def test_statistics_partial_and_commas():
    stats = m.parse_statistics("The game contains 1,234 dialogue blocks, containing 5 words and 6\ncharacters, for x.\n")
    assert stats == {"dialogue_blocks": 1234, "words": 5, "characters": 6}
    assert m.parse_statistics("nothing here") == {}


def test_version_missing():
    assert m.parse_lint("hello\n")["version"] == ""


def test_clean_requires_statistics():
    assert m.parse_lint("Ren'Py 8.0.0 lint report, generated at: x\n")["clean"] is False


def test_crash_pattern_in_issue_text_counts():
    assert m.parse_lint("The game contains 1 menus.\nValueError: nope\n")["crashed"] is True


def test_render_markdown_clean():
    text = m.render_markdown(m.parse_lint(read("clean.txt")))
    assert text.splitlines()[:3] == ["## Lint", "- 版本：8.5.3.26051504", "- 統計：對白 541 段、18027 字、0 個選單、71 張圖、25 個畫面"]
    assert "- 沒有發現問題" in text and text.endswith("\n")


def test_render_markdown_problems_and_crash():
    text = m.render_markdown(m.parse_lint(read("problems.txt")))
    assert "- [錯誤] The image named 'ellie_reed angry' was not found.（game/vn_ch001.rpy:12）" in text
    assert "- [警告] Strange text 'foo' (style warning).（game/vn_ch002.rpy:44）" in text
    assert "沒有發現問題" not in text
    crash = m.render_markdown(m.parse_lint(read("crash.txt")))
    assert "- [錯誤] Lint 執行時當掉，請看 lint.txt" in crash and "統計" not in crash
    assert m.render_markdown(m.parse_lint("")).splitlines()[1] == "- 版本：未知"
