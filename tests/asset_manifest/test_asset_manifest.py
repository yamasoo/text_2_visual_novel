import copy
import json

import pytest
from jsonschema import Draft202012Validator

from t2vn import asset_manifest
from t2vn.asset_manifest import SPEC, build_manifest, collect_needs, main

CAST = {"book_id": "bk", "characters": {"ellie": {"names": {"zh-TW": "艾莉"}}, "zhang": {"names": {"zh-TW": "老張"}}, "player": {"names": {"zh-TW": "內心"}}}}
STYLE = {"locations": {"yard": {"label": "舊工坊"}}, "times": {"dawn": {"label": "清晨"}}}


def scene(sid, bg, loc, tod, cast=None, lines=None, **extra):
    return {"scene_id": sid, "location_id": loc, "time_of_day": tod, "bg": {"asset_id": bg}, "cast": cast or [],
            "lines": lines or [{"type": "narration", "text": "x", "source_para": 1}], **extra}


DOC1 = {"scenes": [
    scene("ch001_sc01", "bg_yard_dawn", "yard", "dawn", cast=[{"char_id": "ellie", "expr": "neutral"}, {"char_id": "zhang"}], bgm="bgm_a", ambience="amb_x",
          lines=[{"type": "dialogue", "speaker": "zhang", "expr": "smile", "text": "好", "source_para": 1},
                 {"type": "dialogue", "speaker": "player", "expr": "sad", "text": "唉", "source_para": 2}]),
    scene("ch001_sc02", "bg_yard_dawn", "yard", "dawn", cast=[{"char_id": "ellie", "expr": "neutral"}], bgm="bgm_a"),
]}
DOC2 = {"scenes": [scene("ch002_sc01", "bg_dock_night", "dock", "night", bgm=None, ambience=None)]}


def by_id(needs):
    return {n["asset_id"]: n for n in needs}


def test_collect_backgrounds_names_and_used_in():
    needs = by_id(collect_needs([DOC1, DOC2], CAST, STYLE))
    bg = needs["bg_yard_dawn"]
    assert bg["kind"] == "bg" and bg["names"] == {"zh-TW": "舊工坊・清晨"} and bg["file"] == "assets/bg/bg_yard_dawn.png"
    assert bg["used_in"] == ["ch001_sc01", "ch001_sc02"] and bg["spec"] == SPEC["bg"]
    assert needs["bg_dock_night"]["names"] == {"zh-TW": "dock・夜晚"}  # 沒有風格設定：地點用 id、時段用內建名稱


def test_collect_sprites_from_cast_and_dialogue_not_player():
    needs = by_id(collect_needs([DOC1], CAST, STYLE))
    assert {k for k in needs if k.startswith("sprite_")} == {"sprite_ellie_neutral", "sprite_zhang_neutral"}  # 立繪只出 neutral（其他表情用頭像）
    ellie = needs["sprite_ellie_neutral"]
    assert ellie["file"] == "assets/sprite/ellie_neutral.png" and ellie["names"] == {"zh-TW": "艾莉（neutral）"}
    assert ellie["used_in"] == ["ch001_sc01", "ch001_sc02"] and ellie["spec"] == SPEC["sprite"]
    assert "ch001_sc01" in needs["sprite_zhang_neutral"]["used_in"]  # smile 的用處併進 neutral


def test_collect_audio_and_order():
    needs = collect_needs([DOC1, DOC2], CAST, STYLE)
    ids = [n["asset_id"] for n in needs]
    assert ids[0] == "bg_yard_dawn" and ids.index("sprite_ellie_neutral") < ids.index("bgm_a")
    audio = by_id(needs)
    assert audio["bgm_a"]["file"] == "assets/bgm/bgm_a.opus" and "spec" not in audio["bgm_a"]
    assert audio["amb_x"]["kind"] == "ambience" and audio["amb_x"]["file"] == "assets/ambience/amb_x.opus"
    assert len(ids) == len(set(ids))


def test_unknown_character_falls_back_to_id():
    needs = by_id(collect_needs([{"scenes": [scene("ch001_sc01", "bg_a", "a", "day", cast=[{"char_id": "ghost"}])]}], CAST))
    assert needs["sprite_ghost_neutral"]["names"] == {"zh-TW": "ghost（neutral）"}


