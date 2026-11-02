import json

import pytest

from t2vn import appearance_draft as m

CHAR = {"name": "Ellie", "summary": "A young surveyor.", "appearance": "silver hair"}
WC = [{"name": "hair_color", "count": 12, "sample": ["black", "silver"]}, {"name": "outfit_survey", "count": 3, "sample": ["coat"]}]
GOOD = {"headshot_prompt": "long hair, __hair_color__, brown eyes, hair ribbon, mole under eye",
        "sprite_prompt": "__outfit_survey__, boots, arms crossed"}


def test_build_prompt_has_two_fields_and_split_rule():
    p = m.build_prompt(CHAR, [], None)
    assert "- headshot_prompt:" in p and "- sprite_prompt:" in p
    assert "neck up" in p and "below the neck" in p
    assert "Do NOT include facial expression" in p
    assert 'Return ONLY one JSON object' in p and '"headshot_prompt": ""' in p and '"sprite_prompt": ""' in p
    assert "hair_style" not in p and "outfit:" not in p  # 舊欄位不能再出現
    assert "Character:" in p and "name: Ellie" in p and "summary: A young surveyor." in p and "appearance: silver hair" in p
    assert "wildcard" not in p.lower()
    assert "Passages from the novel:" not in p and "Current draft" not in p and "no passages" in p


def test_build_prompt_with_wildcards():
    p = m.build_prompt(CHAR, [], WC)
    assert "__hair_color__ (12 options) e.g. black; silver" in p
    assert "__outfit_survey__ (3 options) e.g. coat" in p
    assert "use only these names" in p


def test_build_prompt_with_evidence_and_current():
    current = {"headshot_prompt": "black hair", "sprite_prompt": "coat"}
    p = m.build_prompt({"name": "Ellie"}, ["她有一頭銀髮。", "穿著藍色外套。"], None, current)
    assert "Passages from the novel:\n1. 她有一頭銀髮。\n2. 穿著藍色外套。" in p
    assert "Do not invent" in p and "no passages" not in p
    assert "Current draft (revise it, keep what is still correct):" in p
    assert json.dumps(current, ensure_ascii=False) in p
    assert "summary:" not in p


def test_build_prompt_none_character():
    assert "Character:" in m.build_prompt(None, [], None)


def test_parse_plain_and_fenced_and_chatty():
    raw = json.dumps(GOOD)
    for text in (raw, f"```json\n{raw}\n```", f"Sure! Here it is:\n{raw}\nHope it helps."):
        out, warns = m.parse_reply(text, ["hair_color", "outfit_survey"])
        assert out == GOOD and warns == []


def test_parse_fills_missing_and_coerces():
    out, _ = m.parse_reply('{"headshot_prompt": " bob ", "junk": 1}')
    assert out == {"headshot_prompt": "bob", "sprite_prompt": ""}
    out, _ = m.parse_reply('{"headshot_prompt": null, "sprite_prompt": 5}')
    assert out == {"headshot_prompt": "", "sprite_prompt": "5"}


def test_parse_disallowed_wildcard_removed_with_warning():
    out, warns = m.parse_reply(json.dumps(GOOD), ["hair_color"])
    assert out["sprite_prompt"] == "boots, arms crossed"  # token 與多餘逗號被清掉
    assert out["headshot_prompt"] == GOOD["headshot_prompt"]
    assert warns == ["sprite_prompt: 不在清單內的 wildcard：outfit_survey"]


def test_parse_no_allowed_means_all_removed():
    out, warns = m.parse_reply(json.dumps(GOOD))
    assert "__" not in out["headshot_prompt"] and "__" not in out["sprite_prompt"] and len(warns) == 2


@pytest.mark.parametrize("bad", ["", "no json here", "{broken", "[1, 2]", '{"foo": 1}', '"text"', None])
def test_parse_errors(bad):
    with pytest.raises(ValueError):
        m.parse_reply(bad)


def test_parse_old_format_reply_is_rejected():
    with pytest.raises(ValueError):
        m.parse_reply('{"headshot": {"hair_style": "x"}, "sprite": {"outfit": "y"}}')


def test_prompt_has_fallback_rule_with_and_without_evidence():
    from t2vn import appearance_draft as m
    with_ev = m.build_prompt({"name": "Old Yu"}, ["Yu mended nets."])
    without = m.build_prompt({"name": "Old Yu"}, [])
    for text in (with_ev, without):
        assert m.FALLBACK_LINE in text and "never return both prompts empty" in text
    assert m.NO_EVIDENCE_LINE in without and m.NO_EVIDENCE_LINE not in with_ev
