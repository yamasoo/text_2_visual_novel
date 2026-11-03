import json

import pytest

from t2vn import voice_lines
from t2vn.schema_check import validate_data
from tests.voice.conftest import cast_voice_doc, scene_doc


def test_line_id_is_stable_and_starts_at_one():
    assert voice_lines.line_id("ch001_sc02", 1) == "ch001_sc02_l001"
    assert voice_lines.line_id("ch001_sc02", 12) == "ch001_sc02_l012"
    assert voice_lines.parse_line_id("ch001_sc02_l012") == ("ch001_sc02", 12)
    assert voice_lines.chapter_of("ch001_sc02_l012") == "ch001"


@pytest.mark.parametrize("bad", ["ch1_sc2_l1", "ch001_sc02_l", "ch001_sc02_l1000", "x"])
def test_line_id_rejects_bad_values(bad):
    with pytest.raises(voice_lines.VoiceError):
        voice_lines.parse_line_id(bad)


def test_iter_lines_counts_narration_and_direction():
    doc = scene_doc()
    lines = list(voice_lines.iter_lines(doc))
    assert [line.line_id for line in lines[:3]] == ["ch001_sc01_l001", "ch001_sc01_l002", "ch001_sc01_l003"]
    assert lines[0].type == "narration" and lines[1].speaker == "ellie_reed"
    assert sum(1 for line in lines if line.type == "dialogue") == 10


def test_collect_lines_only_dialogue_by_default(book):
    lines = voice_lines.collect_lines(book.work)
    assert len(lines) == 10
    assert {line.speaker for line in lines} == {"ellie_reed", "lao_zhang"}
    with_narration = voice_lines.collect_lines(book.work, include_narration=True)
    assert len(with_narration) == 12


def test_collect_lines_filters_chapters(book):
    assert voice_lines.collect_lines(book.work, chapters=[2]) == []


def test_voice_paths():
    paths = voice_lines.voice_paths("ch001", "ellie_reed", "ch001_sc02_l003")
    assert paths == {
        "work_file": "assets/voice/ch001/ellie_reed_ch001_sc02_l003.ogg",
        "file": "audio/voice/ch001/ellie_reed_ch001_sc02_l003.ogg",
        "audio": "game/audio/voice/ch001/ellie_reed_ch001_sc02_l003.ogg",
    }


def test_validate_cast_voice_accepts_both_modes(book):
    assert voice_lines.validate_cast_voice(cast_voice_doc(), book.work,
                                           json.loads((book.work / "cast.json").read_text(encoding="utf-8"))) == []


def test_validate_cast_voice_reports_missing_reference(book):
    doc = cast_voice_doc()
    doc["characters"]["ellie_reed"]["reference_audio"] = "refs/nope.wav"
    errors = voice_lines.validate_cast_voice(doc, book.work)
    assert any("not found" in e for e in errors)


def test_validate_cast_voice_reports_missing_prompt(book):
    doc = cast_voice_doc()
    del doc["characters"]["lao_zhang"]["voice_design_prompt"]
    errors = voice_lines.validate_cast_voice(doc, book.work)
    assert any("voice_design_prompt is required" in e for e in errors)


def test_validate_cast_voice_reports_unknown_character(book):
    doc = cast_voice_doc()
    doc["characters"]["nobody"] = {"source": "voice_design_prompt", "voice_design_prompt": "x"}
    errors = voice_lines.validate_cast_voice(doc, book.work, json.loads((book.work / "cast.json").read_text(encoding="utf-8")))
    assert any("not in cast.json" in e for e in errors)


def test_validate_cast_voice_reports_bad_seed(book):
    doc = cast_voice_doc()
    doc["characters"]["ellie_reed"]["seed"] = -1
    assert any("seed" in e for e in voice_lines.validate_cast_voice(doc, book.work))


def test_cast_voice_schema(book):
    assert validate_data(cast_voice_doc(), "cast_voice") == []
    bad = cast_voice_doc()
    bad["characters"]["ellie_reed"]["source"] = "reference_audio"
    del bad["characters"]["ellie_reed"]["reference_audio"]
    assert validate_data(bad, "cast_voice")


def test_config_hash_changes_with_reference_content(book):
    spec = cast_voice_doc()["characters"]["ellie_reed"]
    ref = book.work / "voice" / "refs" / "ellie.wav"
    before = voice_lines.config_hash(spec, "moss-tts-local-1.7b", reference=ref)
    ref.write_bytes(b"different")
    after = voice_lines.config_hash(spec, "moss-tts-local-1.7b", reference=ref)
    assert before != after


def test_config_hash_is_stable(book):
    spec = cast_voice_doc()["characters"]["lao_zhang"]
    first = voice_lines.config_hash(spec, "moss-tts-local-1.7b")
    assert first == voice_lines.config_hash(spec, "moss-tts-local-1.7b")


def test_seeds_are_deterministic_and_line_specific():
    assert voice_lines.character_seed("ellie_reed") == voice_lines.character_seed("ellie_reed")
    base = voice_lines.character_seed("ellie_reed", 4217)
    assert base == 4217
    assert voice_lines.line_seed(base, "ch001_sc01_l002") == voice_lines.line_seed(base, "ch001_sc01_l002")
    assert voice_lines.line_seed(base, "ch001_sc01_l002") != voice_lines.line_seed(base, "ch001_sc01_l003")


def test_engine_for_design_uses_voice_generator():
    assert voice_lines.engine_for({"source": "voice_design_prompt"}) == "moss-voicegenerator-1.7b"
    assert voice_lines.engine_for({"source": "design_then_clone"}) == "moss-tts-local-1.7b"


def test_voice_map_round_trip_and_legacy(book):
    entry = {"line_id": "ch001_sc01_l002", "speaker": "ellie_reed", "chapter": "ch001", "scene_id": "ch001_sc01",
             "text": "你終於來了。", "audio": "game/audio/voice/ch001/ellie_reed_ch001_sc01_l002.ogg",
             "file": "audio/voice/ch001/ellie_reed_ch001_sc01_l002.ogg", "source": "reference_audio",
             "engine": "moss-tts-local-1.7b", "seed": 1, "text_hash": "0" * 16, "config_hash": "1" * 16}
    data = {"schema_version": "0.1", "book_id": "demo", "engine": "moss-tts-local-1.7b",
            "lines": {"ch001_sc01_l002": entry}}
    voice_lines.save_voice_map(book.config, book.work, data)
    loaded = voice_lines.load_voice_map(book.work)
    assert loaded["lines"]["ch001_sc01_l002"]["line_id"] == "ch001_sc01_l002"
    assert loaded["updated"]
    assert validate_data(loaded, "voice_map") == []


def test_reference_path_prefers_voice_folder(book):
    assert voice_lines.reference_path(book.work, "refs/ellie.wav").is_file()
    assert voice_lines.reference_path(book.work, str(book.work / "voice" / "refs" / "ellie.wav")).is_file()
