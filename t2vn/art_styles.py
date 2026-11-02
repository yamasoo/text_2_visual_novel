"""畫風表匯入與套用（S6）。

使用者有一份 84 款畫風的 xlsx（名稱三語、標籤、英文描述、負面標籤、分類）。這個模組
把它轉成 JSON 目錄（每款有穩定的 ASCII ``style_id``），並提供「把一款畫風套用到本書
art 設定的 ``extra``／``negative``」的純函式：不重複堆疊、可換款、不弄丟使用者自己
寫的標籤。UI 與出圖由別的模組接。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import openpyxl

CATEGORY_LABELS: dict[str, str] = {
    "art_form": "藝術形式",
    "interior": "內文插圖風格",
    "cover": "封面藝術風格",
}
REQUIRED_COLUMNS: tuple[str, ...] = (
    "name",
    "prompt",
    "naturl_prompt",
    "negative_prompt",
    "category",
    "namezh",
    "namejp",
    "nameen",
)
UNSUITABLE_WORDS: tuple[str, ...] = (
    "cover",
    "poster",
    "typography",
    "title",
    "border",
    "frame",
)

CATEGORY_OTHER = "other"
STYLE_ID_PREFIX = "st_"
STYLE_ID_LENGTH = 8
NAME_KEYS = ("zh-TW", "ja", "en")
ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\ufeff"
SPLIT_PATTERN = re.compile(r"[,\n]")
SEPARATOR = ", "
UTF8 = "utf-8"

_LABEL_TO_CATEGORY = {label: key for key, label in CATEGORY_LABELS.items()}


# ------------------------------------------------------------------------ 標籤


def split_tags(text: str) -> list[str]:
    """把標籤字串拆成清單：逗號或換行分隔、壓縮空白、小寫、去重（保留順序）。

    空段略過；**不**把底線換成空白。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    seen: set[str] = set()
    tags: list[str] = []
    for raw in SPLIT_PATTERN.split(text):
        tag = " ".join(raw.split()).lower()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tags


def _join_tags(tags: Iterable[str]) -> str:
    """用 ``", "`` 把標籤接回字串。"""
    return SEPARATOR.join(tags)


# ------------------------------------------------------------------ 讀 xlsx


def _clean_cell(value: Any) -> str:
    """儲存格 → 字串：``str()``、去掉零寬字元、去頭尾空白；``None`` 當空字串。"""
    if value is None:
        return ""
    text = str(value)
    for char in ZERO_WIDTH_CHARS:
        text = text.replace(char, "")
    return text.strip()


def _style_id_for(display_name: str, seen: dict[str, int]) -> str:
    """``st_`` ＋ sha1 前 8 碼；同一個 id 第 2 次起加 ``_2``、``_3``…"""
    digest = hashlib.sha1(display_name.encode(UTF8)).hexdigest()[:STYLE_ID_LENGTH]
    base = STYLE_ID_PREFIX + digest
    count = seen.get(base, 0) + 1
    seen[base] = count
    return base if count == 1 else f"{base}_{count}"


def _build_style(
    values: dict[str, str], prompt_tags: list[str], seen: dict[str, int]
) -> dict[str, Any]:
    """把一列的欄位組成目錄項目。"""
    display_name = values["namezh"] or values["name"]
    category_label = values["category"]
    return {
        "style_id": _style_id_for(display_name, seen),
        "category": _LABEL_TO_CATEGORY.get(category_label, CATEGORY_OTHER),
        "category_label": category_label,
        "names": {
            "zh-TW": display_name,
            "ja": values["namejp"],
            "en": values["nameen"],
        },
        "tags": _join_tags(prompt_tags),
        "description": values["naturl_prompt"],
        "negative": _join_tags(split_tags(values["negative_prompt"])),
    }


def parse_styles_xlsx(path: str | Path) -> list[dict[str, Any]]:
    """讀畫風 xlsx 的第一張工作表，回傳畫風目錄。

    第一列是欄名（去空白、轉小寫比對，欄位順序不固定）；缺任何
    :data:`REQUIRED_COLUMNS` → ``ValueError``（訊息含「缺少欄位」）。``prompt`` 為空的
    列略過；沒有可用資料列 → ``ValueError``。
    """
    workbook = openpyxl.load_workbook(
        filename=str(path), read_only=True, data_only=True
    )
    try:
        sheet = workbook.worksheets[0]
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            raise ValueError("xlsx 是空的，沒有欄名列")

        columns: dict[str, int] = {}
        for index, value in enumerate(header):
            name = _clean_cell(value).lower()
            if name and name not in columns:
                columns[name] = index
        missing = [name for name in REQUIRED_COLUMNS if name not in columns]
        if missing:
            raise ValueError("缺少欄位：" + ", ".join(missing))

        seen: dict[str, int] = {}
        catalog: list[dict[str, Any]] = []
        for row in rows:
            values: dict[str, str] = {}
            for name in REQUIRED_COLUMNS:
                index = columns[name]
                values[name] = _clean_cell(row[index]) if index < len(row) else ""
            prompt_tags = split_tags(values["prompt"])
            if not prompt_tags:
                continue
            catalog.append(_build_style(values, prompt_tags, seen))

        if not catalog:
            raise ValueError("xlsx 沒有任何可用資料列（prompt 全空）")
        return catalog
    finally:
        workbook.close()


