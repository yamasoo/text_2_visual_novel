import copy
import json
from pathlib import Path

import pytest

from t2vn.prototype import (EXPR_TO_MOOD, TEMPLATE_PATCHES, PrototypeError, build_prototype, patched_template,
                            render_html, to_ruxi_script, validate_script, walk)

STYLE = {
    "title": "T", "subtitle": "S", "version": "v0", "credits": "C",
    "palette": {"bg": "#000", "panel": "#111", "text": "#eee", "dim": "#888", "accent": "#c90"},
    "player": {"name": "P", "role": "R", "hue": 10, "shape": "small"},
    "labels": {"source_loc": "ch{chapter_no} {first}-{last}", "source_loc_one": "ch{chapter_no} {first}",
               "book_line": "ch{chapter_no} {title}", "ending_title": "{title} end", "ending_line": "-- end --"},
    "locations": {"yard": {"label": "Yard"}, "bag": {"label": "Bag", "c1": "#010101", "c2": "#020202"}},
    "times": {"dawn": {"label": "Dawn", "c1": "#111111", "c2": "#222222"}},
}
CAST = {"schema_version": "0.1", "book_id": "bk", "characters": {
    "hero": {"names": {"zh-TW": "H"}, "aliases": [], "role": "protagonist", "hue": 20, "shape": "small"},
    "old": {"names": {"zh-TW": "O"}, "aliases": [], "role": "supporting", "hue": 200, "shape": "bent"},
    "player": {"names": {"zh-TW": "Me"}, "aliases": [], "role": "player"}}}
CHAPTER = {"chapter_id": "ch001", "chapter_no": 1, "title": "Title",
           "paragraphs": [{"para_no": i, "text": f"paragraph {i} " + "x" * 80} for i in range(1, 6)]}
SCENES = {"schema_version": "0.1", "book_id": "bk", "chapter_id": "ch001", "lang": "zh-TW", "scenes": [
    {"scene_id": "ch001_sc01", "title": {"zh-TW": "One"}, "location_id": "yard", "time_of_day": "dawn",
     "bg": {"asset_id": "bg_a"}, "lines": [
        {"type": "narration", "text": "n1", "source_para": 1},
        {"type": "dialogue", "speaker": "hero", "expr": "angry", "text": "d1", "source_para": 2},
        {"type": "direction", "effect": "shake"},
        {"type": "dialogue", "speaker": "player", "text": "inner", "source_para": 3}],
     "next": "ch001_sc02"},
    {"scene_id": "ch001_sc02", "location_id": "bag", "time_of_day": "dawn", "bg": {"asset_id": "bg_b"},
     "lines": [{"type": "dialogue", "speaker": "old", "text": "d2", "source_para": 5}], "next": None}]}


def make():
    return to_ruxi_script(copy.deepcopy(SCENES), CAST, CHAPTER, STYLE)


def test_conversion():
    s = make()
    assert set(s["cast"]) == {"hero", "old", "me"}
    sc1, sc2 = s["scenes"]
    assert sc1["lines"] == [{"who": None, "text": "n1"}, {"who": "hero", "text": "d1", "mood": "hard"},
                            {"who": "me", "text": "inner"}]
    assert sc2["lines"][0]["mood"] == "calm"  # 沒有 expr -> neutral -> calm
    assert sc1["bg"]["label"] == "Yard・Dawn" and sc2["bg"]["c1"] == "#010101"
    assert sc1["source"]["loc"] == "ch1 1-3" and len(sc1["source"]["zh"]) <= 60
    assert sc2["next"] == "end_ch001" and s["endings"][0]["canon"] is True
    assert s["endings"][0]["source"]["loc"] == "ch1 5"
    assert s["verdict"] == {}


def test_validates_and_walks():
    s = make()
    errors, _ = validate_script(s)
    assert errors == []
    paths = walk(s)
    assert len(paths) == 1 and paths[0][1] == "end_ch001"


