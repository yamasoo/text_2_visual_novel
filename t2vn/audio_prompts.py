"""BGM／環境音的 YuE2 提示詞：組給 AI 的提示詞與回覆解析（S7）。

YuE2 是歌曲模型，要做純配樂時：曲風描述寫成英文標籤並以 ``pure instrumental, no vocals``
結尾；歌詞欄只放段落標籤（``[Intro]``）與括號裡的編曲註記（``(Soft piano)``），不放任何
要唱的字。本模組不呼叫 AI（呼叫端做），只做兩件事：組提示詞、解析並整理 AI 的回覆。
"""
from __future__ import annotations

import json
import re
from typing import Any

KINDS = ("bgm", "ambience")
INSTRUMENTAL = "pure instrumental, no vocals"
AMBIENCE_TAGS = "ambient soundscape, no melody, no drums, seamless loop"
MAX_STYLE_CHARS = 600
MAX_SCENES = 6
TAG_LINE = re.compile(r"^\[[A-Za-z][A-Za-z0-9 \-]{0,30}\]$")
NOTE_LINE = re.compile(r"^\(.{1,200}\)$")
FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
NON_ASCII = re.compile(r"[^\x00-\x7f]")
SPACES = re.compile(r"\s+")

INTRO = (
    "You write music prompts for YuE2, an AI music model, to make background tracks "
    "for a visual novel. The tracks must be purely instrumental."
)
KIND_HELP = {
    "bgm": "A background music track with a clear mood that loops quietly under dialogue.",
    "ambience": (
        "An ambient sound bed for a place: sparse, slowly evolving, no melody and no drums, "
        "meant to loop under dialogue."
    ),
}
REPLY_FORMAT = (
    'Return ONLY one JSON object, no commentary: {"style": "", "lyrics": ""}. '
    "Both values are strings."
)
STYLE_RULES = (
    "style: English comma-separated tags for genre, tempo (e.g. 70 bpm), instruments and mood, "
    f"under {MAX_STYLE_CHARS} characters, ending with \"{INSTRUMENTAL}\"."
)
LYRICS_RULES = (
    "lyrics: ONLY section tags on their own line such as [Intro], [Verse 1], [Chorus], [Outro], "
    "each followed by one line with an arrangement note in parentheses, for example "
    "(Soft piano, atmospheric pads). Never write words to be sung. "
    "Use 4 to 6 sections for bgm and 3 sections for ambience."
)
DEFAULT_LYRICS = {
    "bgm": (
        "[Intro]\n(Soft atmospheric opening)\n\n"
        "[Verse 1]\n(Steady gentle rhythm, melody carried by the lead instrument)\n\n"
        "[Chorus]\n(Fuller arrangement, warm and open)\n\n"
        "[Verse 2]\n(Back to the gentle texture)\n\n"
        "[Outro]\n(Final chord fading to silence)"
    ),
    "ambience": (
        "[Intro]\n(Quiet evolving texture)\n\n"
        "[Verse]\n(Steady low ambience, very sparse)\n\n"
        "[Outro]\n(Texture fades out)"
    ),
}

TRACK_KEYS = ("id", "label", "note", "prompt")
SCENE_KEYS = ("scene_id", "title", "location", "time_of_day", "summary")
NO_SCENE_LINE = (
    "No scene information is available; infer the mood from the track description."
)
DRAFT_TITLE = "Current draft (revise it, keep what is still correct):"


def _check_kind(kind: Any) -> str:
    """``kind`` 只能是 bgm／ambience，否則 ``ValueError``。"""
    if kind not in KINDS:
        raise ValueError(f"kind 必須是 {'／'.join(KINDS)} 之一：{kind!r}")
    return kind


def default_lyrics(kind: str) -> str:
    """沒有可用歌詞時的備用段落（只有標籤與編曲註記）。"""
    _check_kind(kind)
    return DEFAULT_LYRICS[kind]


def _scene_line(scene: dict[str, Any]) -> str:
    """一場的證據行：``- ch002_sc01 | 標題 | place: … | time: … | 摘要``。"""
    parts = [str(scene.get(key, "") or "") for key in SCENE_KEYS]
    return "- " + " | ".join(
        [
            parts[0],
            parts[1],
            f"place: {parts[2]}",
            f"time: {parts[3]}",
            parts[4],
        ]
    )


