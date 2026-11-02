"""每個角色固定要有的素材：頭像與立繪（S6）。

不管角色有沒有出現在場景，素材清單都固定列出「頭像」「立繪」。依角色重要度：主角立繪與頭像都依外型卡勾選的表情各一張；
主要配角（main、antagonist）頭像依勾選、立繪只有 neutral；其餘立繪與頭像都只有 neutral（預設也只有 neutral）。場景用到的立繪由 ``asset_manifest``
另外算出，這裡只補「還沒被算出來」的預設項目，``used_in`` 是空的（所以批次出圖不會
自動出，要在角色卡按出圖）。
"""
from __future__ import annotations

from typing import Any

from .asset_manifest import (ASSET_KIND_SPEC, NAME_SEPARATOR, SPRITE_EXPRESSION_ROLES, SPRITE_NAME_TEMPLATE, _character_label,
                             _role_of, headshot_exprs_for)

SKIP_CHARACTERS = ("player",)
HEADSHOT_EXPRESSIONS = ("neutral", "smile", "angry", "sad", "serious", "worried", "surprised")
DEFAULT_HEADSHOT_EXPRESSIONS = ("neutral",)
HEADSHOT_NAME_TEMPLATE = "{name}（頭像" + NAME_SEPARATOR + "{expr}）"


def headshot_asset_id(char_id: str, expr: str) -> str:
    """``headshot_<char_id>_<expr>``。"""
    return f"headshot_{char_id}_{expr}"


def character_needs(cast_doc: dict[str, Any], lang: str, taken_ids: set[str] | None = None,
                    expressions: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """替每個角色補預設素材（立繪 neutral、頭像各表情），回傳「需求」清單（格式同 ``collect_needs``）。

    ``taken_ids``：已經有的 asset_id（不重複補）；``expressions``：``{char_id: [表情,...]}``，
    沒有的角色用 :data:`DEFAULT_HEADSHOT_EXPRESSIONS`；不認得的表情略過。
    """
    taken = set(taken_ids or ())
    characters = (cast_doc or {}).get("characters")
    characters = characters if isinstance(characters, dict) else {}
    wanted = expressions or {}
    needs: list[dict[str, Any]] = []

    def add(asset_id: str, kind: str, file: str, name: str) -> None:
        if asset_id in taken:
            return
        taken.add(asset_id)
        needs.append({"asset_id": asset_id, "kind": kind, "names": {lang: name}, "file": file,
                      "spec": dict(ASSET_KIND_SPEC[kind]), "used_in": []})

    for char_id in characters:
        if char_id in SKIP_CHARACTERS:
            continue
        label = _character_label(char_id, characters, lang)
        role = _role_of(characters, char_id)
        wanted_exprs = [e for e in (wanted.get(char_id) or DEFAULT_HEADSHOT_EXPRESSIONS) if e in HEADSHOT_EXPRESSIONS]
        exprs = list(dict.fromkeys(headshot_exprs_for(role, wanted_exprs)))
        sprite_exprs = exprs if role in SPRITE_EXPRESSION_ROLES else ["neutral"]
        for expr in dict.fromkeys(["neutral", *sprite_exprs]):
            add(f"sprite_{char_id}_{expr}", "sprite", f"assets/sprite/{char_id}_{expr}.png",
                SPRITE_NAME_TEMPLATE.format(name=label, expr=expr))
        for expr in exprs:
            add(headshot_asset_id(char_id, expr), "headshot", f"assets/headshot/{char_id}_{expr}.png",
                HEADSHOT_NAME_TEMPLATE.format(name=label, expr=expr))
    return needs
