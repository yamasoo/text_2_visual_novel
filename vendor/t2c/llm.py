"""LLM 連線（S4）：OpenAI 相容的 Chat Completions，DeepSeek、llama.cpp（llama-server）都適用。

只用標準函式庫，不依賴 FastAPI／fekit，可以整包搬走。providers 的格式和 Novel Studio 相同
（`config.json` 的 `providers`、`default_provider`、`stage_providers`）。

用法::

    cfg = settings.load()
    prov = llm.find_provider(cfg, stage="storyboard")
    data, reply = llm.chat_json(prov, [{"role": "user", "content": "…"}], should_stop=lambda: job.stopping)

停止：呼叫端傳 `should_stop`（介面任務用 `lambda: job.stopping`）。按停止後 0.2 秒內會丟出 `Cancelled`，
連線同時被切斷（本機模型也會停止生成）。呼叫端收到 `Cancelled` 後再呼叫 `job.checkpoint()` 結束任務。
"""
import json
import os
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

RETRY_STATUS = (408, 409, 425, 429, 500, 502, 503, 504)
POLL_SECONDS = 0.2


class LLMError(Exception):
    """連線或回應的問題。訊息是給人看的繁體中文。"""

    def __init__(self, message, status=None, retryable=False, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class JSONParseError(LLMError):
    """模型回傳的內容不是可解析的 JSON。"""

    def __init__(self, message, text=""):
        super().__init__(message)
        self.text = text


class Cancelled(Exception):
    """呼叫端要求停止。"""


@dataclass
class Reply:
    text: str = ""
    usage: dict = field(default_factory=dict)
    finish: str = ""
    thinking: str = ""
    attempts: int = 1


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
def find_provider(cfg, provider_id=None, stage=None):
    """依序：明確指定的 id → `stage_providers[stage]` → `default_provider` → 第一個。找不到丟 LLMError。"""
    provs = [p for p in (cfg.get("providers") or []) if isinstance(p, dict)]
    if not provs:
        raise LLMError("config.json 沒有設定 providers")
    wanted = provider_id or (cfg.get("stage_providers") or {}).get(stage or "") or cfg.get("default_provider") or provs[0].get("id")
    for p in provs:
        if p.get("id") == wanted:
            return p
    raise LLMError("找不到 provider「%s」（現有：%s）" % (wanted, "、".join(str(p.get("id")) for p in provs)))


KEYRING_SERVICE = "novel-studio"   # 與 Novel Studio、text_2_visual_novel 共用同一個系統金鑰庫項目（只讀不寫）


def keyring_available():
    """系統金鑰庫能不能用（沒裝 keyring 回 False）。"""
    try:
        import keyring
    except Exception:
        return False
    return hasattr(keyring, "get_password")


def keyring_get(account):
    """讀系統金鑰庫（Windows 認證管理員等）。沒裝 keyring 或沒有這筆就回傳空字串。"""
    try:
        import keyring
        return (keyring.get_password(KEYRING_SERVICE, account) or "").strip()
    except Exception:
        return ""


def keyring_set(account, secret):
    """把金鑰存進系統金鑰庫（只存金鑰庫，絕不寫任何檔案）。沒裝 keyring 丟 LLMError。"""
    secret = str(secret or "").strip()
    if not secret:
        raise LLMError("金鑰是空的")
    try:
        import keyring
    except Exception as error:
        raise LLMError("沒有安裝 keyring：請在 Python 環境執行 pip install keyring（%s）" % error)
    try:
        keyring.set_password(KEYRING_SERVICE, account, secret)
    except Exception as error:
        raise LLMError("存不進系統金鑰庫（服務 %s、帳號 %s）：%s" % (KEYRING_SERVICE, account, error))


def _keyring_get(account):
    """舊呼叫端沿用的名稱（api_key 用）。"""
    return keyring_get(account)


def api_key(prov):
    """金鑰來源：環境變數（`key_env`）→ 系統金鑰庫（服務 novel-studio、帳號 = provider id）。
    沒有 `key_env` 的 provider（本機 llama.cpp）不需要金鑰。金鑰絕不寫進檔案。"""
    env = prov.get("key_env")
    if not env:
        return ""
    return os.environ.get(env, "").strip() or _keyring_get(prov.get("key_id") or prov.get("id") or "")


def _is_local(url):
    host = urllib.parse.urlparse(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")


def _opener(url):
    if _is_local(url):   # 本機服務不走代理
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _endpoint(prov, path):
    base = (prov.get("base_url") or "").rstrip("/")
    if not base:
        raise LLMError("尚未設定 %s 的網址（base_url）" % (prov.get("name") or prov.get("id")))
    return base + path


def _headers(prov):
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json"}
    key = api_key(prov)
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def _check_key(prov):
    env = prov.get("key_env")
    if env and not api_key(prov) and not _is_local(prov.get("base_url") or ""):
        raise LLMError("沒有金鑰：請設定環境變數 %s，或把金鑰存進系統金鑰庫（需要 pip install keyring；服務 %s、帳號 %s）。金鑰不要寫進任何檔案"
                       % (env, KEYRING_SERVICE, prov.get("id")))


# ---------------------------------------------------------------------------
# 請求內容
# ---------------------------------------------------------------------------
def apply_reasoning(body, prov, stage):
    """`prov["reasoning"]` = {"adapter": "deepseek"|"openai_effort"|"qwen_template"|"none", "<stage>": "off|low|medium|high|max"}。
    沒有設定、或沒指定 stage 就不動請求。"""
    cfg = prov.get("reasoning") or {}
    adapter = cfg.get("adapter") or "none"
    if adapter == "none" or not stage:
        return
    effort = cfg.get(stage) or "off"
    if adapter == "deepseek":
        if effort == "off":
            body["thinking"] = {"type": "disabled"}
        else:
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = {"medium": "high"}.get(effort, effort)   # DeepSeek 只有 low／high／max
    elif adapter == "openai_effort":
        if effort != "off":
            body["reasoning_effort"] = {"max": "high"}.get(effort, effort)
    elif adapter == "qwen_template":
        body["chat_template_kwargs"] = {"enable_thinking": effort != "off"}


def build_body(prov, messages, json_mode=False, max_tokens=None, stage=None, temperature=None):
    body = {
        "model": prov.get("model") or "default",
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": int(max_tokens or prov.get("max_output") or 4096),
    }
    temp = temperature if temperature is not None else prov.get("temperature")
    if temp not in (None, ""):
        body["temperature"] = float(temp)
    if json_mode and prov.get("json_mode", True):
        body["response_format"] = {"type": "json_object"}
    apply_reasoning(body, prov, stage)
    extra = prov.get("extra")
    if isinstance(extra, dict):
        body.update(extra)   # 使用者的額外參數優先
    return body


# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------
_CJK = re.compile(r"[⺀-鿿豈-﫿＀-￯　-〿]")


def cjk_ratio(prov):
    """每個中日韓字約幾 token。沒設定時：DeepSeek 用官方估計 0.6，其他用保守的 1.2。"""
    try:
        ratio = float(prov.get("cjk_token_ratio") or 0)
    except (TypeError, ValueError):
        ratio = 0
    if ratio > 0:
        return ratio
    return 0.6 if "deepseek" in (prov.get("base_url") or "").lower() else 1.2


def estimate_tokens(text, prov=None):
    """粗估 token 數：中日韓字 × 比例＋其他字元 ÷ 4。只用來預先檢查長度與顯示進度，不是帳單。"""
    text = text or ""
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return int(round(cjk * cjk_ratio(prov or {}) + other / 4.0))


def estimate_messages(messages, prov=None):
    return sum(estimate_tokens(m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"), ensure_ascii=False), prov) + 4
               for m in messages)


def context_left(prov, messages, max_tokens=None):
    """上下文剩多少 token（扣掉提示詞與預留的輸出）。負數表示放不下。"""
    window = int(prov.get("context_tokens") or 0)
    if not window:
        return None
    out = int(max_tokens or prov.get("max_output") or 4096)
    return window - estimate_messages(messages, prov) - out


def check_context(prov, messages, max_tokens=None):
    """放不下就丟 LLMError（寫出差多少）。估算有誤差，所以只在明顯超過時擋。"""
    left = context_left(prov, messages, max_tokens)
    if left is not None and left < 0:
        raise LLMError("提示詞太長：估計超過上下文 %d token（上下文 %d、輸出預留 %d）。請縮短輸入或分段處理。"
                       % (-left, int(prov["context_tokens"]), int(max_tokens or prov.get("max_output") or 4096)))
    return left


# ---------------------------------------------------------------------------
# 連線（背景執行緒讀取，主執行緒輪詢停止）
# ---------------------------------------------------------------------------
def _http_error(err):
    body, message = "", ""
    try:
        body = err.read().decode("utf-8", "replace")
        data = json.loads(body)
        error = data.get("error") if isinstance(data, dict) else None
        message = (error.get("message") if isinstance(error, dict) else error) or ""
    except Exception:
        pass
    text = str(message or body or err)[:400]
    hint = {400: "請求格式錯誤", 401: "金鑰錯誤或未設定", 402: "帳戶餘額不足", 404: "網址或模型名稱錯誤",
            413: "提示詞太長", 429: "請求太頻繁或額度用完"}.get(err.code, "")
    retry_after = None
    try:
        retry_after = float(err.headers.get("Retry-After")) if err.headers else None
    except (TypeError, ValueError):
        pass
    return LLMError("HTTP %s %s：%s" % (err.code, hint, text), status=err.code,
                    retryable=err.code in RETRY_STATUS, retry_after=retry_after)


def _kill(holder):
    """切斷連線，讓卡在讀取的執行緒結束，也讓本機伺服器知道客戶端走了。"""
    resp = holder.get("resp")
    if resp is None:
        return
    try:
        raw = getattr(getattr(resp, "fp", None), "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    # resp.close() 要拿緩衝區的鎖；讀取執行緒正卡在 recv 時握著它（Windows 上 shutdown 不一定叫得醒），
    # 主執行緒在這裡等就會等到伺服器下一次寫資料。所以交給背景執行緒關，取消不能被它拖住。
    def _close():
        try:
            resp.close()
        except Exception:
            pass
    threading.Thread(target=_close, daemon=True).start()


def _reader(req, timeout, q, holder):
    try:
        resp = _opener(req.full_url).open(req, timeout=timeout)
        holder["resp"] = resp
        if holder.get("dead"):
            _kill(holder)
            return
        if "text/event-stream" not in resp.headers.get("Content-Type", ""):
            q.put(("body", resp.read()))
        else:
            for raw in resp:
                q.put(("line", raw))
        q.put(("end", None))
    except urllib.error.HTTPError as err:
        q.put(("error", _http_error(err)))
    except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as err:
        q.put(("error", LLMError("連不上 %s：%s" % (req.full_url, getattr(err, "reason", err)), retryable=True)))
    except Exception as err:   # 連線被 _kill 切斷、伺服器中途斷線等
        q.put(("error", LLMError("串流中斷：%s" % err, retryable=True)))


def _once(prov, body, on_delta, on_thinking, should_stop, timeout):
    req = urllib.request.Request(_endpoint(prov, "/chat/completions"),
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=_headers(prov), method="POST")
    q, holder = queue.Queue(), {}
    threading.Thread(target=_reader, args=(req, timeout, q, holder), daemon=True).start()
    parts, thinking, usage, finish = [], [], {}, ""
    saw_done = False
    try:
        while True:
            if should_stop and should_stop():
                raise Cancelled()
            try:
                kind, value = q.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            if kind == "error":
                err = value
                if parts and err.retryable:     # 已經吐出一部分字了，重試會重複，所以不重試
                    err.retryable = False
                    err.args = ("串流中斷（已收到 %d 字）：%s" % (len("".join(parts)), err.args[0]),)
                raise err
            if kind == "end":
                break
            if kind == "body":    # 不支援串流的伺服器：一次回傳
                try:
                    data = json.loads(value.decode("utf-8"))
                except ValueError:
                    raise LLMError("伺服器的回應不是 JSON")
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                if msg.get("reasoning_content") and on_thinking:
                    on_thinking(msg["reasoning_content"])
                    thinking.append(msg["reasoning_content"])
                content = msg.get("content") or ""
                if content and on_delta:
                    on_delta(content)
                return Reply(content, data.get("usage") or {}, choice.get("finish_reason") or "", "".join(thinking))
            line = value.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                saw_done = True
                break
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if event.get("error"):
                raise LLMError("伺服器回報錯誤：%s" % str(event["error"])[:400])
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    thinking.append(reasoning)
                    if on_thinking:
                        on_thinking(reasoning)
                content = delta.get("content")
                if content:
                    parts.append(content)
                    if on_delta:
                        on_delta(content)
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        if not saw_done and not finish:
            got = len("".join(parts))
            raise LLMError("串流中斷：伺服器在輸出完成前就斷線了" + ("（已收到 %d 字）" % got if got else ""), retryable=not parts)
        return Reply("".join(parts), usage, finish, "".join(thinking))
    finally:
        holder["dead"] = True
        _kill(holder)


def _sleep(seconds, should_stop):
    """可以被停止打斷的等待。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if should_stop and should_stop():
            raise Cancelled()
        time.sleep(min(POLL_SECONDS, max(0.0, end - time.monotonic())))


def chat(prov, messages, json_mode=False, max_tokens=None, stage=None, temperature=None,
         on_delta=None, on_thinking=None, should_stop=None, timeout=300, retries=3, backoff=2.0, on_retry=None):
    """串流呼叫 /chat/completions，回傳 `Reply`。

    on_delta(text)：正文片段；on_thinking(text)：推理過程片段。
    retries：失敗後最多再試幾次（429、5xx、連線失敗、尚未收到任何字就斷線）；等待 backoff×2^n 秒（最多 30），
    伺服器給 Retry-After 就照它。401、402、404 等不會重試。on_retry(次數, 錯誤, 等待秒數) 可用來顯示進度。
    timeout：單次讀取等多久沒有資料就算失敗（本機模型處理很長的提示詞時可以調大）。
    """
    _check_key(prov)
    body = build_body(prov, messages, json_mode, max_tokens, stage, temperature)
    attempt = 0
    while True:
        try:
            reply = _once(prov, body, on_delta, on_thinking, should_stop, timeout)
            reply.attempts = attempt + 1
            return reply
        except LLMError as err:
            if not err.retryable or attempt >= retries:
                raise
            wait = err.retry_after if err.retry_after is not None else min(30.0, backoff * (2 ** attempt))
            attempt += 1
            if on_retry:
                on_retry(attempt, err, wait)
            _sleep(wait, should_stop)


def list_models(prov, timeout=15):
    _check_key(prov)
    req = urllib.request.Request(_endpoint(prov, "/models"), headers=_headers(prov))
    try:
        with _opener(req.full_url).open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise _http_error(err)
    except (urllib.error.URLError, socket.timeout, ConnectionError, ValueError) as err:
        raise LLMError("連不上 %s：%s" % (req.full_url, getattr(err, "reason", err)))
    items = data.get("data") or data.get("models") or [] if isinstance(data, dict) else []
    return [m.get("id") or m.get("name") or m.get("model") for m in items if isinstance(m, dict) and (m.get("id") or m.get("name") or m.get("model"))]


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def parse_json(text):
    """容錯解析：去掉 ```json 圍欄與前後雜訊，物件或陣列都可以。失敗丟 JSONParseError。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        a, b = t.find(open_), t.rfind(close)
        if a >= 0 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except ValueError:
                continue
    if not t:
        raise JSONParseError("模型沒有回傳內容", text)
    raise JSONParseError("模型回傳的不是有效的 JSON（開頭：%s）" % t[:60].replace("\n", " "), text)


def chat_json(prov, messages, fix_rounds=1, **kw):
    """要求 JSON 並解析，回傳 `(資料, Reply)`。

    輸出被截斷（finish_reason=length）直接報錯，不重試。語法錯誤時把錯誤訊息回給模型修一次（fix_rounds 次）。
    內容欄位的驗證（分鏡規則等）不在這裡，由 S5 的提示詞流程處理。
    """
    msgs = list(messages)
    for round_ in range(fix_rounds + 1):
        reply = chat(prov, msgs, json_mode=True, **kw)
        if reply.finish == "length":
            raise LLMError("輸出被截斷（超過 max_tokens）。請調高 max_output 或把工作切小。")
        try:
            return parse_json(reply.text), reply
        except JSONParseError as err:
            if round_ >= fix_rounds:
                raise
            msgs += [{"role": "assistant", "content": reply.text},
                     {"role": "user", "content": "上一次的輸出無法解析成 JSON：%s\n請只輸出修正後的完整 JSON，不要加說明或圍欄。" % err}]
