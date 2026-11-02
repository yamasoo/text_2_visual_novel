"""標註漏敘述的偵測與補回（QA #1：整句動作敘述被丟掉）。"""
from __future__ import annotations

from t2vn.annotate import expand, omitted_clauses, paragraph_omissions, repair_omissions

SOURCE = "「石頭輕了。」艾莉從工作檯抽出鐵盒，打開來，她把紙條攤平。「你看。」"


def _chapter(text=SOURCE):
    return {"chapter_id": "ch001", "paragraphs": [{"para_no": 1, "text": text}]}


def _cast():
    return {"characters": {"ellie": {}}}


def test_speech_tags_are_not_omissions():
    assert omitted_clauses("「好。」她說", ["好。"]) == []
    assert omitted_clauses("「好。」艾莉問", ["好。"]) == []


def test_dropped_narration_is_detected():
    lines = [{"type": "dialogue", "text": "石頭輕了。", "source_para": 1},
             {"type": "dialogue", "text": "你看。", "source_para": 1}]
    missing = paragraph_omissions(lines, {1: SOURCE})
    assert list(missing) == [1]
    assert "艾莉從工作檯抽出鐵盒" in missing[1]


def test_complete_paragraph_has_no_omission():
    lines = [{"type": "dialogue", "text": "石頭輕了。", "source_para": 1},
             {"type": "narration", "text": "艾莉從工作檯抽出鐵盒，打開來，她把紙條攤平。", "source_para": 1},
             {"type": "dialogue", "text": "你看。", "source_para": 1}]
    assert paragraph_omissions(lines, {1: SOURCE}) == {}


def test_repair_inserts_in_source_order():
    doc = {"scenes": [{"lines": [
        {"type": "dialogue", "speaker": "ellie", "text": "石頭輕了。", "source_para": 1},
        {"type": "dialogue", "speaker": "ellie", "text": "你看。", "source_para": 1}]}]}
    added = repair_omissions(doc, {1: SOURCE})
    assert added >= 1
    texts = [ln["text"] for ln in doc["scenes"][0]["lines"]]
    assert texts[0] == "石頭輕了。" and texts[-1] == "你看。"
    assert "艾莉從工作檯抽出鐵盒" in "".join(texts[1:-1])
    assert doc["scenes"][0]["lines"][1]["type"] == "narration"
    assert paragraph_omissions(doc["scenes"][0]["lines"], {1: SOURCE}) == {}


def test_repair_is_idempotent_and_noop_when_complete():
    doc = {"scenes": [{"lines": [{"type": "narration", "text": "今天天氣很好。", "source_para": 1}]}]}
    assert repair_omissions(doc, {1: "今天天氣很好。"}) == 0
    assert repair_omissions(doc, {1: "今天天氣很好。"}) == 0


def test_repair_no_trailing_comma():
    doc = {"scenes": [{"lines": [{"type": "dialogue", "speaker": "ellie", "text": "好。", "source_para": 1}]}]}
    repair_omissions(doc, {1: "她打開盒子，拿出紙條，「好。」"})
    narr = [ln["text"] for ln in doc["scenes"][0]["lines"] if ln["type"] == "narration"]
    assert narr and not narr[0].endswith("，")


def test_expand_reports_dropped_narration():
    ops = {"scenes": [{"title": "t", "bg": "bg_x", "ops": [
        {"k": "d", "p": 1, "s": "ellie", "t": "石頭輕了。"},
        {"k": "d", "p": 1, "s": "ellie", "t": "你看。"}]}]}
    result = expand(ops, _chapter(), _cast(), "b", "zh")
    assert any("narration was dropped" in i for i in result.issues)


def test_tags_with_negation_or_action_are_not_pure_speech_tags():
    for tag in ("老張問", "最後說", "工頭問"):
        assert omitted_clauses(tag, []) == []
    for tag in ("老漁民沒有馬上回答", "最後只說了一句", "艾莉打斷他"):
        assert omitted_clauses(tag, []) == [tag]
