import json
from pathlib import Path

from t2vn.schema_check import check_scene_links, guess_kind, validate_data, validate_file

CAST = {"schema_version": "0.1", "book_id": "sample_book", "characters": {
    "ellie_reed": {"names": {"zh-TW": "x"}, "aliases": [], "role": "protagonist"},
    "bad_guy": {"names": {"zh-TW": "y"}, "aliases": [], "role": "antagonist"}}}


def test_cast_ok():
    assert validate_data(CAST, "cast") == []


def test_cast_bad_role_and_key():
    data = json.loads(json.dumps(CAST))
    data["characters"]["ellie_reed"]["role"] = "hero"
    data["characters"]["Bad"] = data["characters"]["bad_guy"]
    paths = {i.path for i in validate_data(data, "cast")}
    assert "/characters/ellie_reed/role" in paths


def test_guess_kind():
    assert guess_kind("a/scenes/ch001_sc01.json") == "scene"
    assert guess_kind("a/cast.json") == "cast"
    assert guess_kind("a/chapters/ch001.json") == "chapter"
    assert guess_kind("a/chapters/index.json") == "chapter_index"
    assert guess_kind("a/other.json") is None


def test_validate_file_invalid_json(tmp_path):
    p = tmp_path / "cast.json"
    p.write_text("{", encoding="utf-8")
    assert validate_file(p)[0].message.startswith("invalid JSON")


def scene(sid, nxt=None, lines=None, cast=None):
    return {"scene_id": sid, "next": nxt, "cast": cast or [], "lines": lines or []}


def test_links():
    doc = {"chapter_id": "ch001", "scenes": [
        scene("ch001_sc01", "ch001_sc02", [{"type": "dialogue", "speaker": "player"}, {"type": "dialogue", "speaker": "ghost"}]),
        scene("ch001_sc02", "ch002_sc01"),
        scene("ch001_sc03", "ch001_sc99"),
        scene("ch001_sc03", "end_a"),
    ]}
    msgs = [(i.path, i.message) for i in check_scene_links(doc, CAST)]
    assert any("ghost" in m for _, m in msgs)
    assert any("ch001_sc99" in m for _, m in msgs)
    assert any("duplicate" in m for _, m in msgs)
    assert not any("player" in m for _, m in msgs)
    assert not any("ch002" in m for _, m in msgs)


def test_links_without_cast_skips_character_check():
    doc = {"chapter_id": "ch001", "scenes": [scene("ch001_sc01", None, [{"type": "dialogue", "speaker": "ghost"}])]}
    assert check_scene_links(doc) == []
