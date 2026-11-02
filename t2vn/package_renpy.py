"""S9：把匯出好的 Ren'Py 專案打包成 Windows 版（呼叫 Ren'Py 內建的 ``distribute``）。

指令形式（Ren'Py 官方 CLI）::

    renpy.py <SDK>/launcher distribute <專案資料夾> --destination <輸出資料夾> --package win --no-update

第一個參數必須是 SDK 內 ``launcher`` 資料夾的**絕對路徑**；只寫 ``launcher`` 時 Ren'Py 會從目前工作資料夾找，
找不到就報 ``Base directory ... does not exist``。

打包前會先跑前置檢查，有錯誤就不打。成品是輸出資料夾裡的 ``.zip``。
"""
from __future__ import annotations

import time
import zipfile
from pathlib import Path
from typing import Any

from .errors import T2vnError
from .renpy_preflight import check_project
from .verify_renpy import TEST_FILE, Runner, VerifyError, find_runner, run_command

PACKAGE = "win"
DEFAULT_TIMEOUT = 3600
LOG_TAIL = 15
ARTIFACT_EXTS = (".zip", ".tar.bz2", ".dmg")
FORBIDDEN_PARTS = ("/game/saves/", "vn_verify_test", "_tracesave", "/tests/screenshots/", "traceback.txt", "errors.txt")
MAX_FORBIDDEN = 20
THEME_FILE = "vn_theme.rpy"
REQUIRED_RULES = ('build.classify("tests/**", None)', 'build.classify("game/cache/**", None)', 'build.classify("game/saves/**", None)')


class PackageError(T2vnError):
    """打包前提不成立或 Ren'Py 打包失敗（訊息直接給使用者看）。"""


def build_command(prefix: list[str], sdk: Path, project: Path, dest: Path, package: str = PACKAGE) -> list[str]:
    """``distribute`` 的完整指令（``prefix`` 是 :func:`find_runner` 的結果；``sdk`` 用來指出 launcher 的絕對路徑）。"""
    return [*prefix, str(Path(sdk).resolve() / "launcher"), "distribute", str(project), "--destination", str(dest), "--package", package, "--no-update"]


def list_artifacts(dest: Path) -> list[dict[str, Any]]:
    """輸出資料夾裡的成品（zip 等），新的在前；每項 ``{"name", "bytes", "mtime"}``。"""
    dest = Path(dest)
    if not dest.is_dir():
        return []
    rows = [{"name": p.name, "bytes": p.stat().st_size, "mtime": p.stat().st_mtime}
            for p in dest.iterdir() if p.is_file() and p.name.endswith(ARTIFACT_EXTS)]
    return sorted(rows, key=lambda row: (-row["mtime"], row["name"]))


def inspect_zip(path: Path) -> dict[str, Any]:
    """看成品 zip 裡有沒有不該出貨的東西。

    回傳 ``{"entries", "has_exe", "forbidden"}``：``forbidden`` 是路徑含 :data:`FORBIDDEN_PARTS` 任一段的檔名
    （存檔、驗收測試檔、除錯輸出…；最多列 :data:`MAX_FORBIDDEN` 個）。不是合法 zip → ``{"entries": 0, "has_exe": False, "forbidden": [], "error": 訊息}``。
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        return {"entries": 0, "has_exe": False, "forbidden": [], "error": str(exc)}
    bad = [n for n in names if any(part in "/" + n for part in FORBIDDEN_PARTS)]
    return {"entries": len(names), "has_exe": any(n.lower().endswith(".exe") for n in names), "forbidden": bad[:MAX_FORBIDDEN]}


def package(config: Any, sdk: Path, project: Path, dest: Path, *, package_name: str = PACKAGE,
            timeout: int = DEFAULT_TIMEOUT, runner: Runner = run_command) -> dict[str, Any]:
    """打包並回傳摘要 ``{"package", "dest", "code", "artifacts", "log_tail"}``。

    前提不成立（沒有 ``game/``、前置檢查有錯誤、輸出資料夾唯讀）或 Ren'Py 失敗／沒產生成品時丟 :class:`PackageError`。
    """
    project, dest = Path(project), Path(dest)
    if not (project / "game").is_dir():
        raise PackageError(f"{project} 底下沒有 game 資料夾；請先匯出")
    if config.is_write_protected(dest):
        raise PackageError(f"不能寫入唯讀資料夾：{dest}")
    try:
        prefix, _warning = find_runner(sdk)
    except VerifyError as exc:
        raise PackageError(str(exc)) from exc
    theme = project / "game" / THEME_FILE
    text = theme.read_text(encoding="utf-8") if theme.is_file() else ""
    if not all(rule in text for rule in REQUIRED_RULES):
        raise PackageError("匯出的專案還是舊版（vn_theme.rpy 沒有打包排除規則，成品會帶進測試截圖與快取）；請先到「輸出」頁重新匯出再打包")
    errors = [i for i in check_project(project) if i["level"] == "error"]
    if errors:
        first = errors[0]
        raise PackageError(f"前置檢查有 {len(errors)} 個錯誤，先修好再打包（例如 {first['file']}:{first['line']} {first['message']}）")
    leftover = project / "game" / TEST_FILE
    if leftover.exists():
        leftover.unlink()  # 驗收殘留的測試檔不能進成品
    dest.mkdir(parents=True, exist_ok=True)
    started = time.time()
    code, text = runner(build_command(prefix, Path(sdk), project, dest, package_name), timeout)
    tail = [line for line in text.splitlines() if line.strip()][-LOG_TAIL:]
    artifacts = [row for row in list_artifacts(dest) if row["mtime"] >= started - 1]
    if code != 0 or not artifacts:
        reason = f"結束碼 {code}" if code != 0 else "沒有產生成品"
        raise PackageError(f"打包失敗（{reason}）。最後輸出：\n" + "\n".join(tail))
    for row in artifacts:
        if str(row["name"]).endswith(".zip"):
            row.update(inspect_zip(dest / str(row["name"])))
    return {"package": package_name, "dest": str(dest), "code": code, "artifacts": artifacts, "log_tail": tail}
