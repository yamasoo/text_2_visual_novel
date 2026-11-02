import random

import pytest
from PIL import Image, ImageDraw

from t2vn.sprite_cutout import CANVAS, border_color, cutout, cutout_file, despill, is_chroma, main


def make(bg=(255, 255, 255), size=(400, 800), noise=0, seed=1):
    image = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 200, 299, 799], fill=(200, 30, 30))      # 身體，貼到底邊
    draw.ellipse([140, 80, 260, 200], fill=(200, 30, 30))          # 頭
    draw.rectangle([180, 400, 219, 439], fill=bg)                  # 身體裡被圍住的「背景色」小洞（例如眼白）
    if noise:
        rng = random.Random(seed)
        pixels = image.load()
        for x in range(size[0]):
            for y in range(size[1]):
                if pixels[x, y] == bg:
                    pixels[x, y] = tuple(max(0, min(255, c + rng.randint(-noise, noise))) for c in bg)
    return image


def test_border_color_is_dominant_background():
    assert all(abs(a - b) <= 8 for a, b in zip(border_color(make()), (255, 255, 255)))
    assert all(abs(a - b) <= 8 for a, b in zip(border_color(make(bg=(0, 200, 0))), (0, 200, 0)))


def test_white_background_removed_subject_kept_hole_kept():
    out, report = cutout(make(), feather=0)
    assert out.mode == "RGBA" and out.size == CANVAS == report["size"]
    assert out.getpixel((2, 2))[3] == 0 and out.getpixel((CANVAS[0] - 3, 5))[3] == 0     # 角落透明
    cx = CANVAS[0] // 2
    assert out.getpixel((cx, CANVAS[1] - 3))[3] == 255                                  # 腳貼底
    r, g, b, a = out.getpixel((cx, CANVAS[1] - 100))
    assert a == 255 and r > 150 and g < 80
    assert 0.3 < report["removed_ratio"] < 0.7 and report["subject_box"] == (100, 80, 300, 800)


def test_hole_inside_subject_stays_opaque():
    out, report = cutout(make(), feather=0)
    scale = (CANVAS[1] - 40) / 720                      # 裁切後高 720 → 縮放到 1560 高
    x = round((CANVAS[0] - 200 * scale) / 2 + (200 - 100) * scale)   # 洞中心的 x（裁切框左 100、洞中心 x=200）
    y = round(CANVAS[1] - 720 * scale + (420 - 80) * scale)
    assert out.getpixel((x, y))[3] == 255


