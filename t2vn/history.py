"""編輯備份的列表、還原與修剪（S4b U2）。

校對網頁每次存檔前會把舊檔複製成 ``<dir>/_history/<stem>_<YYYYMMDDHHMMSS><suffix>``
（例：``scenes/_history/ch002_20261101153000.json``）。這個模組只做檔案操作，
不解析內容、不驗證 schema：

- :func:`list_history`：列出某個檔案的所有備份（新到舊）。
- :func:`restore_history`：還原某份備份；還原前先把目前內容也備份一份，
  所以還原本身可以再還原回去。
- :func:`prune_history`：只保留最近 N 份備份。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict

HISTORY_DIR_NAME = "_history"
STAMP_FORMAT = "%Y%m%d%H%M%S"
STAMP_PATTERN = re.compile(r"^[0-9]{14}$")
UTF8 = "utf-8"


class HistoryEntry(TypedDict):
    """一份備份檔的摘要。"""

    name: str
    stamp: str
    size: int


def _history_dir(target: Path) -> Path:
    """``target`` 的備份資料夾（``target.parent / "_history"``）。"""
    return target.parent / HISTORY_DIR_NAME


def _entry_name(target: Path, stamp: str) -> str:
    """組出備份檔名：``<stem>_<stamp><suffix>``。"""
    return f"{target.stem}_{stamp}{target.suffix}"


def _stamp_of(name: str, target: Path) -> str | None:
    """從檔名取出 14 位數字時間戳；不是這個格式回傳 None。"""
    prefix = f"{target.stem}_"
    suffix = target.suffix
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    if len(name) <= len(prefix) + len(suffix):
        return None
    stamp = name[len(prefix) : len(name) - len(suffix)]
    return stamp if STAMP_PATTERN.match(stamp) else None


def list_history(target: Path) -> list[HistoryEntry]:
    """列出 ``target`` 的所有備份，依時間戳由新到舊。

    ``target`` 本身可以不存在；備份在 ``target.parent / "_history"``，檔名必須恰為
    ``f"{target.stem}_{stamp}{target.suffix}"`` 且 ``stamp`` 恰為 14 位數字，
    其他檔名一律不算。資料夾不存在或讀不到時回傳空清單。
    """
    folder = _history_dir(target)
    try:
        candidates = sorted(folder.iterdir(), key=lambda item: item.name)
    except OSError:
        return []

    entries: list[HistoryEntry] = []
    for path in candidates:
        stamp = _stamp_of(path.name, target)
        if stamp is None:
            continue
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        entries.append({"name": path.name, "stamp": stamp, "size": size})

    entries.sort(key=lambda entry: (entry["stamp"], entry["name"]), reverse=True)
    return entries


def _write_backup(target: Path, text: str, now: datetime | None) -> HistoryEntry:
    """把 ``text`` 寫成一份新備份；時間戳撞名時往後加秒，回傳新備份的摘要。"""
    moment = now if now is not None else datetime.now()
    folder = _history_dir(target)
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        stamp = moment.strftime(STAMP_FORMAT)
        path = folder / _entry_name(target, stamp)
        if not path.exists():
            path.write_text(text, encoding=UTF8)
            return {"name": path.name, "stamp": stamp, "size": path.stat().st_size}
        moment = moment + timedelta(seconds=1)


def restore_history(
    target: Path, name: str, now: datetime | None = None
) -> HistoryEntry | None:
    """把備份 ``name`` 的內容寫回 ``target``。

    ``name`` 必須是 :func:`list_history` 列的檔名之一，否則 ``ValueError``
    （這同時擋掉路徑穿越）。``target`` 已存在時，先把目前內容備份成新的一份
    （時間戳用 ``now``，沒給就用現在時間；撞名就往後加秒）再寫回，回傳新備份的摘要；
    ``target`` 不存在時直接建立它，不產生新備份，回傳 ``None``。
    """
    entry = next((item for item in list_history(target) if item["name"] == name), None)
    if entry is None:
        raise ValueError(f"不是可還原的備份：{name!r}")

    backup_path = _history_dir(target) / entry["name"]
    try:
        content = backup_path.read_text(encoding=UTF8)
    except OSError as exc:
        raise ValueError(f"讀不到備份 {name!r}：{exc}") from exc

    saved: HistoryEntry | None = None
    if target.is_file():
        try:
            current = target.read_text(encoding=UTF8)
        except OSError as exc:
            raise ValueError(f"讀不到目前的檔案 {target}：{exc}") from exc
        saved = _write_backup(target, current, now)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding=UTF8)
    return saved


def prune_history(target: Path, keep: int) -> list[str]:
    """只保留最近 ``keep`` 份備份，刪除其餘，回傳被刪的檔名（舊的在前）。

    ``keep`` 小於 0 → ``ValueError``；``keep`` 為 0 時全部刪除。不會動到
    ``target`` 本身。
    """
    if keep < 0:
        raise ValueError(f"keep 不可小於 0：{keep}")

    entries = list_history(target)
    doomed = entries[keep:]
    removed: list[str] = []
    for entry in reversed(doomed):
        path = _history_dir(target) / entry["name"]
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(entry["name"])
    return removed
