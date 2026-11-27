"""漫畫專案（vendor/t2c）與 t2vn 的書、章之間的對應。

這一層只做五件事：路徑映射、設定注入、角色卡產生、逐句嵌字、錯誤轉換。真正的漫畫工作在
vendor/t2c/（workspace.py 是它的服務層），不要在這裡重寫（整合計畫 D8）。

目錄（docs/05_資料格式規格.md §5.11）::

    works/<book_id>/comic/<project_id>/                     t2c 的專案結構
    works/<book_id>/comic/<project_id>/cast_placeholders.json   佔位卡清單
    works/<book_id>/comic/<project_id>/letters/chNNN.json       逐句嵌字微調

守門只做一半（D2）：建立專案那一刻過 Config.assert_writable；之後目錄內的寫入由 t2c
自己負責，宿主攔不到。例外是 letters（宿主介面寫的），寫檔前自己過 assert_writable。
讀取端一律走 assert_inside()。
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

from . import appearance_card
from .art_cards import SKIP_CHARACTERS
from .config import Config, load_config
from .errors import ConfigError, T2vnError
from .ingest import BOOK_ID_RE, validate_book_id
from .schema_check import validate_data

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_JSON = REPO_ROOT / "vendor" / "t2c" / "default_config.json"
# 取不到外觀時用的英文外觀描述；看就知道是佔位（D10）
PLACEHOLDER_LOOK = "unidentified adult, plain clothes"
PLACEHOLDERS_FILE = "cast_placeholders.json"


from vendor.t2c import comfy as _t2c_comfy, render as _t2c_render   # noqa: E402  (錯誤型別要在檔案層可用)

_COMFY_ERRORS = (_t2c_render.RenderError, _t2c_comfy.ComfyError)


def _explain_comfy_text(text: Any) -> str:
    """把 ComfyUI 的原始錯誤補一句人話（最常見的是 preset 的 lora 檔名不在清單裡）。"""
    msg = str(text)
    if "value_not_in_list" in msg and "lora_name" in msg:
        return msg + (chr(10) + "→ LoRA 檔名不在 ComfyUI 的清單裡：到「設定 → ComfyUI（出圖）」按「抓模型清單」，"
                       "再用 preset 表格的 lora 欄位重挑（不要自己打路徑）。")
    return msg


class ComicError(T2vnError):
    """漫畫專案的錯誤；訊息直接給使用者看。"""


# ---------------------------------------------------------------- 路徑


def _config(config: Config | None) -> Config:
    return config if config is not None else load_config()


def book_work(book_id: str, config: Config | None = None) -> Path:
    """works/<book_id>；書不存在就丟錯（同 Studio.work 的判斷）。"""
    config = _config(config)
    try:
        book_id = validate_book_id(book_id)
    except T2vnError as error:
        raise ComicError(str(error)) from error
    work = config.works_dir / book_id
    if not (work / "book.json").is_file():
        raise ComicError("works/ 底下找不到這本書：%s" % book_id)
    return work


def comics_root(book_id: str, config: Config | None = None) -> Path:
    """works/<book_id>/comic：這本書的漫畫專案都放在這裡。"""
    return book_work(book_id, config) / "comic"


def project_dir(book_id: str, project_id: str, config: Config | None = None) -> Path:
    """漫畫專案資料夾；只組路徑，不檢查存在、也不寫入。"""
    project_id = validate_project_id(project_id)
    return comics_root(book_id, config) / project_id


def validate_project_id(project_id: Any) -> str:
    """project_id 與 book_id 同一條規則，順便擋掉 ../x 這種路徑穿越。"""
    value = project_id if isinstance(project_id, str) else ""
    if not BOOK_ID_RE.match(value):
        raise ComicError("project_id 只能用小寫英文開頭、接小寫英文數字與底線（2–31 字元）：%r" % (project_id,))
    return value


def assert_inside(root: Path, path: Any) -> Path:
    """把使用者給的路徑解析成絕對路徑，並確認它還在 root 之内；越界就丟錯。"""
    base = Path(root).resolve()
    raw = str(path or "").strip()
    if not raw:
        raise ComicError("沒有給檔案路徑")
    target = Path(raw)
    target = (target if target.is_absolute() else base / target).resolve()
    if not target.is_relative_to(base):
        raise ComicError("路徑跑出漫畫專案之外：%s" % raw)
    return target


# ---------------------------------------------------------------- 專案設定（D12）


SETTINGS_FILE = "settings.json"
LETTERING_TYPES: dict[str, Any] = {
    "dialogue_font": str, "shout_font": str, "narration_font": str, "sfx_font": str,
    "vertical": bool, "dialogue_pt": float, "narration_pt": float,
}
FONT_KEYS = ("dialogue_font", "shout_font", "narration_font", "sfx_font")
POINT_RANGE = (4.0, 48.0)
LORA_MODES = ("append", "replace")
SFX_MODES = ("overlay", "moss")
PROJECT_STYLE_KEYS = ("comfy_profile", "style_preset", "style", "meta", "look", "sfx_mode")


def _parse_project_loras(value: Any) -> list[dict[str, Any]]:
    """驗證漫畫專案自己的 LoRA 清單：[{name, strength, on}]；略過空白檔名列。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ComicError("loras 要是一串物件（[{name, strength, on}]）")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise ComicError("loras 第 %d 項要是物件" % i)
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        on = item.get("on", True)
        if not isinstance(on, bool):
            raise ComicError("loras 第 %d 項的 on 要是 true／false：%r" % (i, on))
        raw_strength = item.get("strength", 1.0)
        if isinstance(raw_strength, bool):
            raise ComicError("loras 第 %d 項的 strength 要是數字：%r" % (i, raw_strength))
        try:
            strength = float(raw_strength)
        except (TypeError, ValueError) as exc:
            raise ComicError("loras 第 %d 項的 strength 要是數字：%r" % (i, raw_strength)) from exc
        out.append({"name": name, "strength": strength, "on": on})
    return out


def _style_presets_info() -> dict[str, dict[str, Any]]:
    """從 vendor/t2c/data/styles.json 讀畫風預設集（mono / color / american）。"""
    from vendor.t2c import styles as t2c_styles

    out: dict[str, dict[str, Any]] = {}
    for key in t2c_styles.names():
        val = t2c_styles.resolve({"style_preset": key})
        out[key] = {
            "name": str(val.get("name") or key),
            "look": str(val.get("look") or ""),
            "style": str(val.get("style") or ""),
            "meta": list(val.get("meta") or []),
            "gray": bool(val.get("gray", False)),
        }
    return out


def _read_project_style(book_id: str, project_id: str, config: Config | None = None) -> dict[str, Any]:
    """讀 project.yaml 裡的畫風與 comfy_profile 設定；檔案不存在就回預設空值。"""
    import yaml

    path = project_dir(book_id, project_id, config) / "project.yaml"
    if not path.is_file():
        return {"comfy_profile": "", "style_preset": "mono", "style": "", "meta": [], "look": "", "sfx_mode": "overlay"}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    meta_raw = raw.get("meta")
    meta = [str(x).strip() for x in meta_raw if str(x).strip()] if isinstance(meta_raw, list) else []
    return {
        "comfy_profile": str(raw.get("comfy_profile") or "").strip(),
        "style_preset": str(raw.get("style_preset") or "mono").strip(),
        "style": str(raw.get("style") or "").strip(),
        "meta": meta,
        "look": str(raw.get("look") or "").strip(),
        "sfx_mode": str(raw.get("sfx_mode") or "overlay").strip(),
    }


