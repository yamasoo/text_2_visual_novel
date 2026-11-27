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


def _base_config(tmp_path, name="p", **comfyui):
    # 每個案例用不同的子資料夾：make() 會建 proj/，同一個路徑不能建兩次。
    base = tmp_path / name
    base.mkdir(parents=True, exist_ok=True)
    cfg, _root = make(base, comfyui={"base_url": "http://127.0.0.1:8188", **comfyui})
    return cfg


def test_comfyui_profiles_parse_and_default_must_exist(tmp_path):
    # QQ-1：profiles 是正本（VN 與漫畫共用）；沒寫就是空字典，呼叫端各自 fallback。
    cfg = _base_config(tmp_path, "ok", profiles={"moss": {"kind": "moss",
                                                     "models": {"unet": "u", "clip": "c", "vae": "v"},
                                                     "negative": "nsfw", "presets": {"final": {"steps": 25}}}},
                           default_profile="moss")
    assert list(cfg.comfy_profiles) == ["moss"] and cfg.comfy_default_profile == "moss"
    assert cfg.comfy_profiles["moss"]["kind"] == "moss"
    assert cfg.comfy_profiles["moss"]["models"]["unet"] == "u"
    assert cfg.comfy_profiles["moss"]["presets"]["final"]["steps"] == 25
    assert _base_config(tmp_path, "empty").comfy_profiles == {}
    with pytest.raises(ConfigError, match="kind"):
        _base_config(tmp_path, "bad-kind", profiles={"bad": {"kind": "sdxl"}})
    with pytest.raises(ConfigError, match="default_preset"):
        _base_config(tmp_path, "bad-preset", profiles={"moss": {"kind": "moss", "presets": {"turbo": {"steps": 4}}, "default_preset": "final"}}, default_profile="moss")
    with pytest.raises(ConfigError, match="default_profile"):
        _base_config(tmp_path, "bad-default", profiles={"moss": {"kind": "moss"}}, default_profile="nope")


def test_comfyui_profile_loras_parse_and_validate(tmp_path):
    cfg = _base_config(
        tmp_path, "loras-ok",
        profiles={"anima": {
            "kind": "anima",
            "loras": [
                {"name": "style\\watercolor.safetensors", "strength": 0.75, "on": True, "trigger": "watercolor style"},
                {"name": "detail.safetensors", "on": False},
            ],
        }},
        default_profile="anima",
    )
    loras = cfg.comfy_profiles["anima"]["loras"]
    assert len(loras) == 2
    assert loras[0] == {"name": "style\\watercolor.safetensors", "strength": 0.75, "on": True, "trigger": "watercolor style"}
    assert loras[1] == {"name": "detail.safetensors", "strength": 1.0, "on": False, "trigger": ""}
    with pytest.raises(ConfigError, match="沒有 name"):
        _base_config(tmp_path, "loras-empty", profiles={"anima": {"kind": "anima", "loras": [{"name": ""}]}})
    with pytest.raises(ConfigError, match="strength"):
        _base_config(tmp_path, "loras-bad-str", profiles={"anima": {"kind": "anima", "loras": [{"name": "a.safetensors", "strength": "high"}]}})
    with pytest.raises(ConfigError, match="on"):
        _base_config(tmp_path, "loras-bad-on", profiles={"anima": {"kind": "anima", "loras": [{"name": "a.safetensors", "on": "yes"}]}})
