"""外觀分頁的後端接線（不含 HTTP）：把 ``art_cards`` 與設定卡、內文證據、出圖流程接在一起。

``t2vn/server.py`` 只是薄外殼。純邏輯在 ``art_cards``（DS 外包模組）；這裡負責
跨模組的部分：角色清單帶設定卡文字、AI 草擬（找內文證據）、頭像＋立繪合併成一個出圖任務。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import asset_manifest, appearance_card, art_appearance, art_cards, art_gen, art_prompts, art_studio, character_assets, sprite_consistency, wildcards
from .errors import T2vnError
from .llm import LlmFn, RunHooks, call_llm

STYLE_CATALOG_NAME = "art_styles.json"
XLSX_SUFFIXES = (".xlsx", ".xlsm")


class ArtCardsStudioError(T2vnError):
    """外觀分頁操作失敗。訊息會直接顯示給使用者。"""


def catalog_path(project_root: Path) -> Path:
    """畫風目錄存放處（``config/art_styles.json``）。"""
    return Path(project_root) / "config" / STYLE_CATALOG_NAME


def load_wildcards(directory: Path | None) -> tuple[dict[str, list[str]], list[str]]:
    """讀 wildcard 資料夾（唯讀）；資料夾不存在就當作沒有，不報錯。"""
    if directory is None or not Path(directory).is_dir():
        return {}, []
    return wildcards.load_library(directory)


def load_catalog(project_root: Path) -> list[dict[str, Any]]:
    """讀畫風目錄；還沒匯入就是空清單。"""
    from . import art_styles
    path = catalog_path(project_root)
    if not path.is_file():
        return []
    try:
        return art_styles.load_catalog(path)
    except (OSError, ValueError) as exc:
        raise ArtCardsStudioError(f"讀不了畫風目錄：{exc}") from exc


# ------------------------------------------------------------------ 角色清單

def _characters(work: Path, library: Path | None, lang: str) -> list[dict[str, Any]]:
    return art_studio.appearance_items(work, library, lang)["characters"]


def _character_assets(work: Path) -> dict[str, list[dict[str, Any]]]:
    """manifest 裡每位角色的頭像與立繪列（頭像依表情順序在前、立繪在後），帶候選圖。"""
    manifest = art_gen.load_manifest(work)
    if manifest is None:
        return {}
    order = {expr: i for i, expr in enumerate(character_assets.HEADSHOT_EXPRESSIONS)}
    found: dict[str, list[tuple[tuple[int, int], dict[str, Any]]]] = {}
    for asset in manifest.get("assets", []):
        kind = asset.get("kind")
        try:
            if kind == "headshot":
                char_id, expr = art_prompts.split_headshot_file(asset.get("file"))
                rank = (0, order.get(expr, 99))
            elif kind == "sprite":
                char_id, expr = sprite_consistency.split_sprite(asset)
                rank = (1, 0 if expr == "neutral" else 1)
            else:
                continue
        except ValueError:
            continue
        row = {"asset_id": asset["asset_id"], "kind": kind, "expr": expr, "status": asset.get("status", ""),
               "used_in": len(asset.get("used_in") or []),
               "candidates": art_studio.candidates_with_selected(work, asset)}
        found.setdefault(char_id, []).append((rank, row))
    return {c: [row for _rank, row in sorted(rows, key=lambda item: (item[0], item[1]["asset_id"]))] for c, rows in found.items()}


def overview(work: Path, library: Path | None, lang: str) -> dict[str, Any]:
    """外觀頁的角色清單：每位角色帶設定卡「外在」、外型卡、素材列（頭像、立繪）與立繪的場景數。"""
    people = _characters(work, library, lang)
    labels = {p["id"]: p["label"] for p in people}
    texts = {p["id"]: p["card_text"] for p in people}
    assets = _character_assets(work)
    rows = art_cards.list_cards(work, {p["id"]: None for p in people}, labels, texts, assets)
    for row in rows:
        row["sprite_scenes"] = sum(a["used_in"] for a in row["assets"] if a["kind"] == "sprite")
    return {"characters": rows}


# ------------------------------------------------------------------ AI 草擬

def _library_story_core(library: Path | None) -> str:
    """從書庫的故事核心檔摘取時代／世界觀背景（最多約 800 字）。"""
    if library is None or not Path(library).is_dir():
        return ""
    for rel in ("bible/story-core.md", "設定/故事核心.md"):
        p = Path(library) / rel
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="ignore").strip()
                if text.startswith("---"):
                    end = text.find("\n---", 3)
                    if end > 0:
                        text = text[end + 4 :].strip()
                return text[:800]
            except OSError:
                return ""
    return ""


def read_world_context(work: Path, library: Path | None = None) -> dict[str, str]:
    """讀本書的時代與服飾基調：回傳 ``{"world_context": 自訂值, "fallback": 書庫自動摘錄}``。"""
    style = art_cards._read_json(art_cards._style_path(work), {})
    custom = str(style.get("world_context") or "").strip() if isinstance(style, dict) else ""
    return {"world_context": custom, "fallback": _library_story_core(library)}


def save_world_context(assert_writable: Callable[[Path], Any], work: Path, text: str) -> str:
    """存本書的時代與服飾基調到 ``work/art/style.json`` 的 ``world_context``（空字串表示清除並退回自動讀取）。"""
    path = art_cards._style_path(work)
    style = art_cards._read_json(path, {})
    if not isinstance(style, dict):
        style = {}
    cleaned = str(text or "").strip()
    if cleaned:
        style["world_context"] = cleaned
    else:
        style.pop("world_context", None)
    art_cards._write_json(assert_writable, path, style)
    return cleaned


def _world_context(library: Path | None, work: Path | None = None) -> str:
    """優先使用本書自訂的 ``world_context``，留空才退回書庫 ``story-core.md``。"""
    if work is not None:
        info = read_world_context(work, library)
        return info["world_context"] or info["fallback"]
    return _library_story_core(library)


def draft(work: Path, library: Path | None, lang: str, char_id: str, wild: dict[str, list[str]],
          enabled: list[str] | None, llm: LlmFn, hooks: RunHooks | None = None) -> dict[str, Any]:
    """請 AI 草擬外型卡。回傳 ``{card, warnings, evidence}``，不存檔；``evidence`` 是 UI 要顯示的段落（dict）。"""
    row = next((p for p in _characters(work, library, lang) if p["id"] == char_id), None)
    if row is None:
        raise ArtCardsStudioError(f"{char_id}: 找不到這個角色")
    docs = art_studio._chapter_docs(work)
    evidence = art_appearance.find_evidence(docs, row["names"], art_appearance.CHARACTER_KEYWORDS)
    character = {"name": row["label"], "summary": "", "appearance": row["card_text"]}
    current = art_cards.get_card(work, char_id) if art_cards.card_path(work, char_id).is_file() else None
    world_ctx = _world_context(library, work)
    state = {"round": 0}

    def ask(prompt: str) -> str:
        state["round"] += 1
        return call_llm(llm, [{"role": "user", "content": prompt}], hooks, state["round"]).text

    card, warnings = art_cards.draft_card(
        char_id, character, [e["text"] for e in evidence], wild, enabled, ask, current, world_context=world_ctx
    )
    if not evidence and not row["card_text"]:
        warnings = ["設定卡與內文都沒找到外觀描寫，這份是依人名、職業與身分推測的通用外觀，請自己修改"] + list(warnings)
    return {"card": card, "warnings": warnings, "evidence": evidence}


# ------------------------------------------------------------------ 出圖任務

def generation_ids(work: Path, char_id: str, headshot: bool, sprite: bool) -> list[str]:
    """這位角色要出的素材 id：頭像與立繪；表情數依角色重要度（主角各表情、主要配角只有頭像各表情、其餘只有 neutral）。"""
    ids: list[str] = []
    cast = art_gen._read_json(work / "cast.json", {})
    role = asset_manifest._role_of((cast or {}).get("characters") or {}, char_id)
    card = art_cards.get_card(work, char_id).get("expressions") or list(character_assets.DEFAULT_HEADSHOT_EXPRESSIONS)
    exprs = list(dict.fromkeys(asset_manifest.headshot_exprs_for(role, card)))
    if headshot:
        ids += [character_assets.headshot_asset_id(char_id, expr) for expr in exprs]
    if sprite:
        sprite_exprs = exprs if role in asset_manifest.SPRITE_EXPRESSION_ROLES else ["neutral"]
        ids += [f"sprite_{char_id}_{expr}" for expr in dict.fromkeys(["neutral", *sprite_exprs])]
    return ids


def run_cards_generation(config: Any, work: Path, backend: Any, *, book_id: str, lang: str, char_id: str,
                         headshot: bool, sprite: bool, candidates: int, wild: dict[str, list[str]], enabled: list[str] | None,
                         should_stop: Callable[[], bool], on_progress: Callable[[dict[str, Any]], None],
                         log: Callable[[str], None]) -> dict[str, Any]:
    """背景任務本體：為一位角色出頭像（每個勾選的表情一張素材）與立繪。

    先把提示詞裡還沒擲骰的 ``__名稱__`` 用角色種子固定並同步到畫風設定，再重掃 manifest，
    最後走素材清單的出圖流程（``art_gen.generate(only=…, regenerate=True)``）：
    每按一次「出圖」，每個素材都**再加** ``candidates`` 張新的（用新種子，舊圖保留）。回傳 ``{generated, skipped, failed, stopped}``。
    """
    if not (headshot or sprite):
        raise ArtCardsStudioError("頭像與立繪至少要勾一個")
    if not art_cards.card_path(work, char_id).is_file():
        raise ArtCardsStudioError("沒有外型卡：先存草稿或按「確認」")
    card = art_cards.get_card(work, char_id)
    resolved, warnings = art_cards.roll_card(card, wild, enabled, seed=sprite_consistency.character_seed(char_id))
    for message in warnings:
        log(f"{char_id}: {message}")
    art_cards._sync_style(config.assert_writable, work, char_id, resolved,
                          appearance_card.sprite_tags(resolved), appearance_card.headshot_tags(resolved))
    art_gen.refresh_manifest(config, work, book_id, lang)
    ids = generation_ids(work, char_id, headshot, sprite)
    report = art_gen.generate(config, work, backend, candidates=candidates, only=ids, regenerate=True,
                             should_stop=should_stop, progress=on_progress)
    for warning in report.warnings:
        log("warning: " + warning)
    return {"generated": report.generated, "skipped": report.skipped, "failed": report.failed, "stopped": report.stopped}
