import base64
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from t2vn import ingest
from t2vn.config import parse_config
from t2vn.extract import extract_book
from t2vn.llm import ReplayLLM
from t2vn.server import create_app
from t2vn.studio import Studio, StudioError, fidelity_issues, inferred_flags, normalize_scenes

ZH_NAME = "".join(chr(c) for c in (0x8001, 0x5F35))  # 老張；測試不在 key 位置寫中文

BOOK_TEXT = (
    "第一章 開始\n\n天亮了，工坊裡很安靜。\n\n「走吧。」老張說。\n\n她點點頭。\n\n"
    "第二章 結束\n\n門關上了。\n\n風從海面吹來。\n"
)


def make_config(tmp_path):
    project = tmp_path / "proj"
    library = tmp_path / "lib"
    project.mkdir()
    library.mkdir()
    return parse_config({"project_root": str(project), "book_library": {"path": str(library), "access": "read_only"}})


def b64(text_or_bytes):
    data = text_or_bytes.encode("utf-8") if isinstance(text_or_bytes, str) else text_or_bytes
    return base64.b64encode(data).decode("ascii")


def imported(tmp_path, book_id="demo"):
    config = make_config(tmp_path)
    studio = Studio(config)
    studio.commit_import(book_id, "示範", {"text": BOOK_TEXT}, {"lang": "zh-TW"})
    return config, studio


def sample_ops(chapter_paras):
    return {"chapter_id": "ch001", "scenes": [
        {"title": "開場", "location_id": "dock", "time_of_day": "dawn", "bgm": "bgm_a", "cast": [],
         "ops": [{"k": "N", "p": 1}, {"k": "d", "p": 2, "s": "lao_zhang", "e": "neutral", "t": "走吧。"}, {"k": "N", "p": 3}]}],
        "new_characters": [{"id": "lao_zhang", "name": "老張", "aliases": [], "role": "supporting"}]}


# ---------------------------------------------------------------- 匯入

def test_import_text_chapters(tmp_path):
    config, studio = imported(tmp_path)
    books = studio.list_books()
    assert books[0]["book_id"] == "demo" and books[0]["chapters"] == 2 and books[0]["source_kind"] == "import"
    chapter = studio.chapter_doc("demo", 1)
    assert chapter["title"] == "開始" and len(chapter["paragraphs"]) == 3
    cast = studio.get_cast("demo")
    assert list(cast["characters"]) == ["player"]


def test_import_refuses_overwrite_and_bad_id(tmp_path):
    config, studio = imported(tmp_path)
    with pytest.raises(Exception):
        studio.commit_import("demo", "x", {"text": BOOK_TEXT}, {})
    with pytest.raises(Exception):
        studio.commit_import("Bad-Id", "x", {"text": BOOK_TEXT}, {})


def make_epub(chapters):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        items = "".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(len(chapters)))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        z.writestr("OEBPS/content.opf", f'<package xmlns="http://www.idpf.org/2007/opf"><manifest>{items}</manifest><spine>{spine}</spine></package>')
        for i, (title, paras) in enumerate(chapters):
            body = "".join(f"<p>{p}</p>" for p in paras)
            z.writestr(f"OEBPS/c{i}.xhtml", f"<html><body><h1>{title}</h1>{body}</body></html>")
    return buf.getvalue()


def test_epub_spine_import(tmp_path):
    long = "這是一段很長的正文。" * 30
    epub = make_epub([("序", ["短"]), ("海", [long, long]), ("山", [long]), ("雲", [long, "「好。」她說。"])])
    result = ingest.preview({"files": [{"name": "b.epub", "data": b64(epub)}]}, {"mode": "spine"})
    assert [c["title"] for c in result["chapters"]] == ["海", "山", "雲"]
    assert [c["no"] for c in result["chapters"]] == [1, 2, 3]
    assert any("skipped" in w for w in result["warnings"])
    config = make_config(tmp_path)
    Studio(config).commit_import("epub_book", "EPUB 書", {"files": [{"name": "b.epub", "data": b64(epub)}]}, {"mode": "spine"})
    assert Studio(config).chapter_doc("epub_book", 3)["paragraphs"][-1]["text"] == "「好。」她說。"


