"""package_renpy：用假的 runner 驗證指令、前提檢查與成品判讀（不需要 Ren'Py）。"""
from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

import pytest

from t2vn import package_renpy as m
from tests.export_renpy.test_export_renpy import make_env


def fake_sdk(tmp_path: Path) -> Path:
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "renpy.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return sdk


def exported(tmp_path: Path):
    from t2vn import export_renpy

    config, work, renpy = make_env(tmp_path)
    export_renpy.export_book(config, "bk", [1], renpy, placeholders=True)
    return config, work, renpy


def test_build_command():
    cmd = m.build_command(["/sdk/renpy.sh"], Path("/sdk"), Path("/p"), Path("/d"), "win")
    assert cmd == ["/sdk/renpy.sh", str(Path("/sdk").resolve() / "launcher"), "distribute", str(Path("/p")), "--destination", str(Path("/d")), "--package", "win", "--no-update"]


def test_list_artifacts_sorted_and_filtered(tmp_path):
    (tmp_path / "a.zip").write_bytes(b"1")
    (tmp_path / "b.zip").write_bytes(b"22")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    os.utime(tmp_path / "a.zip", (time.time() - 100, time.time() - 100))
    rows = m.list_artifacts(tmp_path)
    assert [r["name"] for r in rows] == ["b.zip", "a.zip"] and rows[0]["bytes"] == 2
    assert m.list_artifacts(tmp_path / "nope") == []


def test_package_success(tmp_path):
    config, work, renpy = exported(tmp_path)
    dest = tmp_path / "dist"
    seen = {}

    def runner(command, timeout):
        seen["command"], seen["timeout"] = command, timeout
        dest.mkdir(exist_ok=True)
        (dest / "bk-1.0-win.zip").write_bytes(b"zip")
        return 0, "Building...\nDone.\n"

    (renpy / "game" / "vn_verify_test.rpy").write_text("testsuite x:\n", encoding="utf-8")
    sdk = fake_sdk(tmp_path)
    result = m.package(config, sdk, renpy, dest, runner=runner, timeout=99)
    assert result["artifacts"][0]["name"] == "bk-1.0-win.zip" and result["code"] == 0 and result["package"] == "win"
    assert seen["command"][1:] == [str(sdk.resolve() / "launcher"), "distribute", str(renpy), "--destination", str(dest), "--package", "win", "--no-update"]
    assert seen["timeout"] == 99
    assert not (renpy / "game" / "vn_verify_test.rpy").exists()
    assert result["log_tail"][-1] == "Done."


def test_package_requires_game_folder(tmp_path):
    config, _, _ = make_env(tmp_path)
    with pytest.raises(m.PackageError, match="game"):
        m.package(config, fake_sdk(tmp_path), tmp_path / "empty", tmp_path / "dist", runner=lambda c, t: (0, ""))


def test_package_requires_sdk(tmp_path):
    config, _, renpy = exported(tmp_path)
    (tmp_path / "nosdk").mkdir()
    with pytest.raises(m.PackageError, match="Ren'Py"):
        m.package(config, tmp_path / "nosdk", renpy, tmp_path / "dist", runner=lambda c, t: (0, ""))


def test_package_blocks_on_preflight_errors(tmp_path):
    config, _, renpy = exported(tmp_path)
    (renpy / "game" / "vn_ch001.rpy").write_text("label ch001:\n\tjump vn_end\n", encoding="utf-8")  # Tab 縮排＝前置檢查錯誤
    called = []
    with pytest.raises(m.PackageError, match="前置檢查"):
        m.package(config, fake_sdk(tmp_path), renpy, tmp_path / "dist", runner=lambda c, t: called.append(1) or (0, ""))
    assert called == []


def test_package_failure_includes_log_tail(tmp_path):
    config, _, renpy = exported(tmp_path)
    with pytest.raises(m.PackageError, match="結束碼 2") as info:
        m.package(config, fake_sdk(tmp_path), renpy, tmp_path / "dist", runner=lambda c, t: (2, "a\nb\nboom\n"))
    assert "boom" in str(info.value)


def test_package_without_artifact_is_failure(tmp_path):
    config, _, renpy = exported(tmp_path)
    with pytest.raises(m.PackageError, match="沒有產生成品"):
        m.package(config, fake_sdk(tmp_path), renpy, tmp_path / "dist", runner=lambda c, t: (0, "ok\n"))


def test_old_artifacts_do_not_count(tmp_path):
    config, _, renpy = exported(tmp_path)
    dest = tmp_path / "dist"
    dest.mkdir()
    old = dest / "old-win.zip"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 1000, time.time() - 1000))
    with pytest.raises(m.PackageError, match="沒有產生成品"):
        m.package(config, fake_sdk(tmp_path), renpy, dest, runner=lambda c, t: (0, ""))


def make_zip(path: Path, names: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, "x")


def test_inspect_zip_flags_unshippable_files(tmp_path):
    make_zip(tmp_path / "a.zip", ["bk-1.0-win/bk.exe", "bk-1.0-win/game/script.rpyc", "bk-1.0-win/game/saves/auto-1-LT1.save",
                                  "bk-1.0-win/game/vn_verify_test.rpyc", "bk-1.0-win/traceback.txt"])
    info = m.inspect_zip(tmp_path / "a.zip")
    assert info["entries"] == 5 and info["has_exe"] is True
    assert info["forbidden"] == ["bk-1.0-win/game/saves/auto-1-LT1.save", "bk-1.0-win/game/vn_verify_test.rpyc", "bk-1.0-win/traceback.txt"]


def test_inspect_zip_clean_and_broken(tmp_path):
    make_zip(tmp_path / "ok.zip", ["bk-win/bk.exe", "bk-win/game/script.rpyc"])
    assert m.inspect_zip(tmp_path / "ok.zip")["forbidden"] == []
    (tmp_path / "bad.zip").write_bytes(b"not a zip")
    info = m.inspect_zip(tmp_path / "bad.zip")
    assert info["entries"] == 0 and info["error"]


def test_package_reports_zip_inspection(tmp_path):
    config, _, renpy = exported(tmp_path)
    dest = tmp_path / "dist"

    def runner(command, timeout):
        dest.mkdir(exist_ok=True)
        make_zip(dest / "bk-win.zip", ["bk-win/bk.exe", "bk-win/game/saves/x.save"])
        return 0, "ok\n"

    row = m.package(config, fake_sdk(tmp_path), renpy, dest, runner=runner)["artifacts"][0]
    assert row["has_exe"] is True and row["forbidden"] == ["bk-win/game/saves/x.save"]


def test_package_requires_current_export(tmp_path):
    config, _, renpy = exported(tmp_path)
    theme = renpy / "game" / "vn_theme.rpy"
    theme.write_text(theme.read_text(encoding="utf-8").replace('build.classify("tests/**", None)', ""), encoding="utf-8")
    called = []
    with pytest.raises(m.PackageError, match="重新匯出"):
        m.package(config, fake_sdk(tmp_path), renpy, tmp_path / "dist", runner=lambda c, t: called.append(1) or (0, ""))
    assert called == []
