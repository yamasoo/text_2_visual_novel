"""rpy_reach 的驗收測試（合成劇本，不含小說原文）。"""

from __future__ import annotations

import pytest

from t2vn import rpy_reach as m

GOOD = {
    "script.rpy": "label start:\n    jump ch001\n",
    "vn_ch001.rpy": (
        "label ch001:\n    scene black\n    centered \"第 1 章\"\n\n"
        "label ch001_sc01:\n    scene bg_a\n    c_a \"jump nowhere\"  # p1\n    jump ch001_sc02\n\n"
        "label ch001_sc02:\n    \"旁白\"\n    jump vn_end\n"
    ),
    "vn_end.rpy": "label vn_end:\n    scene black\n    return\n",
}


def kinds(result, level=None):
    return sorted((i["kind"], i["label"]) for i in result["issues"] if level in (None, i["level"]))


def test_parse_labels_basic():
    labels, dups = m.parse_labels(GOOD)
    assert dups == []
    names = [x["name"] for x in labels]
    assert names == ["start", "ch001", "ch001_sc01", "ch001_sc02", "vn_end"]
    by = {x["name"]: x for x in labels}
    assert by["start"]["jumps"] == ["ch001"] and by["start"]["terminal"] == "jump"
    assert by["ch001"]["terminal"] is None and by["ch001"]["fallthrough"] == "ch001_sc01"
    assert by["ch001_sc01"]["jumps"] == ["ch001_sc02"]  # 對白字串裡的 "jump nowhere" 不算
    assert by["vn_end"]["terminal"] == "return" and by["vn_end"]["fallthrough"] is None
    assert by["start"]["file"] == "script.rpy" and by["start"]["line"] == 1
    assert by["ch001_sc02"]["line"] == 10


def test_parse_ignores_comments_and_blank_tail():
    labels, _ = m.parse_labels({"a.rpy": "label a:\n    jump b\n    # 註解\n\n\nlabel b:\n    return\n"})
    assert labels[0]["terminal"] == "jump"


def test_parse_last_statement_decides_terminal():
    labels, _ = m.parse_labels({"a.rpy": "label a:\n    jump b\n    \"後面還有字\"\nlabel b:\n    return\n"})
    assert labels[0]["terminal"] is None and labels[0]["fallthrough"] == "b"


def test_parse_call_edges():
    labels, _ = m.parse_labels({"a.rpy": "label a:\n    call helper\n    return\nlabel helper:\n    return\n"})
    assert labels[0]["calls"] == ["helper"] and labels[0]["terminal"] == "return"


def test_parse_duplicates():
    labels, dups = m.parse_labels({"a.rpy": "label a:\n    return\nlabel a:\n    return\n", "b.rpy": "label a:\n    return\n"})
    assert dups == ["a"] and [x["name"] for x in labels] == ["a"]


def test_parse_fallthrough_does_not_cross_files():
    labels, _ = m.parse_labels({"a.rpy": "label a:\n    \"x\"\n", "b.rpy": "label b:\n    return\n"})
    assert labels[0]["fallthrough"] is None


def test_parse_label_with_parameters_and_top_level_stop():
    labels, _ = m.parse_labels({"a.rpy": "label f(x):\n    return\ndefine gui.x = 1\nlabel g:\n    jump f\n"})
    assert [x["name"] for x in labels] == ["f", "g"]


def test_analyze_good():
    result = m.analyze(GOOD)
    assert result["ok"] is True and result["end_reachable"] is True
    assert result["issues"] == [] and result["unreachable"] == []
    assert set(result["reachable"]) == {"start", "ch001", "ch001_sc01", "ch001_sc02", "vn_end"}
    assert result["labels"] == 5


def test_analyze_missing_target():
    files = dict(GOOD, **{"vn_ch001.rpy": GOOD["vn_ch001.rpy"].replace("jump ch001_sc02", "jump ch001_sc99")})
    result = m.analyze(files)
    assert result["ok"] is False
    assert ("missing_target", "ch001_sc01") in kinds(result, "error")
    assert ("unreachable", "ch001_sc02") in kinds(result, "warning")
    assert ("end_unreachable", "") in kinds(result, "error")


def test_analyze_dead_end():
    files = dict(GOOD, **{"vn_ch001.rpy": GOOD["vn_ch001.rpy"].replace("    jump vn_end\n", "")})
    files["vn_ch001.rpy"] += "\n"
    result = m.analyze(files)
    assert ("dead_end", "ch001_sc02") in kinds(result, "error")
    assert result["ok"] is False


def test_analyze_unreachable_is_only_warning():
    files = dict(GOOD, **{"extra.rpy": "label ch009_sc01:\n    jump vn_end\n"})
    result = m.analyze(files)
    assert result["ok"] is True
    assert kinds(result) == [("unreachable", "ch009_sc01")]
    assert result["issues"][0]["level"] == "warning" and result["issues"][0]["file"] == "extra.rpy" and result["issues"][0]["line"] == 1


def test_analyze_special_and_underscore_labels_not_reported():
    files = dict(GOOD, **{"x.rpy": "label splashscreen:\n    return\nlabel _helper:\n    return\nlabel after_load:\n    return\n"})
    assert m.analyze(files)["issues"] == []


def test_analyze_start_missing():
    result = m.analyze({"a.rpy": "label foo:\n    jump vn_end\nlabel vn_end:\n    return\n"})
    assert ("start_missing", "") in kinds(result, "error")
    assert result["ok"] is False
    assert not any(i["kind"] == "end_unreachable" for i in result["issues"])