def test_epub_mixed_with_text_refused():
    with pytest.raises(Exception):
        ingest.plan_chapters({"files": [{"name": "b.epub", "data": b64(make_epub([("a", ["x"])]))}], "text": "hello"}, {})


def test_chapter_doc_scene_breaks():
    doc = ingest.chapter_doc("demo", 1, "T", "第一段\n\n＊＊＊\n\n第二段\n\n第三段", "f.txt")
    assert doc["scene_breaks"] == [2] and len(doc["paragraphs"]) == 3


# ---------------------------------------------------------------- 場景整理與檢查

def scene(location="dock", lines=None, **extra):
    base = {"scene_id": "x", "location_id": location, "time_of_day": "dawn", "bg": {"asset_id": "bg_old"}, "cast": [],
            "lines": lines or [{"type": "narration", "text": "a", "source_para": 1}], "next": None}
    base.update(extra)
    return base


def test_normalize_renumbers_and_links():
    doc = {"schema_version": "0.1", "book_id": "demo", "chapter_id": "ch001", "lang": "zh-TW", "source": {},
           "scenes": [scene(scene_id="ch001_sc07", next="ch001_sc09"), scene(scene_id="ch001_sc09", location="inn", _ui=1)]}
    out = normalize_scenes(doc, "ch001")
    assert [s["scene_id"] for s in out["scenes"]] == ["ch001_sc01", "ch001_sc02"]
    assert out["scenes"][0]["next"] == "ch001_sc02" and out["scenes"][1]["next"] is None
    assert out["scenes"][1]["bg"]["asset_id"] == "bg_inn_dawn" and "_ui" not in out["scenes"][1]
    assert out["source"]["paragraphs"] == [1, 1]


def test_normalize_adds_missing_speaker_to_cast():
    lines = [{"type": "dialogue", "speaker": "lao_zhang", "text": "好", "source_para": 1}]
    out = normalize_scenes({"schema_version": "0.1", "book_id": "d", "chapter_id": "ch001", "lang": "zh-TW", "scenes": [scene(lines=lines)]}, "ch001")
    assert out["scenes"][0]["cast"][0]["char_id"] == "lao_zhang"


def test_fidelity_issues():
    chapter = {"paragraphs": [{"para_no": 1, "text": "天亮了，工坊裡很安靜。"}, {"para_no": 2, "text": "她點點頭。"}]}
    doc = {"scenes": [{"lines": [{"type": "narration", "text": "天亮了。", "source_para": 1},
                                 {"type": "narration", "text": "完全不同的句子", "source_para": 1}]}]}
    issues = fidelity_issues(doc, chapter)
    assert any(i["line"] == 1 and "not found" in i["message"] for i in issues)
    assert any(i["scene"] is None and "unused" in i["message"] for i in issues)


def test_inferred_flags_from_review():
    review = "| sc02-03 | 5 | **台詞** | **老張** | **平常** | 內容 | ? |\n| sc02-04 | 6 | 旁白 | | | x | |\n"
    doc = {"scenes": [{"scene_id": "ch001_sc01"}, {"scene_id": "ch001_sc02"}]}
    assert inferred_flags(review, doc) == [[1, 2]]


# ---------------------------------------------------------------- 存檔

def valid_doc(studio, book_id="demo"):
    return {"schema_version": "0.1", "book_id": book_id, "chapter_id": "ch001", "lang": "zh-TW", "source": {},
            "scenes": [scene(lines=[{"type": "narration", "text": "天亮了，工坊裡很安靜。", "source_para": 1},
                                    {"type": "narration", "text": "她點點頭。", "source_para": 3}])]}


