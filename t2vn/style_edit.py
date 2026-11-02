"""HTML 原型 ``style.json`` 的驗證、合併與讀寫（S4b U3）。

網頁要讓使用者安全地編輯演出設定（地點顏色與名稱、時段顏色、主角外觀、介面文字），
這個模組提供純函式：先 :func:`validate_style` 檢查、用 :func:`merge_style` 套用
局部修改、再 :func:`save_style` 寫回。不碰 HTTP、不產生預設 style。
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
SHAPES = ("tall", "broad", "small", "bent", "round")
PLACE_SECTIONS = ("locations", "times")
PLACE_COLOR_KEYS = ("c1", "c2")
UTF8 = "utf-8"


def _dump(doc: Any) -> str:
    """統一序列化格式：不跳脫中文、縮排 2、結尾換行、保持 key 順序。"""
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def _is_color(value: Any) -> bool:
    """是不是 ``#rrggbb``（大小寫皆可）。"""
    return isinstance(value, str) and COLOR_PATTERN.match(value) is not None


def _is_int_in_range(value: Any, low: int, high: int) -> bool:
    """是不是範圍內的整數（bool 不算）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return low <= value <= high


def _check_title(doc: dict[str, Any], errors: list[str]) -> None:
    """``title`` 若有必須是非空字串。"""
    if "title" not in doc:
        return
    title = doc["title"]
    if not isinstance(title, str) or not title.strip():
        errors.append(f"title：必須是非空字串（現在是 {title!r}）")


def _check_palette(doc: dict[str, Any], errors: list[str]) -> None:
    """``palette`` 若有必須是 dict，所有值都是 ``#rrggbb``。"""
    if "palette" not in doc:
        return
    palette = doc["palette"]
    if not isinstance(palette, dict):
        errors.append(f"palette：必須是物件（現在是 {type(palette).__name__}）")
        return
    for name, value in palette.items():
        if not _is_color(value):
            errors.append(
                f"palette.{name}：必須是 #rrggbb 的顏色（現在是 {value!r}）"
            )


def _check_player(doc: dict[str, Any], errors: list[str]) -> None:
    """``player`` 若有必須是 dict；``hue`` 0–359 整數；``shape`` 是固定的幾種之一。"""
    if "player" not in doc:
        return
    player = doc["player"]
    if not isinstance(player, dict):
        errors.append(f"player：必須是物件（現在是 {type(player).__name__}）")
        return
    if "hue" in player and not _is_int_in_range(player["hue"], 0, 359):
        errors.append(f"player.hue：必須是 0–359 的整數（現在是 {player['hue']!r}）")
    if "shape" in player and player["shape"] not in SHAPES:
        errors.append(
            f"player.shape：必須是 {'／'.join(SHAPES)} 之一（現在是 {player['shape']!r}）"
        )


def _check_labels(doc: dict[str, Any], errors: list[str]) -> None:
    """``labels`` 若有必須是 dict，所有值都是字串。"""
    if "labels" not in doc:
        return
    labels = doc["labels"]
    if not isinstance(labels, dict):
        errors.append(f"labels：必須是物件（現在是 {type(labels).__name__}）")
        return
    for name, value in labels.items():
        if not isinstance(value, str):
            errors.append(f"labels.{name}：必須是字串（現在是 {value!r}）")


def _check_places(doc: dict[str, Any], section: str, errors: list[str]) -> None:
    """``locations``／``times``：id 格式、值必須是物件、label 與顏色欄位。"""
    if section not in doc:
        return
    places = doc[section]
    if not isinstance(places, dict):
        errors.append(f"{section}：必須是物件（現在是 {type(places).__name__}）")
        return
    for place_id, value in places.items():
        if not isinstance(place_id, str) or ID_PATTERN.match(place_id) is None:
            errors.append(f"{section}.{place_id}：id 必須符合 [a-z0-9_]+")
        if not isinstance(value, dict):
            errors.append(f"{section}.{place_id}：必須是物件（現在是 {type(value).__name__}）")
            continue
        if "label" in value:
            label = value["label"]
            if not isinstance(label, str) or not label.strip():
                errors.append(
                    f"{section}.{place_id}.label：必須是非空字串（現在是 {label!r}）"
                )
        for color_key in PLACE_COLOR_KEYS:
            if color_key in value and not _is_color(value[color_key]):
                errors.append(
                    f"{section}.{place_id}.{color_key}："
                    f"必須是 #rrggbb 的顏色（現在是 {value[color_key]!r}）"
                )


def validate_style(doc: Any) -> list[str]:
    """回傳錯誤訊息清單（空清單＝通過），一次回報所有問題。

    規則：

    - 頂層必須是 dict；全部欄位都是選填，未知的頂層 key 允許（保留）。
    - ``title`` 若有：非空字串。
    - ``palette`` 若有：dict，所有值都是 ``#rrggbb``（大小寫皆可）。
    - ``player`` 若有：dict；``hue`` 若有必須是 0–359 的 int（不可 bool、float）；
      ``shape`` 若有必須是 tall／broad／small／bent／round 之一。
    - ``labels`` 若有：dict，所有值都是字串。
    - ``locations``、``times`` 若有：dict；key 必須符合 ``[a-z0-9_]+``；每個值必須是
      dict；``label`` 若有必須是非空字串；``c1``、``c2`` 若有必須是 ``#rrggbb``。

    每則訊息都會帶出問題欄位的路徑（例如 ``palette.bg``）。
    """
    if not isinstance(doc, dict):
        return [f"style 文件最外層必須是物件（現在是 {type(doc).__name__}）"]

    errors: list[str] = []
    _check_title(doc, errors)
    _check_palette(doc, errors)
    _check_player(doc, errors)
    _check_labels(doc, errors)
    for section in PLACE_SECTIONS:
        _check_places(doc, section, errors)
    return errors


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """遞迴合併的實作；回傳的 dict 與輸入完全不共用物件。"""
    result: dict[str, Any] = copy.deepcopy(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
            continue
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def merge_style(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """遞迴合併，回傳新 dict，不修改 ``base`` 與 ``patch``。

    兩邊都是 dict → 遞迴；其他情況（含 list）→ 用 patch 的值整個取代。
    patch 的值是 ``None`` → 刪除該 key（本來就沒有就靜默略過）。
    key 順序：``base`` 原有的 key 照原順序，patch 新增的 key 附在後面。
    """
    return _merge(base, patch)


def load_style(path: Path) -> dict[str, Any]:
    """讀 JSON：檔案不存在 → ``FileNotFoundError``；不是合法 JSON 或頂層不是 dict → ``ValueError``。"""
    text = path.read_text(encoding=UTF8)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path.name} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 最外層必須是物件")
    return data


def save_style(path: Path, doc: dict[str, Any]) -> None:
    """先 :func:`validate_style`；有錯 → ``ValueError``（訊息用 ``"; "`` 接起來），且不動原檔。

    通過才寫入：JSON ``indent=2``、``ensure_ascii=False``、結尾換行，並保持 doc 的 key 順序。
    """
    errors = validate_style(doc)
    if errors:
        raise ValueError("; ".join(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(doc), encoding=UTF8)
