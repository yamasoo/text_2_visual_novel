"""最小的 ComfyUI API 客戶端（只用標準函式庫）。

workflow 用 API 格式（ComfyUI 選單 Export (API)）。內建兩組（t2c/data/）：
moss_layout.api.json（MOSS-Image）與 anima.api.json（Anima）。用哪一組、模型檔名、取樣預設、LoRA
由「出圖 profile」決定——config.json 的 comfy.profiles（見 resolve_profile），project.yaml 的
comfy_profile 指定專案用哪一組，呼叫端也可以臨時換（tools/panels.py --profile、介面）。

沒有 comfy.profiles 的舊設定照舊可用：頂層的 comfy.workflow／comfy.models／comfy.presets 就等於
一組叫 moss 的 profile（workflow_path 保留給舊呼叫端）。

要改的節點從 KSampler 往回追：positive／negative 的 CLIPTextEncode、latent 的寬高、seed；
模型檔名照 profile 的 models 覆蓋。步數、CFG、取樣器照 workflow；傳入 preset 就覆蓋，
並可在模型後面接 LoRA（見 apply_preset）。
"""
import copy
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
DEFAULT_WORKFLOW = DATA / "moss_layout.api.json"
# 舊的 config.json 寫的是工具資料夾裡的相對路徑；那個檔案已搬進套件，照舊寫法也指到內建的
LEGACY_WORKFLOWS = {"workflows/moss_layout.api.json", "./workflows/moss_layout.api.json"}
KINDS = {"moss": "MOSS-Image", "anima": "Anima"}
KIND_WORKFLOWS = {"moss": DEFAULT_WORKFLOW, "anima": DATA / "anima.api.json"}
LEGACY_PROFILE = "moss"                 # 沒有 comfy.profiles 時，頂層設定等於這一組
LOADERS = {"UNETLoader": "unet_name", "CLIPLoader": "clip_name", "VAELoader": "vae_name"}
MODEL_KEYS = {"UNETLoader": "unet", "CLIPLoader": "clip", "VAELoader": "vae"}

# 依檔名猜模型用的關鍵字（照 Novel Studio 的 studio/comfy.py:30-37；只在使用者還沒選時用）
GUESS = {
    "moss": {"unet": ("moss_image", "moss"), "clip": ("qwen3.5_9b", "qwen3.5"), "vae": ("flux2-vae", "flux2")},
    "anima": {"unet": ("anima",), "clip": ("qwen_3_06b", "qwen3_06b", "qwen_3_0.6b"), "vae": ("qwen_image_vae",)},
}

PRESET_KEYS = {"steps": "steps", "cfg": "cfg", "sampler": "sampler_name", "scheduler": "scheduler"}
PRESET_OTHER = {"lora", "lora_strength", "note"}
LORA_NODE_ID = "t2c_lora"


class ComfyError(Exception):
    """給人看的錯誤訊息。"""


# ---------------------------------------------------------------------------
# 出圖 profile（kind、workflow、模型、預設、負向、LoRA）
# ---------------------------------------------------------------------------
def _comfy(cfg):
    block = (cfg or {}).get("comfy")
    if block is not None and not isinstance(block, dict):
        raise ComfyError("config.json 的 comfy 要是物件")
    return block or {}


def profile_table(cfg):
    """config.json 的 comfy.profiles（略過 _ 開頭的說明鍵）。回傳 {名稱: 原始內容}。"""
    raw = _comfy(cfg).get("profiles")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ComfyError("config.json 的 comfy.profiles 要是物件（{名稱: {kind、models…}}），目前是 %s" % type(raw).__name__)
    table = {}
    for name, entry in raw.items():
        if str(name).startswith("_"):
            continue
        if not isinstance(entry, dict):
            raise ComfyError("config.json 的 comfy.profiles.%s 要是物件（kind、workflow、models…），目前是 %s" % (name, type(entry).__name__))
        table[str(name)] = entry
    return table


def default_profile_name(cfg):
    """comfy.default_profile；沒寫就用唯一的一組；有好幾組又沒指定就丟錯。"""
    table = profile_table(cfg)
    name = str(_comfy(cfg).get("default_profile") or "").strip()
    if not table:
        return name or LEGACY_PROFILE
    if name:
        return name
    if len(table) == 1:
        return next(iter(table))
    raise ComfyError("沒有指定出圖 profile：config.json 的 comfy.default_profile 要選一組（目前有：%s），"
                     "或在 project.yaml 寫 comfy_profile" % "、".join(table))


