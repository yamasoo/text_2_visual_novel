"""S4b 第一批模組接上伺服器：串流緩衝、任務紀錄、總覽、設定集、風格、場景歷史。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from t2vn.llm import ReplayLLM
from t2vn.server import create_app
from tests.studio.test_studio import imported, sample_ops, wait_job


def make_http(tmp_path):
    config, studio = imported(tmp_path)
    reply = json.dumps(sample_ops(None), ensure_ascii=False)
    return TestClient(create_app(config, llm_factory=lambda: ReplayLLM([reply])), base_url="http://127.0.0.1:7866"), studio, config


def run_job(http):
    started = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]}).json()
    return wait_job(http, started["id"])


def test_job_stream_and_record(tmp_path):
    http, studio, config = make_http(tmp_path)
    job = run_job(http)
    assert job["status"] == "done"
    stream = http.get(f"/api/jobs/{job['id']}?stream_since=0").json()["stream"]
    kinds = {e["type"] for e in stream["events"]}
    assert {"begin", "text", "end"} <= kinds and stream["last"] >= 3
    later = http.get(f"/api/jobs/{job['id']}?stream_since={stream['last']}").json()["stream"]
    assert later["events"] == []
    assert "stream" not in http.get(f"/api/jobs/{job['id']}").json()
    # 任務結束後自動存紀錄
    rows = http.get("/api/books/demo/job-records").json()["records"]
    assert [r["id"] for r in rows] == [job["id"]] and rows[0]["status"] == "done"
    full = http.get(f"/api/books/demo/job-records/{job['id']}").json()
    assert full["kind"] == "annotate" and full["log"] and full["executor"]
    assert http.get("/api/books/demo/job-records/nosuch").status_code == 400


def test_overview_bible_style_history(tmp_path):
    http, studio, config = make_http(tmp_path)
    run_job(http)
    ov = http.get("/api/books/demo/overview").json()
    assert ov["chapters"] >= 1 and ov["scenes"] >= 1 and ov["per_chapter"]
    # 設定集
    view = http.get("/api/books/demo/bible").json()
    assert any(r["id"] == "dock" for r in view["locations"])
    put = http.put("/api/books/demo/bible/locations/dock", json={"label": "碼頭", "prompt": "morning dock"})
    assert put.status_code == 200 and put.json()["entry"]["label"] == "碼頭"
    assert http.put("/api/books/demo/bible/locations/Bad Id", json={"label": "x"}).status_code == 400
    assert http.put("/api/books/demo/bible/nothing/dock", json={"label": "x"}).status_code == 400
    assert http.delete("/api/books/demo/bible/locations/dock").json() == {"removed": True}
    # 風格：還沒建原型時要明講
    assert http.get("/api/books/demo/style").status_code == 400
    http.post("/api/books/demo/prototype", json={"chapters": [1]})
    style = http.get("/api/books/demo/style").json()["style"]
    assert isinstance(style, dict) and style
    bad = http.put("/api/books/demo/style", json={"palette": "not-a-mapping"})
    assert bad.status_code == 400
    assert http.get("/api/books/demo/style").json()["style"] == style  # 驗證失敗不動檔案
    assert http.put("/api/books/demo/style", json={}).status_code == 200
    # 場景歷史：存兩次後有備份，可以還原
    chapter = http.get("/api/books/demo/chapters/1").json()
    doc = chapter["scenes"]
    studio.save_scenes("demo", 1, doc)
    hist = http.get("/api/books/demo/chapters/1/history").json()["history"]
    assert hist
    restored = http.post("/api/books/demo/chapters/1/history/restore", json={"name": hist[0]["name"]})
    assert restored.status_code == 200
    assert http.post("/api/books/demo/chapters/1/history/restore", json={"name": "../../x"}).status_code == 400


def test_job_events_label_chunks():
    from t2vn.server import Job
    job = Job("demo", "annotate", [3])
    job.set_item(3, "running")
    job.event("chunk", {"index": 2, "total": 4, "first": 40, "last": 79})
    job.event("request", {"round": 1})
    job.event("delta", {"chars": 3, "text": "abc"})
    job.event("response", {"usage": {}})
    public = job.public(0, 0)
    assert public["current"]["chunk"] == "2/4" and public["current"]["chunk_range"] == [40, 79]
    begin = public["stream"]["events"][0]
    assert begin["type"] == "begin" and "第 2/4 塊" in begin["label"] and "第 1 輪" in begin["label"]
    job.set_item(4, "running")
    assert "chunk" not in job.public(0)["current"]
