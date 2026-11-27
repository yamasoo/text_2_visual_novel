"""畫風預設集（N5）：黑白／彩色／美式三組，style、meta 與文字端的畫風描述（look）只有這一份來源。

    p = styles.resolve(project.config)          # {"preset", "name", "look", "style", "meta", "sfx", "gray"}
    p = styles.resolve({"style_preset": "color"})
    prompts.system(p["look"])                    # 文字端提示詞跟著預設集走

project.yaml 的 style_preset 選一組（預設 mono）；明寫的 style／meta／look／sfx 只蓋掉那一欄。
資料在 t2c/data/styles.json，只用標準函式庫（CLAUDE.md 第 3 節）。
"""
import json
from pathlib import Path

DATA = Path(__file__).with_name("data") / "styles.json"
DEFAULT_PRESET = "mono"
FIELDS = ("name", "look", "style", "meta", "sfx")


class StylesError(Exception):
    """給人看的錯誤訊息（預設集名稱不認得、資料檔壞掉）。"""


_cache = {}


def _load():
    """讀並檢查資料檔（第一次呼叫時讀，之後用快取）。"""
    if "presets" in _cache:
        return _cache
    try:
        raw = json.loads(DATA.read_text(encoding="utf-8"))
    except OSError as error:
        raise StylesError("讀不到畫風預設集 %s：%s" % (DATA.name, error))
    except ValueError as error:
        raise StylesError("畫風預設集 %s 的 JSON 壞了：%s" % (DATA.name, error))
    presets = raw.get("presets") if isinstance(raw, dict) else None
    if not isinstance(presets, dict) or not presets:
        raise StylesError("畫風預設集 %s 沒有 presets" % DATA.name)
    for name, preset in presets.items():
        if not isinstance(preset, dict):
            raise StylesError("畫風預設集 %s 的 presets.%s 不是物件" % (DATA.name, name))
        missing = [key for key in FIELDS if not str(preset.get(key) or "").strip()]
        if missing:
            raise StylesError("畫風預設集 %s 的 presets.%s 缺 %s" % (DATA.name, name, "、".join(missing)))
        if not isinstance(preset["meta"], list) or not all(str(m).strip() for m in preset["meta"]):
            raise StylesError("畫風預設集 %s 的 presets.%s.meta 要是字串陣列" % (DATA.name, name))
        if not isinstance(preset.get("gray"), bool):
            raise StylesError("畫風預設集 %s 的 presets.%s.gray 要是 true 或 false（組版要不要轉灰階）" % (DATA.name, name))
    _cache["presets"] = presets
    _cache["default"] = str(raw.get("default") or DEFAULT_PRESET)
    if _cache["default"] not in presets:
        raise StylesError("畫風預設集 %s 的 default「%s」不在 presets 裡" % (DATA.name, _cache["default"]))
    return _cache


def names():
    """有哪幾組（排序後）。"""
    return sorted(_load()["presets"])


def default_name():
    return _load()["default"]


def preset(name=None):
    """一組預設集（沒給名字就是 default）。不認得就丟 StylesError 並列出可用的。"""
    presets = _load()["presets"]
    key = str(name or "").strip() or _cache["default"]
    if key not in presets:
        raise StylesError("沒有這組畫風「%s」（可用：%s）" % (key, "、".join(names())))
    return {field: list(value) if isinstance(value, list) else value for field, value in presets[key].items()}


def resolve(cfg=None):
    """專案設定 → 這一部作品實際要用的畫風。cfg 是 project.yaml 的內容。"""
    cfg = cfg or {}
    key = str(cfg.get("style_preset") or "").strip() or default_name()
    base = preset(key)
    style = " ".join(str(cfg.get("style") or base["style"]).split())
    meta = [str(m).strip() for m in (cfg.get("meta") or base["meta"]) if str(m).strip()]
    look = str(cfg.get("look") or base["look"]).strip()
    sfx = str(cfg.get("sfx") or base["sfx"]).strip()
    out = cfg.get("output") if isinstance(cfg.get("output"), dict) else {}
    gray = out["gray"] if isinstance(out.get("gray"), bool) else base["gray"]
    return {"preset": key, "name": base["name"], "look": look, "style": style, "meta": meta, "sfx": sfx, "gray": bool(gray)}
