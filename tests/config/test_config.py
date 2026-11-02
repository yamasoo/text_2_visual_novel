import pytest

from t2vn.config import init_config, load_config, parse_config
from t2vn.errors import ConfigError, WriteProtectedError


def make(tmp_path, **over):
    root = tmp_path / "proj"
    root.mkdir()
    raw = {
        "project_root": str(root),
        "book_library": {"path": str(tmp_path / "lib"), "access": "read_only"},
        "reference_sources": {"ns": {"path": str(tmp_path / "ns"), "access": "read_only", "commit": "abc"}},
        "books": [{"book_id": "sample_book", "source_dir": "folder", "lang": "zh-TW", "display_name": {"zh-TW": "x"}}],
    }
    raw.update(over)
    return parse_config(raw), root


def test_basic(tmp_path):
    cfg, root = make(tmp_path)
    assert cfg.book("sample_book").lang == "zh-TW"
    assert cfg.work_dir("sample_book") == root / "works" / "sample_book"
    assert cfg.reference_commits["ns"] == "abc"


def test_write_protection(tmp_path):
    cfg, root = make(tmp_path)
    assert cfg.assert_writable(root / "works" / "a.txt")
    with pytest.raises(WriteProtectedError):
        cfg.assert_writable(tmp_path / "lib" / "folder" / "a.txt")
    with pytest.raises(WriteProtectedError):
        cfg.assert_writable(tmp_path / "ns" / "a.txt")
    with pytest.raises(WriteProtectedError):
        cfg.assert_writable(tmp_path / "elsewhere.txt")


def test_rejects_non_ascii_key(tmp_path):
    with pytest.raises(ConfigError):
        make(tmp_path, **{chr(0x66f8): 1})


def test_rejects_writable_access(tmp_path):
    with pytest.raises(ConfigError):
        make(tmp_path, book_library={"path": str(tmp_path / "lib"), "access": "read_write"})


@pytest.mark.parametrize("bad", ["Bad", "1abc", "a", "../x"])
def test_rejects_bad_book_id(tmp_path, bad):
    with pytest.raises(ConfigError):
        make(tmp_path, books=[{"book_id": bad, "source_dir": "f", "lang": "zh-TW", "display_name": {"zh-TW": "x"}}])


def test_rejects_overlap(tmp_path):
    root = tmp_path / "proj"
    with pytest.raises(ConfigError):
        parse_config({"project_root": str(root), "book_library": {"path": str(root / "lib")}})


def test_rejects_escaping_source_dir(tmp_path):
    with pytest.raises(ConfigError):
        make(tmp_path, books=[{"book_id": "abc", "source_dir": "../x", "lang": "zh-TW", "display_name": {"zh-TW": "x"}}])


# ---- 第一次使用：從範例檔產生 config.yaml


def example_text(old_root, library):
    """範例檔內容；路徑用參數傳，避免在原始碼裡寫死磁碟機代號。"""
    return (
        "# 這是範例\n"
        f"project_root: {old_root}        # 會改成實際位置\n"
        "book_library:\n"
        f"  path: {library}               # 唯讀\n"
        "  access: read_only\n"
    )


def test_init_config_rewrites_project_root_and_keeps_comments(tmp_path):
    old_root = tmp_path / "old-checkout"
    example = tmp_path / "config.example.yaml"
    example.write_text(example_text(old_root, tmp_path / "novels"), encoding="utf-8")
    dest = tmp_path / "config.yaml"

    created = init_config(dest, example=example, project_root=tmp_path)

    assert created == dest
    text = dest.read_text(encoding="utf-8")
    assert f"project_root: {tmp_path}" in text
    assert str(old_root) not in text
    assert "# 這是範例" in text and str(tmp_path / "novels") in text
    assert "# 唯讀" in text


def test_init_config_refuses_to_overwrite(tmp_path):
    example = tmp_path / "config.example.yaml"
    example.write_text(example_text(tmp_path / "old", tmp_path / "lib"), encoding="utf-8")
    dest = tmp_path / "config.yaml"
    dest.write_text("project_root: mine\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        init_config(dest, example=example)
    assert dest.read_text(encoding="utf-8") == "project_root: mine\n"


def test_init_config_reports_missing_example(tmp_path):
    with pytest.raises(ConfigError):
        init_config(tmp_path / "config.yaml", example=tmp_path / "nope.yaml")


def test_init_config_output_is_loadable(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    library = tmp_path / "novels"
    library.mkdir()
    example = tmp_path / "config.example.yaml"
    example.write_text(example_text(tmp_path / "old", library), encoding="utf-8")
    dest = project / "config.yaml"
    init_config(dest, example=example, project_root=project)

    config = load_config(dest)
    assert config.project_root == project
    assert config.book_library == library
