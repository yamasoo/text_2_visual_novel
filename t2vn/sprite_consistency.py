"""立繪一致性：同角色共用種子、出圖排序、對齊畫布（S6）。

立繪由 ComfyUI＋Anima 以文字生圖，每個角色有數個表情、各自獨立出圖，臉容易不一致。
這個模組提供三個純函式讓出圖流程改善一致性：

- :func:`character_seed`：同角色所有表情共用同一個種子（臉與姿勢才會接近）。
- :func:`split_sprite`／:func:`order_sprite_assets`：決定出圖順序（每個角色 neutral 先出）。
- :func:`align_sprites`：把同角色各表情對齊到同一個畫布與腳底線（輸入已去背）。

不呼叫 ComfyUI、不讀寫檔案、不做去背與臉部對齊。
"""
from __future__ import annotations

import hashlib
from typing import Any

from PIL import Image

EXPRESSIONS = (
    "neutral",
    "smile",
    "sad",
    "angry",
    "surprised",
    "worried",
    "serious",
    "embarrassed",
)
SEED_MODULUS = 2**31
DEFAULT_CANVAS = (768, 1344)
DEFAULT_FILL = 0.95
SPRITE_KIND = "sprite"
NEUTRAL_EXPRESSION = "neutral"
ALPHA_THRESHOLD = 8
UTF8 = "utf-8"

_SPRITE_PATH_PREFIX = "assets/sprite/"
_PNG_SUFFIX = ".png"
_EXPRESSION_ORDER = {expr: index for index, expr in enumerate(EXPRESSIONS)}


def character_seed(char_id: str, attempt: int = 0) -> int:
    """同一個角色（同一次嘗試）永遠得到同一個種子，範圍 ``0 ≤ seed < 2**31``。

    ``char_id`` 不是非空字串、``attempt`` 不是 ≥ 0 的 int（bool 也不行）→ ``ValueError``。
    """
    if not isinstance(char_id, str) or not char_id:
        raise ValueError(f"char_id 必須是非空字串：{char_id!r}")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError(f"attempt 必須是 0 或正整數：{attempt!r}")
    digest = hashlib.sha256(f"sprite:{char_id}:{attempt}".encode(UTF8)).digest()
    return int.from_bytes(digest[:8], "big") % SEED_MODULUS


def split_sprite(asset: dict) -> tuple[str, str]:
    """解析立繪素材項目，回傳 ``(char_id, expr)``；不是立繪或格式不對 → ``ValueError``。"""
    if not isinstance(asset, dict):
        raise ValueError(f"素材項目必須是物件：{asset!r}")
    if asset.get("kind") != SPRITE_KIND:
        raise ValueError(f"不是立繪項目（kind={asset.get('kind')!r}）")
    file = asset.get("file")
    if not isinstance(file, str):
        raise ValueError(f"立繪缺少 file：{asset!r}")
    if not file.startswith(_SPRITE_PATH_PREFIX) or not file.endswith(_PNG_SUFFIX):
        raise ValueError(f"立繪路徑格式不對：{file!r}")
    stem = file[len(_SPRITE_PATH_PREFIX) : -len(_PNG_SUFFIX)]
    if not stem or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in stem):
        raise ValueError(f"立繪檔名只能用 [a-z0-9_]：{file!r}")

    for expr in EXPRESSIONS:
        suffix = "_" + expr
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)], expr
    raise ValueError(f"立繪檔名結尾不是已知的表情：{file!r}")


def order_sprite_assets(assets: list[dict]) -> list[dict]:
    """依 ``(char_id, EXPRESSIONS 順序)`` 穩定排序立繪項目。

    略過不是立繪或解析失敗的項目；回傳新 list（項目本身不複製、不修改）；
    ``None`` 或空 list → ``[]``。
    """
    if not assets:
        return []

    keyed: list[tuple[tuple[str, int], dict]] = []
    for asset in assets:
        try:
            char_id, expr = split_sprite(asset)
        except ValueError:
            continue
        keyed.append(((char_id, _EXPRESSION_ORDER[expr]), asset))

    keyed.sort(key=lambda item: item[0])
    return [asset for _key, asset in keyed]


def _check_layout(canvas: tuple[int, int], fill: float, bottom_margin: int) -> tuple[int, int]:
    """檢查畫布與比例，回傳 (canvas 寬, canvas 高)。"""
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
    if isinstance(fill, bool) or not isinstance(fill, (int, float)) or not 0 < fill <= 1:
        raise ValueError(f"fill 必須介於 0（不含）與 1 之間：{fill!r}")
    if isinstance(bottom_margin, bool) or not isinstance(bottom_margin, int) or bottom_margin < 0:
        raise ValueError(f"bottom_margin 必須是 0 或正整數：{bottom_margin!r}")
    if bottom_margin >= height:
        raise ValueError(f"bottom_margin 不可大於等於畫布高度：{bottom_margin}")
    return width, height


def _opaque_box(image: Image.Image) -> tuple[int, int, int, int] | None:
    """alpha > 8 的不透明外框；全透明回傳 None。"""
    mask = image.getchannel("A").point(
        lambda value: 255 if value > ALPHA_THRESHOLD else 0
    )
    return mask.getbbox()


def align_sprites(
    images: dict[str, Image.Image],
    canvas: tuple[int, int] = DEFAULT_CANVAS,
    fill: float = DEFAULT_FILL,
    bottom_margin: int = 0,
) -> dict[str, Image.Image]:
    """把同一角色的各表情對齊到同一個畫布與腳底線，回傳新 dict。

    參考圖＝``neutral``，沒有就取 key 排序後第一個；縮放比例用參考圖算一次，所有圖
    共用（不要各自撐滿）。每張圖先裁到自己的不透明範圍（alpha > 8），再用同一個比例
    縮放、水平置中、底邊貼在 ``canvas 高 − bottom_margin``。全透明或空 dict 的行為見
    卡片：空 dict → ``{}``；參考圖全透明 → ``ValueError``。
    """
    if not images:
        return {}
    width, height = _check_layout(canvas, fill, bottom_margin)

    reference_key = NEUTRAL_EXPRESSION if NEUTRAL_EXPRESSION in images else sorted(images)[0]
    reference_box = _opaque_box(images[reference_key].convert("RGBA"))
    if reference_box is None:
        raise ValueError(f"參考圖 {reference_key!r} 全透明，找不到角色範圍")

    reference_width = reference_box[2] - reference_box[0]
    reference_height = reference_box[3] - reference_box[1]
    scale = min(
        height * fill / reference_height,
        width * fill / reference_width,
    )

    aligned: dict[str, Image.Image] = {}
    for key, image in images.items():
        rgba = image.convert("RGBA")
        box = _opaque_box(rgba)
        if box is None:
            raise ValueError(f"{key!r} 全透明，找不到角色範圍")
        cropped = rgba.crop(box)
        new_width = max(1, round(cropped.width * scale))
        new_height = max(1, round(cropped.height * scale))
        resized = cropped.resize((new_width, new_height), Image.LANCZOS)

        output = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        left = (width - new_width) // 2
        top = height - bottom_margin - new_height
        output.paste(resized, (left, top))
        aligned[key] = output
    return aligned
