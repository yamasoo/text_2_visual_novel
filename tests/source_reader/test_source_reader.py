import pytest

from t2vn.errors import SourceError
from t2vn.source_reader import (bullet_items, load_field_map, parse_list_value, read_all_outlines, read_character,
                                read_chapter_outline, read_characters, read_glossary, split_frontmatter)

FM = load_field_map()

OUTLINE = """---
類型: 章綱
章號: 016
章名: 上一個雇主
所屬卷: 第1卷
所屬單元: 單元二
字數目標:
狀態: 已確認
---

# 第16章 上一個雇主

## 本章四問
1. a

## 出場角色
- 艾莉・里德
- 凱爾・奧登

## 禁止事項
- 不能說
"""

CARD = """---
類型: 角色
名稱: 老張
別名: [阿周, 周伯]
定位: 配角
首次登場: 第2卷第91章前後
狀態: 正式
---

# 老張

## 一句話
船主。
"""

GLOSS = """---
類型: 名詞表
---

# 名詞表

| 名詞 | 類別 | 說明 | 注意 |
|---|---|---|---|
| 老張 | 角色 | 船主 | 首次出現：第001章 |
| 磅秤 | 物品 | 秤 | |
| 老張 | 角色 | 重複 | |
"""


def write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_outline(tmp_path):
    o = read_chapter_outline(write(tmp_path, "o/第016章.md", OUTLINE), FM)
    assert o.chapter_no == 16 and o.status == "confirmed"
    assert o.cast_names == ["艾莉・里德", "凱爾・奧登"]
    assert set(o.sections) == {"four_questions", "cast_names", "forbidden"}


def test_unmapped_key_and_heading(tmp_path):
    with pytest.raises(SourceError):
        read_chapter_outline(write(tmp_path, "a.md", OUTLINE.replace("字數目標", "神祕欄位")), FM)
    with pytest.raises(SourceError):
        read_chapter_outline(write(tmp_path, "b.md", OUTLINE + "\n## 新標題\nx\n"), FM)
    with pytest.raises(SourceError):
        read_chapter_outline(write(tmp_path, "c.md", OUTLINE.replace("已確認", "未知")), FM)


def test_wrong_doc_type(tmp_path):
    with pytest.raises(SourceError):
        read_character(write(tmp_path, "d.md", OUTLINE), FM)


def test_character(tmp_path):
    c = read_character(write(tmp_path, "c.md", CARD), FM)
    assert c.role == "supporting" and c.first_appearance == 91
    assert c.aliases == ["阿周", "周伯"] and c.sections["one_line"] == "船主。"


def test_all_outlines_sorted_and_duplicates(tmp_path):
    write(tmp_path, "大綱/章綱/第016章.md", OUTLINE)
    write(tmp_path, "大綱/章綱/第003章.md", OUTLINE.replace("016", "3"))
    write(tmp_path, "大綱/章綱/readme.md", "not an outline")
    assert [o.chapter_no for o in read_all_outlines(tmp_path, FM)] == [3, 16]
    write(tmp_path, "大綱/章綱/第017章.md", OUTLINE)
    with pytest.raises(SourceError):
        read_all_outlines(tmp_path, FM)


def test_characters_duplicate_name(tmp_path):
    write(tmp_path, "設定/角色/a.md", CARD)
    write(tmp_path, "設定/角色/b.md", CARD)
    with pytest.raises(SourceError):
        read_characters(tmp_path, FM)


def test_glossary(tmp_path):
    write(tmp_path, "設定/名詞表.md", GLOSS)
    g = read_glossary(tmp_path, FM)
    assert [t.term for t in g.terms] == ["老張", "磅秤"]
    assert g.duplicates == ["老張"]
    assert g.terms[0].first_appearance == 1 and g.terms[1].first_appearance is None
    assert g.terms[1].category == "item"


def test_helpers():
    assert parse_list_value("[]") == [] and parse_list_value("x") == ["x"]
    assert bullet_items("1. a\n- b\ntext") == ["a", "b"]
    with pytest.raises(SourceError):
        split_frontmatter("no front", "x")