def test_save_scenes_backup_status_and_errors(tmp_path):
    config, studio = imported(tmp_path)
    saved = studio.save_scenes("demo", 1, valid_doc(studio), "reviewed")
    work = studio.work("demo")
    assert (work / "scenes" / "ch001.json").is_file()
    assert studio.chapters("demo")[0]["scenes_state"] == "reviewed"
    studio.save_scenes("demo", 1, saved["doc"])
    assert len(list((work / "scenes" / "_history").glob("ch001_*.json"))) == 1
    bad = valid_doc(studio)
    bad["scenes"][0]["lines"][0]["text"] = "字" * 120
    with pytest.raises(StudioError):
        studio.save_scenes("demo", 1, bad)
    unknown = valid_doc(studio)
    unknown["scenes"][0]["lines"].append({"type": "dialogue", "speaker": "ghost", "text": "嗚", "source_para": 2})
    unknown["scenes"][0]["cast"] = []
    with pytest.raises(StudioError):
        studio.save_scenes("demo", 1, unknown)


def test_cast_save_validates(tmp_path):
    config, studio = imported(tmp_path)
    cast = studio.get_cast("demo")
    cast["characters"]["lao_zhang"] = {"names": {"zh-TW": "老張"}, "aliases": ["周伯"], "role": "supporting", "hue": 200, "shape": "bent"}
    studio.save_cast("demo", cast)
    cast["characters"][ZH_NAME] = cast["characters"]["lao_zhang"]
    with pytest.raises(StudioError):
        studio.save_cast("demo", cast)


# ---------------------------------------------------------------- 抽取與標註

def extract_response():
    return json.dumps({"summary": "老張要出發。", "plot_points": ["[1-3] 出發"], "cast_names": ["老張"],
                       "new_characters": [{"id": "lao_zhang", "name": "老張", "aliases": [], "role": "supporting", "one_line": "老船夫。"}]},
                      ensure_ascii=False)


def test_extract_writes_outline_and_cast(tmp_path):
    config, studio = imported(tmp_path)
    work = studio.work("demo")
    logs = []
    result = extract_book(work, [1], ReplayLLM([extract_response()]), "demo", "zh-TW", log=logs.append)
    assert result["done"] == [1]
    outline = json.loads((work / "outlines" / "ch001.json").read_text(encoding="utf-8"))
    assert outline["sections"]["plot_points"].startswith("- [1-3]")
    assert json.loads((work / "cast.json").read_text(encoding="utf-8"))["characters"]["lao_zhang"]["one_line"] == {"zh-TW": "老船夫。"}
    again = extract_book(work, [1], ReplayLLM([]), "demo", "zh-TW")
    assert again["skipped"] == [1]  # 續跑：已有的章不再呼叫 LLM
    outlines, notes, roles = studio.annotation_context("demo")
    assert 1 in outlines and notes.get(ZH_NAME) == "老船夫。"


def test_extract_bad_response_keeps_raw(tmp_path):
    config, studio = imported(tmp_path)
    work = studio.work("demo")
    result = extract_book(work, [1], ReplayLLM(["no", "no", "no"]), "demo", "zh-TW")
    assert result["failed"] == [1] and (work / "outlines" / "_raw" / "ch001_r1.json").is_file()


# ---------------------------------------------------------------- HTTP

@pytest.fixture
def client(tmp_path):
    config, studio = imported(tmp_path)
    responses = [json.dumps(sample_ops(None), ensure_ascii=False)]
    app = create_app(config, llm_factory=lambda: ReplayLLM(responses))
    return TestClient(app, base_url="http://127.0.0.1:7866"), studio


def test_http_books_and_chapter(client):
    http, studio = client
    assert http.get("/api/books").json()["books"][0]["book_id"] == "demo"
    chapter = http.get("/api/books/demo/chapters/1").json()
    assert chapter["scenes"] is None and chapter["status"] == "none"
    assert http.get("/api/books/nope/chapters").status_code == 400
    assert http.get("/").status_code == 200


