"""端到端煙霧測試：用完全自造的迷你書跑「標註 → 展開」。

這組測試不讀 `works/`，所以 clone 後仍會執行；真正的人工黃金集測試見
`tests/annotate/test_annotate.py`（需要作者本機的 `works/sample_book`，沒有就整組跳過）。
"""
from __future__ import annotations

from t2vn.annotate import ChapterContext, annotate_chapter, expand
from t2vn.llm import ReplayLLM

CHAPTER = {
    "schema_version": "0.1",
    "book_id": "synth_book",
    "chapter_id": "ch001",
    "chapter_no": 1,
    "title": "倉庫",
    "paragraphs": [
        {"para_no": 1, "text": "倉庫的門在風裡作響。"},
        {"para_no": 2, "text": "他把燈掛上鐵鉤，回頭看了一眼。"},
        {"para_no": 3, "text": "「東西都搬完了？」她問。"},
        {"para_no": 4, "text": "他點點頭，把清單折好收進口袋。"},
    ],
}

CAST = {
    "schema_version": "0.1",
    "book_id": "synth_book",
    "characters": {
        "lin": {"names": {"zh-TW": "林"}, "aliases": [], "role": "protagonist", "hue": 200},
        "yu": {"names": {"zh-TW": "于"}, "aliases": [], "role": "supporting", "hue": 20},
    },
}

OPS = {
    "chapter_id": "ch001",
    "scenes": [
        {
            "title": "倉庫",
            "location_id": "warehouse",
            "time_of_day": "night",
            "bgm": None,
            "cast": [
                {"id": "lin", "e": "neutral", "pos": "left"},
                {"id": "yu", "e": "neutral", "pos": "right"},
            ],
            "ops": [
                {"k": "N", "p": 1},
                {"k": "N", "p": 2},
                {"k": "d", "p": 3, "s": "yu", "e": "neutral", "t": "東西都搬完了？"},
                {"k": "n", "p": 3, "t": "她問。"},
                {"k": "N", "p": 4},
            ],
        }
    ],
}


def context() -> ChapterContext:
    return ChapterContext(CHAPTER, CAST, {"plot_points": "1. 搬貨"}, {}, ["warehouse"], [], None)


def test_expand_accepts_a_self_made_chapter():
    result = expand(OPS, CHAPTER, CAST, "synth_book", "zh-TW")
    assert result.issues == []
    scene = result.scene_doc["scenes"][0]
    assert scene["bg"] == {"asset_id": "bg_warehouse_night"}
    assert [ln["type"] for ln in scene["lines"]] == [
        "narration", "narration", "dialogue", "narration", "narration"]
    assert scene["lines"][2]["speaker"] == "yu"


def test_annotate_round_trip_without_any_book_data():
    result = annotate_chapter(context(), ReplayLLM([OPS]), "synth_book", "zh-TW")
    assert result.rounds == 1
    assert result.added_characters == []
    scene = result.scene_doc["scenes"][0]
    assert [c["char_id"] for c in scene["cast"]] == ["lin", "yu"]
    assert result.scene_doc["book_id"] == "synth_book"
