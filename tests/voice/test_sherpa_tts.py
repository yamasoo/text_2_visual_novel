import json

import pytest

from t2vn import sherpa_tts, voice_gen, voice_lines
from t2vn.schema_check import validate_data


class FakeSherpa:
    def __init__(self):
        self.calls = []

    def wav_bytes(self, text, speaker_id, speed=1.0):
        self.calls.append((text, speaker_id, speed))
        return b"RIFF-fake"


def sherpa_cast() -> dict:
    return {
        "schema_version": "0.1", "book_id": "demo", "engine": "sherpa-onnx",
        "sherpa_model": "models/sherpa/vits-zh-hf-fanchen-C",
        "characters": {
            "ellie_reed": {"source": "speaker_id", "speaker_id": 20, "speed": 1.0},
            "lao_zhang": {"source": "speaker_id", "speaker_id": 100, "speed": 1.05},
        },
    }


def write_cast(book, doc):
    (book.work / "voice" / "cast_voice.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_speaker_presets_loader(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({"demo": [
        {"id": 0, "label": "female"}, {"id": "7-15", "label": "unverified"}]}), encoding="utf-8")
    presets = sherpa_tts.load_speaker_presets(path)
    assert [item["id"] for item in presets["demo"]] == [0, "7-15"]
    assert sherpa_tts.load_speaker_presets(tmp_path / "missing.json") == {}


def test_speaker_catalog_parses_demographics(tmp_path):
    model_dir = tmp_path / "vits-zh-hf-fanchen-C"
    samples = tmp_path / "samples"
    csv_path = samples / model_dir.name / "demographics.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(
        "id,model,gender,gender_confidence,age_years,age_label,file\n"
        "2,vits-zh-hf-fanchen-C,male,0.8,42.0,中年,a.wav\n"
        "0,vits-zh-hf-fanchen-C,female,0.9,21.0,青年,b.wav\n",
        encoding="utf-8",
    )
    catalog = sherpa_tts.speaker_catalog(model_dir, samples_root=samples)
    assert catalog["available"] and len(catalog["speakers"]) == 2
    assert [item["id"] for item in catalog["speakers"]] == [0, 2]
    assert catalog["speakers"][0]["gender_label"] == "女"


def test_sherpa_cast_schema_and_validation(book):
    doc = sherpa_cast()
    assert validate_data(doc, "cast_voice") == []
    assert voice_lines.validate_cast_voice(doc, book.work, json.loads((book.work / "cast.json").read_text(encoding="utf-8"))) == []
    bad = sherpa_cast()
    del bad["characters"]["ellie_reed"]["speaker_id"]
    assert validate_data(bad, "cast_voice")
    assert any("speaker_id" in e for e in voice_lines.validate_cast_voice(bad, book.work))


def test_sherpa_generate_all_lines(book):
    write_cast(book, sherpa_cast())
    fake = FakeSherpa()
    report = voice_gen.generate(book.config, book.work, book.backend, sherpa=fake, runner=book.runner)
    assert report.generated and len(report.generated) == 10 and report.failed == {}
    assert len(fake.calls) == 10
    assert {c[1] for c in fake.calls} == {20, 100}
    assert book.backend.calls == []
    voice_map = voice_lines.load_voice_map(book.work)
    assert validate_data(voice_map, "voice_map") == []
    entries = voice_map["lines"]
    assert all(e["engine"] == "sherpa-onnx" and e["source"] == "speaker_id" for e in entries.values())
    assert {e["speaker"]: e["seed"] for e in entries.values()} == {"ellie_reed": 20, "lao_zhang": 100}


def test_sherpa_generate_skips_matching_entries(book):
    write_cast(book, sherpa_cast())
    fake = FakeSherpa()
    voice_gen.generate(book.config, book.work, book.backend, sherpa=fake, runner=book.runner)
    calls = len(fake.calls)
    report = voice_gen.generate(book.config, book.work, book.backend, sherpa=fake, runner=book.runner)
    assert report.generated == [] and len(report.skipped) == 10
    assert len(fake.calls) == calls


def test_sherpa_model_change_regenerates(book):
    write_cast(book, sherpa_cast())
    fake = FakeSherpa()
    voice_gen.generate(book.config, book.work, book.backend, sherpa=fake, runner=book.runner)
    doc = sherpa_cast()
    doc["sherpa_model"] = "models/sherpa/another-model"
    write_cast(book, doc)
    report = voice_gen.generate(book.config, book.work, book.backend, sherpa=fake, runner=book.runner)
    assert len(report.generated) == 10 and report.skipped == []


def test_sherpa_speed_change_regenerates(book):
    write_cast(book, sherpa_cast())
    fake = FakeSherpa()
    voice_gen.generate(book.config, book.work, book.backend, sherpa=fake, runner=book.runner)
    doc = sherpa_cast()
    doc["characters"]["ellie_reed"]["speed"] = 1.2
    write_cast(book, doc)
    report = voice_gen.generate(book.config, book.work, book.backend, sherpa=fake, runner=book.runner)
    assert report.generated and all(line.startswith("ch001_sc01") for line in report.generated)
    assert len(report.skipped) == 5


def test_sherpa_missing_model_is_reported_per_line(book):
    write_cast(book, {**sherpa_cast(), "sherpa_model": "models/sherpa/nope"})
    report = voice_gen.generate(book.config, book.work, book.backend, runner=book.runner)
    assert len(report.failed) == 10 and report.generated == []
