"""I4-C 端到端：假模型跑完 annotate → plan → script → storyboard。

模型回覆放在 fixtures/comic/replies.json（切對話、改編規劃、劇本第 1 頁、分鏡第 1 頁），
原著與聖經取自 text2comics 的 examples/sample_comic，所以四份資料彼此對得上。
"""
import dataclasses
import json
import os
from pathlib import Path

import pytest

from t2vn import comic
from t2vn.config import load_config
from t2vn.errors import JobStopped
from tests.studio.test_comic_kind import FakeFE

FIX = Path(__file__).resolve().parent / "fixtures" / "comic"


@pytest.fixture()
def pipeline(tmp_path):
    from vendor.t2c import annotate as t2c_annotate

    config = dataclasses.replace(load_config(), works_dir_override=tmp_path)
    work = tmp_path / "demo"
    (work / "chapters").mkdir(parents=True)
    source = (FIX / "source.txt").read_text(encoding="utf-8")
    paragraphs = t2c_annotate.split_paragraphs(source)
    doc = {"schema_version": "0.1", "book_id": "demo", "chapter_id": "ch001", "chapter_no": 1,
           "title": "遲到的便當", "source_file": "demo.txt", "sha256": "x", "char_count": 0,
           "paragraphs": [{"para_no": i + 1, "text": text} for i, text in enumerate(paragraphs)],
           "scene_breaks": []}
    (work / "book.json").write_text(json.dumps({"book_id": "demo"}, ensure_ascii=False), encoding="utf-8")
    (work / "chapters" / "ch001.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (work / "cast.json").write_text(json.dumps({"characters": {}}, ensure_ascii=False), encoding="utf-8")
    comic.create_project("demo", "ch001", "遲到的便當", config=config)
    root = comic.project_dir("demo", "ch001", config)
    for sub in ("characters", "locations"):
        target = root / "bible" / sub
        target.mkdir(parents=True, exist_ok=True)
        for path in sorted((FIX / "bible" / sub).glob("*.md")):
            (target / path.name).write_bytes(path.read_bytes())
    info = comic.write_source("demo", "ch001", 1, config)
    assert info["paragraphs"] == len(paragraphs)
    replies = json.loads((FIX / "replies.json").read_text(encoding="utf-8"))
    return config, root, replies


class FakeReply:
    def __init__(self, text):
        self.text = text
        self.usage = {"prompt_tokens": 10, "completion_tokens": 20}
        self.finish = "stop"


def make_fakes(replies):
    """四個階段各呼叫一次：切對話（JSON）、規劃（JSON）、劇本（純文字）、分鏡（JSON）。"""
    jsons = [replies["ops"], replies["plan"], replies["storyboard"]]
    texts = [replies["script"]]
    calls = []

    def chat(prov, messages, **kw):
        calls.append(("text", kw.get("stage")))
        return FakeReply(texts.pop(0))

    def chat_json(prov, messages, **kw):
        calls.append(("json", kw.get("stage")))
        data = jsons.pop(0)
        return data, FakeReply(json.dumps(data, ensure_ascii=False))

    return chat, chat_json, calls


def test_four_stages_end_to_end(pipeline):
    config, root, replies = pipeline
    chat, chat_json, calls = make_fakes(replies)
    result = comic.run_stages("demo", "ch001", [1], list(comic.STAGES),
                              config=config, chat=chat, chat_json=chat_json)
    assert result["stages"] == ["annotate", "plan", "script", "storyboard"]
    assert [kind for kind, _ in calls] == ["json", "json", "text", "json"], calls
    assert (root / "scenes" / "ch001.json").is_file()
    assert (root / "plan" / "ch001.json").is_file()
    assert (root / "script" / "ch001.md").is_file()
    board = json.loads((root / "storyboard" / "ch001.json").read_text(encoding="utf-8"))
    assert board["pages"] and board["pages"][0]["panels"]
    builds = sorted(path.name for path in (root / "build" / "adapt").glob("ch001_*.md"))
    assert "ch001_scenes_fidelity.md" in builds and "ch001_fidelity.md" in builds, builds
    assert result["usage"]["calls"] == 4


def test_stop_before_start_writes_nothing(pipeline):
    config, root, replies = pipeline
    chat, chat_json, _ = make_fakes(replies)
    def stop_now():
        return True
    from vendor.t2c import llm as t2c_llm
    with pytest.raises(t2c_llm.Cancelled):
        comic.run_stages("demo", "ch001", [1], ["annotate"], config=config,
                         chat=chat, chat_json=chat_json, should_stop=stop_now)
    assert not (root / "scenes" / "ch001.json").exists()

# ---------------------------------------------------------------- HTTP 任務層

def fixture_chapter(path: Path) -> None:
    """把 fixtures 的原著寫成 chapters/ch001.json（段落號要與 replies 的 ops 對上）。"""
    from vendor.t2c import annotate as t2c_annotate

    paragraphs = t2c_annotate.split_paragraphs((FIX / "source.txt").read_text(encoding="utf-8"))
    doc = {"schema_version": "0.1", "book_id": "demo", "chapter_id": "ch001", "chapter_no": 1,
           "title": "遲到的便當", "source_file": "demo.txt", "sha256": "x", "char_count": 0,
           "paragraphs": [{"para_no": i + 1, "text": text} for i, text in enumerate(paragraphs)],
           "scene_breaks": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def prepare_project(config, work: Path) -> Path:
    fixture_chapter(work / "chapters" / "ch001.json")
    comic.create_project("demo", "ch001", "遲到的便當", config=config)
    root = comic.project_dir("demo", "ch001", config)
    for sub in ("characters", "locations"):
        target = root / "bible" / sub
        target.mkdir(parents=True, exist_ok=True)
        for path in sorted((FIX / "bible" / sub).glob("*.md")):
            (target / path.name).write_bytes(path.read_bytes())
    comic.write_source("demo", "ch001", 1, config)
    return root


def test_comic_job_writes_record_and_items(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import imported, wait_job

    config, studio = imported(tmp_path)
    prepare_project(config, studio.work("demo"))
    replies = json.loads((FIX / "replies.json").read_text(encoding="utf-8"))
    chat, chat_json, _ = make_fakes(replies)
    monkeypatch.setattr(comic, "make_chat", lambda *args, **kw: (chat, chat_json))

    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    started = http.post("/api/books/demo/comic/ch001/job",
                        json={"chapters": [1], "stages": list(comic.STAGES)}).json()
    patch = wait_job(http, started["id"])
    assert patch["status"] == "done"
    assert patch["kind"] == "comic"
    assert patch["items"][0]["state"] == "done"
    assert "切對話" in patch["items"][0]["note"]

    records = http.get("/api/books/demo/job-records").json()["records"]
    record = next(row for row in records if row["id"] == started["id"])
    assert record["kind"] == "comic"
    assert record["chapters"] == [1]


def test_comic_job_rejects_unknown_stage(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import imported

    config, studio = imported(tmp_path)
    prepare_project(config, studio.work("demo"))
    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    response = http.post("/api/books/demo/comic/ch001/job",
                         json={"chapters": [1], "stages": ["render"]})
    assert response.status_code == 400
    assert "階段要是" in response.json()["error"]


def test_second_comic_job_is_busy(tmp_path, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import imported, wait_job

    config, studio = imported(tmp_path)
    prepare_project(config, studio.work("demo"))
    replies = json.loads((FIX / "replies.json").read_text(encoding="utf-8"))
    chat, chat_json, _ = make_fakes(replies)

    def slow_chat_json(prov, messages, **kw):
        time.sleep(0.2)
        return chat_json(prov, messages, **kw)

    monkeypatch.setattr(comic, "make_chat", lambda *args, **kw: (chat, slow_chat_json))
    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    first = http.post("/api/books/demo/comic/ch001/job", json={"chapters": [1], "stages": ["annotate"]}).json()
    second = http.post("/api/books/demo/comic/ch001/job", json={"chapters": [1], "stages": ["annotate"]})
    assert second.status_code == 409
    assert second.json()["code"] == "busy"
    wait_job(http, first["id"])

def test_stop_mid_stage_records_stopped(tmp_path, monkeypatch):
    import threading
    import time

    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import imported, wait_job

    config, studio = imported(tmp_path)
    prepare_project(config, studio.work("demo"))
    replies = json.loads((FIX / "replies.json").read_text(encoding="utf-8"))
    chat, chat_json, _ = make_fakes(replies)
    started = threading.Event()

    def slow_json(prov, messages, **kw):
        from vendor.t2c import llm as t2c_llm

        started.set()
        stop = kw.get("should_stop") or (lambda: False)
        for _ in range(400):
            if stop():
                raise t2c_llm.Cancelled()
            time.sleep(0.01)
        return chat_json(prov, messages, **kw)

    monkeypatch.setattr(comic, "make_chat", lambda *args, **kw: (chat, slow_json))
    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    job_id = http.post("/api/books/demo/comic/ch001/job",
                       json={"chapters": [1], "stages": ["annotate"]}).json()["id"]
    assert started.wait(3)
    assert http.post(f"/api/fe-jobs/{job_id}/stop").status_code == 200
    patch = wait_job(http, job_id)
    assert patch["status"] == "stopped", patch
    rows = http.get("/api/books/demo/job-records").json()["records"]
    record = next(row for row in rows if row["id"] == job_id)
    assert record["status"] == "stopped" and record["kind"] == "comic"

# ---------------------------------------------------------------- 出圖（I4-D）

def fake_comfy(monkeypatch, payload=None):
    """假的 ComfyUI：run 不連網，回傳依 seed 決定的合法 PNG（組版要真的讀得到圖）。"""
    import io

    from PIL import Image

    from vendor.t2c import comfy as t2c_comfy

    seen = []

    def png(seed):
        image = Image.new("RGB", (96, 96), (seed % 200, 120, 90))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def fake_run(base, wf, out_id, should_stop=lambda: False, timeout=1800):
        seen.append({"base": base, "out_id": out_id})
        seed = 0
        for node in (wf.values() if isinstance(wf, dict) else []):
            if isinstance(node, dict):
                inputs = node.get("inputs") or {}
                for key in ("seed", "noise_seed"):
                    if isinstance(inputs.get(key), int):
                        seed = inputs[key]
        return payload if payload is not None else png(seed)

    monkeypatch.setattr(t2c_comfy, "run", fake_run)
    return seen


def render_project(config, root, replies, candidates=2):
    """把分鏡寫進專案並把候選張數設成 candidates。"""
    (root / "storyboard").mkdir(parents=True, exist_ok=True)
    board = {"version": 1, "chapter": 1, "title": "遲到的便當",
             "pages": [{"page": 1, "template": "5a", "panels": replies["storyboard"]["panels"]}]}
    (root / "storyboard" / "ch001.json").write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    yaml = (root / "project.yaml").read_text(encoding="utf-8")
    lines = [line if not line.startswith("candidates:") else "candidates: %d" % candidates
             for line in yaml.splitlines()]
    (root / "project.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return board


def test_render_chapter_writes_candidates(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=2)
    seen = fake_comfy(monkeypatch)
    out = comic.render_chapter("demo", "ch001", 1, config=config)
    assert out["cells"] == 5
    assert len(out["written"]) == 10, out["written"]
    folder = root / "renders" / "ch001"
    assert len(list(folder.glob("*.png"))) == 10
    assert len(seen) == 10


def test_render_same_seed_twice_is_identical(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=2)
    fake_comfy(monkeypatch)
    first = comic.render_chapter("demo", "ch001", 1, config=config)
    folder = root / "renders" / "ch001"
    before = {name: (folder / name).read_bytes() for name in first["written"]}
    fake_comfy(monkeypatch)
    second = comic.render_chapter("demo", "ch001", 1, force=True, config=config)
    assert sorted(second["written"]) == sorted(before)
    for name, data in before.items():
        assert (folder / name).read_bytes() == data, name


def test_render_skips_existing_files(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    fake_comfy(monkeypatch)
    comic.render_chapter("demo", "ch001", 1, config=config)
    seen = fake_comfy(monkeypatch)
    out = comic.render_chapter("demo", "ch001", 1, config=config)
    assert out["written"] == []
    assert seen == []


def test_render_stop_maps_to_jobstopped(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    fake_comfy(monkeypatch)
    fe = FakeFE()
    fe.stop_requested = True
    with pytest.raises(JobStopped):
        comic.comic_render_job(fe, "demo", "ch001", [1], config=config)


def test_render_reports_items(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    fake_comfy(monkeypatch)
    fe = FakeFE()
    out = comic.comic_render_job(fe, "demo", "ch001", [1], config=config)
    assert len(out["written"]) == 5
    assert fe.items["ch001"]["state"] == "done"
    assert "5 格／5 張" in fe.items["ch001"]["note"]


# ---------------------------------------------------------------- 出圖成本控制（第四段）


class ProgressFE(FakeFE):
    """多記 progress 的 FakeFE（看單格出圖的 1/1）。"""

    def __init__(self):
        super().__init__()
        self.progress = []

    def set(self, **fields):
        if "progress" in fields:
            self.progress.append(fields["progress"])


def render_two_page_board(root, replies, candidates=1):
    """兩頁分鏡：第 1 頁 5a 五格、第 2 頁 3a 三格。只補某一頁要靠它。"""
    first = replies["storyboard"]["panels"]
    second = [dict(panel, id="p002_k%d" % slot, slot=slot) for slot, panel in enumerate(first[:3], 1)]
    (root / "storyboard").mkdir(parents=True, exist_ok=True)
    board = {"version": 1, "chapter": 1, "title": "遲到的便當",
             "pages": [{"page": 1, "template": "5a", "panels": first},
                       {"page": 2, "template": "3a", "panels": second}]}
    (root / "storyboard" / "ch001.json").write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    yaml = (root / "project.yaml").read_text(encoding="utf-8")
    lines = [line if not line.startswith("candidates:") else "candidates: %d" % candidates
             for line in yaml.splitlines()]
    (root / "project.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return board


def test_render_single_panel_writes_only_that_panel(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=2)
    fake_comfy(monkeypatch)
    out = comic.render_chapter("demo", "ch001", 1, panel="p001_k3", candidates=1, config=config)
    assert out["cells"] == 1
    assert len(out["written"]) == 1, out["written"]
    assert out["written"][0].startswith("p001_k3")
    filenames = sorted(path.name for path in (root / "renders" / "ch001").glob("*.png"))
    assert filenames == sorted(out["written"])


def test_render_single_page_limits_to_that_page(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_two_page_board(root, replies, candidates=1)
    fake_comfy(monkeypatch)
    out = comic.render_chapter("demo", "ch001", 1, page=2, config=config)
    assert out["cells"] == 3, out["cells"]
    assert len(out["written"]) == 3, out["written"]
    assert all(name.startswith("p002_") for name in out["written"])


def test_render_candidates_override_beats_project_yaml(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=2)      # project.yaml 寫 2
    folder = root / "renders" / "ch001"
    fake_comfy(monkeypatch)
    one = comic.render_chapter("demo", "ch001", 1, candidates=1, config=config)
    assert one["cells"] == 5 and len(one["written"]) == 5, one["written"]
    before = {path.name for path in folder.glob("*.png")}
    fake_comfy(monkeypatch)
    comic.render_chapter("demo", "ch001", 1, candidates=3, config=config)
    after = {path.name for path in folder.glob("*.png")}
    assert len(after) == 15, sorted(after)      # 5 格 × 3 張
    assert len(after - before) == 10            # 已經畫過的 2 個 seed 由第二輪跳過


@pytest.mark.parametrize("kwargs, message", [({"panel": "p999_k1"}, "分鏡裡沒有格"),
                                             ({"page": 9}, "分鏡裡沒有第 9 頁")])
def test_render_unknown_panel_and_page_raise_comicerror(pipeline, monkeypatch, kwargs, message):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    fake_comfy(monkeypatch)
    with pytest.raises(comic.ComicError) as caught:
        comic.render_chapter("demo", "ch001", 1, config=config, **kwargs)
    assert message in str(caught.value)


def test_render_job_note_for_single_panel(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=2)
    fake_comfy(monkeypatch)
    fe = ProgressFE()
    out = comic.comic_render_job(fe, "demo", "ch001", [1], page=1, panel="p001_k2", candidates=1, config=config)
    assert len(out["written"]) == 1
    assert fe.items["ch001"]["note"] == "1 格／1 張"
    assert fe.progress == [(0, 1)], fe.progress
    assert "出圖完成：1 張" in fe.said[-1][0]


def test_render_chapter_without_subset_still_runs_whole_chapter(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=2)
    fake_comfy(monkeypatch)
    out = comic.render_chapter("demo", "ch001", 1, config=config)
    assert out["cells"] == 5
    assert len(out["written"]) == 10


@pytest.mark.parametrize("body", [{"page": 0}, {"page": "x"}, {"candidates": 0},
                                  {"candidates": 13}, {"candidates": "x"}])
def test_render_endpoint_rejects_bad_subset_params(tmp_path, monkeypatch, body):
    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import imported

    config, studio = imported(tmp_path)
    prepare_project(config, studio.work("demo"))
    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    response = http.post("/api/books/demo/comic/ch001/chapter/1/render", json=body)
    assert response.status_code == 400, response.text


def test_render_endpoint_forwards_subset(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import imported, wait_job

    config, studio = imported(tmp_path)
    prepare_project(config, studio.work("demo"))
    seen = {}

    def fake_job(fe, book_id, project_id, chapters, **kwargs):
        seen.update(kwargs)
        seen["chapters"] = chapters
        return {"text": "ok"}

    monkeypatch.setattr(comic, "comic_render_job", fake_job)
    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    started = http.post("/api/books/demo/comic/ch001/chapter/1/render",
                        json={"page": 1, "panel": "p001_k2", "candidates": 3}).json()
    wait_job(http, started["id"])
    assert seen["chapters"] == [1]
    assert seen["page"] == 1 and seen["panel"] == "p001_k2" and seen["candidates"] == 3

# ---------------------------------------------------------------- 組版與 PDF（I4-E 的驗收）


def test_render_then_compose_then_pdf(pipeline, monkeypatch):
    """出圖 → 組版 → PDF：整條線在上面那層跑一次，PDF 檔頭要對。"""
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    fake_comfy(monkeypatch)
    comic.render_chapter("demo", "ch001", 1, config=config)
    out = comic.compose_pages("demo", "ch001", 1, config=config)
    assert out["pages"], "組版沒有產出頁面"
    for name in out["pages"]:
        assert (root / "pages" / "ch001" / name).is_file()
    pdf = comic.export_file("demo", "ch001", 1, "pdf", config=config)
    data = Path(pdf).read_bytes()
    assert data[:4] == b"%PDF"
    assert len(data) > 20000
    cbz = comic.export_file("demo", "ch001", 1, "cbz", config=config)
    assert Path(cbz).read_bytes()[:2] == b"PK"


def test_comic_settings_roundtrip(pipeline):
    config, root, _ = pipeline
    assert comic.read_settings("demo", "ch001", config) == {"lettering": {}, "problems": []}
    font = root / "font.ttf"
    font.write_bytes(b"ttf")
    saved = comic.save_settings("demo", "ch001", {"dialogue_font": str(font), "narration_pt": 10.5, "vertical": False}, config)
    assert saved["lettering"]["dialogue_font"] == str(font) and saved["lettering"]["narration_pt"] == 10.5
    assert saved["problems"] == []
    on_disk = json.loads((root / comic.SETTINGS_FILE).read_text(encoding="utf-8"))
    assert on_disk["lettering"]["vertical"] is False
    cfg = comic.comic_cfg(config, comic.read_settings("demo", "ch001", config))
    assert cfg["lettering"]["dialogue_font"] == str(font) and cfg["lettering"]["narration_pt"] == 10.5
    assert cfg["lettering"]["shout_font"] == ""      # 沒設定的仍用 vendored default（D12：只覆寫有寫的鍵）


def test_comic_settings_reject_bad_values(pipeline):
    config, root, _ = pipeline
    with pytest.raises(comic.ComicError, match="不認得"):
        comic.save_settings("demo", "ch001", {"nope": 1}, config)
    with pytest.raises(comic.ComicError, match="dialogue_pt"):
        comic.save_settings("demo", "ch001", {"dialogue_pt": 999}, config)
    with pytest.raises(comic.ComicError, match="vertical"):
        comic.save_settings("demo", "ch001", {"vertical": "yes"}, config)
    problems = comic.save_settings("demo", "ch001", {"dialogue_font": str(root / "missing.ttf")}, config)["problems"]
    assert problems and "找不到字型檔" in problems[0]


def test_panels_payload_is_a_list(pipeline):
    """D8 薄包裝：panels 要直接是清單（page_comic.js:167 讀 .panels 的 length）。"""
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    payload = comic.panels("demo", "ch001", 1, config)
    assert isinstance(payload["panels"], list), list(payload["panels"])[:3]
    assert len(payload["panels"]) == 5


def test_image_path_joins_the_chapter_folder(pipeline, monkeypatch):
    """候選圖／頁面圖端點收的是 /panels、/pages 回的檔名（不含 chNNN 資料夾），而且擋路徑穿越。"""
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    fake_comfy(monkeypatch)
    comic.render_chapter("demo", "ch001", 1, config=config)
    name = comic.compose_pages("demo", "ch001", 1, config=config)["pages"][0]
    found = comic.image_path("demo", "ch001", 1, name, "page", config=config)
    assert found.is_file() and found.name == name
    assert "pages" in found.parts and "ch001" in found.parts
    with pytest.raises(comic.ComicError):
        comic.image_path("demo", "ch001", 1, "../../project.yaml", config=config)


def test_export_without_pages_says_so(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    with pytest.raises(comic.ComicError, match="還沒有頁面圖"):
        comic.export_file("demo", "ch001", 1, "pdf", config=config)

# ---------------------------------------------------------------- N5 逐句嵌字（第三段）


def test_letters_read_empty_before_save(pipeline):
    """沒有 letters/chNNN.json 不是錯：回空 panels，exists=false。"""
    config, root, _ = pipeline
    out = comic.read_letters("demo", "ch001", 1, config)
    assert out["chapter"] == 1 and out["exists"] is False and out["panels"] == {}
    assert Path(out["path"]).parts[-2:] == ("letters", "ch001.json")
    assert not (root / "letters" / "ch001.json").exists()


def test_letters_save_roundtrip_file_shape(pipeline):
    """存檔要寫出 t2c 的形狀：version 1、chapter、panel 排序、座標 4 位、讀回一致。"""
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    panels = {"p001_k1": {"faces": None, "sfx": [],
                          "bubbles": [{}, {"center": [0.1234567, 0.5], "scale": 1.5, "rows": 6, "text": "改過的台詞"}]}}
    saved = comic.save_letters("demo", "ch001", 1, panels, config)
    doc = json.loads((root / "letters" / "ch001.json").read_text(encoding="utf-8"))
    assert doc["version"] == 1 and doc["chapter"] == 1
    assert list(doc["panels"]) == ["p001_k1"]
    assert doc["panels"]["p001_k1"]["bubbles"][0] == {}
    assert doc["panels"]["p001_k1"]["bubbles"][1]["center"] == [0.1235, 0.5]
    assert saved["exists"] is True
    assert saved["panels"]["p001_k1"]["faces"] is None and saved["panels"]["p001_k1"]["sfx"] == []
    assert saved["panels"] == comic.read_letters("demo", "ch001", 1, config)["panels"]


def test_letters_save_keeps_bubble_padding(pipeline):
    """只改第 2 個框時要保留前面的 {}，尾端空的項目則剪掉；空的一格不寫檔。"""
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    saved = comic.save_letters("demo", "ch001", 1, {"p001_k1": {"bubbles": [{}, {"scale": 1.2}]}}, config)
    assert saved["panels"]["p001_k1"]["bubbles"] == [{}, {"scale": 1.2}]
    cleared = comic.save_letters("demo", "ch001", 1, {"p001_k1": {"bubbles": [{}, {}]}}, config)
    assert cleared["panels"] == {}
    assert json.loads((root / "letters" / "ch001.json").read_text(encoding="utf-8"))["panels"] == {}


@pytest.mark.parametrize("bubble, message", [
    ({"scale": 0.4}, "scale"),
    ({"scale": 2.5}, "scale"),
    ({"rows": 0}, "rows"),
    ({"center": [1.2, 0.5]}, "center"),
    ({"font": "x"}, "不認得"),
])
def test_letters_save_rejects_bad_values(pipeline, bubble, message):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    with pytest.raises(comic.ComicError, match=message):
        comic.save_letters("demo", "ch001", 1, {"p001_k1": {"bubbles": [bubble]}}, config)
    assert not (root / "letters" / "ch001.json").exists()


def test_letters_save_rejects_unknown_panel(pipeline):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    with pytest.raises(comic.ComicError, match="p999_k1"):
        comic.save_letters("demo", "ch001", 1, {"p999_k1": {"bubbles": [{"scale": 1.2}]}}, config)
    assert not (root / "letters" / "ch001.json").exists()


def test_letters_save_requires_storyboard(pipeline):
    """pipeline 還沒寫分鏡：PUT 要說「分鏡」而不是寫出一個沒人對得上的檔。"""
    config, root, _ = pipeline
    with pytest.raises(comic.ComicError, match="分鏡"):
        comic.save_letters("demo", "ch001", 1, {"p001_k1": {"bubbles": [{"scale": 1.2}]}}, config)
    assert not (root / "letters" / "ch001.json").exists()


def test_letters_plan_lists_pages_and_panels(pipeline):
    """plan 回 plan_page() 的完整結果：rect、faces、bubbles 的像素座標。"""
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    from vendor.t2c import letter as t2c_letter
    out = comic.letters_plan("demo", "ch001", 1, config=config)
    assert out["chapter"] == 1 and out["page"] is None
    assert len(out["pages"]) == 1
    plan = out["pages"][0]
    assert plan["page"] == 1 and len(plan["panels"]) == 5
    assert plan["size"][0] > 0 and plan["dpi"] > 0
    for panel in plan["panels"]:
        assert {"id", "slot", "rect", "window", "faces", "faces_auto", "bubbles", "sfx"} <= set(panel)
        x0, y0, x1, y1 = panel["rect"]
        assert x1 > x0 and y1 > y0
        for bubble in panel["bubbles"]:
            assert x0 <= bubble["center"][0] <= x1 and y0 <= bubble["center"][1] <= y1
            assert bubble["rx"] > 0 and bubble["ry"] > 0 and bubble["cell"] > 0
            assert bubble["lines"] and bubble["kind"] in t2c_letter.KINDS
            assert bubble["auto_center"] in (True, False) and "auto_tail" in bubble


def test_letters_plan_honors_override(pipeline):
    """計畫 §2 的驗收：改 scale 與位置 → 重算 → 這框變大、位移，其他框不動。"""
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    before = comic.letters_plan("demo", "ch001", 1, config=config)["pages"][0]
    rect = before["panels"][0]["rect"]
    base_cell = before["panels"][0]["bubbles"][1]["cell"]
    comic.save_letters("demo", "ch001", 1,
                       {"p001_k1": {"bubbles": [{}, {"center": [0.25, 0.5], "scale": 1.6, "rows": 6}]}}, config)
    after = comic.letters_plan("demo", "ch001", 1, config=config)["pages"][0]
    panel = after["panels"][0]
    bubble = panel["bubbles"][1]
    assert bubble["auto_center"] is False and bubble["scale"] == 1.6 and bubble["rows"] == 6
    assert bubble["center"][0] == pytest.approx(rect[0] + 0.25 * (rect[2] - rect[0]), abs=1.0)
    assert bubble["center"][1] == pytest.approx(rect[1] + 0.5 * (rect[3] - rect[1]), abs=1.0)
    assert bubble["cell"] == pytest.approx(base_cell * 1.6, rel=1e-3)
    for index, old in enumerate(before["panels"]):
        for position, old_bubble in enumerate(old["bubbles"]):
            if index == 0 and position == 1:
                continue
            new_bubble = after["panels"][index]["bubbles"][position]
            assert new_bubble["center"] == old_bubble["center"]
            assert new_bubble["tail"] == old_bubble["tail"]


def test_letters_plan_page_filter(pipeline):
    config, root, replies = pipeline
    render_two_page_board(root, replies, candidates=1)
    only = comic.letters_plan("demo", "ch001", 1, page=2, config=config)
    assert only["page"] == 2 and [page["page"] for page in only["pages"]] == [2]
    with pytest.raises(comic.ComicError, match="第 99 頁"):
        comic.letters_plan("demo", "ch001", 1, page=99, config=config)


def test_letters_endpoints_roundtrip(tmp_path):
    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import imported

    config, studio = imported(tmp_path)
    root = prepare_project(config, studio.work("demo"))
    replies = json.loads((FIX / "replies.json").read_text(encoding="utf-8"))
    render_project(config, root, replies, candidates=1)
    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    url = "/api/books/demo/comic/ch001/chapter/1/letters"
    first = http.get(url).json()
    assert first["exists"] is False and first["panels"] == {}
    saved = http.put(url, json={"panels": {"p001_k1": {"bubbles": [{}, {"scale": 1.3}]}}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["panels"]["p001_k1"]["bubbles"][1]["scale"] == 1.3
    assert http.get(url).json()["panels"] == saved.json()["panels"]
    replaced = http.put(url, json={"panels": {"p001_k2": {"bubbles": [{"center": [0.5, 0.4]}]}}})
    assert replaced.status_code == 200, replaced.text
    assert set(http.get(url).json()["panels"]) == {"p001_k2"}      # 整份取代
    plan = http.get(url + "/plan?page=1")
    assert plan.status_code == 200 and len(plan.json()["pages"]) == 1


@pytest.mark.parametrize("body", [
    {"panels": [1, 2]},
    {"panels": {"p001_k1": {"bubbles": [{"scale": 3}]}}},
    {"panels": {"p999_k1": {}}},
])
def test_letters_endpoint_rejects_bad_payload(tmp_path, body):
    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import imported

    config, studio = imported(tmp_path)
    root = prepare_project(config, studio.work("demo"))
    replies = json.loads((FIX / "replies.json").read_text(encoding="utf-8"))
    render_project(config, root, replies, candidates=1)
    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    response = http.put("/api/books/demo/comic/ch001/chapter/1/letters", json=body)
    assert response.status_code == 400, response.text
    assert not (root / "letters" / "ch001.json").exists()


def test_letters_put_preserves_faces_and_sfx(pipeline):
    """介面只改 bubbles；沒顯示的 faces／sfx 要原樣回寫（PUT 是整份取代）。"""
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    faces = [[0.5, 0.4, 0.8, 0.9]]
    sfx = {"box": [0.55, 0.1, 0.95, 0.35], "angle": -12, "vertical": True}
    comic.save_letters("demo", "ch001", 1,
                       {"p001_k1": {"faces": faces, "bubbles": [{}, {"scale": 1.2}], "sfx": [sfx]}}, config)
    saved = comic.save_letters("demo", "ch001", 1,
                               {"p001_k1": {"faces": faces, "sfx": [sfx],
                                            "bubbles": [{}, {"scale": 1.4, "center": [0.4, 0.3]}]}}, config)
    assert saved["panels"]["p001_k1"]["faces"] == faces
    assert saved["panels"]["p001_k1"]["sfx"] == [{"box": [0.55, 0.1, 0.95, 0.35], "angle": -12.0, "vertical": True}]


def test_letters_save_then_compose_changes_page(pipeline, monkeypatch):
    """存檔 → 重組版：頁面圖真的變了，而且改字會回「台詞改過了」。"""
    import hashlib

    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    fake_comfy(monkeypatch)
    comic.render_chapter("demo", "ch001", 1, config=config)
    first = comic.compose_pages("demo", "ch001", 1, config=config)
    page = root / "pages" / "ch001" / first["pages"][0]
    before = hashlib.sha256(page.read_bytes()).hexdigest()
    comic.save_letters("demo", "ch001", 1,
                       {"p001_k1": {"bubbles": [{}, {"center": [0.3, 0.6], "scale": 1.8, "text": "改過的台詞"}]}}, config)
    second = comic.compose_pages("demo", "ch001", 1, config=config)
    after = hashlib.sha256(page.read_bytes()).hexdigest()
    assert before != after
    assert any("台詞改過了" in warning for warning in second["warnings"])


def test_letters_schema_matches_t2c(pipeline):
    """schema 的範圍要與 t2c 常數一致；存出來的檔通過 schema，壞檔不通過。"""
    from t2vn.schema_check import load_schema, validate_data
    from vendor.t2c import letter as t2c_letter

    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    properties = load_schema("letters")["$defs"]["bubble"]["properties"]
    assert properties["scale"]["minimum"] == t2c_letter.SCALE_MIN
    assert properties["scale"]["maximum"] == t2c_letter.SCALE_MAX
    assert properties["rows"]["minimum"] == t2c_letter.ROWS_MIN
    assert properties["rows"]["maximum"] == t2c_letter.ROWS_MAX
    assert sorted(t2c_letter.KINDS) == sorted(["speech", "thought", "shout", "whisper", "radio", "narration"])
    comic.save_letters("demo", "ch001", 1, {"p001_k1": {"bubbles": [{}, {"scale": 1.5}]}}, config)
    doc = json.loads((root / "letters" / "ch001.json").read_text(encoding="utf-8"))
    assert validate_data(doc, "letters") == []
    broken = {"version": 1, "panels": {"p001_k1": {"bubbles": [{"scale": 3}]}}}
    assert validate_data(broken, "letters")
    assert validate_data({"version": 1, "panels": {"p1": {}}}, "letters")

# ---------------------------------------------------------------- 收尾段（計畫 §5 常態項）

# 系統字型資料夾（測試不寫死磁碟機代號：lint 的 hardcoded_path 會擋）
FONT_DIR = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "") / "Fonts"
NOTO_SANS = FONT_DIR / "NotoSansTC-VF.ttf"
NOTO_SERIF = FONT_DIR / "NotoSerifTC-VF.ttf"
needs_noto = pytest.mark.skipif(not (NOTO_SANS.is_file() and NOTO_SERIF.is_file()),
                                reason="需要已安裝的 Noto Sans TC／Noto Serif TC 可變字型")


def _lettering() -> dict[str, str]:
    return {"dialogue_font": str(NOTO_SANS), "shout_font": str(NOTO_SANS),
            "narration_font": str(NOTO_SERIF), "sfx_font": str(NOTO_SANS)}


def _ink(font, text="喊叫測試"):
    """同一串字畫出來有多少墨（字重比較用）。"""
    from PIL import Image, ImageDraw

    image = Image.new("L", (240, 80), 0)
    ImageDraw.Draw(image).text((10, 20), text, font=font, fill=255)
    return sum(1 for value in image.tobytes() if value > 128)


def _composed(pipeline, monkeypatch):
    """出圖 → 組版，回 (config, root)。"""
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    fake_comfy(monkeypatch)
    comic.render_chapter("demo", "ch001", 1, config=config)
    comic.compose_pages("demo", "ch001", 1, config=config)
    return config, root


@needs_noto
def test_comic_lettering_applies_variable_font_weight():
    """NotoSansTC-VF 的預設實例是 Thin：依角色套 Regular／Black 才回得到正常字重。"""
    from PIL import ImageFont

    settings = comic.lettering_settings(_lettering())
    assert settings.notes == []
    dialogue, shout = settings.font("dialogue", 32), settings.font("shout", 32)
    assert _ink(dialogue) > _ink(ImageFont.truetype(str(NOTO_SANS), 32))   # 比預設實例粗
    assert _ink(shout) > _ink(dialogue)                                    # 喊叫比對白粗


@needs_noto
def test_comic_compose_has_no_font_warnings_when_four_fonts_set(pipeline, monkeypatch):
    """四個字型都設定之後，組版 warnings 不能再有「沒有設定 lettering.」或系統字型提示。"""
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    fake_comfy(monkeypatch)
    comic.render_chapter("demo", "ch001", 1, config=config)
    comic.save_settings("demo", "ch001", _lettering(), config)
    out = comic.compose_pages("demo", "ch001", 1, config=config)
    assert out["pages"]
    assert not [w for w in out["warnings"] if "沒有設定 lettering." in w], out["warnings"]
    assert not [w for w in out["warnings"] if "系統字型" in w], out["warnings"]


@needs_noto
def test_comic_settings_reports_no_problems_with_four_fonts(pipeline):
    """四個字型檔都在：存得下去，problems 為空（設定卡不該再出現找不到字型的提醒）。"""
    config, root, _ = pipeline
    saved = comic.save_settings("demo", "ch001", _lettering(), config)
    assert saved["problems"] == []
    assert sorted(comic.FONT_KEYS) == ["dialogue_font", "narration_font", "sfx_font", "shout_font"]
    assert sorted(comic.FONT_WEIGHTS) == ["dialogue", "narration", "sfx", "shout"]
    assert not set(comic.FONT_WEIGHTS) - {key.removesuffix("_font") for key in comic.FONT_KEYS}


def test_export_cbz_comicinfo_has_title_series_pages(pipeline, monkeypatch):
    """CBZ 的 ComicInfo.xml：Title 有書名與章名、Series 是書名、Number 是章號。"""
    import zipfile
    from xml.etree import ElementTree

    config, root = _composed(pipeline, monkeypatch)
    board_path = root / "storyboard" / "ch001.json"
    board = json.loads(board_path.read_text(encoding="utf-8"))
    board["title"] = "第一話 便當"
    board_path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    cbz = Path(comic.export_file("demo", "ch001", 1, "cbz", config=config))
    with zipfile.ZipFile(cbz) as archive:
        xml = ElementTree.fromstring(archive.read("ComicInfo.xml").decode("utf-8"))
    values = {node.tag: (node.text or "") for node in xml}
    assert values["Series"] == "遲到的便當"      # 書名
    assert values["Title"] == "第一話 便當"      # 章名（分鏡的 title）
    assert values["Number"] == "1"


def test_export_cbz_comicinfo_page_count_matches_pngs(pipeline, monkeypatch):
    """頁數要等於 ZIP 裡實際的頁面 PNG 數，不是分鏡頁數。"""
    import zipfile

    config, root = _composed(pipeline, monkeypatch)
    cbz = Path(comic.export_file("demo", "ch001", 1, "cbz", config=config))
    with zipfile.ZipFile(cbz) as archive:
        pngs = [name for name in archive.namelist() if name.endswith(".png")]
        xml = archive.read("ComicInfo.xml").decode("utf-8")
    assert pngs and "<PageCount>%d</PageCount>" % len(pngs) in xml


def test_export_cbz_without_storyboard_title(pipeline, monkeypatch):
    """分鏡不在（只剩頁面圖）時仍出得了 CBZ；ComicInfo 至少有書名、章號與頁數。"""
    import zipfile

    config, root = _composed(pipeline, monkeypatch)
    (root / "storyboard" / "ch001.json").unlink()
    cbz = Path(comic.export_file("demo", "ch001", 1, "cbz", config=config))
    with zipfile.ZipFile(cbz) as archive:
        xml = archive.read("ComicInfo.xml").decode("utf-8")
    assert "<Series>遲到的便當</Series>" in xml
    assert "<Number>1</Number>" in xml
    assert "<Title>遲到的便當　第 1 章</Title>" in xml
    assert "<PageCount>" in xml


def test_render_more_adds_one_new_seed_per_panel(pipeline, monkeypatch):
    """「加畫一張」＝每格再畫一個沒用過的 seed（不是補到 N 張）。"""
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    folder = root / "renders" / "ch001"
    fake_comfy(monkeypatch)
    comic.render_chapter("demo", "ch001", 1, candidates=1, config=config)
    before = {path.name for path in folder.glob("*.png")}
    assert len(before) == 5, sorted(before)
    fake_comfy(monkeypatch)
    out = comic.render_more("demo", "ch001", 1, more=1, config=config)
    after = {path.name for path in folder.glob("*.png")}
    assert out["cells"] == 5 and len(out["written"]) == 5, out["written"]
    assert len(after) == 10 and len(after - before) == 5, sorted(after)


def test_render_more_rejects_zero_and_unknown_panel(pipeline):
    config, root, replies = pipeline
    with pytest.raises(comic.ComicError, match="大於 0"):
        comic.render_more("demo", "ch001", 1, more=0, config=config)


def test_delete_renders_protects_picked_and_rejects_bad_names(pipeline, monkeypatch):
    config, root, replies = pipeline
    render_project(config, root, replies, candidates=2)
    folder = root / "renders" / "ch001"
    fake_comfy(monkeypatch)
    out = comic.render_chapter("demo", "ch001", 1, panel="p001_k3", candidates=2, config=config)
    names = sorted(out["written"])
    assert len(names) == 2
    picked = names[0]
    comic.pick("demo", "ch001", 1, "p001_k3", picked, config=config)
    report = comic.delete_renders("demo", "ch001", 1, names, config=config)
    assert report["protected"] == [picked], report
    assert report["deleted"] == [names[1]], report
    assert (folder / picked).exists() and not (folder / names[1]).exists()
    with pytest.raises(comic.ComicError, match="不合法"):
        comic.delete_renders("demo", "ch001", 1, ["../x.png"], config=config)


def test_render_endpoint_forwards_more_and_delete_endpoint_validates(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import imported, wait_job

    config, studio = imported(tmp_path)
    prepare_project(config, studio.work("demo"))
    seen = {}

    def fake_job(fe, book_id, project_id, chapters, **kwargs):
        seen.update(kwargs)
        return {"text": "ok"}

    monkeypatch.setattr(comic, "comic_render_job", fake_job)
    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    started = http.post("/api/books/demo/comic/ch001/chapter/1/render",
                        json={"panel": "p001_k2", "more": 1}).json()
    wait_job(http, started["id"])
    assert seen["more"] == 1 and seen["panel"] == "p001_k2"
    assert http.post("/api/books/demo/comic/ch001/chapter/1/render",
                     json={"more": 0}).status_code == 400
    assert http.post("/api/books/demo/comic/ch001/chapter/1/renders/delete", json={}).status_code == 400


def test_comic_cfg_does_not_inherit_vendored_preset_when_host_says_none():
    """實測踩到：宿主的 profile 沒指定 preset（空字串），卻沿用了 vendored 的 final，
    而 presets 已被宿主換成 turbo／preset → t2c 報「找不到出圖預設 final」、整章出圖失敗。"""
    import types

    host = types.SimpleNamespace(
        comfyui_base_url="http://127.0.0.1:8188",
        comfy_default_profile="moss",
        comfy_profiles={"moss": {"kind": "moss", "workflow": "",
                                 "models": {"unet": "u.safetensors", "clip": "c.safetensors", "vae": "v.safetensors"},
                                 "negative": "", "default_preset": "",
                                 "presets": {"turbo": {"steps": 4, "cfg": 1.0}}}},
    )
    merged = comic.comic_cfg(host)
    moss = merged["comfy"]["profiles"]["moss"]
    assert list(moss["presets"]) == ["turbo"]
    assert moss["default_preset"] == ""          # 不可以被 vendored 的 final 蓋回來
    from vendor.t2c import comfy as t2c_comfy, render

    profile = t2c_comfy.resolve_profile(merged)
    assert render.resolve_preset(merged, None, profile)[0] is None   # None＝用 workflow 內建，不會拋錯


def test_render_endpoint_add_one_end_to_end(pipeline, monkeypatch):
    """「加畫一張」走真實的 comic_render_job（不是 monkeypatch 掉的替身）：使用者回報 400。"""
    from fastapi.testclient import TestClient

    from t2vn.server import create_app
    from tests.studio.test_studio import wait_job

    config, root, replies = pipeline
    render_project(config, root, replies, candidates=1)
    fake_comfy(monkeypatch)
    http = TestClient(create_app(config), base_url="http://127.0.0.1:7866")
    started = http.post("/api/books/demo/comic/ch001/chapter/1/render", json={"panel": "p001_k3", "more": 1})
    assert started.status_code == 200, started.text
    job = wait_job(http, started.json()["id"])
    assert job["status"] == "done", job
    folder = root / "renders" / "ch001"
    assert len(list(folder.glob("*.png"))) == 1
