import json

import openpyxl
import pytest

from t2vn import appearance_card as ac
from t2vn import art_cards as m

LIB = {"hair_color": ["black", "silver"], "outfit_a": ["blue coat"], "outfit_b": ["red coat"]}


@pytest.fixture
def work(tmp_path):
    w = tmp_path / "works" / "b"
    w.mkdir(parents=True)
    return w


def ok(path):
    return None


def make_card(work, char_id="ellie", headshot="", sprite=""):
    card = ac.new_card(char_id)
    card["headshot_prompt"], card["sprite_prompt"] = headshot, sprite
    m.save_card(ok, work, char_id, card)
    return card


# ---------------------------------------------------------------- 路徑與 id

@pytest.mark.parametrize("bad", ["", "Ellie", "../x", "a/b", "1a"])
def test_bad_ids(work, bad):
    with pytest.raises(m.ArtCardsError):
        m.card_path(work, bad)


def test_paths(work):
    assert m.card_path(work, "ellie") == work / "art" / "appearance" / "ellie.json"


# ---------------------------------------------------------------- wildcard 啟用

def test_enabled_roundtrip_and_default(work):
    assert m.load_enabled(work) is None
    assert m.save_enabled(ok, work, ["outfit_b", "nope", "hair_color", "hair_color"], LIB) == ["hair_color", "outfit_b"]
    assert m.load_enabled(work) == ["hair_color", "outfit_b"]
    (work / "art" / "wildcards_enabled.json").write_text("[]", encoding="utf-8")
    with pytest.raises(m.ArtCardsError):
        m.load_enabled(work)


def test_usable_and_overview():
    assert m.usable_library(LIB, None) == LIB
    assert sorted(m.usable_library(LIB, ["outfit_a", "zzz"])) == ["outfit_a"]
    assert m.usable_library(LIB, []) == {}
    ov = m.wildcard_overview(LIB, ["hair_color"])
    assert [(o["name"], o["enabled"], o["count"]) for o in ov] == [("hair_color", True, 2), ("outfit_a", False, 1), ("outfit_b", False, 1)]
    assert all(o["enabled"] for o in m.wildcard_overview(LIB, None))


def test_save_enabled_respects_assert_writable(work):
    def deny(path):
        raise PermissionError("no")
    with pytest.raises(PermissionError):
        m.save_enabled(deny, work, ["hair_color"], LIB)


# ---------------------------------------------------------------- 卡片存取

def test_get_card_default(work):
    assert m.get_card(work, "ellie") == ac.new_card("ellie")


def test_save_card_writes_and_syncs_style(work):
    card = ac.new_card("ellie")
    card["headshot_prompt"] = "silver hair, blue eyes"
    card["sprite_prompt"] = "blue coat, arms crossed"
    card["expressions"] = ["smile", "neutral"]
    card["confirmed"] = True
    saved = m.save_card(ok, work, "ellie", card)
    assert saved["char_id"] == "ellie" and saved["confirmed"] is True and saved["expressions"] == ["neutral", "smile"]
    assert m.get_card(work, "ellie") == saved
    art = json.loads((work / "art" / "style.json").read_text(encoding="utf-8"))
    assert art["characters"]["ellie"] == {
        "tags": "silver hair, blue eyes, blue coat, arms crossed",
        "headshot_tags": "silver hair, blue eyes",
        "description": "", "notes": "", "confirmed": True,
    }


def test_save_card_sync_keeps_existing_fields_and_other_keys(work):
    (work / "art").mkdir()
    (work / "art" / "style.json").write_text(json.dumps({"artist": "x", "characters": {"ellie": {"tags": "old", "description": "desc", "notes": "n", "confirmed": True}}}), encoding="utf-8")
    card = ac.new_card("ellie")
    card["sprite_prompt"] = "coat"
    m.save_card(ok, work, "ellie", card)
    art = json.loads((work / "art" / "style.json").read_text(encoding="utf-8"))
    assert art["artist"] == "x"
    assert art["characters"]["ellie"] == {"tags": "coat", "headshot_tags": "", "description": "desc", "notes": "n", "confirmed": False}


def test_save_card_empty_does_not_create_style_entry(work):
    m.save_card(ok, work, "ellie", ac.new_card("ellie"))
    assert not (work / "art" / "style.json").exists()


def test_save_card_clearing_card_clears_synced_tags(work):
    make_card(work, "ellie", headshot="silver hair", sprite="coat")
    m.save_card(ok, work, "ellie", ac.new_card("ellie"))
    art = json.loads((work / "art" / "style.json").read_text(encoding="utf-8"))
    assert art["characters"]["ellie"]["tags"] == "" and art["characters"]["ellie"]["headshot_tags"] == ""


