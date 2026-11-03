"""語音測試共用的作品資料、假 ComfyUI 後端與假 ffmpeg。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from t2vn.config import parse_config
from t2vn.voice_gen import VoiceFailure

REPO = Path(__file__).resolve().parents[2]


def scene_doc() -> dict:
    """2 個角色、10 句對白（另有 2 句旁白，用來檢查 line_id 的行號包含旁白）。"""
    ellie = [
        "你終於來了。",
        "我等了很久。",
        "今天的風不太一樣。",
        "你聽得見嗎？",
        "我們走吧。",
    ]
    zhang = [
        "來了就好。",
        "別急，先坐下。",
        "這條路我走過很多次。",
        "天亮之前要回來。",
        "把燈帶上。",
    ]
    return {
        "schema_version": "0.1", "book_id": "demo", "chapter_id": "ch001", "lang": "zh-TW",
        "scenes": [
            {
                "scene_id": "ch001_sc01", "title": {"zh-TW": "碼頭"}, "location_id": "dock",
                "time_of_day": "dawn", "bg": {"asset_id": "bg_dock_dawn"},
                "cast": [{"char_id": "ellie_reed", "expr": "neutral", "pos": "center"}],
                "lines": [
                    {"type": "narration", "text": "天亮了。", "source_para": 1},
                    {"type": "dialogue", "speaker": "ellie_reed", "text": ellie[0], "source_para": 2},
                    {"type": "dialogue", "speaker": "ellie_reed", "text": ellie[1], "source_para": 3},
                    {"type": "narration", "text": "風從海面吹來。", "source_para": 4},
                    {"type": "dialogue", "speaker": "ellie_reed", "text": ellie[2], "source_para": 5},
                    {"type": "dialogue", "speaker": "ellie_reed", "text": ellie[3], "source_para": 6},
                    {"type": "dialogue", "speaker": "ellie_reed", "text": ellie[4], "source_para": 7},
                ],
                "next": "ch001_sc02",
            },
            {
                "scene_id": "ch001_sc02", "title": {"zh-TW": "船屋"}, "location_id": "boathouse",
                "time_of_day": "dawn", "bg": {"asset_id": "bg_boathouse_dawn"},
                "cast": [{"char_id": "lao_zhang", "expr": "neutral", "pos": "center"}],
                "lines": [
                    {"type": "dialogue", "speaker": "lao_zhang", "text": zhang[i], "source_para": 10 + i}
                    for i in range(5)
                ],
                "next": None,
            },
        ],
    }


def cast_doc() -> dict:
    return {
        "schema_version": "0.1", "book_id": "demo",
        "characters": {
            "ellie_reed": {"names": {"zh-TW": "艾莉"}, "role": "protagonist"},
            "lao_zhang": {"names": {"zh-TW": "老張"}, "role": "supporting"},
        },
    }


def cast_voice_doc() -> dict:
    return {
        "schema_version": "0.1", "book_id": "demo", "engine": "moss-tts-local-1.7b",
        "characters": {
            "ellie_reed": {"source": "reference_audio", "reference_audio": "refs/ellie.wav", "seed": 4217},
            "lao_zhang": {"source": "design_then_clone", "voice_design_prompt": "低沉、緩慢的中年男性。", "seed": 9173},
        },
    }


class FakeBackend:
    """不連 ComfyUI：回傳固定音檔；可指定某句文字失敗。"""

    def __init__(self, fail_on: str | None = None):
        self.calls: list[dict] = []
        self.uploads: list[str] = []
        self.fail_on = fail_on

    def template(self, kind: str) -> dict:
        name = "moss_tts_clone_api.json" if kind == "clone" else "moss_tts_design_api.json"
        return json.loads((REPO / "workflows" / name).read_text(encoding="utf-8"))

    def upload_reference(self, path: Path, name: str) -> str:
        self.uploads.append(name)
        return name

    def run(self, workflow: dict, should_stop):
        text = ""
        for node in workflow.values():
            if isinstance(node, dict) and node.get("class_type") in ("MossTTSGenerate", "MossTTSVoiceDesign"):
                text = str(node.get("inputs", {}).get("text") or "")
        self.calls.append(workflow)
        if self.fail_on and self.fail_on in text:
            raise VoiceFailure("boom")
        return [(".flac", b"FAKE-AUDIO")]


def fake_runner(command: list[str]):
    """ffmpeg 寫出假 OGG；ffprobe 回固定長度與取樣率。"""
    if "ffprobe" in Path(command[0]).name.lower():
        return 0, json.dumps({"streams": [{"codec_type": "audio", "codec_name": "vorbis", "channels": 1,
                                           "sample_rate": "24000"}], "format": {"duration": "1.250"}})
    target = Path(command[-1])
    target.write_bytes(b"OggS-fake")
    return 0, ""


@pytest.fixture
def book(tmp_path):
    project = tmp_path / "proj"
    library = tmp_path / "lib"
    (library / "b").mkdir(parents=True)
    project.mkdir()
    config = parse_config({
        "project_root": str(project), "book_library": {"path": str(library), "access": "read_only"},
        "books": [{"book_id": "demo", "source_dir": "b", "lang": "zh-TW", "display_name": {"zh-TW": "示範"}}],
    })
    work = config.work_dir("demo")
    (work / "scenes").mkdir(parents=True)
    (work / "voice" / "refs").mkdir(parents=True)
    (work / "book.json").write_text(json.dumps({"schema_version": "0.1", "book_id": "demo", "lang": "zh-TW",
                                                "display_name": {"zh-TW": "示範"}}), encoding="utf-8")
    (work / "cast.json").write_text(json.dumps(cast_doc(), ensure_ascii=False), encoding="utf-8")
    (work / "scenes" / "ch001.json").write_text(json.dumps(scene_doc(), ensure_ascii=False), encoding="utf-8")
    (work / "voice" / "refs" / "ellie.wav").write_bytes(b"RIFF-fake-wav")
    (work / "voice" / "cast_voice.json").write_text(json.dumps(cast_voice_doc(), ensure_ascii=False), encoding="utf-8")
    return SimpleNamespace(config=config, work=work, backend=FakeBackend(), runner=fake_runner)
