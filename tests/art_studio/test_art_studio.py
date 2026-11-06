"""素材頁：清單、外觀草擬與確認、畫風設定、出圖任務、挑選。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from t2vn.llm import ReplayLLM
from t2vn.server import create_app
from tests.art_gen.test_art_gen import Fake
from tests.studio.test_studio import imported, sample_ops, wait_job

DRAFT = json.dumps({"tags": "1girl, short hair", "description": "A small woman in a work apron.", "notes": ""})
BAD = "我不知道"


def make_http(tmp_path, replies=None):
    config, studio = imported(tmp_path)
    annotate = json.dumps(sample_ops(None), ensure_ascii=False)
    fake = Fake()
    replay = ReplayLLM([annotate] + (replies or []))  # 同一個實例，回覆才會依序用掉
    app = create_app(config, llm_factory=lambda: replay, comfy_factory=lambda: fake)
    http = TestClient(app, base_url="http://127.0.0.1:7866")
    started = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]}).json()
    assert wait_job(http, started["id"])["status"] == "done"
    return http, config, fake


def test_assets_empty_then_refresh(tmp_path):
    http, config, _ = make_http(tmp_path)
    assert http.get("/api/books/demo/art/assets").json()["ready"] is False
    data = http.post("/api/books/demo/art/refresh").json()
    ids = [a["asset_id"] for a in data["assets"]]
    assert data["ready"] and "bg_dock_dawn" in ids and all(a["candidates"] == [] for a in data["assets"])
    assert data["counts"]["planned"] == len(ids)


def test_style_roundtrip_and_validation(tmp_path):
    http, config, _ = make_http(tmp_path)
    saved = http.put("/api/books/demo/art/style", json={"artist": "@someone", "candidates": 2, "unet": "m.safetensors"}).json()
    assert saved["artist"] == "@someone" and saved["candidates"] == 2 and saved["unet"] == "m.safetensors"
    assert http.get("/api/books/demo/art/style").json()["candidates"] == 2
    assert http.put("/api/books/demo/art/style", json={"candidates": 99}).status_code == 400


def test_appearance_list_save_and_confirm(tmp_path):
    http, config, _ = make_http(tmp_path)
    items = http.get("/api/books/demo/art/appearance").json()
    assert [x["id"] for x in items["locations"]] == ["title_screen", "dock"] and items["locations"][1]["scenes"] == 1
    assert "lao_zhang" in [x["id"] for x in items["characters"]]
    url = "/api/books/demo/art/appearance/location/dock"
    assert http.put(url, json={"confirmed": True}).status_code == 400  # 空的不能確認
    saved = http.put(url, json={"tags": "harbor, pier", "description": "A foggy pier.", "confirmed": True}).json()
    assert saved["confirmed"] is True
    again = http.get("/api/books/demo/art/appearance").json()["locations"][1]["art"]
    assert again["tags"] == "harbor, pier"
    assert http.put("/api/books/demo/art/appearance/place/dock", json={}).status_code == 400
    assert http.put("/api/books/demo/art/appearance/location/a%2Fb", json={"tags": "x"}).status_code in (400, 404)


def test_draft_appearance_retries_once_then_parses(tmp_path):
    http, config, _ = make_http(tmp_path, replies=[BAD, DRAFT])
    r = http.post("/api/books/demo/art/appearance/character/lao_zhang/draft", json={})
    assert r.status_code == 200 and r.json()["tags"] == "1girl, short hair" and "evidence" in r.json()
    assert "沒找到描寫" in r.json()["notes"] or r.json()["evidence"]


def test_draft_gives_error_when_reply_stays_bad(tmp_path):
    http, config, _ = make_http(tmp_path, replies=[BAD, BAD])
    r = http.post("/api/books/demo/art/appearance/location/dock/draft", json={})
    assert r.status_code == 400 and "格式" in r.json()["error"]


def test_generate_job_then_select_and_status(tmp_path):
    http, config, fake = make_http(tmp_path)
    started = http.post("/api/books/demo/art/generate", json={"only": ["bg_dock_dawn"], "candidates": 2}).json()
    job = wait_job(http, started["id"])
    assert job["status"] == "done" and job["result"]["generated"] == ["bg_dock_dawn"]
    assert any("還沒按" in entry["text"] for entry in job["log"])  # 提醒外觀未確認
    assets = http.get("/api/books/demo/art/assets").json()
    bg = next(a for a in assets["assets"] if a["asset_id"] == "bg_dock_dawn")
    assert bg["status"] == "generated" and len(bg["candidates"]) == 2
    img = http.get("/api/books/demo/art/file", params={"path": bg["candidates"][0]["file"]})
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
    chosen = http.post("/api/books/demo/art/select", json={"asset_id": "bg_dock_dawn", "name": bg["candidates"][0]["name"]}).json()
    assert chosen["status"] == "processed" and chosen["file"] == "assets/bg/bg_dock_dawn.png"
    assert http.post("/api/books/demo/art/status", json={"asset_id": "bg_dock_dawn", "status": "final"}).status_code == 400
    ok = http.post("/api/books/demo/art/status", json={"asset_id": "bg_dock_dawn", "status": "final", "license": "AI generated, own use"})
    assert ok.json()["status"] == "final"


def test_art_file_refuses_outside_assets(tmp_path):
    http, config, _ = make_http(tmp_path)
    http.post("/api/books/demo/art/refresh")
    for bad in ("manifest.json", "../demo/book.json", "assets/../manifest.json", "assets/nosuch.png"):
        assert http.get("/api/books/demo/art/file", params={"path": bad}).status_code == 400


def test_generate_stop(tmp_path):
    http, config, fake = make_http(tmp_path)
    fake.stop_after = 0
    started = http.post("/api/books/demo/art/generate", json={"candidates": 1}).json()
    job = wait_job(http, started["id"])
    assert job["status"] == "stopped"   # fekit 的 result 在 stopped 時是 null；摘要留在任務紀錄裡


def test_appearance_lists_title_screen_first(tmp_path):
    from t2vn import art_studio as m

    work = tmp_path / "w"
    (work / "art").mkdir(parents=True)
    (work / "art" / "style.json").write_text('{"locations": {"title_screen": {"tags": "sunset", "description": "d", "confirmed": true}}}', encoding="utf-8")
    items = m.appearance_items(work, None)
    assert items["locations"][0]["id"] == "title_screen" and items["locations"][0]["label"] == "標題畫面"
    assert items["locations"][0]["art"]["tags"] == "sunset" and items["locations"][0]["scenes"] == 0


def test_location_rows_list_their_background_assets_and_generate_by_location(tmp_path):
    http, config, fake = make_http(tmp_path)
    before = http.get("/api/books/demo/art/appearance").json()["locations"]
    assert [a["asset_id"] for a in before[0]["assets"]] == ["bg_title"] and before[0]["assets"][0]["in_manifest"] is False
    dock = next(x for x in before if x["id"] == "dock")
    assert [a["asset_id"] for a in dock["assets"]] == ["bg_dock_dawn"] and dock["assets"][0]["time"]
    http.post("/api/books/demo/art/refresh")
    job = http.post("/api/books/demo/art/generate", json={"only": ["bg_title", "bg_dock_dawn"], "candidates": 1, "regenerate": True}).json()
    assert wait_job(http, job["id"])["status"] == "done"
    after = http.get("/api/books/demo/art/appearance").json()["locations"]
    assert after[0]["assets"][0]["candidates"] == 1 and after[0]["assets"][0]["in_manifest"] is True