def _number(value, label):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ComfyError("%s 要是數字：%r" % (label, value))


def _models(value, label):
    """{unet, clip, vae} → 去掉空白的 dict。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ComfyError("%s 要是物件（unet／clip／vae）：目前是 %s" % (label, type(value).__name__))
    out = {}
    for key, name in value.items():
        if str(key).startswith("_"):
            continue
        name = str(name or "").strip()
        if name:
            out[str(key)] = name
    return out


def _presets(value, label):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ComfyError("%s 要是物件（{預設名稱: {steps、cfg、sampler…}}）：目前是 %s" % (label, type(value).__name__))
    out = {}
    for name, preset in value.items():
        if str(name).startswith("_"):
            continue
        if preset is None:
            continue
        if not isinstance(preset, dict):
            raise ComfyError("%s.%s 要是物件（steps、cfg、sampler、scheduler、lora…）：目前是 %s" % (label, name, type(preset).__name__))
        out[str(name)] = preset
    return out


def _loras(value, label):
    """[{name, strength, on}] → 只留勾選且有檔名的，依序回傳。"""
    if not value:
        return []
    if not isinstance(value, list):
        raise ComfyError("%s 要是一串物件（[{name, strength, on}]）：目前是 %s" % (label, type(value).__name__))
    out = []
    for i, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise ComfyError("%s 第 %d 個要是物件（name、strength、on）：目前是 %s" % (label, i, type(item).__name__))
        name = str(item.get("name") or "").strip()
        if not name:
            raise ComfyError("%s 第 %d 個沒有 name（ComfyUI 列出的 LoRA 檔名，含子資料夾）" % (label, i))
        on = item.get("on", True)
        if not isinstance(on, bool):
            raise ComfyError("%s 第 %d 個的 on 要是 true／false：%r" % (label, i, on))
        strength = _number(item.get("strength", 1.0), "%s 第 %d 個的 strength" % (label, i))
        if on:
            out.append({"name": name, "strength": strength})
    return out


def _workflow_file(cfg, spec, kind, name):
    """profile 的 workflow 檔：空字串／舊寫法 → 那種 kind 的內建檔；相對路徑以 config.json 所在資料夾為準。"""
    spec = str(spec or "").strip()
    if not spec or spec.replace("\\", "/") in LEGACY_WORKFLOWS:
        return KIND_WORKFLOWS[kind]
    path = Path(spec).expanduser()
    if not path.is_absolute():
        base = Path(cfg["_path"]).resolve().parent if cfg.get("_path") else Path.cwd()
        path = base / path
    if not path.is_file():
        raise ComfyError("找不到 profile「%s」的 workflow：%s（config.json 的 comfy.profiles.%s.workflow；"
                         "留空＝內建的 %s）" % (name, path, name, kind))
    return path


def resolve_profile(cfg, name=None, project_config=None):
    """這次出圖要用的一組設定。選擇順序：name → project_config 的 comfy_profile → comfy.default_profile。

    回傳 {"name", "kind", "workflow"(Path), "models", "presets", "default_preset", "negative", "loras", "base_url"}。
    命名過的 profile 是自足的：模型、預設、LoRA 都寫在那一組裡（頂層的 comfy.models／comfy.presets 只給舊設定用），
    所以不會拿 MOSS 的模型或預設去跑 Anima。
    """
    table = profile_table(cfg)
    picked = str(name or "").strip() or str(((project_config or {}).get("comfy_profile") or "")).strip()
    picked = picked or default_profile_name(cfg)
    comfy_cfg = _comfy(cfg)
    base_url = str(comfy_cfg.get("url") or "http://127.0.0.1:8188")
    if not table:
        if picked != LEGACY_PROFILE:
            raise ComfyError("找不到出圖 profile「%s」：config.json 沒有 comfy.profiles（舊設定只有一組 moss；"
                             "要加 profile 請寫在 config.json 的 comfy.profiles）" % picked)
        presets = _presets(comfy_cfg.get("presets"), "config.json 的 comfy.presets")
        return {"name": picked, "kind": "moss",
                "workflow": _workflow_file(cfg, comfy_cfg.get("workflow"), "moss", picked),
                "models": _models(comfy_cfg.get("models"), "config.json 的 comfy.models"),
                "presets": presets, "default_preset": str(comfy_cfg.get("default_preset") or "") or None,
                "negative": "", "loras": [], "base_url": base_url}
    if picked not in table:
        raise ComfyError("找不到出圖 profile「%s」（config.json 的 comfy.profiles 有：%s；"
                         "用 comfy.default_profile 或 project.yaml 的 comfy_profile 指定）" % (picked, "、".join(table) or "無"))
    entry = table[picked]
    kind = str(entry.get("kind") or "").strip()
    if not kind:
        raise ComfyError("profile「%s」缺少 kind（可用：%s）" % (picked, "、".join(KINDS)))
    if kind not in KINDS:
        raise ComfyError("profile「%s」的 kind「%s」不認得（可用：%s）" % (picked, kind, "、".join(KINDS)))
    negative = entry.get("negative", "")
    if negative is not None and not isinstance(negative, str):
        raise ComfyError("config.json 的 comfy.profiles.%s.negative 要是字串" % picked)
    return {"name": picked, "kind": kind,
            "workflow": _workflow_file(cfg, entry.get("workflow"), kind, picked),
            "models": _models(entry.get("models"), "config.json 的 comfy.profiles.%s.models" % picked),
            "presets": _presets(entry.get("presets"), "config.json 的 comfy.profiles.%s.presets" % picked),
            "default_preset": str(entry.get("default_preset") or "") or None,
            "negative": negative or "",
            "loras": _loras(entry.get("loras"), "config.json 的 comfy.profiles.%s.loras" % picked),
            "base_url": base_url}


def resolve_preset(profile, name=None):
    """(選用的預設名稱, 內容, 預設名稱)。profile 是 resolve_profile 的結果。"""
    presets = profile.get("presets") or {}
    default = profile.get("default_preset") or None
    chosen = str(name or "").strip() or default
    if chosen and chosen not in presets:
        raise ComfyError("找不到出圖預設「%s」（profile「%s」的 presets 有：%s）" % (chosen, profile.get("name"), "、".join(presets) or "無"))
    return chosen, (presets.get(chosen) if chosen else None), default


# ---------------------------------------------------------------------------
# workflow 檔
# ---------------------------------------------------------------------------
def load_workflow(path=None):
    path = Path(path or DEFAULT_WORKFLOW)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ComfyError("找不到 workflow：%s（config.json 的 comfy.workflow／comfy.profiles.<名稱>.workflow；留空就用內建的）" % path)
    except json.JSONDecodeError as error:
        raise ComfyError("%s 不是合法的 JSON：第 %d 行第 %d 欄：%s" % (path, error.lineno, error.colno, error.msg))
    except OSError as error:
        raise ComfyError("讀不到 workflow %s：%s" % (path, error))


def workflow_path(cfg):
    """預設 profile 的 workflow 檔（舊呼叫端與測試用）。"""
    if profile_table(cfg):
        return resolve_profile(cfg)["workflow"]
    return _workflow_file(cfg, _comfy(cfg).get("workflow"), "moss", LEGACY_PROFILE)


def config_workflow(cfg):
    """依工具設定的預設 profile 載入 workflow。"""
    return load_workflow(workflow_path(cfg))


# ---------------------------------------------------------------------------
# ComfyUI 現在掛了哪些模型（object_info）
# ---------------------------------------------------------------------------
def object_info(base, cls=None, timeout=30):
    """GET /object_info（或 /object_info/<節點類別>）：ComfyUI 掛了哪些模型、有哪些取樣器。"""
    path = "/object_info" + ("/" + urllib.parse.quote(str(cls)) if cls else "")
    return _req(base, path, timeout=timeout)


def _choices(info, cls, field):
    """某一類節點某個欄位可選的值（Combo）；節點不存在或格式不同就回空清單。"""
    try:
        spec = info[cls]["input"]["required"][field]
    except (KeyError, TypeError):
        return []
    if isinstance(spec, list) and spec:
        if isinstance(spec[0], list):
            return list(spec[0])
        if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            return list(spec[1].get("options") or [])
    return []


def model_lists(info):
    """object_info → 設定頁下拉要的清單（unet／clip／vae／lora／sampler／scheduler）。"""
    return {"unet": _choices(info, "UNETLoader", "unet_name"),
            "clip": _choices(info, "CLIPLoader", "clip_name"),
            "vae": _choices(info, "VAELoader", "vae_name"),
            "lora": _choices(info, "LoraLoaderModelOnly", "lora_name"),
            "sampler": _choices(info, "KSampler", "sampler_name"),
            "scheduler": _choices(info, "KSampler", "scheduler")}


def guess_models(kind, lists):
    """依檔名猜這個 kind 該用哪幾個模型（猜不到回空字串）。"""
    out = {}
    for key, words in GUESS.get(str(kind or ""), {}).items():
        out[key] = next((name for word in words for name in lists.get(key) or [] if word in str(name).lower()), "")
    return out


def available_models(base, timeout=30):
    """ComfyUI 現在有哪些模型；連不上丟 ComfyError（設定頁顯示那句話）。"""
    return model_lists(object_info(base, timeout=timeout))


# ---------------------------------------------------------------------------
# 修改 workflow
# ---------------------------------------------------------------------------
def apply_preset(wf, preset, loras=None):
    """把出圖預設套到 workflow（就地修改）：steps、cfg、sampler、scheduler，以及可選的 lora。

    preset 例：{"steps": 4, "cfg": 1.0, "sampler": "euler", "scheduler": "simple",
                "lora": "ANIMA\\moss_image_sft_4step_v2.0.safetensors", "lora_strength": 1.0}
    loras 是 profile 的 LoRA 清單（[{name, strength}]），接在預設的 lora 後面。
    lora 用 ComfyUI 列出的名稱（含子資料夾）；套用方式是在 KSampler 的 model 前面接 LoraLoaderModelOnly。
    """
    preset = preset or {}
    chain = []
    if preset:
        unknown = set(preset) - set(PRESET_KEYS) - PRESET_OTHER
        if unknown:
            raise ComfyError("出圖預設有不認得的欄位：%s（可用：%s）" % ("、".join(sorted(unknown)), "、".join(sorted(set(PRESET_KEYS) | PRESET_OTHER))))
        if preset.get("lora"):
            try:
                chain.append((str(preset["lora"]), float(preset.get("lora_strength", 1.0))))
            except (TypeError, ValueError):
                raise ComfyError("出圖預設的 lora_strength 要是數字：%r" % (preset.get("lora_strength"),))
    chain += [(item["name"], float(item.get("strength", 1.0))) for item in (loras or [])]
    if not preset and not chain:
        return wf
    ins = wf[_sampler(wf)]["inputs"]
    try:
        for key, target in PRESET_KEYS.items():
            if key in preset:
                ins[target] = int(preset[key]) if key == "steps" else float(preset[key]) if key == "cfg" else str(preset[key])
    except (TypeError, ValueError):
        raise ComfyError("出圖預設的 steps 要是整數、cfg 要是數字：%r" % {k: preset[k] for k in ("steps", "cfg") if k in preset})
    model = ins["model"]
    for i, (name, strength) in enumerate(chain):
        node_id = LORA_NODE_ID if i == 0 else "%s_%d" % (LORA_NODE_ID, i + 1)
        wf[node_id] = {"class_type": "LoraLoaderModelOnly", "_meta": {"title": "LoRA（%s）" % name},
                       "inputs": {"model": model, "lora_name": name, "strength_model": strength}}
        model = [node_id, 0]
    if chain:
        ins["model"] = model
    return wf


def render_name(panel_id, seed, preset_name=None, default_preset=None, profile_name=None, default_profile=None):
    """出圖檔名：<格 id>_s<seed>[_<預設>][__<profile>].png。

    不是預設的出圖預設加 _<預設名>；不是預設的 profile 再加 __<profile>，兩組 profile 的同一張不會互相蓋掉。
    """
    name = "%s_s%d" % (panel_id, int(seed))
    if preset_name and preset_name != default_preset:
        name += "_%s" % preset_name
    if profile_name and profile_name != default_profile:
        name += "__%s" % profile_name
    return name + ".png"


def _req(base, path, data=None, timeout=30, raw=False):
    url = base.rstrip("/") + path
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:800]
        raise ComfyError("ComfyUI 回應 %d：%s" % (error.code, detail))
    except (urllib.error.URLError, OSError) as error:
        raise ComfyError("連不到 ComfyUI（%s）：%s。ComfyUI 有開嗎？" % (base, error))
    return payload if raw else json.loads(payload.decode("utf-8") or "{}")


def _sampler(wf):
    found = [nid for nid, node in wf.items() if node.get("class_type") == "KSampler"]
    if len(found) != 1:
        raise ComfyError("workflow 要剛好有一個 KSampler（目前 %d 個）" % len(found))
    return found[0]


def prepare(wf, positive, negative, seed, width, height, models=None, prefix="text2comics/panel", preset=None,
            loras=None, profile_name=None):
    """回傳 (修改後的 workflow, 輸出節點 id)。不改動傳入的 wf。preset／loras 見 apply_preset。

    profile_name 只用來把錯誤訊息寫清楚（哪一組 profile 少了模型檔名）。
    """
    wf = copy.deepcopy(wf)
    apply_preset(wf, preset, loras)
    sid = _sampler(wf)
    ins = wf[sid]["inputs"]
    ins["seed"] = int(seed)
    for key, text in (("positive", positive), ("negative", negative)):
        nid = ins[key][0]
        node = wf[nid]
        if node["class_type"] == "CLIPTextEncode":
            node["inputs"]["text"] = text
        elif node["class_type"] != "ConditioningZeroOut":   # CFG 1 時負向可以換成 ConditioningZeroOut
            raise ComfyError("KSampler 的 %s 要接 CLIPTextEncode（目前是 %s）" % (key, node["class_type"]))
    latent = wf[ins["latent_image"][0]]["inputs"]
    latent["width"], latent["height"] = int(width), int(height)
    if "batch_size" in latent:
        latent["batch_size"] = 1
    where = ("config.json 的 comfy.profiles.%s.models" % profile_name) if profile_name else "config.json 的 comfy.models"
    for node in wf.values():
        cls = node.get("class_type")
        name = (models or {}).get(MODEL_KEYS.get(cls, ""), "")
        if cls in LOADERS and name:
            node["inputs"][LOADERS[cls]] = name
        if cls in LOADERS and not node["inputs"].get(LOADERS[cls]):
            raise ComfyError("%s 沒有設定模型檔名（%s.%s）" % (cls, where, MODEL_KEYS[cls]))
    outputs = [nid for nid, node in wf.items() if node.get("class_type") in ("SaveImage", "PreviewImage")]
    if not outputs:
        raise ComfyError("workflow 裡沒有 SaveImage／PreviewImage")
    wf[outputs[0]]["inputs"]["filename_prefix"] = prefix
    if wf[outputs[0]]["class_type"] == "PreviewImage":
        wf[outputs[0]]["inputs"].pop("filename_prefix")
    return wf, outputs[0]


def run(base, wf, out_id, should_stop=lambda: False, timeout=1800):
    """送出並等待，回傳第一張圖的 bytes。"""
    result = _req(base, "/prompt", {"prompt": wf, "client_id": uuid.uuid4().hex})
    if result.get("node_errors"):
        raise ComfyError("workflow 有錯：%s" % json.dumps(result["node_errors"], ensure_ascii=False)[:800])
    pid = result.get("prompt_id")
    if not pid:
        raise ComfyError("ComfyUI 沒有回傳任務編號")
    start = time.time()
    while True:
        if should_stop():
            _req(base, "/interrupt", {"prompt_id": pid})
            raise ComfyError("已停止")
        entry = _req(base, "/history/" + urllib.parse.quote(pid), timeout=10).get(pid)
        if entry:
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                messages = [m[1] for m in status.get("messages") or [] if m and m[0] == "execution_error"]
                detail = messages[0].get("exception_message", "") if messages else ""
                raise ComfyError("ComfyUI 執行失敗：%s" % detail)
            images = ((entry.get("outputs") or {}).get(out_id) or {}).get("images") or []
            if images:
                image = images[0]
                query = urllib.parse.urlencode({"filename": image["filename"], "subfolder": image.get("subfolder", ""),
                                                "type": image.get("type", "output")})
                return _req(base, "/view?" + query, timeout=60, raw=True)
            if status.get("completed"):
                raise ComfyError("ComfyUI 完成了，但沒有輸出圖片")
        if time.time() - start > timeout:
            raise ComfyError("等太久了（超過 %d 分鐘）" % (timeout // 60))
        time.sleep(1.0)
