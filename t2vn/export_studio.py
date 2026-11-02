"""「輸出」頁後端：標題素材、匯出到 Ren'Py 專案、自動驗收、報告檢視。

設定存在 ``works/<book>/build/export_settings.json``（Ren'Py SDK 路徑、專案資料夾）；
驗收報告放 ``works/<book>/build/reports/verify_<時間>/``。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import art_gen, asset_manifest, export_renpy, package_renpy, verify_renpy
from .errors import T2vnError

SETTINGS_FILE = "export_settings.json"
REPORTS_DIR = "reports"
DIST_DIR = "dist"
REPORT_KEEP = 10
AUDIO_EXTS = (".opus", ".ogg", ".mp3", ".wav", ".flac")


class ExportStudioError(T2vnError):
    """輸出頁操作失敗（訊息直接給使用者看）。"""


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def build_dir(work: Path) -> Path:
    return Path(work) / "build"


def default_project(work: Path, book_id: str) -> Path:
    """沒另外指定時的 Ren'Py 專案資料夾（裡面要有 Ren'Py Launcher 建好的 ``game/``）。"""
    return build_dir(work) / "renpy" / "game" / book_id


def load_settings(work: Path, book_id: str) -> dict[str, str]:
    data = _read_json(build_dir(work) / SETTINGS_FILE, {})
    data = data if isinstance(data, dict) else {}
    return {"sdk": str(data.get("sdk") or ""), "project": str(data.get("project") or default_project(work, book_id))}


def save_settings(config: Any, work: Path, book_id: str, sdk: str, project: str) -> dict[str, str]:
    sdk, project = str(sdk or "").strip(), str(project or "").strip()
    path = build_dir(work) / SETTINGS_FILE
    config.assert_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sdk": sdk, "project": project}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load_settings(work, book_id)


# ------------------------------------------------------------------ 標題素材


def title_prompt(work: Path) -> dict[str, str]:
    entry = ((art_gen.load_art(Path(work)).get("locations") or {}).get(asset_manifest.TITLE_LOCATION_ID)) or {}
    return {"tags": str(entry.get("tags") or ""), "description": str(entry.get("description") or "")}


def save_title_prompt(config: Any, work: Path, tags: str, description: str) -> dict[str, str]:
    """標題圖的提示詞存成 ``art.locations.title_screen``（出圖時當成一個地點）。"""
    art = art_gen.load_art(Path(work))
    locations = art.setdefault("locations", {})
    entry = locations.get(asset_manifest.TITLE_LOCATION_ID)
    entry = entry if isinstance(entry, dict) else {}
    entry.update({"tags": str(tags or "").strip(), "description": str(description or "").strip(), "confirmed": True})
    locations[asset_manifest.TITLE_LOCATION_ID] = entry
    art_gen.save_art(config, Path(work), art)
    return title_prompt(work)


def _title_asset(work: Path, asset_id: str, folder: str, exts: tuple[str, ...]) -> dict[str, Any]:
    manifest = art_gen.load_manifest(Path(work)) or {}
    item = next((a for a in manifest.get("assets", []) if a.get("asset_id") == asset_id), None)
    present = any((Path(work) / "assets" / folder / f"{asset_id}{ext}").is_file() for ext in exts)
    return {"asset_id": asset_id, "in_manifest": item is not None, "status": (item or {}).get("status", ""), "present": present}


def title_state(work: Path) -> dict[str, Any]:
    return {"bg": _title_asset(work, asset_manifest.TITLE_BG_ID, "bg", (".png",)),
            "bgm": _title_asset(work, asset_manifest.TITLE_BGM_ID, "bgm", AUDIO_EXTS),
            "prompt": title_prompt(work)}


# ------------------------------------------------------------------ 匯出


def project_ready(project: Path | str) -> bool:
    return (Path(project) / "game").is_dir()


def run_export(config: Any, work: Path, book_id: str, numbers: list[int], *, placeholders: bool = False, clean: bool = False) -> dict[str, Any]:
    """匯出選定章節到 Ren'Py 專案；回傳摘要（檔案數、警告、前置檢查結果）。"""
    if not numbers:
        raise ExportStudioError("請至少選一章")
    settings = load_settings(work, book_id)
    project = Path(settings["project"])
    if not project_ready(project):
        raise ExportStudioError(f"找不到 Ren'Py 專案：{project}（裡面要有 game 資料夾；請先用 Ren'Py Launcher 建立專案，或到「輸出」頁改專案路徑）")
    result = export_renpy.export_book(config, book_id, sorted(set(numbers)), project, clean=clean, placeholders=placeholders)
    errors = [i for i in result.preflight if i.get("level") == "error"]
    return {"project": str(project), "files": len(result.files), "chapters": sorted(set(numbers)), "warnings": list(result.warnings),
            "preflight_errors": len(errors), "preflight_warnings": len(result.preflight) - len(errors),
            "issues": [f"{i.get('file')}:{i.get('line')} {i.get('message')}" for i in result.preflight[:20]],
            "placeholders": ({k: len(v) for k, v in result.placeholders.items()} if result.placeholders else None)}