def test_http_refuses_non_local_host_and_origin(tmp_path):
    config, studio = imported(tmp_path)
    app = create_app(config, llm_factory=lambda: ReplayLLM([]))
    assert TestClient(app, base_url="http://evil.example").get("/api/books").status_code == 403
    local = TestClient(app, base_url="http://127.0.0.1:7866")
    assert local.put("/api/books/demo/cast", json={}, headers={"origin": "http://evil.example"}).status_code == 403


def wait_job(http, job_id):
    """等任務結束，回傳 fekit 的 patch。"""
    import time
    for _ in range(100):
        patch = http.get(f"/api/fe-jobs/{job_id}?since=0").json()
        if patch["status"] != "running":
            return patch
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_http_annotate_job_then_edit_and_prototype(client):
    http, studio = client
    started = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]}).json()
    job = wait_job(http, started["id"])
    assert job["status"] == "done", job["log"]
    chapter = http.get("/api/books/demo/chapters/1").json()
    assert chapter["status"] == "draft" and len(chapter["scenes"]["scenes"]) == 1
    doc = chapter["scenes"]
    doc["scenes"][0]["lines"][1]["expr"] = "angry"
    saved = http.put("/api/books/demo/chapters/1/scenes", json={"doc": doc, "status": "reviewed"})
    assert saved.status_code == 200 and saved.json()["doc"]["scenes"][0]["lines"][1]["expr"] == "angry"
    built = http.post("/api/books/demo/prototype", json={}).json()
    assert built["files"][0] == "index.html" and any(a.startswith("location:dock") for a in built["style_added"])
    page = http.get("/api/books/demo/prototype/index.html")
    assert page.status_code == 200 and "<html" in page.text
    # 第二個任務不能和仍在執行的任務重疊；跑完之後可以再開
    active = http.get("/api/books/demo/jobs/active").json()["job"]
    assert active["status"] == "done" and active["items"][0]["state"] == "done" and active["items"][0]["note"] == "1 scenes"


def test_http_job_requires_llm_key(tmp_path):
    from t2vn.llm import LlmError

    config, studio = imported(tmp_path)

    def broken():
        raise LlmError("no API key")
    http = TestClient(create_app(config, llm_factory=broken), base_url="http://127.0.0.1:7866")
    response = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]})
    assert response.status_code == 400 and "no API key" in response.json()["error"]


def test_http_import_preview_and_fs(client):
    http, studio = client
    preview = http.post("/api/import/preview", json={"text": BOOK_TEXT, "opts": {}}).json()
    assert [c["no"] for c in preview["chapters"]] == [1, 2]
    listing = http.get("/api/fs/list", params={"path": ""}).json()
    assert listing["roots"]


# ---------------------------------------------------------------- 進度、停止、設定

def test_job_reports_progress_and_streams(tmp_path):
    config, studio = imported(tmp_path)
    http = TestClient(create_app(config, llm_factory=lambda: ReplayLLM([json.dumps(sample_ops(None), ensure_ascii=False)])),
                      base_url="http://127.0.0.1:7866")
    job = wait_job(http, http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]}).json()["id"])
    assert job["status"] == "done" and job["ended_at"] >= job["started_at"]
    assert sum(len(part.get("text", "")) for part in job["stream"]) > 0  # 串流有回報


def test_job_can_be_stopped_mid_call(tmp_path):
    import threading
    import time

    from t2vn.errors import JobStopped
    from t2vn.llm import LlmResult

    config, studio = imported(tmp_path)
    started = threading.Event()

    def slow(messages, on_delta=None, should_stop=None):
        started.set()
        for _ in range(200):  # 模擬串流：每一段都檢查是否被要求停止
            if should_stop and should_stop():
                raise JobStopped("stopped")
            if on_delta:
                on_delta("x")
            time.sleep(0.01)
        return LlmResult(text="{}", usage={})
    http = TestClient(create_app(config, llm_factory=lambda: slow), base_url="http://127.0.0.1:7866")
    job_id = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1, 2]}).json()["id"]
    assert started.wait(2)
    http.post(f"/api/fe-jobs/{job_id}/stop")
    job = wait_job(http, job_id)
    assert job["status"] == "stopped" and job["items"][0]["state"] == "stopped" and job["items"][1]["state"] == "pending"


