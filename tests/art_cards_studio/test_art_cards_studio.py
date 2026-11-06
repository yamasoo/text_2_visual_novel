"""外觀分頁接線：路由、AI 草擬（含內文證據）、頭像＋立繪出圖任務、畫風庫、立繪種子與對齊。"""
from __future__ import annotations

import json

import openpyxl
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from t2vn import art_gen, art_prompts, sprite_consistency
from t2vn.llm import ReplayLLM
from t2vn.server import create_app
from tests.art_gen.test_art_gen import CAST, Fake, env  # noqa: F401  (env 是 fixture)
from tests.studio.test_studio import imported, sample_ops, wait_job

DRAFT = json.dumps({"headshot_prompt": "short hair, __hair_color__, brown eyes", "sprite_prompt": "work apron, arms crossed"})
HEADER = ["name", "prompt", "naturl_prompt", "negative_prompt", "category", "namezh", "namejp", "nameen"]


def make_http(tmp_path, replies=None):
    config, studio = imported(tmp_path)
    wild = tmp_path / "wild"
    wild.mkdir()
    (wild / "Hair Color.txt").write_text("silver hair\nblack hair\n", encoding="utf-8")
    (wild / "mood.txt").write_text("calm\n", encoding="utf-8")
    replay = ReplayLLM([json.dumps(sample_ops(None), ensure_ascii=False)] + (replies or []))
    fake = Fake()
    app = create_app(config, llm_factory=lambda: replay, comfy_factory=lambda: fake, wildcard_dir=wild)
    http = TestClient(app, base_url="http://127.0.0.1:7866")
    started = http.post("/api/books/demo/jobs", json={"kind": "annotate", "chapters": [1]}).json()
    assert wait_job(http, started["id"])["status"] == "done"
    cast_path = studio.work("demo") / "cast.json"
    cast = json.loads(cast_path.read_text(encoding="utf-8"))
    cast["characters"]["lao_zhang"]["role"] = "main"  # 主要配角：頭像可有各種表情（其餘角色只有 neutral）
    cast_path.write_text(json.dumps(cast, ensure_ascii=False), encoding="utf-8")
    return http, config, fake


def test_cards_list_save_roll(tmp_path):
    http, config, _ = make_http(tmp_path)
    rows = http.get("/api/books/demo/art/cards").json()["characters"]
    row = next(r for r in rows if r["id"] == "lao_zhang")
    assert row["has_card"] is False and "headshots" not in row and all(r["id"] != "player" for r in rows)
    assert row["card"]["expressions"] == ["neutral"] and row["sprite_scenes"] >= 0
    card = {"headshot_prompt": "short hair, __hair_color__", "sprite_prompt": "apron", "expressions": ["smile", "neutral"]}
    rolled = http.post("/api/books/demo/art/cards/roll", json={"card": {**card, "char_id": "lao_zhang"}}).json()["card"]
    assert rolled["headshot_prompt"] in ("short hair, silver hair", "short hair, black hair")
    saved = http.put("/api/books/demo/art/cards/lao_zhang", json={**card, "confirmed": True}).json()
    assert saved["confirmed"] is True and saved["expressions"] == ["neutral", "smile"]
    again = next(r for r in http.get("/api/books/demo/art/cards").json()["characters"] if r["id"] == "lao_zhang")
    assert again["has_card"] is True and again["card"]["sprite_prompt"] == "apron"
    # 存卡後素材清單裡每個勾選的頭像表情都有一列，加上固定的立繪
    kinds = [(a["kind"], a["expr"]) for a in again["assets"]]
    assert kinds == [("headshot", "neutral"), ("headshot", "smile"), ("sprite", "neutral")]
    assert http.put("/api/books/demo/art/cards/Bad%20Id", json=card).status_code == 400


