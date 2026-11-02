"""外型卡 v2：兩個提示詞、頭像表情、擲骰固定與標籤（S6）。

每個角色有一張「外型卡」（``works/<book>/art/appearance/<char_id>.json``），v2 把外觀
簡化成**兩個提示詞框**：

- ``headshot_prompt``：頸部以上（髮型、髮色、眼型、眼色、頭飾、耳環、項鍊、特殊特徵），
  不含表情。
- ``sprite_prompt``：頸部以下（衣服、身體、動作）。

再加上要出哪些頭像表情（``expressions``）；出圖時頭像用「頭像提示詞＋表情」，立繪用
「頭像提示詞＋立繪提示詞」（臉才會和頭像一致）。提示詞可以引用使用者的 wildcard
（``__名稱__``，「擲骰」會把它固定成具體文字）。

這個模組只做純函式：整理／讀寫外型卡、展開固定 wildcard（展開函式由呼叫端傳入）、
轉成標籤。不讀 wildcard 檔、不呼叫 AI、不組完整出圖提示詞。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

EXPRESSIONS = ("neutral", "smile", "angry", "sad", "serious", "worried", "surprised")
DEFAULT_EXPRESSIONS = ("neutral",)
EXPRESSION_TAGS = {
    "neutral": "neutral expression, closed mouth",
    "smile": "smile, happy",
    "angry": "angry, furrowed brow",
    "sad": "sad, downcast eyes",
    "serious": "serious, determined",
    "worried": "worried, anxious",
    "surprised": "surprised, wide eyes, open mouth",
}
PROMPT_FIELDS = ("headshot_prompt", "sprite_prompt")
CHAR_ID = re.compile(r"^[a-z][a-z0-9_]*$")
SCORE = re.compile(r"^score_[0-9]+$")

WILDCARD_MARKER = "__"
TOKEN = re.compile(r"__([a-z0-9_]+?)__")
SPLIT_PATTERN = re.compile(r"[,\n]")
WHITESPACE_PATTERN = re.compile(r"\s+")
SEPARATOR = ", "
UTF8 = "utf-8"
DEFAULT_EXPRESSION_LIST = ["neutral"]


# ------------------------------------------------------------------------ 標籤


def normalize_tags(text: Any) -> str:
    """整理標籤字串：逗號或換行分隔、小寫、``_`` 換空白（``score_數字`` 例外）、去重。"""
    if not isinstance(text, str) or not text.strip():
        return ""
    seen: set[str] = set()
    tags: list[str] = []
    for raw in SPLIT_PATTERN.split(text):
        tag = WHITESPACE_PATTERN.sub(" ", raw).strip().lower()
        if not tag:
            continue
        if SCORE.match(tag) is None:
            tag = WHITESPACE_PATTERN.sub(" ", tag.replace("_", " ")).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return SEPARATOR.join(tags)


def _clean_field(value: Any) -> str:
    """文字欄位 → 字串：``None`` 當空字串，其餘 ``str().strip()``。"""
    if value is None:
        return ""
    return str(value).strip()


def _clean_tags(text: Any) -> str:
    """去掉含 ``__`` 的片段（沒擲骰的 wildcard 不能送給模型）後再正規化。"""
    if not isinstance(text, str) or not text.strip():
        return ""
    kept = [
        segment for segment in SPLIT_PATTERN.split(text) if WILDCARD_MARKER not in segment
    ]
    return normalize_tags(SEPARATOR.join(kept))


def _normalize_expressions(value: Any) -> list[str]:
    """表情清單：只留認得的、去重、依 :data:`EXPRESSIONS` 的順序；空的話回預設。"""
    if not isinstance(value, (list, tuple)):
        return list(DEFAULT_EXPRESSION_LIST)
    chosen = {item for item in value if isinstance(item, str) and item in EXPRESSIONS}
    ordered = [expr for expr in EXPRESSIONS if expr in chosen]
    return ordered or list(DEFAULT_EXPRESSION_LIST)


# -------------------------------------------------------------------- 外型卡


def new_card(char_id: str) -> dict[str, Any]:
    """回傳一張空的外型卡；``char_id`` 不合法 → ``ValueError``。"""
    if not isinstance(char_id, str) or CHAR_ID.match(char_id) is None:
        raise ValueError(f"char_id 不合法：{char_id!r}")
    return {
        "char_id": char_id,
        "confirmed": False,
        "headshot_prompt": "",
        "sprite_prompt": "",
        "expressions": list(DEFAULT_EXPRESSION_LIST),
        "notes": "",
    }


def normalize_card(data: Any, char_id: str | None = None) -> dict[str, Any]:
    """把任意 dict 整理成 v2 外型卡：補預設、丟未知鍵（含舊版 ``headshot``／``sprite``）。

    ``char_id`` 參數（不是 ``None`` 時）優先於 ``data["char_id"]``；不合法 →
    ``ValueError``。``data`` 不是 dict → ``ValueError``。回傳新 dict，不改傳入物件。
    """
    if not isinstance(data, dict):
        raise ValueError(f"外型卡必須是 dict：{data!r}")
    raw_id = char_id if char_id is not None else data.get("char_id")
    if not isinstance(raw_id, str) or CHAR_ID.match(raw_id) is None:
        raise ValueError(f"char_id 不合法：{raw_id!r}")

    return {
        "char_id": raw_id,
        "confirmed": bool(data.get("confirmed", False)),
        "headshot_prompt": _clean_field(data.get("headshot_prompt")),
        "sprite_prompt": _clean_field(data.get("sprite_prompt")),
        "expressions": _normalize_expressions(data.get("expressions")),
        "notes": _clean_field(data.get("notes")),
    }


def load_card(path: str | Path) -> dict[str, Any]:
    """讀外型卡 JSON；``char_id`` 用檔名主檔名（檔案不存在讓 ``FileNotFoundError`` 丟出）。"""
    target = Path(path)
    data = json.loads(target.read_text(encoding=UTF8))
    return normalize_card(data, target.stem)


def save_card(card: dict[str, Any], path: str | Path) -> None:
    """整理後寫出外型卡；``char_id`` 與檔名主檔名不同 → ``ValueError``。"""
    target = Path(path)
    normalized = normalize_card(card)
    if normalized["char_id"] != target.stem:
        raise ValueError(
            f"char_id 與檔名不一致：{normalized['char_id']!r} != {target.stem!r}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding=UTF8
    )


def resolve_card(
    card: dict[str, Any], expand: Any, seed: int | str
) -> tuple[dict[str, Any], list[str]]:
    """把兩個提示詞裡的 ``__名稱__`` 展開固定，回傳 ``(新卡, warnings)``。

    ``expand(text, seed) -> (text, warnings)`` 由呼叫端提供；只對含 ``__`` 的欄位呼叫，
    種子用 ``f"{seed}:{field}"``（每個欄位固定不同結果）。其他欄位原樣保留。
    """
    resolved = normalize_card(card)
    warnings_list: list[str] = []
    for field in PROMPT_FIELDS:
        value = resolved[field]
        if WILDCARD_MARKER not in value:
            continue
        text, messages = expand(value, f"{seed}:{field}")
        resolved[field] = _clean_field(text)
        for message in messages:
            warnings_list.append(f"{field}: {message}")
    return resolved, warnings_list


# ---------------------------------------------------------------- 提示詞標籤


def expression_tags(expr: str) -> str:
    """表情 → 標籤；不認得 → ``ValueError``。"""
    if expr not in EXPRESSION_TAGS:
        raise ValueError(
            f"不認得的表情：{expr!r}（可用：{', '.join(EXPRESSIONS)}）"
        )
    return EXPRESSION_TAGS[expr]


def headshot_tags(card: dict[str, Any]) -> str:
    """頭像標籤：頭像提示詞（不含表情）。"""
    return _clean_tags(normalize_card(card)["headshot_prompt"])


def sprite_tags(card: dict[str, Any]) -> str:
    """立繪標籤：頭像提示詞 ＋ 立繪提示詞（頭像在前，重複的標籤去重）。"""
    normalized = normalize_card(card)
    joined = SEPARATOR.join(
        [normalized["headshot_prompt"], normalized["sprite_prompt"]]
    )
    return _clean_tags(joined)


def unresolved_wildcards(card: dict[str, Any]) -> list[str]:
    """兩個提示詞裡還沒擲骰的 ``__名稱__``（先頭像後立繪、去重、保留順序）。"""
    normalized = normalize_card(card)
    names: list[str] = []
    for field in PROMPT_FIELDS:
        for name in TOKEN.findall(normalized[field]):
            if name not in names:
                names.append(name)
    return names