def test_analyze_duplicate_label_is_error():
    files = dict(GOOD, **{"dup.rpy": "label vn_end:\n    return\n"})
    result = m.analyze(files)
    assert ("duplicate_label", "vn_end") in kinds(result, "error")


def test_analyze_fallthrough_reaches_next_label():
    files = {"a.rpy": "label start:\n    \"hi\"\nlabel vn_end:\n    return\n"}
    result = m.analyze(files)
    assert result["ok"] is True and result["end_reachable"] is True


def test_analyze_last_label_without_end_is_dead_end():
    result = m.analyze({"a.rpy": "label start:\n    \"hi\"\n"})
    assert ("dead_end", "start") in kinds(result, "error")
    assert ("end_unreachable", "") in kinds(result, "error")


def test_analyze_call_reaches_target():
    files = {"a.rpy": "label start:\n    call vn_end\n    return\nlabel vn_end:\n    return\n"}
    result = m.analyze(files)
    assert result["ok"] is True and result["end_reachable"] is True


def test_analyze_custom_start_and_end():
    files = {"a.rpy": "label begin:\n    jump finish\nlabel finish:\n    return\n"}
    result = m.analyze(files, start="begin", end="finish")
    assert result["ok"] is True


def test_analyze_does_not_mutate_input():
    files = dict(GOOD)
    m.analyze(files)
    assert files == GOOD


def test_referenced_files():
    files = {"a.rpy": (
        'image ellie neutral = Transform("images/sprite/ellie_neutral.png", zoom=0.62)\n'
        'image side ellie smile = "images/headshot/ellie_smile.png"\n'
        '    play music "audio/bgm/bgm_a.opus" fadein 1.0\n'
        '# image x = "images/x.png"\n'
        '    c_a "images/not_a_file.txt"\n'
        '    show screen vn_caption("地點・白天")\n'
    )}
    refs = m.referenced_files(files)
    assert [(r["path"], r["line"]) for r in refs] == [
        ("images/sprite/ellie_neutral.png", 1), ("images/headshot/ellie_smile.png", 2), ("audio/bgm/bgm_a.opus", 3)]
    assert all(r["file"] == "a.rpy" for r in refs)


def test_missing_files_dedupes_by_path():
    refs = [{"path": "images/a.png", "file": "x.rpy", "line": 1}, {"path": "images/a.png", "file": "x.rpy", "line": 5},
            {"path": "images/b.png", "file": "x.rpy", "line": 6}]
    out = m.missing_files(refs, {"images/b.png"})
    assert [(r["path"], r["line"]) for r in out] == [("images/a.png", 1)]


def test_scan_existing(tmp_path):
    (tmp_path / "images" / "bg").mkdir(parents=True)
    (tmp_path / "audio" / "bgm").mkdir(parents=True)
    (tmp_path / "images" / "bg" / "a.png").write_bytes(b"x")
    (tmp_path / "audio" / "bgm" / "b.opus").write_bytes(b"x")
    (tmp_path / "other.png").write_bytes(b"x")
    assert m.scan_existing(tmp_path) == {"images/bg/a.png", "audio/bgm/b.opus"}
    assert m.scan_existing(tmp_path / "nope") == set()


def _write_game(tmp_path, extra=""):
    game = tmp_path / "game"
    (game / "images" / "bg").mkdir(parents=True)
    (game / "images" / "bg" / "bg_a.png").write_bytes(b"x")
    (game / "script.rpy").write_text("label start:\n    jump vn_end\n" + extra, encoding="utf-8")
    (game / "vn_end.rpy").write_text("label vn_end:\n    return\n", encoding="utf-8")
    (game / "vn_verify_test.rpy").write_text("label should_be_ignored:\n    return\n", encoding="utf-8")
    return game


def test_check_game_ok(tmp_path):
    game = _write_game(tmp_path, 'image bg_a = "images/bg/bg_a.png"\n')
    result = m.check_game(game)
    assert result["ok"] is True and result["missing_files"] == [] and result["issues"] == []
    assert result["labels"] == 2


def test_check_game_missing_file(tmp_path):
    game = _write_game(tmp_path, 'image bg_b = "images/bg/bg_b.png"\n')
    result = m.check_game(game)
    assert result["ok"] is False
    assert [i["kind"] for i in result["issues"]] == ["missing_file"]
    assert result["issues"][0]["file"] == "script.rpy" and result["issues"][0]["line"] == 3
    assert result["missing_files"][0]["path"] == "images/bg/bg_b.png"


def test_check_game_not_a_dir(tmp_path):
    with pytest.raises(ValueError):
        m.check_game(tmp_path / "nope")


def test_render_markdown_clean_and_issues():
    clean = m.render_markdown(m.analyze(GOOD))
    assert clean.splitlines()[0] == "## 劇本可達性"
    assert "結局：可達" in clean.splitlines()[1] and "label 數：5" in clean and "- 沒有發現問題" in clean
    assert clean.endswith("\n")
    bad = m.render_markdown(m.analyze({"a.rpy": "label start:\n    \"hi\"\n"}))
    assert "- [錯誤] " in bad and "（a.rpy:1）" in bad and "結局：走不到" in bad
    assert "沒有發現問題" not in bad
    warn = m.render_markdown(m.analyze(dict(GOOD, **{"e.rpy": "label ch9:\n    jump vn_end\n"})))
    assert "- [警告] " in warn
