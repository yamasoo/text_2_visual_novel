import copy

import pytest

from t2vn.art_prompts import (BG_NEGATIVE, DEFAULT_ART, EXPR_TAGS, EXPRESSIONS, SPRITE_NEGATIVE, TIME_TAGS, bg_index, bg_prompt, merge_art,
                              normalize_tags, prompt_for_asset, seed_for, split_sprite_file, sprite_prompt)

ART = {
    "artist": "someone, @other",
    "extra": "watercolor",
    "locations": {"yard": {"tags": "scrapyard, Rust, rust", "description": "A rusty airship yard by the sea"}},
    "characters": {"ellie": {"tags": "1girl, short_hair, brown eyes", "description": "A young mechanic with oil on her cheek."}},
    "bg": {"width": 1000},
    "sprite": {"extra": "flat colors"},
}


def test_normalize_tags():
    assert normalize_tags("  Masterpiece,\nbest_quality , score_7,, Best Quality, @Artist_Name ") == "masterpiece, best quality, score_7, @artist name"
    assert normalize_tags("") == "" and normalize_tags(None) == ""


def test_merge_art_does_not_mutate_and_merges_sections():
    original = copy.deepcopy(ART)
    merged = merge_art(ART)
    assert ART == original and DEFAULT_ART["bg"]["width"] == 1280
    assert merged["bg"] == {"width": 1000, "height": 720, "extra": ""} and merged["sprite"]["height"] == 1568
    assert merged["quality"] == DEFAULT_ART["quality"] and merge_art(None)["negative"] == DEFAULT_ART["negative"]


def test_bg_prompt_structure():
    result = bg_prompt("yard", "dawn", ART)
    head, _, description = result["positive"].partition("\n")
    assert head == ("masterpiece, best quality, score_7, safe, @someone, @other, no humans, scenery, scrapyard, rust, "
                    + TIME_TAGS["dawn"] + ", watercolor.")
    assert description == "A rusty airship yard by the sea."
    assert result["width"] == 1000 and result["height"] == 720 and result["warnings"] == []
    for tag in BG_NEGATIVE.split(", "):
        assert tag in result["negative"].split(", ")
    assert "worst quality" in result["negative"]


def test_bg_prompt_unknown_location_and_time():
    result = bg_prompt("old_warehouse", "night", None)
    assert "old warehouse" in result["positive"] and "\n" not in result["positive"] and len(result["warnings"]) == 1
    with pytest.raises(ValueError):
        bg_prompt("yard", "midnight", ART)


def test_sprite_prompt_structure():
    result = sprite_prompt("ellie", "worried", ART)
    head, _, description = result["positive"].partition("\n")
    assert head == ("masterpiece, best quality, score_7, safe, @someone, @other, solo, 1girl, short hair, brown eyes, "
                    + EXPR_TAGS["worried"] + ", standing, full body, looking at viewer, simple background, green background, watercolor, flat colors.")
    assert description == "A young mechanic with oil on her cheek."
    assert (result["width"], result["height"]) == (672, 1568) and result["warnings"] == []
    for tag in SPRITE_NEGATIVE.split(", "):
        assert tag in result["negative"].split(", ")


def test_sprite_prompt_missing_tags_and_bad_expr():
    result = sprite_prompt("ghost", "smile", None)
    assert len(result["warnings"]) == 1 and result["positive"].startswith("masterpiece") and "solo, smile" in result["positive"]
    with pytest.raises(ValueError):
        sprite_prompt("ellie", "grumpy", ART)


def test_every_expression_and_time_has_tags():
    assert set(EXPRESSIONS) == set(EXPR_TAGS) and set(TIME_TAGS) == {"dawn", "morning", "day", "dusk", "night"}


def test_bg_index_first_occurrence_wins():
    docs = [{"scenes": [{"scene_id": "a", "location_id": "yard", "time_of_day": "dawn", "bg": {"asset_id": "bg_1"}},
                        {"scene_id": "b", "location_id": "dock", "time_of_day": "night", "bg": {"asset_id": "bg_1"}},
                        {"scene_id": "c", "location_id": "dock", "time_of_day": "night", "bg": {"asset_id": "bg_2"}}]}]
    assert bg_index(docs) == {"bg_1": ("yard", "dawn"), "bg_2": ("dock", "night")}


def test_split_sprite_file():
    assert split_sprite_file("assets/sprite/ellie_reed_neutral.png") == ("ellie_reed", "neutral")
    assert split_sprite_file("assets/sprite/yumin_1_worried.png") == ("yumin_1", "worried")
    for bad in ("assets/sprite/ellie.png", "assets/bg/x_smile.png", "assets/sprite/_smile.png", "assets/sprite/a_smile.jpg"):
        with pytest.raises(ValueError):
            split_sprite_file(bad)


def test_prompt_for_asset_dispatch():
    index = {"bg_yard_dawn": ("yard", "dawn")}
    assert prompt_for_asset({"kind": "bg", "asset_id": "bg_yard_dawn", "file": "assets/bg/bg_yard_dawn.png"}, index, ART) == bg_prompt("yard", "dawn", ART)
    assert prompt_for_asset({"kind": "sprite", "asset_id": "sprite_ellie_smile", "file": "assets/sprite/ellie_smile.png"}, index, ART) == sprite_prompt("ellie", "smile", ART)
    with pytest.raises(ValueError):
        prompt_for_asset({"kind": "bg", "asset_id": "bg_other", "file": "assets/bg/bg_other.png"}, index, ART)
    with pytest.raises(ValueError):
        prompt_for_asset({"kind": "bgm", "asset_id": "bgm_a", "file": "assets/bgm/bgm_a.opus"}, index, ART)


def test_seed_for_is_stable_and_varies():
    assert seed_for("bg_a") == seed_for("bg_a", 0) and seed_for("bg_a", 1) != seed_for("bg_a", 0) and seed_for("bg_b") != seed_for("bg_a")
    assert 0 <= seed_for("x", 5) < 2**31
    with pytest.raises(ValueError):
        seed_for("x", -1)
