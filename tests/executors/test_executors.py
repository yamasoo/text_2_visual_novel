"""AI 執行者設定：合併、驗證、舊寫法轉換、設定頁讀寫、連線測試、任務指定執行者。"""
from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from t2vn import executors
from t2vn.config import load_config, parse_config
from t2vn.errors import ConfigError
from t2vn.llm import LlmError, ReplayLLM
from t2vn.server import create_app
from t2vn.settings import SettingsError, apply_settings, check_connection, read_settings, store_key


def ids(rows):
    return [r["id"] for r in rows]


def test_builtin_providers_have_project_overlay():
    rows = executors.build_providers({})
    by = {r["id"]: r for r in rows}
    assert "deepseek" in by and "llamacpp" in by and "claude_code" not in by
    assert by["deepseek"]["model"] == "deepseek-flash" and by["deepseek"]["max_output"] == 32768
    assert by["llamacpp"]["max_output"] == 16000


def test_overrides_and_custom_provider():
    rows = executors.build_providers({"llamacpp": {"model": "qwen3", "context_tokens": 65536},
                                      "my_server": {"base_url": "http://10.0.0.5:8000/v1", "model": "foo"}})
    by = {r["id"]: r for r in rows}
    assert by["llamacpp"]["model"] == "qwen3" and by["llamacpp"]["context_tokens"] == 65536
    assert by["my_server"]["builtin"] is False and by["my_server"]["base_url"].endswith("/v1")
    assert executors.resolve_executor(rows, "my_server")["model"] == "foo"
    assert executors.resolve_executor(rows, "deepseek/deepseek-v4-pro")["model"] == "deepseek-v4-pro"
    with pytest.raises(ConfigError):
        executors.resolve_executor(rows, "llamacpp/unlisted")


@pytest.mark.parametrize("overrides", [
    {"claude_code": {"base_url": "http://x/v1"}},
    {"Bad-Id": {"base_url": "http://x/v1"}},
    {"custom": {"model": "m"}},                       # 自訂供應商沒有 base_url
    {"deepseek": {"nope": 1}},
    {"deepseek": {"max_output": "lots"}},
    {"deepseek": {"temperature": 9}},
    {"deepseek": {"base_url": "ftp://x"}},
    {"deepseek": {"reasoning": {"structured": "extreme"}}},
])
def test_invalid_overrides_rejected(overrides):
    with pytest.raises(ConfigError):
        executors.build_providers(overrides)


def test_unknown_executor_rejected():
    with pytest.raises(ConfigError):
        executors.resolve_executor(executors.build_providers({}), "ghost")


def test_legacy_llm_section_is_converted():
    section = executors.normalize_llm_section({"provider": "deepseek", "model": "deepseek-v4-pro", "thinking": "high", "max_output": 20000})
    assert section["default"] == "deepseek"
    assert section["providers"]["deepseek"]["model"] == "deepseek-v4-pro"
    assert section["providers"]["deepseek"]["reasoning"]["structured"] == "high"
    assert section["providers"]["deepseek"]["max_output"] == 20000


