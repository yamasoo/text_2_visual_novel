"""t2vn/bible.py 的驗收測試（場景／BGM 清單與註冊表）。全部用合成資料。"""
import json
from pathlib import Path

import pytest

from t2vn.bible import (collect_ids, load_registry, merged_view, remove_entry, save_registry, set_entry,
                        validate_registry)


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")


def scene(sid, loc, bgm=None, time="day"):
    return {"scene_id": sid, "location_id": loc, "time_of_day": time, "bgm": bgm, "cast": [], "lines": []}


@pytest.fixture()
def work(tmp_path):
    w = tmp_path / "work"
    write(w / "scenes" / "ch001.json", {"scenes": [scene("ch001_sc01", "a_dock", "bgm_calm"), scene("ch001_sc02", "a_dock")]})
    write(w / "scenes" / "ch002.json", {"scenes": [scene("ch002_sc01", "b_sea", "bgm_calm"),
                                                    scene("ch002_sc02", "a_dock", "bgm_tension", "night")]})
    # scenes/ 已有 ch001，所以 gold/ch001 要被忽略；gold/ch003 沒有對應的 scenes 檔，要被採用
    write(w / "gold" / "ch001.json", {"scenes": [scene("ch001_sc01", "gold_only", "bgm_gold_only")]})
    write(w / "gold" / "ch003.json", {"scenes": [scene("ch003_sc01", "c_gold", "bgm_gold")]})
    write(w / "scenes" / "ch009.json", "not json")  # 壞檔要被略過，不能丟例外
    write(w / "prototype" / "style.json", {"locations": {"a_dock": {"label": "碼頭"}, "z_style_only": {"label": "只在風格檔"}}})
    return w


def ids(entries):
    return [e["id"] for e in entries]


def test_collect_ids_counts_and_order(work):
    got = collect_ids(work)
    assert set(got) == {"locations", "bgm"}
    assert ids(got["locations"]) == ["a_dock", "b_sea", "c_gold"]          # 依第一次出現的順序
    dock = got["locations"][0]
    assert dock == {"id": "a_dock", "count": 3, "chapters": ["ch001", "ch002"],
                    "scenes": ["ch001_sc01", "ch001_sc02", "ch002_sc02"]}
    assert ids(got["bgm"]) == ["bgm_calm", "bgm_tension", "bgm_gold"]       # bgm 為 null 的場不算
    assert got["bgm"][0]["count"] == 2 and got["bgm"][0]["scenes"] == ["ch001_sc01", "ch002_sc01"]


def test_collect_ids_prefers_scenes_over_gold(work):
    got = collect_ids(work)
    assert "gold_only" not in ids(got["locations"]) and "bgm_gold_only" not in ids(got["bgm"])


def test_collect_ids_empty_work(tmp_path):
    assert collect_ids(tmp_path / "nothing") == {"locations": [], "bgm": []}


def test_load_registry_missing_gives_skeleton(tmp_path):
    assert load_registry(tmp_path) == {"schema_version": "0.1", "locations": {}, "bgm": {}}


def test_load_registry_bad_json_raises(tmp_path):
    write(tmp_path / "bible.json", "{oops")
    with pytest.raises(ValueError):
        load_registry(tmp_path)


def test_validate_registry_rules():
    ok = {"schema_version": "0.1", "locations": {"a_dock": {"label": "碼頭", "note": "n"}},
          "bgm": {"bgm_calm": {"label": "平靜", "prompt": "soft piano"}}}
    assert validate_registry(ok) == []
    assert validate_registry([]) != []
    bad_id = {"schema_version": "0.1", "locations": {"BadId": {"label": "x"}}, "bgm": {}}
    assert any("BadId" in e for e in validate_registry(bad_id))
    chinese_id = {"schema_version": "0.1", "locations": dict.fromkeys(["碼頭"], {"label": "x"}), "bgm": {}}
    assert validate_registry(chinese_id) != []
    bad_field = {"schema_version": "0.1", "locations": {"a_dock": {"label": "x", "color": "red"}}, "bgm": {}}
    assert any("color" in e for e in validate_registry(bad_field))
    bad_type = {"schema_version": "0.1", "locations": {"a_dock": {"label": 5}}, "bgm": {}}
    assert any("a_dock" in e for e in validate_registry(bad_type))
    assert any("extra" in e for e in validate_registry({"schema_version": "0.1", "locations": {}, "bgm": {}, "extra": 1}))
    assert validate_registry({"schema_version": "9", "locations": {}, "bgm": {}}) != []