def _save_project_style(book_id: str, project_id: str, changes: dict[str, Any], config: Config) -> dict[str, Any]:
    """把畫風與 comfy_profile 變更寫回 project.yaml（留空＝移除覆蓋、回到預設）。"""
    import yaml
    from .atomic import write_atomic

    if not isinstance(changes, dict):
        raise ComicError("project 要是物件")
    unknown = sorted(set(changes) - set(PROJECT_STYLE_KEYS))
    if unknown:
        raise ComicError("不認得的專案設定：%s" % "、".join(unknown))
    path = project_dir(book_id, project_id, config) / "project.yaml"
    if not path.is_file():
        raise ComicError("找不到漫畫專案：%s" % path.parent)
    config.assert_writable(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ComicError("讀不到 %s：%s" % (path, error)) from error
    if not isinstance(raw, dict):
        raw = {}

    presets = _style_presets_info()
    if "style_preset" in changes:
        preset = str(changes["style_preset"] or "").strip() or "mono"
        if presets and preset not in presets:
            raise ComicError("不認得的畫風預設集「%s」（可用：%s）" % (preset, "、".join(presets)))
        raw["style_preset"] = preset
    if "comfy_profile" in changes:
        prof = str(changes["comfy_profile"] or "").strip()
        if prof:
            raw["comfy_profile"] = prof
        else:
            raw.pop("comfy_profile", None)
    if "sfx_mode" in changes:
        mode = str(changes["sfx_mode"] or "overlay").strip()
        if mode not in SFX_MODES:
            raise ComicError("sfx_mode 只能是 %s（目前是 %r）" % ("、".join(SFX_MODES), mode))
        raw["sfx_mode"] = mode
    for key in ("style", "look"):
        if key in changes:
            val = str(changes[key] or "").strip()
            if val:
                raw[key] = val
            else:
                raw.pop(key, None)
    if "meta" in changes:
        raw_meta = changes["meta"]
        if isinstance(raw_meta, str):
            items = [part.strip() for part in re.split(r"[\n,]+", raw_meta) if part.strip()]
        elif isinstance(raw_meta, list):
            items = [str(x).strip() for x in raw_meta if str(x).strip()]
        else:
            raise ComicError("meta 要是字串或字串陣列")
        if items:
            raw["meta"] = items
        else:
            raw.pop("meta", None)

    write_atomic(path, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))
    return _read_project_style(book_id, project_id, config)


def settings_path(book_id: str, project_id: str, config: Config | None = None) -> Path:
    """漫畫專案自己的設定檔（覆寫 t2c default_config 的 lettering 與專案專屬 loras）；只組路徑，不檢查存在。"""
    return project_dir(book_id, project_id, config) / SETTINGS_FILE


def read_settings(book_id: str, project_id: str, config: Config | None = None) -> dict[str, Any]:
    """專案設定（lettering、專案專屬 loras、project.yaml 畫風與 comfy_profile）。"""
    cfg = _config(config)
    path = settings_path(book_id, project_id, cfg)
    doc: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                doc = loaded
        except (OSError, ValueError) as error:
            raise ComicError("讀不到 %s：%s" % (path, error)) from error
    lettering = doc.get("lettering")
    kept = dict(lettering) if isinstance(lettering, dict) else {}
    loras = _parse_project_loras(doc.get("loras")) if isinstance(doc.get("loras"), list) else []
    lora_mode = str(doc.get("lora_mode") or "append").strip()
    if lora_mode not in LORA_MODES:
        lora_mode = "append"
    return {
        "lettering": kept,
        "problems": font_problems(kept),
        "loras": loras,
        "lora_mode": lora_mode,
        "project": _read_project_style(book_id, project_id, cfg),
        "style_presets": _style_presets_info(),
        "comfy_profiles": list(cfg.comfy_profiles.keys()),
        "comfy_default_profile": cfg.comfy_default_profile,
    }


