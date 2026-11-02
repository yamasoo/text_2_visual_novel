import json

import pytest

from t2vn import audio_prompts as m

GOOD = json.dumps({
    "style": "Cinematic ambient underscore, 70 bpm, warm piano and soft strings, calm mood, pure instrumental, no vocals",
    "lyrics": "[Intro]\n(Soft piano)\n\n[Verse 1]\n(Gentle rhythm)\n\n[Outro]\n(Fade out)",
})
SCENES = [{"scene_id": "ch002_sc01", "title": "借磅秤", "location": "scrapyard", "time_of_day": "dawn", "summary": "Ellie borrows a scale."}]


def test_build_prompt_contains_rules_track_and_scenes():
    text = m.build_prompt("bgm", {"id": "bgm_workshop_daily", "label": "工坊日常", "note": "", "prompt": "warm"}, SCENES)
    assert "YuE2" in text and '{"style": "", "lyrics": ""}' in text
    assert "pure instrumental, no vocals" in text and "Never write words to be sung" in text
    assert "id: bgm_workshop_daily" in text and "label: 工坊日常" in text and "note:" not in text and "prompt: warm" in text
    assert "- ch002_sc01 | 借磅秤 | place: scrapyard | time: dawn | Ellie borrows a scale." in text
    assert "Current draft" not in text


def test_build_prompt_kind_scene_limit_and_current():
    many = [dict(SCENES[0], scene_id=f"ch002_sc{i:02d}") for i in range(10)]
    text = m.build_prompt("ambience", {"id": "amb_scrapyard"}, many, {"style": "x", "lyrics": "y"})
    assert "no melody and no drums" in text
    assert text.count("- ch002_sc") == m.MAX_SCENES
    assert "Current draft" in text and '"style": "x"' in text
    empty = m.build_prompt("bgm", None, None)
    assert "No scene information is available" in empty
    with pytest.raises(ValueError):
        m.build_prompt("sfx", None, None)


def test_parse_reply_ok():
    fields, warns = m.parse_reply(GOOD, "bgm")
    assert warns == []
    assert fields["style"].endswith("pure instrumental, no vocals")
    assert fields["lyrics"] == "[Intro]\n(Soft piano)\n\n[Verse 1]\n(Gentle rhythm)\n\n[Outro]\n(Fade out)"


def test_parse_reply_fence_and_chatter():
    fields, _ = m.parse_reply("Sure!\n```json\n" + GOOD + "\n```\nDone", "bgm")
    assert fields["lyrics"].startswith("[Intro]")
    fields, _ = m.parse_reply("here you go " + GOOD + " bye", "bgm")
    assert fields["style"].startswith("Cinematic")


def test_style_gets_instrumental_and_ambience_tags():
    fields, _ = m.parse_reply(json.dumps({"style": "  soft   piano , calm,  ", "lyrics": "[Intro]\n(x)"}), "bgm")
    assert fields["style"] == "soft piano , calm, pure instrumental, no vocals"
    assert fields["style"].count("no vocals") == 1
    fields, _ = m.parse_reply(json.dumps({"style": "low drone, water", "lyrics": "[Intro]\n(x)"}), "ambience")
    assert "pure instrumental, no vocals" in fields["style"] and "ambient soundscape" in fields["style"]
    fields, _ = m.parse_reply(json.dumps({"style": "ambient water bed, no vocals", "lyrics": "[Intro]\n(x)"}), "ambience")
    assert "seamless loop" not in fields["style"]


def test_style_long_and_non_english():
    long_style = ", ".join(f"tag{i}" for i in range(300))
    fields, warns = m.parse_reply(json.dumps({"style": long_style, "lyrics": "[Intro]\n(x)"}), "bgm")
    assert len(fields["style"]) <= m.MAX_STYLE_CHARS + len(m.INSTRUMENTAL) + 2
    assert fields["style"].endswith("pure instrumental, no vocals") and any("截短" in w for w in warns)
    _, warns = m.parse_reply(json.dumps({"style": "溫暖的鋼琴, no vocals", "lyrics": "[Intro]\n(x)"}), "bgm")
    assert any("非英文" in w for w in warns)


def test_lyrics_sung_lines_removed():
    lyrics = "[Verse 1]\nLa la la sing along\n(Soft piano)\n\n\n\n[Chorus]\n你好\n(Strings join)\n"
    fields, warns = m.parse_reply(json.dumps({"style": "calm, no vocals", "lyrics": lyrics}), "bgm")
    assert fields["lyrics"] == "[Verse 1]\n(Soft piano)\n\n[Chorus]\n(Strings join)"
    assert any("2 行" in w for w in warns)


def test_lyrics_missing_or_without_tags_uses_default():
    for bad in ("", None, "just words", "(only a note)"):
        fields, warns = m.parse_reply(json.dumps({"style": "calm, no vocals", "lyrics": bad}), "ambience")
        assert fields["lyrics"] == m.default_lyrics("ambience")
        assert any("預設段落" in w for w in warns)
    assert m.default_lyrics("bgm").startswith("[Intro]") and m.default_lyrics("bgm") != m.default_lyrics("ambience")


@pytest.mark.parametrize("text", ["", "   ", "no json here", "{bad json}", "[1, 2]", '{"lyrics": "[Intro]\\n(x)"}', '{"style": "  "}'])
def test_parse_reply_errors(text):
    with pytest.raises(ValueError):
        m.parse_reply(text, "bgm")


def test_parse_reply_non_string_and_bad_kind():
    with pytest.raises(ValueError):
        m.parse_reply(None, "bgm")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        m.parse_reply(GOOD, "voice")
    with pytest.raises(ValueError):
        m.default_lyrics("voice")
