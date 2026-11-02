import json
from pathlib import Path

import pytest

from t2vn.llm import ReplayLLM

from t2vn import audio_gen as m

TEMPLATE = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x"}},
    "2": {"class_type": "YuE2GenerateMusic", "inputs": {"style": "", "lyrics": "", "seed": 0, "max_duration": 60.0, "abc": ["8", 0]}},
    "5": {"class_type": "KSampler", "inputs": {"seed": 0}},
    "7": {"class_type": "SaveAudioAdvanced", "inputs": {"filename_prefix": "a", "format": "opus"}},
    "8": {"class_type": "YuE2GenerateABC", "inputs": {"style": "", "lyrics": "", "seed": 0}},
}


class Cfg:
    def assert_writable(self, path):
        pass


class Backend:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def template(self):
        return TEMPLATE

    def run(self, workflow, should_stop):
        self.calls.append(workflow)
        if self.fail:
            raise m.AudioFailure("boom")
        return [(".flac", b"AUDIO%d" % len(self.calls))]


@pytest.fixture
def work(tmp_path):
    w = tmp_path / "work"
    (w / "scenes").mkdir(parents=True)
    manifest = {"schema_version": "0.1", "book_id": "bk", "assets": [
        {"asset_id": "bgm_a", "kind": "bgm", "names": {"zh-TW": "日常"}, "file": "assets/bgm/bgm_a.ogg", "status": "planned", "license": ""},
        {"asset_id": "amb_x", "kind": "ambience", "names": {"zh-TW": "風"}, "file": "assets/ambience/amb_x.ogg", "status": "planned", "license": ""},
        {"asset_id": "bg_y", "kind": "bg", "names": {}, "file": "assets/bg/bg_y.png", "status": "planned", "license": ""},
    ]}
    (w / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    scene = {"scenes": [{"scene_id": "ch001_sc01", "title": {"zh-TW": "借磅秤"}, "location_id": "yard", "time_of_day": "dawn",
                         "bgm": "bgm_a", "ambience": "amb_x"}]}
    (w / "scenes" / "ch001.json").write_text(json.dumps(scene), encoding="utf-8")
    return w


def manifest(w):
    return {a["asset_id"]: a for a in json.loads((w / "manifest.json").read_text(encoding="utf-8"))["assets"]}


def test_scenes_for_flattens_title(work):
    asset = manifest(work)["bgm_a"]
    assert m.scenes_for(work, asset) == [{"scene_id": "ch001_sc01", "title": "借磅秤", "location": "yard", "time_of_day": "dawn", "summary": ""}]


REPLY = json.dumps({"style": "calm piano, warm, slow", "lyrics": "[Intro]\n(soft)\n[Outro]"})


def test_draft_prompt_saves_and_retries(work):
    fields, _ = m.draft_prompt(Cfg(), work, "bgm_a", ReplayLLM(["not json", REPLY]))
    assert "no vocals" in fields["style"]
    assert m.load_prompts(work)["bgm_a"]["style"] == fields["style"]


def test_draft_prompt_gives_up(work):
    with pytest.raises(m.AudioGenError):
        m.draft_prompt(Cfg(), work, "bgm_a", ReplayLLM(["nope", "nope"]), retries=1)


def test_draft_prompt_rejects_image_asset(work):
    with pytest.raises(m.AudioGenError):
        m.draft_prompt(Cfg(), work, "bg_y", ReplayLLM([REPLY]))


def test_generate_needs_prompt(work):
    report = m.generate(Cfg(), work, Backend())
    assert set(report.failed) == {"bgm_a", "amb_x"} and report.generated == []


def test_generate_makes_candidates_and_marks_generated(work):
    m.save_prompt(Cfg(), work, "bgm_a", "calm piano, pure instrumental, no vocals", "[Intro]")
    backend = Backend()
    report = m.generate(Cfg(), work, backend, only=["bgm_a"], candidates=2)
    assert report.generated == ["bgm_a"]
    assert len(m.list_candidates(work, "bgm_a")) == 2
    assert manifest(work)["bgm_a"]["status"] == "generated"
    assert backend.calls[0]["2"]["inputs"]["style"].startswith("calm piano")
    assert backend.calls[0]["7"]["inputs"]["filename_prefix"] == "audio/bgm_a"
    report = m.generate(Cfg(), work, backend, only=["bgm_a"], candidates=2)   # 續跑不重出
    assert report.skipped == ["bgm_a"] and len(backend.calls) == 2
    m.generate(Cfg(), work, backend, only=["bgm_a"], candidates=1, regenerate=True)
    assert len(m.list_candidates(work, "bgm_a")) == 3


def test_generate_failure_keeps_others(work):
    m.save_prompt(Cfg(), work, "bgm_a", "x, pure instrumental, no vocals", "[Intro]")
    report = m.generate(Cfg(), work, Backend(fail=True), only=["bgm_a"])
    assert report.failed == {"bgm_a": "boom"} and manifest(work)["bgm_a"]["status"] == "planned"


def test_generate_stop(work):
    m.save_prompt(Cfg(), work, "bgm_a", "x", "[Intro]")
    report = m.generate(Cfg(), work, Backend(), only=["bgm_a"], should_stop=lambda: True)
    assert report.stopped and not m.list_candidates(work, "bgm_a")


def test_select_converts_and_registers(work):
    m.save_prompt(Cfg(), work, "bgm_a", "x", "[Intro]")
    m.generate(Cfg(), work, Backend(), only=["bgm_a"])
    name = m.list_candidates(work, "bgm_a")[0]["name"]
    seen = []

    def runner(command):
        seen.append(command)
        Path(command[-1]).write_bytes(b"opus")
        return 0, ""

    asset = m.select_candidate(Cfg(), work, "bgm_a", name, runner=runner)
    assert asset["file"] == "assets/bgm/bgm_a.opus" and asset["status"] == "processed"
    assert asset["license"] == m.LICENSE_TEXT and (work / asset["file"]).read_bytes() == b"opus"
    assert "-filter_complex" in seen[0]


def test_select_ffmpeg_missing_and_bad_name(work):
    m.save_prompt(Cfg(), work, "bgm_a", "x", "[Intro]")
    m.generate(Cfg(), work, Backend(), only=["bgm_a"])
    name = m.list_candidates(work, "bgm_a")[0]["name"]
    with pytest.raises(m.AudioGenError, match="ffmpeg"):
        m.select_candidate(Cfg(), work, "bgm_a", name, runner=lambda c: (127, "找不到 ffmpeg"))
    with pytest.raises(m.AudioGenError):
        m.select_candidate(Cfg(), work, "bgm_a", "../x.flac")
    assert manifest(work)["bgm_a"]["status"] == "generated"
