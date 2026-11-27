"""VRAM 排程（S7）：本機 llama.cpp 與 ComfyUI 不要同時佔 GPU。見 docs/05_介面設計.md 第 4 節。

- 呼叫本機 LLM 前：請 ComfyUI 卸下模型（POST /free）。
- 出圖前：看本機 llama-server 是否還在執行（GET /health）；還在就由呼叫端決定要不要等使用者關掉它。

只用標準函式庫；連不上一律當成「沒在執行」，不丟錯（排程是輔助，不能擋住正常流程）。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from . import llm


def _open(url, data=None, timeout=3.0):
    body = None if data is None else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"} if body else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 本機服務不走代理
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def is_local(prov):
    return llm._is_local(str((prov or {}).get("base_url") or ""))


def local_providers(cfg):
    """設定裡網址是本機的 provider（llama.cpp 之類）。"""
    return [p for p in (cfg.get("providers") or []) if isinstance(p, dict) and is_local(p)]


def stage_provider(cfg, stage):
    """某個階段（adapt／script／storyboard／bible）實際會用的 provider；設定錯了回傳 None。"""
    try:
        return llm.find_provider(cfg, stage=stage)
    except llm.LLMError:
        return None


def server_root(prov):
    """OpenAI 相容網址（…/v1）→ 伺服器根網址（llama-server 的 /health 在根目錄）。"""
    base = str(prov.get("base_url") or "").rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def llm_running(prov, timeout=2.0):
    """本機 LLM 伺服器有沒有在執行。有回應（含 503 載入中）就算在執行。"""
    if not is_local(prov):
        return False
    try:
        _open(server_root(prov) + "/health", timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def running_local_llms(cfg, timeout=2.0):
    return [p for p in local_providers(cfg) if llm_running(p, timeout)]


def comfy_stats(url, timeout=3.0):
    """ComfyUI 的 /system_stats（含 VRAM）；連不上回傳 None。"""
    try:
        _, raw = _open(str(url).rstrip("/") + "/system_stats", timeout=timeout)
        return json.loads(raw.decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def comfy_free(url, timeout=5.0):
    """請 ComfyUI 卸下模型、釋放 VRAM。成功回傳 True；ComfyUI 沒開或不支援回傳 False。"""
    try:
        _open(str(url).rstrip("/") + "/free", {"unload_models": True, "free_memory": True}, timeout=timeout)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def vram_summary(stats):
    """/system_stats → 「GPU 名稱：已用 x／共 y GiB」；沒有資料回傳空字串。"""
    devices = (stats or {}).get("devices") or []
    if not devices:
        return ""
    d = devices[0]
    total, free = d.get("vram_total") or 0, d.get("vram_free") or 0
    if not total:
        return str(d.get("name") or "")
    gib = 1024 ** 3
    return "%s：已用 %.1f／共 %.1f GiB" % (d.get("name") or "GPU", (total - free) / gib, total / gib)