# ---------------------------------------------------------------- 目錄存取


def save_catalog(catalog: list[dict[str, Any]], path: str | Path) -> None:
    """把畫風目錄寫成 JSON（``ensure_ascii=False``、``indent=2``、結尾換行）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding=UTF8
    )


def load_catalog(path: str | Path) -> list[dict[str, Any]]:
    """讀畫風目錄 JSON；不是 list → ``ValueError``（檔案不存在讓 ``FileNotFoundError`` 丟出）。"""
    data = json.loads(Path(path).read_text(encoding=UTF8))
    if not isinstance(data, list):
        raise ValueError(f"{Path(path).name} 的最外層必須是 list")
    return data


def filter_styles(
    catalog: list[dict[str, Any]],
    category: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """篩選畫風目錄：``category`` 比對分類；``query`` 在名稱與標籤裡找子字串（不分大小寫）。"""
    needle = query.lower() if isinstance(query, str) else None
    selected: list[dict[str, Any]] = []
    for style in catalog or []:
        if not isinstance(style, dict):
            continue
        if category is not None and style.get("category") != category:
            continue
        if needle is not None:
            names = style.get("names")
            name_text = " ".join(
                value for value in names.values() if isinstance(value, str)
            ) if isinstance(names, dict) else ""
            haystack = f"{name_text} {style.get('tags', '')}".lower()
            if needle not in haystack:
                continue
        selected.append(style)
    return selected


def get_style(catalog: list[dict[str, Any]], style_id: str) -> dict[str, Any]:
    """取單款畫風；找不到 → ``KeyError(style_id)``。"""
    for style in catalog or []:
        if isinstance(style, dict) and style.get("style_id") == style_id:
            return style
    raise KeyError(style_id)


# ---------------------------------------------------------------- 套用畫風


def clear_style(art: dict[str, Any] | None) -> dict[str, Any]:
    """回傳新的 art：把先前套用的畫風標籤拿掉、刪掉 ``style_applied``。

    只拿掉當初記在 ``style_applied`` 裡的標籤，使用者自己寫的標籤不受影響；拿完變空
    字串就刪掉該鍵。沒套用過就原樣回傳深拷貝。
    """
    result: dict[str, Any] = copy.deepcopy(art) if isinstance(art, dict) else {}
    applied = result.get("style_applied")
    if not isinstance(applied, dict):
        result.pop("style_applied", None)
        return result

    for field in ("extra", "negative"):
        recorded = applied.get(f"{field}_tags")
        tags = recorded if isinstance(recorded, list) else []
        to_remove = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
        if not to_remove:
            continue
        current = split_tags(result.get(field, ""))
        remaining = [tag for tag in current if tag not in to_remove]
        if remaining:
            result[field] = _join_tags(remaining)
        else:
            result.pop(field, None)

    result.pop("style_applied", None)
    return result


def _is_unsuitable(tag: str) -> bool:
    """標籤是否含不適合內文插圖的字（子字串比對）。"""
    return any(word in tag for word in UNSUITABLE_WORDS)


def apply_style(
    art: dict[str, Any] | None, style: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """把一款畫風套用到 art，回傳 ``(新 art, 被丟掉的標籤)``。

    先 :func:`clear_style` 換掉先前套用的畫風；含 :data:`UNSUITABLE_WORDS` 的標籤不套用
    （列進第二個回傳值）；已在 ``extra``／``negative`` 裡的標籤不重複加。新 ``extra``
    一定會寫入；新 ``negative`` 只在原本有或有新增時才寫入。
    """
    if not isinstance(style, dict):
        raise ValueError(f"畫風必須是 dict：{style!r}")
    result = clear_style(art)

    style_tags = split_tags(style.get("tags", ""))
    dropped = [tag for tag in style_tags if _is_unsuitable(tag)]
    keep = [tag for tag in style_tags if not _is_unsuitable(tag)]

    current_extra = split_tags(result.get("extra", ""))
    current_negative = split_tags(result.get("negative", ""))
    add_extra = [tag for tag in keep if tag not in current_extra]
    add_negative = [
        tag for tag in split_tags(style.get("negative", "")) if tag not in current_negative
    ]

    result["extra"] = _join_tags(current_extra + add_extra)
    merged_negative = current_negative + add_negative
    if merged_negative:
        result["negative"] = _join_tags(merged_negative)
    else:
        result.pop("negative", None)

    result["style_applied"] = {
        "style_id": style.get("style_id", ""),
        "extra_tags": add_extra,
        "negative_tags": add_negative,
    }
    return result, dropped