def test_set_entry_creates_merges_and_persists(tmp_path):
    first = set_entry(tmp_path, "locations", "a_dock", {"label": "碼頭"})
    assert first == {"label": "碼頭", "note": ""}
    second = set_entry(tmp_path, "locations", "a_dock", {"note": "清晨有霧"})
    assert second == {"label": "碼頭", "note": "清晨有霧"}          # 合併，不覆蓋沒給的欄位
    saved = json.loads((tmp_path / "bible.json").read_text(encoding="utf-8"))
    assert saved["locations"]["a_dock"] == {"label": "碼頭", "note": "清晨有霧"}
    assert (tmp_path / "bible.json").read_text(encoding="utf-8").endswith("\n")
    bgm = set_entry(tmp_path, "bgm", "bgm_calm", {"label": "平靜", "prompt": "soft piano"})
    assert bgm == {"label": "平靜", "note": "", "prompt": "soft piano"}


@pytest.mark.parametrize("kind,entry_id,fields", [
    ("maps", "a_dock", {"label": "x"}),             # 未知種類
    ("locations", "Bad Id", {"label": "x"}),        # 不合格的 id
    ("locations", "a_dock", {"color": "red"}),      # 未知欄位
    ("locations", "a_dock", {"label": 3}),          # 型別錯
])
def test_set_entry_rejects_bad_input(tmp_path, kind, entry_id, fields):
    with pytest.raises(ValueError):
        set_entry(tmp_path, kind, entry_id, fields)
    assert not (tmp_path / "bible.json").exists()   # 失敗不能留下檔案


def test_remove_entry(tmp_path):
    set_entry(tmp_path, "bgm", "bgm_calm", {"label": "平靜"})
    assert remove_entry(tmp_path, "bgm", "bgm_calm") is True
    assert remove_entry(tmp_path, "bgm", "bgm_calm") is False
    assert load_registry(tmp_path)["bgm"] == {}
    with pytest.raises(ValueError):
        remove_entry(tmp_path, "maps", "x")


def test_save_registry_validates(tmp_path):
    with pytest.raises(ValueError) as info:
        save_registry(tmp_path, {"schema_version": "0.1", "locations": {"BadId": {}}, "bgm": {"Also Bad": {}}})
    assert "BadId" in str(info.value) and "Also Bad" in str(info.value) and ";" in str(info.value)
    assert not (tmp_path / "bible.json").exists()


def test_merged_view(work):
    set_entry(work, "locations", "a_dock", {"label": "港口", "note": "主要場景"})
    set_entry(work, "locations", "only_registry", {"label": "還沒用到"})
    set_entry(work, "bgm", "bgm_calm", {"label": "平靜"})
    view = merged_view(work)
    locs = {e["id"]: e for e in view["locations"]}
    # 順序：場景裡用到的（依第一次出現）→ 只在註冊表 → 只在 style.json
    assert ids(view["locations"]) == ["a_dock", "b_sea", "c_gold", "only_registry", "z_style_only"]
    assert locs["a_dock"] == {"id": "a_dock", "label": "港口", "note": "主要場景", "count": 3, "chapters": ["ch001", "ch002"],
                              "scenes": ["ch001_sc01", "ch001_sc02", "ch002_sc02"], "used": True,
                              "sources": ["scenes", "registry", "style"]}   # 註冊表的 label 優先於 style.json
    assert locs["b_sea"]["label"] == "" and locs["b_sea"]["sources"] == ["scenes"] and locs["b_sea"]["used"] is True
    assert locs["only_registry"] == {"id": "only_registry", "label": "還沒用到", "note": "", "count": 0, "chapters": [],
                                     "scenes": [], "used": False, "sources": ["registry"]}
    assert locs["z_style_only"]["label"] == "只在風格檔" and locs["z_style_only"]["sources"] == ["style"]
    bgm = {e["id"]: e for e in view["bgm"]}
    assert ids(view["bgm"]) == ["bgm_calm", "bgm_tension", "bgm_gold"]
    assert bgm["bgm_calm"]["label"] == "平靜" and bgm["bgm_calm"]["sources"] == ["scenes", "registry"]
    assert bgm["bgm_tension"]["sources"] == ["scenes"]


def test_style_label_used_when_registry_label_empty(work):
    set_entry(work, "locations", "a_dock", {"note": "只有註解"})
    entry = {e["id"]: e for e in merged_view(work)["locations"]}["a_dock"]
    assert entry["label"] == "碼頭" and entry["note"] == "只有註解"


def test_merged_view_without_style_file(tmp_path):
    write(tmp_path / "scenes" / "ch001.json", {"scenes": [scene("ch001_sc01", "a_dock", "bgm_calm")]})
    view = merged_view(tmp_path)
    assert ids(view["locations"]) == ["a_dock"] and view["locations"][0]["sources"] == ["scenes"]
