import json

import openpyxl
import pytest

from t2vn import art_styles as m

HEADER = ["name", "prompt", "naturl_prompt", "negative_prompt", "category", "namezh", "namejp", "nameen"]


def make_xlsx(path, rows, header=HEADER):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)


ROW_A = ["甲/a/A", "Oil Painting, impasto texture,  Warm Palette, oil painting", "A warm oil look.", "low quality, Blurry, low quality",
         "內文插圖風格", "甲風", "甲ふう", "Style A"]
ROW_B = ["乙/b/B", "bold outlines, flat colors, cover art, poster layout, simple shapes", "Flat look.", "text, watermark",
         "封面藝術風格", "乙​風", "乙ふう", "Style B"]
ROW_C = ["丙/c/C", "pencil sketch, paper grain", "", "", "藝術形式", "丙風", "", "Style C"]


@pytest.fixture
def catalog(tmp_path):
    p = tmp_path / "s.xlsx"
    make_xlsx(p, [ROW_A, ROW_B, ROW_C])
    return m.parse_styles_xlsx(p)


def test_parse_basic(catalog):
    assert [s["names"]["zh-TW"] for s in catalog] == ["甲風", "乙風", "丙風"]  # 零寬字元被去掉
    a = catalog[0]
    assert a["tags"] == "oil painting, impasto texture, warm palette"  # 小寫、去重
    assert a["negative"] == "low quality, blurry"
    assert a["category"] == "interior" and a["category_label"] == "內文插圖風格"
    assert a["description"] == "A warm oil look."
    assert a["names"] == {"zh-TW": "甲風", "ja": "甲ふう", "en": "Style A"}
    assert [s["category"] for s in catalog] == ["interior", "cover", "art_form"]


def test_style_id_stable_and_ascii(tmp_path, catalog):
    import re
    for s in catalog:
        assert re.fullmatch(r"st_[0-9a-f]{8}", s["style_id"])
    p = tmp_path / "s2.xlsx"
    make_xlsx(p, [ROW_C, ROW_A])  # 順序不同，id 不變
    again = m.parse_styles_xlsx(p)
    assert again[1]["names"]["zh-TW"] == "甲風"
    assert again[1]["style_id"] == catalog[0]["style_id"]


def test_duplicate_names_get_suffix(tmp_path):
    p = tmp_path / "d.xlsx"
    make_xlsx(p, [ROW_A, ROW_A])
    c = m.parse_styles_xlsx(p)
    assert c[1]["style_id"] == c[0]["style_id"] + "_2"


def test_unknown_category_and_blank_rows(tmp_path):
    p = tmp_path / "u.xlsx"
    r = list(ROW_C)
    r[4] = "怪分類"
    make_xlsx(p, [r, [None] * 8, ROW_A])
    c = m.parse_styles_xlsx(p)
    assert len(c) == 2 and c[0]["category"] == "other" and c[0]["category_label"] == "怪分類"


def test_missing_column_and_empty(tmp_path):
    p = tmp_path / "m.xlsx"
    make_xlsx(p, [["a"] * 3], header=["name", "prompt", "category"])
    with pytest.raises(ValueError, match="缺少欄位"):
        m.parse_styles_xlsx(p)
    q = tmp_path / "e.xlsx"
    make_xlsx(q, [])
    with pytest.raises(ValueError):
        m.parse_styles_xlsx(q)


def test_save_load_roundtrip(tmp_path, catalog):
    f = tmp_path / "c.json"
    m.save_catalog(catalog, f)
    assert "甲風" in f.read_text(encoding="utf-8")  # 不轉義中文
    assert m.load_catalog(f) == catalog
    f.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        m.load_catalog(f)
    with pytest.raises(FileNotFoundError):
        m.load_catalog(tmp_path / "none.json")


def test_filter_and_get(catalog):
    assert [s["names"]["en"] for s in m.filter_styles(catalog, category="cover")] == ["Style B"]
    assert len(m.filter_styles(catalog, query="PENCIL")) == 1
    assert len(m.filter_styles(catalog, query="甲")) == 1
    assert len(m.filter_styles(catalog)) == 3
    assert m.get_style(catalog, catalog[1]["style_id"]) is catalog[1]
    with pytest.raises(KeyError):
        m.get_style(catalog, "st_nope")


def test_apply_basic_and_no_mutation(catalog):
    art = {"extra": "rain", "negative": "blurry", "artist": "x"}
    snap = json.dumps(art)
    new, dropped = m.apply_style(art, catalog[0])
    assert json.dumps(art) == snap
    assert new["extra"] == "rain, oil painting, impasto texture, warm palette"
    assert new["negative"] == "blurry, low quality"  # blurry 已存在不重複
    assert new["artist"] == "x" and dropped == []
    assert new["style_applied"] == {"style_id": catalog[0]["style_id"],
                                    "extra_tags": ["oil painting", "impasto texture", "warm palette"],
                                    "negative_tags": ["low quality"]}


def test_apply_drops_unsuitable(catalog):
    new, dropped = m.apply_style({}, catalog[1])
    assert dropped == ["cover art", "poster layout"]
    assert new["extra"] == "bold outlines, flat colors, simple shapes"


def test_apply_replaces_previous_style(catalog):
    first, _ = m.apply_style({"extra": "rain"}, catalog[0])
    second, _ = m.apply_style(first, catalog[2])
    assert second["extra"] == "rain, pencil sketch, paper grain"
    assert "low quality" not in second.get("negative", "")
    assert second["style_applied"]["style_id"] == catalog[2]["style_id"]


def test_apply_same_twice_idempotent(catalog):
    a, _ = m.apply_style({}, catalog[0])
    b, _ = m.apply_style(a, catalog[0])
    assert a == b


def test_apply_when_user_already_had_tag(catalog):
    new, _ = m.apply_style({"extra": "oil painting"}, catalog[0])
    assert new["style_applied"]["extra_tags"] == ["impasto texture", "warm palette"]
    cleared = m.clear_style(new)
    assert cleared["extra"] == "oil painting"  # 使用者原有的標籤不被拿掉


def test_clear_style(catalog):
    new, _ = m.apply_style({}, catalog[0])
    cleared = m.clear_style(new)
    assert "style_applied" not in cleared and "extra" not in cleared and "negative" not in cleared
    assert m.clear_style(None) == {}
    assert m.clear_style({"extra": "a"}) == {"extra": "a"}