def test_build_manifest_new_and_schema_valid():
    manifest = build_manifest("bk", collect_needs([DOC1, DOC2], CAST, STYLE))
    assert manifest["schema_version"] == "0.1" and manifest["book_id"] == "bk"
    assert all(a["status"] == "planned" and a["license"] == "" for a in manifest["assets"])
    schema = json.load(open("schema/manifest.schema.json", encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(manifest))
    assert errors == []


def test_merge_keeps_progress_and_marks_unused():
    first = build_manifest("bk", collect_needs([DOC1, DOC2], CAST, STYLE))
    for a in first["assets"]:
        if a["asset_id"] == "bg_yard_dawn":
            a.update(status="selected", seed=7, prompt_en="p", negative_prompt="n", model="m", loras=["l"], license="CC0", notes="ok")
        if a["asset_id"] == "bg_dock_night":
            a["status"] = "generated"
    snapshot = copy.deepcopy(first)
    second = build_manifest("bk", collect_needs([DOC1], CAST, STYLE), first)
    assert first == snapshot  # 不改傳入的物件
    kept = by_id(second["assets"])["bg_yard_dawn"]
    assert (kept["status"], kept["seed"], kept["prompt_en"], kept["model"], kept["loras"], kept["license"], kept["notes"]) == ("selected", 7, "p", "m", ["l"], "CC0", "ok")
    gone = by_id(second["assets"])["bg_dock_night"]
    assert gone["used_in"] == [] and gone["status"] == "generated"
    assert [a["asset_id"] for a in second["assets"]][-1] == "bg_dock_night"  # 沒用到的接在後面
    assert "ch001_sc01" in by_id(second["assets"])["sprite_zhang_neutral"]["used_in"]


def test_merge_refreshes_names_and_rejects_other_book():
    first = build_manifest("bk", collect_needs([DOC1], CAST, STYLE))
    first["assets"][0]["names"] = {"zh-TW": "舊名"}
    second = build_manifest("bk", collect_needs([DOC1], CAST, STYLE), first)
    assert second["assets"][0]["names"] == {"zh-TW": "舊工坊・清晨"}
    with pytest.raises(ValueError):
        build_manifest("other", [], first)
    with pytest.raises(ValueError):
        build_manifest("bk", [{"asset_id": "Bad Id", "kind": "bg", "names": {}, "file": "x", "used_in": []}])


def test_cli(tmp_path, capsys):
    work = tmp_path / "bk"
    (work / "gold").mkdir(parents=True)
    (work / "scenes").mkdir()
    (work / "gold" / "ch001.json").write_text(json.dumps(DOC1, ensure_ascii=False), encoding="utf-8")
    (work / "scenes" / "ch002.json").write_text(json.dumps(DOC2, ensure_ascii=False), encoding="utf-8")
    (work / "cast.json").write_text(json.dumps(CAST, ensure_ascii=False), encoding="utf-8")
    assert main(["--book-dir", str(work)]) == 0
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["book_id"] == "bk" and any(a["asset_id"] == "bg_dock_night" for a in manifest["assets"])
    assert "bg 2" in capsys.readouterr().out
    assert main(["--book-dir", str(tmp_path / "nothing")]) == 1


def test_sprite_expressions_only_kept_for_the_protagonist():
    cast = {"characters": {"hero": {"role": "protagonist"}, "side": {"role": "main"}, "crew": {"role": "supporting"}}}
    d = {"scenes": [scene("ch001_sc01", "bg_a", "a", "day", cast=[{"char_id": c, "expr": "angry"} for c in ("hero", "side", "crew")])]}
    ids = {k for k in by_id(collect_needs([d], cast)) if k.startswith("sprite_")}
    assert ids == {"sprite_hero_angry", "sprite_side_neutral", "sprite_crew_neutral"}


def test_title_needs_adds_title_background_and_music_once():
    needs = asset_manifest.title_needs("zh-TW")
    assert [(n["asset_id"], n["kind"], n["file"], n["used_in"]) for n in needs] == [
        ("bg_title", "bg", "assets/bg/bg_title.png", []), ("bgm_title", "bgm", "assets/bgm/bgm_title.opus", [])]
    assert needs[0]["names"] == {"zh-TW": "標題圖"} and "spec" in needs[0] and "spec" not in needs[1]
    assert asset_manifest.title_needs("zh-TW", {"bg_title"})[0]["asset_id"] == "bgm_title"
    asset_manifest.build_manifest("bk", needs)  # 能寫進合法的 manifest
