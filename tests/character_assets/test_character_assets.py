"""每個角色固定有頭像與立繪兩列；頭像出圖、提示詞、挑選。"""
from __future__ import annotations

import json

import pytest
from PIL import Image

from t2vn import art_gen, art_prompts, sprite_consistency
from t2vn.character_assets import character_needs, headshot_asset_id
from tests.art_gen.test_art_gen import CAST, Fake, env  # noqa: F401


def test_every_character_gets_headshot_and_sprite_rows():
    cast = {"characters": {"player": {}, "ellie": {"names": {"zh-TW": "艾莉"}}, "zhang": {"names": {"zh-TW": "老張"}}}}
    needs = character_needs(cast, "zh-TW")
    assert [n["asset_id"] for n in needs] == ["sprite_ellie_neutral", "headshot_ellie_neutral", "sprite_zhang_neutral", "headshot_zhang_neutral"]
    head = needs[1]
    assert head["kind"] == "headshot" and head["file"] == "assets/headshot/ellie_neutral.png" and head["used_in"] == []
    assert head["names"]["zh-TW"] == "艾莉（頭像・neutral）" and head["spec"]["width"] == 864


def test_taken_ids_are_not_duplicated_and_expressions_follow_the_card():
    cast = {"characters": {"ellie": {"role": "main"}}}
    needs = character_needs(cast, "zh-TW", {"sprite_ellie_neutral"}, {"ellie": ["smile", "surprised", "bogus", "smile"]})
    assert [n["asset_id"] for n in needs] == [headshot_asset_id("ellie", "smile"), headshot_asset_id("ellie", "surprised")]


def test_refresh_manifest_adds_default_rows_and_reads_card_expressions(env):  # noqa: F811
    config, work = env
    ids = [a["asset_id"] for a in art_gen.load_manifest(work)["assets"]]
    assert "headshot_ellie_neutral" in ids and ids.count("sprite_ellie_neutral") == 1
    card_dir = work / "art" / "appearance"
    card_dir.mkdir(parents=True)
    (card_dir / "ellie.json").write_text(json.dumps({"expressions": ["neutral", "angry"]}), encoding="utf-8")
    cast = json.loads((work / "cast.json").read_text(encoding="utf-8"))
    cast["characters"]["ellie"]["role"] = "main"
    (work / "cast.json").write_text(json.dumps(cast), encoding="utf-8")
    art_gen.refresh_manifest(config, work, "bk")
    ids = [a["asset_id"] for a in art_gen.load_manifest(work)["assets"]]
    assert "headshot_ellie_angry" in ids


def test_unused_default_rows_are_not_in_batch_but_can_be_picked_explicitly(env):  # noqa: F811
    config, work = env
    manifest = art_gen.load_manifest(work)
    assert "headshot_ellie_neutral" not in [a["asset_id"] for a in art_gen.pending_assets(manifest)]
    assert [a["asset_id"] for a in art_gen.pending_assets(manifest, only=["headshot_ellie_neutral"])] == ["headshot_ellie_neutral"]


def test_headshot_prompt_uses_headshot_tags_expression_and_size():
    art = {"characters": {"ellie": {"headshot_tags": "long hair, black hair", "tags": "ignored for headshots"}}}
    result = art_prompts.headshot_prompt("ellie", "angry", art)
    assert "long hair, black hair" in result["positive"] and "angry" in result["positive"] and "portrait" in result["positive"]
    assert "ignored" not in result["positive"] and "full body" not in result["positive"].split(".")[0]
    assert "full body" in result["negative"]
    assert (result["width"], result["height"]) == (864, 1152) and result["warnings"] == []
    assert art_prompts.headshot_prompt("nobody", "neutral")["warnings"]
    with pytest.raises(ValueError):
        art_prompts.headshot_prompt("ellie", "bogus")


def test_prompt_for_asset_handles_headshot_and_default_sizes_follow_ratio_rules():
    asset = {"asset_id": "headshot_ellie_smile", "kind": "headshot", "file": "assets/headshot/ellie_smile.png"}
    assert "smile" in art_prompts.prompt_for_asset(asset, {}, None)["positive"]
    defaults = art_prompts.DEFAULT_ART
    ratio = lambda k: defaults[k]["width"] / defaults[k]["height"]  # noqa: E731
    assert abs(ratio("bg") - 16 / 9) < 0.01 and abs(ratio("headshot") - 3 / 4) < 0.01 and ratio("sprite") < 9 / 16
    assert all(defaults[k][d] % 16 == 0 for k in ("bg", "sprite", "headshot") for d in ("width", "height"))


def test_generate_headshot_uses_character_seed_and_headshot_size(env):  # noqa: F811
    config, work = env
    fake = Fake()
    report = art_gen.generate(config, work, fake, candidates=2, only=["headshot_ellie_neutral"])
    assert report.generated == ["headshot_ellie_neutral"] and not report.failed
    assert [c["seed"] for c in fake.calls] == [sprite_consistency.character_seed("ellie", 0), sprite_consistency.character_seed("ellie", 1)]
    assert fake.calls[0]["size"] == (864, 1152)


def test_select_headshot_keeps_the_image_without_cutout(env):  # noqa: F811
    config, work = env
    art_gen.generate(config, work, Fake(), candidates=1, only=["headshot_ellie_neutral"])
    name = art_gen.list_candidates(work, "headshot_ellie_neutral")[0]["name"]
    asset = art_gen.select_candidate(config, work, "headshot_ellie_neutral", name)
    assert asset["status"] == "processed" and asset["file"] == "assets/headshot/ellie_neutral.png"
    image = Image.open(work / asset["file"])
    assert image.size[0] > 0 and image.mode in ("RGB", "RGBA")


def test_expressions_follow_the_character_role():
    cast = {"characters": {"hero": {"role": "protagonist"}, "side": {"role": "main"}, "bad": {"role": "antagonist"},
                           "crew": {"role": "supporting"}, "extra": {"role": "minor"}, "nobody": {}}}
    card = ["neutral", "smile", "angry"]
    needs = character_needs(cast, "zh-TW", None, {k: card for k in cast["characters"]})
    ids = {n["asset_id"] for n in needs}
    for expr in card:   # 主角：立繪與頭像都有各種表情
        assert f"sprite_hero_{expr}" in ids and headshot_asset_id("hero", expr) in ids
    for who in ("side", "bad"):   # 主要配角：頭像各種表情、立繪只有 neutral
        assert {f"sprite_{who}_neutral", headshot_asset_id(who, "smile"), headshot_asset_id(who, "angry")} <= ids
        assert f"sprite_{who}_smile" not in ids
    for who in ("crew", "extra", "nobody"):   # 其餘：各一張 neutral
        assert {i for i in ids if i.endswith(f"_{who}_neutral") or f"_{who}_" in i} == {f"sprite_{who}_neutral", headshot_asset_id(who, "neutral")}
