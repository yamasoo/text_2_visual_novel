import pytest
from PIL import Image

from t2vn import sprite_consistency as m


def sprite(w, h, box, size=(200, 400)):
    """size 大小的透明圖，在 box=(x0,y0,x1,y1) 畫不透明紅色。"""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), box)
    return img


def opaque_box(img):
    return img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()


def A(file, kind="sprite"):
    return {"asset_id": file, "kind": kind, "file": file}


def test_character_seed_deterministic_and_range():
    assert m.character_seed("ellie") == m.character_seed("ellie")
    assert m.character_seed("ellie") != m.character_seed("ellie", 1)
    assert m.character_seed("ellie") != m.character_seed("pesk")
    assert 0 <= m.character_seed("ellie", 7) < 2**31


def test_character_seed_known_value():
    import hashlib
    d = hashlib.sha256(b"sprite:ellie:0").digest()
    assert m.character_seed("ellie", 0) == int.from_bytes(d[:8], "big") % 2**31


@pytest.mark.parametrize("cid,att", [("", 0), (None, 0), ("a", -1), ("a", True), ("a", 1.5)])
def test_character_seed_errors(cid, att):
    with pytest.raises(ValueError):
        m.character_seed(cid, att)


def test_split_sprite():
    assert m.split_sprite(A("assets/sprite/ellie_reed_smile.png")) == ("ellie_reed", "smile")
    assert m.split_sprite(A("assets/sprite/pesk_neutral.png")) == ("pesk", "neutral")


@pytest.mark.parametrize("a", [A("assets/bg/x.png", "bg"), A("assets/sprite/ellie_unknown.png"),
                               A("assets/sprite/neutral.png"), A("x/y.png"), {"kind": "sprite"}, "str"])
def test_split_sprite_errors(a):
    with pytest.raises(ValueError):
        m.split_sprite(a)


def test_order_sprite_assets():
    items = [A("assets/sprite/pesk_sad.png"), A("assets/bg/a.png", "bg"), A("assets/sprite/ellie_serious.png"),
             A("assets/sprite/pesk_neutral.png"), A("assets/sprite/ellie_smile.png"), A("assets/sprite/bad.png"),
             A("assets/sprite/ellie_neutral.png")]
    snap = list(items)
    out = [a["file"] for a in m.order_sprite_assets(items)]
    assert out == ["assets/sprite/ellie_neutral.png", "assets/sprite/ellie_smile.png", "assets/sprite/ellie_serious.png",
                   "assets/sprite/pesk_neutral.png", "assets/sprite/pesk_sad.png"]
    assert items == snap
    assert m.order_sprite_assets([]) == [] and m.order_sprite_assets(None) == []


def test_align_same_canvas_bottom_and_center():
    imgs = {"neutral": sprite(200, 400, (50, 20, 150, 380)), "smile": sprite(200, 400, (60, 40, 140, 380))}
    out = m.align_sprites(imgs, canvas=(300, 600), fill=0.9, bottom_margin=10)
    assert set(out) == {"neutral", "smile"}
    for im in out.values():
        assert im.size == (300, 600) and im.mode == "RGBA"
        box = opaque_box(im)
        assert abs(box[3] - 590) <= 1  # 底邊貼齊
        assert abs((box[0] + box[2]) / 2 - 150) <= 1.5  # 水平置中


def test_align_scale_from_reference_only():
    imgs = {"neutral": sprite(200, 400, (50, 20, 150, 380)), "smile": sprite(200, 400, (60, 40, 140, 380))}
    out = m.align_sprites(imgs, canvas=(300, 600), fill=0.9)
    n, s = opaque_box(out["neutral"]), opaque_box(out["smile"])
    assert abs((n[3] - n[1]) - 540) <= 2  # 360 * 1.5 = 540 = 600*0.9
    assert abs((s[2] - s[0]) - 80 * 1.5) <= 2  # 同一個縮放比例，不各自撐滿
    assert (s[3] - s[1]) < (n[3] - n[1]) + 1


def test_align_width_limited():
    imgs = {"neutral": sprite(400, 200, (0, 50, 400, 150), size=(400, 200))}
    out = m.align_sprites(imgs, canvas=(300, 600), fill=1.0)
    box = opaque_box(out["neutral"])
    assert box[2] - box[0] == 300


def test_align_reference_fallback_and_no_mutation():
    a = sprite(200, 400, (50, 20, 150, 380))
    b = sprite(200, 400, (50, 20, 150, 200))
    before = a.tobytes()
    out = m.align_sprites({"b_sad": b, "a_smile": a}, canvas=(300, 600), fill=0.9)  # 無 neutral → 'a_smile' 當參考
    assert a.tobytes() == before and a.size == (200, 400)
    assert abs((opaque_box(out["a_smile"])[3] - opaque_box(out["a_smile"])[1]) - 540) <= 2


def test_align_errors_and_empty():
    assert m.align_sprites({}) == {}
    with pytest.raises(ValueError):
        m.align_sprites({"neutral": Image.new("RGBA", (50, 50), (0, 0, 0, 0))})
