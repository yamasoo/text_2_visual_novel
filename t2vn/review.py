"""產生給人看的校對表（Markdown）：每行一列，台詞的說話者與表情加粗，推斷的說話者標 ``?``。"""
from __future__ import annotations

from typing import Any

EXPR_LABELS = {
    "neutral": "平常", "smile": "笑", "sad": "難過", "angry": "生氣",
    "surprised": "驚訝", "worried": "不安", "serious": "認真", "embarrassed": "尷尬",
}
POS_LABELS = {"left": "左", "center": "中", "right": "右"}
PLAYER_ID = "player"


def render_review(scene_doc: dict[str, Any], cast_doc: dict[str, Any], flagged: set[tuple[str, int]] | None = None,
                  heading: str | None = None, notes: list[str] | None = None) -> str:
    """``flagged`` 是 (scene_id, 行號從 1 起) 的集合。"""
    flagged = flagged or set()
    lang = scene_doc["lang"]
    characters = cast_doc["characters"]

    def name(char_id: str) -> str:
        char = characters.get(char_id)
        return char["names"].get(lang, char_id) if char else char_id

    chapter_id = scene_doc["chapter_id"]
    out = [f"# {heading or chapter_id + ' 校對表'}", ""]
    out += ["> 主要看**台詞**行的說話者與表情；註記 `?` 是推斷的說話者，請優先確認。要改的回覆編號＋改法，例如 `sc03-12 表情改 認真`。", ""]
    for note in notes or []:
        out.append(f"- {note}")
    if notes:
        out.append("")
    for scene in scene_doc["scenes"]:
        sid = scene["scene_id"]
        short = sid.split("_")[-1]
        paras = [line["source_para"] for line in scene["lines"] if "source_para" in line]
        title = scene.get("title", {}).get(lang, "")
        cast = "、".join(
            f"{name(c['char_id'])}（{EXPR_LABELS.get(c.get('expr', ''), '—')}，{POS_LABELS.get(c.get('pos', ''), '—')}）"
            for c in scene.get("cast", [])
        ) or "—"
        out += [
            f"## {short}　{title}（第 {min(paras)}–{max(paras)} 段）", "",
            f"- 地點／時段：`{scene['location_id']}`／`{scene['time_of_day']}`　背景 `{scene['bg']['asset_id']}`　"
            f"BGM `{scene.get('bgm')}`　環境音 `{scene.get('ambience')}`",
            f"- 出場：{cast}", "",
            "| 編號 | 段 | 類型 | 說話者 | 表情 | 內容 | 註記 |", "|---|---|---|---|---|---|---|",
        ]
        for index, line in enumerate(scene["lines"], 1):
            ref = f"{short}-{index:02d}"
            if line["type"] == "direction":
                out.append(f"| {ref} | | 演出 | | | （`{line['effect']}`） | |")
                continue
            text = line.get("text", "").replace("|", "｜")
            if line["type"] == "narration":
                out.append(f"| {ref} | {line['source_para']} | 旁白 | | | {text} | |")
            elif line["type"] == "dialogue":
                note = "內心獨白" if line["speaker"] == PLAYER_ID else ("?" if (sid, index) in flagged else "")
                expr = EXPR_LABELS.get(line.get("expr", ""), "—")
                out.append(f"| {ref} | {line['source_para']} | **台詞** | **{name(line['speaker'])}** | **{expr}** | {text} | {note} |")
        out.append("")
    return "\n".join(out)
