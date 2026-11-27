"""t_i4_fix_json_mode：json_mode 要一路傳到 providers.chat，四階段要能重跑。

對應 docs/tasks/t_i4_fix_json_mode.md 第 8 節：這裡全部用假執行者，不打真模型。
F1（json_mode）、F2（force 重跑）、F3（空白輸出）、F4（finish_reason）各一項。
"""
import json
from typing import Any, Callable

import pytest

from t2vn import comic, llm
from t2vn.llm import LlmResult
from tests.studio.test_comic_pipeline import FIX, prepare_project

# 四個階段各呼叫模型一次，順序與 adapt.Adapter 走的順序相同
STAGE_ORDER = ("annotate", "plan", "script", "storyboard")
REPLY_KEYS = ("ops", "plan", "script", "storyboard")


@pytest.fixture()
def fake_project(tmp_path):
    """一本匯入好的書加一個漫畫專案，回傳 (config, 專案根目錄, fixtures 的模型回覆)。"""
    from tests.studio.test_studio import imported

    config, studio = imported(tmp_path)
    root = prepare_project(config, studio.work("demo"))
    replies = json.loads((FIX / "replies.json").read_text(encoding="utf-8"))
    return config, root, replies


def fake_executor(replies: dict[str, Any], seen: list[tuple[str, bool]],
                  finish_reason: str = "") -> Callable[..., LlmResult]:
    """假執行者：依序回傳四個階段的回覆，並記下每次呼叫收到的 (階段, json_mode)。"""
    queue = [(name, replies[key]) for name, key in zip(STAGE_ORDER, REPLY_KEYS)]

    def call(messages, on_delta=None, should_stop=None, on_thinking=None,
             json_mode: bool = True) -> LlmResult:
        name, payload = queue.pop(0)
        seen.append((name, json_mode))
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        if on_delta:
            on_delta(text)
        usage: dict[str, Any] = {"prompt_tokens": 10, "completion_tokens": 20}
        if finish_reason and name == "script":
            usage["finish_reason"] = finish_reason
        return LlmResult(text=text, usage=usage, model="fake")

    return call


def run_all(fake_project, monkeypatch, *, script_text: str | None = None,
            finish_reason: str = "") -> list[tuple[str, bool]]:
    """跑完四個文字階段，回傳每個階段的 (名稱, json_mode)。"""
    config, _root, replies = fake_project
    replies = dict(replies)
    if script_text is not None:
        replies["script"] = script_text
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(llm, "make_executor", lambda *args, **kw: fake_executor(replies, seen, finish_reason))
    comic.run_stages("demo", "ch001", [1], list(comic.STAGES), config=config)
    return seen


def test_script_stage_gets_plain_text_mode(fake_project, monkeypatch):
    """F1：script 是文字階段，json_mode 必須是 False（否則 response_format 會把輸出壓成空白）。"""
    modes = dict(run_all(fake_project, monkeypatch))
    assert modes["script"] is False, modes


def test_json_stages_keep_json_mode(fake_project, monkeypatch):
    """F1：annotate／plan／storyboard 仍然要 json_mode=True。"""
    modes = dict(run_all(fake_project, monkeypatch))
    assert modes["annotate"] is True, modes
    assert modes["plan"] is True, modes
    assert modes["storyboard"] is True, modes


def test_blank_text_output_says_blank(fake_project, monkeypatch):
    """F3：空白輸出要講「模型回傳空白」，而不是 t2c 的「0 個標題」。"""
    with pytest.raises(comic.ComicError) as caught:
        run_all(fake_project, monkeypatch, script_text="   ")
    message = str(caught.value)
    assert "模型回傳空白" in message, message
    assert "token 20" in message, message


def test_finish_reason_reaches_t2c(fake_project, monkeypatch):
    """F4：上游的 finish_reason 要傳下去，t2c 的截斷保護才會生效（寫死 "stop" 就會靜靜通過）。"""
    from vendor.t2c import llm as t2c_llm

    with pytest.raises(t2c_llm.LLMError) as caught:
        run_all(fake_project, monkeypatch, finish_reason="length")
    assert "輸出被截斷" in str(caught.value), str(caught.value)


def test_job_passes_force_to_run_stages(fake_project, monkeypatch):
    """F2：POST .../job 帶 force: true 時，run_stages 要收到 force=True。"""
    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import wait_job

    config, _root, _replies = fake_project
    seen: dict[str, Any] = {}

    def dry_run(*args, **kwargs) -> dict[str, Any]:
        seen.update(kwargs)
        return {"stages": list(comic.STAGES), "chapters": [1], "warnings": [], "usage": {},
                "project": "dry"}

    monkeypatch.setattr(comic, "run_stages", dry_run)
    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    started = http.post("/api/books/demo/comic/ch001/job",
                        json={"chapters": [1], "stages": list(comic.STAGES), "force": True}).json()
    patch = wait_job(http, started["id"])
    assert patch["status"] == "done", patch
    assert seen["force"] is True, seen


def test_job_defaults_to_no_force(fake_project, monkeypatch):
    """F2：沒帶 force 時維持原本「已存在就跳過」的語意。"""
    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import wait_job

    config, _root, _replies = fake_project
    seen: dict[str, Any] = {}

    def dry_run(*args, **kwargs) -> dict[str, Any]:
        seen.update(kwargs)
        return {"stages": list(comic.STAGES), "chapters": [1], "warnings": [], "usage": {},
                "project": "dry"}

    monkeypatch.setattr(comic, "run_stages", dry_run)
    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    started = http.post("/api/books/demo/comic/ch001/job",
                        json={"chapters": [1], "stages": list(comic.STAGES)}).json()
    wait_job(http, started["id"])
    assert seen["force"] is False, seen


def test_force_really_overwrites_existing_stage(fake_project, monkeypatch):
    """F2：force 要真的傳到 t2c——沒 force 會被「已存在」擋下，有 force 才覆蓋得掉舊內容。"""
    from vendor.t2c import adapt as t2c_adapt

    config, root, replies = fake_project
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(llm, "make_executor", lambda *args, **kw: fake_executor(replies, seen))

    comic.run_stages("demo", "ch001", [1], ["annotate"], config=config)
    path = root / "scenes" / "ch001.json"
    assert path.is_file()

    path.write_text('{"sentinel": true}\n', encoding="utf-8")
    with pytest.raises(t2c_adapt.AdaptError, match="已經存在"):
        comic.run_stages("demo", "ch001", [1], ["annotate"], config=config)
    assert "sentinel" in path.read_text(encoding="utf-8")        # 被擋下，沒覆蓋

    comic.run_stages("demo", "ch001", [1], ["annotate"], config=config, force=True)
    assert "sentinel" not in path.read_text(encoding="utf-8")    # 真的覆蓋掉了
