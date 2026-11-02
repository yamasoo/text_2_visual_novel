"""候選圖檔案的安全刪除（S6）。

每個素材（背景、立繪、頭像）出圖後，候選圖存在一個資料夾裡：``<seed>.png``，旁邊有
同主檔名的 ``<seed>.json``（出圖參數）。使用者在網頁上勾選不要的候選圖並刪除；這個模組
只負責「刪掉指定的候選圖」：檢查檔名（防止路徑跳脫）、保護不能刪的（例如已選中的那張）、
刪之前呼叫呼叫端傳入的 ``assert_writable``（專案的寫入權限檢查）。

不碰資料夾本身、不碰 manifest、不列出候選圖（別的模組做）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

NAME_PATTERN = re.compile(r"[a-z0-9_]+\.png")
META_SUFFIX = ".json"


def check_names(names: Iterable[str]) -> list[str]:
    """檢查候選圖檔名，回傳去重（保留順序）後的清單。

    每個檔名都必須符合 ``^[a-z0-9_]+\\.png$``（不能有路徑、大寫、其他副檔名）；
    任何一個不合法 → ``ValueError``（訊息含該檔名的 repr）。
    """
    checked: list[str] = []
    for name in names or []:
        if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"候選圖檔名不合法：{name!r}")
        if name not in checked:
            checked.append(name)
    return checked


def delete_candidates(
    assert_writable: Callable[[Path], Any],
    folder: str | Path,
    names: Sequence[str],
    protect: Iterable[str] = (),
) -> dict[str, list[str]]:
    """刪掉指定的候選圖（``<seed>.png`` 與同名 ``.json``），回傳處理結果。

    先檢查全部檔名（有不合法的就一個都不刪）；接著依序處理：在 ``protect`` 裡的記進
    ``protected``，檔案不存在記進 ``missing``，其餘先對每個目標各呼叫一次
    ``assert_writable`` 再全部 ``unlink``、記進 ``deleted``。``assert_writable`` 的例外
    不吞掉（該檔案的兩個檔都不刪，後面的也不處理）。
    """
    checked = check_names(names)
    keep = set(protect or ())
    root = Path(folder)
    report: dict[str, list[str]] = {"deleted": [], "missing": [], "protected": []}

    for name in checked:
        if name in keep:
            report["protected"].append(name)
            continue
        png = root / name
        if not png.is_file():
            report["missing"].append(name)
            continue

        targets = [png]
        meta = png.with_suffix(META_SUFFIX)
        if meta.is_file():
            targets.append(meta)
        for target in targets:
            assert_writable(target)
        for target in targets:
            target.unlink()
        report["deleted"].append(name)

    return report