def test_save_card_unresolved_wildcard_not_synced(work):
    card = ac.new_card("ellie")
    card["sprite_prompt"] = "__outfit_a__"
    m.save_card(ok, work, "ellie", card)
    assert not (work / "art" / "style.json").exists()  # 沒展開的標籤被丟掉，所以沒有標籤可同步
    assert m.get_card(work, "ellie")["sprite_prompt"] == "__outfit_a__"


def test_save_card_confirm_needs_content(work):
    card = ac.new_card("ellie")
    card["confirmed"] = True
    with pytest.raises(m.ArtCardsError, match="至少填一個提示詞"):
        m.save_card(ok, work, "ellie", card)
    assert not (work / "art" / "appearance" / "ellie.json").exists()


def test_save_card_url_id_wins_and_validates(work):
    card = ac.new_card("other")
    card["headshot_prompt"] = "red hair"
    assert m.save_card(ok, work, "ellie", card)["char_id"] == "ellie"
    with pytest.raises(m.ArtCardsError):
        m.save_card(ok, work, "Bad", card)
    with pytest.raises(m.ArtCardsError):
        m.save_card(ok, work, "ellie", [])


def test_save_card_calls_assert_writable_for_each_write(work):
    seen = []
    card = ac.new_card("ellie")
    card["sprite_prompt"] = "coat"
    m.save_card(seen.append, work, "ellie", card)
    assert work / "art" / "appearance" / "ellie.json" in seen and work / "art" / "style.json" in seen


def test_list_cards(work):
    make_card(work, "ellie", headshot="silver hair")
    chars = {"player": {}, "ellie": {}, "pesk": {}, "Bad-Id": {}}
    assets = {"ellie": [{"asset_id": "headshot_ellie_neutral", "status": "planned"}], "player": [{"asset_id": "x"}]}
    rows = m.list_cards(work, chars, {"ellie": "艾莉"}, {"pesk": "高大的男人"}, assets)
    assert [r["id"] for r in rows] == ["ellie", "pesk"]
    assert rows[0]["label"] == "艾莉" and rows[0]["has_card"] is True and rows[0]["card"]["headshot_prompt"] == "silver hair"
    assert rows[0]["assets"] == assets["ellie"] and rows[0]["assets"] is not assets["ellie"]
    assert rows[1]["label"] == "pesk" and rows[1]["has_card"] is False and rows[1]["card_text"] == "高大的男人"
    assert rows[1]["card"] == ac.new_card("pesk") and rows[1]["assets"] == []
    assert "headshots" not in rows[0]
    assert [r["assets"] for r in m.list_cards(work, {"ellie": {}})] == [[]]


# ---------------------------------------------------------------- 擲骰

def test_roll_card_deterministic_with_seed_and_only_enabled():
    card = ac.new_card("ellie")
    card["headshot_prompt"] = "__hair_color__ hair"
    card["sprite_prompt"] = "__outfit_a__"
    a, warns = m.roll_card(card, LIB, None, seed=5)
    b, _ = m.roll_card(card, LIB, None, seed=5)
    assert a == b and "__" not in a["headshot_prompt"] and a["sprite_prompt"] == "blue coat" and warns == []
    assert card["sprite_prompt"] == "__outfit_a__"  # 不改傳入
    c, warns = m.roll_card(card, LIB, ["hair_color"], seed=5)
    assert c["sprite_prompt"] == "__outfit_a__" and any("outfit_a" in w for w in warns)


def test_roll_card_random_seed_varies():
    card = ac.new_card("ellie")
    card["headshot_prompt"] = "__big__"
    lib = {"big": [str(i) for i in range(50)]}
    assert len({m.roll_card(card, lib, None)[0]["headshot_prompt"] for _ in range(20)}) > 1


# ---------------------------------------------------------------- AI 草擬

GOOD = json.dumps({"headshot_prompt": "long hair, __hair_color__, serious eyes", "sprite_prompt": "__outfit_a__, arms crossed"})


def test_draft_card_ok_and_prompt_contents():
    prompts = []

    def llm(p):
        prompts.append(p)
        return GOOD
    card, warns = m.draft_card("ellie", {"name": "Ellie"}, ["她有一頭銀髮。"], LIB, ["hair_color", "outfit_a"], llm, current={"notes": "舊備註", "expressions": ["smile"]})
    assert card["char_id"] == "ellie" and card["confirmed"] is False and card["notes"] == "舊備註" and card["expressions"] == ["smile"]
    assert card["headshot_prompt"] == "long hair, __hair_color__, serious eyes" and card["sprite_prompt"] == "__outfit_a__, arms crossed" and warns == []
    assert "__hair_color__" in prompts[0] and "__outfit_b__" not in prompts[0]  # 只列啟用的
    assert "她有一頭銀髮。" in prompts[0]


