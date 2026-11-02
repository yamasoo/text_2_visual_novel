"""t2vn/overview.py 的驗收測試：一本書的進度與統計（總覽頁用）。全部用合成資料。"""
import json
from pathlib import Path

from t2vn.overview import book_overview


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")


def line(kind, speaker=None):
    out = {"type": kind}
    if kind == "dialogue":
        out["speaker"] = speaker
    if kind == "direction":
        out["effect"] = "shake"
    else:
        out["text"] = "字"
        out["source_para"] = 1
    return out


def make_work(tmp_path):
    w = tmp_path / "work"
    write(w / "chapters" / "index.json", {"chapters": [
        {"chapter_id": "ch001", "chapter_no": 1, "title": "甲", "char_count": 100, "paragraph_count": 5},
        {"chapter_id": "ch002", "chapter_no": 2, "title": "乙", "char_count": 200, "paragraph_count": 6},
        {"chapter_id": "ch003", "chapter_no": 3, "title": "丙", "char_count": 300, "paragraph_count": 7},
        {"chapter_id": "ch004", "chapter_no": 4, "title": "丁", "char_count": 400, "paragraph_count": 8},
    ]})
    # ch001：只有 gold（預設視為 reviewed）
    write(w / "gold" / "ch001.json", {"scenes": [{"scene_id": "ch001_sc01", "lines": [
        line("narration"), line("dialogue", "ellie"), line("dialogue", "ellie"), line("direction")]}]})
    # ch002：scenes（預設 draft），2 場
    write(w / "scenes" / "ch002.json", {"scenes": [
        {"scene_id": "ch002_sc01", "lines": [line("narration"), line("dialogue", "boss")]},
        {"scene_id": "ch002_sc02", "lines": [line("dialogue", "boss"), line("dialogue", "ellie"), line("dialogue", "boss")]}]})
    # ch003：沒有任何場景檔 → none
    # ch004：scenes 壞檔 → 當作 none，不能丟例外
    write(w / "scenes" / "ch004.json", "{broken")
    write(w / "review_state.json", {"chapters": {"ch002": {"status": "reviewed"}}})
    return w


def test_book_overview_totals(tmp_path):
    got = book_overview(make_work(tmp_path))
    assert got["chapters"] == 4 and got["chars"] == 1000
    assert got["status_counts"] == {"none": 2, "draft": 0, "reviewed": 2}
    assert got["scenes"] == 3
    assert got["lines"] == 9 and got["narration"] == 2 and got["dialogue"] == 6 and got["direction"] == 1


def test_speakers_sorted_by_lines_then_id(tmp_path):
    got = book_overview(make_work(tmp_path))
    assert got["speakers"] == [{"id": "boss", "lines": 3}, {"id": "ellie", "lines": 3}]
    assert [s["id"] for s in got["speakers"]] == ["boss", "ellie"]     # 同數量依 id 字母序


def test_per_chapter_rows(tmp_path):
    rows = {r["chapter_id"]: r for r in book_overview(make_work(tmp_path))["per_chapter"]}
    assert list(rows) == ["ch001", "ch002", "ch003", "ch004"]           # 依 index 順序
    assert rows["ch001"] == {"chapter_id": "ch001", "chapter_no": 1, "title": "甲", "char_count": 100,
                             "status": "reviewed", "scenes": 1, "lines": 4, "dialogue": 2, "narration": 1}
    assert rows["ch002"]["status"] == "reviewed"                         # review_state.json 覆寫
    assert rows["ch002"]["scenes"] == 2 and rows["ch002"]["lines"] == 5
    assert rows["ch003"] == {"chapter_id": "ch003", "chapter_no": 3, "title": "丙", "char_count": 300,
                             "status": "none", "scenes": 0, "lines": 0, "dialogue": 0, "narration": 0}
    assert rows["ch004"]["status"] == "none" and rows["ch004"]["scenes"] == 0


def test_default_status_depends_on_folder(tmp_path):
    w = make_work(tmp_path)
    (w / "review_state.json").unlink()
    rows = {r["chapter_id"]: r["status"] for r in book_overview(w)["per_chapter"]}
    assert rows == {"ch001": "reviewed", "ch002": "draft", "ch003": "none", "ch004": "none"}


def test_review_state_can_downgrade_gold(tmp_path):
    w = make_work(tmp_path)
    write(w / "review_state.json", {"chapters": {"ch001": {"status": "draft"}, "ch003": {"status": "reviewed"}}})
    rows = {r["chapter_id"]: r["status"] for r in book_overview(w)["per_chapter"]}
    assert rows["ch001"] == "draft"
    assert rows["ch003"] == "none"          # 沒有場景檔，狀態表寫什麼都是 none


def test_unknown_status_in_state_file_falls_back(tmp_path):
    w = make_work(tmp_path)
    write(w / "review_state.json", {"chapters": {"ch002": {"status": "weird"}}})
    rows = {r["chapter_id"]: r["status"] for r in book_overview(w)["per_chapter"]}
    assert rows["ch002"] == "draft"


def test_scenes_folder_wins_over_gold(tmp_path):
    w = make_work(tmp_path)
    write(w / "scenes" / "ch001.json", {"scenes": [{"scene_id": "ch001_sc01", "lines": [line("narration")]}]})
    row = {r["chapter_id"]: r for r in book_overview(w)["per_chapter"]}["ch001"]
    assert row["lines"] == 1 and row["status"] == "draft"


def test_unknown_line_types_count_in_lines_only(tmp_path):
    w = tmp_path / "w"
    write(w / "chapters" / "index.json", {"chapters": [{"chapter_id": "ch001", "chapter_no": 1, "title": "t", "char_count": 1, "paragraph_count": 1}]})
    write(w / "scenes" / "ch001.json", {"scenes": [{"scene_id": "s", "lines": [{"type": "choice"}, line("narration")]}]})
    got = book_overview(w)
    assert got["lines"] == 2 and got["narration"] == 1 and got["dialogue"] == 0 and got["direction"] == 0


def test_empty_or_missing_work(tmp_path):
    expected = {"chapters": 0, "chars": 0, "status_counts": {"none": 0, "draft": 0, "reviewed": 0}, "scenes": 0,
                "lines": 0, "narration": 0, "dialogue": 0, "direction": 0, "speakers": [], "per_chapter": []}
    assert book_overview(tmp_path / "nothing") == expected
    write(tmp_path / "bad" / "chapters" / "index.json", "{broken")
    assert book_overview(tmp_path / "bad") == expected
