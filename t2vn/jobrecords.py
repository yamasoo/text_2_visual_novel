"""背景任務紀錄的存檔與查詢（任務紀錄頁用）。

每個背景任務（AI 標註、抽取）結束時存一份 ``works/<book>/_jobs/<id>.json``，
網頁的「任務紀錄」頁用它列出歷史、看失敗原因。紀錄本體的欄位由呼叫端決定，
這個模組只檢查必要欄位，並提供列表、讀取、修剪。

- :func:`save_record`：驗證後原子寫入（先寫 ``.tmp`` 再 ``os.replace``）。
- :func:`load_record`：讀單筆完整內容（壞檔視為沒有）。
- :func:`list_records`：列出摘要（新到舊），不含完整 log。
- :func:`prune_records`：只保留最近 N 筆。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

JOBS_DIR_NAME = "_jobs"
# 開頭可以是數字：P1 之前的紀錄 id 是 uuid 十六進位（例如 930fb90d44），那些檔案還打得開。
# 字元集不含斜線、反斜線與點，所以一樣擋掉路徑穿越。
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{1,39}$")
STATUS_VALUES = ("running", "done", "error", "stopped")
REQUIRED_FIELDS = ("id", "book_id", "kind", "status", "started")
MAX_LOG_LINES = 2000
UTF8 = "utf-8"


def _jobs_dir(work: Path) -> Path:
    """紀錄資料夾：``<work>/_jobs``。"""
    return work / JOBS_DIR_NAME


def _record_path(work: Path, job_id: str) -> Path:
    """單筆紀錄的路徑。"""
    return _jobs_dir(work) / f"{job_id}.json"


def _validate_id(job_id: Any) -> str:
    """檢查任務 id 格式（同時擋掉路徑穿越）。"""
    if not isinstance(job_id, str) or ID_PATTERN.fullmatch(job_id) is None:
        raise ValueError(
            f"job id 不合法：{job_id!r}（必須符合 {ID_PATTERN.pattern}）"
        )
    return job_id


def _dump(data: Any) -> str:
    """統一序列化格式：不跳脫中文、縮排 2、結尾換行。"""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _read_record(path: Path) -> dict[str, Any] | None:
    """讀一筆紀錄；讀不到、不是合法 JSON、或不是 dict 都回傳 None。"""
    try:
        data = json.loads(path.read_text(encoding=UTF8))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


ITEM_STATE = {"pending": "pending", "running": "running", "done": "done",
              "skipped": "skipped", "error": "error", "stopped": "stopped"}


def _items(items: Any) -> Any:
    """把 P4 之前的 items（``{no,status,note}``）讀成 fekit 形狀（``{key,label,state,note}``）。

    紀錄是磁碟上的既有資料，不會自己改寫；統一在讀的那一端轉，頁面就只認一種形狀。
    """
    if not isinstance(items, list):
        return items
    rows = []
    for row in items:
        if not isinstance(row, dict) or "state" in row or not isinstance(row.get("no"), int):
            rows.append(row)
            continue
        number = row["no"]
        rows.append({"key": "ch%03d" % number, "label": "第 %d 章" % number,
                     "state": ITEM_STATE.get(row.get("status"), "pending"), "note": row.get("note", "")})
    return rows


def _summary(record: dict[str, Any]) -> dict[str, Any] | None:
    """把完整紀錄濃縮成列表要用的摘要；缺必要欄位時回傳 None（視為壞檔）。"""
    for field in REQUIRED_FIELDS:
        if field not in record:
            return None
    job_id = record["id"]
    started = record["started"]
    if not isinstance(job_id, str):
        return None
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return None

    summary = record.get("summary")
    failed = summary.get("failed", 0) if isinstance(summary, dict) else 0
    log = record.get("log")
    return {
        "id": job_id,
        "book_id": record["book_id"],
        "kind": record["kind"],
        "status": record["status"],
        "started": started,
        "ended": record.get("ended"),
        "chapters": record.get("chapters", []),
        "tokens": record.get("tokens", 0),
        "log_lines": len(log) if isinstance(log, list) else 0,
        "failed": failed,
    }


def save_record(work: Path, record: dict[str, Any]) -> Path:
    """存到 ``work/_jobs/<id>.json`` 並回傳路徑（必要時建立資料夾）。

    必填欄位：``id``、``book_id``、``kind``、``status``、``started``，缺任何一個
    都會 ``ValueError``。``id`` 必須符合 ``^[a-z0-9][a-z0-9_]{1,39}$``；``status`` 必須是
    running／done／error／stopped 之一；``started`` 必須是 int 或 float
    （不可 bool、不可字串）；``log`` 若有必須是 list，且只保留最後 2000 行。
    其他欄位原樣保存。同一個 id 再存一次就是覆蓋。
    """
    if not isinstance(record, dict):
        raise ValueError("record 必須是 dict")
    for field in REQUIRED_FIELDS:
        if field not in record:
            raise ValueError(f"紀錄缺少必填欄位：{field}")

    job_id = _validate_id(record["id"])
    status = record["status"]
    if not isinstance(status, str) or status not in STATUS_VALUES:
        raise ValueError(f"status 必須是 {STATUS_VALUES} 之一：{status!r}")
    started = record["started"]
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        raise ValueError(f"started 必須是數字（int 或 float）：{started!r}")
    log = record.get("log")
    if log is not None and not isinstance(log, list):
        raise ValueError("log 必須是 list")

    data = dict(record)
    if isinstance(log, list) and len(log) > MAX_LOG_LINES:
        data["log"] = log[-MAX_LOG_LINES:]

    # 先序列化：內容不能存時不留下任何檔案。
    try:
        text = _dump(data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"紀錄無法序列化成 JSON：{exc}") from exc

    path = _record_path(work, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{job_id}.tmp")
    try:
        tmp_path.write_text(text, encoding=UTF8)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return path


def load_record(work: Path, job_id: str) -> dict[str, Any] | None:
    """讀單筆完整紀錄。id 不合格 → ``ValueError``；檔案不存在或壞掉 → ``None``。"""
    _validate_id(job_id)
    record = _read_record(_record_path(work, job_id))
    if record is not None and "items" in record:
        record["items"] = _items(record["items"])
    return record


def list_records(work: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """依 ``started`` 由新到舊列出摘要，最多 ``limit`` 筆（``None``＝全部）。

    壞檔略過；資料夾不存在回傳空清單。每筆摘要恰好有這些 key：
    ``id``、``book_id``、``kind``、``status``、``started``、``ended``、
    ``chapters``、``tokens``、``log_lines``、``failed``。
    """
    try:
        paths = sorted(_jobs_dir(work).glob("*.json"), key=lambda item: item.name)
    except OSError:
        return []

    rows: list[dict[str, Any]] = []
    for path in paths:
        record = _read_record(path)
        if record is None:
            continue
        row = _summary(record)
        if row is not None:
            rows.append(row)

    rows.sort(key=lambda row: (row["started"], row["id"]), reverse=True)
    if limit is not None:
        rows = rows[: max(int(limit), 0)]
    return rows


def prune_records(work: Path, keep: int) -> list[str]:
    """只保留 ``started`` 最新的 ``keep`` 筆，刪除其餘，回傳被刪的 id（舊的在前）。

    ``keep`` 小於 0 → ``ValueError``；``keep`` 為 0 全刪。
    """
    if keep < 0:
        raise ValueError(f"keep 不可小於 0：{keep}")

    rows = list_records(work)
    doomed = rows[max(keep, 0) :]
    removed: list[str] = []
    for row in reversed(doomed):
        path = _record_path(work, row["id"])
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(row["id"])
    return removed
