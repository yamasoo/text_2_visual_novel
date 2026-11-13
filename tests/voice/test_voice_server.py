import json

from fastapi.testclient import TestClient

from t2vn.server import create_app, tts_executor
from tests.studio.test_studio import wait_job
from tests.voice.conftest import FakeBackend, cast_voice_doc


def client(book):
    return TestClient(create_app(book.config, tts_backend_factory=lambda: book.backend, tts_runner=book.runner),
                      base_url="http://127.0.0.1:7866")


def test_tts_overview_and_cast(book):
    http = client(book)
    view = http.get("/api/books/demo/tts").json()
    assert view["ready"] and view["totals"]["lines"] == 10
    cast = http.get("/api/books/demo/tts/cast").json()
    assert cast["exists"] and set(cast["cast_voice"]["characters"]) == {"ellie_reed", "lao_zhang"}


def test_tts_executor_label_uses_sherpa():
    assert tts_executor({"engine": "sherpa-onnx"}) == "sherpa-onnx"
    assert tts_executor({"engine": "moss-tts-local-1.7b"}) == "ComfyUI"
    assert tts_executor(None) == "ComfyUI"


def test_tts_sample_endpoint(book, monkeypatch):
    from t2vn import sherpa_tts

    class FakeSherpa:
        def __init__(self, model=None):
            self.model = model

        def wav_bytes(self, text, speaker_id, speed=1.0):
            assert text and speaker_id == 20
            return b"RIFF-fake"

    monkeypatch.setattr(sherpa_tts, "SherpaTts", FakeSherpa)
    http = client(book)
    response = http.post("/api/books/demo/tts/sample",
                         json={"text": "測試", "speaker_id": 20, "speed": 1.0})
    assert response.status_code == 200
    assert response.json()["audio"].startswith("data:audio/wav;base64,")


def test_tts_speakers_endpoint(book, monkeypatch):
    from t2vn import sherpa_tts

    monkeypatch.setattr(sherpa_tts, "speaker_catalog", lambda model=None: {
        "available": True, "model": "demo", "speakers": [{"id": 0, "gender": "female",
        "gender_label": "女", "age_label": "青年", "age_years": 21.0, "confidence": "0.9"}],
    })
    http = client(book)
    data = http.get("/api/books/demo/tts/speakers").json()
    assert data["available"] and data["speakers"][0]["gender_label"] == "女"


def test_tts_models_endpoint(book, monkeypatch):
    from t2vn import sherpa_tts

    monkeypatch.setattr(sherpa_tts, "list_models", lambda: [
        {"id": "demo", "path": "models/sherpa/demo", "label": "Demo", "speakers": 1, "language": "zh"}])
    http = client(book)
    data = http.get("/api/books/demo/tts/models").json()
    assert data["models"][0]["id"] == "demo" and data["root"] == "models/sherpa"


def test_tts_generate_job_and_voice_map(book):
    http = client(book)
    started = http.post("/api/books/demo/tts/generate", json={"chapters": [1]}).json()
    assert started["kind"] == "tts"
    job = wait_job(http, started["id"])
    assert job["status"] == "done", job["log"]
    assert len(job["result"]["generated"]) == 10
    assert (book.work / "voice" / "VoiceMap.json").is_file()
    view = http.get("/api/books/demo/tts").json()
    assert view["totals"]["generated"] == 10 and view["totals"]["pending"] == 0
    records = http.get("/api/books/demo/job-records").json()["records"]
    assert records[0]["kind"] == "tts"


def test_tts_compat_endpoint(book):
    http = client(book)
    started = http.post("/tts/generate", json={"book_id": "demo", "chapters": [1], "only": ["ch001_sc02_l003"]}).json()
    job = wait_job(http, started["id"])
    assert job["status"] == "done"
    assert job["result"]["generated"] == ["ch001_sc02_l003"]


def test_tts_compat_requires_book_id(book):
    http = client(book)
    assert http.post("/tts/generate", json={"chapters": [1]}).status_code == 400


def test_tts_put_cast_validates(book):
    http = client(book)
    good = cast_voice_doc()
    assert http.put("/api/books/demo/tts/cast", json={"cast_voice": good}).status_code == 200
    bad = cast_voice_doc()
    del bad["characters"]["lao_zhang"]["voice_design_prompt"]
    assert http.put("/api/books/demo/tts/cast", json=bad).status_code == 400


def test_tts_design_endpoint(book):
    http = client(book)
    started = http.post("/api/books/demo/tts/design", json={}).json()
    job = wait_job(http, started["id"])
    assert job["status"] == "done"
    assert job["result"]["designed"] == ["lao_zhang"]


def test_tts_file_preview(book):
    http = client(book)
    wait_job(http, http.post("/api/books/demo/tts/generate", json={"chapters": [1]}).json()["id"])
    rel = "assets/voice/ch001/ellie_reed_ch001_sc01_l002.ogg"
    response = http.get("/api/books/demo/tts/file", params={"path": rel})
    assert response.status_code == 200 and response.content == b"OggS-fake"
    assert http.get("/api/books/demo/tts/file", params={"path": "../scenes/ch001.json"}).status_code == 400


def test_tts_generate_without_cast_voice_is_400(book):
    (book.work / "voice" / "cast_voice.json").unlink()
    http = client(book)
    response = http.post("/api/books/demo/tts/generate", json={"chapters": [1]})
    assert response.status_code == 400
    assert "cast_voice" in response.json()["error"]
