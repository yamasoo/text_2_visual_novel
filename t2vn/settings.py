"""網頁「設定」頁的後端：讀取、驗證、寫回 config.yaml，並管理金鑰與連線測試。

- 可以從網頁改的：AI 執行者（供應商、模型、預設執行者，見 ``executors.py``）、``book_library.path``、``works_dir``、
  ``comfyui.base_url``、``server.port``。其他欄位（books、reference_sources…）原樣保留。
- 寫回時會重寫整個檔案（YAML 註解不保留），舊檔先備份成 ``config.yaml.bak``。
- 金鑰不寫進 config.yaml，只存系統 keyring（沿用 novel-studio 的 service 名稱）；API 從不回傳金鑰本身。

API 形狀（``read_settings`` 的回傳）：

- ``executors``（新）：``{"default", "providers": [完整供應商設定＋key 狀態], "choices", "adapters", "efforts"}``
- ``llm``（舊，給舊版設定頁用，之後會拿掉）：``{"provider", "model", "thinking", "max_output", "key_found", "key_where", ...}``

寫入（``apply_settings`` 的 changes）：

- ``changes["executors"] = {"default": "deepseek/deepseek-v4-pro", "providers": {"llamacpp": {"base_url": "...", "model": "..."}},
  "remove": ["my_server"]}``。providers 裡每個欄位只寫要改的；值為 ``null`` 表示「回到預設」（自訂供應商的必要欄位除外）。
- ``changes["llm"] = {"provider", "model", "thinking", "max_output"}``（舊寫法，仍可用）。
"""
from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import yaml

from . import executors
from .config import Config, ConfigError, parse_config
from .errors import T2vnError
from .llm import LlmError, key_status_for, make_llm_from, set_key

THINKING = ["off", "low", "medium", "high", "max"]


class SettingsError(T2vnError):
    """設定值不合格式或無法寫入。"""


def _provider_rows(config: Config) -> list[dict[str, Any]]:
    rows = []
    for prov in executors.build_providers(config.llm_overrides):
        row = copy.deepcopy(prov)
        row["reasoning"] = {"adapter": (prov.get("reasoning") or {}).get("adapter", "none"), executors.STAGE: executors.structured_effort(prov)}
        row["key"] = key_status_for(prov)
        rows.append(row)
    return rows


def read_settings(config: Config, config_path: Path | None, running_port: int | None = None) -> dict[str, Any]:
    from vendor.novel_studio import providers as vendor_providers

    rows = _provider_rows(config)
    current = next((r for r in rows if r["id"] == config.llm_provider), rows[0])
    default_choices = executors.choices(executors.build_providers(config.llm_overrides))
    legacy_providers = [{"id": r["id"], "label": r["name"], "needs_key": r["key"]["needed"],
                         "models": list(dict.fromkeys([m for m in [r.get("model")] + [x["id"] for x in r.get("models") or []] if m]))}
                        for r in rows]
    return {
        "config_path": str(config_path) if config_path else "",
        "executors": {"default": config.llm_default, "providers": rows, "choices": default_choices,
                      "adapters": dict(vendor_providers.ADAPTERS), "efforts": list(vendor_providers.EFFORTS)},
        "llm": {"provider": config.llm_provider, "model": config.llm_model, "thinking": config.llm_thinking,
                "max_output": config.llm_max_output, "key_found": current["key"]["found"] or not current["key"]["needed"],
                "key_where": current["key"]["where"], "providers": legacy_providers, "thinking_values": THINKING},
        "paths": {"project_root": str(config.project_root), "works_dir": str(config.works_dir),
                  "works_dir_is_default": config.works_dir_override is None,
                  "book_library": str(config.book_library), "library_exists": config.book_library.is_dir()},
        "comfyui": {"base_url": config.comfyui_base_url},
        "server": {"port": config.server_port, "running_port": running_port},
    }


def _merge_llm_changes(raw: dict[str, Any], changes: dict[str, Any]) -> None:
    """把 changes 裡的 llm／executors 變更合併進 raw["llm"]（寫成新寫法，舊的 provider/model/thinking/max_output 鍵會被轉換掉）。"""
    section = executors.normalize_llm_section(raw.get("llm"))
    overrides: dict[str, dict[str, Any]] = section["providers"]
    default = section["default"]

    legacy = changes.get("llm") or {}
    if legacy:
        provider = str(legacy.get("provider") or "").strip() or None
        target = overrides.setdefault(provider or default.partition("/")[0], {})
        if provider:
            default = provider
        if "model" in legacy and str(legacy["model"]).strip():
            target["model"] = str(legacy["model"]).strip()
        if "max_output" in legacy:
            target["max_output"] = int(legacy["max_output"])
        if "thinking" in legacy:
            target.setdefault("reasoning", {})[executors.STAGE] = str(legacy["thinking"]).strip()

    new = changes.get("executors") or {}
    for pid in new.get("remove") or []:
        if any(p["id"] == pid and p.get("builtin") for p in executors.build_providers({})):
            overrides.pop(pid, None)  # 內建的不能刪，只能回到預設
        else:
            overrides.pop(pid, None)
            if default.partition("/")[0] == pid:
                default = executors.DEFAULT_EXECUTOR
    for pid, fields in (new.get("providers") or {}).items():
        if not isinstance(fields, dict):
            raise SettingsError(f"executors.providers.{pid} must be an object")
        entry = overrides.setdefault(pid, {})
        for key, value in fields.items():
            if value is None:
                entry.pop(key, None)
            elif key == "reasoning" and isinstance(value, dict) and isinstance(entry.get("reasoning"), dict):
                entry["reasoning"] = {**entry["reasoning"], **value}
            else:
                entry[key] = value
        if not entry:
            overrides.pop(pid, None)
    if new.get("default"):
        default = str(new["default"]).strip()
    raw["llm"] = {"default": default, "providers": overrides}


