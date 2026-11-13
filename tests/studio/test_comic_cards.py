"""漫畫線資源共用（D22–D26）：外觀卡 t2c 子物件、bible 卡轉換與檢查。

驗收分兩層：程式層（檢查器如實回報、地點卡寫得出來）與資料層（補完 t2c 之後 0 error）。
這支測試只驗程式層，用的是合成資料。
"""
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from t2vn import appearance_card, comic
from t2vn.config import load_config
from t2vn.server import create_app
from vendor.t2c import bible_check as bc

STYLE = {
    "characters": {},
    "locations": {
        "warehouse": {
            "tags": "no humans, warehouse",
            "description": "An old wooden warehouse with a rusted iron roof, stacked crates along the walls, "
                           "dust in the air and a single bare bulb hanging from a beam.",
        },
        "yard": {
            "tags": "no humans, yard",
            "description": "A wide gravel yard with figures standing near the gate.",   # 有人 → error
        },
    },
}

MANIFEST = {
    "schema_version": "0.1", "book_id": "demo",
    "assets": [
        {"asset_id": "bg_warehouse_day", "kind": "bg", "names": {"zh-TW": "倉庫・白天"}},
        {"asset_id": "bg_yard_dawn", "kind": "bg", "names": {"zh-TW": "空地・清晨"}},
    ],
}

CAST = {
    "schema_version": "0.1", "book_id": "demo",
    "characters": {
        "ellie": {"names": {"zh-TW": "艾莉"}, "aliases": ["艾莉姐"]},
        "alec": {"names": {"zh-TW": "亞歷"}, "aliases": []},
        "nobody": {"names": {"zh-TW": "無名"}, "aliases": []},
        "player": {"names": {"zh-TW": "主角（內心）"}, "aliases": [], "role": "player"},
    },
}

T2C_OK = {
    "sections": {"head": ["a 17-year-old girl", "long black hair", "brown eyes"],
                 "body": ["a slim build"]},
    "outfits": {"default": {"set": ["a navy school uniform"]}},
}


