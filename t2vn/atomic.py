"""原子寫檔工具：先寫同目錄暫存檔並 fsync，再用 ``os.replace`` 取代目標檔。

寫到一半失敗（磁碟滿、程序被砍、檔案被鎖）時原檔維持舊內容，不留下半個檔或殘留 ``.tmp``。
暫存檔名帶 pid 與 thread id，避免同程序多執行緒或上次殘留的暫存檔互踩。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

UTF8 = "utf-8"


def _tmp_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def write_atomic(path: str | Path, text: str, encoding: str = UTF8, newline: str = "\n") -> None:
    """把文字原子寫入 ``path``（必要時建立父資料夾）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(target)
    try:
        with open(tmp, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
