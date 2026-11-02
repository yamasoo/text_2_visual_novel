import json
from pathlib import Path

import pytest
from PIL import Image

from t2vn.placeholders import RECORD_FILE, make_placeholders, main, render_background, render_sprite

EXPORT = {
    "schema_version": "0.1", "book_id": "demo",
    "backgrounds": [
        {"asset_id": "bg_dock_dawn", "file": "images/bg/bg_dock_dawn.png", "location_id": "dock", "time_of_day": "dawn", "label": "碼頭", "present": False},
        {"asset_id": "bg_dock_night", "file": "images/bg/bg_dock_night.png", "location_id": "dock", "time_of_day": "night", "label": "碼頭", "present": False},
    ],
    "sprites": [
        {"char_id": "lao_zhang", "expr": "smile", "file": "images/sprite/lao_zhang_smile.png", "name": "老張", "hue": 28, "shape": "broad", "present": False},
        {"char_id": "ellie", "expr": "neutral", "file": "images/sprite/ellie_neutral.png", "name": "艾莉", "hue": 240, "shape": "tall", "present": False},
    ],
    "audio": [{"asset_id": "bgm_a", "kind": "bgm", "file": "audio/bgm/bgm_a.ogg", "present": False}],
    "font": None,
}


def alpha_area(image):
    return sum(1 for a in image.getchannel("A").getdata() if a > 0)


def test_background_gradient_and_sizes():
    dawn = render_background("bg_dock_dawn", "dawn")
    assert dawn.size == (1920, 1080) and dawn.mode == "RGB"
    assert dawn.getpixel((5, 5)) != dawn.getpixel((5, 1074))  # 上下漸層
    assert render_background("x", "night").getpixel((5, 5)) != dawn.getpixel((5, 5))
    with pytest.raises(ValueError):
        render_background("x", "midnight")


def test_sprite_transparent_silhouette_with_hue_and_shape():
    red = render_sprite("a", "neutral", 0, "tall")
    blue = render_sprite("a", "neutral", 240, "tall")
    assert red.size == (800, 1600) and red.mode == "RGBA"
    assert red.getpixel((2, 2))[3] == 0 and red.getpixel((400, 1000))[3] == 255  # 角落透明、身體不透明
    r, g, b, _ = red.getpixel((400, 1000))
    r2, g2, b2, _ = blue.getpixel((400, 1000))
    assert r > b and b2 > r2
    areas = {s: alpha_area(render_sprite("a", "neutral", 100, s)) for s in ("tall", "broad", "small", "bent", "round")}
    assert len(set(areas.values())) == 5  # 五種體型輪廓都不同
    for bad in ({"shape": "huge", "hue": 1}, {"shape": "tall", "hue": 360}, {"shape": "tall", "hue": -1}, {"shape": "tall", "hue": 1.5}):
        with pytest.raises(ValueError):
            render_sprite("a", "n", bad["hue"], bad["shape"])


def test_make_placeholders_creates_missing_and_records(tmp_path):
    result = make_placeholders(EXPORT, tmp_path)
    assert result["created"] == [e["file"] for e in EXPORT["backgrounds"] + EXPORT["sprites"]] and result["skipped"] == []
    for file in result["created"]:
        Image.open(tmp_path / file).verify()
    record = json.loads((tmp_path / RECORD_FILE).read_text(encoding="utf-8"))
    assert record["files"] == sorted(result["created"])
    assert not (tmp_path / "audio").exists()  # 音訊不做佔位


def test_real_art_is_never_overwritten(tmp_path):
    real = tmp_path / "images" / "bg" / "bg_dock_dawn.png"
    real.parent.mkdir(parents=True)
    Image.new("RGB", (10, 10), (1, 2, 3)).save(real)
    before = real.read_bytes()
    result = make_placeholders(EXPORT, tmp_path)
    assert "images/bg/bg_dock_dawn.png" in result["skipped"] and real.read_bytes() == before
    # overwrite=True 也不動「沒登記為佔位」的真素材
    result = make_placeholders(EXPORT, tmp_path, overwrite=True)
    assert real.read_bytes() == before and "images/bg/bg_dock_dawn.png" in result["skipped"]
    record = json.loads((tmp_path / RECORD_FILE).read_text(encoding="utf-8"))
    assert "images/bg/bg_dock_dawn.png" not in record["files"]


def test_second_run_skips_and_overwrite_redraws_only_placeholders(tmp_path):
    make_placeholders(EXPORT, tmp_path)
    again = make_placeholders(EXPORT, tmp_path)
    assert again["created"] == [] and len(again["skipped"]) == 4
    changed = json.loads(json.dumps(EXPORT))
    changed["sprites"][0]["hue"] = 200
    old = (tmp_path / "images/sprite/lao_zhang_smile.png").read_bytes()
    redo = make_placeholders(changed, tmp_path, overwrite=True)
    assert "images/sprite/lao_zhang_smile.png" in redo["created"] and (tmp_path / "images/sprite/lao_zhang_smile.png").read_bytes() != old


def test_record_drops_files_that_disappeared(tmp_path):
    make_placeholders(EXPORT, tmp_path)
    (tmp_path / "images/bg/bg_dock_night.png").unlink()
    only = {**EXPORT, "backgrounds": [], "sprites": []}
    make_placeholders(only, tmp_path)
    record = json.loads((tmp_path / RECORD_FILE).read_text(encoding="utf-8"))
    assert "images/bg/bg_dock_night.png" not in record["files"] and len(record["files"]) == 3


@pytest.mark.parametrize("bad", ["../evil.png", "/abs/evil.png", "images\\evil.png", "images/bg/x.jpg", "images/../../x.png"])
def test_unsafe_paths_write_nothing(tmp_path, bad):
    export = json.loads(json.dumps(EXPORT))
    export["backgrounds"].append({"asset_id": "bg_x", "file": bad, "location_id": "x", "time_of_day": "day", "label": "", "present": False})
    with pytest.raises(ValueError):
        make_placeholders(export, tmp_path)
    assert not any(tmp_path.rglob("*.png")) and not (tmp_path / RECORD_FILE).exists()


def test_invalid_item_writes_nothing(tmp_path):
    export = json.loads(json.dumps(EXPORT))
    export["sprites"][1]["shape"] = "huge"
    with pytest.raises(ValueError):
        make_placeholders(export, tmp_path)
    assert not any(tmp_path.rglob("*.png"))


def test_cli(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    (game / "vn_export.json").write_text(json.dumps(EXPORT, ensure_ascii=False), encoding="utf-8")
    assert main(["--game", str(game)]) == 0 and (game / "images/bg/bg_dock_dawn.png").is_file()
    assert main(["--game", str(tmp_path / "missing")]) == 1
