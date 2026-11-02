import pytest

from t2vn import wildcards as m


def write(d, name, text, enc="utf-8"):
    (d / name).write_bytes(text.encode(enc))


@pytest.fixture
def lib_dir(tmp_path):
    write(tmp_path, "Hair Color.txt", "black\r\nsilver\r\n\r\n# 註解\r\nblonde,\r\nblack\r\n")
    write(tmp_path, "hair_color.txt", "never used\n")  # 與 Hair Color.txt 衝突（排序：空白 < 底線，所以留 Hair Color.txt）
    write(tmp_path, "Chinese fantasy clothes.txt", "﻿(Wuxia attire:1.3),long robe\nhanfu\n")
    write(tmp_path, "empty.txt", "# only comment\n\n")
    write(tmp_path, "4040.txt", "x\ny\n")
    write(tmp_path, "pose.txt", "pose\nvictory pose\n")
    write(tmp_path, "notes.md", "ignored")
    (tmp_path / "sub").mkdir()
    write(tmp_path / "sub", "deep.txt", "ignored")
    return tmp_path


def test_normalize_name():
    assert m.normalize_name("Chinese fantasy clothes") == "chinese_fantasy_clothes"
    assert m.normalize_name("hair-female") == "hair_female"
    assert m.normalize_name("AngleDistance") == "angledistance"
    assert m.normalize_name("4040") == "w_4040"
    assert m.normalize_name("  a  b ") == "a_b"
    with pytest.raises(ValueError):
        m.normalize_name("！！")


def test_load_library(lib_dir):
    lib, warns = m.load_library(lib_dir)
    assert sorted(lib) == ["chinese_fantasy_clothes", "hair_color", "pose", "w_4040"]
    assert lib["hair_color"] == ["black", "silver", "blonde"]  # 去空行、註解、尾逗號、重複
    assert lib["chinese_fantasy_clothes"] == ["(Wuxia attire:1.3),long robe", "hanfu"]  # BOM 去掉、權重寫法原樣
    assert any("hair_color.txt" in w and "Hair Color.txt" in w for w in warns)
    assert any("empty.txt" in w for w in warns)


def test_load_library_missing_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        m.load_library(tmp_path / "nope")


def test_describe_library():
    lib = {"b": ["x" * 50, "y", "z", "w"], "a": ["one"]}
    d = m.describe_library(lib)
    assert [x["name"] for x in d] == ["a", "b"]
    assert d[1]["count"] == 4 and d[1]["sample"] == ["x" * 40, "y", "z"]
    assert m.describe_library({}) == []


def test_pick_deterministic():
    lib = {"c": ["a", "b", "c", "d", "e"]}
    assert m.pick(lib, "c", 1) == m.pick(lib, "c", 1)
    seen = {m.pick(lib, "c", s) for s in range(50)}
    assert len(seen) > 1 and seen <= set(lib["c"])
    with pytest.raises(KeyError):
        m.pick(lib, "nope", 1)


def test_pick_known_value():
    import hashlib
    lib = {"c": ["a", "b", "c"]}
    d = hashlib.sha256(b"7:c:s").digest()
    assert m.pick(lib, "c", 7, "s") == lib["c"][int.from_bytes(d[:8], "big") % 3]


def test_expand_basic_and_deterministic():
    lib = {"hair_color": ["black", "silver"], "outfit": ["robe"]}
    a = m.expand("long hair, __hair_color__ hair, __outfit__", lib, 5)
    b = m.expand("long hair, __hair_color__ hair, __outfit__", lib, 5)
    assert a == b
    text, warns = a
    assert text.startswith("long hair, ") and text.endswith("hair, robe") and warns == []
    assert "__" not in text


def test_expand_no_tokens_and_score_tags():
    assert m.expand("score_7, masterpiece", {}, 1) == ("score_7, masterpiece", [])
    assert m.expand("", {}, 1) == ("", [])


def test_expand_unknown_warns_once():
    text, warns = m.expand("__nope__ and __nope__", {}, 1)
    assert text == "__nope__ and __nope__"
    assert warns == ["未知的 wildcard：nope"]


def test_expand_same_token_twice_uses_different_salt():
    lib = {"c": [str(i) for i in range(30)]}
    outs = {m.expand("__c__ __c__", lib, s)[0] for s in range(20)}
    assert any(o.split()[0] != o.split()[1] for o in outs)


def test_expand_nested():
    lib = {"outer": ["__inner__ coat"], "inner": ["red"]}
    assert m.expand("__outer__", lib, 3) == ("red coat", [])


def test_expand_cycle_stops():
    lib = {"a": ["__b__"], "b": ["__a__"]}
    text, warns = m.expand("__a__", lib, 1, max_depth=3)
    assert text in ("__a__", "__b__")
    assert any("太深" in w for w in warns)


def test_expand_depth_zero_keeps_token():
    text, warns = m.expand("__a__", {"a": ["x"]}, 1, max_depth=0)
    assert text == "__a__" and warns