def test_wildcards_list_and_enable(tmp_path):
    http, config, _ = make_http(tmp_path)
    names = [w["name"] for w in http.get("/api/books/demo/art/wildcards").json()["wildcards"]]
    assert names == ["hair_color", "mood"]
    saved = http.put("/api/books/demo/art/wildcards/enabled", json={"names": ["mood", "nosuch"]}).json()
    assert saved["enabled"] == ["mood"] and {w["name"]: w["enabled"] for w in saved["wildcards"]} == {"hair_color": False, "mood": True}
    rows = http.get("/api/books/demo/art/wildcards").json()["wildcards"]
    assert {w["name"]: w["enabled"] for w in rows} == {"hair_color": False, "mood": True}


def test_draft_returns_card_and_evidence(tmp_path):
    http, config, _ = make_http(tmp_path, replies=[DRAFT])
    r = http.post("/api/books/demo/art/cards/lao_zhang/draft", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["card"]["headshot_prompt"] == "short hair, __hair_color__, brown eyes" and body["card"]["sprite_prompt"] == "work apron, arms crossed"
    assert body["card"]["confirmed"] is False and isinstance(body["evidence"], list)
    assert http.post("/api/books/demo/art/cards/nosuch/draft", json={}).status_code == 400


def _ready_card(http, **extra):
    card = {"headshot_prompt": "short hair, silver hair", "sprite_prompt": "apron", "confirmed": True, **extra}
    assert http.put("/api/books/demo/art/cards/lao_zhang", json=card).status_code == 200


def _row(http):
    return next(r for r in http.get("/api/books/demo/art/cards").json()["characters"] if r["id"] == "lao_zhang")


def test_generate_per_character_headshots_and_sprite(tmp_path):
    http, config, fake = make_http(tmp_path)
    _ready_card(http, expressions=["neutral", "smile"])
    assert http.post("/api/books/demo/art/cards/generate", json={"char_id": "lao_zhang"}).status_code == 400   # 都沒勾
    assert http.post("/api/books/demo/art/cards/generate", json={"headshot": True}).status_code == 400        # 沒選角色
    assert http.post("/api/books/demo/art/cards/generate", json={"char_id": "lao_zhang", "headshot": True, "candidates": 99}).status_code == 400
    started = http.post("/api/books/demo/art/cards/generate", json={"char_id": "lao_zhang", "headshot": True, "sprite": True, "candidates": 1}).json()
    job = wait_job(http, started["id"])
    assert job["status"] == "done", job
    assert sorted(job["result"]["generated"]) == ["headshot_lao_zhang_neutral", "headshot_lao_zhang_smile", "sprite_lao_zhang_neutral"]
    assert not job["result"]["failed"]
    row = _row(http)
    assert [len(a["candidates"]) for a in row["assets"]] == [1, 1, 1]
    shot = row["assets"][0]["candidates"][0]
    img = http.get("/api/books/demo/art/file", params={"path": shot["file"]})
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
    # 頭像與立繪用同一個角色種子
    assert {c["seed"] for c in fake.calls} == {sprite_consistency.character_seed("lao_zhang", 0)}
    # 頭像提示詞用外型卡的頭像標籤；立繪是頭像＋立繪標籤
    positives = [c["positive"] for c in fake.calls]
    assert any("portrait" in p and "silver hair" in p and "apron" not in p for p in positives)
    assert any("solo" in p.split(".")[0] and "silver hair" in p and "apron" in p for p in positives)


def test_generate_only_headshot_when_sprite_not_ticked(tmp_path):
    http, config, fake = make_http(tmp_path)
    _ready_card(http)
    started = http.post("/api/books/demo/art/cards/generate", json={"char_id": "lao_zhang", "headshot": True, "candidates": 2}).json()
    job = wait_job(http, started["id"])
    assert job["result"]["generated"] == ["headshot_lao_zhang_neutral"] and len(fake.calls) == 2
    assert [len(a["candidates"]) for a in _row(http)["assets"] if a["kind"] == "sprite"] == [0]


def test_generate_without_card_reports_failure(tmp_path):
    http, config, _ = make_http(tmp_path)
    started = http.post("/api/books/demo/art/cards/generate", json={"char_id": "lao_zhang", "headshot": True}).json()
    job = wait_job(http, started["id"])
    assert job["status"] == "error" and "沒有外型卡" in json.dumps(job, ensure_ascii=False)


def test_generate_rolls_leftover_wildcards_stably(tmp_path):
    http, config, fake = make_http(tmp_path)
    _ready_card(http, headshot_prompt="short hair, __hair_color__")
    job = wait_job(http, http.post("/api/books/demo/art/cards/generate", json={"char_id": "lao_zhang", "headshot": True}).json()["id"])
    assert job["status"] == "done", job
    assert "__" not in fake.calls[0]["positive"] and ("silver hair" in fake.calls[0]["positive"] or "black hair" in fake.calls[0]["positive"])


def test_candidates_delete_protects_selected(tmp_path):
    http, config, fake = make_http(tmp_path)
    _ready_card(http)
    wait_job(http, http.post("/api/books/demo/art/cards/generate", json={"char_id": "lao_zhang", "headshot": True, "candidates": 3}).json()["id"])
    cands = _row(http)["assets"][0]["candidates"]
    assert len(cands) == 3 and not any(c["selected"] for c in cands)
    assert http.post("/api/books/demo/art/select", json={"asset_id": "headshot_lao_zhang_neutral", "name": cands[0]["name"]}).status_code == 200
    cands = _row(http)["assets"][0]["candidates"]
    assert [c["selected"] for c in cands] == [True, False, False]
    r = http.post("/api/books/demo/art/candidates/delete", json={"asset_id": "headshot_lao_zhang_neutral", "names": [c["name"] for c in cands]}).json()
    assert r["protected"] == [cands[0]["name"]] and sorted(r["deleted"]) == sorted(c["name"] for c in cands[1:])
    left = _row(http)["assets"][0]["candidates"]
    assert [c["name"] for c in left] == [cands[0]["name"]]
    assert http.post("/api/books/demo/art/candidates/delete", json={"asset_id": "nosuch", "names": ["1.png"]}).status_code == 400
    assert http.post("/api/books/demo/art/candidates/delete", json={"asset_id": "headshot_lao_zhang_neutral", "names": ["../x.png"]}).status_code == 400


def test_headshot_files_only_under_headshots(tmp_path):
    http, config, _ = make_http(tmp_path)
    for bad in ("art/style.json", "art/headshots/../style.json", "art/appearance/lao_zhang.json"):
        assert http.get("/api/books/demo/art/file", params={"path": bad}).status_code == 400


def test_styles_import_apply_clear(tmp_path):
    http, config, _ = make_http(tmp_path)
    assert http.get("/api/books/demo/art/styles").json() == {"styles": [], "current": ""}
    assert http.post("/api/books/demo/art/styles/import", json={"path": str(tmp_path / "x.txt")}).status_code == 400
    xlsx = tmp_path / "s.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(HEADER)
    wb.active.append(["甲/a/A", "oil painting, warm palette", "A warm oil look.", "blurry", "內文插圖風格", "甲風", "", "Style A"])
    wb.save(xlsx)
    assert http.post("/api/books/demo/art/styles/import", json={"path": str(xlsx)}).json() == {"count": 1}
    data = http.get("/api/books/demo/art/styles").json()
    style_id = data["styles"][0]["style_id"]
    applied = http.post("/api/books/demo/art/styles/apply", json={"style_id": style_id}).json()
    assert applied["style_id"] == style_id
    assert http.get("/api/books/demo/art/styles").json()["current"] == style_id
    assert http.post("/api/books/demo/art/styles/apply", json={"style_id": "nosuch"}).status_code == 400
    assert http.post("/api/books/demo/art/styles/clear").json() == {"style_id": ""}
    assert http.get("/api/books/demo/art/styles").json()["current"] == ""


# ---------------------------------------------------------------- art_gen：立繪種子與對齊

def _figure(box):
    image = Image.new("RGB", (400, 800), (255, 255, 255))
    ImageDraw.Draw(image).rectangle(box, fill=(190, 40, 40))
    return image


def test_sprite_seed_is_shared_per_character():
    a = {"asset_id": "sprite_ellie_neutral", "kind": "sprite", "file": "assets/sprite/ellie_neutral.png"}
    b = {"asset_id": "sprite_ellie_smile", "kind": "sprite", "file": "assets/sprite/ellie_smile.png"}
    bg = {"asset_id": "bg_yard_dawn", "kind": "bg", "file": "assets/bg/bg_yard_dawn.png"}
    assert art_gen._seed_for(a, 0) == art_gen._seed_for(b, 0) == sprite_consistency.character_seed("ellie", 0)
    assert art_gen._seed_for(a, 1) != art_gen._seed_for(a, 0)
    assert art_gen._seed_for(bg, 2) == art_prompts.seed_for("bg_yard_dawn", 2)


def test_todo_order_puts_backgrounds_first_then_sprites_by_character():
    todo = [{"asset_id": "s2", "kind": "sprite", "file": "assets/sprite/zed_smile.png"},
            {"asset_id": "b", "kind": "bg", "file": "assets/bg/b.png"},
            {"asset_id": "s1", "kind": "sprite", "file": "assets/sprite/amy_sad.png"},
            {"asset_id": "s0", "kind": "sprite", "file": "assets/sprite/amy_neutral.png"}]
    assert [a["asset_id"] for a in art_gen._order_todo(todo)] == ["b", "s0", "s1", "s2"]


def test_select_sprite_aligns_other_expressions_of_same_character(env):  # noqa: F811
    config, work = env
    manifest = art_gen.load_manifest(work)
    template = next(a for a in manifest["assets"] if a["asset_id"] == "sprite_ellie_neutral")
    smile = dict(template, asset_id="sprite_ellie_smile", file="assets/sprite/ellie_smile.png", status="planned")
    manifest["assets"].append(smile)
    (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for asset_id, box in (("sprite_ellie_neutral", [100, 100, 300, 790]), ("sprite_ellie_smile", [150, 300, 250, 790])):
        folder = art_gen.candidate_dir(work, asset_id)
        folder.mkdir(parents=True, exist_ok=True)
        _figure(box).save(folder / "7.png")
    art_gen.select_candidate(config, work, "sprite_ellie_neutral", "7.png")
    art_gen.select_candidate(config, work, "sprite_ellie_smile", "7.png")
    sizes = {}
    for expr in ("neutral", "smile"):
        image = Image.open(work / f"assets/sprite/ellie_{expr}.png").convert("RGBA")
        sizes[expr] = (image.size, image.getchannel("A").getbbox())
    assert sizes["neutral"][0] == sizes["smile"][0]
    assert sizes["neutral"][1][3] == sizes["smile"][1][3]  # 腳底線一致
    # 笑臉比較窄、同一比例縮放後仍比 neutral 窄
    assert (sizes["smile"][1][2] - sizes["smile"][1][0]) < (sizes["neutral"][1][2] - sizes["neutral"][1][0])


def test_ui_files_are_not_cached_by_browser(tmp_path):
    http, config, _ = make_http(tmp_path)
    for path in ("/", "/ui/page_assets.js"):
        assert http.get(path).headers["cache-control"] == "no-cache"
    assert "cache-control" not in http.get("/api/books/demo/art/cards").headers


def test_styles_upload_xlsx(tmp_path):
    http, config, _ = make_http(tmp_path)
    xlsx = tmp_path / "up.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(HEADER)
    wb.active.append(["甲/a/A", "oil painting, warm palette", "A warm oil look.", "blurry", "內文插圖風格", "甲風", "", "Style A"])
    wb.save(xlsx)
    ok = http.post("/api/books/demo/art/styles/upload", content=xlsx.read_bytes(), headers={"X-Filename": "%E7%95%AB%E9%A2%A8.xlsx"})
    assert ok.json() == {"count": 1}
    assert len(http.get("/api/books/demo/art/styles").json()["styles"]) == 1
    assert not (config.project_root / "config" / "_upload_styles.xlsx").exists()
    assert http.post("/api/books/demo/art/styles/upload", content=b"x", headers={"X-Filename": "a.txt"}).status_code == 400
    assert http.post("/api/books/demo/art/styles/upload", content=b"", headers={"X-Filename": "a.xlsx"}).status_code == 400
    assert http.post("/api/books/demo/art/styles/upload", content=b"notxlsx", headers={"X-Filename": "a.xlsx"}).status_code == 400


def test_generate_again_adds_new_candidates_even_if_some_exist(tmp_path):
    http, config, fake = make_http(tmp_path)
    _ready_card(http)
    for _ in range(2):
        job = wait_job(http, http.post("/api/books/demo/art/cards/generate", json={"char_id": "lao_zhang", "headshot": True, "sprite": True, "candidates": 1}).json()["id"])
        assert job["status"] == "done", job
    row = _row(http)
    assert [len(a["candidates"]) for a in row["assets"]] == [2, 2]
    assert len(fake.calls) == 4


def test_asset_list_groups_headshots_and_sprites_per_character(tmp_path):
    http, config, _ = make_http(tmp_path)
    _ready_card(http, expressions=["neutral", "smile"])
    ids = [a["asset_id"] for a in http.get("/api/books/demo/art/assets").json()["assets"]]
    assert ids.index("headshot_lao_zhang_neutral") + 1 == ids.index("headshot_lao_zhang_smile")
    assert ids.index("headshot_lao_zhang_smile") + 1 == ids.index("sprite_lao_zhang_neutral")
    assert all(not i.startswith(("headshot_", "sprite_")) for i in ids[:ids.index("headshot_lao_zhang_neutral")])


def test_delete_all_candidates_resets_status_to_planned(tmp_path):
    http, config, fake = make_http(tmp_path)
    _ready_card(http)
    wait_job(http, http.post("/api/books/demo/art/cards/generate", json={"char_id": "lao_zhang", "headshot": True, "candidates": 2}).json()["id"])
    row = _row(http)["assets"][0]
    assert row["status"] == "generated"
    http.post("/api/books/demo/art/candidates/delete", json={"asset_id": "headshot_lao_zhang_neutral", "names": [c["name"] for c in row["candidates"]]})
    after = _row(http)["assets"][0]
    assert after["candidates"] == [] and after["status"] == "planned"
    listed = http.get("/api/books/demo/art/assets").json()["assets"]
    assert next(a for a in listed if a["asset_id"] == "headshot_lao_zhang_neutral")["char_id"] == "lao_zhang"


def test_unchecked_expression_is_pruned_from_manifest(tmp_path):
    http, config, fake = make_http(tmp_path)
    _ready_card(http, expressions=["neutral", "smile"])
    ids = [a["asset_id"] for a in http.get("/api/books/demo/art/assets").json()["assets"]]
    assert "headshot_lao_zhang_smile" in ids
    _ready_card(http, expressions=["neutral"])
    ids = [a["asset_id"] for a in http.get("/api/books/demo/art/assets").json()["assets"]]
    assert "headshot_lao_zhang_smile" not in ids and "headshot_lao_zhang_neutral" in ids


def test_supporting_character_only_gets_neutral_even_if_card_checks_more(tmp_path):
    http, config, fake = make_http(tmp_path)
    cast_path = next(config.works_dir.rglob("cast.json"))
    cast = json.loads(cast_path.read_text(encoding="utf-8"))
    cast["characters"]["lao_zhang"]["role"] = "supporting"
    cast_path.write_text(json.dumps(cast, ensure_ascii=False), encoding="utf-8")
    _ready_card(http, expressions=["neutral", "smile"])
    ids = [a["asset_id"] for a in http.get("/api/books/demo/art/assets").json()["assets"]]
    assert "headshot_lao_zhang_neutral" in ids and "headshot_lao_zhang_smile" not in ids
    started = http.post("/api/books/demo/art/cards/generate", json={"char_id": "lao_zhang", "headshot": True, "sprite": True, "candidates": 1}).json()
    job = wait_job(http, started["id"])
    assert sorted(job["result"]["generated"]) == ["headshot_lao_zhang_neutral", "sprite_lao_zhang_neutral"]
