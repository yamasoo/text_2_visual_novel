"""從小說內文找外觀描寫，組 AI 提示詞並解析回覆（S6）。

設定集常常沒有外觀資料（尤其是路人與地點），所以從章節段落裡找出「有描寫」的證據，
請 AI 草擬 Danbooru 標籤與英文描述，再由使用者確認。這個模組只有三個純函式：
:func:`find_evidence`、:func:`build_prompt`、:func:`parse_reply`；不呼叫 AI、不連網、
不讀寫檔案。
"""
from __future__ import annotations

import json
from typing import Any

CHARACTER_KEYWORDS = (
    "髮",
    "頭髮",
    "眼",
    "瞳",
    "臉",
    "膚",
    "身高",
    "高大",
    "矮",
    "瘦",
    "壯",
    "穿",
    "衣",
    "袍",
    "外套",
    "圍裙",
    "褲",
    "鞋",
    "帽",
    "手套",
    "鬍",
    "疤",
    "歲",
    "年輕",
    "年老",
    "蒼老",
    "圍巾",
    "戴著",
)
LOCATION_KEYWORDS = (
    "牆",
    "地板",
    "屋頂",
    "棚",
    "窗",
    "燈",
    "門",
    "梁",
    "柱",
    "鏽",
    "鐵皮",
    "石",
    "木",
    "水",
    "海",
    "天空",
    "霧",
    "光",
    "堆",
    "排",
    "架",
    "塢",
    "倉",
    "艙",
    "味",
)
SENTENCE_END = "。！？"

KINDS = ("character", "location")
DEFAULT_LIMIT = 6
DEFAULT_MAX_CHARS = 300
KEYWORD_WEIGHT = 3
CJK_START = 0x3400
CJK_END = 0x9FFF
REQUIRED_FIELDS = ("tags", "description")
INTRO_LINE = (
    "You write Anima prompts from a Chinese novel. Anima is an anime-style text-to-image "
    "model that takes Danbooru tags and an English sentence or two."
)
INSTRUCTION_LINE = (
    "Using the material below, write `tags` (English, lowercase, comma-separated Danbooru "
    "tags; underscores replaced by spaces) and `description` (2-3 English sentences) for "
    "{subject}."
)
CHARACTER_LINE = (
    "Describe appearance only: do not describe personality, backstory or plot."
)
LOCATION_LINE = (
    "Describe only the scenery and atmosphere that can be seen; no people appear in the image."
)
GROUNDING_LINE = (
    "Only state what the source supports. If something is missing, do not invent it; say "
    "what is missing in `notes`."
)
REPLY_FORMAT = (
    "Reply with a single JSON object and nothing else:\n"
    '{"tags": "...", "description": "...", "notes": "..."}'
)
EVIDENCE_TITLE = "Passages from the novel:"
CARD_TITLE = "Setting card:"
DRAFT_TITLE = "Current draft (revise it, keep what is still correct):"


# ------------------------------------------------------------------ 找證據


def _clip_text(text: str, max_chars: int) -> str:
    """空白縮成一格並截到 ``max_chars``；斷在句尾或補省略號。"""
    normalized = " ".join(text.split())
    if max_chars <= 0:
        return "…" if normalized else ""
    if len(normalized) <= max_chars:
        return normalized

    prefix = normalized[:max_chars]
    last_end = max(prefix.rfind(char) for char in SENTENCE_END)
    if last_end >= max_chars // KEYWORD_WEIGHT:
        return normalized[: last_end + 1]
    return prefix + "…"


def find_evidence(
    chapter_docs: list[dict[str, Any]],
    names: list[str],
    keywords: tuple[str, ...],
    limit: int = DEFAULT_LIMIT,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """在章節段落裡找提到 ``names`` 任一個名字的段落，挑出最有用的 ``limit`` 段。

    分數＝（命中的不同關鍵字數 × 3）＋（名字出現次數）；沒提到名字的段落不算；同分時
    較早的章與段落優先。回傳的每一項是 ``{chapter_id, para_no, text, score}``，
    ``text`` 空白縮成一格並截到 ``max_chars``（截斷範圍內最後一個 ``。！？`` 的位置
    ≥ ``max_chars // 3`` 就斷在它之後，否則直接截斷並補 ``…``）。結果依閱讀順序回傳。
    """
    valid_names = list(
        dict.fromkeys(
            name for name in (names or []) if isinstance(name, str) and name
        )
    )
    if not valid_names or limit <= 0:
        return []
    keyword_list = list(
        dict.fromkeys(
            word for word in (keywords or ()) if isinstance(word, str) and word
        )
    )

    candidates: list[dict[str, Any]] = []
    for doc in chapter_docs or []:
        if not isinstance(doc, dict):
            continue
        chapter_id = doc.get("chapter_id")
        chapter_id = chapter_id if isinstance(chapter_id, str) else ""
        paragraphs = doc.get("paragraphs")
        if not isinstance(paragraphs, list):
            continue
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict):
                continue
            text = paragraph.get("text")
            if not isinstance(text, str) or not text:
                continue
            hits = sum(text.count(name) for name in valid_names)
            if hits <= 0:
                continue
            para_no = paragraph.get("para_no")
            if isinstance(para_no, bool) or not isinstance(para_no, int):
                para_no = 0
            keyword_hits = sum(1 for word in keyword_list if word in text)
            candidates.append(
                {
                    "chapter_id": chapter_id,
                    "para_no": para_no,
                    "text": text,
                    "score": keyword_hits * KEYWORD_WEIGHT + hits,
                }
            )

    ranked = sorted(
        candidates,
        key=lambda item: (-item["score"], item["chapter_id"], item["para_no"]),
    )[:limit]
    ranked.sort(key=lambda item: (item["chapter_id"], item["para_no"]))
    for item in ranked:
        item["text"] = _clip_text(item["text"], max_chars)
    return ranked


