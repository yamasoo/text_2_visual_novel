"""AI 執行者（供應商＋模型）的設定：合併內建與使用者覆寫、驗證、解析成可呼叫的完整設定。

config.yaml 的 ``llm`` 段（新寫法）::

    llm:
      default: deepseek            # 預設執行者；"供應商" 或 "供應商/模型"
      providers:                   # 只寫要改的欄位；沒寫的用內建預設
        deepseek: {model: deepseek-flash, max_output: 32768}
        llamacpp: {base_url: "http://127.0.0.1:8080/v1", model: "qwen3-14b", context_tokens: 32768}
        my_server: {name: 我的伺服器, base_url: "http://192.168.1.5:8000/v1", model: foo}   # 自訂（OpenAI 相容）

舊寫法 ``llm: {provider, model, thinking, max_output}`` 仍可讀，會自動轉成新寫法。

供應商欄位格式沿用 novel-studio（``vendor/novel_studio/providers.py``）：金鑰另外存在系統 keyring，不在這裡。
本專案的任務都是「整理成 JSON」，所以只用 ``structured`` 這個思考階段。
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .errors import ConfigError

ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
DEFAULT_EXECUTOR = "deepseek"
STAGE = "structured"
RESERVED_IDS = {"claude_code"}
# 本專案對內建供應商的預設值（蓋在 novel-studio 的預設之上）。標註是結構化工作，溫度用低的。
OVERLAY: dict[str, dict[str, Any]] = {
    "deepseek": {
        "model": "deepseek-flash", "temperature": 0.3, "max_output": 32768,
        "models": [{"id": "deepseek-flash", "name": "DeepSeek Flash"}, {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"}],
    },
    "llamacpp": {"temperature": 0.3, "max_output": 16000},
}
STRING_FIELDS = ("name", "base_url", "model", "key_env")
INT_FIELDS = {"context_tokens": (1000, 10_000_000), "max_output": (16, 1_000_000)}
FLOAT_FIELDS = {"temperature": (0.0, 2.0), "cjk_token_ratio": (0.2, 3.0), "price_in": (0.0, 1e6), "price_out": (0.0, 1e6)}
ALLOWED_FIELDS = set(STRING_FIELDS) | set(INT_FIELDS) | set(FLOAT_FIELDS) | {"json_mode", "reasoning", "models", "extra"}


def _providers_module():
    from vendor.novel_studio import providers  # 延後 import：測試不需要網路模組
    return providers


def _builtin() -> list[dict[str, Any]]:
    """內建供應商（只留 OpenAI 相容的；Claude Code 不走這條路）已套用本專案預設值。"""
    out = []
    for p in _providers_module().DEFAULT_PROVIDERS:
        if p.get("type") != "openai":
            continue
        merged = copy.deepcopy(p)
        merged.update(copy.deepcopy(OVERLAY.get(p["id"], {})))
        merged["builtin"] = True
        out.append(merged)
    return out


def normalize_llm_section(raw: Any) -> dict[str, Any]:
    """舊寫法與新寫法都收，回傳新寫法 ``{"default": str, "providers": {id: {欄位覆寫}}}``。不驗證欄位內容。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("llm must be a mapping")
    providers = raw.get("providers") or {}
    if not isinstance(providers, dict):
        raise ConfigError("llm.providers must be a mapping")
    overrides: dict[str, dict[str, Any]] = {}
    for pid, fields in providers.items():
        if not isinstance(fields, dict):
            raise ConfigError(f"llm.providers.{pid} must be a mapping")
        overrides[str(pid)] = copy.deepcopy(fields)
    default = raw.get("default")
    if any(raw.get(k) not in (None, "") for k in ("provider", "model", "thinking", "max_output")):
        pid = str(raw.get("provider") or DEFAULT_EXECUTOR)
        entry = overrides.setdefault(pid, {})
        if raw.get("model") and "model" not in entry:
            entry["model"] = str(raw["model"])
        if raw.get("max_output") and "max_output" not in entry:
            try:
                entry["max_output"] = int(raw["max_output"])
            except (TypeError, ValueError):
                raise ConfigError("llm.max_output must be an integer") from None
        if raw.get("thinking"):
            reasoning = entry.setdefault("reasoning", {})
            reasoning.setdefault(STAGE, str(raw["thinking"]))
        default = default or pid
    return {"default": str(default or DEFAULT_EXECUTOR), "providers": overrides}