# ------------------------------------------------------------------ 驗收


def run_verify(config: Any, work: Path, book_id: str) -> dict[str, Any]:
    """對已匯出的專案跑 lint＋自動測試；報告放 ``build/reports/verify_<時間>/``。"""
    settings = load_settings(work, book_id)
    if not settings["sdk"]:
        raise ExportStudioError("請先填 Ren'Py SDK 路徑（例如 C:\\...\\renpy-8.5.3-sdk）")
    project = Path(settings["project"])
    if not project_ready(project):
        raise ExportStudioError(f"找不到 Ren'Py 專案：{project}；請先匯出")
    out = build_dir(work) / REPORTS_DIR / ("verify_" + time.strftime("%Y%m%d_%H%M%S"))
    try:
        summary = verify_renpy.verify(config, Path(settings["sdk"]), project, out)
    except verify_renpy.VerifyError as exc:
        raise ExportStudioError(str(exc)) from exc
    summary["report"] = out.name
    _prune_reports(work)
    return summary


def _prune_reports(work: Path) -> None:
    import shutil

    root = build_dir(work) / REPORTS_DIR
    for old in sorted(p for p in root.glob("verify_*") if p.is_dir())[:-REPORT_KEEP]:
        shutil.rmtree(old, ignore_errors=True)


def list_reports(work: Path) -> list[dict[str, Any]]:
    root = build_dir(work) / REPORTS_DIR
    rows = []
    for path in sorted((p for p in root.glob("verify_*") if p.is_dir()), reverse=True):
        shots = sorted(p.name for p in (path / "screens").glob("*.png")) if (path / "screens").is_dir() else []
        rows.append({"name": path.name, "has_report": (path / "report.md").is_file(), "screens": shots})
    return rows


def _safe_report_dir(work: Path, name: str) -> Path:
    if not name.startswith("verify_") or "/" in name or "\\" in name or ".." in name:
        raise ExportStudioError("報告名稱不合法")
    path = build_dir(work) / REPORTS_DIR / name
    if not path.is_dir():
        raise ExportStudioError("找不到這份報告")
    return path


def read_report(work: Path, name: str) -> str:
    path = _safe_report_dir(work, name) / "report.md"
    if not path.is_file():
        raise ExportStudioError("這份報告沒有 report.md（可能跑到一半被中斷）")
    return path.read_text(encoding="utf-8")


def report_shot(work: Path, name: str, shot: str) -> Path:
    if "/" in shot or "\\" in shot or not shot.endswith(".png"):
        raise ExportStudioError("截圖名稱不合法")
    path = _safe_report_dir(work, name) / "screens" / shot
    if not path.is_file():
        raise ExportStudioError("找不到這張截圖")
    return path


# ------------------------------------------------------------------ 打包


def dist_dir(work: Path) -> Path:
    """Windows 版成品資料夾：``works/<書>/build/dist``。"""
    return build_dir(work) / DIST_DIR


def run_package(config: Any, work: Path, book_id: str) -> dict[str, Any]:
    """用 Ren'Py ``distribute`` 打包 Windows 版；成品放 :func:`dist_dir`。"""
    settings = load_settings(work, book_id)
    if not settings["sdk"]:
        raise ExportStudioError("請先填 Ren'Py SDK 路徑（例如 C:\\...\\renpy-8.5.3-sdk）")
    project = Path(settings["project"])
    if not project_ready(project):
        raise ExportStudioError(f"找不到 Ren'Py 專案：{project}；請先匯出")
    try:
        return package_renpy.package(config, Path(settings["sdk"]), project, dist_dir(work))
    except package_renpy.PackageError as exc:
        raise ExportStudioError(str(exc)) from exc


# ------------------------------------------------------------------ 總覽


def overview(work: Path, book_id: str, chapter_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """輸出頁要的資料：章節（可選哪些）、標題素材、設定、報告清單。"""
    settings = load_settings(work, book_id)
    chapters = [{"chapter_no": r["chapter_no"], "chapter_id": r["chapter_id"], "title": r["title"],
                 "scenes_state": r["scenes_state"], "scene_count": r["scene_count"], "exportable": r["scene_count"] > 0}
                for r in chapter_rows]
    return {"chapters": chapters, "title": title_state(work), "settings": settings,
            "project_ready": project_ready(settings["project"]), "reports": list_reports(work),
            "dist": {"dir": str(dist_dir(work)), "artifacts": package_renpy.list_artifacts(dist_dir(work))}}
