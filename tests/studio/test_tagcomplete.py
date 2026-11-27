"""TagComplete 標籤補全模組、設定與 /api/tags/suggest 端點測試。"""
from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from t2vn import tagcomplete
from t2vn.server import create_app


def _write_dicts(tmp_path):
    en = tmp_path / "danbooru.csv"
    en.write_text(
        'long_hair,0,500000,"long hair,flowing hair"\n'
        'blue_eyes,0,400000,"azure eyes"\n'
        'wlop,1,80000,"wlop_art"\n'
        'witch_hat,0,120000,"wizard hat"\n',
        encoding="utf-8",
    )
    zh = tmp_path / "danbooru-0-zh.csv"
    zh.write_text(
        "long_hair,0,長髮|長頭髮\n"
        "blue_eyes,0,藍眼|藍眼睛\n"
        "witch_hat,0,魔女帽|巫師帽\n",
        encoding="utf-8",
    )
    return str(en), str(zh)


def test_suggest_tags_english_alias_artist_and_chinese(tmp_path):
    en, zh = _write_dicts(tmp_path)

    # 英文前綴＋中文附註
    r_en = tagcomplete.suggest_tags("long", en, zh)
    assert r_en["has_dict"] is True
    assert r_en["items"][0]["tag"] == "long hair"
    assert r_en["items"][0]["zh"] == "長髮"

    # 別名反查
    r_alias = tagcomplete.suggest_tags("azure", en, zh)
    assert r_alias["items"][0]["tag"] == "blue eyes"
    assert r_alias["items"][0]["alias"] == "azure eyes"

    # 畫師 @ 優先
    r_artist = tagcomplete.suggest_tags("@wl", en, zh)
    assert len(r_artist["items"]) == 1
    assert r_artist["items"][0]["insert"] == "@wlop"
    assert r_artist["items"][0]["cat"] == "1"

    # 中文反查
    r_zh = tagcomplete.suggest_tags("魔女", en, zh)
    assert r_zh["items"][0]["tag"] == "witch hat"
    assert r_zh["items"][0]["zh"] == "魔女帽"

    # 無字典
    assert tagcomplete.suggest_tags("long", "", "") == {"items": [], "has_dict": False}


def test_resolve_dict_paths_auto_detects_forge_extension(tmp_path):
    tags_dir = tmp_path / "forge" / "extensions" / "sd-webui-tagcomplete-neo" / "tags"
    tags_dir.mkdir(parents=True)
    (tags_dir / "danbooru.csv").write_text("1girl,0,1000\n", encoding="utf-8")
    (tags_dir / "danbooru-0-zh.csv").write_text("1girl,0,一名女孩\n", encoding="utf-8")
    en, zh = tagcomplete.resolve_dict_paths(forge_dir=str(tmp_path / "forge"))
    assert en.endswith("danbooru.csv")
    assert zh.endswith("danbooru-0-zh.csv")


def test_api_tags_suggest_uses_config_paths(tmp_path, monkeypatch):
    from tests.config.test_config import make

    en, zh = _write_dicts(tmp_path)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg, _ = make(cfg_dir)
    cfg = replace(cfg, tag_csv=en, tag_zh_csv=zh)
    http = TestClient(create_app(cfg), base_url="http://127.0.0.1:7866")
    res = http.get("/api/tags/suggest", params={"q": "藍眼"})
    assert res.status_code == 200
    body = res.json()
    assert body["has_dict"] is True
    assert body["items"][0]["tag"] == "blue eyes"
