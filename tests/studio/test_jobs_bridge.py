"""任務只有一份：fekit 的 Job（P4 之後舊的 /api/jobs/* 已刪）。

這裡確認 id 形狀、停止、紀錄與同時只跑一個任務。
"""
import json
import threading
import time

from fastapi.testclient import TestClient

from t2vn.errors import JobStopped
from t2vn.llm import LlmResult, ReplayLLM
from t2vn.server import create_app
from tests.studio.test_studio import imported, sample_ops, wait_job


def make_http(tmp_path, llm_factory=None):
    config, studio = imported(tmp_path)
    if llm_factory is None:
        reply = json.dumps(sample_ops(None), ensure_ascii=False)
        llm_factory = lambda: ReplayLLM([reply])  # noqa: E731
    return TestClient(create_app(config, llm_factory=llm_factory), base_url="http://127.0.0.1:7866"), studio


def test_job_id_and_book_active(tmp_path):
    http, studio = make_http(tmp_path)
    started = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]}).json()
    assert started["id"].startswith("j_") and "_" in started["id"]   # fekit 形狀

    job = wait_job(http, started["id"])
    assert job["id"] == started["id"] and job["status"] == "done"

    fe = http.get(f"/api/fe-jobs/{started['id']}?since=0").json()
    assert fe["id"] == started["id"] and fe["status"] == "done"
    assert fe["items"][0]["key"] == "ch001" and fe["items"][0]["state"] == "done"
    assert fe["result"] == {"done": [1], "failed": []}
    assert fe["seq"] > 0

    listed = http.get("/api/fe-jobs?status=done").json()["jobs"]
    assert any(row["id"] == fe["id"] for row in listed)

    book_active = http.get("/api/books/demo/jobs/active").json()["job"]
    assert book_active is not None and book_active["id"] == fe["id"]


def test_record_status_is_written_before_fekit_finishes(tmp_path):
    http, studio = make_http(tmp_path)
    started = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]}).json()
    wait_job(http, started["id"])
    rows = http.get("/api/books/demo/job-records").json()["records"]
    assert [row["id"] for row in rows] == [started["id"]]
    assert rows[0]["status"] == "done" and rows[0]["log_lines"] > 0
    full = http.get(f"/api/books/demo/job-records/{started['id']}").json()
    assert full["status"] == "done" and full["started"] and full["ended"]


def test_fekit_stop_endpoint_stops_the_legacy_job(tmp_path):
    started_event = threading.Event()

    def slow(messages, on_delta=None, should_stop=None):
        started_event.set()
        for _ in range(200):
            if should_stop and should_stop():
                raise JobStopped("stopped")
            if on_delta:
                on_delta("x")
            time.sleep(0.01)
        return LlmResult(text="{}", usage={})

    http, studio = make_http(tmp_path, llm_factory=lambda: slow)
    job_id = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1, 2]}).json()["id"]
    assert started_event.wait(2)
    assert http.post(f"/api/fe-jobs/{job_id}/stop").status_code == 200
    job = wait_job(http, job_id)
    assert job["status"] == "stopped" and job["stop_requested"] is True
    rows = http.get("/api/books/demo/job-records").json()["records"]
    assert rows[0]["id"] == job_id and rows[0]["status"] == "stopped"


def test_second_job_is_rejected_with_409(tmp_path):
    started_event = threading.Event()

    def slow(messages, on_delta=None, should_stop=None):
        started_event.set()
        for _ in range(300):
            if should_stop and should_stop():
                raise JobStopped("stopped")
            time.sleep(0.01)
        return LlmResult(text="{}", usage={})

    http, studio = make_http(tmp_path, llm_factory=lambda: slow)
    first = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]})
    assert first.status_code == 200
    job_id = first.json()["id"]
    assert started_event.wait(2)
    second = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]})
    assert second.status_code == 409 and second.json()["code"] == "busy"
    http.post(f"/api/fe-jobs/{job_id}/stop")
    wait_job(http, job_id)
