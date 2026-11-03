import json
from pathlib import Path

from t2vn import voice_lines
from t2vn.config import parse_config
from t2vn.export_renpy import ChapterInput, export_book, render_export

from tests.export_renpy.test_export_renpy import CAST, STYLE, doc, scene


def chapter(lines):
    return [ChapterInput("ch001", 1, "補囊", doc(scene("ch001_sc01", lines=lines, next=None)))]


DIALOGUE = [
    {"type": "narration", "text": "天亮了。", "source_para": 1},
    {"type": "dialogue", "speaker": "lao_zhang", "text": "走吧。", "source_para": 2},
]


def test_voice_statement_is_inserted_before_dialogue():
    out = render_export(
        book_id="bk", title="書", lang="zh-TW", chapters=chapter(DIALOGUE), cast_doc=CAST, style=STYLE,
        exists=lambda rel: False, audio_file=lambda kind, aid: None,
        voice_file=lambda line_id: "audio/voice/ch001/lao_zhang_ch001_sc01_l002.ogg" if line_id == "ch001_sc01_l002" else None)
    text = out.files["vn_ch001.rpy"]
    assert '    voice "audio/voice/ch001/lao_zhang_ch001_sc01_l002.ogg"\n    c_lao_zhang "走吧。"' in text
    assert out.manifest["voices"] == [{
        "line_id": "ch001_sc01_l002", "speaker": "lao_zhang",
        "file": "audio/voice/ch001/lao_zhang_ch001_sc01_l002.ogg", "present": True}]


def test_no_voice_callback_keeps_output_unchanged():
    out = render_export(book_id="bk", title="書", lang="zh-TW", chapters=chapter(DIALOGUE), cast_doc=CAST, style=STYLE,
                        exists=lambda rel: False, audio_file=lambda kind, aid: None)
    assert "voice " not in out.files["vn_ch001.rpy"]
    assert out.manifest["voices"] == []


def test_narration_never_gets_voice():
    calls = []

    def voice_file(line_id):
        calls.append(line_id)
        return "audio/voice/x.ogg"

    out = render_export(book_id="bk", title="書", lang="zh-TW", chapters=chapter(DIALOGUE), cast_doc=CAST, style=STYLE,
                        exists=lambda rel: False, audio_file=lambda kind, aid: None, voice_file=voice_file)
    assert calls == ["ch001_sc01_l002"]
    assert out.files["vn_ch001.rpy"].count("voice ") == 1


def make_book(tmp_path):
    project = tmp_path / "proj"
    library = tmp_path / "lib"
    (library / "b").mkdir(parents=True)
    project.mkdir()
    config = parse_config({
        "project_root": str(project), "book_library": {"path": str(library), "access": "read_only"},
        "books": [{"book_id": "demo", "source_dir": "b", "lang": "zh-TW", "display_name": {"zh-TW": "示範"}}]})
    work = config.work_dir("demo")
    (work / "scenes").mkdir(parents=True)
    (work / "voice").mkdir(parents=True)
    (work / "assets" / "voice" / "ch001").mkdir(parents=True)
    (work / "book.json").write_text(json.dumps({"schema_version": "0.1", "book_id": "demo", "lang": "zh-TW",
                                                "display_name": {"zh-TW": "示範"}}), encoding="utf-8")
    (work / "cast.json").write_text(json.dumps({"schema_version": "0.1", "book_id": "demo", "characters": {
        "lao_zhang": {"names": {"zh-TW": "老張"}, "role": "supporting"}}}), encoding="utf-8")
    (work / "scenes" / "ch001.json").write_text(json.dumps(doc(scene("ch001_sc01", lines=DIALOGUE, next=None)),
                                                           ensure_ascii=False), encoding="utf-8")
    voice = b"OggS-voice"
    (work / "assets" / "voice" / "ch001" / "lao_zhang_ch001_sc01_l002.ogg").write_bytes(voice)
    (work / "assets" / "voice" / "ch001" / "orphan.ogg").write_bytes(b"OggS-orphan")
    voice_lines.save_voice_map(config, work, {
        "schema_version": "0.1", "book_id": "demo", "engine": "moss-tts-local-1.7b",
        "lines": {"ch001_sc01_l002": {
            "line_id": "ch001_sc01_l002", "speaker": "lao_zhang", "chapter": "ch001", "scene_id": "ch001_sc01",
            "text": "走吧。", "audio": "game/audio/voice/ch001/lao_zhang_ch001_sc01_l002.ogg",
            "file": "audio/voice/ch001/lao_zhang_ch001_sc01_l002.ogg",
            "work_file": "assets/voice/ch001/lao_zhang_ch001_sc01_l002.ogg",
            "source": "reference_audio", "engine": "moss-tts-local-1.7b", "seed": 1,
            "text_hash": "0" * 16, "config_hash": "1" * 16}}})
    return config, work


def test_export_book_copies_only_referenced_voice(tmp_path):
    config, work = make_book(tmp_path)
    renpy = tmp_path / "rp"
    (renpy / "game").mkdir(parents=True)
    export_book(config, "demo", [1], renpy)
    game = renpy / "game"
    assert (game / "audio/voice/ch001/lao_zhang_ch001_sc01_l002.ogg").read_bytes() == b"OggS-voice"
    assert not (game / "audio/voice/ch001/orphan.ogg").exists()
    text = (game / "vn_ch001.rpy").read_text(encoding="utf-8")
    assert 'voice "audio/voice/ch001/lao_zhang_ch001_sc01_l002.ogg"' in text
    manifest = json.loads((game / "vn_export.json").read_text(encoding="utf-8"))
    assert manifest["voices"][0]["present"] is True


def test_export_book_voice_false_skips_all_voice(tmp_path):
    config, work = make_book(tmp_path)
    renpy = tmp_path / "rp"
    (renpy / "game").mkdir(parents=True)
    export_book(config, "demo", [1], renpy, voice=False)
    game = renpy / "game"
    assert not (game / "audio/voice").exists()
    assert "voice " not in (game / "vn_ch001.rpy").read_text(encoding="utf-8")
    assert json.loads((game / "vn_export.json").read_text(encoding="utf-8"))["voices"] == []
