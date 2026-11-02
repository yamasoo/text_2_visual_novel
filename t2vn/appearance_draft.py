"""AI 草擬外型卡 v2：兩個提示詞的提示詞組裝與回覆解析（S6）。

「AI 草擬」那一步的兩個純函式：把角色資料、小說原文證據、可用的 wildcard 摘要組成
給 AI 的英文提示詞；解析 AI 的 JSON 回覆成兩個提示詞（頸部以上放頭像、頸部以下放
立繪）。不呼叫 AI（呼叫端做）；表情由使用者勾選，不在這裡處理。
"""
from __future__ import annotations

import json
import re
from typing import Any

PROMPT_FIELDS = ("headshot_prompt", "sprite_prompt")
TOKEN = re.compile(r"__([a-z0-9_]+?)__")
FIELD_HELP = {
    "headshot_prompt": (
        "everything visible from the neck up, as comma-separated Danbooru-style tags: hairstyle, hair color, "
        "eye shape, eye color, hair accessories, earrings, necklace, special features such as a mole or scar. "
        "Do NOT include facial expression, clothing, body or pose"
    ),
    "sprite_prompt": (
        "everything below the neck, as comma-separated tags: clothing (follow the faction or occupation uniform "
        "if the story gives one), body type, accessories carried. Always a neutral standing pose with arms relaxed at the sides: "
        "do NOT write sitting, crouching, kneeling, leaning, or any action from the story. "
        "Do NOT repeat anything from headshot_prompt"
    ),
}
INTRO = (
    "You are drafting an appearance card for an anime-style visual novel character, "
    "to be turned into image prompts."
)
CHARACTER_KEYS = ("name", "summary", "appearance")
EVIDENCE_LINE = (
    "Base the card on these passages wherever they describe the character's looks, and do not "
    "contradict them. For anything they do not mention, follow the fallback rule below."
)
NO_EVIDENCE_LINE = (
    "There are no passages describing this character's looks; follow the fallback rule below."
)
FALLBACK_LINE = (
    "Fallback rule: never return both prompts empty. When the passages and character data say "
    "nothing about looks (common for minor characters), infer a plausible, plain, generic anime "
    "appearance from the character's name (gender, age and cultural setting cues), occupation, "
    "role and faction. Examples: a dockworker -> short hair, work clothes, gloves; a patrol guard "
    "-> uniform, cap; an old fisherman -> gray hair, weathered face, simple coat. Do not invent "
    "scars, tattoos or other distinctive marks. Keep headshot_prompt to hair, eyes and head "
    "features only, and sprite_prompt to clothing and build only."
)
DRAFT_TITLE = "Current draft (revise it, keep what is still correct):"
WILDCARD_TITLE = (
    "Wildcard lists you may reference (write a value as __name__ so the user can roll "
    "it later; use only these names):"
)
SPACES = re.compile(r"\s+")
COMMAS = re.compile(r"\s*,\s*")
FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
WILDCARD_WARNING = "不在清單內的 wildcard"

REPLY_FORMAT = (
    'Return ONLY one JSON object, no commentary: {"headshot_prompt": "", '
    '"sprite_prompt": ""}. All values are strings.'
)


def build_prompt(
    character: dict[str, Any] | None,
    evidence: list[str],
    wildcards: list[dict[str, Any]] | None = None,
    current: dict[str, Any] | None = None,
) -> str:
    """組給 AI 的英文提示詞（角色資料、證據、wildcard 摘要、目前草稿）。

    各段依序：任務說明與回覆格式、兩個欄位說明、wildcard 清單（有才出現）、
    ``Character:`` 區塊、內文證據（沒有證據時是一行說明）、目前草稿（有才出現）。
    """
    lines = [INTRO, REPLY_FORMAT, "Fields:"]
    for field in PROMPT_FIELDS:
        lines.append(f"- {field}: {FIELD_HELP[field]}")

    if wildcards:
        lines.append(WILDCARD_TITLE)
        for item in wildcards:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            count = item.get("count", 0)
            sample = "; ".join(str(value) for value in (item.get("sample") or []))
            lines.append(f"- __{name}__ ({count} options) e.g. {sample}")

    lines.append("Character:")
    if isinstance(character, dict):
        for key in CHARACTER_KEYS:
            value = character.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(f"{key}: {value.strip()}")

    if evidence:
        lines.append("Passages from the novel:")
        for index, passage in enumerate(evidence, start=1):
            lines.append(f"{index}. {passage}")
        lines.append(EVIDENCE_LINE)
    else:
        lines.append(NO_EVIDENCE_LINE)
    lines.append(FALLBACK_LINE)

    if isinstance(current, dict) and current:
        lines.append(DRAFT_TITLE)
        lines.append(json.dumps(current, ensure_ascii=False))

    return "\n".join(lines)


def _tidy(text: str) -> str:
    """把逗號前後的空白整理成 ``", "``、壓縮空白、去掉空段與頭尾逗號。"""
    text = COMMAS.sub(", ", SPACES.sub(" ", text))
    parts = [part.strip() for part in text.split(",")]
    return ", ".join(part for part in parts if part)


def _extract_json(text: str) -> Any:
    """先找 ```json 圍欄裡的 JSON，找不到再取第一個 ``{`` 到最後一個 ``}``。"""
    match = FENCED.search(text)
    if match is not None:
        try:
            return json.loads(match.group(1))
        except ValueError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回覆裡找不到 JSON 物件")
    try:
        return json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ValueError(f"回覆裡的 JSON 不合法：{exc}") from exc


def _clean_value(
    value: Any, field: str, allowed: set[str], warnings: list[str]
) -> str:
    """單一欄位：轉字串、strip，移除不在清單內的 wildcard 並整理。"""
    if value is None:
        return ""
    text = value.strip() if isinstance(value, str) else str(value).strip()
    if "__" not in text:
        return text

    removed = [False]

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in allowed:
            return match.group(0)
        removed[0] = True
        warnings.append(f"{field}: {WILDCARD_WARNING}：{name}")
        return ""

    cleaned = TOKEN.sub(replace, text)
    return _tidy(cleaned) if removed[0] else cleaned


def parse_reply(
    text: str, allowed_wildcards: list[str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """解析 AI 的回覆，回傳 ``({"headshot_prompt": ..., "sprite_prompt": ...}, warnings)``。

    容許前後有說明文字或 ``````json`` 圍欄；缺漏或 ``None`` 的欄位補 ``""``，非字串轉
    ``str``、strip，未知欄位丟掉；值中的 ``__名稱__`` 不在 ``allowed_wildcards`` 時移除
    並警告。找不到 JSON、根不是 dict、兩個欄位都不存在 → ``ValueError``。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("回覆必須是非空字串，而且裡面要有 JSON 物件")

    data = _extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("回覆的 JSON 根必須是物件")
    if not any(field in data for field in PROMPT_FIELDS):
        raise ValueError("回覆缺少 headshot_prompt 或 sprite_prompt")

    allowed = set(allowed_wildcards or [])
    warnings: list[str] = []
    result = {
        field: _clean_value(data.get(field), field, allowed, warnings)
        for field in PROMPT_FIELDS
    }
    return result, warnings
