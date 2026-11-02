"""t2vn/history.py 的驗收測試：編輯備份的列表、還原、修剪。"""
from datetime import datetime
from pathlib import Path

import pytest

from t2vn.history import list_history, prune_history, restore_history

NOW = datetime(2026, 11, 1, 12, 0, 0)


def backup(target: Path, stamp: str, text: str) -> Path:
    folder = target.parent / "_history"
    folder.mkdir(exist_ok=True)
    path = folder / f"{target.stem}_{stamp}{target.suffix}"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def target(tmp_path):
    path = tmp_path / "scenes" / "ch002.json"
    path.parent.mkdir()
    path.write_text('{"v": 3}', encoding="utf-8")
    return path


def test_list_history_newest_first(target):
    backup(target, "20261101100000", '{"v": 1}')
    backup(target, "20261101110000", '{"v": 2}')
    rows = list_history(target)
    assert [r["stamp"] for r in rows] == ["20261101110000", "20261101100000"]
    assert rows[0] == {"name": "ch002_20261101110000.json", "stamp": "20261101110000", "size": len('{"v": 2}')}


def test_list_history_ignores_other_files(target):
    backup(target, "20261101100000", "x")
    folder = target.parent / "_history"
    (folder / "ch002_2026.json").write_text("short stamp", encoding="utf-8")
    (folder / "ch002_20261101100000x.json").write_text("junk suffix", encoding="utf-8")
    (folder / "ch0021_20261101100000.json").write_text("other stem prefix", encoding="utf-8")
    (folder / "ch003_20261101100000.json").write_text("other file", encoding="utf-8")
    (folder / "ch002_20261101100000.txt").write_text("other suffix", encoding="utf-8")
    (folder / "ch002_abcdefghijklmn.json").write_text("not digits", encoding="utf-8")
    assert [r["name"] for r in list_history(target)] == ["ch002_20261101100000.json"]


def test_list_history_no_folder_or_no_target(tmp_path):
    assert list_history(tmp_path / "missing" / "x.json") == []
    only_backups = tmp_path / "x.json"            # target 本身不存在也要能列出備份
    backup(only_backups, "20261101100000", "a")
    assert len(list_history(only_backups)) == 1


def test_restore_writes_back_and_backs_up_current(target):
    backup(target, "20261101100000", '{"v": 1}')
    saved = restore_history(target, "ch002_20261101100000.json", now=NOW)
    assert target.read_text(encoding="utf-8") == '{"v": 1}'
    assert saved == {"name": "ch002_20261101120000.json", "stamp": "20261101120000", "size": len('{"v": 3}')}
    assert (target.parent / "_history" / "ch002_20261101120000.json").read_text(encoding="utf-8") == '{"v": 3}'
    restore_history(target, "ch002_20261101120000.json", now=NOW)      # 還原本身可以復原
    assert target.read_text(encoding="utf-8") == '{"v": 3}'


def test_restore_same_second_does_not_overwrite(target):
    backup(target, "20261101100000", '{"v": 1}')
    backup(target, "20261101120000", "already here")
    saved = restore_history(target, "ch002_20261101100000.json", now=NOW)
    assert saved["stamp"] == "20261101120001"
    assert (target.parent / "_history" / "ch002_20261101120000.json").read_text(encoding="utf-8") == "already here"


def test_restore_when_target_missing_creates_it_and_returns_none(tmp_path):
    target = tmp_path / "x.json"
    backup(target, "20261101100000", "old")
    assert not target.exists()
    assert restore_history(target, "x_20261101100000.json", now=NOW) is None   # 沒有「目前內容」可備份
    assert target.read_text(encoding="utf-8") == "old"
    assert len(list_history(target)) == 1                                # 沒有多出備份


@pytest.mark.parametrize("name", ["../ch002.json", "ch002_20261101100000.json/../../x", "nope.json", "", "sub/ch002_20261101100000.json",
                                  "ch002_20261101100000.json\\..\\x"])
def test_restore_rejects_unknown_names(target, name):
    backup(target, "20261101100000", "a")
    with pytest.raises(ValueError):
        restore_history(target, name, now=NOW)
    assert target.read_text(encoding="utf-8") == '{"v": 3}'


def test_prune_keeps_newest(target):
    for i in range(1, 6):
        backup(target, f"2026110110000{i}", str(i))
    removed = prune_history(target, keep=2)
    assert removed == ["ch002_20261101100001.json", "ch002_20261101100002.json", "ch002_20261101100003.json"]
    assert [r["stamp"] for r in list_history(target)] == ["20261101100005", "20261101100004"]
    assert prune_history(target, keep=2) == []
    assert target.exists()                                                # 不動目前的檔案


def test_prune_zero_and_negative(target):
    backup(target, "20261101100000", "a")
    backup(target, "20261101110000", "b")
    with pytest.raises(ValueError):
        prune_history(target, keep=-1)
    assert len(list_history(target)) == 2
    assert len(prune_history(target, keep=0)) == 2
    assert list_history(target) == []


def test_utf8_content_survives(target):
    backup(target, "20261101100000", "第二章：根")
    restore_history(target, "ch002_20261101100000.json", now=NOW)
    assert target.read_text(encoding="utf-8") == "第二章：根"
