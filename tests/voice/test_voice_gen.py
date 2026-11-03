import json
from pathlib import Path

import pytest

from t2vn import voice_gen, voice_lines
from t2vn.schema_check import validate_data
from tests.voice.conftest import FakeBackend


def load_scene(book):
    return json.loads((book.work / "scenes" / "ch001.json").read_text(encoding="utf-8"))


def save_scene(book, doc):
    (book.work / "scenes" / "ch001.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def run(book, **kwargs):
    kwargs.setdefault("runner", book.runner)
    return voice_gen.generate(book.config, book.work, book.backend, **kwargs)


def test_generate_ten_lines_and_voice_map(book):
    report = run(book)
    assert report.failed == {} and report.stopped is False
    assert len(report.generated) == 10 and report.designed == ["lao_zhang"]
    assert len(report.skipped) == 0
    voice_map = voice_lines.load_voice_map(book.work)
    assert len(voice_map["lines"]) == 10
    assert validate_data(voice_map, "voice_map") == []
    for line_id, entry in voice_map["lines"].items():
        assert entry["audio"].startswith("game/audio/voice/ch001/")
        assert entry["file"].startswith("audio/voice/ch001/")
        assert entry["seed"] >= 0 and entry["duration"] == 1.25
        assert (book.work / entry["work_file"]).is_file()
        assert entry["speaker"] in ("ellie_reed", "lao_zhang")
    assert (book.work / "voice" / "refs" / "lao_zhang_design.flac").is_file()
    assert (book.work / "assets" / "voice" / "ch001" / "ellie_reed_ch001_sc01_l002.ogg").is_file()
    # 同一角色的克隆句都上傳同一個參考音
    ellie_uploads = [name for name in book.backend.uploads if "ellie_reed" in name]
    zhang_uploads = [name for name in book.backend.uploads if "lao_zhang" in name]
    assert len(set(ellie_uploads)) == 1 and len(set(zhang_uploads)) == 1


def test_second_run_skips_everything(book):
    run(book)
    calls = len(book.backend.calls)
    report = run(book)
    assert report.generated == [] and len(report.skipped) == 10
    assert len(book.backend.calls) == calls  # 連設計都沒有重跑


def test_text_change_regenerates_only_that_line(book):
    run(book)
    doc = load_scene(book)
    doc["scenes"][0]["lines"][1]["text"] = "你終於來了嗎？"
    save_scene(book, doc)
    report = run(book)
    assert report.generated == ["ch001_sc01_l002"]
    assert len(report.skipped) == 9
    assert voice_lines.load_voice_map(book.work)["lines"]["ch001_sc01_l002"]["text"] == "你終於來了嗎？"


def test_force_regenerates_with_same_seed(book):
    run(book)
    before = voice_lines.load_voice_map(book.work)["lines"]
    report = run(book, regenerate=True)
    after = voice_lines.load_voice_map(book.work)["lines"]
    assert len(report.generated) == 10
    assert {k: v["seed"] for k, v in before.items()} == {k: v["seed"] for k, v in after.items()}


def test_reference_content_change_regenerates_character(book):
    run(book)
    (book.work / "voice" / "refs" / "ellie.wav").write_bytes(b"RIFF-new-wav")
    report = run(book)
    assert sorted(report.generated) == ["ch001_sc01_l002", "ch001_sc01_l003", "ch001_sc01_l005",
                                        "ch001_sc01_l006", "ch001_sc01_l007"]
    assert len(report.skipped) == 5


def test_failure_isolation(book):
    book.backend.fail_on = "今天的風"
    report = run(book)
    assert list(report.failed) == ["ch001_sc01_l005"]
    assert len(report.generated) == 9
    assert "ch001_sc01_l005" not in voice_lines.load_voice_map(book.work)["lines"]


def test_voice_design_prompt_generates_without_clone(book):
    doc = json.loads((book.work / "voice" / "cast_voice.json").read_text(encoding="utf-8"))
    doc["characters"]["lao_zhang"] = {"source": "voice_design_prompt",
                                     "voice_design_prompt": "低沉、緩慢的中年男性。", "seed": 9173,
                                     "max_new_tokens": 256}
    (book.work / "voice" / "cast_voice.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    report = run(book)
    assert len(report.generated) == 10 and report.failed == {}
    entries = voice_lines.load_voice_map(book.work)["lines"]
    lao = [e for e in entries.values() if e["speaker"] == "lao_zhang"]
    assert lao and all(e["source"] == "voice_design_prompt" and e["engine"] == "moss-voicegenerator-1.7b" for e in lao)
    design_calls = [wf for wf in book.backend.calls
                    if any(n.get("class_type") == "MossTTSVoiceDesign" for n in wf.values())]
    assert len(design_calls) == 5
    assert not (book.work / "voice" / "refs" / "lao_zhang_design.flac").exists()


def test_design_prompt_change_regenerates_design_and_lines(book):
    run(book)
    doc = json.loads((book.work / "voice" / "cast_voice.json").read_text(encoding="utf-8"))
    doc["characters"]["lao_zhang"]["voice_design_prompt"] = "更低沉、更慢的男性。"
    (book.work / "voice" / "cast_voice.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    report = run(book)
    assert report.designed == ["lao_zhang"]
    assert sorted(report.generated) == [f"ch001_sc02_l{i:03d}" for i in range(1, 6)]
    assert len(report.skipped) == 5


def test_design_failure_marks_character(book):
    book.backend.fail_on = "這是一段固定角色音色的參考錄音"
    report = run(book)
    assert "lao_zhang" in report.failed
    assert len(report.generated) == 5
    assert all(line_id.startswith("ch001_sc02") for line_id in report.failed if line_id.startswith("ch001"))


def test_only_filter(book):
    report = run(book, only=["ch001_sc02_l003"])
    assert report.generated == ["ch001_sc02_l003"]
    assert len(voice_lines.load_voice_map(book.work)["lines"]) == 1


def test_design_only(book):
    report = run(book, design_only=True)
    assert report.designed == ["lao_zhang"]
    assert report.generated == []
    assert (book.work / "voice" / "refs" / "lao_zhang_design.flac").is_file()
    assert not (book.work / "assets" / "voice").exists()


def test_missing_voice_config_is_skipped(book):
    doc = json.loads((book.work / "voice" / "cast_voice.json").read_text(encoding="utf-8"))
    del doc["characters"]["lao_zhang"]
    (book.work / "voice" / "cast_voice.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    report = run(book)
    assert len(report.generated) == 5 and report.unvoiced == ["lao_zhang"]
    assert len(report.skipped) == 5


def test_invalid_cast_voice_is_rejected(book):
    doc = json.loads((book.work / "voice" / "cast_voice.json").read_text(encoding="utf-8"))
    doc["characters"]["ellie_reed"]["reference_audio"] = "refs/nope.wav"
    (book.work / "voice" / "cast_voice.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(voice_gen.VoiceGenError):
        run(book)


def test_stop_keeps_finished_lines(book):
    seen = {"n": 0}

    def stop():
        seen["n"] += 1
        return seen["n"] > 3

    report = run(book, should_stop=stop)
    assert report.stopped
    assert len(voice_lines.load_voice_map(book.work)["lines"]) >= 1


def test_overview(book):
    run(book)
    view = voice_gen.overview(book.work)
    assert view["ready"] and view["totals"] == {"lines": 10, "generated": 10, "stale": 0, "pending": 0, "missing": 0}
    assert view["errors"] == []
    assert {c["char_id"]: c["generated"] for c in view["characters"]} == {"lao_zhang": 5, "ellie_reed": 5}


def test_overview_marks_changed_text_stale(book):
    run(book)
    doc = load_scene(book)
    doc["scenes"][0]["lines"][1]["text"] = "改過的台詞。"
    save_scene(book, doc)
    view = voice_gen.overview(book.work)
    assert view["totals"]["generated"] == 9 and view["totals"]["stale"] == 1
    assert next(x for x in view["lines"] if x["line_id"] == "ch001_sc01_l002")["status"] == "stale"


def test_overview_marks_mode_change_stale(book):
    run(book)
    doc = json.loads((book.work / "voice" / "cast_voice.json").read_text(encoding="utf-8"))
    doc["characters"]["lao_zhang"] = {"source": "voice_design_prompt",
                                     "voice_design_prompt": "低沉、緩慢的中年男性。", "seed": 9173}
    (book.work / "voice" / "cast_voice.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    view = voice_gen.overview(book.work)
    assert view["totals"]["stale"] == 5
    assert all(x["status"] == "stale" for x in view["lines"] if x["speaker"] == "lao_zhang")


def test_overview_without_cast_voice(book):
    (book.work / "voice" / "cast_voice.json").unlink()
    view = voice_gen.overview(book.work)
    assert view["ready"] is False and view["characters"] == []


def test_audio_file_guard(book):
    run(book)
    rel = "assets/voice/ch001/ellie_reed_ch001_sc01_l002.ogg"
    assert voice_gen.audio_file(book.work, rel).is_file()
    game_rel = "game/audio/voice/ch001/ellie_reed_ch001_sc01_l002.ogg"
    assert voice_gen.audio_file(book.work, game_rel).is_file()
    with pytest.raises(voice_gen.VoiceGenError):
        voice_gen.audio_file(book.work, "../scenes/ch001.json")
    with pytest.raises(voice_gen.VoiceGenError):
        voice_gen.audio_file(book.work, "assets/voice/ch001/missing.ogg")
