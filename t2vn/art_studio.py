"""素材頁的後端邏輯（不含 HTTP）：素材清單、外觀（地點／角色）草擬與確認、畫風設定。

``t2vn/server.py`` 只是薄外殼。外觀資料存在 ``works/<book>/art/style.json`` 的
``locations`` / ``characters``，每筆 ``{tags, description, notes, confirmed}``；
``confirmed`` 是使用者按過「確認」才為真，出圖前頁面會提醒沒確認的。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from . import art_appearance, art_gen, art_prompts, asset_manifest, character_assets, sprite_consistency
from .errors import SourceError, T2vnError
from .llm import LlmFn, RunHooks, call_llm
from .source_reader import read_characters

TITLE_LABEL = "標題畫面"
TITLE_HINT = "這是遊戲標題畫面的背景圖（主視覺）：請依整本書的世界觀與氣氛構圖，以風景或場景為主，不要人物特寫。"
KINDS = ("location", "character")
SECTION = {"location": "locations", "character": "characters"}
STYLE_FIELDS = ("quality", "safety", "artist", "extra", "negative")
MAX_RETRY = 1


class ArtStudioError(T2vnError):
    """素材頁操作失敗。訊息會直接顯示給使用者。"""


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtStudioError(f"cannot read {path.name}: {exc}") from exc


# ------------------------------------------------------------------ 素材清單

def candidates_with_selected(work: Path, asset: dict[str, Any]) -> list[dict[str, Any]]:
    """素材的候選圖，每張加 ``selected``：素材已處理／定稿，且種子等於素材記下的種子。"""
    chosen = asset.get("seed") if asset.get("status") in ("selected", "processed", "final") else None
    rows = art_gen.list_candidates(work, asset["asset_id"])
    for row in rows:
        row["selected"] = chosen is not None and row.get("seed") == chosen
    return rows


def character_of(asset: dict[str, Any]) -> tuple[str, int] | None:
    """頭像／立繪素材 → ``(角色 id, 同角色內的順序)``：頭像依表情順序在前，立繪（neutral 先）在後；其他素材 → ``None``。"""
    try:
        if asset.get("kind") == "headshot":
            char_id, expr = art_prompts.split_headshot_file(asset.get("file"))
            order = character_assets.HEADSHOT_EXPRESSIONS
            return char_id, order.index(expr) if expr in order else len(order)
        if asset.get("kind") == "sprite":
            char_id, expr = sprite_consistency.split_sprite(asset)
            return char_id, 10 if expr == "neutral" else 11
    except ValueError:
        return None
    return None


def order_images(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """背景在前；之後每個角色的頭像與立繪排在一起（角色依第一次出現的順序）。"""
    others = [a for a in images if character_of(a) is None]
    first: dict[str, int] = {}
    for index, asset in enumerate(images):
        found = character_of(asset)
        if found is not None:
            first.setdefault(found[0], index)
    people = sorted((a for a in images if character_of(a) is not None),
                    key=lambda a: (first[character_of(a)[0]], character_of(a)[1], a["asset_id"]))
    for asset in people:
        asset["char_id"] = character_of(asset)[0]
    return others + people


def list_assets(work: Path) -> dict[str, Any]:
    """manifest 的圖片素材（bg／sprite）加上候選圖；音訊素材另列在 ``audio``。"""
    manifest = art_gen.load_manifest(work)
    if manifest is None:
        return {"ready": False, "assets": [], "audio": [], "counts": {}}
    images, audio, counts = [], [], {}
    for asset in manifest.get("assets", []):
        if asset["kind"] in art_gen.GEN_KINDS:
            item = dict(asset)
            item["candidates"] = candidates_with_selected(work, asset)
            images.append(item)
            counts[asset["status"]] = counts.get(asset["status"], 0) + 1
        else:
            audio.append(asset)
    return {"ready": True, "assets": order_images(images), "audio": audio, "counts": counts}


def asset_file(work: Path, rel: str) -> Path:
    """網頁要顯示的圖片：只准 ``assets/`` 或 ``art/headshots/`` 底下的 png，不准跳出資料夾。"""
    bases = [(work / "assets").resolve(), (work / "art" / "headshots").resolve()]
    target = (work / rel).resolve()
    if target.suffix.lower() != ".png" or not any(base in target.parents for base in bases) or not target.is_file():
        raise ArtStudioError("image not found")
    return target


# ------------------------------------------------------------------ 畫風設定

def get_style(work: Path) -> dict[str, Any]:
    art = art_gen.load_art(work)
    comfy = art.get("comfy") or {}
    return {**{k: art.get(k, "") for k in STYLE_FIELDS}, "candidates": comfy.get("candidates", art_gen.DEFAULT_CANDIDATES),
            "unet": comfy.get("unet", ""), "clip": comfy.get("clip", ""), "vae": comfy.get("vae", "")}


def save_style(config: Any, work: Path, data: dict[str, Any]) -> dict[str, Any]:
    art = art_gen.load_art(work)
    for key in STYLE_FIELDS:
        if key in data:
            art[key] = str(data[key]).strip()
    comfy = dict(art.get("comfy") or {})
    for key in ("unet", "clip", "vae"):
        if key in data:
            comfy[key] = str(data[key]).strip()
    if "candidates" in data:
        count = int(data["candidates"])
        if count < 1 or count > 16:
            raise ArtStudioError("candidates must be between 1 and 16")
        comfy["candidates"] = count
    art["comfy"] = comfy
    art_gen.save_art(config, work, art)
    return get_style(work)


# ------------------------------------------------------------------ 外觀

def _location_ids(work: Path) -> dict[str, int]:
    """場景用到的地點 id → 場景數。"""
    counts: dict[str, int] = {}
    for doc in art_gen.load_scene_docs(work):
        for scene in doc.get("scenes", []):
            loc = scene.get("location_id")
            if isinstance(loc, str) and loc:
                counts[loc] = counts.get(loc, 0) + 1
    return counts


def _cards(library: Path | None) -> list[Any]:
    if library is None:
        return []
    try:
        return read_characters(library)
    except SourceError:
        return []


def _card_for(cards: list[Any], names: list[str]) -> Any | None:
    for card in cards:
        if card.name in names or any(a in names for a in card.aliases):
            return card
    return None


def _names(entry: dict[str, Any], lang: str) -> list[str]:
    """角色的所有稱呼：顯示名、各語言名與別名（去重、保序）。"""
    found = [n for n in (entry.get("names") or {}).values() if isinstance(n, str)]
    found += [a for a in entry.get("aliases") or [] if isinstance(a, str)]
    shown = (entry.get("names") or {}).get(lang)
    if isinstance(shown, str):
        found.insert(0, shown)
    return list(dict.fromkeys(n for n in found if n))


def appearance_items(work: Path, library: Path | None, lang: str = "zh-TW") -> dict[str, Any]:
    """外觀分頁的內容：地點與角色各一列，附設定卡的「外在」與已存的外觀。"""
    art = art_gen.load_art(work)
    style = _read_json(work / "prototype" / "style.json", {})
    cast = _read_json(work / "cast.json", {})
    characters = cast.get("characters") or {}
    cards = _cards(library)
    index = art_prompts.bg_index(art_gen.load_scene_docs(work))
    manifest = {a["asset_id"]: a for a in (art_gen.load_manifest(work) or {}).get("assets", [])}

    def bg_assets(loc: str) -> list[dict[str, Any]]:
        """這個地點的背景素材（每個時段一項）；標題畫面對應 ``bg_title``。"""
        if loc == asset_manifest.TITLE_LOCATION_ID:
            ids = [(asset_manifest.TITLE_BG_ID, "")]
        else:
            ids = [(aid, tod) for aid, (where, tod) in index.items() if where == loc]
        rows = []
        for aid, tod in ids:
            entry = manifest.get(aid)
            rows.append({"asset_id": aid, "time": asset_manifest._time_label(tod, style) if tod else "", "in_manifest": entry is not None,
                         "status": (entry or {}).get("status", ""), "candidates": len(art_gen.list_candidates(work, aid)) if entry else 0})
        return rows

    locations = [{"id": loc, "label": asset_manifest._location_label(loc, style), "scenes": count,
                  "card_text": "", "art": (art.get("locations") or {}).get(loc) or {}, "assets": bg_assets(loc)}
                 for loc, count in sorted(_location_ids(work).items()) if loc != asset_manifest.TITLE_LOCATION_ID]
    locations.insert(0, {"id": asset_manifest.TITLE_LOCATION_ID, "label": TITLE_LABEL, "scenes": 0, "card_text": TITLE_HINT,
                         "art": (art.get("locations") or {}).get(asset_manifest.TITLE_LOCATION_ID) or {},
                         "assets": bg_assets(asset_manifest.TITLE_LOCATION_ID)})
    people = []
    for char_id, entry in characters.items():
        names = _names(entry, lang)
        card = _card_for(cards, names)
        people.append({"id": char_id, "label": asset_manifest._character_label(char_id, characters, lang), "names": names,
                       "card_text": card.sections.get("appearance", "") if card else "",
                       "art": (art.get("characters") or {}).get(char_id) or {}})
    return {"locations": locations, "characters": people}


def save_appearance(config: Any, work: Path, kind: str, item_id: str, data: dict[str, Any]) -> dict[str, Any]:
    if kind not in KINDS:
        raise ArtStudioError(f"kind must be one of {KINDS}")
    if not item_id or not all(c.isalnum() or c == "_" for c in item_id):
        raise ArtStudioError(f"invalid id {item_id!r}")
    entry = {"tags": str(data.get("tags", "")).strip(), "description": str(data.get("description", "")).strip(),
             "notes": str(data.get("notes", "")).strip(), "confirmed": bool(data.get("confirmed"))}
    if entry["confirmed"] and not (entry["tags"] or entry["description"]):
        raise ArtStudioError("tags 與 description 至少要填一個才能確認")
    art = art_gen.load_art(work)
    art.setdefault(SECTION[kind], {})[item_id] = entry
    art_gen.save_art(config, work, art)
    return entry


def _chapter_docs(work: Path) -> list[dict[str, Any]]:
    docs = []
    for path in sorted((work / "chapters").glob("ch[0-9][0-9][0-9].json")):
        docs.append(_read_json(path, {}))
    return docs


def draft_appearance(work: Path, library: Path | None, kind: str, item_id: str, llm: LlmFn, lang: str = "zh-TW",
                     hooks: RunHooks | None = None) -> dict[str, Any]:
    """請 AI 依設定卡「外在」與小說內文草擬外觀；回傳 ``{tags, description, notes, evidence}``，不存檔。"""
    if kind not in KINDS:
        raise ArtStudioError(f"kind must be one of {KINDS}")
    items = appearance_items(work, library, lang)
    row = next((x for x in items[SECTION[kind]] if x["id"] == item_id), None)
    if row is None:
        raise ArtStudioError(f"{item_id}: not found")
    if kind == "character":
        names, keywords = row["names"], art_appearance.CHARACTER_KEYWORDS
    else:
        names = [n for n in (row["id"].replace("_", " "), row["label"]) if n]
        keywords = art_appearance.LOCATION_KEYWORDS
    evidence = art_appearance.find_evidence(_chapter_docs(work), names, keywords)
    current = {k: row["art"].get(k, "") for k in ("tags", "description")} if row["art"] else None
    prompt = art_appearance.build_prompt(kind, row["label"], row["card_text"], evidence, current)
    messages = [{"role": "user", "content": prompt}]
    issue = ""
    for attempt in range(MAX_RETRY + 1):
        result = call_llm(llm, messages, hooks, attempt + 1)
        try:
            draft = art_appearance.parse_reply(result.text)
            break
        except ValueError as exc:
            issue = str(exc)
            messages = messages + [{"role": "assistant", "content": result.text},
                                   {"role": "user", "content": f"Your reply was rejected: {issue}. Reply again with only the JSON object, English only."}]
    else:
        raise ArtStudioError(f"AI 的回覆格式不對（{issue}），請再試一次")
    draft["evidence"] = evidence
    if not evidence and not row["card_text"]:
        draft["notes"] = ("（設定卡與內文都沒找到描寫，這份純屬推測，請自己修改）" + draft["notes"]).strip()
    return draft


# ------------------------------------------------------------------ 出圖任務

def run_generation(config: Any, work: Path, backend: art_gen.ComfyBackend, *, only: list[str] | None, regenerate: bool,
                   candidates: int | None, should_stop: Callable[[], bool], on_progress: Callable[[dict[str, Any]], None],
                   log: Callable[[str], None]) -> dict[str, Any]:
    """背景任務本體：先重掃 manifest，再逐項出圖。回傳 ``{generated, skipped, failed, stopped}``。"""
    lang = _read_json(work / "book.json", {}).get("lang", "zh-TW")
    book_id = _read_json(work / "book.json", {}).get("book_id", work.name)
    art_gen.refresh_manifest(config, work, book_id, lang)
    items = appearance_items(work, None, lang)
    unconfirmed = [x["label"] for kind in ("locations", "characters") for x in items[kind]
                   if x["id"] != "player" and not x["art"].get("confirmed")]
    if unconfirmed:
        log("提醒：這些外觀還沒按「確認」：" + "、".join(unconfirmed))
    report = art_gen.generate(config, work, backend, candidates=candidates, only=only, regenerate=regenerate,
                              should_stop=should_stop, progress=on_progress)
    for warning in report.warnings:
        log("warning: " + warning)
    for asset_id, reason in report.failed.items():
        log(f"failed: {asset_id}: {reason}")
    return {"generated": report.generated, "skipped": report.skipped, "failed": report.failed, "stopped": report.stopped}
