"""P1：fekit 的本機保護（權杖）要蓋住舊端點，首頁要帶權杖。"""
from starlette.testclient import TestClient

from t2vn.llm import ReplayLLM
from t2vn.server import create_app
from tests.studio.test_studio import imported


def test_token_is_required_for_mutations(tmp_path):
    config, _ = imported(tmp_path)
    app = create_app(config, llm_factory=lambda: ReplayLLM([]))

    ok = TestClient(app, base_url="http://127.0.0.1:7866")          # conftest 會補上正確權杖
    assert ok.get("/api/books").status_code == 200

    bad = TestClient(app, base_url="http://127.0.0.1:7866",
                     headers={"X-FE-Token": "wrong-token"})        # conftest 不會蓋掉這個
    response = bad.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]})
    assert response.status_code == 403
    assert response.json()["code"] == "bad_token"


def test_index_carries_token_meta(tmp_path):
    config, _ = imported(tmp_path)
    app = create_app(config, llm_factory=lambda: ReplayLLM([]))
    html = TestClient(app, base_url="http://127.0.0.1:7866").get("/").text
    assert 'name="fe-token"' in html and 'name="fe-app"' in html
    # 舊版 index.html 的 script 還是照舊（P2 才換）
    assert '<script src="/ui/app.js"></script>' in html


def test_mutations_with_token_still_work(tmp_path):
    config, _ = imported(tmp_path)
    app = create_app(config, llm_factory=lambda: ReplayLLM([]))
    http = TestClient(app, base_url="http://127.0.0.1:7866")
    response = http.put("/api/books/demo/bible/locations/dock", json={"label": "碼頭"})
    assert response.status_code == 200
