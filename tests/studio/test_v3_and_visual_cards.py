"""階段 A（A1–A5）：novel-studio v3 書庫讀取、角色視覺卡無損同步與專屬表情提示詞測試。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from t2vn import appearance_card, art_cards, art_prompts, comic, source_reader, visual_card_import
from t2vn.config import Config
from t2vn.llm import ReplayLLM
from t2vn.schema_check import validate_data
from t2vn.server import create_app
from t2vn.studio import Studio


def _make_v3_book(library: Path, name: str = "demo_v3") -> Path:
    book = library / name
    (book / "manuscript").mkdir(parents=True)
    (book / "outline" / "chapters").mkdir(parents=True)
    (book / "bible" / "characters").mkdir(parents=True)
    (book / "bible" / "visual-cards").mkdir(parents=True)

    (book / "book.json").write_text(
        json.dumps({"schema_version": 3, "title": "霧港物語", "lang": "zh-TW"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (book / "manuscript" / "ch-0001.md").write_text(
        "# 港口清晨\n\n霧氣漫過防波堤。\n\n「早安，沙耶。」\n",
        encoding="utf-8",
    )
    (book / "outline" / "chapters" / "ch-0001.md").write_text(
        "---\n"
        "type: chapter-card\n"
        "chapter: 1\n"
        "title: 港口清晨\n"
        "volume: 第一卷\n"
        "unit: 開篇\n"
        "status: written\n"
        "---\n\n"
        "## 情節點\n- 兩人於港口會合\n\n"
        "## 出場角色\n- 桐生沙耶\n",
        encoding="utf-8",
    )
    (book / "bible" / "characters" / "桐生沙耶.md").write_text(
        "---\n"
        "type： character\n"
        "name: 桐生沙耶\n"
        "aliases: [沙耶, 霧港技師]\n"
        "role: protagonist\n"
        "first_appearance: 1\n"
        "status: canon\n"
        "---\n\n"
        "## 一句話\n港口機械師。\n\n"
        "## 繪圖提示\n由視覺卡同步。\n",
        encoding="utf-8",
    )
    (book / "bible" / "visual-cards" / "kiryuu-saya.md").write_text(
        "---\n"
        'id: "kiryuu-saya"\n'
        'name: "桐生沙耶"\n'
        'aliases: ["沙耶", "小沙"]\n'
        'status: "confirmed"\n'
        'source: "bible/characters/桐生沙耶.md"\n'
        "sections:\n"
        '  head: ["20-year-old woman", "short black hair", "amber eyes"]\n'
        '  body: ["slim build"]\n'
        '  upper: ["light tan skin"]\n'
        "  lower: []\n"
        '  accessory: ["brass goggles on forehead", "leather tool belt"]\n'
        "outfits:\n"
        "  default:\n"
        '    top: ["navy mechanic jacket", "white tank top"]\n'
        '    bottom: ["dark cargo pants", "brown work boots"]\n'
        "    set: []\n"
        '    notes: "日常工作服"\n'
        "vn:\n"
        '  avatar_accessories: ["brass goggles on forehead"]\n'
        '  sprite_pose: "standing straight, hands on hips"\n'
        "  expressions:\n"
        '    neutral: "calm amber gaze, relaxed closed mouth"\n'
        '    happy: "bright confident grin, sparkling eyes"\n'
        '    angry: "sharp glare, clenched jaw"\n'
        "---\n\n"
        "左眉有細小舊疤。\n",
        encoding="utf-8",
    )
    return book


def test_source_reader_supports_v3_and_fullwidth_colon(tmp_path: Path) -> None:
    book = _make_v3_book(tmp_path)
    assert source_reader.detect_layout(book) == "v3"
    outlines = source_reader.read_all_outlines(book)
    assert len(outlines) == 1 and outlines[0].chapter_no == 1 and outlines[0].title == "港口清晨"
    chars = source_reader.read_characters(book)
    assert len(chars) == 1 and chars[0].name == "桐生沙耶" and chars[0].role == "protagonist"
    assert chars[0].first_appearance == 1 and "art_prompt" in chars[0].sections


def test_import_v3_library_book_syncs_visual_cards_and_expressions(tmp_path: Path) -> None:
    lib = tmp_path / "library"
    works = tmp_path / "works"
    lib.mkdir()
    works.mkdir()
    _make_v3_book(lib, "mist_port")

    cfg = Config(project_root=tmp_path, works_dir_override=works, book_library=lib)
    studio = Studio(cfg)

    candidates = studio.library_candidates()
    assert len(candidates) == 1
    assert candidates[0]["dir_name"] == "mist_port"
    assert candidates[0]["layout"] == "v3"
    assert candidates[0]["title"] == "霧港物語"
    assert candidates[0]["visual_cards"] == 1

    imported_res = studio.import_library_book("mist_port", "mist_port", "", lang="zh-TW")
    assert imported_res["chapters"] == 1
    vc = imported_res["visual_cards"]
    assert vc["created"] == ["kiryuu_saya"]
    assert vc["renamed"] == {"kiryuu-saya": "kiryuu_saya"}
    assert vc["cast_added"] == ["kiryuu_saya"]
    assert vc["errors"] == []

    work = works / "mist_port"
    cast_doc = json.loads((work / "cast.json").read_text(encoding="utf-8"))
    assert validate_data(cast_doc, "cast") == []
    assert cast_doc["characters"]["kiryuu_saya"]["role"] == "protagonist"
    assert cast_doc["characters"]["kiryuu_saya"]["first_chapter"] == "ch001"
    assert vc["library_characters"] == 1 and vc["missing_visual_cards"] == []
    assert "小沙" in cast_doc["characters"]["kiryuu_saya"]["aliases"]

    card_path = art_cards.card_path(work, "kiryuu_saya")
    card_raw = json.loads(card_path.read_text(encoding="utf-8"))
    assert validate_data(card_raw, "appearance_card") == []
    assert card_raw["confirmed"] is True
    assert "20-year-old woman" in card_raw["headshot_prompt"]
    assert "brass goggles on forehead" in card_raw["headshot_prompt"]
    assert "navy mechanic jacket" in card_raw["sprite_prompt"]
    assert "standing straight, hands on hips" in card_raw["sprite_prompt"]
    assert card_raw["expression_prompts"]["smile"] == "bright confident grin, sparkling eyes"
    assert "happy" not in card_raw["expression_prompts"]
    assert card_raw["t2c"]["sections"]["head"][0] == "20-year-old woman"
    assert "notes" not in card_raw["t2c"]["outfits"]["default"]
    assert card_raw["t2c"]["source_sha1"] == appearance_card.prompt_fingerprint(card_raw)
    assert card_raw["notes"] == "左眉有細小舊疤。"

    # 出圖提示詞優先採用角色專屬表情描述
    style = json.loads((work / "art" / "style.json").read_text(encoding="utf-8"))
    hp = art_prompts.headshot_prompt("kiryuu_saya", "smile", style)
    assert "bright confident grin" in hp["positive"]
    sp = art_prompts.sprite_prompt("kiryuu_saya", "angry", style)
    assert "clenched jaw" in sp["positive"]

    # 透過介面編輯外型卡時不帶 expression_prompts 也不會洗掉舊值
    saved = art_cards.save_card(
        cfg.assert_writable,
        work,
        "kiryuu_saya",
        {
            "char_id": "kiryuu_saya",
            "confirmed": True,
            "headshot_prompt": card_raw["headshot_prompt"],
            "sprite_prompt": card_raw["sprite_prompt"],
            "expressions": ["neutral", "smile"],
            "notes": "已確認",
        },
    )
    assert saved["expression_prompts"]["smile"] == "bright confident grin, sparkling eyes"

    # 漫畫線直接從匯入的外型卡跑 bible_check 應該 0 error
    check = comic.appearance_card_check("mist_port", "kiryuu_saya", cfg)
    assert [i for i in check["issues"] if i.get("severity") == "error"] == [], check["issues"]

    # 測試 HTTP API：預設不覆蓋（skip），overwrite=True 會備份並保留已勾選表情
    http = TestClient(create_app(cfg, llm_factory=lambda: ReplayLLM([])), base_url="http://127.0.0.1:7866")
    cards_resp = http.get("/api/books/mist_port/art/cards").json()
    assert cards_resp["has_library"] is True

    r_skip = http.post("/api/books/mist_port/art/cards/sync-library", json={"overwrite": False}).json()
    assert r_skip["skipped"] == ["kiryuu_saya"] and r_skip["updated"] == []

    r_over = http.post("/api/books/mist_port/art/cards/sync-library", json={"overwrite": True}).json()
    assert r_over["updated"] == ["kiryuu_saya"]
    reloaded = appearance_card.load_card(card_path)
    assert reloaded["expressions"] == ["neutral", "smile"]
    assert len(list((card_path.parent / "_history").glob("kiryuu_saya_*.json"))) == 1


def test_normalize_char_id_rules() -> None:
    assert visual_card_import.normalize_char_id("kiryuu-saya") == "kiryuu_saya"
    assert visual_card_import.normalize_char_id("01_guard") == "c_01_guard"
    assert visual_card_import.normalize_char_id("A") == "a_"


def test_auto_book_id_and_unified_comic_import(tmp_path: Path) -> None:
    lib = tmp_path / "library"
    works = tmp_path / "works"
    lib.mkdir()
    works.mkdir()
    _make_v3_book(lib, "霧港物語")

    cfg = Config(project_root=tmp_path, works_dir_override=works, book_library=lib)
    http = TestClient(create_app(cfg, llm_factory=lambda: ReplayLLM([])), base_url="http://127.0.0.1:7866")

    # 空白 book_id + init_comic=True：自動產生 book_id、同步視覺卡並建好預設漫畫專案 main
    res = http.post("/api/import/library", json={"dir_name": "霧港物語", "init_comic": True})
    assert res.status_code == 200, res.text
    body = res.json()
    bid = body["book_id"]
    assert bid.startswith("book_")
    assert body["comic_project"] == "main"
    assert body["visual_cards"]["created"] == ["kiryuu_saya"]

    # 漫畫專案已存在且角色卡已同步進 bible
    projects = http.get(f"/api/books/{bid}/comic").json()["projects"]
    assert [p["project_id"] for p in projects] == ["main"]
    assert projects[0]["title"] == "霧港物語"
    assert (works / bid / "comic" / "main" / "bible" / "characters" / "kiryuu_saya.md").is_file()

    # 再按一次建立（不傳 project_id 與 title）會自動編號 main_2 並帶入書名
    second = http.post(f"/api/books/{bid}/comic", json={})
    assert second.status_code == 200, second.text
    assert second.json()["project_id"] == "main_2"
    assert second.json()["title"] == "霧港物語"


def test_structured_ai_draft_with_world_context_and_t2c() -> None:
    from t2vn import appearance_draft

    prompt = appearance_draft.build_prompt(
        {"name": "艾爾瑪", "appearance": "藥師"},
        [],
        world_context="中世紀奇幻邊境村落，無現代衣物。",
    )
    assert "中世紀奇幻邊境村落" in prompt
    assert "outfits" in prompt and "color or light/dark tone" in prompt

    reply = json.dumps({
        "headshot_prompt": "19-year-old woman, chestnut brown hair, hazel eyes",
        "sprite_prompt": "slim build, off-white linen blouse, dark green skirt, brown leather boots",
        "t2c": {
            "sections": {"head": ["19-year-old woman", "chestnut brown hair", "hazel eyes"], "body": ["slim build"]},
            "outfits": {"default": {"top": ["off-white linen blouse"], "bottom": ["dark green skirt", "brown leather boots"]}},
        },
    })
    parsed, warns = appearance_draft.parse_reply(reply)
    assert warns == []
    assert parsed["t2c"]["sections"]["head"][0] == "19-year-old woman"
    assert parsed["t2c"]["outfits"]["default"]["bottom"] == ["dark green skirt", "brown leather boots"]
    assert parsed["t2c"]["source_sha1"] == appearance_card.prompt_fingerprint(parsed)


def test_sync_world_locations_api(tmp_path: Path) -> None:
    lib = tmp_path / "library"
    works = tmp_path / "works"
    lib.mkdir()
    works.mkdir()
    book = _make_v3_book(lib, "mist_port")
    world_dir = book / "bible" / "world"
    world_dir.mkdir(parents=True)
    (world_dir / "灰谷村.md").write_text(
        "---\ntype: world\nname: 灰谷村\n---\n\n## 一句話\n石牆與鍛冶場的邊境村落。\n\n"
        "## 繪圖提示\ntags: stone wall, medieval village, no humans\ndescription: A quiet frontier village with low stone walls.\n",
        encoding="utf-8",
    )
    (world_dir / "歷史年表.md").write_text(
        "---\ntype: world\nname: 歷史年表\ncategory: history\n---\n\n## 一句話\n紀年表。\n",
        encoding="utf-8",
    )

    cfg = Config(project_root=tmp_path, works_dir_override=works, book_library=lib)
    http = TestClient(create_app(cfg, llm_factory=lambda: ReplayLLM([])), base_url="http://127.0.0.1:7866")
    res = http.post("/api/import/library", json={"dir_name": "mist_port"})
    bid = res.json()["book_id"]

    b0 = http.get(f"/api/books/{bid}/bible").json()
    assert b0["has_library"] is True and b0["locations"] == []

    sync_res = http.post(f"/api/books/{bid}/bible/sync-library", json={}).json()
    assert len(sync_res["created"]) == 1
    assert "歷史年表" in sync_res["skipped"]

    b1 = http.get(f"/api/books/{bid}/bible").json()
    assert len(b1["locations"]) == 1
    assert b1["locations"][0]["label"] == "灰谷村"
    loc_id = b1["locations"][0]["id"]
    style = json.loads((works / bid / "art" / "style.json").read_text(encoding="utf-8"))
    assert "no humans" in style["locations"][loc_id]["tags"]

    # 模擬 AI 標註產生了另一個英文 ID haiya_village，透過合併 API 把 loc_id 的名稱、備註、外觀與場景引用整併過來
    scenes_dir = works / bid / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    (scenes_dir / "ch001.json").write_text(
        json.dumps({
            "schema_version": "0.1", "book_id": bid, "chapter_id": "ch001",
            "scenes": [
                {"scene_id": "ch001_sc01", "title": "灰谷村の入口", "location_id": "haiya_village", "time_of_day": "day", "bgm": "calm", "cast": [], "lines": []},
                {"scene_id": "ch001_sc02", "title": "灰谷村の鍛冶場", "location_id": loc_id, "time_of_day": "day", "bgm": "calm", "cast": [], "lines": []},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    b_before = http.get(f"/api/books/{bid}/bible").json()
    haiya = next(r for r in b_before["locations"] if r["id"] == "haiya_village")
    assert "灰谷村の入口" in (haiya.get("hint") or "")

    merged = http.post(f"/api/books/{bid}/bible/locations/merge", json={"target": "haiya_village", "sources": [loc_id]}).json()
    assert merged["target"] == "haiya_village" and merged["scenes_updated"] == 1
    assert merged["entry"]["label"] == "灰谷村"
    assert "鍛冶場" in merged["entry"]["note"]
    b_after = http.get(f"/api/books/{bid}/bible").json()
    assert [r["id"] for r in b_after["locations"]] == ["haiya_village"]
    assert b_after["locations"][0]["count"] == 2
    style_after = json.loads((works / bid / "art" / "style.json").read_text(encoding="utf-8"))
    assert loc_id not in style_after["locations"]
    assert "no humans" in style_after["locations"]["haiya_village"]["tags"]

    # 本書時代與服飾基調讀寫（PUT /bible/world-context）
    wc_put = http.put(f"/api/books/{bid}/bible/world-context", json={"world_context": "中世紀奇幻，麻布與革靴為主"}).json()
    assert wc_put["world_context"] == "中世紀奇幻，麻布與革靴為主"
    b_wc = http.get(f"/api/books/{bid}/bible").json()
    assert b_wc["world_context"] == "中世紀奇幻，麻布與革靴為主"
