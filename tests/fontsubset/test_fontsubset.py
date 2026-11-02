import json
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from t2vn.fontsubset import BASE_CHARS, collect_chars, main, subset_font

# 合成字型用的字：ASCII 一小段＋一批假的 CJK（每個字一個方塊字形），數量夠多，subset 後才會明顯變小。
SYNTH_CJK = "".join(chr(0x4E00 + i) for i in range(300))
SYNTH_ASCII = "ABC abc"


def make_font(path: Path, chars: str = SYNTH_ASCII + SYNTH_CJK) -> Path:
    glyph_names = [".notdef"] + [f"uni{ord(c):04X}" for c in chars]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyph_names)
    builder.setupCharacterMap({ord(c): f"uni{ord(c):04X}" for c in chars})
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0)); pen.lineTo((0, 700)); pen.lineTo((500, 700)); pen.lineTo((500, 0)); pen.closePath()
    glyph = pen.glyph()
    builder.setupGlyf({name: glyph for name in glyph_names})
    builder.setupHorizontalMetrics({name: (600, 0) for name in glyph_names})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Synth", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    builder.save(str(path))
    return path


SCENES = [{"scenes": [
    {"scene_id": "ch001_sc01", "title": {"zh-TW": "開場", "en": "Open"},
     "lines": [{"type": "narration", "text": "天亮了。\n"}, {"type": "dialogue", "speaker": "a", "text": "走吧！"}, {"type": "direction", "effect": "shake"}]},
    {"scene_id": "ch001_sc02", "lines": [{"type": "narration", "text": "  再見\t"}]},
]}]
CAST = {"characters": {"a": {"names": {"zh-TW": "老張", "en": "Zhang"}, "aliases": ["周叔"]}}}


def test_collect_chars_sources_and_normalisation():
    result = collect_chars(SCENES, CAST, extra="龘")
    for ch in "天亮了走吧開場再見老張叔龘ZOe":  # 台詞、標題（含其他語言）、角色名與別名、extra
        assert ch in result
    assert not any(c in result for c in "\n\t\r")
    assert "　" in result and " " in result
    assert result == "".join(sorted(set(result)))  # 不重複、依字碼排序
    assert set(BASE_CHARS) <= set(result) - set()
    # 沒有 text 的 direction 不會讓程式出錯；空輸入只剩基本字
    assert set(collect_chars([], {})) == set(BASE_CHARS) - {"\n", "\t"}


def test_subset_keeps_requested_and_shrinks(tmp_path):
    src = make_font(tmp_path / "src.ttf")
    dst = tmp_path / "out" / "small.ttf"
    report = subset_font(src, dst, "AB你好" + SYNTH_CJK[:5] + " ")
    cmap = TTFont(str(dst)).getBestCmap()
    assert {ord(c) for c in "AB" + SYNTH_CJK[:5]} <= set(cmap)
    assert ord("C") not in cmap and ord(SYNTH_CJK[100]) not in cmap
    assert report["bytes_after"] < report["bytes_before"] / 2
    assert report["bytes_before"] == src.stat().st_size and report["bytes_after"] == dst.stat().st_size
    assert report["chars_requested"] == len(set("AB你好" + SYNTH_CJK[:5] + " "))
    assert report["glyphs_after"] >= 8
    assert report["chars_missing"] == "你好"  # 來源字型沒有；空白不算缺字


def test_missing_chars_sorted_and_all_present_gives_empty(tmp_path):
    src = make_font(tmp_path / "src.ttf")
    report = subset_font(src, tmp_path / "o.ttf", "龜龍AB")
    assert report["chars_missing"] == "龍龜"  # U+9F8D < U+9F9C
    assert subset_font(src, tmp_path / "o2.ttf", "AB")["chars_missing"] == ""


def test_errors(tmp_path):
    src = make_font(tmp_path / "src.ttf")
    with pytest.raises(FileNotFoundError):
        subset_font(tmp_path / "nope.ttf", tmp_path / "o.ttf", "A")
    with pytest.raises(ValueError):
        subset_font(src, tmp_path / "o.ttf", "")
    with pytest.raises(ValueError):
        subset_font(src, src, "A")
    with pytest.raises(ValueError):
        subset_font(src, tmp_path / "o.otf", "A")
    bad = tmp_path / "bad.ttf"
    bad.write_bytes(b"not a font")
    with pytest.raises(ValueError):
        subset_font(bad, tmp_path / "o.ttf", "A")
    ttc = tmp_path / "c.ttc"
    ttc.write_bytes(src.read_bytes())
    with pytest.raises(ValueError):
        subset_font(ttc, tmp_path / "o.ttc", "A")


def test_cli(tmp_path, capsys):
    src = make_font(tmp_path / "src.ttf")
    (tmp_path / "ch001.json").write_text(json.dumps(SCENES, ensure_ascii=False)[1:-1], encoding="utf-8")
    (tmp_path / "cast.json").write_text(json.dumps(CAST, ensure_ascii=False), encoding="utf-8")
    code = main(["--font", str(src), "--out", str(tmp_path / "o.ttf"), "--scenes", str(tmp_path / "ch001.json"), "--cast", str(tmp_path / "cast.json")])
    report = json.loads(capsys.readouterr().out)
    assert code == 1 and report["chars_missing"]  # 合成字型缺大部分基本字 → 回 1
    assert (tmp_path / "o.ttf").is_file()
