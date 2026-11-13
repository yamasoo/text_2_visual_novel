"""工具本身的設定：config.json（不進版控）；沒有就用 config.example.json。

設定檔在哪裡由呼叫端決定（M1 修正 M-3：t2c 要能整包搬進別的專案，不能假設上一層資料夾是什麼）：
獨立工具的 app.py、tools/*.py 傳 root=工具資料夾；併進別的專案時傳它自己的設定檔路徑。
"""
import json
from pathlib import Path

from . import comfy


def load(path=None, root=None):
    """path：指定的設定檔；沒給就在 root 找 config.json，再找 config.example.json。回傳的 dict 帶 _path（相對路徑以它為準）。"""
    if not path and not root:
        raise ValueError("settings.load() 要給設定檔路徑 path，或設定檔所在的資料夾 root")
    for candidate in ([Path(path)] if path else [Path(root) / "config.json", Path(root) / "config.example.json"]):
        if candidate.is_file():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            data["_path"] = str(candidate)
            return data
    raise FileNotFoundError("找不到 config.json，請複製 config.example.json 再修改")

# 介面可以改的鍵（白名單）。不在這裡的鍵一律拒絕，避免介面把 _說明 之類的東西寫回去。
WRITABLE = ("providers", "default_provider", "stage_providers", "comfy", "lettering")

# stage_providers 可以指定的階段（設定頁的下拉由這份清單產生，不要另外寫死）。
# adapt＝改編規劃（plan 階段的 stage 鍵）、annotate＝切對話、bible＝視覺聖經。
STAGES = ("adapt", "script", "storyboard", "bible", "annotate", "plan")


def save(data, path=None, root=None):
    """把設定寫回 config.json（只寫 WRITABLE 的鍵）。舊檔先備份成 config.json.bak，再用暫存檔換名。

    data 沒帶的 WRITABLE 鍵保持原樣；_ 開頭的鍵（例如 _說明、_path）不會寫進檔案。
    """
    if not isinstance(data, dict):
        raise ValueError("設定必須是物件")
    target = _target(path, root)
    current = {}
    if target.is_file():
        current = json.loads(target.read_text(encoding="utf-8"))
    elif path is None and root is not None:
        example = Path(root) / "config.example.json"
        if example.is_file():
            current = json.loads(example.read_text(encoding="utf-8"))

    merged = {k: v for k, v in current.items() if not str(k).startswith("_")}
    extra = sorted(k for k in data if k not in WRITABLE)
    if extra:
        raise ValueError("不能寫入這些設定：%s（可寫的是 %s）" % ("、".join(extra), "、".join(WRITABLE)))
    for key, value in data.items():
        old = merged.get(key)
        # 物件型的段落做一層合併：呼叫端只送部分欄位時不會把其他欄位清掉。
        merged[key] = {**old, **value} if isinstance(old, dict) and isinstance(value, dict) else value
    _validate(merged)

    if target.is_file():
        backup = target.with_name(target.name + ".bak")
        backup.write_bytes(target.read_bytes())
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    merged["_path"] = str(target)
    return merged


def _target(path, root):
    if path:
        return Path(path)
    if not root:
        raise ValueError("settings.save() 要給設定檔路徑 path，或設定檔所在的資料夾 root")
    return Path(root) / "config.json"


def _validate(cfg):
    """只擋會讓後面的程式壞掉的情況，其餘交給使用者。"""
    providers = cfg.get("providers")
    if providers is not None:
        if not isinstance(providers, list) or not all(isinstance(p, dict) for p in providers):
            raise ValueError("providers 必須是一串物件")
        ids = []
        for item in providers:
            pid = str(item.get("id") or "").strip()
            if not pid:
                raise ValueError("每個 provider 都要有 id")
            if pid in ids:
                raise ValueError("provider id 重複：%s" % pid)
            ids.append(pid)
            if not str(item.get("base_url") or "").strip() and str(item.get("type") or "") != "claude_code":
                raise ValueError("provider %s 缺少 base_url" % pid)
        default = str(cfg.get("default_provider") or "").strip()
        if default and ids and default not in ids:
            raise ValueError("default_provider「%s」不在 providers 裡（可選：%s）" % (default, "、".join(ids)))
    for block in ("stage_providers", "comfy", "lettering"):
        if block in cfg and not isinstance(cfg[block], dict):
            raise ValueError("%s 必須是物件" % block)
    _validate_comfy(cfg.get("comfy") or {})


def _validate_comfy(comfy_cfg):
    """出圖 profile（N4）：只擋會讓出圖壞掉的情況，模型檔名有沒有下載交給 ComfyUI 回報。"""
    profiles = comfy_cfg.get("profiles")
    if profiles is None:
        return
    if not isinstance(profiles, dict):
        raise ValueError("comfy.profiles 要是物件（{名稱: {kind、workflow、models…}}）")
    names = [str(k) for k in profiles if not str(k).startswith("_")]
    for name in names:
        entry = profiles[name]
        if not isinstance(entry, dict):
            raise ValueError("comfy.profiles.%s 要是物件（kind、workflow、models…）" % name)
        kind = str(entry.get("kind") or "").strip()
        if kind not in comfy.KINDS:
            raise ValueError("comfy.profiles.%s 的 kind 要是 %s（目前：%r）" % (name, " 或 ".join(comfy.KINDS), entry.get("kind")))
        for block in ("models", "presets"):
            if block in entry and not isinstance(entry[block], dict):
                raise ValueError("comfy.profiles.%s.%s 要是物件" % (name, block))
        for key in ("workflow", "negative", "default_preset"):
            if key in entry and not isinstance(entry[key], str):
                raise ValueError("comfy.profiles.%s.%s 要是字串" % (name, key))
        if "loras" in entry and not isinstance(entry["loras"], list):
            raise ValueError("comfy.profiles.%s.loras 要是一串物件（[{name, strength, on}]）" % name)
    default = str(comfy_cfg.get("default_profile") or "").strip()
    if default and names and default not in names:
        raise ValueError("comfy.default_profile「%s」不在 comfy.profiles 裡（可選：%s）" % (default, "、".join(names)))
