"""t2vn/jobrecords.py 的驗收測試：背景任務結束後的紀錄存檔，供「任務紀錄」頁查詢。"""
import json

import pytest

from t2vn.jobrecords import list_records, load_record, prune_records, save_record


def rec(job_id="a1b2c3", kind="annotate", started=1000.0, status="done", log=None, **extra):
    base = {"id": job_id, "book_id": "demo", "kind": kind, "status": status, "started": started, "ended": started + 5.5 if isinstance(started, (int, float)) else None,
            "chapters": [1, 2], "items": [{"no": 1, "status": "ok", "note": ""}, {"no": 2, "status": "error", "note": "bad"}],
            "tokens": 1234, "log": log if log is not None else ["line one", "第二行"], "summary": {"failed": 1}}
    base.update(extra)
    return base


def test_save_and_load_roundtrip(tmp_path):
    path = save_record(tmp_path, rec())
    assert path == tmp_path / "_jobs" / "a1b2c3.json"
    assert load_record(tmp_path, "a1b2c3") == rec()
    text = path.read_text(encoding="utf-8")
    assert "第二行" in text and text.endswith("\n")           # ensure_ascii=False
    assert list((tmp_path / "_jobs").glob("*.tmp")) == []      # 不留暫存檔


def test_save_overwrites_same_id(tmp_path):
    save_record(tmp_path, rec(status="running"))
    save_record(tmp_path, rec(status="done"))
    assert load_record(tmp_path, "a1b2c3")["status"] == "done"
    assert len(list((tmp_path / "_jobs").glob("*.json"))) == 1


def test_log_is_truncated_to_last_2000_lines(tmp_path):
    save_record(tmp_path, rec(log=[f"l{i}" for i in range(2500)]))
    log = load_record(tmp_path, "a1b2c3")["log"]
    assert len(log) == 2000 and log[0] == "l500" and log[-1] == "l2499"


def test_load_missing_returns_none(tmp_path):
    assert load_record(tmp_path, "nope12") is None
    save_record(tmp_path, rec())
    (tmp_path / "_jobs" / "broken1.json").write_text("{oops", encoding="utf-8")
    assert load_record(tmp_path, "broken1") is None            # 壞檔當作沒有


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "A1", "a b", "x.json", "a" * 41])
def test_bad_ids_are_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        load_record(tmp_path, bad)
    with pytest.raises(ValueError):
        save_record(tmp_path, rec(job_id=bad))


@pytest.mark.parametrize("field", ["id", "book_id", "kind", "status", "started"])
def test_missing_required_field_raises(tmp_path, field):
    data = rec()
    del data[field]
    with pytest.raises(ValueError):
        save_record(tmp_path, data)


def test_invalid_status_or_types_raise(tmp_path):
    with pytest.raises(ValueError):
        save_record(tmp_path, rec(status="weird"))
    with pytest.raises(ValueError):
        save_record(tmp_path, rec(started="yesterday"))
    with pytest.raises(ValueError):
        save_record(tmp_path, rec(log="not a list"))


def test_list_records_newest_first_without_log(tmp_path):
    save_record(tmp_path, rec("aaa111", started=1000.0))
    save_record(tmp_path, rec("bbb222", started=3000.0, kind="extract", status="error"))
    save_record(tmp_path, rec("ccc333", started=2000.0, log=["x"] * 7))
    rows = list_records(tmp_path)
    assert [r["id"] for r in rows] == ["bbb222", "ccc333", "aaa111"]
    assert "log" not in rows[0] and rows[1]["log_lines"] == 7
    assert rows[0]["kind"] == "extract" and rows[0]["status"] == "error" and rows[0]["tokens"] == 1234
    assert set(rows[0]) == {"id", "book_id", "kind", "status", "started", "ended", "chapters", "tokens", "log_lines", "failed"}
    assert rows[0]["failed"] == 1 and rows[0]["chapters"] == [1, 2]


def test_list_records_limit_and_skips_broken(tmp_path):
    for i in range(5):
        save_record(tmp_path, rec(f"job{i:03d}", started=float(i)))
    (tmp_path / "_jobs" / "broken1.json").write_text("not json", encoding="utf-8")
    assert [r["id"] for r in list_records(tmp_path, limit=2)] == ["job004", "job003"]
    assert len(list_records(tmp_path)) == 5
    assert list_records(tmp_path / "empty") == []


def test_failed_defaults_to_zero_without_summary(tmp_path):
    data = rec()
    del data["summary"]
    save_record(tmp_path, data)
    assert list_records(tmp_path)[0]["failed"] == 0


def test_prune_records_keeps_newest(tmp_path):
    for i in range(5):
        save_record(tmp_path, rec(f"job{i:03d}", started=float(i)))
    removed = prune_records(tmp_path, keep=2)
    assert removed == ["job000", "job001", "job002"]              # 舊的在前
    assert [r["id"] for r in list_records(tmp_path)] == ["job004", "job003"]
    assert prune_records(tmp_path, keep=2) == []
    with pytest.raises(ValueError):
        prune_records(tmp_path, keep=-1)
    assert len(prune_records(tmp_path, keep=0)) == 2