def save_settings(book_id: str, project_id: str, lettering: dict[str, Any] | None = None,
                  config: Config | None = None, *, loras: Any = None,
                  lora_mode: str | None = None, project: dict[str, Any] | None = None) -> dict[str, Any]:
    """存漫畫專案設定：lettering、專案專屬 loras／lora_mode（寫 settings.json），與 project 畫風／profile（寫 project.yaml）。"""
    config = _config(config)
    current = read_settings(book_id, project_id, config)
    merged_lettering = dict(current["lettering"])
    if lettering is not None:
        if not isinstance(lettering, dict):
            raise ComicError("lettering 要是物件")
        unknown = sorted(set(lettering) - set(LETTERING_TYPES))
        if unknown:
            raise ComicError("不認得的設定：%s（可用：%s）" % ("、".join(unknown), "、".join(LETTERING_TYPES)))
        for key, value in lettering.items():
            if value is None or value == "":
                merged_lettering.pop(key, None)
                continue
            want = LETTERING_TYPES[key]
            if want is float:
                low, high = POINT_RANGE
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= float(value) <= high:
                    raise ComicError("%s 要是 %g–%g 的數字：%r" % (key, low, high, value))
                merged_lettering[key] = float(value)
            elif want is bool:
                if not isinstance(value, bool):
                    raise ComicError("%s 要是 true／false：%r" % (key, value))
                merged_lettering[key] = value
            else:
                if not isinstance(value, str):
                    raise ComicError("%s 要是字串：%r" % (key, value))
                merged_lettering[key] = value.strip()

    merged_loras = _parse_project_loras(loras) if loras is not None else list(current["loras"])
    merged_mode = current["lora_mode"]
    if lora_mode is not None:
        mode = str(lora_mode or "append").strip()
        if mode not in LORA_MODES:
            raise ComicError("lora_mode 只能是 %s（目前是 %r）" % ("、".join(LORA_MODES), lora_mode))
        merged_mode = mode

    if project is not None:
        _save_project_style(book_id, project_id, project, config)

    if lettering is not None or loras is not None or lora_mode is not None:
        path = settings_path(book_id, project_id, config)
        config.assert_writable(path)
        from .atomic import write_atomic

        doc: dict[str, Any] = {"lettering": merged_lettering}
        if merged_loras or merged_mode != "append":
            doc["loras"] = merged_loras
            doc["lora_mode"] = merged_mode
        write_atomic(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")

    return read_settings(book_id, project_id, config)


def font_problems(lettering: dict[str, Any]) -> list[str]:
    """字型檔不存在就提醒（存得下去，但組版會退回系統字型；TTC 允許 file#index 寫法）。"""
    problems = []
    for key in FONT_KEYS:
        raw = str(lettering.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw.split("#", 1)[0])
        if not path.is_file():
            problems.append("%s：找不到字型檔 %s（組版會退回系統字型）" % (key, path))
    return problems


# 可變字型（VF）的字重。Pillow 只讀檔不套可變軸，會停在最細的預設實例
# （NotoSansTC-VF 的 wght 預設是 100／Thin），所以依角色自己指定一次。
FONT_WEIGHTS = {"dialogue": "Regular", "narration": "Medium", "shout": "Black", "sfx": "Black"}


@functools.lru_cache(maxsize=1)
def _weighted_settings_class():
    """t2c 的 Settings 加一層：font() 建好字型後依角色套可變字型的字重。

    靜態字型（.ttc／單一實例的 .otf）沒有可變軸，set_variation_by_name 會失敗，
    這時照原樣用——不含可變字型的專案行為完全不變。
    """
    from vendor.t2c import letter as t2c_letter

    class Weighted(t2c_letter.Settings):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._weighted = set()

        def font(self, role, size):
            font = super().font(role, size)
            key = (role, int(getattr(font, "size", 0)))
            weight = FONT_WEIGHTS.get(role)
            if weight and key not in self._weighted:
                self._weighted.add(key)
                try:
                    font.set_variation_by_name(weight)
                except (AttributeError, OSError, ValueError):
                    pass
            return font

    return Weighted


def lettering_settings(lettering: dict[str, Any] | None = None):
    """嵌字設定（t2c 的 Settings 子類別）；字型找不到時轉成 ComicError。"""
    from vendor.t2c import letter as t2c_letter

    try:
        return _weighted_settings_class().from_config(lettering or {})
    except t2c_letter.LetterError as error:
        raise ComicError(str(error)) from error


# ---------------------------------------------------------------- 專案


def open_project(book_id: str, project_id: str, config: Config | None = None):
    """開啟漫畫專案（回傳 t2c 的 Project）。"""
    from vendor.t2c import workspace as t2c_workspace

    root = project_dir(book_id, project_id, config)
    if not (root / "project.yaml").is_file():
        raise ComicError("找不到漫畫專案：%s" % root)
    try:
        return t2c_workspace.open_project(root)
    except t2c_workspace.WorkspaceError as error:
        raise ComicError(str(error)) from error


def create_project(book_id: str, project_id: str, title: str = "", *, preset: str | None = None,
                   style: str = "", config: Config | None = None) -> dict[str, Any]:
    """建立漫畫專案骨架（t2c 的 project.yaml ＋ source/ ＋ bible/）。回傳摘要。"""
    from vendor.t2c import workspace as t2c_workspace

    config = _config(config)
    project_id = validate_project_id(project_id)
    title = str(title or "").strip() or project_id
    root = comics_root(book_id, config) / project_id
    try:
        config.assert_writable(root)          # 建立那一刻的唯一守門（D2）
    except ConfigError as error:
        raise ComicError("漫畫專案要建在 works_dir 之內：%s" % error) from error
    if root.exists() and any(root.iterdir()):
        raise ComicError("漫畫專案 %s 已經存在而且不是空的；換一個 project_id 或先自己清掉" % project_id)
    try:
        project = t2c_workspace.create_project(root, title=title, style=style, preset=preset)
    except t2c_workspace.WorkspaceError as error:
        raise ComicError(str(error)) from error
    return {"book_id": book_id, "project_id": project_id, "title": title,
            "path": str(project.root), "preset": project.config.get("style_preset")}


# ---------------------------------------------------------------- 章節正文


def read_chapter(book_id: str, number: int, config: Config | None = None) -> dict[str, Any]:
    """讀 chapters/chNNN.json（t2vn 正規化過的章節）。"""
    work = book_work(book_id, config)
    try:
        number = int(number)
    except (TypeError, ValueError):
        raise ComicError("章號要是整數：%r" % (number,))
    path = work / "chapters" / ("ch%03d.json" % number)
    if not path.is_file():
        raise ComicError("這本書沒有第 %d 章：%s" % (number, path))
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ComicError("讀不到 %s：%s" % (path, error)) from error
    if not isinstance(doc.get("paragraphs"), list):
        raise ComicError("%s 沒有 paragraphs" % path)
    return doc


def source_text(doc: dict[str, Any]) -> str:
    """章節資料 → 漫畫的 source/chNNN.txt（第一行標題、空行、其後一段一行、段落之間空行）。"""
    number = int(doc.get("chapter_no") or 0)
    title = str(doc.get("title") or "").strip()
    head = "# 第%d章" % number
    if title and title != str(number):
        head += " %s" % title
    blocks = [head]
    for para in doc["paragraphs"]:
        text = str(para.get("text") or "").strip()
        if not text:
            continue
        blocks.append("")
        blocks.append(text)
    return "\n".join(blocks) + "\n"


def write_source(book_id: str, project_id: str, number: int, config: Config | None = None) -> dict[str, Any]:
    """把 chapters/chNNN.json 的段落寫成漫畫專案的 source/chNNN.txt，並驗證讀回來段落數相同。"""
    from vendor.t2c import annotate as t2c_annotate
    from vendor.t2c import workspace as t2c_workspace

    doc = read_chapter(book_id, number, config)
    project = open_project(book_id, project_id, config)
    wanted = [str(para.get("text") or "").strip() for para in doc["paragraphs"]]
    wanted = [text for text in wanted if text]
    text = source_text(doc)
    try:
        path = t2c_workspace.save_source(project, number, text)
    except t2c_workspace.WorkspaceError as error:
        raise ComicError(str(error)) from error
    try:
        got = t2c_annotate.split_paragraphs(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ComicError("寫完讀不回來 %s：%s" % (path, error)) from error
    if got != wanted:
        first = next((i for i, (a, b) in enumerate(zip(got, wanted), 1) if a != b), min(len(got), len(wanted)) + 1)
        raise ComicError("段落對不上：t2vn 有 %d 段、t2c 讀回 %d 段，第 %d 段開始不同（切法不能改，要調寫檔格式）"
                         % (len(wanted), len(got), first))
    return {"book_id": book_id, "project_id": project_id, "chapter": int(number),
            "path": str(path), "paragraphs": len(got), "title": str(doc.get("title") or "")}


# ---------------------------------------------------------------- bible 卡（D22–D26）

TIME_OF_DAY = ("dawn", "morning", "day", "dusk", "night")


def _read_json_file(path: Path, default: Any) -> Any:
    """讀 JSON；檔案不存在或壞掉回 default（轉換器不因單一檔案中斷）。"""
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _appearance_data(work: Path, char_id: str) -> dict[str, Any]:
    """外觀卡的原始 dict（含 D26 的 t2c 子物件）；沒有或壞掉回空 dict。"""
    data = _read_json_file(work / "art" / "appearance" / ("%s.json" % char_id), {})
    return data if isinstance(data, dict) else {}


def _split_prompts(text: Any) -> list[str]:
    """提示詞 → 標籤清單；含 __wildcard__ 的片段跳過（t2c 不展開 wildcard）。"""
    out: list[str] = []
    for raw in str(text or "").replace(chr(10), ",").split(","):
        tag = " ".join(raw.split()).strip(" .")
        if not tag or "__" in tag:
            continue
        if tag not in out:
            out.append(tag)
    return out


def _derived_sections(card: dict[str, Any]) -> dict[str, Any]:
    """沒有 t2c 人工值時的粗拆：頭像→head、體型詞→body、配件→accessory、其餘非動作詞→outfits.default.top。

    這是文字規則的草稿（不是模型），一定還要人看過；check 的 error 會指出哪裡要搬。
    """
    from vendor.t2c import bible as t2c_bible, bible_check as bc

    body, accessory, clothing = [], [], []
    for tag in _split_prompts(card.get("sprite_prompt")):
        words = set(re.findall(r"[a-z-]+", tag.lower()))
        if words & set(bc.POSE_WORDS) or words & set(bc.EXPRESSION_WORDS):
            continue
        if words & set(t2c_bible.ACCESSORY_WORDS):
            accessory.append(tag)
        elif (words & set(t2c_bible.BODY_WORDS) or words & set(t2c_bible.UPPER_WORDS)
              or words & set(t2c_bible.LOWER_WORDS)):
            body.append(tag)
        else:
            clothing.append(tag)
    sections: dict[str, Any] = {"head": _split_prompts(card.get("headshot_prompt"))}
    if body:
        sections["body"] = body
    if accessory:
        sections["accessory"] = accessory
    out: dict[str, Any] = {"sections": sections}
    if clothing:
        out["outfits"] = {"default": {"top": clothing}}
    return out


def _character_card(work: Path, char_id: str, info: dict[str, Any]) -> tuple[dict[str, Any], list[Any], bool]:
    """→ (t2c 卡 dict, check 的 issues, 是否佔位)。沒有外觀卡就回佔位卡（D10 的既有語意）。"""
    from vendor.t2c import bible_check as bc

    card = _appearance_data(work, char_id)
    names = info.get("names") if isinstance(info.get("names"), dict) else {}
    name = str(names.get("zh-TW") or names.get("en") or char_id).strip() or char_id
    aliases = [str(a) for a in (info.get("aliases") or []) if str(a).strip()]
    data: dict[str, Any] = {"id": char_id, "name": name, "aliases": aliases}
    if not card:
        data["look_en"] = PLACEHOLDER_LOOK
        return data, [], True
    block = appearance_card.t2c_block(card)
    if block and block.get("sections"):
        data["sections"] = block["sections"]
        if block.get("outfits"):
            data["outfits"] = block["outfits"]
    else:
        data.update(_derived_sections(card))
    return data, bc.check_card(data, "character", "角色 %s" % char_id), False


def _location_names(work: Path) -> dict[str, str]:
    """地點中文名：從 manifest 的 bg 名稱拆「・」前面那段（bg_scrapyard_dawn → 舊工坊）。"""
    out: dict[str, str] = {}
    manifest = _read_json_file(work / "manifest.json", {})
    assets = manifest.get("assets") if isinstance(manifest, dict) else None
    for item in assets or []:
        if not isinstance(item, dict) or item.get("kind") != "bg":
            continue
        asset_id = str(item.get("asset_id") or "")
        if not asset_id.startswith("bg_"):
            continue
        rest = asset_id[3:]
        for part in TIME_OF_DAY:
            if rest.endswith("_" + part):
                rest = rest[: -(len(part) + 1)]
                break
        if rest in out:
            continue
        label = str((item.get("names") or {}).get("zh-TW") or "").strip()
        label = re.split(r"[・·]", label)[0].strip() if label else ""
        if label:
            out[rest] = label
    return out


def _location_cards(work: Path) -> dict[str, tuple[dict[str, Any], list[Any]]]:
    """style.json 的 locations → 地點卡與 check 結果。look_en 用 description（英文段落）。"""
    from vendor.t2c import bible_check as bc

    style = _read_json_file(work / "art" / "style.json", {})
    locations = style.get("locations") if isinstance(style, dict) else None
    names = _location_names(work)
    out: dict[str, tuple[dict[str, Any], list[Any]]] = {}
    for lid, entry in (locations or {}).items():
        if not isinstance(entry, dict):
            continue
        look_en = str(entry.get("description") or "").strip() or str(entry.get("tags") or "").strip()
        card = {"id": str(lid), "name": names.get(str(lid)) or str(lid), "look_en": look_en}
        out[str(lid)] = (card, bc.check_card(card, "location", "地點 %s" % lid))
    return out


def _bible_body(kind: str, card: dict[str, Any], errors: list[Any]) -> str:
    """卡片的本文（人看的）：來源、衍生物警告、待修清單。"""
    if kind == "character":
        origin = "art/appearance/%s.json" % card["id"]
    else:
        origin = "art/style.json 的 locations.%s" % card["id"]
    lines = [
        "來源：%s。" % origin,
        "這張卡是轉出來的衍生物（D23）；要改請改來源，重跑會覆蓋。",
    ]
    if errors:
        lines += ["", "## 待修（%d 個 error，修完再重跑）" % len(errors)]
        lines += ["- %s：%s" % (item.where or card["id"], item.message) for item in errors[:20]]
    return "\n".join(lines)


def _with_placeholder_flag(text: str) -> str:
    """在 frontmatter 補 `placeholder: true`（宿主標記，t2c 不讀）。

    vendor 的 render_card 只寫它認得的欄位，而 vendor 不能改（sync_t2c 會判漂移），所以
    在 render 之後補這一行；bible_status 靠它把佔位卡從「不合規的卡」分出來（規格 §3.4）。
    """
    lines = text.split("\n")
    if lines and lines[0] == "---":
        try:
            end = lines.index("---", 1)
        except ValueError:
            return text
        lines.insert(end, "placeholder: true")
        return "\n".join(lines)
    return text


def _write_bible(project: Any, kind: str, card: dict[str, Any], issues: list[Any], *,
                 overwrite: bool, config: Config,
                 placeholder: bool = False) -> tuple[str, list[Any], str | None]:
    """寫一張 bible 卡。回 (狀態, errors, 被收掉的舊檔)；狀態是 written／draft／kept。

    0 error 進 bible/<kind>/；有 error 進 bible/drafts/<kind>/（t2c 的 project.py 只讀定案目錄，
    drafts 不會污染 project.characters）。佔位卡照舊進定案目錄（D10），不列入 error。

    一張卡只留一個實體（D28）：寫進這一邊就刪掉另一邊的同 id 舊檔。否則同一個 id 會在清單出現
    兩列，而且 t2c 的 project.py 會繼續讀定案目錄那份舊值（靜默吃舊資料）。bible/*.md 是純衍生物
    （D26：永遠可以重寫），刪掉不損失人工值——人工值在外觀卡的 t2c 子物件。
    """
    from vendor.t2c import bible as t2c_bible

    errors = [] if placeholder else [item for item in issues if item.level == "error"]
    folder = t2c_bible.cards_dir(project, kind, drafts=bool(errors))
    path = folder / ("%s.md" % card["id"])
    stale = t2c_bible.cards_dir(project, kind, drafts=not errors) / path.name
    # 定案卡或草稿已經存在就不動（不覆蓋的語意照 D23）；要看舊卡的問題用 bible_status。
    if not overwrite and (path.is_file() or stale.is_file()):
        return "kept", errors, None
    try:
        config.assert_writable(path)
        if stale.is_file():
            config.assert_writable(stale)   # 先驗再動手：不留「寫了新卡卻刪不掉舊卡」的半套狀態
    except ConfigError as error:
        raise ComicError("bible 卡要寫在可寫範圍內：%s" % error) from error
    folder.mkdir(parents=True, exist_ok=True)
    text = t2c_bible.render_card(card, _bible_body(kind, card, errors),
                                 status="draft" if errors else "confirmed")
    if placeholder:
        text = _with_placeholder_flag(text)
    path.write_text(text, encoding="utf-8")
    removed = None
    if stale.is_file():
        stale.unlink()
        removed = str(stale)
    return ("draft" if errors else "written"), errors, removed


def _record(report: dict[str, Any], state: str, card_id: str, errors: list[Any]) -> None:
    """把一筆結果記進報告；errors 攤平成 {card, where, message}。"""
    key = {"written": "written", "draft": "drafts", "kept": "kept"}[state]
    report[key].append(card_id)
    for item in errors:
        report["issues"].append({"card": card_id, "where": item.where, "message": item.message})


def bible_cards(book_id: str, project_id: str, *, overwrite: bool = False,
                config: Config | None = None) -> dict[str, Any]:
    """由 cast.json／外觀卡／style.json 產生 bible 角色卡與地點卡（D22–D26）。

    檢查（vendor.t2c.bible_check）0 error 才進定案目錄，否則進 drafts 並回報。
    cast 裡的心聲 id（`SKIP_CHARACTERS`，目前只有 player）不寫卡：它不進分鏡的角色名單
    （annotate.py:270），寫一張「主角（內心）」的卡只會變成沒人引用的假角色。
    """
    from vendor.t2c import project as t2c_project

    config = _config(config)
    work = book_work(book_id, config)
    project = open_project(book_id, project_id, config)
    cast = _read_json_file(work / "cast.json", {})
    characters = cast.get("characters") if isinstance(cast, dict) else None
    if not isinstance(characters, dict) or not characters:
        raise ComicError("%s 沒有 characters" % (work / "cast.json"))

    report: dict[str, Any] = {
        "book_id": book_id, "project_id": project_id, "project_root": str(project.root),
        "written": [], "drafts": [], "kept": [], "removed": [], "stale": [], "issues": [],
        "placeholder": [], "skipped_characters": [], "skipped_ids": [],
    }
    for char_id in sorted(characters):
        if str(char_id) in SKIP_CHARACTERS:
            report["skipped_characters"].append(str(char_id))
            continue
        if not t2c_project.ID_RE.match(str(char_id)):
            report["skipped_ids"].append(str(char_id))
            continue
        info = characters[char_id] if isinstance(characters[char_id], dict) else {}
        card, issues, placeholder = _character_card(work, str(char_id), info)
        state, errors, removed = _write_bible(project, "character", card, issues,
                                              overwrite=overwrite, config=config,
                                              placeholder=placeholder)
        if removed:
            report["removed"].append(str(char_id))
        if placeholder:
            report["placeholder"].append(str(char_id))
        appearance = _appearance_data(work, str(char_id))
        block = appearance_card.t2c_block(appearance)
        if not placeholder and not (block and block.get("sections")):
            report["issues"].append({"card": str(char_id), "where": "t2c",
                                     "message": "缺 t2c.sections（人工值）：這張卡是文字規則粗拆的草稿"})
        if block and block.get("source_sha1") and block["source_sha1"] != appearance_card.prompt_fingerprint(appearance):
            report["stale"].append(str(char_id))
        _record(report, state, str(char_id), errors)
    for lid, (card, issues) in _location_cards(work).items():
        state, errors, removed = _write_bible(project, "location", card, issues,
                                              overwrite=overwrite, config=config)
        if removed:
            report["removed"].append(lid)
        _record(report, state, lid, errors)

    marker = project.root / PLACEHOLDERS_FILE
    marker.write_text(json.dumps({
        "book_id": book_id, "project_id": project_id, "characters": len(characters),
        "written": len(report["written"]), "kept": len(report["kept"]),
        "placeholder": report["placeholder"], "skipped_characters": report["skipped_characters"],
        "skipped_ids": report["skipped_ids"],
        "drafts": report["drafts"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def from_cast(book_id: str, project_id: str, *, overwrite: bool = False,
              config: Config | None = None) -> dict[str, Any]:
    """舊名字，等於 bible_cards()；保留給既有呼叫端與測試。"""
    return bible_cards(book_id, project_id, overwrite=overwrite, config=config)


def appearance_card_check(book_id: str, char_id: str, config: Config | None = None) -> dict[str, Any]:
    """單張外觀卡的 t2c 視圖：bible 會寫出的那份卡、逐條 issue、人工值狀態與粗拆初稿。

    走的是 `_character_card()` 同一條路徑，介面看到的問題就是 POST …/bible 會寫進
    `drafts/` 的那些。只讀，不寫任何檔。
    """
    work = book_work(book_id, config)
    cast = _read_json_file(work / "cast.json", {})
    characters = cast.get("characters") if isinstance(cast, dict) else None
    info = characters.get(char_id) if isinstance(characters, dict) else None
    appearance = _appearance_data(work, char_id)
    card, issues, placeholder = _character_card(work, char_id, info if isinstance(info, dict) else {})
    block = appearance_card.t2c_block(appearance) or {}
    current_sha1 = appearance_card.prompt_fingerprint(appearance)
    return {
        "char_id": char_id,
        "source": "placeholder" if placeholder else ("t2c" if block.get("sections") else "derived"),
        "stale": bool(block.get("source_sha1")) and block["source_sha1"] != current_sha1,
        "current_sha1": current_sha1,
        "issues": [
            {"level": item.level, "where": item.where, "message": item.message} for item in issues
        ],
        "draft": _derived_sections(appearance) if appearance else {},
        "card": card,
    }


def save_appearance_t2c(book_id: str, char_id: str, payload: Any,
                        config: Config | None = None) -> dict[str, Any]:
    """漫畫頁送來的 t2c 子物件寫回外型卡（正本）；走 art_cards.save_card 的合併語意。

    只換 `t2c`：兩個提示詞、expressions、confirmed、notes 從舊檔原樣帶回，所以「介面只送
    t2c」不會把提示詞洗成空字串（那是拿不完整卡片呼叫 save_card 的後果）。
    指紋：payload 帶了合法的就用它，否則沿用舊值，第一次寫入時蓋上目前提示詞的指紋。
    """
    from . import art_cards

    config = _config(config)
    work = book_work(book_id, config)
    path = art_cards.card_path(work, char_id)
    if not path.is_file():
        raise ComicError("角色 %s 沒有外型卡（正本），人工值無處可寫：%s" % (char_id, path))
    try:
        card = appearance_card.load_card(path)
    except (OSError, ValueError) as error:
        raise ComicError("讀不到 %s：%s" % (path, error)) from error
    block = appearance_card.t2c_block({"t2c": payload})
    if block is None:
        raise ComicError("t2c 至少要有非空的 sections 或 outfits.default")
    block.setdefault("source_sha1", (appearance_card.t2c_block(card) or {}).get("source_sha1")
                     or appearance_card.prompt_fingerprint(card))
    card["t2c"] = block
    try:
        saved = art_cards.save_card(config.assert_writable, work, char_id, card)
    except art_cards.ArtCardsError as error:
        raise ComicError(str(error)) from error
    return {"card": saved, "check": appearance_card_check(book_id, char_id, config)}


def bible_status(book_id: str, project_id: str, config: Config | None = None) -> dict[str, Any]:
    """列出 bible 卡與每張卡的 check 結果（定案與 drafts 都列）。"""
    from vendor.t2c import bible as t2c_bible, bible_check as bc

    project = open_project(book_id, project_id, config)
    out: dict[str, Any] = {"book_id": book_id, "project_id": project_id, "characters": [], "locations": []}
    for kind, key in (("character", "characters"), ("location", "locations")):
        label = "角色" if kind == "character" else "地點"
        for drafts in (False, True):
            for folder_path, meta, broken in t2c_bible.read_cards(t2c_bible.cards_dir(project, kind, drafts)):
                if meta is None:
                    out[key].append({
                        "id": folder_path.stem, "path": str(folder_path), "draft": drafts,
                        "placeholder": False,
                        "errors": [{"where": item.where, "message": item.message} for item in broken],
                        "warnings": [],
                    })
                    continue
                issues = bc.check_card(meta, kind, "%s %s" % (label, folder_path.stem))
                out[key].append({
                    "id": str(meta.get("id") or folder_path.stem), "path": str(folder_path), "draft": drafts,
                    "placeholder": bool(meta.get("placeholder")),
                    "errors": [{"where": i.where, "message": i.message} for i in issues if i.level == "error"],
                    "warnings": [{"where": i.where, "message": i.message} for i in issues if i.level != "error"],
                })
    return out

# ---------------------------------------------------------------- 設定注入


def comic_cfg(config: Config | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """t2c 的設定：讀 vendored 的 default_config.json，覆寫 comfy.url（D5）與專案的 lettering（D12）。

    直接改 vendored 的 default_config.json 會被 sync_t2c 判成漂移（B5），所以專案層級的
    覆寫一律走 works/<book>/comic/<project>/settings.json，在記憶體裡疊上去。
    """
    config = _config(config)
    if not DEFAULT_CONFIG_JSON.is_file():
        raise ComicError("找不到 %s（先跑 python tools/sync_t2c.py）" % DEFAULT_CONFIG_JSON)
    try:
        cfg = json.loads(DEFAULT_CONFIG_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ComicError("讀不到 %s：%s" % (DEFAULT_CONFIG_JSON, error)) from error
    comfy = cfg.setdefault("comfy", {})
    comfy["url"] = config.comfyui_base_url
    if config.comfy_profiles:
        # 宿主設定是正本（QQ-2）：以 vendored 的同名 profile 當底，再疊使用者填的欄位，
        # 這樣只填 unet 的人不會把 presets／negative 洗掉。vendored 檔本身一張都沒改。
        merged = {name: dict(spec) for name, spec in (comfy.get("profiles") or {}).items() if isinstance(spec, dict)}
        for name, spec in config.comfy_profiles.items():
            base = dict(merged.get(name) or {})
            for key, value in spec.items():
                if key in ("models",) and isinstance(value, dict):
                    base["models"] = {**(base.get("models") or {}), **value}
                elif key in ("presets", "negative", "workflow") and not value:
                    continue   # 空字串／空字典＝沿用 vendored 的
                else:
                    # default_preset 例外：宿主送什麼就用什麼（空字串＝不用 preset）。
                    # 這裡以前把空字串也當「沿用」，結果 vendored 的 final 留著、presets 已被宿主取代，
                    # 出圖就報「找不到出圖預設 final」——見 2026-11-12 的修正。
                    base[key] = value
            presets = base.get("presets") if isinstance(base.get("presets"), dict) else {}
            if base.get("default_preset") and base["default_preset"] not in presets:
                base["default_preset"] = ""   # 指向不存在的 preset 就清掉，交給 workflow 內建
            merged[name] = base
        comfy["profiles"] = merged
        if config.comfy_default_profile:
            comfy["default_profile"] = config.comfy_default_profile
    # 專案專屬 LoRA（settings.json）：疊加在全域 profile.loras 後面，或 replace 整組取代
    proj_loras = (overrides or {}).get("loras") if isinstance((overrides or {}).get("loras"), list) else None
    lora_mode = str((overrides or {}).get("lora_mode") or "append").strip()
    if proj_loras is not None and (proj_loras or lora_mode == "replace"):
        profiles = comfy.get("profiles")
        if isinstance(profiles, dict):
            for prof in profiles.values():
                if not isinstance(prof, dict):
                    continue
                if lora_mode == "replace":
                    prof["loras"] = list(proj_loras)
                else:
                    prof["loras"] = list(prof.get("loras") or []) + list(proj_loras)
    lettering = cfg.setdefault("lettering", {})
    for key, value in ((overrides or {}).get("lettering") or {}).items():
        if key in LETTERING_TYPES:
            lettering[key] = value
    return cfg


def render_profile(config: Config | None = None) -> dict[str, Any]:
    """comic_cfg() 解出來的出圖 profile（base_url、workflow、models、presets）。"""
    from vendor.t2c import comfy as t2c_comfy

    try:
        return t2c_comfy.resolve_profile(comic_cfg(config))
    except t2c_comfy.ComfyError as error:
        raise ComicError(str(error)) from error

# ---------------------------------------------------------------- LLM 注入（D4）

STAGES = ("annotate", "plan", "script", "storyboard")
STAGE_LABEL = {"annotate": "切對話", "plan": "改編規劃", "script": "劇本", "storyboard": "分鏡"}


def make_chat(config: Config | None = None, executor: str | None = None, on_usage=None):
    """回傳 (chat, chat_json)：把 t2c 的模型呼叫轉到宿主的執行者。

    t2c 的簽章是 ``chat(prov, messages, **kw) -> llm.Reply``。``prov`` 只是階段標記：
    真正的供應商由宿主的 ``executors.resolve_executor`` 決定，金鑰也只有宿主那一份。
    """
    from vendor.t2c import llm as t2c_llm

    from .llm import LlmError, make_executor

    config = _config(config)
    try:
        call = make_executor(config, executor)
    except LlmError as error:
        raise ComicError(str(error)) from error

    def chat(prov, messages, json_mode: bool = False, max_tokens=None, stage=None, temperature=None,
             on_delta=None, on_thinking=None, should_stop=None, **_ignored):
        """轉呼叫宿主的執行者；json_mode 決定要不要送 response_format。

        t2c 的文字階段（script）走 adapt.Adapter._call 的 as_json=False 分支進來，預設
        json_mode=False；chat_json 會明確傳 True。
        """
        result = call([dict(m) for m in messages], on_delta=on_delta, should_stop=should_stop,
                      on_thinking=on_thinking, json_mode=bool(json_mode))
        usage = dict(result.usage or {})
        if on_usage is not None:
            on_usage(usage)
        text = str(result.text or "")
        if not json_mode and not text.strip():
            # t2c 的驗證會把空白講成「0 個『## 第 N 頁』標題」，看不出真正的原因
            raise ComicError("模型回傳空白（可用 token %s、finish_reason=%s）：%s 階段沒有拿到任何文字，"
                             "這不是格式不符。可以按「重跑四個文字階段（覆蓋）」再試一次。"
                             % (usage.get("completion_tokens", 0), usage.get("finish_reason") or "沒有",
                                STAGE_LABEL.get(stage, stage or "?")))
        # 上游有 finish_reason 就用它（t2c 靠 "length" 判斷截斷）；沒給才退回 "stop"
        finish = str(usage.get("finish_reason") or "stop")
        return t2c_llm.Reply(text=text, usage=usage, finish=finish)

    def chat_json(prov, messages, fix_rounds=1, **kw):
        msgs = list(messages)
        for round_ in range(int(fix_rounds or 0) + 1):
            reply = chat(prov, msgs, json_mode=True, **kw)
            try:
                return t2c_llm.parse_json(reply.text), reply
            except t2c_llm.JSONParseError as error:
                if round_ >= fix_rounds:
                    raise
                msgs = msgs + [{"role": "assistant", "content": reply.text},
                               {"role": "user", "content": "上一次的輸出無法解析成 JSON：%s；請只輸出修正後的完整 JSON。" % error}]
        raise ComicError("模型沒有回傳可用的 JSON")

    return chat, chat_json


def check_stages(stages: Any) -> list[str]:
    """階段清單要都在 STAGES 之内；重複的收掉、順序照 STAGES。"""
    if isinstance(stages, str):
        stages = [stages]
    wanted = [str(s) for s in (stages or [])]
    bad = [s for s in wanted if s not in STAGES]
    if bad:
        raise ComicError("階段要是 %s 其中之一，不認得：%s" % ("、".join(STAGES), "、".join(bad)))
    if not wanted:
        raise ComicError("至少要選一個階段：%s" % "、".join(STAGES))
    return [s for s in STAGES if s in wanted]


def run_stages(book_id: str, project_id: str, chapters: list[int], stages: list[str], *,
               config: Config | None = None, chat=None, chat_json=None, should_stop=None,
               on_event=None, on_delta=None, on_usage=None, on_stage=None, force: bool = False,
               executor: str | None = None) -> dict[str, Any]:
    """依 STAGES 的順序跑 LLM 階段（一個階段跑完全部章節，再跑下一個）。回傳摘要。

    真正的階段實作在 vendor/t2c 的 adapt.Adapter；這裡只做注入與結果整理。
    """
    from vendor.t2c import adapt

    picked = check_stages(stages)
    numbers = [int(n) for n in (chapters or [])]
    if not numbers:
        raise ComicError("至少要選一章")
    project = open_project(book_id, project_id, config)
    if chat is None or chat_json is None:
        chat, chat_json = make_chat(config, executor, on_usage=on_usage)
    adapter = adapt.Adapter(project, prov_for=lambda key: {"id": str(key)}, should_stop=should_stop,
                            on_event=on_event, on_delta=on_delta, chat=chat, chat_json=chat_json)
    stage_output = {
        "annotate": project.scenes_path,
        "plan": project.plan_path,
        "script": project.script_path,
        "storyboard": project.storyboard_path,
    }
    skipped_stages: list[dict[str, Any]] = []
    for stage in picked:
        for number in numbers:
            if not force and len(picked) > 1 and stage_output[stage](number).is_file():
                skipped_stages.append({"stage": stage, "chapter": number})
                if on_stage is not None:
                    on_stage(stage, number, "已完成（跳過）")
                continue
            if stage == "annotate":
                _, report = adapter.run_annotate(number, "", force)
                note = "段落 %d／%d" % (report["paragraphs_used"], report["paragraphs"])
            elif stage == "plan":
                plan = adapter.run_plan(number, None, "", force)
                note = "%d 頁" % len(plan["pages"])
            elif stage == "script":
                _, report = adapter.run_script(number, None, force)
                note = "台詞 %d 句" % report["dialogue_lines"]
            else:
                board = adapter.run_storyboard(number, None, force)
                note = "%d 頁" % len(board["pages"])
            if on_stage is not None:
                on_stage(stage, number, note)
    return {"stages": picked, "chapters": numbers, "skipped": skipped_stages,
            "warnings": list(adapter.warnings[:20]), "usage": dict(adapter.usage), "project": str(project.root)}


def comic_job(fe: Any, book_id: str, project_id: str, chapters: list[int], stages: list[str], *,
              config: Config | None = None, executor: str | None = None, force: bool = False) -> dict[str, Any]:
    """fekit 任務的 target：跑 LLM 階段；停止時丟 JobStopped（D6）。

    force=True 時覆蓋已存在的階段產出（一路傳到 adapt.Adapter.run_*）。
    """
    from vendor.t2c import llm as t2c_llm

    from .errors import JobStopped

    picked = check_stages(stages)
    numbers = [int(n) for n in (chapters or [])]
    state = {"seg": 0, "tokens": 0}

    def emit(ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind == "call":
            state["seg"] += 1
            label = "%s %s" % (STAGE_LABEL.get(ev.get("stage"), ev.get("stage")), ev.get("label") or "")
            fe.begin("m%d" % state["seg"], label)
            fe.set(phase="waiting", note=label)
        elif kind == "fix":
            fe.set(phase="repair")
            fe.say("回修第 %s 次：%s" % (ev.get("attempt") or 1, ev.get("message") or ""), "warn")

    def delta(text: str) -> None:
        if not state["seg"]:
            return
        fe.write("m%d" % state["seg"], text)
        fe.set(phase="streaming")

    def usage(use: dict[str, Any]) -> None:
        state["tokens"] += int(use.get("prompt_tokens", 0)) + int(use.get("completion_tokens", 0))

    def stage_done(stage: str, number: int, note: str) -> None:
        fe.item("ch%03d" % number, label="第 %d 章" % number,
                note="%s：%s" % (STAGE_LABEL[stage], note))

    try:
        result = run_stages(book_id, project_id, numbers, picked, config=config,
                            should_stop=lambda: bool(fe.stop_requested), on_event=emit, on_delta=delta,
                            on_usage=usage, on_stage=stage_done, executor=executor, force=force)
    except t2c_llm.Cancelled as error:
        raise JobStopped() from error
    if fe.stop_requested:
        raise JobStopped()
    for number in numbers:
        fe.item("ch%03d" % number, state="done", label="第 %d 章" % number,
                note="、".join(STAGE_LABEL[s] for s in result["stages"]))
    text = "完成 %s；%d 章" % ("、".join(STAGE_LABEL[s] for s in result["stages"]), len(numbers))
    fe.say(text)
    fe.say("呼叫 %d 次，約 %d token" % (result["usage"].get("calls", 0), state["tokens"]), "dim")
    return {"text": text, "stages": result["stages"], "chapters": numbers,
            "warnings": result["warnings"], "usage": result["usage"], "project": result["project"]}

# ---------------------------------------------------------------- 出圖（D5）


def render_chapter(book_id: str, project_id: str, number: int, *, preset: str | None = None,
                   force: bool = False, page: int | None = None, panel: str | None = None,
                   candidates: int | None = None, should_stop=None, on_event=None,
                   config: Config | None = None) -> dict[str, Any]:
    """送 ComfyUI 出圖；回傳這一章寫出的檔名。停止時丟 render.Cancelled（由任務層轉 stopped）。

    page／panel／candidates 只畫子集（第四段）：t2c 的 render.chapter_jobs 已經支援，
    不給就整章（跟以前一樣）。
    """
    from vendor.t2c import render

    project = open_project(book_id, project_id, config)
    cfg = comic_cfg(config, read_settings(book_id, project_id, config))
    try:
        jobs = render.chapter_jobs(project, number, page=page, panel=panel, candidates=candidates)
        written = render.render_jobs(project, number, jobs, cfg, preset_name=preset, force=force,
                                     should_stop=should_stop, on_event=on_event)
    except render.Cancelled:
        raise
    except _COMFY_ERRORS as error:
        raise ComicError(_explain_comfy_text(error)) from error
    return {"chapter": int(number), "cells": len(jobs), "written": list(written)}


def render_more(book_id: str, project_id: str, number: int, *, page: int | None = None, panel: str | None = None,
                more: int = 1, preset: str | None = None, should_stop=None, on_event=None,
                config: Config | None = None) -> dict[str, Any]:
    """「加畫 N 張」：每格再畫 N 個還沒用過的 seed（t2c 的 render.plan_more）。

    跟 render_chapter() 的差別：那個是「補到 N 張」（candidates＝總數），這個是「再加 N 張」；
    已經畫過的 seed 不會重畫，所以同一格按幾次就多幾張。
    """
    from vendor.t2c import comfy as t2c_comfy
    from vendor.t2c import render

    count = int(more)
    if count < 1:
        raise ComicError("加畫張數要大於 0（收到 %r）" % (more,))
    project = open_project(book_id, project_id, config)
    cfg = comic_cfg(config, read_settings(book_id, project_id, config))
    try:
        jobs = render.chapter_jobs(project, number, page=page, panel=panel)
        profile = render.resolve_profile(cfg, project, None)
        chosen, _preset, default_preset = render.resolve_preset(cfg, preset, profile)
        jobs = render.plan_more(project, number, jobs, count, chosen, default_preset,
                                None, t2c_comfy.default_profile_name(cfg))
        written = render.render_jobs(project, number, jobs, cfg, preset_name=preset, force=False,
                                     should_stop=should_stop, on_event=on_event)
    except render.Cancelled:
        raise
    except _COMFY_ERRORS as error:
        raise ComicError(_explain_comfy_text(error)) from error
    return {"chapter": int(number), "cells": len(jobs), "written": list(written)}


def delete_renders(book_id: str, project_id: str, number: int, names: Any,
                   config: Config | None = None) -> dict[str, Any]:
    """刪掉候選圖；已選中的那張不能刪（先改選別張）。檔名一律先全部驗過才動任何一個檔。"""
    from vendor.t2c import compose as t2c_compose
    from vendor.t2c import workspace as t2c_workspace

    from . import candidate_files

    config = _config(config)
    project = open_project(book_id, project_id, config)
    folder = project.out_dir("renders", t2c_compose.chapter_name(number))
    if not folder.is_dir():
        raise ComicError("這一章還沒有出圖，沒有候選圖可以刪：%s" % folder)
    status = t2c_workspace.panels_status(project, number)
    protect: list[str] = []
    for row in status.get("panels") or []:
        pick = row.get("pick")
        name = str(pick.get("image") or "") if isinstance(pick, dict) else str(pick or "")   # t2c 的 pick 是 {image, focus, zoom}
        if name:
            protect.append(name)
    try:
        report = candidate_files.delete_candidates(config.assert_writable, folder, names, protect)
    except ValueError as error:
        raise ComicError(str(error)) from error
    return {"chapter": int(number), **report,
            "remaining": sorted(path.name for path in folder.glob("*.png"))}


def _scope_text(page: int | None, panel: str | None) -> str:
    """出圖範圍的說明：只補某頁／某格時接在訊息尾巴，整章時是空字串。"""
    if panel:
        return "（只補 %s）" % panel
    if page:
        return "（只補第 %d 頁）" % page
    return ""


def comic_render_job(fe: Any, book_id: str, project_id: str, chapters: list[int], *,
                     preset: str | None = None, force: bool = False, page: int | None = None,
                     panel: str | None = None, candidates: int | None = None, more: int | None = None,
                     config: Config | None = None) -> dict[str, Any]:
    """fekit 任務的 target：跑出圖；render.Cancelled 與 llm.Cancelled 都轉 JobStopped（R7）。

    ``more`` 有值時走「加畫 N 張」（render_more），否則走「補到 candidates 張」（render_chapter）。
    """
    from vendor.t2c import llm as t2c_llm
    from vendor.t2c import render

    from .errors import JobStopped

    total = {"images": 0}

    def emit(ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind == "start":
            fe.set(progress=(int(ev.get("done") or 1) - 1, int(ev.get("total") or 1)),
                   phase="running", note="第 %s/%s 張" % (ev.get("done"), ev.get("total")))
        elif kind == "skip":
            fe.set(note="略過 %s（已有檔案）" % ev.get("file"))
        elif kind == "done":
            total["images"] += 1

    written: list[str] = []
    numbers = [int(n) for n in (chapters or [])]
    if not numbers:
        raise ComicError("至少要選一章")
    for number in numbers:
        if fe.stop_requested:
            raise JobStopped()
        fe.item("ch%03d" % number, state="running", label="第 %d 章" % number, note="出圖中")
        try:
            if more:
                out = render_more(book_id, project_id, number, preset=preset, page=page, panel=panel,
                                  more=more, should_stop=lambda: bool(fe.stop_requested), on_event=emit,
                                  config=config)
            else:
                out = render_chapter(book_id, project_id, number, preset=preset, force=force, page=page,
                                     panel=panel, candidates=candidates,
                                     should_stop=lambda: bool(fe.stop_requested), on_event=emit, config=config)
        except (render.Cancelled, t2c_llm.Cancelled) as error:
            raise JobStopped() from error
        written += out["written"]
        fe.item("ch%03d" % number, state="done", label="第 %d 章" % number,
                note="%d 格／%d 張" % (out["cells"], len(out["written"])))
    text = "%s：%d 張%s" % ("加畫完成" if more else "出圖完成", len(written), _scope_text(page, panel))
    fe.say(text)
    return {"text": text, "chapters": numbers, "written": written}

# ---------------------------------------------------------------- 端點的薄包裝（D8）


def _call(fn, *args, **kwargs):
    """呼叫 t2c 的服務函式，把它的錯誤類型轉成 ComicError（訊息原本就是給人看的）。"""
    from vendor.t2c import compile as t2c_compile
    from vendor.t2c import compose as t2c_compose
    from vendor.t2c import export as t2c_export
    from vendor.t2c import layout as t2c_layout
    from vendor.t2c import letter as t2c_letter
    from vendor.t2c import project as t2c_project
    from vendor.t2c import workspace as t2c_workspace

    try:
        return fn(*args, **kwargs)
    except (t2c_workspace.WorkspaceError, t2c_project.ProjectError, t2c_compile.CompileError,
            t2c_layout.LayoutError, t2c_export.ExportError, t2c_compose.ComposeError,
            t2c_letter.LetterError) as error:
        raise ComicError(str(error)) from error


def list_projects(book_id: str, config: Config | None = None) -> list[dict[str, Any]]:
    """這本書底下的漫畫專案（掃 comic/*/project.yaml）。"""
    from vendor.t2c import workspace as t2c_workspace

    root = comics_root(book_id, config)
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.iterdir()):
        if not (path / "project.yaml").is_file():
            continue
        try:
            project = t2c_workspace.open_project(path)
        except Exception:  # noqa: BLE001 — 壞掉的專案不該讓清單整頁失敗
            out.append({"project_id": path.name, "title": path.name, "broken": True})
            continue
        out.append({"project_id": path.name, "title": str(project.config.get("title") or path.name),
                    "preset": project.config.get("style_preset"), "broken": False})
    return out


def overview(book_id: str, project_id: str, config: Config | None = None) -> dict[str, Any]:
    """專案總覽（章節、聖經、設定）。"""
    from vendor.t2c import workspace as t2c_workspace

    project = open_project(book_id, project_id, config)
    data = _call(t2c_workspace.overview, project)
    data["project_id"] = project_id
    data["book_id"] = book_id
    return data


def chapter_status(book_id: str, project_id: str, number: int, config: Config | None = None) -> dict[str, Any]:
    """一章各階段有沒有檔案。"""
    from vendor.t2c import workspace as t2c_workspace

    project = open_project(book_id, project_id, config)
    return _call(t2c_workspace.chapter_status, project, number)


def save_source(book_id: str, project_id: str, number: int, text: str, config: Config | None = None) -> dict[str, Any]:
    """介面改過的原著正文寫回 source/chNNN.txt。"""
    from vendor.t2c import workspace as t2c_workspace

    project = open_project(book_id, project_id, config)
    path = _call(t2c_workspace.save_source, project, number, text)
    return {"chapter": int(number), "path": str(path)}


def storyboard(book_id: str, project_id: str, number: int, config: Config | None = None) -> dict[str, Any]:
    """讀分鏡。"""
    from vendor.t2c import workspace as t2c_workspace

    project = open_project(book_id, project_id, config)
    return {"storyboard": _call(t2c_workspace.read_storyboard, project, number)}


def compile_panel(book_id: str, project_id: str, number: int, panel_id: str, config: Config | None = None) -> dict[str, Any]:
    """編譯一格（回傳 prompt 與尺寸）。"""
    from vendor.t2c import workspace as t2c_workspace

    project = open_project(book_id, project_id, config)
    return _call(t2c_workspace.compile_one, project, number, panel_id)


def panels(book_id: str, project_id: str, number: int, config: Config | None = None) -> dict[str, Any]:
    """每一格的候選圖與選圖。"""
    from vendor.t2c import workspace as t2c_workspace

    project = open_project(book_id, project_id, config)
    return _call(t2c_workspace.panels_status, project, number)   # 它本身就是 {"panels": [...], "pages": ...}


def pick(book_id: str, project_id: str, number: int, panel_id: str, image: str | None = None,
         clear: bool = False, config: Config | None = None) -> dict[str, Any]:
    """選一張候選圖（clear=True 取消）。"""
    from vendor.t2c import workspace as t2c_workspace

    project = open_project(book_id, project_id, config)
    return _call(t2c_workspace.set_pick, project, number, panel_id, image, clear=clear)


def pages(book_id: str, project_id: str, number: int, config: Config | None = None) -> dict[str, Any]:
    """組版後的頁面圖（pages/<chapter>/*.png）。"""
    from vendor.t2c import compose as t2c_compose

    project = open_project(book_id, project_id, config)
    folder = t2c_compose.pages_dir(project, number)
    files = sorted(path.name for path in folder.glob("*.png")) if folder.is_dir() else []
    return {"chapter": int(number), "files": files}


def image_path(book_id: str, project_id: str, number: int, name: str, kind: str = "render",
               config: Config | None = None) -> Path:
    """候選圖／頁面圖的實體路徑。

    `name` 是 `panels()`、`pages()` 回傳的那個檔名（不含章節資料夾），所以這裡自己接上
    `renders/chNNN` 或 `pages/chNNN`；`assert_inside` 仍然擋路徑穿越。
    """
    root = project_dir(book_id, project_id, config)
    folder = root / ("pages" if str(kind or "").strip().lower() == "page" else "renders") / ("ch%03d" % int(number))
    target = assert_inside(folder, name)
    if not target.is_file():
        raise ComicError("找不到檔案：%s" % name)
    return target


def export_meta(project, number: int) -> dict[str, Any]:
    """CBZ 的 ComicInfo.xml 與 PDF 的 Info：書名、章號、章名。

    走 t2c 的 export.default_meta()（章名取自分鏡）。分鏡不在時退回「書名＋第 N 章」，
    因為 export_file() 只要求有頁面圖。
    """
    from vendor.t2c import export as t2c_export

    try:
        return t2c_export.default_meta(project, number)
    except Exception:  # noqa: BLE001 — 沒有分鏡也要出得了檔
        title = str(project.config.get("title") or "")
        return {"work": title, "chapter": int(number),
                "title": "　".join(part for part in (title, "第 %d 章" % int(number)) if part)}


def export_file(book_id: str, project_id: str, number: int, kind: str,
                config: Config | None = None) -> Path:
    """出 PDF、CBZ（t2c/export.py）或純單頁 PNG 壓縮檔（zip），回傳檔案路徑。"""
    import os
    import zipfile
    from vendor.t2c import compose as t2c_compose
    from vendor.t2c import export as t2c_export

    kind = str(kind or "").strip().lower()
    if kind not in ("pdf", "cbz", "zip"):
        raise ComicError("kind 只能是 pdf、cbz 或 zip：%r" % (kind,))
    project = open_project(book_id, project_id, config)
    folder = t2c_compose.pages_dir(project, number)
    files = sorted(folder.glob("*.png")) if folder.is_dir() else []
    if not files:
        raise ComicError("第 %d 章還沒有頁面圖；先把這一章組版（pages）" % int(number))
    out_dir = project.out_dir("out")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "%s_ch%03d" % (project_id, int(number))
    if kind == "zip":
        target = out_dir / (stem + "_png.zip")
        tmp = out_dir / (stem + "_png.zip.tmp")
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as archive:
            for path in files:
                archive.write(path, arcname=path.name)
        os.replace(tmp, target)
        return target
    meta = export_meta(project, number)
    if kind == "pdf":
        return Path(_call(t2c_export.build_pdf, files, out_dir / (stem + ".pdf"), meta))
    numbered = [(index, path) for index, path in enumerate(files, 1)]
    return _call(t2c_export.build_cbz, numbered, out_dir / (stem + ".cbz"), meta)

def compose_pages(book_id: str, project_id: str, number: int, *, config: Config | None = None) -> dict[str, Any]:
    """組版＋嵌字 → pages/<chapter>/pNNN.png。"""
    from vendor.t2c import compose as t2c_compose
    from vendor.t2c import letter as t2c_letter

    project = open_project(book_id, project_id, config)
    cfg = comic_cfg(config, read_settings(book_id, project_id, config))
    try:
        storyboard = project.load_storyboard(number)
        lettering = lettering_settings(cfg.get("lettering"))
        picks = t2c_compose.load_picks(project, number)
        letters = t2c_letter.load_letters(project, number)
        pages_data = project.pages()
    except Exception as error:  # noqa: BLE001 — 這幾個錯誤的訊息本來就是給人看的
        raise ComicError(str(error)) from error
    written, warnings = _call(t2c_compose.compose_chapter, project, number, storyboard, pages_data,
                              picks=picks, lettering=lettering, letters=letters)
    return {"chapter": int(number), "pages": [path.name for _, path in written], "warnings": list(warnings)}


# ---------------------------------------------------------------- 逐句嵌字（N5）


def _storyboard_panels(project, number: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """(分鏡, {panel id: panel})；沒有分鏡或 JSON 壞掉都轉成 ComicError。"""
    storyboard = _call(project.load_storyboard, number)
    panels = {}
    for page in storyboard.get("pages") or []:
        for panel in page.get("panels") or []:
            if not isinstance(panel, dict):
                continue
            panel_id = panel.get("id") or "p%03d_k%d" % (int(page.get("page") or 0), int(panel.get("slot") or 0))
            panels[panel_id] = panel
    return storyboard, panels


def read_letters(book_id: str, project_id: str, number: int,
                 config: Config | None = None) -> dict[str, Any]:
    """letters/chNNN.json 的內容；沒有檔案＝空 panels（不丟錯）。

    回傳的 panels 是 t2c `check_letters()` 整理過的形狀：每格都有 faces／bubbles／sfx。
    """
    from vendor.t2c import letter as t2c_letter

    config = _config(config)
    project = open_project(book_id, project_id, config)
    chapter = int(number)
    path = t2c_letter.letters_path(project, chapter)
    panels = _call(t2c_letter.load_letters, project, chapter)
    return {"chapter": chapter, "exists": path.is_file(), "panels": panels, "path": str(path)}


def save_letters(book_id: str, project_id: str, number: int, panels: Any,
                 config: Config | None = None) -> dict[str, Any]:
    """整份取代 letters/chNNN.json：先驗 panel、t2c 檢查、schema，再寫檔並回讀。

    `panels` 是 {panel id: {faces, bubbles, sfx}}；沒有微調的格不要帶。介面每次送完整的一份，
    所以沒帶到的格會從檔案消失（規格 5.11.1）。
    """
    from vendor.t2c import letter as t2c_letter

    config = _config(config)
    project = open_project(book_id, project_id, config)
    chapter = int(number)
    if not isinstance(panels, dict):
        raise ComicError("panels 要是 {panel id: {...}} 的物件：%r" % (type(panels).__name__,))
    _, known = _storyboard_panels(project, chapter)
    unknown = sorted(set(panels) - set(known))
    if unknown:
        raise ComicError("第 %d 章的分鏡沒有這些 panel：%s" % (chapter, "、".join(unknown)))
    version = t2c_letter.LETTERS_VERSION
    checked = _call(t2c_letter.check_letters, {"version": version, "panels": panels})
    issues = validate_data({"version": version, "chapter": chapter, "panels": checked}, "letters")
    if issues:
        issue = issues[0]
        raise ComicError("letters 格式不對：%s %s" % (issue.path or "/", issue.message))
    path = t2c_letter.letters_path(project, chapter)
    try:
        config.assert_writable(path)
    except ConfigError as error:
        raise ComicError("letters 只能寫在 works_dir 之內：%s" % error) from error
    _call(t2c_letter.save_letters, project, chapter, checked)
    return read_letters(book_id, project_id, chapter, config)


def letters_plan(book_id: str, project_id: str, number: int, page: int | None = None,
                 config: Config | None = None) -> dict[str, Any]:
    """plan_page() 算好的位置（座標是整頁像素）；page 省略＝整章。

    座標的用法：`rect` 是格子（裁切後）在整頁的位置，`bubbles[].center`／`tail` 是整頁像素；
    介面拖曳後要換回「相對格子」的 0–1 才能寫進 letters。
    """
    from vendor.t2c import compose as t2c_compose
    from vendor.t2c import letter as t2c_letter

    config = _config(config)
    project = open_project(book_id, project_id, config)
    chapter = int(number)
    storyboard, _ = _storyboard_panels(project, chapter)
    wanted = None if page is None else int(page)
    lettering = comic_cfg(config, read_settings(book_id, project_id, config)).get("lettering")
    settings = lettering_settings(lettering)
    pages_data = _call(project.pages)
    picks = _call(t2c_compose.load_picks, project, chapter)
    letters = _call(t2c_letter.load_letters, project, chapter)
    pages = []
    for item in storyboard.get("pages") or []:
        if wanted is not None and int(item.get("page") or 0) != wanted:
            continue
        pages.append(_call(t2c_letter.plan_page, project, chapter, item, pages_data, picks, letters, settings))
    if wanted is not None and not pages:
        raise ComicError("分鏡裡沒有第 %d 頁" % wanted)
    return {"chapter": chapter, "page": wanted, "pages": pages}
