"""t2vn/style_edit.py 的驗收測試：HTML 原型 style.json 的驗證、合併、讀寫。"""
import copy
import json

import pytest

from t2vn.style_edit import load_style, merge_style, save_style, validate_style

GOOD = {
    "_comment": "保留的未知頂層欄位",
    "title": "示範書", "subtitle": "原型", "version": "v0.1", "credits": "",
    "palette": {"bg": "#11161c", "panel": "#1a222b", "text": "#d7dde3", "dim": "#7c8794", "accent": "#c99a62"},
    "player": {"name": "玩家", "role": "主角", "hue": 28, "shape": "small"},
    "labels": {"source_loc": "第 {chapter_no} 章", "book_line": "第 {chapter_no} 章　{title}"},
    "locations": {"dock": {"label": "碼頭"}, "bag": {"label": "浮筒內", "c1": "#07090c", "c2": "#18222c"}},
    "times": {"dawn": {"label": "清晨", "c1": "#1b2533", "c2": "#4d5a68"}, "day": {"label": "白天", "c1": "#2c4258", "c2": "#6f8597"}},
}


def test_valid_document_passes():
    assert validate_style(GOOD) == []
    assert validate_style({}) == []                      # 空檔也算合法（全部欄位都是選填）


def test_top_level_must_be_dict():
    assert validate_style([]) != [] and validate_style("x") != [] and validate_style(None) != []


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["palette"].__setitem__("bg", "red"), "palette"),
    (lambda d: d["palette"].__setitem__("bg", "#12345"), "palette"),
    (lambda d: d["palette"].__setitem__("bg", "#GGGGGG"), "palette"),
    (lambda d: d["player"].__setitem__("hue", 360), "hue"),
    (lambda d: d["player"].__setitem__("hue", -1), "hue"),
    (lambda d: d["player"].__setitem__("hue", 1.5), "hue"),
    (lambda d: d["player"].__setitem__("hue", True), "hue"),
    (lambda d: d["player"].__setitem__("shape", "cube"), "shape"),
    (lambda d: d["locations"].__setitem__("Bad Key", {"label": "x"}), "Bad Key"),
    (lambda d: d["locations"].__setitem__("碼頭", {"label": "x"}), "碼頭"),
    (lambda d: d["locations"]["dock"].__setitem__("label", ""), "dock"),
    (lambda d: d["locations"]["dock"].__setitem__("label", 5), "dock"),
    (lambda d: d["locations"]["bag"].__setitem__("c1", "blue"), "c1"),
    (lambda d: d["times"].__setitem__("night", "夜晚"), "night"),          # times 的值必須是 dict
    (lambda d: d["times"]["day"].__setitem__("c2", "#12"), "c2"),
    (lambda d: d["labels"].__setitem__("source_loc", 3), "source_loc"),
    (lambda d: d.__setitem__("title", ""), "title"),
    (lambda d: d.__setitem__("palette", "dark"), "palette"),
    (lambda d: d.__setitem__("locations", []), "locations"),
])
def test_invalid_documents_report_a_helpful_error(mutate, needle):
    doc = copy.deepcopy(GOOD)
    mutate(doc)
    errors = validate_style(doc)
    assert errors and any(needle in e for e in errors)


def test_all_errors_are_reported_together():
    doc = copy.deepcopy(GOOD)
    doc["palette"]["bg"] = "red"
    doc["player"]["hue"] = 999
    assert len(validate_style(doc)) >= 2


def test_merge_recurses_into_dicts_and_replaces_other_values():
    base = copy.deepcopy(GOOD)
    patch = {"title": "新書名", "palette": {"bg": "#000000"}, "locations": {"dock": {"label": "新碼頭"}, "new_place": {"label": "新地點"}}}
    merged = merge_style(base, patch)
    assert merged["title"] == "新書名"
    assert merged["palette"]["bg"] == "#000000" and merged["palette"]["panel"] == "#1a222b"      # 其他色保留
    assert merged["locations"]["dock"] == {"label": "新碼頭"}
    assert merged["locations"]["bag"]["c1"] == "#07090c"
    assert merged["locations"]["new_place"] == {"label": "新地點"}
    assert list(merged["locations"]) == ["dock", "bag", "new_place"]        # 舊 key 順序在前，新 key 附後
    assert base == GOOD                                                      # 不修改輸入
    assert patch["palette"] == {"bg": "#000000"}


def test_merge_none_deletes_key_and_list_is_replaced():
    merged = merge_style({"a": {"x": 1, "y": 2}, "b": [1, 2], "c": 3}, {"a": {"x": None}, "b": [9], "c": None, "d": None})
    assert merged == {"a": {"y": 2}, "b": [9]}                               # d 原本就沒有，刪除要靜默


def test_merge_dict_replaced_by_scalar_and_back():
    assert merge_style({"a": {"x": 1}}, {"a": 5}) == {"a": 5}
    assert merge_style({"a": 5}, {"a": {"x": 1}}) == {"a": {"x": 1}}


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "prototype" / "style.json"
    path.parent.mkdir()
    save_style(path, GOOD)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n") and "  \"title\"" in text and "示範書" in text and "\\u" not in text
    assert load_style(path) == GOOD
    assert list(json.loads(text)) == list(GOOD)                               # 保持 key 順序


def test_save_rejects_invalid_and_leaves_file_alone(tmp_path):
    path = tmp_path / "style.json"
    save_style(path, GOOD)
    before = path.read_text(encoding="utf-8")
    bad = copy.deepcopy(GOOD)
    bad["palette"]["bg"] = "red"
    bad["player"]["hue"] = 999
    with pytest.raises(ValueError) as info:
        save_style(path, bad)
    assert "palette" in str(info.value) and "hue" in str(info.value) and ";" in str(info.value)
    assert path.read_text(encoding="utf-8") == before


def test_load_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_style(tmp_path / "nope.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError):
        load_style(broken)
    arr = tmp_path / "arr.json"
    arr.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_style(arr)
