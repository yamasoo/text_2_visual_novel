"""輸出頁後端：設定、標題素材、匯出、驗收（用假的跑法）、報告檢視與路由。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from t2vn import export_studio as m
from t2vn.server import create_app
from tests.export_renpy.test_export_renpy import make_env
from tests.studio.test_studio import imported, sample_ops, wait_job


def test_settings_default_and_roundtrip(tmp_path):
    config, work, _ = make_env(tmp_path)
    got = m.load_settings(work, "bk")
    assert got["sdk"] == "" and got["project"].endswith("renpy/game/bk".replace("/", __import__("os").sep))
    saved = m.save_settings(config, work, "bk", " sdk_dir ", " proj_dir ")
    assert saved == {"sdk": "sdk_dir", "project": "proj_dir"} and m.load_settings(work, "bk") == saved


def test_title_prompt_is_saved_as_a_location(tmp_path):
    config, work, _ = make_env(tmp_path)
    assert m.title_prompt(work) == {"tags": "", "description": ""}
    m.save_title_prompt(config, work, "title screen, sunset", "一座廢棄船塢，遠處有燈。")
    art = json.loads((work / "art" / "style.json").read_text(encoding="utf-8"))
    assert art["locations"]["title_screen"]["tags"] == "title screen, sunset" and art["locations"]["title_screen"]["confirmed"] is True
    assert m.title_prompt(work)["description"].startswith("一座")


def test_title_state_reports_files(tmp_path):
    _, work, _ = make_env(tmp_path)
    state = m.title_state(work)
    assert state["bg"]["present"] is False and state["bgm"]["present"] is False
    (work / "assets" / "bg").mkdir(parents=True)
    (work / "assets" / "bg" / "bg_title.png").write_bytes(b"png")
    (work / "assets" / "bgm").mkdir()
    (work / "assets" / "bgm" / "bgm_title.opus").write_bytes(b"x")
    state = m.title_state(work)
    assert state["bg"]["present"] and state["bgm"]["present"]


def test_run_export_errors_and_success(tmp_path):
    config, work, renpy = make_env(tmp_path)
    with pytest.raises(m.ExportStudioError):
        m.run_export(config, work, "bk", [])
    with pytest.raises(m.ExportStudioError, match="找不到 Ren'Py 專案"):
        m.run_export(config, work, "bk", [1])
    m.save_settings(config, work, "bk", "", str(renpy))
    summary = m.run_export(config, work, "bk", [1], placeholders=True)
    assert summary["chapters"] == [1] and summary["files"] > 3 and summary["preflight_errors"] == 0
    assert (renpy / "game" / "vn_ch001.rpy").is_file()


def test_run_verify_needs_sdk_and_project_then_writes_report(tmp_path, monkeypatch):
    config, work, renpy = make_env(tmp_path)
    with pytest.raises(m.ExportStudioError, match="SDK"):
        m.run_verify(config, work, "bk")
    m.save_settings(config, work, "bk", str(tmp_path / "sdk"), str(tmp_path / "nowhere"))
    with pytest.raises(m.ExportStudioError, match="找不到 Ren'Py 專案"):
        m.run_verify(config, work, "bk")
    m.save_settings(config, work, "bk", str(tmp_path / "sdk"), str(renpy))

    def fake_verify(cfg, sdk, project, out, **kw):
        out.mkdir(parents=True)
        (out / "report.md").write_text("# 報告\n", encoding="utf-8")
        (out / "screens").mkdir()
        (out / "screens" / "a.png").write_bytes(b"png")
        return {"cases": {"launch": True}}

    monkeypatch.setattr(m.verify_renpy, "verify", fake_verify)
    summary = m.run_verify(config, work, "bk")
    assert summary["report"].startswith("verify_") and summary["cases"] == {"launch": True}
    rows = m.list_reports(work)
    assert rows[0]["name"] == summary["report"] and rows[0]["screens"] == ["a.png"]
    assert m.read_report(work, summary["report"]).startswith("# 報告")
    assert m.report_shot(work, summary["report"], "a.png").read_bytes() == b"png"


def test_report_names_are_validated(tmp_path):
    _, work, _ = make_env(tmp_path)
    for bad in ("../x", "verify_/../..", "other", "verify_a/b"):
        with pytest.raises(m.ExportStudioError):
            m.read_report(work, bad)
    with pytest.raises(m.ExportStudioError):
        m.report_shot(work, "verify_x", "../a.png")


def test_old_reports_are_pruned(tmp_path, monkeypatch):
    config, work, _ = make_env(tmp_path)
    root = m.build_dir(work) / m.REPORTS_DIR
    for i in range(m.REPORT_KEEP + 3):
        (root / f"verify_2026110{i % 10}_{i:06d}").mkdir(parents=True)
    m._prune_reports(work)
    assert len(list(root.glob("verify_*"))) == m.REPORT_KEEP


def test_routes(tmp_path):
    config, studio = imported(tmp_path)
    app = create_app(config, llm_factory=lambda: None)
    http = TestClient(app, base_url="http://127.0.0.1:7866")
    data = http.get("/api/books/demo/export").json()
    assert data["chapters"] and data["project_ready"] is False and data["title"]["bg"]["present"] is False
    assert http.put("/api/books/demo/export/title", json={"tags": "a, b", "description": "d"}).json() == {"tags": "a, b", "description": "d"}
    assert http.put("/api/books/demo/export/settings", json={"sdk": "S", "project": "P"}).json() == {"sdk": "S", "project": "P"}
    started = http.post("/api/books/demo/export/run", json={"chapters": [1]}).json()
    done = wait_job(http, started["id"])
    assert done["status"] == "error" and any("找不到 Ren'Py 專案" in line for line in done["log"])
    assert http.get("/api/books/demo/export/report", params={"name": "../x"}).status_code == 400


def test_run_package_checks_then_delegates(tmp_path, monkeypatch):
    config, work, renpy = make_env(tmp_path)
    with pytest.raises(m.ExportStudioError, match="SDK"):
        m.run_package(config, work, "bk")
    m.save_settings(config, work, "bk", str(tmp_path / "sdk"), str(tmp_path / "nowhere"))
    with pytest.raises(m.ExportStudioError, match="找不到 Ren'Py 專案"):
        m.run_package(config, work, "bk")
    m.save_settings(config, work, "bk", str(tmp_path / "sdk"), str(renpy))
    seen = {}

    def fake(cfg, sdk, project, dest, **kw):
        seen.update(sdk=sdk, project=project, dest=dest)
        return {"artifacts": [{"name": "a.zip", "bytes": 3, "mtime": 1.0}]}

    monkeypatch.setattr(m.package_renpy, "package", fake)
    assert m.run_package(config, work, "bk")["artifacts"][0]["name"] == "a.zip"
    assert seen["dest"] == m.dist_dir(work) and seen["project"] == renpy

    def boom(*a, **k):
        raise m.package_renpy.PackageError("壞了")

    monkeypatch.setattr(m.package_renpy, "package", boom)
    with pytest.raises(m.ExportStudioError, match="壞了"):
        m.run_package(config, work, "bk")


def test_overview_lists_dist_artifacts(tmp_path):
    _, work, _ = make_env(tmp_path)
    assert m.overview(work, "bk", [])["dist"] == {"dir": str(m.dist_dir(work)), "artifacts": []}
    m.dist_dir(work).mkdir(parents=True)
    (m.dist_dir(work) / "bk-win.zip").write_bytes(b"zip")
    assert [a["name"] for a in m.overview(work, "bk", [])["dist"]["artifacts"]] == ["bk-win.zip"]


def test_package_route_reports_missing_sdk(tmp_path):
    config, studio = imported(tmp_path)
    app = create_app(config, llm_factory=lambda: None)
    http = TestClient(app, base_url="http://127.0.0.1:7866")
    started = http.post("/api/books/demo/export/package").json()
    done = wait_job(http, started["id"])
    assert done["status"] == "error" and any("SDK" in line for line in done["log"])