@pytest.fixture()
def env(tmp_path):
    config = dataclasses.replace(load_config(), works_dir_override=tmp_path)
    work = tmp_path / "demo"
    (work / "art" / "appearance").mkdir(parents=True)
    (work / "book.json").write_text(json.dumps({"book_id": "demo"}), encoding="utf-8")
    (work / "cast.json").write_text(json.dumps(CAST, ensure_ascii=False), encoding="utf-8")
    (work / "art" / "style.json").write_text(json.dumps(STYLE, ensure_ascii=False), encoding="utf-8")
    (work / "manifest.json").write_text(json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    (work / "art" / "appearance" / "ellie.json").write_text(json.dumps({
        "char_id": "ellie", "confirmed": True,
        "headshot_prompt": "long black hair, brown eyes",
        "sprite_prompt": "school uniform, sneakers",
        "t2c": T2C_OK,
    }, ensure_ascii=False), encoding="utf-8")
    (work / "art" / "appearance" / "alec.json").write_text(json.dumps({
        "char_id": "alec", "confirmed": True,
        "headshot_prompt": "short hair, black hair, brown eyes",
        "sprite_prompt": "grey shirt, brown pants, standing",
    }, ensure_ascii=False), encoding="utf-8")
    comic.create_project("demo", "ch001", "大夜班", config=config)
    return config, work, work / "comic" / "ch001"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_location_cards_written_and_checked(env):
    config, _, root = env
    report = comic.bible_cards("demo", "ch001", config=config)
    assert "warehouse" in report["written"]
    assert "yard" in report["drafts"]
    card = root / "bible" / "locations" / "warehouse.md"
    assert card.is_file()
    text = _read(card)
    assert "倉庫" in text                       # 中文名來自 manifest 的 bg 名稱
    assert "look_en" in text
    drafted = root / "bible" / "drafts" / "locations" / "yard.md"
    assert drafted.is_file() and "figures" in _read(drafted)


def test_character_with_t2c_goes_final(env):
    config, _, root = env
    report = comic.bible_cards("demo", "ch001", config=config)
    assert "ellie" in report["written"]
    card = root / "bible" / "characters" / "ellie.md"
    text = _read(card)
    assert "id: ellie" in text and "sections:" in text
    assert "a navy school uniform" in text
    assert "sneakers" not in text               # 不再把 sprite_prompt 抄成 look_en
    meta_id = [line for line in text.splitlines() if line.startswith("id:")]
    assert meta_id == ["id: ellie"]              # id＝檔名＝cast key
    assert card.stem == "ellie"


def test_character_without_t2c_goes_draft(env):
    config, _, root = env
    report = comic.bible_cards("demo", "ch001", config=config)
    assert "alec" in report["drafts"]
    drafted = root / "bible" / "drafts" / "characters" / "alec.md"
    assert drafted.is_file()
    assert not (root / "bible" / "characters" / "alec.md").exists()
    assert any(item["card"] == "alec" and "t2c" in item["message"] for item in report["issues"])


def test_placeholder_card_written_and_listed(env):
    config, _, root = env
    report = comic.bible_cards("demo", "ch001", config=config)
    assert report["placeholder"] == ["nobody"]
    text = _read(root / "bible" / "characters" / "nobody.md")
    assert comic.PLACEHOLDER_LOOK in text
    marker = json.loads(_read(root / comic.PLACEHOLDERS_FILE))
    assert marker["placeholder"] == ["nobody"]
    assert marker["drafts"] == report["drafts"]


def test_placeholder_card_is_flagged_on_the_card(env):
    """佔位卡自己帶 `placeholder: true`：bible_status 才分得出來它是「還沒畫」不是「不合規」。"""
    config, _, root = env
    comic.bible_cards("demo", "ch001", config=config)
    text = _read(root / "bible" / "characters" / "nobody.md")
    assert "placeholder: true" in text
    assert "look_en" in text                   # 佔位卡沒有 sections
    assert "placeholder" not in _read(root / "bible" / "characters" / "ellie.md")


def test_player_is_skipped(env):
    """player 是心聲 id（art_cards.SKIP_CHARACTERS、annotate 不送進分鏡），不寫角色卡。"""
    config, _, root = env
    report = comic.bible_cards("demo", "ch001", config=config)
    assert report["skipped_characters"] == ["player"]
    assert "player" not in report["written"] + report["drafts"] + report["placeholder"]
    assert not (root / "bible" / "characters" / "player.md").exists()
    marker = json.loads(_read(root / comic.PLACEHOLDERS_FILE))
    assert marker["skipped_characters"] == ["player"]


def test_bible_status_marks_placeholder(env):
    config, _, _ = env
    comic.bible_cards("demo", "ch001", config=config)
    rows = {row["id"]: row for row in comic.bible_status("demo", "ch001", config)["characters"]}
    assert rows["nobody"]["placeholder"] is True
    assert rows["nobody"]["errors"]                  # 佔位卡本身不合規，仍然看得見
    assert rows["ellie"]["placeholder"] is False and rows["ellie"]["errors"] == []
    assert "player" not in rows


def test_clothing_in_sections_is_blocked(env):
    config, work, root = env
    card_path = work / "art" / "appearance" / "ellie.json"
    data = json.loads(_read(card_path))
    data["t2c"]["sections"]["head"].append("a grey shirt")
    card_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    report = comic.bible_cards("demo", "ch001", config=config)
    assert "ellie" in report["drafts"]
    assert any("shirt" in item["message"] for item in report["issues"])


def test_stale_fingerprint_reported(env):
    config, work, _ = env
    card_path = work / "art" / "appearance" / "ellie.json"
    data = json.loads(_read(card_path))
    data["t2c"]["source_sha1"] = hashlib.sha1(b"old prompts").hexdigest()
    card_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    report = comic.bible_cards("demo", "ch001", config=config)
    assert report["stale"] == ["ellie"]
    assert "ellie" in report["written"]          # 過期只提醒，不擋寫入


def test_second_run_keeps_cards(env):
    config, _, _ = env
    comic.bible_cards("demo", "ch001", config=config)
    again = comic.bible_cards("demo", "ch001", config=config)
    assert again["written"] == [] and again["drafts"] == []
    assert set(again["kept"]) == {"ellie", "alec", "nobody", "warehouse", "yard"}


def test_bible_status_matches_check(env):
    config, _, root = env
    comic.bible_cards("demo", "ch001", config=config)
    status = comic.bible_status("demo", "ch001", config)
    rows = {row["id"]: row for row in status["characters"]}
    assert rows["ellie"]["errors"] == []
    assert rows["ellie"]["draft"] is False
    assert rows["alec"]["draft"] is True and rows["alec"]["errors"]
    text = _read(Path(rows["alec"]["path"]))
    assert "待修" in text


def test_bible_endpoints(env):
    config, _, _ = env
    app = create_app(config, llm_factory=lambda: None)
    http = TestClient(app, base_url="http://127.0.0.1:7866")
    first = http.post("/api/books/demo/comic/ch001/bible", json={})
    assert first.status_code == 200
    body = first.json()
    assert "ellie" in body["written"] and "alec" in body["drafts"]
    second = http.post("/api/books/demo/comic/ch001/bible", json={})
    assert second.json()["written"] == []          # 第二次不覆蓋
    listed = http.get("/api/books/demo/comic/ch001/bible")
    assert listed.status_code == 200
    assert {row["id"] for row in listed.json()["characters"]} >= {"ellie", "alec"}


def test_appearance_check_reports_itemized_issues(env):
    config, _, _ = env
    ellie = comic.appearance_card_check("demo", "ellie", config)
    assert ellie["source"] == "t2c" and ellie["stale"] is False
    assert ellie["issues"] == []
    # 粗拆初稿由提示詞算，不讀 t2c 人工值
    assert ellie["draft"]["sections"]["head"] == ["long black hair", "brown eyes"]
    alec = comic.appearance_card_check("demo", "alec", config)
    assert alec["source"] == "derived"
    assert len(alec["issues"]) >= 2                      # 缺性別、缺年齡
    messages = [item["message"] for item in alec["issues"]]
    assert len(set(messages)) == len(messages)           # 逐條：一個問題一則，不是串成一句
    assert any("性別" in item["message"] for item in alec["issues"])
    assert any("年齡" in item["message"] for item in alec["issues"])
    assert all(item["level"] in {"error", "warn"} for item in alec["issues"])


def test_appearance_check_marks_stale_fingerprint(env):
    config, work, _ = env
    path = work / "art" / "appearance" / "ellie.json"
    data = json.loads(_read(path))
    data["t2c"]["source_sha1"] = hashlib.sha1(b"old prompts").hexdigest()
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    ellie = comic.appearance_card_check("demo", "ellie", config)
    assert ellie["stale"] is True
    assert ellie["current_sha1"] != data["t2c"]["source_sha1"]
    assert ellie["issues"] == []                          # 過期只提醒，不擋檢查


def test_appearance_check_placeholder(env):
    config, _, _ = env
    nobody = comic.appearance_card_check("demo", "nobody", config)
    assert nobody["source"] == "placeholder"
    assert nobody["issues"] == [] and nobody["draft"] == {}


def test_appearance_check_endpoint(env):
    config, _, _ = env
    http = TestClient(create_app(config, llm_factory=lambda: None), base_url="http://127.0.0.1:7866")
    got = http.get("/api/books/demo/art/cards/ellie/check")
    assert got.status_code == 200
    body = got.json()
    assert body["char_id"] == "ellie" and body["issues"] == []
    assert body["draft"]["sections"]["head"] == ["long black hair", "brown eyes"]
    missing = http.get("/api/books/demo/art/cards/nobody/check")
    assert missing.status_code == 200 and missing.json()["source"] == "placeholder"


# ---------------------------------------------------------------- 漫畫頁的 t2c 編輯器

NEW_T2C = {
    "sections": {"head": ["a 17-year-old girl", "long black hair", "brown eyes"],
                 "body": ["a slim build"]},
    "outfits": {"default": {"top": ["a white blouse"], "set": ["a navy school uniform"]}},
}

BIBLE_T2C = "/api/books/demo/comic/ch001/bible/ellie/t2c"


def _appearance(work: Path, char_id: str) -> dict:
    return json.loads(_read(work / "art" / "appearance" / ("%s.json" % char_id)))


def test_save_t2c_keeps_prompts_and_rest(env):
    """漫畫頁只送 t2c：兩個提示詞、confirmed、expressions 不能跟著被洗掉。"""
    config, work, _ = env
    path = work / "art" / "appearance" / "ellie.json"
    before = appearance_card.load_card(path)
    result = comic.save_appearance_t2c("demo", "ellie", NEW_T2C, config=config)
    after = appearance_card.load_card(path)
    assert {key: after[key] for key in after if key != "t2c"} == {key: before[key] for key in before if key != "t2c"}
    assert after["t2c"]["sections"] == NEW_T2C["sections"]
    assert after["t2c"]["outfits"]["default"] == NEW_T2C["outfits"]["default"]
    assert result["check"]["source"] == "t2c" and result["check"]["stale"] is False
    assert result["check"]["issues"] == []


def test_save_t2c_stamps_fingerprint_on_first_write(env):
    """第一次在介面上寫 t2c 也要蓋指紋，否則過期判斷永遠不會亮（alec 原本沒有 t2c）。"""
    config, _, _ = env
    result = comic.save_appearance_t2c("demo", "alec", T2C_OK, config=config)
    assert result["card"]["t2c"]["source_sha1"] == result["check"]["current_sha1"]
    assert result["check"]["stale"] is False


def test_save_t2c_keeps_stale_fingerprint(env):
    """舊指紋對不上時沿用舊值（過期只提醒），不偷偷改寫成目前提示詞。"""
    config, work, _ = env
    path = work / "art" / "appearance" / "ellie.json"
    data = json.loads(_read(path))
    data["t2c"]["source_sha1"] = hashlib.sha1(b"old prompts").hexdigest()
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = comic.save_appearance_t2c("demo", "ellie", NEW_T2C, config=config)
    assert result["check"]["stale"] is True
    assert result["card"]["t2c"]["source_sha1"] == data["t2c"]["source_sha1"]


def test_save_t2c_endpoint_rewrites_bible(env):
    """存檔後自動重寫 bible（Q1）：卡上的值換成新的人工值，清單那列仍 0 error。"""
    config, _, root = env
    http = TestClient(create_app(config, llm_factory=lambda: None), base_url="http://127.0.0.1:7866")
    http.post("/api/books/demo/comic/ch001/bible", json={})
    got = http.put(BIBLE_T2C, json={"t2c": NEW_T2C})
    assert got.status_code == 200
    body = got.json()
    assert "ellie" in body["bible"]["written"]      # overwrite=True 重寫，不是 kept
    assert body["check"]["issues"] == []
    assert "a white blouse" in _read(root / "bible" / "characters" / "ellie.md")
    row = {row["id"]: row for row in http.get("/api/books/demo/comic/ch001/bible").json()["characters"]}
    assert row["ellie"]["errors"] == [] and row["ellie"]["draft"] is False


def test_save_t2c_with_bad_value_lands_in_drafts(env):
    """值不合規時卡進 drafts/，回應的 issues 指出是哪一條（漫畫頁照這個顯示）。"""
    config, _, root = env
    http = TestClient(create_app(config, llm_factory=lambda: None), base_url="http://127.0.0.1:7866")
    http.post("/api/books/demo/comic/ch001/bible", json={})
    bad = {"sections": {"head": ["a 17-year-old girl", "a grey shirt"]},
           "outfits": {"default": {"top": ["jeans"]}}}
    body = http.put(BIBLE_T2C, json={"t2c": bad}).json()
    assert "ellie" in body["bible"]["drafts"]
    assert any("shirt" in item["message"] for item in body["bible"]["issues"])
    assert (root / "bible" / "drafts" / "characters" / "ellie.md").is_file()


def test_save_t2c_rejects_without_appearance_card(env):
    """佔位卡沒有正本：不建外型卡，回 400 與一句話（D27：不補佔位卡的外觀）。"""
    config, work, _ = env
    http = TestClient(create_app(config, llm_factory=lambda: None), base_url="http://127.0.0.1:7866")
    got = http.put("/api/books/demo/comic/ch001/bible/nobody/t2c", json={"t2c": NEW_T2C})
    assert got.status_code == 400 and "外型卡" in got.json()["error"]
    assert not (work / "art" / "appearance" / "nobody.json").exists()


def test_save_t2c_rejects_empty_payload(env):
    """全空＝沒有東西可寫：400，不要靜默沿用舊值（會在介面上看起來像存好了）。"""
    config, work, _ = env
    before = _appearance(work, "ellie")
    http = TestClient(create_app(config, llm_factory=lambda: None), base_url="http://127.0.0.1:7866")
    for payload in (None, {}, {"sections": {"head": []}}, {"outfits": {"default": {"top": []}}}):
        got = http.put(BIBLE_T2C, json={"t2c": payload})
        assert got.status_code == 400 and "至少" in got.json()["error"], payload
    assert _appearance(work, "ellie") == before


BAD_T2C = {"sections": {"head": ["a 17-year-old girl", "a grey shirt"]},
           "outfits": {"default": {"top": ["jeans"]}}}


def test_save_t2c_bad_value_removes_confirmed_card(env):
    """值改壞之後舊的定案卡要收掉（D28）：清單只剩草稿那一列，不留同一張卡的兩列。"""
    config, _, root = env
    http = TestClient(create_app(config, llm_factory=lambda: None), base_url="http://127.0.0.1:7866")
    http.post("/api/books/demo/comic/ch001/bible", json={})
    confirmed = root / "bible" / "characters" / "ellie.md"
    assert confirmed.is_file()
    body = http.put(BIBLE_T2C, json={"t2c": BAD_T2C}).json()
    assert body["bible"]["removed"] == ["ellie"]
    assert not confirmed.exists()
    assert (root / "bible" / "drafts" / "characters" / "ellie.md").is_file()
    rows = [row for row in http.get("/api/books/demo/comic/ch001/bible").json()["characters"]
            if row["id"] == "ellie"]
    assert len(rows) == 1 and rows[0]["draft"] is True and len(rows[0]["errors"]) == 1


def test_save_t2c_fix_moves_draft_back_and_drops_it(env):
    """修好之後草稿搬回定案目錄：drafts 那份刪掉，清單還是一列（draft=False）。"""
    config, _, root = env
    http = TestClient(create_app(config, llm_factory=lambda: None), base_url="http://127.0.0.1:7866")
    http.post("/api/books/demo/comic/ch001/bible", json={})
    http.put(BIBLE_T2C, json={"t2c": BAD_T2C})
    drafted = root / "bible" / "drafts" / "characters" / "ellie.md"
    assert drafted.is_file()
    body = http.put(BIBLE_T2C, json={"t2c": NEW_T2C}).json()
    assert body["bible"]["removed"] == ["ellie"]
    assert not drafted.exists()
    assert (root / "bible" / "characters" / "ellie.md").is_file()
    rows = [row for row in http.get("/api/books/demo/comic/ch001/bible").json()["characters"]
            if row["id"] == "ellie"]
    assert len(rows) == 1 and rows[0]["draft"] is False and rows[0]["errors"] == []


def test_bible_cards_overwrite_cleans_old_duplicate(env):
    """這一段之前留下的舊資料也收得掉：定案與草稿同時在時，重跑 overwrite 只留一份。"""
    config, _, root = env
    comic.bible_cards("demo", "ch001", config=config)
    confirmed = root / "bible" / "characters" / "ellie.md"
    stale = root / "bible" / "drafts" / "characters" / "ellie.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(_read(confirmed), encoding="utf-8")
    report = comic.bible_cards("demo", "ch001", overwrite=True, config=config)
    assert report["removed"] == ["ellie"]
    assert not stale.exists() and confirmed.is_file()