def _clean_field(pid: str, key: str, value: Any) -> Any:
    where = f"llm.providers.{pid}.{key}"
    if key in STRING_FIELDS:
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be a string")
        value = value.strip()
        if key == "base_url" and value and not re.match(r"^https?://", value):
            raise ConfigError(f"{where} must start with http:// or https://")
        return value
    if key in INT_FIELDS:
        if isinstance(value, bool):
            raise ConfigError(f"{where} must be an integer")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where} must be an integer") from None
        low, high = INT_FIELDS[key]
        if not low <= number <= high:
            raise ConfigError(f"{where} must be between {low} and {high}")
        return number
    if key in FLOAT_FIELDS:
        if isinstance(value, bool):
            raise ConfigError(f"{where} must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where} must be a number") from None
        low, high = FLOAT_FIELDS[key]
        if not low <= number <= high:
            raise ConfigError(f"{where} must be between {low} and {high}")
        return number
    if key == "json_mode":
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false")
        return value
    if key == "extra":
        if not isinstance(value, dict):
            raise ConfigError(f"{where} must be a mapping")
        return copy.deepcopy(value)
    if key == "reasoning":
        if not isinstance(value, dict):
            raise ConfigError(f"{where} must be a mapping")
        providers = _providers_module()
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == "adapter":
                if v not in providers.ADAPTERS:
                    raise ConfigError(f"{where}.adapter must be one of {sorted(providers.ADAPTERS)}")
            elif k == STAGE:
                if v not in providers.EFFORTS:
                    raise ConfigError(f"{where}.{k} must be one of {providers.EFFORTS}")
            else:
                raise ConfigError(f"{where}: unknown key {k!r} (only adapter and {STAGE} are used)")
            out[k] = v
        return out
    if key == "models":
        if not isinstance(value, list) or not all(isinstance(m, dict) for m in value):
            raise ConfigError(f"{where} must be a list of mappings")
        providers = _providers_module()
        return providers.clean_models(value, list(providers.STAGES), providers.EFFORTS)
    raise ConfigError(f"{where}: unknown field")


def build_providers(overrides: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    """內建供應商＋使用者覆寫＋自訂供應商，回傳完整設定清單（內建的在前，自訂的依寫入順序）。違規丟 ConfigError。"""
    overrides = overrides or {}
    providers = _builtin()
    by_id = {p["id"]: p for p in providers}
    for pid, fields in overrides.items():
        if pid in RESERVED_IDS:
            raise ConfigError(f"llm.providers.{pid}: this id is reserved")
        if not ID_RE.match(pid):
            raise ConfigError(f"llm.providers: id {pid!r} must match [a-z][a-z0-9_]*")
        for key in fields:
            if key not in ALLOWED_FIELDS:
                raise ConfigError(f"llm.providers.{pid}: unknown field {key!r}")
        if pid not in by_id:
            if not str(fields.get("base_url") or "").strip():
                raise ConfigError(f"llm.providers.{pid}: a custom provider needs base_url")
            by_id[pid] = {"id": pid, "type": "openai", "name": pid, "base_url": "", "model": "", "key_env": "",
                          "context_tokens": 32768, "max_output": 8192, "temperature": 0.3, "json_mode": True,
                          "price_in": 0.0, "price_out": 0.0, "extra": {},
                          "reasoning": {"adapter": "none", STAGE: "off"}, "builtin": False}
            providers.append(by_id[pid])
        target = by_id[pid]
        for key, value in fields.items():
            cleaned = _clean_field(pid, key, value)
            if key == "reasoning":
                target["reasoning"] = {**(target.get("reasoning") or {}), **cleaned}
            else:
                target[key] = cleaned
    return providers


def resolve_executor(providers: list[dict[str, Any]], executor_id: str | None) -> dict[str, Any]:
    """把「供應商」或「供應商/模型」展開成完整設定。找不到丟 ConfigError。"""
    pid = (executor_id or "").strip() or DEFAULT_EXECUTOR
    found = _providers_module().resolve(providers, pid)
    if found is None:
        raise ConfigError(f"unknown executor {pid!r}; known: {', '.join(p['id'] for p in providers)}")
    return found


def structured_effort(prov: dict[str, Any]) -> str:
    return str((prov.get("reasoning") or {}).get(STAGE) or "off")


def choices(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """給下拉選單用：``[{id, label, group, name, model, unset}]``。"""
    rows = []
    for item in _providers_module().choices(providers):
        rows.append({"id": item["id"], "label": item.get("label", ""), "group": item.get("group", ""),
                     "name": item.get("name", ""), "model": item.get("model", ""), "unset": bool(item.get("unset"))})
    return rows


def estimate_input_tokens(messages: list[dict[str, str]], prov: dict[str, Any]) -> int:
    """粗估送出內容的 token 數（字數 × 每字 token 比例；偏保守）。"""
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return int(chars * _providers_module().cjk_ratio(prov))


def check_fit(messages: list[dict[str, str]], prov: dict[str, Any]) -> None:
    """送出前先檢查：預估的輸入已經超過模型的上下文長度就直接報錯（本機小模型常見），不用等伺服器回一個看不懂的錯。"""
    from .llm import LlmError

    limit = int(prov.get("context_tokens") or 0)
    if not limit:
        return
    estimate = estimate_input_tokens(messages, prov)
    if estimate + 2048 > limit:
        raise LlmError(
            f"這次要送出的內容約 {estimate:,} tokens，超過 {prov.get('name') or prov['id']} 設定的上下文長度 {limit:,}"
            f"（要留空間給輸出）。請換上下文更長的模型，或到「設定」調高「上下文長度」。")


__all__ = ["DEFAULT_EXECUTOR", "STAGE", "build_providers", "check_fit", "choices", "estimate_input_tokens",
           "normalize_llm_section", "resolve_executor", "structured_effort"]
