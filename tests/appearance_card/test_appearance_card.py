import json

import pytest

from t2vn import appearance_card as m


def test_normalize_tags():
    assert m.normalize_tags("  Long_Hair ,\nblue  eyes,, long hair, score_7, Score_7") == "long hair, blue eyes, score_7"
    assert m.normalize_tags(None) == "" and m.normalize_tags(5) == ""


def test_new_card_shape_and_validation():
    card = m.new_card("ellie_reed")
    assert card == {"char_id": "ellie_reed", "confirmed": False, "headshot_prompt": "", "sprite_prompt": "",
                    "expressions": ["neutral"], "notes": ""}
    for bad in ("Ellie", "1a", "", None, "a-b"):
        with pytest.raises(ValueError):
            m.new_card(bad)


def test_normalize_card_cleans_and_does_not_mutate():
    raw = {"char_id": "a", "confirmed": 1, "headshot_prompt": "  black hair ", "sprite_prompt": None, "notes": 5,
           "expressions": ["angry", "bogus", "smile", "angry", 3], "junk": 1}
    before = json.dumps(raw)
    card = m.normalize_card(raw)
    assert json.dumps(raw) == before
    assert card == {"char_id": "a", "confirmed": True, "headshot_prompt": "black hair", "sprite_prompt": "",
                    "expressions": ["smile", "angry"], "notes": "5"}  # 依 EXPRESSIONS 順序、去重、丟掉未知
    assert m.normalize_card({"expressions": []}, "b")["expressions"] == ["neutral"]
    assert m.normalize_card({"expressions": "smile"}, "b")["expressions"] == ["neutral"]
    assert m.normalize_card({"char_id": "x"}, "y")["char_id"] == "y"  # 參數優先
    with pytest.raises(ValueError):
        m.normalize_card([], "a")
    with pytest.raises(ValueError):
        m.normalize_card({}, "Bad")
    with pytest.raises(ValueError):
        m.normalize_card({})


def test_old_format_cards_do_not_crash():
    old = {"char_id": "a", "headshot": {"hair_style": "x"}, "sprite": {"outfit": "y"}}
    card = m.normalize_card(old)
    assert card["headshot_prompt"] == "" and card["sprite_prompt"] == "" and "headshot" not in card


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "art" / "appearance" / "ellie.json"
    m.save_card({"char_id": "ellie", "headshot_prompt": "black hair, 長髮", "expressions": ["sad"]}, path)
    text = path.read_text(encoding="utf-8")
    assert "長髮" in text and text.endswith("}\n") and '  "char_id"' in text
    assert m.load_card(path)["expressions"] == ["sad"]
    with pytest.raises(ValueError):
        m.save_card({"char_id": "other"}, path)
    with pytest.raises(FileNotFoundError):
        m.load_card(tmp_path / "nope.json")


def test_load_card_uses_filename_as_id(tmp_path):
    path = tmp_path / "zhang.json"
    path.write_text(json.dumps({"char_id": "wrong", "notes": "n"}), encoding="utf-8")
    assert m.load_card(path)["char_id"] == "zhang"


def fake_expand(text, seed):
    return text.replace("__hair_color__", "silver hair").replace("__bad__", "BAD"), (["x"] if "__bad__" in text else [])


def test_resolve_card_only_calls_expand_for_wildcard_fields():
    calls = []

    def expand(text, seed):
        calls.append((text, seed))
        return fake_expand(text, seed)

    card = {"char_id": "a", "confirmed": True, "headshot_prompt": "__hair_color__, ribbon", "sprite_prompt": "coat",
            "expressions": ["smile"], "notes": "keep"}
    out, warns = m.resolve_card(card, expand, 7)
    assert calls == [("__hair_color__, ribbon", "7:headshot_prompt")]
    assert out["headshot_prompt"] == "silver hair, ribbon" and out["sprite_prompt"] == "coat"
    assert out["confirmed"] is True and out["notes"] == "keep" and out["expressions"] == ["smile"] and warns == []
    assert card["headshot_prompt"] == "__hair_color__, ribbon"  # 不改原卡


def test_resolve_card_prefixes_warnings():
    _, warns = m.resolve_card({"char_id": "a", "sprite_prompt": "__bad__"}, fake_expand, "s")
    assert warns == ["sprite_prompt: x"]


def test_tags_drop_unresolved_wildcards():
    card = {"char_id": "a", "headshot_prompt": "Black_Hair, __hair_color__, ribbon", "sprite_prompt": "work apron, __outfit__"}
    assert m.headshot_tags(card) == "black hair, ribbon"
    assert m.sprite_tags(card) == "black hair, ribbon, work apron"  # 頭像在前、立繪在後
    assert m.sprite_tags({"char_id": "a"}) == "" and m.headshot_tags({"char_id": "a"}) == ""
    assert m.unresolved_wildcards(card) == ["hair_color", "outfit"]
    assert m.unresolved_wildcards({"char_id": "a", "headshot_prompt": "__x__, __x__"}) == ["x"]


def test_sprite_tags_dedupes_across_the_two_prompts():
    assert m.sprite_tags({"char_id": "a", "headshot_prompt": "black hair", "sprite_prompt": "Black Hair, coat"}) == "black hair, coat"


def test_expression_tags():
    assert set(m.EXPRESSIONS) == set(m.EXPRESSION_TAGS) == {"neutral", "smile", "angry", "sad", "serious", "worried", "surprised"}
    assert m.expression_tags("surprised") == "surprised, wide eyes, open mouth"
    with pytest.raises(ValueError):
        m.expression_tags("bogus")
