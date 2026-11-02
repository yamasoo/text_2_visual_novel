"""佔位素材產生器：缺的背景與立繪用簡單圖補上（S5 匯出周邊）。

Ren'Py 匯出會寫出 ``game/vn_export.json``，列出遊戲需要的背景與立繪檔。還沒有美術時，
遊戲要能照樣完整玩過，所以這個模組把缺的背景畫成漸層圖、缺的立繪畫成有色剪影。
**已存在的檔（真素材）絕不覆蓋**，只有「紀錄檔裡登記為佔位」的檔案才會被重畫。

用法::

    python -m t2vn.placeholders --game <專案>/game [--overwrite]
"""
from __future__ import annotations

import argparse
import colorsys
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

RECORD_FILE = "images/placeholders.json"
BG_SIZE = (1920, 1080)
SPRITE_SIZE = (800, 1600)
SHAPES = ("tall", "broad", "small", "bent", "round")

# 每個時段的上、下兩個漸層顏色（互不相同；夜晚偏暗）。
TIME_COLORS: dict[str, tuple[str, str]] = {
    "dawn": ("#1b2533", "#6d7f90"),
    "morning": ("#587a9c", "#c8d9e6"),
    "day": ("#2c4258", "#7f97a8"),
    "dusk": ("#3a2a3f", "#c07a52"),
    "night": ("#05070c", "#141d2a"),
}

# 頭部與身體的幾何（800×1600 畫布）；五種體型的面積互不相同。
HEAD_BOX = (300, 150, 500, 400)
BODY_TOP = 430
BODY_BOTTOM = 1450
BODY_POLYGONS: dict[str, tuple[tuple[int, int], ...]] = {
    "tall": ((345, 430), (455, 430), (470, 1450), (330, 1450)),
    "broad": ((250, 430), (550, 430), (620, 1450), (180, 1450)),
    "small": ((325, 470), (475, 470), (475, 1250), (325, 1250)),
    "bent": (
        (350, 430),
        (450, 430),
        (520, 1100),
        (560, 1450),
        (240, 1450),
        (330, 1100),
    ),
}
ROUND_BODY_BOX = (250, 430, 550, 1400)
TEXT_FILL_BG = (236, 242, 248)
HUE_LIGHTNESS = 0.45
HUE_SATURATION = 0.55
UTF8 = "utf-8"


# ---------------------------------------------------------------------- 小工具


def _parse_color(text: str) -> tuple[int, int, int]:
    """``#rrggbb`` → (r, g, b)。"""
    value = text.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _dump(data: Any) -> str:
    """統一序列化格式：不跳脫中文、縮排 2、結尾換行。"""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _load_font(size: int) -> ImageFont.ImageFont:
    """預設字型；舊版 Pillow 不支援 size 參數時退回無參數版本。"""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    size: int,
    fill: Any,
    centered: bool = False,
) -> None:
    """畫一段字；字型或字元不支援時安靜略過（佔位圖不需要漂亮）。"""
    if not text:
        return
    try:
        font = _load_font(size)
        if centered:
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
            x = xy[0] - (right - left) // 2 - left
            y = xy[1] - (bottom - top) // 2 - top
        else:
            x, y = xy
        draw.text((x, y), text, font=font, fill=fill)
    except (OSError, UnicodeEncodeError, ValueError):
        return


# ------------------------------------------------------------------------ 單張圖


