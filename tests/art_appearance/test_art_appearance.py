import pytest

from t2vn.art_appearance import CHARACTER_KEYWORDS, LOCATION_KEYWORDS, build_prompt, find_evidence, parse_reply

DOCS = [
    {"chapter_id": "ch001", "paragraphs": [
        {"para_no": 1, "text": "天亮了。"},
        {"para_no": 2, "text": "艾莉穿著沾滿機油的灰色圍裙，短髮用布條綁在腦後，眼睛是深褐色的。"},
        {"para_no": 3, "text": "艾莉說好。"},
        {"para_no": 4, "text": "老張蹲在石墩上抽菸。"},
    ]},
    {"chapter_id": "ch002", "paragraphs": [
        {"para_no": 1, "text": "艾莉戴著手套，臉頰上有一道油漬，" + "很長的句子" * 60 + "。"},
        {"para_no": 2, "text": "別人都走了。"},
    ]},
]


def test_find_evidence_scores_by_keywords_and_returns_reading_order():
    found = find_evidence(DOCS, ["艾莉"], CHARACTER_KEYWORDS, limit=2)
    assert [(e["chapter_id"], e["para_no"]) for e in found] == [("ch001", 2), ("ch002", 1)]  # 描寫多的兩段；不含只有名字的 ch001 ¶3
    assert found[0]["score"] > 3 and "圍裙" in found[0]["text"]
    only_one = find_evidence(DOCS, ["艾莉"], CHARACTER_KEYWORDS, limit=1)
    assert [(e["chapter_id"], e["para_no"]) for e in only_one] == [("ch001", 2)]


def test_find_evidence_requires_a_name_and_handles_aliases():
    assert find_evidence(DOCS, ["不存在"], CHARACTER_KEYWORDS) == []
    assert find_evidence(DOCS, ["", ""], CHARACTER_KEYWORDS) == [] and find_evidence(DOCS, ["艾莉"], CHARACTER_KEYWORDS, limit=0) == []
    found = find_evidence(DOCS, ["老張", "石墩"], LOCATION_KEYWORDS)
    assert [(e["chapter_id"], e["para_no"]) for e in found] == [("ch001", 4)]


def test_find_evidence_tie_prefers_earlier_and_clips_long_text():
    docs = [{"chapter_id": "ch002", "paragraphs": [{"para_no": 1, "text": "艾莉"}]}, {"chapter_id": "ch001", "paragraphs": [{"para_no": 9, "text": "艾莉"}]}]
    assert [(e["chapter_id"], e["para_no"]) for e in find_evidence(docs, ["艾莉"], (), limit=1)] == [("ch001", 9)]
    long = find_evidence(DOCS, ["艾莉"], CHARACTER_KEYWORDS, limit=3, max_chars=60)
    assert all(len(e["text"]) <= 61 for e in long) and any(e["text"].endswith("…") for e in long)
    sentence = find_evidence([{"chapter_id": "ch001", "paragraphs": [{"para_no": 1, "text": "艾莉有一頭短髮，眼睛很亮。她很高大，穿著外套，後面還有很多很多很多很多字。"}]}], ["艾莉"], CHARACTER_KEYWORDS, max_chars=30)
    assert sentence[0]["text"].endswith("。") and len(sentence[0]["text"]) <= 30


def test_build_prompt_character_full():
    ev = find_evidence(DOCS, ["艾莉"], CHARACTER_KEYWORDS, limit=2)
    prompt = build_prompt("character", "艾莉・里德", "  外在：個子小  ", ev, {"tags": "1girl, short hair", "description": "A mechanic."})
    for needle in ("Anima", "Danbooru", '"tags"', '"description"', '"notes"', "JSON", "Subject: 艾莉・里德", "Setting card:\n外在：個子小",
                   "Passages from the novel:", "[ch001 ¶2] 艾莉穿著沾滿機油", "Current draft", "tags: 1girl, short hair", "description: A mechanic."):
        assert needle in prompt, needle
    assert "no people" not in prompt and "personality" in prompt


def test_build_prompt_location_and_optional_sections():
    prompt = build_prompt("location", "舊工坊", "", [], None)
    assert "no people" in prompt and "Setting card" not in prompt and "Passages" not in prompt and "Current draft" not in prompt
    assert "Current draft" not in build_prompt("location", "x", "", [], {"tags": "", "description": ""})
    with pytest.raises(ValueError):
        build_prompt("item", "x", "", [])


def test_parse_reply_variants():
    plain = '{"tags": "1girl, short hair", "description": "A mechanic.", "notes": ""}'
    assert parse_reply(plain) == {"tags": "1girl, short hair", "description": "A mechanic.", "notes": ""}
    fenced = "Here you go:\n```json\n" + plain.replace('"notes": ""', '"notes": "髮色不明", "extra": 1') + "\n```\nDone."
    result = parse_reply(fenced)
    assert result["notes"] == "髮色不明" and set(result) == {"tags", "description", "notes"}
    assert parse_reply('{"tags": " a ", "description": " b "}') == {"tags": "a", "description": "b", "notes": ""}


@pytest.mark.parametrize("bad", ["no json here", "{not json}", "[1, 2]", '{"tags": "a"}', '{"tags": 1, "description": "b"}', '{"tags": "  ", "description": "b"}',
                                 '{"tags": "短髮", "description": "b"}', '{"tags": "a", "description": "她有短髮"}', '{"tags": "a", "description": "b", "notes": 3}'])
def test_parse_reply_rejects_bad_replies(bad):
    with pytest.raises(ValueError):
        parse_reply(bad)
