"""外型卡後端邏輯 v2：卡片、擲骰、草擬、畫風庫（S6）。

「外觀頁」的後端邏輯層（HTTP 由別人接）：外型卡的清單與存檔、wildcard 啟用清單、擲骰、
AI 草擬、畫風庫匯入與套用。所有對外部的依賴（寫檔權限檢查、AI）都由參數傳入，所以能用
假物件完整測試。

v2 把頭像／立繪的出圖與候選圖全部移到「素材清單」模組，這裡只剩卡片本身：外型卡是
**兩個提示詞**（頭像＝頸部以上、立繪＝頸部以下）加頭像表情勾選。
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any, Callable, Iterable

from . import appearance_card, appearance_draft, art_styles, wildcards

ENABLED_FILE = "wildcards_enabled.json"
SKIP_CHARACTERS = ("player",)
CONFIRM_NEEDS_CONTENT = "至少填一個提示詞才能確認"
APPEARANCE_DIR = "appearance"
STYLE_FILE = "style.json"
ART_DIR_NAME = "art"
SEED_LIMIT = 2**31
UTF8 = "utf-8"


class ArtCardsError(Exception):
    """外型卡相關的可預期錯誤（訊息可以直接顯示給使用者）。"""


# ------------------------------------------------------------------ 路徑與讀寫


def art_dir(work: Path) -> Path:
    """``work/art``。"""
    return Path(work) / ART_DIR_NAME


def _check_id(char_id: Any) -> str:
    """``char_id`` 必須符合外型卡的規則。"""
    if not isinstance(char_id, str) or appearance_card.CHAR_ID.match(char_id) is None:
        raise ArtCardsError(f"角色 id 不合法：{char_id!r}")
    return char_id


def card_path(work: Path, char_id: str) -> Path:
    """外型卡路徑：``work/art/appearance/<char_id>.json``。"""
    return art_dir(work) / APPEARANCE_DIR / f"{_check_id(char_id)}.json"


def _enabled_path(work: Path) -> Path:
    """wildcard 啟用清單路徑。"""
    return art_dir(work) / ENABLED_FILE


def _style_path(work: Path) -> Path:
    """本書畫風設定的路徑。"""
    return art_dir(work) / STYLE_FILE


def _read_json(path: Path, default: Any) -> Any:
    """讀 JSON：檔案不存在用預設值；壞檔 → ``ArtCardsError``。"""
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding=UTF8))
    except (OSError, ValueError) as exc:
        raise ArtCardsError(f"讀不了 {path.name}：{exc}") from exc


def _write_json(assert_writable: Callable[[Path], Any], path: Path, data: Any) -> None:
    """先問權限、建父資料夾，再寫 JSON（UTF-8、``indent=2``、結尾換行）。"""
    assert_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding=UTF8
    )


# ---------------------------------------------------------------- wildcard 啟用


def load_enabled(work: Path) -> list[str] | None:
    """讀啟用清單：檔案不存在 → ``None``（＝全部啟用）；格式不對 → ``ArtCardsError``。"""
    path = _enabled_path(work)
    if not path.is_file():
        return None
    data = _read_json(path, None)
    if not isinstance(data, dict) or not isinstance(data.get("enabled"), list):
        raise ArtCardsError(f"{ENABLED_FILE} 格式不對")
    return [name for name in data["enabled"] if isinstance(name, str)]


def save_enabled(
    assert_writable: Callable[[Path], Any],
    work: Path,
    names: Iterable[str],
    library: dict[str, list[str]],
) -> list[str]:
    """只留 library 內的名稱、去重、排序後存檔並回傳。"""
    kept = sorted({name for name in names if name in library})
    _write_json(assert_writable, _enabled_path(work), {"enabled": kept})
    return kept


def usable_library(
    library: dict[str, list[str]], enabled: list[str] | None
) -> dict[str, list[str]]:
    """只剩啟用的 wildcard；``enabled`` 為 ``None`` 表示全部啟用（回傳新 dict）。"""
    if enabled is None:
        return dict(library)
    allowed = set(enabled)
    return {name: options for name, options in library.items() if name in allowed}


def wildcard_overview(
    library: dict[str, list[str]], enabled: list[str] | None
) -> list[dict[str, Any]]:
    """wildcard 摘要，每一項再加 ``enabled``；``enabled`` 為 ``None`` 時全部為 True。"""
    allowed = None if enabled is None else set(enabled)
    rows = wildcards.describe_library(library)
    for row in rows:
        row["enabled"] = allowed is None or row["name"] in allowed
    return rows


# -------------------------------------------------------------------- 外型卡


def get_card(work: Path, char_id: str) -> dict[str, Any]:
    """讀外型卡；沒有檔案就是空卡。"""
    path = card_path(work, char_id)
    if path.is_file():
        try:
            return appearance_card.load_card(path)
        except ValueError as exc:
            raise ArtCardsError(f"讀不了 {path.name}：{exc}") from exc
    return appearance_card.new_card(char_id)


def list_cards(
    work: Path,
    characters: dict[str, Any],
    labels: dict[str, str] | None = None,
    card_texts: dict[str, str] | None = None,
    assets: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """角色清單（依 ``characters`` 順序、跳過 player 與不合法 id）。

    每項有 ``id``、``label``、``card_text``、``card``、``has_card`` 與 ``assets``
    （``assets.get(id)`` 的淺拷貝；沒有就空清單）。
    """
    label_table = labels or {}
    text_table = card_texts or {}
    asset_table = assets or {}
    rows: list[dict[str, Any]] = []
    for char_id in characters or {}:
        if char_id in SKIP_CHARACTERS:
            continue
        if not isinstance(char_id, str) or appearance_card.CHAR_ID.match(char_id) is None:
            continue
        rows.append(
            {
                "id": char_id,
                "label": label_table.get(char_id) or char_id,
                "card_text": text_table.get(char_id) or "",
                "card": get_card(work, char_id),
                "has_card": card_path(work, char_id).is_file(),
                "assets": list(asset_table.get(char_id) or []),
            }
        )
    return rows


def _sync_style(
    assert_writable: Callable[[Path], Any],
    work: Path,
    char_id: str,
    card: dict[str, Any],
    tags: str,
    head: str,
) -> None:
    """把外型卡的標籤同步進 ``work/art/style.json`` 的角色設定。"""
    path = _style_path(work)
    style = _read_json(path, {})
    if not isinstance(style, dict):
        style = {}
    characters = style.get("characters")
    if not isinstance(characters, dict):
        characters = {}
    if not tags and not head and char_id not in characters:
        return

    entry = characters.get(char_id)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry["tags"] = tags
    entry["headshot_tags"] = head
    entry.setdefault("description", "")
    entry.setdefault("notes", "")
    entry["confirmed"] = bool(card.get("confirmed"))
    characters[char_id] = entry
    style["characters"] = characters
    _write_json(assert_writable, path, style)


def save_card(
    assert_writable: Callable[[Path], Any],
    work: Path,
    char_id: str,
    data: Any,
) -> dict[str, Any]:
    """存外型卡（網址的 ``char_id`` 優先），並同步 ``style.json`` 的角色標籤。

    ``confirmed`` 為真但兩個提示詞都沒有可用標籤 → ``ArtCardsError``（不寫任何檔）。
    """
    _check_id(char_id)
    try:
        card = appearance_card.normalize_card(data, char_id)
    except ValueError as exc:
        raise ArtCardsError(str(exc)) from exc

    tags = appearance_card.sprite_tags(card)
    head = appearance_card.headshot_tags(card)
    if card["confirmed"] and not tags:
        raise ArtCardsError(CONFIRM_NEEDS_CONTENT)

    path = card_path(work, char_id)
    assert_writable(path)
    appearance_card.save_card(card, path)
    _sync_style(assert_writable, work, char_id, card, tags, head)
    return card


# ------------------------------------------------------------------------ 擲骰


def roll_card(
    card: dict[str, Any],
    library: dict[str, list[str]],
    enabled: list[str] | None,
    seed: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """把外型卡兩個提示詞裡的 ``__名稱__`` 用（啟用中的）wildcard 擲骰固定。"""
    usable = usable_library(library or {}, enabled)
    used_seed = secrets.randbelow(SEED_LIMIT) if seed is None else seed

    def expand(text: str, expand_seed: Any) -> tuple[str, list[str]]:
        return wildcards.expand(text, usable, expand_seed)

    try:
        return appearance_card.resolve_card(card, expand, used_seed)
    except ValueError as exc:
        raise ArtCardsError(str(exc)) from exc


# -------------------------------------------------------------------- AI 草擬


def draft_card(
    char_id: str,
    character: dict[str, Any] | None,
    evidence: list[str],
    library: dict[str, list[str]],
    enabled: list[str] | None,
    llm: Callable[[str], str],
    current: dict[str, Any] | None = None,
    retries: int = 1,
) -> tuple[dict[str, Any], list[str]]:
    """請 AI 草擬外型卡；回覆格式不對時重問，最多重問 ``retries`` 次。

    回傳的新卡保留 ``current`` 的表情勾選與備註（``current`` 不是 dict 就用預設）。
    """
    _check_id(char_id)
    usable = usable_library(library or {}, enabled)
    base = appearance_draft.build_prompt(
        character, evidence, wildcards.describe_library(usable), current
    )
    prompt = base
    last_error = ""
    for _attempt in range(max(0, retries) + 1):
        reply = llm(prompt)
        try:
            fields, warns = appearance_draft.parse_reply(reply, sorted(usable))
        except ValueError as exc:
            last_error = str(exc)
            prompt = (
                base
                + f"\n\nYour previous reply was rejected: {last_error}."
                " Reply again with only the JSON object."
            )
            continue
        if not (fields["headshot_prompt"] or fields["sprite_prompt"]):
            last_error = "兩個提示詞都是空的"
            prompt = (
                base
                + "\n\nYour previous reply left both prompts empty, which is not allowed."
                " Infer a plausible generic appearance from the name, occupation and role,"
                " and reply again with only the JSON object."
            )
            continue

        expressions = current.get("expressions") if isinstance(current, dict) else None
        notes = current.get("notes", "") if isinstance(current, dict) else ""
        card = appearance_card.normalize_card(
            {
                "char_id": char_id,
                "confirmed": False,
                "headshot_prompt": fields["headshot_prompt"],
                "sprite_prompt": fields["sprite_prompt"],
                "expressions": expressions,
                "notes": notes,
            },
            char_id,
        )
        return card, warns
    raise ArtCardsError(f"AI 的回覆格式不對（{last_error}），請再試一次")


# -------------------------------------------------------------------- 畫風庫


def import_styles(
    assert_writable: Callable[[Path], Any], xlsx_path: str | Path, dest: str | Path
) -> int:
    """把畫風 xlsx 匯入成 JSON 目錄（寫到 ``dest``），回傳款數。"""
    source = Path(xlsx_path)
    if not source.is_file():
        raise ArtCardsError(f"找不到檔案：{xlsx_path}")
    try:
        catalog = art_styles.parse_styles_xlsx(source)
    except ValueError as exc:
        raise ArtCardsError(str(exc)) from exc
    target = Path(dest)
    assert_writable(target)
    art_styles.save_catalog(catalog, target)
    return len(catalog)


def style_state(work: Path) -> dict[str, str]:
    """目前套用的畫風（沒有就空字串）。"""
    style = _read_json(_style_path(work), {})
    applied = style.get("style_applied") if isinstance(style, dict) else None
    style_id = applied.get("style_id") if isinstance(applied, dict) else ""
    return {"style_id": style_id if isinstance(style_id, str) else ""}


def apply_style(
    assert_writable: Callable[[Path], Any],
    work: Path,
    catalog: list[dict[str, Any]],
    style_id: str,
) -> dict[str, Any]:
    """套用一款畫風到 ``work/art/style.json``，回傳 ``{"style_id", "dropped"}``。"""
    try:
        style = art_styles.get_style(catalog, style_id)
    except KeyError as exc:
        raise ArtCardsError(f"找不到畫風：{style_id}") from exc

    path = _style_path(work)
    art = _read_json(path, {})
    if not isinstance(art, dict):
        art = {}
    updated, dropped = art_styles.apply_style(art, style)
    _write_json(assert_writable, path, updated)
    return {"style_id": style_id, "dropped": dropped}


def clear_style(assert_writable: Callable[[Path], Any], work: Path) -> dict[str, str]:
    """清除目前套用的畫風；``style.json`` 不存在時不建立任何檔案。"""
    path = _style_path(work)
    if path.is_file():
        art = _read_json(path, {})
        if not isinstance(art, dict):
            art = {}
        _write_json(assert_writable, path, art_styles.clear_style(art))
    return {"style_id": ""}
