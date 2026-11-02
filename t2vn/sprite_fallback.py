"""表情圖退回規則（S6）：立繪或頭像缺某個表情時退回 neutral，並產生 Ren'Py 的 image 宣告。

純函式；只有 :func:`scan_available` 會讀資料夾（只讀、不寫）。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

NEUTRAL = "neutral"
KNOWN_EXPRESSIONS = (
    "neutral",
    "smile",
    "angry",
    "sad",
    "serious",
    "worried",
    "surprised",
    "embarrassed",
    "laughing",
)
ID_RE = re.compile(r"^[a-z0-9_]+$")
SPRITE_DIR = "images/sprite"
HEADSHOT_DIR = "images/headshot"
PNG_SUFFIX = ".png"


def _check_id(value: object, name: str) -> str:
    """檢查 id／表情字串只含 ``[a-z0-9_]``，否則 ``ValueError``。"""
    if not isinstance(value, str) or ID_RE.match(value) is None:
        raise ValueError(f"{name} 不合法：{value!r}")
    return value


def pick_expr(expr: str, available: Iterable[str]) -> str | None:
    """要用哪個表情的圖：有 ``expr`` 就用它；沒有但有 ``neutral`` 就退回 ``neutral``；都沒有回 ``None``。"""
    options = set(available)
    if expr in options:
        return expr
    if NEUTRAL in options:
        return NEUTRAL
    return None


def _fallback(expr: str, chosen: str | None) -> bool:
    """是不是退回了別的表情（``None`` 不算退回，因為根本沒有圖）。"""
    return chosen is not None and chosen != expr


def plan_images(
    pairs: Iterable[tuple[str, str]],
    sprites: dict[str, Iterable[str]],
    headshots: dict[str, Iterable[str]],
) -> list[dict[str, object]]:
    """每個 ``(char_id, expr)`` 規劃立繪與頭像要用哪一張。

    ``pairs`` 依出現順序、重複的只留第一次；``expr`` 為空字串當 ``neutral``。
    ``sprites``／``headshots`` 是 ``{char_id: 有圖的表情集合}``，缺角色當沒有圖。
    回傳每項 ``{"char_id", "expr", "sprite_expr", "headshot_expr", "sprite_fallback",
    "headshot_fallback"}``；``char_id`` 或 ``expr`` 不合法 → ``ValueError``。
    """
    sprite_sets = {key: set(value) for key, value in sprites.items()}
    headshot_sets = {key: set(value) for key, value in headshots.items()}

    plan: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for char_id, expr in pairs:
        _check_id(char_id, "char_id")
        wanted = expr or NEUTRAL
        _check_id(wanted, "expr")
        key = (char_id, wanted)
        if key in seen:
            continue
        seen.add(key)

        sprite_expr = pick_expr(wanted, sprite_sets.get(char_id, ()))
        headshot_expr = pick_expr(wanted, headshot_sets.get(char_id, ()))
        plan.append(
            {
                "char_id": char_id,
                "expr": wanted,
                "sprite_expr": sprite_expr,
                "headshot_expr": headshot_expr,
                "sprite_fallback": _fallback(wanted, sprite_expr),
                "headshot_fallback": _fallback(wanted, headshot_expr),
            }
        )
    return plan


def image_lines(plan: list[dict[str, object]]) -> list[str]:
    """Ren'Py ``image`` 宣告。

    每項先一行立繪：``image <char_id> <expr> = "images/sprite/<char_id>_<用的表情>.png"``；
    沒有任何立繪圖（``sprite_expr`` 為 ``None``）時檔名用原本的 ``expr``（讓佔位圖機制補圖）。
    有頭像時再一行：``image side <char_id> <expr> = "images/headshot/<char_id>_<用的表情>.png"``。
    """
    lines: list[str] = []
    for item in plan:
        char_id = item["char_id"]
        expr = item["expr"]
        sprite_expr = item["sprite_expr"] or expr
        lines.append(
            f'image {char_id} {expr} = "{SPRITE_DIR}/{char_id}_{sprite_expr}{PNG_SUFFIX}"'
        )
        headshot_expr = item["headshot_expr"]
        if headshot_expr is not None:
            lines.append(
                f'image side {char_id} {expr} = "{HEADSHOT_DIR}/{char_id}_{headshot_expr}{PNG_SUFFIX}"'
            )
    return lines


def needed_files(plan: list[dict[str, object]]) -> list[str]:
    """實際要複製進 ``game/`` 的檔案（相對 ``game/``）：只含有圖的，去重、保持順序。"""
    files: list[str] = []
    for item in plan:
        char_id = item["char_id"]
        sprite_expr = item["sprite_expr"]
        headshot_expr = item["headshot_expr"]
        if sprite_expr is not None:
            files.append(f"{SPRITE_DIR}/{char_id}_{sprite_expr}{PNG_SUFFIX}")
        if headshot_expr is not None:
            files.append(f"{HEADSHOT_DIR}/{char_id}_{headshot_expr}{PNG_SUFFIX}")

    unique: list[str] = []
    seen: set[str] = set()
    for path in files:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _expression_of(stem: str) -> tuple[str, str] | None:
    """``<char_id>_<expr>`` → ``(char_id, expr)``；沒有已知表情結尾或 id 為空回 ``None``。"""
    best: tuple[str, str] | None = None
    for expr in KNOWN_EXPRESSIONS:
        suffix = "_" + expr
        if stem.endswith(suffix) and len(stem) > len(suffix):
            candidate = (stem[: -len(suffix)], expr)
            if best is None or len(candidate[1]) > len(best[1]):
                best = candidate
    return best


def _scan_folder(folder: Path) -> dict[str, set[str]]:
    """掃一個資料夾的 ``<char_id>_<expr>.png``，回傳 ``{char_id: {expr}}``。"""
    found: dict[str, set[str]] = {}
    if not folder.is_dir():
        return found
    for path in folder.iterdir():
        if not path.is_file() or path.suffix != PNG_SUFFIX:
            continue
        parsed = _expression_of(path.stem)
        if parsed is None:
            continue
        char_id, expr = parsed
        found.setdefault(char_id, set()).add(expr)
    return found


def scan_available(
    assets_root: Path,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """掃 ``assets/sprite``、``assets/headshot`` 底下的 ``<char_id>_<expr>.png``。

    回傳 ``(sprites, headshots)``，都是 ``{char_id: {expr}}``。檔名要以 ``_<已知表情>``
    結尾（角色 id 本身可含底線，取最後一個符合的表情）；不符的略過；資料夾不存在給空 dict。
    """
    root = Path(assets_root)
    return (
        _scan_folder(root / Path(SPRITE_DIR).name),
        _scan_folder(root / Path(HEADSHOT_DIR).name),
    )