def render_background(
    asset_id: str, time_of_day: str, label_ascii: str = ""
) -> Image.Image:
    """1920×1080 RGB 的上下漸層背景，圖中央寫 ``asset_id``。

    顏色取自 :data:`TIME_COLORS`；``time_of_day`` 不認得 → ``ValueError``。
    """
    if time_of_day not in TIME_COLORS:
        raise ValueError(
            f"不認得的 time_of_day：{time_of_day!r}"
            f"（可用：{', '.join(sorted(TIME_COLORS))}）"
        )

    top = _parse_color(TIME_COLORS[time_of_day][0])
    bottom = _parse_color(TIME_COLORS[time_of_day][1])
    width, height = BG_SIZE

    column = Image.new("RGB", (1, height))
    pixels = []
    for y in range(height):
        ratio = y / (height - 1) if height > 1 else 0.0
        pixels.append(
            tuple(round(start + (end - start) * ratio) for start, end in zip(top, bottom))
        )
    column.putdata(pixels)
    image = column.resize((width, height), Image.NEAREST)

    draw = ImageDraw.Draw(image)
    center = (width // 2, height // 2)
    _draw_text(draw, asset_id, center, 56, TEXT_FILL_BG, centered=True)
    if label_ascii:
        _draw_text(
            draw,
            label_ascii,
            (center[0], center[1] + 90),
            36,
            TEXT_FILL_BG,
            centered=True,
        )
    return image


def _validate_hue(hue: Any) -> int:
    """``hue`` 必須是 0–359 的整數（不接受 bool、float）。"""
    if isinstance(hue, bool) or not isinstance(hue, int):
        raise ValueError(f"hue 必須是 0–359 的整數：{hue!r}")
    if not 0 <= hue <= 359:
        raise ValueError(f"hue 必須是 0–359 的整數：{hue!r}")
    return hue


def _hue_color(hue: int) -> tuple[int, int, int, int]:
    """hue → 剪影顏色（HLS：亮度 0.45、飽和度 0.55）。"""
    red, green, blue = colorsys.hls_to_rgb(
        hue / 360.0, HUE_LIGHTNESS, HUE_SATURATION
    )
    return (
        int(round(red * 255)),
        int(round(green * 255)),
        int(round(blue * 255)),
        255,
    )


def render_sprite(char_id: str, expr: str, hue: int, shape: str) -> Image.Image:
    """800×1600 RGBA、透明底的剪影：頭（橢圓）＋身體（依 ``shape`` 不同）。

    顏色由 ``hue``（0–359）換算，頭旁邊寫 ``expr``。
    ``shape`` 不在 :data:`SHAPES`、``hue`` 不是 0–359 的整數 → ``ValueError``。
    """
    if shape not in SHAPES:
        raise ValueError(f"不認得的 shape：{shape!r}（可用：{', '.join(SHAPES)}）")
    color = _hue_color(_validate_hue(hue))

    image = Image.new("RGBA", SPRITE_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if shape == "round":
        draw.ellipse(ROUND_BODY_BOX, fill=color)
    else:
        draw.polygon(list(BODY_POLYGONS[shape]), fill=color)
    draw.ellipse(HEAD_BOX, fill=color)

    head_right = HEAD_BOX[2]
    _draw_text(draw, expr, (head_right + 20, HEAD_BOX[1] + 30), 40, color)
    _draw_text(
        draw,
        char_id,
        (SPRITE_SIZE[0] // 2, BODY_BOTTOM + 70),
        36,
        color,
        centered=True,
    )
    return image


# ---------------------------------------------------------------- 路徑與清單處理


def _relative_png(raw: Any) -> str:
    """驗證 ``file`` 是安全的相對 ``.png`` 路徑，回傳正規化的字串。"""
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"file 必須是相對路徑字串：{raw!r}")
    if "\\" in raw:
        raise ValueError(f"file 不可含反斜線：{raw!r}")
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise ValueError(f"file 不可是絕對路徑：{raw!r}")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"file 不可含空段落或 '..'：{raw!r}")
    if not raw.endswith(".png"):
        raise ValueError(f"file 的副檔名必須是 .png：{raw!r}")
    return "/".join(parts)


def _entry_list(export: dict[str, Any], key: str) -> list[Any]:
    """取 export 裡的清單欄位；沒有就當空清單。"""
    value = export.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"vn_export.json 的 {key} 必須是清單")
    return value


def _validate_background(item: Any) -> str:
    """驗證一筆背景項目，回傳安全的相對路徑。"""
    if not isinstance(item, dict):
        raise ValueError("backgrounds 的項目必須是物件")
    rel_path = _relative_png(item.get("file"))
    asset_id = item.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id:
        raise ValueError(f"背景缺少 asset_id：{rel_path}")
    time_of_day = item.get("time_of_day")
    if time_of_day not in TIME_COLORS:
        raise ValueError(f"背景 {asset_id} 的 time_of_day 不認得：{time_of_day!r}")
    return rel_path


def _validate_sprite(item: Any) -> str:
    """驗證一筆立繪項目，回傳安全的相對路徑。"""
    if not isinstance(item, dict):
        raise ValueError("sprites 的項目必須是物件")
    rel_path = _relative_png(item.get("file"))
    char_id = item.get("char_id")
    if not isinstance(char_id, str) or not char_id:
        raise ValueError(f"立繪缺少 char_id：{rel_path}")
    if item.get("shape") not in SHAPES:
        raise ValueError(f"立繪 {char_id} 的 shape 不認得：{item.get('shape')!r}")
    _validate_hue(item.get("hue"))
    return rel_path


