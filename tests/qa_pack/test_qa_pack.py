"""qa_pack：產生獨立驗收包（不含開發過程文件）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from t2vn import export_renpy
from t2vn import qa_pack as m
from tests.export_renpy.test_export_renpy import make_env


def make_repo(root: Path, with_qa: bool = True) -> Path:
    for rel in ("CLAUDE.md", "docs/05_資料格式規格.md", "docs/20_標註規則.md", "docs/30_RenPy匯出規格.md",
                "docs/HANDOFF.md", "docs/decisions.md", "docs/tasks/t_x.md"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n", encoding="utf-8")
    if with_qa:
        (root / "docs" / "qa").mkdir()
        (root / "docs/qa/驗收清單.md").write_text("# 清單\n", encoding="utf-8")
        (root / "docs/qa/交接說明.md").write_text("# 說明\n", encoding="utf-8")
    (root / "schema").mkdir()
    (root / "schema" / "scene.schema.json").write_text("{}", encoding="utf-8")
    return root


def exported(tmp_path: Path):
    config, work, renpy = make_env(tmp_path)
    export_renpy.export_book(config, "bk", [1], renpy, placeholders=True)
    (renpy / "game" / "vn_verify_test.rpy").write_text("testsuite x:\n", encoding="utf-8")
    return config, work, renpy


def test_exported_chapters(tmp_path):
    _, _, renpy = exported(tmp_path)
    assert m.exported_chapters(renpy) == [1]
    assert m.exported_chapters(tmp_path / "nope") == []


def test_build_pack_contents_and_exclusions(tmp_path):
    config, work, renpy = exported(tmp_path)
    repo = make_repo(tmp_path / "repo")
    report = work / "build" / "reports" / "verify_20260101_000000"
    (report / "screens").mkdir(parents=True)
    (report / "report.md").write_text("# 報告\n", encoding="utf-8")
    (report / "screens" / "a.png").write_bytes(b"png")
    out = tmp_path / "project" / "reports" / "qa_pack"
    result = m.build_pack(config, repo, work, renpy, out, book_id="bk", dist_note="某資料夾")
    assert result["chapters"] == [1] and result["missing"] == []
    for rel in ("CLAUDE.md", "docs/05_資料格式規格.md", "docs/qa/驗收清單.md", "schema/scene.schema.json", "scenes/ch001.json",
                "source/ch001.json", "cast.json", "book.json", "renpy/game/vn_ch001.rpy", "renpy/game/vn_export.json",
                "renpy/media_inventory.tsv", "verify_report/report.md", "verify_report/screens/a.png", "PACK.md"):
        assert (out / rel).is_file(), rel
    assert not (out / "docs/HANDOFF.md").exists() and not (out / "docs/decisions.md").exists() and not (out / "docs/tasks").exists()
    assert not (out / "renpy/game/vn_verify_test.rpy").exists()
    assert not list(out.rglob("*.png")) or all("verify_report" in str(p) for p in out.rglob("*.png"))
    pack = (out / "PACK.md").read_text(encoding="utf-8")
    assert "第 1 章" in pack and "某資料夾" in pack and "HANDOFF.md" in pack and "`cast.json`" in pack
    assert json.loads((out / "scenes/ch001.json").read_text(encoding="utf-8")) == json.loads(
        (work / "scenes" / "ch001.json").read_text(encoding="utf-8"))
    assert result["files"] >= 14


def test_missing_optional_files_are_listed(tmp_path):
    config, work, renpy = exported(tmp_path)
    repo = make_repo(tmp_path / "repo", with_qa=False)
    out = tmp_path / "project" / "reports" / "qa_pack"
    result = m.build_pack(config, repo, work, renpy, out)
    assert "docs/qa/驗收清單.md" in result["missing"] and any("驗收報告" in x for x in result["missing"])
    assert "缺少的選用檔案" in (out / "PACK.md").read_text(encoding="utf-8")


def test_scenes_fall_back_to_gold(tmp_path):
    config, work, renpy = make_env(tmp_path, scenes_dir="gold")
    export_renpy.export_book(config, "bk", [1], renpy, placeholders=True)
    out = tmp_path / "project" / "reports" / "qa_pack"
    m.build_pack(config, make_repo(tmp_path / "repo"), work, renpy, out)
    assert (out / "scenes" / "ch001.json").is_file()


def test_errors(tmp_path):
    config, work, renpy = exported(tmp_path)
    repo = make_repo(tmp_path / "repo")
    out = tmp_path / "project" / "reports" / "qa_pack"
    with pytest.raises(m.QaPackError, match="game"):
        m.build_pack(config, repo, work, tmp_path / "empty", out)
    with pytest.raises(m.QaPackError, match="第 2 章"):
        m.build_pack(config, repo, work, renpy, out, chapters=[2])
    out.mkdir(parents=True)
    (out / "x.txt").write_text("x", encoding="utf-8")
    with pytest.raises(m.QaPackError, match="不是空的"):
        m.build_pack(config, repo, work, renpy, out)
    with pytest.raises(m.QaPackError, match="不能寫入"):
        m.build_pack(config, repo, work, renpy, tmp_path / "library" / "bookdir" / "pack")
    with pytest.raises(m.QaPackError, match="不能寫入"):
        m.build_pack(config, repo, work, renpy, tmp_path / "elsewhere" / "pack")
    (work / "cast.json").unlink()
    with pytest.raises(m.QaPackError, match="角色表"):
        m.build_pack(config, repo, work, renpy, tmp_path / "project" / "reports" / "other")


def test_media_inventory(tmp_path):
    game = tmp_path / "game"
    (game / "images" / "bg").mkdir(parents=True)
    (game / "audio").mkdir()
    (game / "images" / "bg" / "a.png").write_bytes(b"123")
    (game / "audio" / "b.opus").write_bytes(b"12")
    (game / "other.txt").write_bytes(b"x")
    assert m.media_inventory(game) == ["audio/b.opus\t2", "images/bg/a.png\t3"]
    assert m.media_inventory(tmp_path / "none") == []


def test_latest_report(tmp_path):
    assert m.latest_report(tmp_path) is None
    for name in ("verify_20260101_000000", "verify_20260102_000000", "other"):
        (tmp_path / "build" / "reports" / name).mkdir(parents=True)
    assert m.latest_report(tmp_path).name == "verify_20260102_000000"


def test_big_screenshots_are_shrunk_to_jpeg(tmp_path, monkeypatch):
    from PIL import Image

    config, work, renpy = exported(tmp_path)
    report = work / "build" / "reports" / "verify_20260101_000000"
    (report / "screens").mkdir(parents=True)
    (report / "report.md").write_text("# 報告\n", encoding="utf-8")
    Image.effect_noise((2400, 1350), 80).convert("RGB").save(report / "screens" / "big.png")
    Image.new("RGB", (100, 60), "red").save(report / "screens" / "small.png")
    monkeypatch.setattr(m, "SHRINK_OVER", 100_000)
    assert (report / "screens" / "big.png").stat().st_size > 100_000
    out = tmp_path / "project" / "reports" / "qa_pack"
    result = m.build_pack(config, make_repo(tmp_path / "repo"), work, renpy, out)
    assert result["shrunk"] == 1
    assert (out / "verify_report/screens/big.jpg").is_file() and not (out / "verify_report/screens/big.png").exists()
    assert (out / "verify_report/screens/small.png").is_file()
    with Image.open(out / "verify_report/screens/big.jpg") as image:
        assert image.width == m.SHRINK_WIDTH
    assert "已縮成 JPEG" in (out / "PACK.md").read_text(encoding="utf-8")
    out2 = tmp_path / "project" / "reports" / "qa_pack2"
    assert m.build_pack(config, make_repo(tmp_path / "repo2"), work, renpy, out2, shrink_screens=False)["shrunk"] == 0
    assert (out2 / "verify_report/screens/big.png").is_file()
