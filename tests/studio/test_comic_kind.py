"""I4-C：comic 的 kind 在三處同步，以及 LLM 注入與停止對應。"""
from pathlib import Path

import pytest

from t2vn.server import JOB_KINDS, create_app  # create_app 會把 vendor/fekit/py 加進 sys.path
from t2vn import comic, jobs_bridge
from t2vn.config import load_config
from t2vn.errors import JobStopped
from vendor.t2c import llm as t2c_llm

REPO = Path(__file__).resolve().parents[2]


def test_kind_title_registered():
    assert jobs_bridge.TITLES["comic"] == "漫畫（t2c）"


def test_kind_label_in_ui():
    text = (REPO / "ui" / "shared.js").read_text(encoding="utf-8")
    assert "comic: " in text and "漫畫（t2c）" in text, "ui/shared.js 的 KIND_LABEL 少了 comic"


def test_kind_not_in_generic_job_endpoint():
    assert "comic" not in JOB_KINDS


def test_comic_job_route_registered():
    app = create_app(load_config())
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/books/{book_id}/comic/{project_id}/job" in paths
    assert "/api/books/{book_id}/comic/{project_id}/chapter/{number}/render" in paths


def test_stage_allowlist():
    assert comic.STAGES == ("annotate", "plan", "script", "storyboard")
    for stage in comic.STAGES:
        assert comic.check_stages([stage]) == [stage]
    assert comic.check_stages(["storyboard", "annotate", "annotate"]) == ["annotate", "storyboard"]
    with pytest.raises(comic.ComicError, match="階段要是"):
        comic.check_stages(["render"])
    with pytest.raises(comic.ComicError, match="至少要選一個階段"):
        comic.check_stages([])


class Result:
    text = "hi"
    usage = {"prompt_tokens": 3, "completion_tokens": 4}
    model = "fake/model"


def test_make_chat_wraps_host_result(monkeypatch):
    from t2vn import llm

    monkeypatch.setattr(llm, "make_executor", lambda *args, **kw: (lambda messages, **kw2: Result()))
    seen = []
    chat, _ = comic.make_chat(load_config(), on_usage=seen.append)
    reply = chat({"id": "adapt"}, [{"role": "user", "content": "x"}])
    assert reply.text == "hi"
    assert reply.finish == "stop"
    assert reply.usage == {"prompt_tokens": 3, "completion_tokens": 4}
    assert seen == [{"prompt_tokens": 3, "completion_tokens": 4}]


class Reply0:
    text = "不是 JSON"
    usage = {}


class Reply1:
    text = "{\"ok\": 1}"
    usage = {}


def test_chat_json_retries_on_bad_json(monkeypatch):
    from t2vn import llm

    replies = [Reply0(), Reply1()]
    monkeypatch.setattr(llm, "make_executor", lambda *args, **kw: (lambda messages, **kw2: replies.pop(0)))
    _, chat_json = comic.make_chat(load_config())
    data, reply = chat_json({"id": "adapt"}, [{"role": "user", "content": "x"}])
    assert data == {"ok": 1}
    assert reply.text == "{\"ok\": 1}"


class FakeFE:
    """只實作 comic_job 會用到的 fekit Job 介面。"""

    def __init__(self):
        self.stop_requested = False
        self.items = {}
        self.said = []

    def begin(self, seg, label=""):
        pass

    def write(self, seg, text):
        pass

    def set(self, **fields):
        pass

    def say(self, text, level=""):
        self.said.append((text, level))

    def item(self, key, **fields):
        self.items.setdefault(key, {}).update(fields)


def fake_result(*args, **kw):
    return {"stages": ["annotate", "plan"], "chapters": [1], "warnings": [], "usage": {"calls": 2}, "project": "x"}


def test_comic_job_maps_cancelled_to_stopped(monkeypatch):
    def boom(*args, **kw):
        raise t2c_llm.Cancelled()

    monkeypatch.setattr(comic, "run_stages", boom)
    with pytest.raises(JobStopped):
        comic.comic_job(FakeFE(), "demo", "ch001", [1], ["annotate"])


def test_comic_job_stops_when_flag_set(monkeypatch):
    monkeypatch.setattr(comic, "run_stages", fake_result)
    fe = FakeFE()
    fe.stop_requested = True
    with pytest.raises(JobStopped):
        comic.comic_job(fe, "demo", "ch001", [1], ["annotate"])


def test_comic_job_marks_items_done(monkeypatch):
    monkeypatch.setattr(comic, "run_stages", fake_result)
    fe = FakeFE()
    result = comic.comic_job(fe, "demo", "ch001", [1], ["plan", "annotate"])
    assert result["stages"] == ["annotate", "plan"]
    assert fe.items["ch001"]["state"] == "done"
    assert "切對話、改編規劃" in fe.items["ch001"]["note"]