def _load_record(path: Path) -> set[str]:
    """讀舊紀錄（讀不到就當空集合）；只保留安全的路徑。"""
    try:
        data = json.loads(path.read_text(encoding=UTF8))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    files = data.get("files")
    if not isinstance(files, list):
        return set()
    known: set[str] = set()
    for item in files:
        try:
            known.add(_relative_png(item))
        except ValueError:
            continue
    return known


def _dest_path(game_dir: Path, rel_path: str) -> Path:
    """相對路徑 → 實際檔案路徑。"""
    return game_dir.joinpath(*rel_path.split("/"))


def _write_png(dest: Path, kind: str, item: dict[str, Any]) -> None:
    """畫出並寫入一張佔位圖。"""
    if kind == "background":
        image = render_background(
            str(item["asset_id"]),
            str(item["time_of_day"]),
            str(item.get("label_ascii") or ""),
        )
    else:
        image = render_sprite(
            str(item["char_id"]),
            str(item.get("expr") or ""),
            int(item["hue"]),
            str(item["shape"]),
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(dest, format="PNG")


def make_placeholders(
    export: dict[str, Any], game_dir: Path, overwrite: bool = False
) -> dict[str, list[str]]:
    """依 ``vn_export.json`` 的內容，在 ``game_dir`` 底下補齊缺的背景與立繪 PNG。

    只處理 ``export["backgrounds"]`` 與 ``export["sprites"]``；``file`` 是相對
    ``game_dir`` 的 ``/`` 路徑（絕對路徑、含 ``..`` 或反斜線、副檔名不是 ``.png``
    一律 ``ValueError``，而且什麼都不寫）。已存在的檔不動，列在 ``skipped``；
    ``overwrite=True`` 時只重畫紀錄檔裡登記為佔位的檔案。新建的列在 ``created``
    （照 export 順序）。最後寫 ``game_dir/images/placeholders.json``，記錄所有仍然
    存在的佔位圖（含以前登記的）。
    """
    if not isinstance(export, dict):
        raise ValueError("export 必須是 dict")
    backgrounds = _entry_list(export, "backgrounds")
    sprites = _entry_list(export, "sprites")

    # 先驗證全部項目，任何錯誤都在寫檔前丟出。
    planned: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for item in backgrounds:
        rel_path = _validate_background(item)
        if rel_path in seen:
            continue
        seen.add(rel_path)
        planned.append(("background", rel_path, item))
    for item in sprites:
        rel_path = _validate_sprite(item)
        if rel_path in seen:
            continue
        seen.add(rel_path)
        planned.append(("sprite", rel_path, item))

    game_path = Path(game_dir)
    record_path = _dest_path(game_path, RECORD_FILE)
    previous = _load_record(record_path)
    created: list[str] = []
    skipped: list[str] = []

    for kind, rel_path, item in planned:
        dest = _dest_path(game_path, rel_path)
        if dest.exists():
            if overwrite and rel_path in previous:
                _write_png(dest, kind, item)
                created.append(rel_path)
            else:
                skipped.append(rel_path)
            continue
        _write_png(dest, kind, item)
        created.append(rel_path)

    record_files = sorted(
        rel_path
        for rel_path in previous | set(created)
        if _dest_path(game_path, rel_path).is_file()
    )
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(_dump({"files": record_files}), encoding=UTF8)
    return {"created": created, "skipped": skipped}


# -------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.placeholders --game <專案>/game [--overwrite]``。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="placeholders.py",
        description="依 vn_export.json 補齊缺少的背景與立繪佔位圖。",
    )
    parser.add_argument("--game", required=True, help="Ren'Py 專案的 game/ 資料夾")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="重畫紀錄檔裡登記為佔位的檔案（真素材仍不動）",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return 0 if code == 0 else 1

    game_dir = Path(args.game)
    export_path = game_dir / "vn_export.json"
    if not game_dir.is_dir():
        print(f"error: 找不到資料夾 {game_dir}", file=sys.stderr)
        return 1
    if not export_path.is_file():
        print(f"error: 找不到 {export_path}", file=sys.stderr)
        return 1
    try:
        export = json.loads(export_path.read_text(encoding=UTF8))
    except (OSError, ValueError) as exc:
        print(f"error: 讀不了 {export_path.name}：{exc}", file=sys.stderr)
        return 1
    if not isinstance(export, dict):
        print(f"error: {export_path.name} 最外層必須是物件", file=sys.stderr)
        return 1

    try:
        result = make_placeholders(export, game_dir, overwrite=args.overwrite)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"created {len(result['created'])}, skipped {len(result['skipped'])}"
        f" -> {_dest_path(game_dir, RECORD_FILE)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
