"""出圖提示詞：把素材項目轉成 Anima 的標籤＋自然語言提示詞（S6）。

Anima 是動漫風文生圖模型，吃 Danbooru 標籤加英文句子。這個模組把素材清單裡的一項
（背景或立繪）加上本書的畫風設定，組成正向／負向提示詞與出圖尺寸。純函式：不連網、
不讀寫檔案。
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

EXPRESSIONS = (
    "neutral",
    "smile",
    "sad",
    "angry",
    "surprised",
    "worried",
    "serious",
    "embarrassed",
    "laughing",
)
TIME_TAGS = {
    "dawn": "dawn, early morning, orange and purple sky, soft light",
    "morning": "morning, sunlight, clear sky",
    "day": "daytime, bright sunlight, blue sky",
    "dusk": "dusk, sunset, orange sky, long shadows",
    "night": "night, dark, moonlight, starry sky",
}
EXPR_TAGS = {
    "neutral": "neutral expression, closed mouth",
    "smile": "smile, happy",
    "sad": "sad, downcast eyes",
    "angry": "angry, furrowed brow",
    "surprised": "surprised, wide eyes, open mouth",
    "worried": "worried, anxious",
    "serious": "serious, determined",
    "embarrassed": "embarrassed, blush, looking away",
    "laughing": "laughing, open mouth, happy",
}
DEFAULT_ART = {
    "quality": "masterpiece, best quality, score_7",
    "safety": "safe",
    "artist": "",
    "extra": "",
    "negative": "worst quality, low quality, score_1, score_2, score_3, artist name, watermark",
    "bg": {"width": 1280, "height": 720, "extra": ""},
    # 比例決定構圖：立繪要窄長（約 9:21）模型才會畫全身；頭像 3:4；背景 16:9。尺寸為 16 的倍數、約 1 MP。
    "sprite": {"width": 672, "height": 1568, "extra": ""},
    "headshot": {"width": 864, "height": 1152, "extra": ""},
    "locations": {},
    "characters": {},
}
BG_NEGATIVE = "1girl, 1boy, person, people, text"
SPRITE_NEGATIVE = (
    "multiple views, cropped, cut off, text, scenery, complex background, "
    "sitting, crouching, kneeling, leaning, lying, squatting, chair, stool"
)
HEADSHOT_NEGATIVE = "multiple views, text, scenery, complex background, full body"

SPLIT_PATTERN = re.compile(r"[,\n]")
WHITESPACE_PATTERN = re.compile(r"\s+")
SCORE_TAG_PATTERN = re.compile(r"^score_[0-9]+$")
SPRITE_FILE_PATTERN = re.compile(r"^assets/sprite/([a-z0-9_]+)\.png$")
HEADSHOT_FILE_PATTERN = re.compile(r"^assets/headshot/([a-z0-9_]+)\.png$")
SENTENCE_ENDS = (".", "!", "?", "。", "！", "？")
SEED_MODULUS = 2**31


# ------------------------------------------------------------------------ 標籤


def normalize_tags(text: str) -> str:
    """整理成 ``a, b, c``：逗號或換行分隔、去空白、小寫、底線換空白、去重。

    ``score_7`` 這類 ``score_數字`` 保留底線；``@artist`` 保留 ``@``；不分大小寫去重
    （留第一個）。空輸入（含 ``None``）回傳空字串。
    """
    if not isinstance(text, str) or not text.strip():
        return ""

    seen: set[str] = set()
    tags: list[str] = []
    for raw in SPLIT_PATTERN.split(text):
        tag = WHITESPACE_PATTERN.sub(" ", raw).strip().lower()
        if not tag:
            continue
        if not SCORE_TAG_PATTERN.match(tag):
            tag = WHITESPACE_PATTERN.sub(" ", tag.replace("_", " ")).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return ", ".join(tags)


def _join_tags(*parts: Any) -> str:
    """把幾段標籤字串用逗號接起來（略過空的）。"""
    return ", ".join(
        part.strip() for part in parts if isinstance(part, str) and part.strip()
    )


def _artist_tags(value: Any) -> str:
    """畫家標籤：normalize 後逐個補上 ``@``。"""
    normalized = normalize_tags(value) if isinstance(value, str) else ""
    if not normalized:
        return ""
    return ", ".join(
        tag if tag.startswith("@") else "@" + tag for tag in normalized.split(", ")
    )


def _sentence(text: str) -> str:
    """描述句：去掉頭尾空白，沒句末標點就補一個句號。"""
    value = text.strip()
    if not value:
        return ""
    if value.endswith(SENTENCE_ENDS):
        return value
    return value + "."


def merge_art(art: dict[str, Any] | None) -> dict[str, Any]:
    """把使用者的畫風設定疊在 :data:`DEFAULT_ART` 上，回傳新 dict（不改傳入的物件）。

    ``bg``、``sprite`` 逐欄合併；其餘的鍵整個覆蓋。
    """
    merged = copy.deepcopy(DEFAULT_ART)
    if not isinstance(art, dict):
        return merged
    for key, value in art.items():
        if key in ("bg", "sprite", "headshot") and isinstance(value, dict):
            section = merged.get(key)
            if not isinstance(section, dict):
                section = {}
            section.update(copy.deepcopy(value))
            merged[key] = section
        else:
            merged[key] = copy.deepcopy(value)
    return merged


# ------------------------------------------------------------------------ 提示詞


def _section(merged: dict[str, Any], key: str) -> dict[str, Any]:
    """取某個區塊（bg／sprite／locations／characters），不是 dict 就空 dict。"""
    value = merged.get(key)
    return value if isinstance(value, dict) else {}


def bg_prompt(
    location_id: str, time_of_day: str, art: dict[str, Any] | None = None
) -> dict[str, Any]:
    """背景提示詞，回傳 ``{"positive", "negative", "width", "height", "warnings"}``。

    positive ＝ 標籤行 ＋ ``"."`` ＋（有地點描述時）換行與描述句。標籤行順序固定：
    quality、safety、artist、``no humans``、``scenery``、地點 tags（沒有就用
    location_id 的底線換空白）、時段 tags、``art["extra"]``、``art["bg"]["extra"]``。
    不認得的時段 → ``ValueError``。
    """
    if time_of_day not in TIME_TAGS:
        raise ValueError(
            f"不認得的 time_of_day：{time_of_day!r}"
            f"（可用：{', '.join(sorted(TIME_TAGS))}）"
        )

    merged = merge_art(art)
    locations = _section(merged, "locations")
    warnings: list[str] = []
    entry = locations.get(location_id)
    if not isinstance(entry, dict):
        entry = None

    tags = entry.get("tags") if entry is not None else None
    if isinstance(tags, str) and tags.strip():
        location_tags = tags
    else:
        location_tags = location_id.replace("_", " ")
        if entry is None:
            warnings.append(
                f"沒有地點設定：{location_id}（提示詞少了地點標籤與描述）"
            )

    bg_section = _section(merged, "bg")
    tagline = normalize_tags(
        _join_tags(
            merged.get("quality"),
            merged.get("safety"),
            _artist_tags(merged.get("artist")),
            "no humans",
            "scenery",
            location_tags,
            TIME_TAGS[time_of_day],
            merged.get("extra"),
            bg_section.get("extra"),
        )
    )

    positive = tagline + "."
    description = entry.get("description") if entry is not None else None
    if isinstance(description, str) and description.strip():
        positive += "\n" + _sentence(description)

    return {
        "positive": positive,
        "negative": normalize_tags(
            _join_tags(merged.get("negative"), BG_NEGATIVE)
        ),
        "width": bg_section.get("width"),
        "height": bg_section.get("height"),
        "warnings": warnings,
    }


def sprite_prompt(
    char_id: str, expr: str, art: dict[str, Any] | None = None
) -> dict[str, Any]:
    """立繪提示詞（綠底，方便去背；被手臂圍住的背景也能去乾淨，白衣服不受影響），回傳格式同 :func:`bg_prompt`。

    標籤行：quality、safety、artist、``solo``、角色 tags（沒有就不加並在 warnings
    提醒）、表情 tags、``standing``、``full body``、``looking at viewer``、
    ``simple background``、``green background``、``art["extra"]``、
    ``art["sprite"]["extra"]``；接著換行放角色描述句。不認得的表情 → ``ValueError``。
    """
    if expr not in EXPR_TAGS:
        raise ValueError(
            f"不認得的 expr：{expr!r}（可用：{', '.join(EXPRESSIONS)}）"
        )

    merged = merge_art(art)
    characters = _section(merged, "characters")
    warnings: list[str] = []
    entry = characters.get(char_id)
    if not isinstance(entry, dict):
        entry = None

    tags = entry.get("tags") if entry is not None else None
    character_tags = tags if isinstance(tags, str) and tags.strip() else ""
    if not character_tags:
        warnings.append(
            f"沒有角色設定：{char_id}（提示詞少了角色標籤與描述）"
        )

    sprite_section = _section(merged, "sprite")
    parts = [
        merged.get("quality"),
        merged.get("safety"),
        _artist_tags(merged.get("artist")),
        "solo",
    ]
    if character_tags:
        parts.append(character_tags)
    parts.extend(
        [
            EXPR_TAGS[expr],
            "standing",
            "full body",
            "looking at viewer",
            "simple background",
            "green background",
            merged.get("extra"),
            sprite_section.get("extra"),
        ]
    )
    tagline = normalize_tags(_join_tags(*parts))

    positive = tagline + "."
    description = entry.get("description") if entry is not None else None
    if isinstance(description, str) and description.strip():
        positive += "\n" + _sentence(description)

    return {
        "positive": positive,
        "negative": normalize_tags(
            _join_tags(merged.get("negative"), SPRITE_NEGATIVE)
        ),
        "width": sprite_section.get("width"),
        "height": sprite_section.get("height"),
        "warnings": warnings,
    }


def headshot_prompt(
    char_id: str, expr: str, art: dict[str, Any] | None = None
) -> dict[str, Any]:
    """頭像提示詞（頸部以上，白底），回傳格式同 :func:`bg_prompt`。

    角色標籤取 ``art["characters"][char_id]["headshot_tags"]``（外型卡的頭像提示詞），
    沒有就不加並在 warnings 提醒；表情用 :data:`EXPR_TAGS`；不認得的表情 → ``ValueError``。
    """
    if expr not in EXPR_TAGS:
        raise ValueError(
            f"不認得的 expr：{expr!r}（可用：{', '.join(EXPRESSIONS)}）"
        )
    merged = merge_art(art)
    entry = _section(merged, "characters").get(char_id)
    tags = entry.get("headshot_tags") if isinstance(entry, dict) else None
    character_tags = tags if isinstance(tags, str) and tags.strip() else ""
    warnings: list[str] = []
    if not character_tags:
        warnings.append(f"沒有頭像提示詞：{char_id}（提示詞少了角色標籤）")
    section = _section(merged, "headshot")
    parts = [merged.get("quality"), merged.get("safety"), _artist_tags(merged.get("artist")), "solo"]
    if character_tags:
        parts.append(character_tags)
    parts.extend([EXPR_TAGS[expr], "portrait", "face focus", "looking at viewer", "simple background",
                  "white background", merged.get("extra"), section.get("extra")])
    return {
        "positive": normalize_tags(_join_tags(*parts)) + ".",
        "negative": normalize_tags(_join_tags(merged.get("negative"), HEADSHOT_NEGATIVE)),
        "width": section.get("width"),
        "height": section.get("height"),
        "warnings": warnings,
    }


# ------------------------------------------------------------------ manifest 對照


def bg_index(scene_docs: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """``{bg asset_id: (location_id, time_of_day)}``；同一個背景以第一次出現的場為準。"""
    index: dict[str, tuple[str, str]] = {}
    for doc in scene_docs or []:
        if not isinstance(doc, dict):
            continue
        scenes = doc.get("scenes")
        if not isinstance(scenes, list):
            continue
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            bg = scene.get("bg")
            asset_id = bg.get("asset_id") if isinstance(bg, dict) else None
            if not isinstance(asset_id, str) or not asset_id or asset_id in index:
                continue
            index[asset_id] = (
                str(scene.get("location_id") or ""),
                str(scene.get("time_of_day") or ""),
            )
    return index


def split_sprite_file(file: str) -> tuple[str, str]:
    """``assets/sprite/<char_id>_<expr>.png`` → ``(char_id, expr)``。

    ``expr`` 取 :data:`EXPRESSIONS` 裡符合結尾的那個；都不符合或路徑不對 →
    ``ValueError``。
    """
    if not isinstance(file, str):
        raise ValueError(f"立繪路徑必須是字串：{file!r}")
    match = SPRITE_FILE_PATTERN.match(file)
    if match is None:
        raise ValueError(f"立繪路徑格式不對：{file!r}")
    stem = match.group(1)
    for expr in EXPRESSIONS:
        suffix = "_" + expr
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)], expr
    raise ValueError(f"立繪路徑看不出表情：{file!r}")


def split_headshot_file(file: str) -> tuple[str, str]:
    """``assets/headshot/<char_id>_<expr>.png`` → ``(char_id, expr)``；格式不對 → ``ValueError``。"""
    if not isinstance(file, str):
        raise ValueError(f"頭像路徑必須是字串：{file!r}")
    match = HEADSHOT_FILE_PATTERN.match(file)
    if match is None:
        raise ValueError(f"頭像路徑格式不對：{file!r}")
    stem = match.group(1)
    for expr in EXPRESSIONS:
        suffix = "_" + expr
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)], expr
    raise ValueError(f"頭像路徑看不出表情：{file!r}")


def prompt_for_asset(
    asset: dict[str, Any],
    backgrounds: dict[str, tuple[str, str]],
    art: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """依 manifest 的一個項目選 :func:`bg_prompt`、:func:`sprite_prompt` 或 :func:`headshot_prompt`。

    ``kind`` 不是 bg／sprite／headshot → ``ValueError``；背景不在 ``backgrounds`` → ``ValueError``。
    """
    if not isinstance(asset, dict):
        raise ValueError("素材項目必須是物件")
    kind = asset.get("kind")
    if kind == "bg":
        asset_id = asset.get("asset_id")
        if not isinstance(asset_id, str) or asset_id not in backgrounds:
            raise ValueError(f"找不到背景對照：{asset_id!r}")
        location_id, time_of_day = backgrounds[asset_id]
        return bg_prompt(location_id, time_of_day, art)
    if kind == "sprite":
        char_id, expr = split_sprite_file(asset.get("file"))
        return sprite_prompt(char_id, expr, art)
    if kind == "headshot":
        char_id, expr = split_headshot_file(asset.get("file"))
        return headshot_prompt(char_id, expr, art)
    raise ValueError(f"不支援的 kind：{kind!r}")


def seed_for(asset_id: str, attempt: int = 0) -> int:
    """固定的種子：同一個素材同一次嘗試永遠得到同一個值，不同嘗試不同。

    範圍 ``0 ≤ seed < 2**31``；``attempt`` 小於 0 → ``ValueError``。
    """
    if not isinstance(asset_id, str) or not asset_id:
        raise ValueError(f"asset_id 不合法：{asset_id!r}")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError(f"attempt 必須是 0 或正整數：{attempt!r}")
    digest = hashlib.sha256(f"{asset_id}:{attempt}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % SEED_MODULUS
