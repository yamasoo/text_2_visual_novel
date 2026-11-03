import copy
import json
from pathlib import Path

import pytest

from t2vn import voice_workflow

REPO = Path(__file__).resolve().parents[2]


def template(kind: str) -> dict:
    name = "moss_tts_clone_api.json" if kind == "clone" else "moss_tts_design_api.json"
    return json.loads((REPO / "workflows" / name).read_text(encoding="utf-8"))


def test_clone_template_is_api_format():
    wf = template("clone")
    assert {node["class_type"] for node in wf.values()} == {
        "MossTTSModelLoader", "LoadAudio", "MossTTSGenerate", "SaveAudioAdvanced"}


def test_fill_clone_sets_text_seed_audio_and_reference():
    wf = voice_workflow.fill_clone(
        template("clone"), text="你終於來了。", seed=4217, reference_audio="book_ellie.wav",
        filename_prefix="voice/ch001/ch001_sc01_l002")
    assert wf["3"]["inputs"]["text"] == "你終於來了。"
    assert wf["3"]["inputs"]["seed"] == 4217
    assert wf["3"]["inputs"]["reference_audio"] == ["2", 0]
    assert wf["2"]["inputs"]["audio"] == "book_ellie.wav"
    assert wf["4"]["inputs"]["audio"] == ["3", 0]
    assert wf["4"]["inputs"]["filename_prefix"] == "voice/ch001/ch001_sc01_l002"
    assert wf["4"]["inputs"]["format"] == "flac"


def test_fill_clone_does_not_mutate_template():
    original = template("clone")
    snapshot = copy.deepcopy(original)
    voice_workflow.fill_clone(original, text="x", seed=1, reference_audio="r.wav")
    assert original == snapshot


def test_fill_clone_without_reference_removes_load_audio():
    wf = voice_workflow.fill_clone(template("clone"), text="x", seed=1, reference_audio=None)
    assert "2" not in wf
    assert "reference_audio" not in wf["3"]["inputs"]


def test_fill_clone_rejects_bad_values():
    with pytest.raises(ValueError):
        voice_workflow.fill_clone(template("clone"), text="", seed=1, reference_audio="r.wav")
    with pytest.raises(ValueError):
        voice_workflow.fill_clone(template("clone"), text="x", seed=-1, reference_audio="r.wav")
    with pytest.raises(ValueError):
        voice_workflow.fill_clone(template("clone"), text="x", seed=1, reference_audio="r.wav", language="fr")
    with pytest.raises(ValueError):
        voice_workflow.fill_clone(template("clone"), text="x", seed=1, reference_audio="r.wav", engine="moss-voicegenerator-1.7b")


def test_fill_clone_engine_and_params():
    wf = voice_workflow.fill_clone(template("clone"), text="x", seed=1, reference_audio="r.wav",
                                   engine="moss-tts-delay-8b", params={"temperature": 1.7, "top_k": 25})
    assert wf["1"]["inputs"]["model_variant"] == "MOSS-TTS (Delay 8B)"
    assert wf["3"]["inputs"]["temperature"] == 1.7 and wf["3"]["inputs"]["top_k"] == 25


def test_fill_design_sets_instruction_and_model():
    wf = voice_workflow.fill_design(template("design"), text="你終於來了。", instruction="低沉的中年男性",
                                    seed=9173, filename_prefix="voice/design/lao_zhang")
    assert wf["1"]["inputs"]["model_variant"] == "MOSS-VoiceGenerator"
    assert wf["2"]["inputs"]["instruction"] == "低沉的中年男性"
    assert wf["2"]["inputs"]["seed"] == 9173
    assert wf["3"]["inputs"]["audio"] == ["2", 0]
    assert wf["3"]["inputs"]["filename_prefix"] == "voice/design/lao_zhang"


def test_fill_design_requires_instruction():
    with pytest.raises(ValueError):
        voice_workflow.fill_design(template("design"), text="x", instruction=" ", seed=1)


def test_reference_filename_is_safe_and_content_independent():
    name = voice_workflow.reference_filename("sample_book", "ellie_reed", Path("refs") / "My Ref.WAV")
    assert name.startswith("sample_book_ellie_reed_") and name.endswith(".wav")
    assert " " not in name and "/" not in name


def test_estimate_max_tokens_scales_with_text():
    assert voice_workflow.estimate_max_tokens("") == 64
    short = voice_workflow.estimate_max_tokens("你好，這是一段語音測試。")
    long = voice_workflow.estimate_max_tokens("字" * 90)
    assert 64 <= short < long <= 4096
    assert voice_workflow.estimate_max_tokens("字" * 5000) == 4096


def test_fill_clone_uses_estimated_cap_not_template_default():
    wf = voice_workflow.fill_clone(template("clone"), text="你好。", seed=1, reference_audio="r.wav")
    assert wf["3"]["inputs"]["max_new_tokens"] == voice_workflow.estimate_max_tokens("你好。")


def test_output_audio_paths_reads_audio_key():
    outputs = {"4": {"audio": [{"filename": "voice/a.flac", "subfolder": "", "type": "output"}]}}
    assert voice_workflow.output_audio_paths(outputs) == [
        {"node_id": "4", "filename": "voice/a.flac", "subfolder": "", "type": "output"}]