def test_settings_roundtrip_and_validation(tmp_path):
    project = tmp_path / "proj"
    library = tmp_path / "lib"
    other_library = tmp_path / "lib2"
    for d in (project, library, other_library):
        d.mkdir()
    cfg = project / "config.yaml"
    cfg.write_text(f"project_root: {json.dumps(str(project))}\nbook_library:\n  path: {json.dumps(str(library))}\n  access: read_only\n"
                   "llm:\n  provider: deepseek\n  model: deepseek-v4-pro\n", encoding="utf-8")
    from t2vn.config import load_config

    config = load_config(cfg)
    assert config.llm_model == "deepseek-v4-pro" and config.server_port == 7866
    http = TestClient(create_app(config, llm_factory=lambda: ReplayLLM([]), config_path=cfg, port=7866), base_url="http://127.0.0.1:7866")
    got = http.get("/api/settings").json()
    assert got["llm"]["model"] == "deepseek-v4-pro" and got["server"]["port"] == 7866 and got["paths"]["works_dir_is_default"]
    works = tmp_path / "vn_works"
    saved = http.put("/api/settings", json={"llm": {"model": "deepseek-flash", "thinking": "low"},
                                            "paths": {"works_dir": str(works), "book_library": str(other_library)},
                                            "server": {"port": 7900}}).json()
    assert saved["llm"]["model"] == "deepseek-flash" and saved["restart_required"] is True
    assert works.is_dir() and (project / "config.yaml.bak").is_file()
    reloaded = load_config(cfg)
    assert reloaded.llm_thinking == "low" and reloaded.works_dir == works and reloaded.server_port == 7900
    # 之後匯入的書放到新的作品位置
    http.post("/api/import/commit", json={"text": BOOK_TEXT, "book_id": "later", "display_name": "x", "opts": {}})
    assert (works / "later" / "book.json").is_file()
    # 不合法的值不會寫進檔案
    bad = http.put("/api/settings", json={"llm": {"thinking": "extreme"}})
    assert bad.status_code == 400 and load_config(cfg).llm_thinking == "low"
    assert http.put("/api/settings", json={"paths": {"book_library": str(tmp_path / "nope")}}).status_code == 400
    assert http.put("/api/settings", json={"server": {"port": 80}}).status_code == 400


def test_settings_never_returns_key_and_requires_provider(tmp_path):
    config, studio = imported(tmp_path)
    http = TestClient(create_app(config, llm_factory=lambda: ReplayLLM([])), base_url="http://127.0.0.1:7866")
    assert set(http.get("/api/settings").json()["llm"]) == {"provider", "model", "thinking", "max_output", "key_found", "key_where",
                                                            "providers", "thinking_values"}
    assert http.post("/api/settings/key", json={"provider": "no_such_provider", "key": "x"}).status_code == 400
    assert http.put("/api/settings", json={}).status_code == 400  # 沒有 config 檔時不能存


def test_works_dir_is_writable_but_library_is_not(tmp_path):
    from t2vn.errors import WriteProtectedError

    project = tmp_path / "proj"
    library = tmp_path / "lib"
    works = tmp_path / "elsewhere"
    for d in (project, library, works):
        d.mkdir()
    config = parse_config({"project_root": str(project), "works_dir": str(works),
                           "book_library": {"path": str(library), "access": "read_only"}})
    assert config.assert_writable(works / "b" / "x.json")
    with pytest.raises(WriteProtectedError):
        config.assert_writable(library / "x.json")
    with pytest.raises(WriteProtectedError):
        config.assert_writable(tmp_path / "other" / "x.json")
    with pytest.raises(Exception):
        parse_config({"project_root": str(project), "works_dir": str(library),
                      "book_library": {"path": str(library), "access": "read_only"}})