def apply_settings(config_path: Path, changes: dict[str, Any]) -> Config:
    """把網頁送來的變更寫進 config.yaml。先用 ``parse_config`` 驗證，通過才寫檔。回傳新的 Config。"""
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsError(f"cannot read {config_path.name}: {exc}") from exc

    if changes.get("llm") or changes.get("executors"):
        try:
            _merge_llm_changes(raw, changes)
        except (ConfigError, ValueError, TypeError) as exc:
            raise SettingsError(str(exc)) from exc

    paths = changes.get("paths") or {}
    if paths.get("book_library"):
        library = Path(str(paths["book_library"]).strip()).expanduser()
        if not library.is_dir():
            raise SettingsError(f"book library folder not found: {library}")
        raw["book_library"] = {**(raw.get("book_library") or {}), "path": str(library), "access": "read_only"}
    if "works_dir" in paths:
        value = str(paths["works_dir"] or "").strip()
        if value:
            raw["works_dir"] = str(Path(value).expanduser())
        else:
            raw.pop("works_dir", None)
    if (changes.get("comfyui") or {}).get("base_url"):
        raw["comfyui"] = {**(raw.get("comfyui") or {}), "base_url": str(changes["comfyui"]["base_url"]).strip()}
    if (changes.get("server") or {}).get("port"):
        raw["server"] = {**(raw.get("server") or {}), "port": int(changes["server"]["port"])}

    try:
        config = parse_config(raw, base_dir=config_path.parent)
    except (ConfigError, ValueError) as exc:
        raise SettingsError(str(exc)) from exc
    works = config.works_dir
    try:
        works.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SettingsError(f"cannot create works folder {works}: {exc}") from exc

    backup = config_path.with_name(config_path.name + ".bak")
    shutil.copyfile(config_path, backup)
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return config


def store_key(provider: str, key: str, config: Config | None = None) -> str:
    """存金鑰到系統 keyring。供應商必須存在於設定裡（內建或自訂）。"""
    known = {p["id"] for p in executors.build_providers(config.llm_overrides if config else {})}
    if provider not in known:
        raise SettingsError(f"unknown provider {provider!r}")
    try:
        return set_key(provider, key)
    except LlmError as exc:
        raise SettingsError(str(exc)) from exc


def check_connection(config: Config, executor: str | None = None, secrets_home: str | None = None) -> dict[str, Any]:
    """確認連線。回傳 ``{ok, message, model, tokens, models}``。

    - 先向服務要模型清單（``/models``）；要不到不算失敗（有些服務沒有這個端點）。
    - 還沒填模型名稱（本機 llama.cpp 常見）：有拿到清單就回報清單，請使用者挑一個；不打聊天請求。
    - 有模型名稱：打一個最小的聊天請求，確認金鑰、模型名稱與網路都可用。
    """
    from vendor.novel_studio import providers as vendor_providers

    try:
        prov = executors.resolve_executor(executors.build_providers(config.llm_overrides), executor or config.llm_default)
    except ConfigError as exc:
        return {"ok": False, "message": str(exc), "models": []}
    home = secrets_home or str(Path.home() / ".t2vn")
    models: list[str] = []
    list_error = ""
    try:
        models = vendor_providers.list_models(home, prov)
    except vendor_providers.ProviderError as exc:
        list_error = str(exc)
    if not prov.get("model"):
        if models:
            return {"ok": True, "message": f"連線成功，找到 {len(models)} 個模型；請選一個填進「模型名稱」。", "model": "",
                    "tokens": 0, "models": models}
        return {"ok": False, "message": list_error or "沒有取得模型清單，也還沒填模型名稱。", "models": []}
    try:
        llm = make_llm_from(prov, home, timeout=60)
        result = llm([{"role": "system", "content": "Reply with the JSON object {\"ok\": true} and nothing else."},
                      {"role": "user", "content": "ping"}])
    except LlmError as exc:
        return {"ok": False, "message": str(exc), "models": models}
    usage = result.usage or {}
    return {"ok": True, "message": result.text.strip()[:80], "model": result.model,
            "tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0), "models": models}
