"""音樂路由：總覽、擬稿、出候選（任務）、試聽檔、挑選轉檔、掃描。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from t2vn.server import create_app
from tests.audio_gen.test_audio_gen import Backend
from tests.studio.test_studio import imported, sample_ops, wait_job

REPLY = json.dumps({"style": "calm piano, warm", "lyrics": "[Intro]\n(soft)\n[Outro]"})


def make(tmp_path):
    config, studio = imported(tmp_path)
    ops = json.dumps(sample_ops(None), ensure_ascii=False)

    from t2vn.llm import LlmResult

    def replay(messages, on_delta=None, should_stop=None):
        text = REPLY if "instrumental" in messages[-1]["content"] else ops
        return LlmResult(text=text, usage={}, model="replay")

    backend = Backend()

    def runner(command):
        Path(command[-1]).write_bytes(b"opus")
        return 0, ""

    app = create_app(config, llm_factory=lambda: replay, audio_backend_factory=lambda: backend, audio_runner=runner)
    http = TestClient(app, base_url="http://127.0.0.1:7866")
    started = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]}).json()
    assert wait_job(http, started["id"])["status"] == "done"
    http.post("/api/books/demo/art/refresh")
    return http, studio, backend


def test_audio_flow(tmp_path):
    http, studio, backend = make(tmp_path)
    items = http.get("/api/books/demo/art/audio").json()["items"]
    assert items, "demo book has no bgm/ambience"
    asset_id = items[0]["asset_id"]
    assert items[0]["prompt"] == {"style": "", "lyrics": ""} and items[0]["candidates"] == []

    drafted = http.post(f"/api/books/demo/art/audio/{asset_id}/draft", json={}).json()
    assert "prompt" in drafted, drafted
    assert "no vocals" in drafted["prompt"]["style"]
    assert http.put(f"/api/books/demo/art/audio/{asset_id}/prompt", json={"style": "x, pure instrumental, no vocals", "lyrics": "[Intro]"}).json() == {"ok": True}

    job = http.post("/api/books/demo/art/audio/generate", json={"only": [asset_id], "candidates": 2}).json()
    assert wait_job(http, job["id"])["status"] == "done"
    item = next(i for i in http.get("/api/books/demo/art/audio").json()["items"] if i["asset_id"] == asset_id)
    assert len(item["candidates"]) == 2 and item["status"] == "generated"
    assert backend.calls[0]["2"]["inputs"]["style"].startswith("x, pure")

    cand = item["candidates"][0]
    assert http.get("/api/books/demo/art/audio/file", params={"path": cand["file"]}).content.startswith(b"AUDIO")
    assert http.get("/api/books/demo/art/audio/file", params={"path": "../book.json"}).status_code in (400, 404)

    chosen = http.post("/api/books/demo/art/audio/select", json={"asset_id": asset_id, "name": cand["name"]}).json()
    assert chosen["status"] == "processed" and chosen["file"].endswith(f"{asset_id}.opus")
    item = next(i for i in http.get("/api/books/demo/art/audio").json()["items"] if i["asset_id"] == asset_id)
    assert item["has_file"] is True


def test_audio_scan_registers_dropped_file(tmp_path):
    http, studio, _ = make(tmp_path)
    asset_id = http.get("/api/books/demo/art/audio").json()["items"][0]["asset_id"]
    ctx_work = studio.work("demo")
    folder = ctx_work / "assets" / ("bgm" if asset_id.startswith("bgm") else "ambience")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{asset_id}.opus").write_bytes(b"x")
    (folder / f"{asset_id}.json").write_text(json.dumps({"license": "CC0", "source_url": "u"}), encoding="utf-8")
    (folder / "stray.opus").write_bytes(b"x")
    result = http.post("/api/books/demo/art/audio/scan").json()
    assert result["updated"] == [asset_id] and result["orphans"] == [f"{folder.name}/stray.opus"]
    item = next(i for i in http.get("/api/books/demo/art/audio").json()["items"] if i["asset_id"] == asset_id)
    assert item["status"] == "generated" and item["license"] == "CC0" and item["file"].endswith(".opus")