# ------------------------------------------------------------------ 組提示詞


def _has_cjk(text: str) -> bool:
    """有沒有 CJK 字元（U+3400–U+9FFF）。"""
    return any(CJK_START <= ord(char) <= CJK_END for char in text)


def _current_section(current: dict[str, str] | None) -> list[str]:
    """「目前草稿」段落（tags／description 都空就不出現）。"""
    if not isinstance(current, dict):
        return []
    tags = current.get("tags")
    description = current.get("description")
    tags = tags if isinstance(tags, str) else ""
    description = description if isinstance(description, str) else ""
    if not tags.strip() and not description.strip():
        return []
    return [DRAFT_TITLE, f"tags: {tags.strip()}", f"description: {description.strip()}"]


def build_prompt(
    kind: str,
    subject: str,
    card_text: str,
    evidence: list[dict[str, Any]],
    current: dict[str, str] | None = None,
) -> str:
    """組給 AI 的提示詞（英文指示＋中文原文）；``kind`` 為 character 或 location。

    內容依序：任務說明、回覆格式、``Subject:``、設定卡（有才出現）、內文證據（有才
    出現，每筆一行 ``[ch001 ¶3] 原文``）、目前草稿（有才出現）。
    """
    if kind not in KINDS:
        raise ValueError(f"kind 必須是 {' 或 '.join(KINDS)}：{kind!r}")

    instruction = INSTRUCTION_LINE.format(subject=subject)
    if kind == "character":
        task = [INTRO_LINE, instruction, CHARACTER_LINE, GROUNDING_LINE]
    else:
        task = [INTRO_LINE, instruction, LOCATION_LINE, GROUNDING_LINE]

    sections: list[str] = [
        "\n".join(task),
        REPLY_FORMAT,
        f"Subject: {subject}",
    ]

    card = card_text.strip() if isinstance(card_text, str) else ""
    if card:
        sections.append("\n".join([CARD_TITLE, card]))

    if evidence:
        lines = [EVIDENCE_TITLE]
        for item in evidence:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text:
                continue
            chapter_id = item.get("chapter_id", "")
            para_no = item.get("para_no", "")
            lines.append(f"[{chapter_id} ¶{para_no}] {text}")
        if len(lines) > 1:
            sections.append("\n".join(lines))

    draft = _current_section(current)
    if draft:
        sections.append("\n".join(draft))

    return "\n\n".join(sections)


# ------------------------------------------------------------------ 解析回覆


def parse_reply(text: str) -> dict[str, str]:
    """解析 AI 的回覆，回傳 ``{"tags", "description", "notes"}``。

    取第一個 ``{`` 到最後一個 ``}`` 之間當 JSON（容許前後有說明文字或 `````json``
    圍欄）。找不到 JSON、不是合法 JSON、不是物件、缺 ``tags``／``description``、值不是
    字串、``tags``／``description`` 是空的或含中文、``notes`` 不是字串 → ``ValueError``。
    多餘的鍵忽略。
    """
    if not isinstance(text, str):
        raise ValueError("回覆必須是字串")

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回覆裡找不到 JSON 物件")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ValueError(f"回覆裡的 JSON 不合法：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("回覆裡的 JSON 必須是物件")

    result: dict[str, str] = {}
    for field in REQUIRED_FIELDS:
        if field not in data:
            raise ValueError(f"回覆缺少 {field}")
        value = data[field]
        if not isinstance(value, str):
            raise ValueError(f"{field} 必須是字串")
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{field} 不可為空")
        if _has_cjk(stripped):
            raise ValueError(f"{field} 必須是英文（含中文字元）")
        result[field] = stripped

    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise ValueError("notes 必須是字串")
    result["notes"] = notes.strip()
    return {
        "tags": result["tags"],
        "description": result["description"],
        "notes": result["notes"],
    }
