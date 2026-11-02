import copy

import pytest

from t2vn import audio_workflow as m


def template():
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "yue2_3b_int8_convrot.safetensors"}},
        "2": {"class_type": "YuE2GenerateMusic", "inputs": {
            "clip": ["1", 1], "style": "old", "lyrics": "old", "abc": ["8", 0], "seed": 1, "mode": "full",
            "max_duration": 360.0, "temperature": 1, "top_p": 0.95, "top_k": 100, "repetition_penalty": 1.2, "cfg_scale": 1}},
        "3": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["2", 0]}},
        "4": {"class_type": "EmptyYuE2LatentAudio", "inputs": {"seconds": ["2", 1], "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                                                   "latent_image": ["4", 0], "seed": 1, "steps": 32, "cfg": 1,
                                                   "sampler_name": "dpm_2", "scheduler": "sgm_uniform", "denoise": 1}},
        "6": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveAudioAdvanced", "inputs": {"audio": ["6", 0], "filename_prefix": "audio/yue2_bgm",
                                                            "format": "opus", "format.quality": "128k"}},
        "8": {"class_type": "YuE2GenerateABC", "inputs": {"clip": ["1", 1], "style": "old", "lyrics": "old", "seed": 1, "mode": "full"}},
    }


STYLE = "calm piano, 70 bpm, pure instrumental, no vocals"
LYRICS = "[Intro]\n(Soft piano)"


def fill(**kw):
    args = dict(style=STYLE, lyrics=LYRICS, seed=123)
    args.update(kw)
    return m.fill_workflow(template(), **args)


def test_fill_basic_with_abc():
    flow = fill(max_duration=90)
    music = flow["2"]["inputs"]
    assert music["style"] == STYLE and music["lyrics"] == LYRICS and music["seed"] == 123
    assert music["max_duration"] == 90.0 and isinstance(music["max_duration"], float)
    assert music["abc"] == ["8", 0]
    assert flow["8"]["inputs"]["style"] == STYLE and flow["8"]["inputs"]["lyrics"] == LYRICS and flow["8"]["inputs"]["seed"] == 123
    assert flow["5"]["inputs"]["seed"] == 123
    assert flow["1"]["inputs"]["ckpt_name"] == "yue2_3b_int8_convrot.safetensors"
    assert flow["7"]["inputs"]["filename_prefix"] == "audio/yue2_bgm"


def test_template_not_mutated():
    before = template()
    snapshot = copy.deepcopy(before)
    m.fill_workflow(before, style=STYLE, lyrics=LYRICS, seed=5, abc_planning=False)
    assert before == snapshot


def test_without_abc_removes_node():
    flow = fill(abc_planning=False)
    assert "8" not in flow and flow["2"]["inputs"]["abc"] == ""
    # 樣板本來就沒有 ABC 節點也可以
    tpl = template()
    del tpl["8"]
    flow = m.fill_workflow(tpl, style=STYLE, lyrics=LYRICS, seed=1, abc_planning=False)
    assert flow["2"]["inputs"]["abc"] == ""
    with pytest.raises(ValueError, match="YuE2GenerateABC"):
        m.fill_workflow(tpl, style=STYLE, lyrics=LYRICS, seed=1, abc_planning=True)


def test_optional_overrides():
    flow = fill(ckpt_name="other.safetensors", filename_prefix="audio/bgm_tension", audio_format="flac", quality="V0")
    assert flow["1"]["inputs"]["ckpt_name"] == "other.safetensors"
    save = flow["7"]["inputs"]
    assert save["filename_prefix"] == "audio/bgm_tension" and save["format"] == "flac" and save["format.quality"] == "V0"
    with pytest.raises(ValueError):
        fill(filename_prefix="  ")


@pytest.mark.parametrize("missing", ["1", "2", "5", "7"])
def test_missing_required_node(missing):
    tpl = template()
    del tpl[missing]
    with pytest.raises(ValueError, match="缺少"):
        m.fill_workflow(tpl, style=STYLE, lyrics=LYRICS, seed=1)


def test_duplicate_node():
    tpl = template()
    tpl["9"] = copy.deepcopy(tpl["5"])
    with pytest.raises(ValueError, match="不只一個"):
        m.fill_workflow(tpl, style=STYLE, lyrics=LYRICS, seed=1)


@pytest.mark.parametrize("kw", [
    {"style": ""}, {"style": None}, {"lyrics": "  "}, {"seed": -1}, {"seed": 2**63}, {"seed": True}, {"seed": 1.5},
    {"max_duration": 9}, {"max_duration": 601}, {"max_duration": True}, {"max_duration": "120"},
])
def test_bad_arguments(kw):
    with pytest.raises(ValueError):
        fill(**kw)


def test_bad_template():
    with pytest.raises(ValueError):
        m.fill_workflow([], style=STYLE, lyrics=LYRICS, seed=1)  # type: ignore[arg-type]


def test_duration_bounds_accepted():
    assert fill(max_duration=10)["2"]["inputs"]["max_duration"] == 10.0
    assert fill(max_duration=600)["2"]["inputs"]["max_duration"] == 600.0
    assert fill(seed=0)["5"]["inputs"]["seed"] == 0


def test_output_files():
    outputs = {
        "9": {"audio": [{"filename": "b.opus", "subfolder": "audio", "type": "output"}]},
        "10": {"audio": [{"filename": "a.opus"}, {"subfolder": "x"}, "junk"], "images": [{"filename": "p.png"}]},
        "11": {"text": ["hi"]},
        "12": "junk",
        "7": {"audio": [{"filename": "c.opus", "subfolder": 5, "type": ""}]},
    }
    assert m.output_files(outputs) == [
        {"node_id": "10", "filename": "a.opus", "subfolder": "", "type": "output"},
        {"node_id": "7", "filename": "c.opus", "subfolder": "", "type": "output"},
        {"node_id": "9", "filename": "b.opus", "subfolder": "audio", "type": "output"},
    ]


@pytest.mark.parametrize("value", [None, [], "x", 3, {}])
def test_output_files_empty(value):
    assert m.output_files(value) == []