def test_noisy_and_colored_backgrounds():
    out, _ = cutout(make(bg=(240, 240, 240), noise=6), tolerance=40, feather=0)
    assert out.getpixel((3, 3))[3] == 0 and out.getpixel((CANVAS[0] // 2, CANVAS[1] - 3))[3] == 255
    out, _ = cutout(make(bg=(0, 200, 0)), feather=0)
    assert out.getpixel((3, 3))[3] == 0 and out.getpixel((CANVAS[0] // 2, CANVAS[1] - 3))[3] == 255


def test_tolerance_controls_what_is_removed():
    image = Image.new("RGB", (200, 400), (255, 255, 255))
    ImageDraw.Draw(image).rectangle([50, 100, 149, 399], fill=(225, 225, 225))   # 非常淺的角色
    with pytest.raises(ValueError):
        cutout(image, tolerance=40)                                              # 容差太大 → 角色也被當成背景
    out, _ = cutout(image, tolerance=10, feather=0)
    assert out.getpixel((CANVAS[0] // 2, CANVAS[1] - 3))[3] == 255


def test_feather_softens_edges():
    hard, _ = cutout(make(), feather=0)
    soft, _ = cutout(make(), feather=3)
    values = {soft.getpixel((x, 800))[3] for x in range(0, CANVAS[0])}
    assert any(0 < a < 255 for a in values) and not any(0 < hard.getpixel((x, 800))[3] < 255 for x in range(CANVAS[0]))


def test_fits_canvas_with_margin_and_custom_canvas():
    out, report = cutout(make(), canvas=(300, 500), margin=20, feather=0)
    assert out.size == (300, 500)
    alpha = out.getchannel("A")
    left, top, right, bottom = alpha.getbbox()
    assert left >= 20 and right <= 280 and top >= 20 and bottom == 500
    with pytest.raises(ValueError):
        cutout(make(), canvas=(30, 500), margin=20)
    with pytest.raises(ValueError):
        cutout(make(), tolerance=300)


def test_all_background_is_an_error():
    with pytest.raises(ValueError, match="no subject"):
        cutout(Image.new("RGB", (100, 100), (255, 255, 255)))


def test_cutout_file_and_cli(tmp_path):
    src = tmp_path / "in.png"
    make().save(src)
    dst = tmp_path / "out" / "sprite.png"
    report = cutout_file(src, dst)
    assert Image.open(dst).size == CANVAS and report["size"] == CANVAS
    with pytest.raises(FileNotFoundError):
        cutout_file(tmp_path / "nope.png", dst)
    with pytest.raises(ValueError):
        cutout_file(src, src)
    with pytest.raises(ValueError):
        cutout_file(src, tmp_path / "x.jpg")
    assert main(["--src", str(src), "--dst", str(tmp_path / "cli.png")]) == 0
    assert main(["--src", str(tmp_path / "nope.png"), "--dst", str(tmp_path / "cli2.png")]) == 1


GREEN = (0, 255, 0)


def make_gap(bg, body=(240, 240, 240)):
    """角色身體旁邊有一塊被手臂圍住的背景色區塊（寬 60 高 150），身體本身接近白色。"""
    image = Image.new("RGB", (400, 800), bg)
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 299, 799], fill=body)
    draw.rectangle([200, 300, 259, 449], fill=bg)
    return image


def alpha_at_source(out, source_xy, box=(100, 100, 300, 800)):
    """來源圖座標 → 輸出圖上同一點的 alpha（依裁切框與縮放計算）。"""
    left, top, right, bottom = box
    scale = min((CANVAS[0] - 80) / (right - left), (CANVAS[1] - 40) / (bottom - top))
    new_w, new_h = round((right - left) * scale), round((bottom - top) * scale)
    x = (CANVAS[0] - new_w) // 2 + round((source_xy[0] - left) * scale)
    y = CANVAS[1] - new_h + round((source_xy[1] - top) * scale)
    return out.getpixel((x, y))[3]


def test_is_chroma():
    assert is_chroma((0, 255, 0)) and is_chroma((10, 200, 30))
    assert not is_chroma((252, 252, 252)) and not is_chroma((128, 128, 128))


def test_white_background_keeps_enclosed_white_region():
    out, report = cutout(make_gap((255, 255, 255), body=(150, 150, 170)), feather=0)
    assert report["mode"] == "edge"
    assert alpha_at_source(out, (230, 375)) == 255            # 白底：被圍住的白不去（可能是白衣服）


def test_green_background_removes_enclosed_gap():
    out, report = cutout(make_gap(GREEN), feather=0)
    assert report["mode"] == "chroma"
    assert alpha_at_source(out, (230, 375)) == 0              # 綠底：被圍住的綠一起去掉
    assert alpha_at_source(out, (150, 375)) == 255            # 身體還在
    assert out.getpixel((2, 2))[3] == 0


def test_mode_can_be_forced():
    out, report = cutout(make_gap(GREEN), feather=0, mode="edge")
    assert report["mode"] == "edge" and alpha_at_source(out, (230, 375)) == 255
    with pytest.raises(ValueError):
        cutout(make_gap(GREEN), mode="magic")


def test_white_shirt_survives_on_green_background():
    out, _ = cutout(make_gap(GREEN, body=(255, 255, 255)), feather=0)
    assert alpha_at_source(out, (150, 500)) == 255
    r, g, b, a = out.getpixel((CANVAS[0] // 2 - 100, CANVAS[1] - 100))
    assert a == 255 and min(r, g, b) > 240


def test_despill_removes_green_tint_only_inside_zone():
    image = Image.new("RGB", (4, 1), (200, 250, 200))
    zone = Image.new("L", (4, 1), 0)
    zone.putpixel((0, 0), 255)
    zone.putpixel((1, 0), 255)
    out = despill(image, GREEN, zone)
    assert out.getpixel((0, 0)) == (200, 200, 200) and out.getpixel((3, 0)) == (200, 250, 200)


def test_edge_fringe_is_not_green_after_cutout():
    image = make_gap(GREEN, body=(200, 120, 120))
    draw = ImageDraw.Draw(image)
    draw.rectangle([98, 100, 99, 799], fill=(100, 190, 80))   # 身體左側一條綠色混色邊
    out, _ = cutout(image, feather=0)
    for x in range(CANVAS[0]):
        r, g, b, a = out.getpixel((x, CANVAS[1] - 100))
        if a == 255:
            assert g <= max(r, b) + 15


def test_cli_mode_option(tmp_path):
    src = tmp_path / "g.png"
    make_gap(GREEN).save(src)
    assert main(["--src", str(src), "--dst", str(tmp_path / "o.png"), "--mode", "chroma"]) == 0


def test_muted_teal_background_removes_enclosed_gap_but_keeps_similar_shadow():
    teal = (95, 140, 130)
    img = Image.new("RGB", (120, 240), teal)
    d = ImageDraw.Draw(img)
    d.rectangle((30, 20, 90, 230), fill=(230, 200, 180))
    d.rectangle((50, 80, 70, 120), fill=teal)  # 被角色圍住的背景
    d.rectangle((40, 150, 80, 190), fill=(150, 175, 180))  # 偏青的灰藍陰影，要保留
    out, report = cutout(img)
    assert report["mode"] == "chroma"
    w, h = out.size
    ys = [y for y in range(h) if out.getpixel((w // 2, y))[3] > 200]
    top, bottom = min(ys), max(ys)
    mid_hole = out.getpixel((w // 2, top + (bottom - top) * (100 - 20) // 210))
    shadow = out.getpixel((w // 2, top + (bottom - top) * (170 - 20) // 210))
    assert mid_hole[3] < 50
    assert shadow[3] > 200
