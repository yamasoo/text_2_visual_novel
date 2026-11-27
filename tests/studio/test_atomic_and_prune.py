"""B3：原子寫檔、_history 備份上限修剪、Jobs.meta 回收測試。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

_FEKIT_PY = str(Path(__file__).resolve().parents[2] / "vendor" / "fekit" / "py")
if _FEKIT_PY not in sys.path:
    sys.path.insert(0, _FEKIT_PY)

from fekit import JobRunner  # noqa: E402

from t2vn import history  # noqa: E402
from t2vn.atomic import write_atomic  # noqa: E402
from t2vn.jobs_bridge import Jobs  # noqa: E402
from tests.studio.test_studio import imported  # noqa: E402


def test_write_atomic_creates_parent_and_leaves_no_tmp(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "data.json"
    write_atomic(target, '{"ok": true}\n')
    assert target.read_text(encoding="utf-8") == '{"ok": true}\n'
    assert list(target.parent.glob("*.tmp")) == []


def test_write_atomic_preserves_original_on_replace_error(tmp_path: Path) -> None:
    target = tmp_path / "cast.json"
    target.write_text('{"old": 1}\n', encoding="utf-8")
    with mock.patch("t2vn.atomic.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            write_atomic(target, '{"new": 2}\n')
    assert target.read_text(encoding="utf-8") == '{"old": 1}\n'
    assert list(tmp_path.glob("*.tmp")) == []


def test_studio_backup_prunes_history(tmp_path: Path) -> None:
    cfg, studio = imported(tmp_path)
    target = cfg.works_dir / "demo" / "scenes" / "ch001.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"v": 0}\n', encoding="utf-8")

    hdir = target.parent / history.HISTORY_DIR_NAME
    hdir.mkdir()
    for i in range(5):
        (hdir / f"ch001_2026112400000{i}.json").write_text(f'{{"v": {i}}}\n', encoding="utf-8")

    with mock.patch("t2vn.studio.time.strftime", return_value="20261124000009"):
        studio._backup(target, hdir, keep=3)

    entries = history.list_history(target)
    assert len(entries) == 3
    assert entries[0]["stamp"] == "20261124000009"
    assert [e["stamp"] for e in entries] == [
        "20261124000009",
        "20261124000004",
        "20261124000003",
    ]


def test_jobs_bridge_prunes_finished_meta_and_keeps_active() -> None:
    runner = JobRunner(max_running=4)
    jobs = Jobs(runner, max_finished=2)

    gate = threading.Event()
    started = threading.Event()

    def slow_task(_fe):
        started.set()
        gate.wait(timeout=5)
        return {"ok": True}

    active_fe = jobs.start("book_active", "annotate", [1], "deepseek", slow_task)
    try:
        assert started.wait(timeout=3)

        finished_ids = []
        for idx in range(4):
            done = threading.Event()
            fe = jobs.start(
                f"book_{idx}",
                "annotate",
                [1],
                "deepseek",
                lambda _fe: {"ok": True},
                on_finish=lambda _fe, ev=done: ev.set(),
            )
            assert done.wait(timeout=3)
            time.sleep(0.01)
            finished_ids.append(fe.id)

        deadline = time.time() + 3
        while len(jobs.meta) > 3 and time.time() < deadline:
            time.sleep(0.01)

        assert active_fe.id in jobs.meta
        kept_finished = [jid for jid in finished_ids if jid in jobs.meta]
        assert kept_finished == finished_ids[-2:]
    finally:
        gate.set()

    deadline = time.time() + 3
    while (
        jobs.meta.get(active_fe.id, {}).get("ended") is None
        or len(jobs.meta) > 2
    ) and time.time() < deadline:
        time.sleep(0.01)
    assert len(jobs.meta) == 2
