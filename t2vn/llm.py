"""LLM 呼叫的薄包裝：真正的 HTTP 呼叫用 vendor/novel_studio/providers.py。

- ``make_llm(config)``：依 config.yaml 的 ``llm`` 設定建立呼叫函式（目前支援 DeepSeek 與 OpenAI 相容端點）。
- ``ReplayLLM``：依序回傳事先存好的回應，用於測試與離線重跑。

呼叫函式的介面：``llm(messages) -> LlmResult``。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import JobStopped, T2vnError

THINKING_VALUES = ("off", "low", "high", "max")
# novel-studio 的 reasoning 設定用「階段」名稱；這裡固定用一個階段名對應 thinking 設定。
_STAGE = "structured"


class LlmError(T2vnError):
    """LLM 呼叫失敗（連線、金鑰、回應格式）。"""


@dataclass(frozen=True)
class LlmResult:
    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""


LlmFn = Callable[..., LlmResult]  # llm(messages, on_delta=None, should_stop=None[, on_thinking=None]) -> LlmResult


@dataclass
class RunHooks:
    """長任務的回報與中斷：UI 用來顯示進度、停止任務。

    ``on_event(kind, info)``：kind 為 request（開始呼叫模型）、delta（串流收到文字，info["chars"]、info["text"]）、
    thinking（串流收到思考過程，info["text"]；只有支援的執行者才會有）、
    response（回來了，info["usage"]）、repair（回應有問題，要修正重跑，info["issues"]）。
    """

    should_stop: Callable[[], bool] = lambda: False
    on_event: Callable[[str, dict[str, Any]], None] = lambda kind, info: None


def call_llm(llm: LlmFn, messages: list[dict[str, str]], hooks: RunHooks | None, round_no: int) -> LlmResult:
    """呼叫模型；有 hooks 時串流回報字數、可隨時中斷。沒有 hooks 時和 ``llm(messages)`` 一樣。"""
    if hooks is None:
        return llm(messages)
    if hooks.should_stop():
        raise JobStopped("stopped")
    hooks.on_event("request", {"round": round_no})
    state = {"chars": 0}

    def on_delta(text: str) -> None:
        state["chars"] += len(text)
        hooks.on_event("delta", {"round": round_no, "chars": state["chars"], "text": text})

    def on_thinking(text: str) -> None:
        hooks.on_event("thinking", {"round": round_no, "text": text})

    extra = {"on_thinking": on_thinking} if getattr(llm, "supports_thinking", False) else {}
    result = llm(messages, on_delta=on_delta, should_stop=hooks.should_stop, **extra)
    hooks.on_event("response", {"round": round_no, "usage": dict(result.usage)})
    return result


def make_llm(provider: str, model: str, thinking: str = "off", max_output: int = 32768,
             base_url: str | None = None, secrets_home: str | Path | None = None, timeout: int = 900,
             temperature: float = 0.3) -> LlmFn:
    """建立呼叫函式。金鑰依序找：系統 keyring（service ``novel-studio``）→ secrets_home/secrets.json → 環境變數。"""
    from vendor.novel_studio import providers  # 延後 import：測試不需要網路模組

    if thinking not in THINKING_VALUES:
        raise LlmError(f"llm.thinking must be one of {THINKING_VALUES}, got {thinking!r}")
    base = next((dict(p) for p in providers.DEFAULT_PROVIDERS if p["id"] == provider), None)
    if base is None:
        raise LlmError(f"unknown llm provider {provider!r}; known: deepseek, llamacpp")
    base["model"] = model
    base["max_output"] = int(max_output)
    base["temperature"] = temperature  # 標註是結構化工作，用低溫；思考模式下 DeepSeek 會忽略
    if base_url:
        base["base_url"] = base_url
    reasoning = dict(base.get("reasoning") or {})
    reasoning[_STAGE] = thinking
    base["reasoning"] = reasoning
    home = str(Path(secrets_home).expanduser()) if secrets_home else str(Path.home() / ".t2vn")

    if base.get("key_env") and not providers.api_key(home, base):
        raise LlmError(f"no API key for {provider}.\n" + key_report(provider, home)
                       + f"\nfix: python -m t2vn.llm set-key {provider}")

    def call(messages: list[dict[str, str]], on_delta: Callable[[str], None] | None = None,
             should_stop: Callable[[], bool] | None = None) -> LlmResult:
        try:
            text, usage = providers.chat(home, base, messages, on_delta=on_delta, json_mode=True, stage=_STAGE,
                                         timeout=timeout, should_stop=should_stop)
        except providers.Cancelled as exc:
            raise JobStopped("stopped") from exc
        except providers.ProviderError as exc:
            raise LlmError(str(exc)) from exc
        return LlmResult(text=text, usage=usage, model=f"{provider}/{model}")

    return call


def make_llm_from(prov: dict[str, Any], secrets_home: str | Path | None = None, timeout: int = 900) -> LlmFn:
    """用一份完整的供應商設定（``executors.resolve_executor`` 的結果）建立呼叫函式。

    - 需要金鑰（設定了 ``key_env``）但找不到時，立刻丟 LlmError 並說明找過哪些地方。
    - 送出前會先估算內容長度；超過上下文長度就直接報錯（``executors.check_fit``）。
    - 思考強度用供應商設定的 ``reasoning.structured``。
    """
    from vendor.novel_studio import providers

    from . import executors

    home = str(Path(secrets_home).expanduser()) if secrets_home else str(Path.home() / ".t2vn")
    key_id = prov.get("key_id") or prov["id"]
    if prov.get("key_env") and not providers.api_key(home, prov):
        raise LlmError(f"no API key for {prov.get('name') or key_id}.\n" + key_report(key_id, home)
                       + f"\nfix: set it on the Settings page, or: python -m t2vn.llm set-key {key_id}")
    if not prov.get("model"):
        raise LlmError(f"{prov.get('name') or key_id} has no model name yet; set one on the Settings page"
                       "（本機服務可以按「測試連線」從清單選）")

    def call(messages: list[dict[str, str]], on_delta: Callable[[str], None] | None = None,
             should_stop: Callable[[], bool] | None = None, on_thinking: Callable[[str], None] | None = None) -> LlmResult:
        executors.check_fit(messages, prov)
        try:
            text, usage = providers.chat(home, prov, messages, on_delta=on_delta, on_thinking=on_thinking, json_mode=True,
                                         stage=executors.STAGE, timeout=timeout, should_stop=should_stop)
        except providers.Cancelled as exc:
            raise JobStopped("stopped") from exc
        except providers.ProviderError as exc:
            raise LlmError(str(exc)) from exc
        return LlmResult(text=text, usage=usage, model=f"{prov.get('base_id') or prov['id']}/{prov.get('model')}")

    call.supports_thinking = True  # type: ignore[attr-defined]
    return call


def make_executor(config: Any, executor: str | None = None, secrets_home: str | Path | None = None,
                  timeout: int = 900) -> LlmFn:
    """依 config 建立指定執行者（沒指定就用預設執行者）的呼叫函式。"""
    from . import executors
    from .errors import ConfigError

    try:
        providers = executors.build_providers(config.llm_overrides)
        prov = executors.resolve_executor(providers, executor or config.llm_default)
    except ConfigError as exc:
        raise LlmError(str(exc)) from exc
    return make_llm_from(prov, secrets_home, timeout)


def key_status(provider: str, secrets_home: str | Path | None = None) -> tuple[bool, str]:
    """(有沒有金鑰, 存在哪裡)。不回傳金鑰本身。不需要金鑰的 provider（本機 llama.cpp）一律回傳 True。"""
    import os

    from vendor.novel_studio import providers

    home = Path(secrets_home).expanduser() if secrets_home else Path.home() / ".t2vn"
    base = next((p for p in providers.DEFAULT_PROVIDERS if p["id"] == provider), None)
    if base is None:
        return False, "unknown provider"
    if not base.get("key_env"):
        return True, "not needed"
    if providers._keyring() and providers._kr_get(provider):
        return True, "system keyring"
    if providers.load_secrets(str(home)).get(provider):
        return True, "secrets file"
    if os.environ.get(base["key_env"]):
        return True, f"environment variable {base['key_env']}"
    return False, ""


def key_status_for(prov: dict[str, Any], secrets_home: str | Path | None = None) -> dict[str, Any]:
    """一個供應商的金鑰狀態：``{"needed": 要不要金鑰, "found": 找到沒有, "where": 存在哪裡（不含金鑰本身）}``。
    沒設 ``key_env`` 的供應商（例如本機 llama.cpp）視為不需要金鑰；但使用者還是可以替它存一把。"""
    from vendor.novel_studio import providers

    home = str(Path(secrets_home).expanduser()) if secrets_home else str(Path.home() / ".t2vn")
    where = providers.key_status(home, prov)
    return {"needed": bool(prov.get("key_env")), "found": bool(where), "where": where}


def key_report(provider: str, secrets_home: str | Path | None = None) -> str:
    """說明金鑰查找的每一個位置是否可用（不印出金鑰本身）。"""
    import os

    from vendor.novel_studio import providers

    home = Path(secrets_home).expanduser() if secrets_home else Path.home() / ".t2vn"
    base = next((p for p in providers.DEFAULT_PROVIDERS if p["id"] == provider), {})
    kr = providers._keyring()
    try:
        import keyring  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    lines = [
        f"- keyring package: {'installed' if installed else 'NOT installed (pip install keyring)'}",
        f"- system keyring backend: {'available' if kr else 'not available'}",
        f"- keyring entry (service {providers.KEYRING_SERVICE!r}, user {provider!r}): "
        f"{'found' if kr and providers._kr_get(provider) else 'not found'}",
        f"- {home / providers.SECRETS_NAME}: "
        f"{'has key' if providers.load_secrets(str(home)).get(provider) else 'no key'}",
    ]
    env = base.get("key_env")
    if env:
        lines.append(f"- environment variable {env}: {'set' if os.environ.get(env) else 'not set'}")
    return "\n".join(lines)


def set_key(provider: str, key: str, secrets_home: str | Path | None = None) -> str:
    """存金鑰：有系統 keyring 就存那裡（和 novel-studio 共用），沒有才存 secrets.json。回傳存放位置。"""
    from vendor.novel_studio import providers

    home = Path(secrets_home).expanduser() if secrets_home else Path.home() / ".t2vn"
    home.mkdir(parents=True, exist_ok=True)
    if not key.strip():
        raise LlmError("empty key")
    return providers.save_secret(str(home), provider, key.strip()) or "?"


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.llm check-key deepseek`` / ``python -m t2vn.llm set-key deepseek``"""
    import argparse
    import getpass
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Check or store an LLM API key.")
    parser.add_argument("action", choices=["check-key", "set-key"])
    parser.add_argument("provider", nargs="?", default="deepseek")
    args = parser.parse_args(argv)
    if args.action == "check-key":
        print(key_report(args.provider))
        return 0
    key = getpass.getpass(f"{args.provider} API key (input hidden): ")
    try:
        where = set_key(args.provider, key)
    except LlmError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"saved to: {where}")
    return 0


class ReplayLLM:
    """依序回傳事先準備的回應（字串或 dict）。回應用完再呼叫會丟 LlmError。"""

    def __init__(self, responses: list[str | dict[str, Any]], model: str = "replay"):
        self._responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []
        self.model = model

    @classmethod
    def from_files(cls, paths: list[str | Path]) -> "ReplayLLM":
        return cls([Path(p).read_text(encoding="utf-8") for p in paths], model="replay")

    def __call__(self, messages: list[dict[str, str]], on_delta: Callable[[str], None] | None = None,
                 should_stop: Callable[[], bool] | None = None) -> LlmResult:
        self.calls.append(messages)
        if not self._responses:
            raise LlmError("replay: no more responses")
        item = self._responses.pop(0)
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        if on_delta:
            on_delta(text)
        return LlmResult(text=text, usage={}, model=self.model)


if __name__ == "__main__":
    raise SystemExit(main())