def build_prompt(
    kind: str,
    entry: dict[str, Any] | None,
    scenes: list[dict[str, Any]] | None,
    current: dict[str, Any] | None = None,
) -> str:
    """組給 AI 的英文提示詞。

    內容依序：任務說明與回覆格式、曲風／歌詞規則、曲目資料（``id``／``label``／
    ``note``／``prompt``，空的不寫）、用到它的場景（最多 :data:`MAX_SCENES` 場；沒有
    場景時是一行說明）、目前草稿（有才出現）。``kind`` 不是 bgm／ambience →
    ``ValueError``。
    """
    _check_kind(kind)
    lines = [INTRO, KIND_HELP[kind], REPLY_FORMAT, STYLE_RULES, LYRICS_RULES, "", "Track:"]

    data = entry if isinstance(entry, dict) else {}
    written = False
    for key in TRACK_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{key}: {value.strip()}")
            written = True
    if not written:
        lines.append("No track information is available.")

    picked = [item for item in (scenes or []) if isinstance(item, dict)][:MAX_SCENES]
    lines.append("")
    if picked:
        lines.append("Scenes that use this track:")
        for scene in picked:
            lines.append(_scene_line(scene))
    else:
        lines.append(NO_SCENE_LINE)

    if isinstance(current, dict) and current:
        lines.append("")
        lines.append(DRAFT_TITLE)
        lines.append(json.dumps(current, ensure_ascii=False))
    return "\n".join(lines)


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


def _clean_style(value: str, kind: str, warnings: list[str]) -> str:
    """整理曲風標籤：壓縮空白、去尾逗號、截短、補環境音與「純配樂」結尾。"""
    text = SPACES.sub(" ", value).strip().rstrip(",").strip()
    if not text:
        return ""
    if len(text) > MAX_STYLE_CHARS:
        cut = text.rfind(",", 0, MAX_STYLE_CHARS)
        text = (text[:cut] if cut > 0 else text[:MAX_STYLE_CHARS]).rstrip(",").strip()
        warnings.append(f"style 太長，已截短到 {MAX_STYLE_CHARS} 字元")
    if kind == "ambience" and "ambient" not in text.lower():
        text = f"{text}, {AMBIENCE_TAGS}"
    if INSTRUMENTAL not in text.lower():
        text = f"{text}, {INSTRUMENTAL}"
    if NON_ASCII.search(text):
        warnings.append("style 含非英文字元，請自行改成英文標籤")
    return text


def _clean_lyrics(value: Any, kind: str, warnings: list[str]) -> str:
    """只留段落標籤與編曲註記；沒有可用標籤時退回備用段落。"""
    if not isinstance(value, str) or not value.strip():
        warnings.append("沒有可用歌詞，改用預設段落")
        return default_lyrics(kind)

    sections: list[tuple[str, list[str]]] = []
    removed = 0
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if TAG_LINE.match(line):
            sections.append((line, []))
        elif NOTE_LINE.match(line) and sections:
            sections[-1][1].append(line)
        else:
            removed += 1

    if not sections:
        warnings.append("沒有可用歌詞，改用預設段落")
        return default_lyrics(kind)
    if removed:
        warnings.append(f"歌詞有 {removed} 行不是段落標籤或編曲註記，已移除")
    return "\n\n".join("\n".join([tag, *notes]) for tag, notes in sections)


def parse_reply(text: str, kind: str) -> tuple[dict[str, str], list[str]]:
    """解析 AI 的回覆，回傳 ``({"style", "lyrics"}, warnings)``。

    容許前後說明文字或 ``````json`` 圍欄。``style`` 必填（非空字串），會整理成英文標籤
    並確保以 :data:`INSTRUMENTAL` 結尾（環境音另外補 :data:`AMBIENCE_TAGS`）；``lyrics``
    只留段落標籤與編曲註記，沒有可用標籤時退回 :func:`default_lyrics`。找不到 JSON、
    根不是物件、缺 ``style`` 或清完是空字串 → ``ValueError``。
    """
    _check_kind(kind)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("回覆必須是非空字串，而且裡面要有 JSON")

    data = _extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("回覆的 JSON 根必須是物件")

    style = data.get("style")
    if not isinstance(style, str):
        raise ValueError("回覆缺少 style（或不是字串）")
    warnings: list[str] = []
    cleaned_style = _clean_style(style, kind, warnings)
    if not cleaned_style:
        raise ValueError("style 不可為空")

    lyrics = _clean_lyrics(data.get("lyrics"), kind, warnings)
    return {"style": cleaned_style, "lyrics": lyrics}, warnings