def test_draft_card_disabled_wildcard_removed():
    card, warns = m.draft_card("ellie", {}, [], LIB, ["hair_color"], lambda p: GOOD)
    assert card["sprite_prompt"] == "arms crossed" and warns == ["sprite_prompt: 不在清單內的 wildcard：outfit_a"]


def test_draft_card_retry_then_ok():
    replies = iter(["not json", GOOD])
    prompts = []

    def llm(p):
        prompts.append(p)
        return next(replies)
    card, _ = m.draft_card("ellie", {}, [], LIB, None, llm)
    assert len(prompts) == 2 and prompts[1].startswith(prompts[0]) and "Your previous reply was rejected:" in prompts[1]
    assert prompts[1].endswith("Reply again with only the JSON object.")
    assert card["headshot_prompt"].startswith("long hair")


def test_draft_card_gives_up():
    calls = []
    with pytest.raises(m.ArtCardsError, match="AI 的回覆格式不對"):
        m.draft_card("ellie", {}, [], LIB, None, lambda p: calls.append(p) or "nope", retries=1)
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(m.ArtCardsError):
        m.draft_card("ellie", {}, [], LIB, None, lambda p: calls.append(p) or "nope", retries=0)
    assert len(calls) == 1


# ---------------------------------------------------------------- 畫風庫

def make_xlsx(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["name", "prompt", "naturl_prompt", "negative_prompt", "category", "namezh", "namejp", "nameen"])
    ws.append(["甲/a/A", "oil painting, warm palette, cover art", "d", "blurry", "內文插圖風格", "甲風", "", "A"])
    wb.save(path)


def test_import_styles(tmp_path):
    x = tmp_path / "s.xlsx"
    make_xlsx(x)
    dest = tmp_path / "cfg" / "styles.json"
    seen = []
    assert m.import_styles(seen.append, x, dest) == 1
    assert seen == [dest] and json.loads(dest.read_text(encoding="utf-8"))[0]["names"]["zh-TW"] == "甲風"
    with pytest.raises(m.ArtCardsError, match="找不到檔案"):
        m.import_styles(ok, tmp_path / "none.xlsx", dest)
    bad = tmp_path / "bad.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["x"])
    wb.save(bad)
    with pytest.raises(m.ArtCardsError, match="缺少欄位"):
        m.import_styles(ok, bad, dest)


def test_apply_and_clear_style(tmp_path, work):
    x = tmp_path / "s.xlsx"
    make_xlsx(x)
    dest = tmp_path / "styles.json"
    m.import_styles(ok, x, dest)
    catalog = json.loads(dest.read_text(encoding="utf-8"))
    sid = catalog[0]["style_id"]
    assert m.style_state(work) == {"style_id": ""}
    (work / "art").mkdir()
    (work / "art" / "style.json").write_text(json.dumps({"artist": "foo", "extra": "rain"}), encoding="utf-8")
    assert m.apply_style(ok, work, catalog, sid) == {"style_id": sid, "dropped": ["cover art"]}
    art = json.loads((work / "art" / "style.json").read_text(encoding="utf-8"))
    assert art["extra"] == "rain, oil painting, warm palette" and art["artist"] == "foo" and art["negative"] == "blurry"
    assert m.style_state(work) == {"style_id": sid}
    assert m.clear_style(ok, work) == {"style_id": ""}
    art = json.loads((work / "art" / "style.json").read_text(encoding="utf-8"))
    assert art == {"artist": "foo", "extra": "rain"} and m.style_state(work) == {"style_id": ""}
    with pytest.raises(m.ArtCardsError, match="找不到畫風"):
        m.apply_style(ok, work, catalog, "st_nope")


def test_apply_style_creates_style_json_and_clear_without_file(work):
    catalog = [{"style_id": "st_1", "tags": "flat colors", "negative": ""}]
    m.apply_style(ok, work, catalog, "st_1")
    assert json.loads((work / "art" / "style.json").read_text(encoding="utf-8"))["extra"] == "flat colors"
    fresh = work.parent / "w2"
    fresh.mkdir()
    assert m.clear_style(ok, fresh) == {"style_id": ""} and not (fresh / "art").exists()


def test_draft_card_retries_when_both_prompts_empty():
    replies = iter(['{"headshot_prompt": "", "sprite_prompt": ""}', GOOD])
    prompts = []

    def llm(p):
        prompts.append(p)
        return next(replies)
    card, _ = m.draft_card("minor", {"name": "Dock Guard"}, [], LIB, None, llm)
    assert len(prompts) == 2 and "left both prompts empty" in prompts[1]
    assert card["headshot_prompt"].startswith("long hair")
    with pytest.raises(m.ArtCardsError):
        m.draft_card("minor", {}, [], LIB, None, lambda p: '{"headshot_prompt": "", "sprite_prompt": ""}', retries=1)