def make_cfg(tmp_path, llm):
    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    raw = {"book_library": {"path": str(library), "access": "read_only"}, "llm": llm}
    (project / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return project / "config.yaml"


def test_config_exposes_derived_fields_for_both_forms(tmp_path):
    legacy = load_config(make_cfg(tmp_path, {"provider": "deepseek", "model": "deepseek-v4-pro", "thinking": "low", "max_output": 8000}))
    assert (legacy.llm_provider, legacy.llm_model, legacy.llm_thinking, legacy.llm_max_output) == ("deepseek", "deepseek-v4-pro", "low", 8000)
    new = load_config(make_cfg(tmp_path, {"default": "llamacpp", "providers": {"llamacpp": {"model": "qwen3", "max_output": 4000}}}))
    assert (new.llm_provider, new.llm_model, new.llm_max_output, new.llm_default) == ("llamacpp", "qwen3", 4000, "llamacpp")


def test_check_fit_blocks_oversized_prompt():
    prov = executors.resolve_executor(executors.build_providers({"llamacpp": {"context_tokens": 4096}}), "llamacpp")
    executors.check_fit([{"role": "user", "content": "短"}], prov)
    with pytest.raises(LlmError):
        executors.check_fit([{"role": "user", "content": "字" * 20000}], prov)


def test_apply_settings_new_and_legacy(tmp_path):
    path = make_cfg(tmp_path, {"provider": "deepseek", "model": "deepseek-flash", "thinking": "off", "max_output": 32768})
    cfg = apply_settings(path, {"executors": {"default": "llamacpp", "providers": {
        "llamacpp": {"base_url": "http://127.0.0.1:9000/v1", "model": "qwen3", "context_tokens": 65536},
        "mine": {"base_url": "http://10.0.0.5:8000/v1", "model": "foo"}}}})
    assert cfg.llm_default == "llamacpp" and cfg.llm_model == "qwen3"
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["llm"]
    assert saved["default"] == "llamacpp" and "provider" not in saved and "mine" in saved["providers"]
    assert saved["providers"]["deepseek"]["max_output"] == 32768  # 舊設定被保留成覆寫
    # null 表示回到預設；remove 刪掉自訂供應商
    cfg = apply_settings(path, {"executors": {"providers": {"llamacpp": {"model": None}}, "remove": ["mine"]}})
    assert cfg.llm_model == "" and "mine" not in cfg.llm_overrides
    # 舊寫法仍可用
    cfg = apply_settings(path, {"llm": {"provider": "deepseek", "model": "deepseek-v4-pro", "thinking": "low", "max_output": 9000}})
    assert (cfg.llm_default, cfg.llm_model, cfg.llm_thinking, cfg.llm_max_output) == ("deepseek", "deepseek-v4-pro", "low", 9000)


def test_apply_settings_invalid_leaves_file_untouched(tmp_path):
    path = make_cfg(tmp_path, {"default": "deepseek"})
    before = path.read_text(encoding="utf-8")
    for bad in ({"executors": {"providers": {"deepseek": {"max_output": "x"}}}},
                {"executors": {"providers": {"newone": {"model": "m"}}}},
                {"executors": {"default": "ghost"}}):
        with pytest.raises(SettingsError):
            apply_settings(path, bad)
    assert path.read_text(encoding="utf-8") == before


def test_read_settings_shapes(tmp_path):
    path = make_cfg(tmp_path, {"default": "deepseek"})
    data = read_settings(load_config(path), path)
    assert data["executors"]["default"] == "deepseek"
    assert {"deepseek", "llamacpp"} <= set(ids(data["executors"]["providers"]))
    for row in data["executors"]["providers"]:
        assert set(row["key"]) == {"needed", "found", "where"} and "api_key" not in row
    assert data["llm"]["provider"] == "deepseek" and data["llm"]["providers"][0]["models"]
    assert any(c["id"] == "deepseek" for c in data["executors"]["choices"])


def test_store_key_only_for_known_provider(tmp_path, monkeypatch):
    path = make_cfg(tmp_path, {"default": "deepseek"})
    config = load_config(path)
    monkeypatch.setattr("t2vn.settings.set_key", lambda provider, key: f"saved:{provider}")
    assert store_key("llamacpp", "k", config) == "saved:llamacpp"
    with pytest.raises(SettingsError):
        store_key("ghost", "k", config)


def test_check_connection_lists_models_when_model_unset(tmp_path, monkeypatch):
    from vendor.novel_studio import providers

    config = load_config(make_cfg(tmp_path, {"default": "llamacpp"}))
    monkeypatch.setattr(providers, "list_models", lambda home, prov, timeout=15: ["a.gguf", "b.gguf"])
    out = check_connection(config, "llamacpp")
    assert out["ok"] and out["models"] == ["a.gguf", "b.gguf"] and out["model"] == ""

    def boom(home, prov, timeout=15):
        raise providers.ProviderError("連不上")
    monkeypatch.setattr(providers, "list_models", boom)
    out = check_connection(config, "llamacpp")
    assert not out["ok"] and "連不上" in out["message"]
    assert not check_connection(config, "ghost")["ok"]


def test_server_test_route_and_job_executor(tmp_path, monkeypatch):
    path = make_cfg(tmp_path, {"default": "deepseek"})
    config = load_config(path)
    seen = {}
    monkeypatch.setattr("t2vn.server.check_connection", lambda c, executor=None: seen.setdefault("executor", executor) or {"ok": True})
    http = TestClient(create_app(config, llm_factory=lambda: ReplayLLM([]), config_path=path), base_url="http://127.0.0.1:7866")
    assert http.post("/api/settings/test", json={"executor": "llamacpp"}).status_code == 200
    assert seen["executor"] == "llamacpp"
    assert http.post("/api/settings/test").status_code == 200
    state = http.get("/api/state").json()
    assert state["default_executor"] == "deepseek"
    put = http.put("/api/settings", json={"executors": {"default": "llamacpp"}}).json()
    assert put["executors"]["default"] == "llamacpp"
