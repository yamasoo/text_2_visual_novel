"""立繪去背：單色背景變透明、裁切並放進固定畫布（S6）。

角色立繪由文生圖產生，提示詞要求白色簡單背景。這個模組把「與影像邊緣相連」的單色
背景變透明（角色身上被圍住的同色區域，例如眼白，要保留），裁到角色範圍，等比縮放後
貼進 800×1600 的透明畫布（腳貼底、水平置中），存成 PNG 給 Ren'Py 用。

用法::

    python -m t2vn.sprite_cutout --src in.png --dst out.png [--tolerance 40]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

CANVAS = (800, 1600)
FILLED = 128
DEFAULT_TOLERANCE = 40
DEFAULT_MARGIN = 40
DEFAULT_FEATHER = 1
QUANTIZE_STEP = 8
ALPHA_CUTOFF = 16
CHROMA_MIN_SPREAD = 30
ENCLOSED_TOLERANCE = 7
FRINGE_SIZE = 5
GROW_SIZE = 3
MODE_EDGE = "edge"
MODE_CHROMA = "chroma"
MODES = ("auto", MODE_EDGE, MODE_CHROMA)
MAX_CHANNEL = 255
PNG_SUFFIX = ".png"
UTF8 = "utf-8"


def border_color(image: Image.Image) -> tuple[int, int, int]:
    """最外圈一格像素裡出現最多的顏色。

    每個通道先除以 8 分組再還原成組的中心值（``組 * 8 + 4``），容許輕微雜訊。
    """
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    counts: dict[tuple[int, int, int], int] = {}

    def visit(x: int, y: int) -> None:
        red, green, blue = pixels[x, y][:3]
        key = (red // QUANTIZE_STEP, green // QUANTIZE_STEP, blue // QUANTIZE_STEP)
        counts[key] = counts.get(key, 0) + 1

    for x in range(width):
        visit(x, 0)
        visit(x, height - 1)
    for y in range(1, height - 1):
        visit(0, y)
        visit(width - 1, y)

    if not counts:
        return (0, 0, 0)
    best = max(counts.items(), key=lambda item: item[1])[0]
    return (
        best[0] * QUANTIZE_STEP + QUANTIZE_STEP // 2,
        best[1] * QUANTIZE_STEP + QUANTIZE_STEP // 2,
        best[2] * QUANTIZE_STEP + QUANTIZE_STEP // 2,
    )


def background_mask(
    image: Image.Image, color: tuple[int, int, int], tolerance: int
) -> Image.Image:
    """模式 ``L``：與 ``color`` 每個通道差都 ≤ ``tolerance`` 的像素是 255，其餘 0。"""
    rgb = image.convert("RGB")
    reference = Image.new("RGB", rgb.size, tuple(color[:3]))
    difference = ImageChops.difference(rgb, reference)
    red, green, blue = difference.split()
    maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return maximum.point(lambda value: MAX_CHANNEL if value <= tolerance else 0)


def connected_background(mask: Image.Image) -> Image.Image:
    """只留下「與影像邊緣相連」的背景。

    回傳模式 ``L``：相連的背景是 255，其餘（含被角色圍住的同色區域，如眼白）是 0。
    """
    work = mask.convert("L").copy()
    width, height = work.size

    def fill(x: int, y: int) -> None:
        if work.getpixel((x, y)) == MAX_CHANNEL:
            ImageDraw.floodfill(work, (x, y), FILLED)

    for x in range(width):
        fill(x, 0)
        fill(x, height - 1)
    for y in range(1, height - 1):
        fill(0, y)
        fill(width - 1, y)

    return work.point(lambda value: MAX_CHANNEL if value == FILLED else 0)


def is_chroma(color: tuple[int, int, int]) -> bool:
    """顏色有色調（最大與最小通道差 ≥ 30）→ True。白、灰、淺色都是 False。"""
    return max(color[:3]) - min(color[:3]) >= CHROMA_MIN_SPREAD


def despill(image: Image.Image, color: tuple[int, int, int], zone: Image.Image) -> Image.Image:
    """去除背景色在邊緣的溢色，只處理 ``zone``（模式 ``L``，255 = 要處理）內的像素。

    ``color`` 的最大通道 k：把每個像素的 k 通道壓到不超過另外兩個通道的最大值。
    回傳 RGB 影像。
    """
    rgb = image.convert("RGB")
    channels = list(rgb.split())
    k = max(range(3), key=lambda i: color[i])
    others = [channels[i] for i in range(3) if i != k]
    limit = ImageChops.lighter(others[0], others[1])
    fixed = list(channels)
    fixed[k] = ImageChops.darker(channels[k], limit)
    corrected = Image.merge("RGB", fixed)
    return Image.composite(corrected, rgb, zone)


def _check_options(
    tolerance: Any, canvas: Any, margin: Any, feather: Any
) -> tuple[int, int, int]:
    """檢查數值參數，回傳 (canvas 寬, canvas 高, margin)。"""
    if isinstance(tolerance, bool) or not isinstance(tolerance, int):
        raise ValueError(f"tolerance 必須是 0–255 的整數：{tolerance!r}")
    if not 0 <= tolerance <= MAX_CHANNEL:
        raise ValueError(f"tolerance 必須是 0–255 的整數：{tolerance!r}")

    if not isinstance(canvas, (tuple, list)) or len(canvas) != 2:
        raise ValueError(f"canvas 必須是 (寬, 高)：{canvas!r}")
    width, height = canvas
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        raise ValueError(f"canvas 必須是兩個正整數：{canvas!r}")

    if isinstance(margin, bool) or not isinstance(margin, int) or margin < 0:
        raise ValueError(f"margin 必須是 0 或正整數：{margin!r}")
    if width - 2 * margin <= 0 or height - margin <= 0:
        raise ValueError(
            f"canvas {canvas!r} 減掉 margin {margin} 後沒有空間放角色"
        )

    if isinstance(feather, bool) or not isinstance(feather, int) or feather < 0:
        raise ValueError(f"feather 必須是 0 或正整數：{feather!r}")
    return width, height, margin


def cutout(
    image: Image.Image,
    tolerance: int = DEFAULT_TOLERANCE,
    canvas: tuple[int, int] = CANVAS,
    margin: int = DEFAULT_MARGIN,
    feather: int = DEFAULT_FEATHER,
    mode: str = "auto",
) -> tuple[Image.Image, dict[str, Any]]:
    """去背、裁切、縮放並貼進畫布，回傳 ``(RGBA 影像, 報告)``。

    步驟：取邊框主色當背景色 → 只把「與邊緣相連、顏色在 tolerance 內」的像素變透明
    （被角色圍住的同色區域保留）→ alpha 做 ``GaussianBlur(feather)`` 柔邊 → 裁到
    alpha > 16 的範圍 → 等比縮放到放得進 ``(canvas 寬 − 2×margin, canvas 高 − margin)``
    （可以放大）→ 貼在畫布底邊、水平置中。找不到角色（全部都是背景）→ ``ValueError``。

    ``mode``：``auto``（背景色鮮明 → ``chroma``，否則 ``edge``）、``edge``、``chroma``。
    ``chroma`` 模式：與邊緣相連的背景（容差 tolerance）加上整張圖「非常接近背景色」（容差 7）的像素都去掉（包含被角色圍住的區塊），
    背景區多長 1 px，並對邊緣 5 px 內做去溢色（:func:`despill`）。

    報告：``{"background", "removed_ratio", "subject_box", "size", "mode"}``。
    """
    canvas_width, canvas_height, margin = _check_options(
        tolerance, canvas, margin, feather
    )
    if mode not in MODES:
        raise ValueError(f"mode 必須是 {', '.join(MODES)} 之一：{mode!r}")

    color = border_color(image)
    used_mode = mode if mode != "auto" else (MODE_CHROMA if is_chroma(color) else MODE_EDGE)
    mask = background_mask(image, color, tolerance)
    if used_mode == MODE_CHROMA:
        # 與邊緣相連的用 tolerance；被圍住的區塊只收「非常接近背景色」的像素（容差 7），
        # 避免把衣服上偏背景色的陰影也吃掉。
        tight = background_mask(image, color, min(tolerance, ENCLOSED_TOLERANCE))
        merged = ImageChops.lighter(connected_background(mask), tight)
        background = merged.filter(ImageFilter.MaxFilter(GROW_SIZE))
    else:
        background = connected_background(mask)
    removed_ratio = background.histogram()[MAX_CHANNEL] / float(
        background.size[0] * background.size[1]
    )

    rgba = image.convert("RGBA")
    if used_mode == MODE_CHROMA:
        zone = background.filter(ImageFilter.MaxFilter(FRINGE_SIZE))
        rgba = despill(rgba, color, zone).convert("RGBA")
    alpha = ImageChops.invert(background)
    if feather:
        alpha = alpha.filter(ImageFilter.GaussianBlur(feather))
    rgba.putalpha(alpha)

    subject = alpha.point(lambda value: MAX_CHANNEL if value > ALPHA_CUTOFF else 0)
    subject_box = subject.getbbox()
    if subject_box is None:
        raise ValueError("no subject found: the image is all background")

    cropped = rgba.crop(subject_box)
    crop_width, crop_height = cropped.size
    available_width = canvas_width - 2 * margin
    available_height = canvas_height - margin
    scale = min(available_width / crop_width, available_height / crop_height)
    new_width = max(1, min(available_width, round(crop_width * scale)))
    new_height = max(1, min(available_height, round(crop_height * scale)))
    resized = cropped.resize((new_width, new_height), Image.LANCZOS)

    output = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    left = (canvas_width - new_width) // 2
    top = canvas_height - new_height
    output.paste(resized, (left, top), resized)

    report = {
        "background": tuple(color),
        "removed_ratio": removed_ratio,
        "subject_box": subject_box,
        "size": output.size,
        "mode": used_mode,
    }
    return output, report


def cutout_file(src: Path, dst: Path, **options: Any) -> dict[str, Any]:
    """讀 ``src``（PNG／JPG／WEBP）→ :func:`cutout` → 存成 PNG ``dst``（自動建資料夾）。

    ``src`` 不存在 → ``FileNotFoundError``；``dst`` 與 ``src`` 相同、或副檔名不是
    ``.png`` → ``ValueError``。回傳 cutout 的報告。
    """
    src_path = Path(src)
    dst_path = Path(dst)
    if not src_path.is_file():
        raise FileNotFoundError(f"找不到來源圖：{src_path}")
    if dst_path.suffix.lower() != PNG_SUFFIX:
        raise ValueError(f"輸出必須是 .png：{dst_path.name}")
    try:
        same_file = src_path.resolve() == dst_path.resolve()
    except OSError as exc:
        raise ValueError(f"無法解析路徑：{exc}") from exc
    if same_file:
        raise ValueError(f"輸出不可覆蓋來源圖：{dst_path}")

    with Image.open(src_path) as image:
        output, report = cutout(image, **options)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    output.save(dst_path, format="PNG")
    return report


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.sprite_cutout --src in.png --dst out.png [--tolerance 40]``。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="sprite_cutout.py",
        description="把立繪的單色背景變透明，裁切後貼進 800×1600 畫布。",
    )
    parser.add_argument("--src", required=True, help="來源圖（PNG／JPG／WEBP）")
    parser.add_argument("--dst", required=True, help="輸出 PNG")
    parser.add_argument(
        "--tolerance", type=int, default=DEFAULT_TOLERANCE, help="背景色容差 0–255"
    )
    parser.add_argument("--mode", choices=MODES, default="auto", help="去背模式")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return 0 if code == 0 else 1

    try:
        report = cutout_file(
            Path(args.src), Path(args.dst), tolerance=args.tolerance, mode=args.mode
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    red, green, blue = report["background"]
    percent = round(report["removed_ratio"] * 100)
    print(f"ok: background ({red}, {green}, {blue}), removed {percent}% -> {args.dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