def test_all_exprs_mapped():
    assert set(EXPR_TO_MOOD) == {"neutral", "smile", "sad", "angry", "surprised", "worried", "serious", "embarrassed"}
    assert set(EXPR_TO_MOOD.values()) <= {"calm", "tense", "soft", "hard"}


def test_missing_visual_or_style_raises():
    cast = copy.deepcopy(CAST)
    del cast["characters"]["old"]["hue"]
    with pytest.raises(PrototypeError):
        to_ruxi_script(SCENES, cast, CHAPTER, STYLE)
    style = copy.deepcopy(STYLE)
    del style["locations"]["bag"]
    with pytest.raises(PrototypeError):
        to_ruxi_script(SCENES, CAST, CHAPTER, style)


def test_choice_not_supported_yet():
    scenes = copy.deepcopy(SCENES)
    scenes["scenes"][1]["lines"].append({"type": "choice", "prompt": "p", "options": []})
    with pytest.raises(PrototypeError):
        to_ruxi_script(scenes, CAST, CHAPTER, STYLE)


def test_template_patches_apply_and_remove_simplified_ui():
    html = patched_template()
    for old, new in TEMPLATE_PATCHES:
        assert new in html
    assert 'lang="zh-Hant-TW"' in html and "Noto Serif TC" in html
    rendered = render_html(make())
    assert "/*__SCRIPT_JSON__*/null" not in rendered and '"title":"T"' in rendered


def test_broken_template_is_detected(tmp_path):
    bad = tmp_path / "t.html"
    bad.write_text("<html>/*__SCRIPT_JSON__*/null</html>", encoding="utf-8")
    with pytest.raises(PrototypeError):
        patched_template(bad)


def test_build_writes_files(tmp_path):
    work = tmp_path / "works"
    (work / "chapters").mkdir(parents=True)
    (work / "chapters" / "ch001.json").write_text(json.dumps(CHAPTER), encoding="utf-8")
    (work / "cast.json").write_text(json.dumps(CAST), encoding="utf-8")
    scenes = tmp_path / "ch001.json"
    scenes.write_text(json.dumps(SCENES), encoding="utf-8")
    style = tmp_path / "style.json"
    style.write_text(json.dumps(STYLE), encoding="utf-8")
    summary = build_prototype(scenes, work, tmp_path / "out", style)
    assert summary["scenes"] == 2 and summary["paths"] == 1
    assert (tmp_path / "out" / "index.html").is_file() and (tmp_path / "out" / "script.json").is_file()


def test_multi_chapter_chains_and_builds(tmp_path):
    from t2vn.prototype import to_ruxi_script_multi
    ch2 = copy.deepcopy(CHAPTER)
    ch2.update(chapter_id="ch002", chapter_no=2, title="Two")
    sc2 = copy.deepcopy(SCENES)
    sc2["chapter_id"] = "ch002"
    for i, scene in enumerate(sc2["scenes"], 1):
        scene["scene_id"] = f"ch002_sc0{i}"
    sc2["scenes"][0]["next"] = "ch002_sc02"
    style = copy.deepcopy(STYLE)
    style["labels"]["book_line_range"] = "{first_no}-{last_no}"
    script = to_ruxi_script_multi([(copy.deepcopy(SCENES), CHAPTER), (sc2, ch2)], CAST, style)
    ids = [s["id"] for s in script["scenes"]]
    assert ids == ["ch001_sc01", "ch001_sc02", "ch002_sc01", "ch002_sc02"]
    assert script["scenes"][1]["next"] == "ch002_sc01"          # 章末接下一章
    assert script["scenes"][3]["next"] == "end_ch002"           # 只有最後一章接結局
    assert [e["id"] for e in script["endings"]] == ["end_ch002"]
    assert script["meta"]["book"]["title"] == "1-2"
    errors, _ = validate_script(script)
    assert errors == []
    assert [p[1] for p in walk(script)] == ["end_ch002"]
    with pytest.raises(PrototypeError):
        to_ruxi_script_multi([(copy.deepcopy(SCENES), CHAPTER), (copy.deepcopy(SCENES), CHAPTER)], CAST, style)
