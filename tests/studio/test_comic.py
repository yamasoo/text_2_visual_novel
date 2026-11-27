"""漫畫專案（I4-B）：目錄骨架、來源章節、角色卡、路徑守門、編譯第一格。"""
import dataclasses
import json
from pathlib import Path

import pytest

from t2vn import comic
from t2vn.config import load_config
from vendor.t2c import workspace as t2c_workspace

CHAPTER = {
    "schema_version": "0.1", "book_id": "demo", "chapter_id": "ch001", "chapter_no": 1,
    "title": "大夜班", "source_file": "demo.txt", "sha256": "x", "char_count": 0,
    "paragraphs": [
        {"para_no": 1, "text": "凌晨兩點，店裡只剩冷氣的聲音。"},
        {"para_no": 2, "text": "她把手上的關東煮放下。"},
        {"para_no": 3, "text": "「走吧。」"},
    ],
    "scene_breaks": [],
}

CAST = {
    "schema_version": "0.1", "book_id": "demo",
    "characters": {
        "ellie": {"names": {"zh-TW": "艾莉"}, "aliases": []},
        "old_zhang": {"names": {"zh-TW": "老張"}, "aliases": []},
        "Bad ID": {"names": {"zh-TW": "不合法的 id"}, "aliases": []},
    },
}

STORYBOARD = {
    "version": 1, "chapter": 1, "title": "大夜班",
    "pages": [{"page": 1, "template": "2h", "panels": [
        {"slot": 1, "id": "p001_k1", "shot": "medium", "angle": "eye",
         "scene": "A young woman in a dim convenience store aisle, holding a paper cup.",
         "characters": [{"id": "ellie", "pose": "standing at the counter", "expression": "neutral",
                         "box": [0.2, 0.1, 0.8, 1]}],
         "bubbles": [{"kind": "speech", "speaker": "ellie", "text": "走吧。", "anchor": [0.3, 0.2]}]},
        {"slot": 2, "id": "p001_k2", "shot": "wide", "angle": "high",
         "scene": "An empty street at night.", "characters": [],
         "bubbles": [{"kind": "narration", "text": "凌晨兩點。", "anchor": [0.5, 0.1]}]},
    ]}],
}


@pytest.fixture()
def env(tmp_path):
    config = dataclasses.replace(load_config(), works_dir_override=tmp_path)
    work = tmp_path / "demo"
    (work / "chapters").mkdir(parents=True)
    (work / "art" / "appearance").mkdir(parents=True)
    (work / "book.json").write_text(json.dumps({"book_id": "demo"}, ensure_ascii=False), encoding="utf-8")
    (work / "chapters" / "ch001.json").write_text(json.dumps(CHAPTER, ensure_ascii=False), encoding="utf-8")
    (work / "cast.json").write_text(json.dumps(CAST, ensure_ascii=False), encoding="utf-8")
    (work / "art" / "appearance" / "ellie.json").write_text(
        json.dumps({
            "char_id": "ellie",
            "headshot_prompt": "long black hair, brown eyes",
            "sprite_prompt": "school uniform, sneakers",
            "t2c": {
                "sections": {"head": ["a 17-year-old girl", "long black hair", "brown eyes"],
                             "body": ["a slim build"]},
                "outfits": {"default": {"set": ["a navy school uniform"]}},
            },
        }, ensure_ascii=False), encoding="utf-8")
    return config, work


def test_create_project_writes_skeleton(env):
    config, _ = env
    info = comic.create_project("demo", "ch001", "大夜班", config=config)
    root = Path(info["path"])
    assert (root / "project.yaml").is_file()
    assert (root / "bible" / "characters").is_dir()
    assert (root / "source").is_dir()
    assert info["preset"]


def test_create_project_refuses_non_empty_folder(env):
    config, _ = env
    comic.create_project("demo", "ch001", "大夜班", config=config)
    with pytest.raises(comic.ComicError, match="已經存在而且不是空的"):
        comic.create_project("demo", "ch001", "大夜班", config=config)


def test_project_id_rejects_path_traversal(env):
    config, _ = env
    for bad in ("../x", "Ch001", "", "a/../b"):
        with pytest.raises(comic.ComicError, match="project_id"):
            comic.create_project("demo", bad, "x", config=config)


def test_write_source_roundtrip(env):
    config, _ = env
    comic.create_project("demo", "ch001", "大夜班", config=config)
    info = comic.write_source("demo", "ch001", 1, config)
    text = Path(info["path"]).read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# 第1章 大夜班"
    assert text.splitlines()[1] == ""
    assert info["paragraphs"] == len(CHAPTER["paragraphs"])


def test_write_source_detects_swallowed_paragraph(env):
    config, work = env
    broken = dict(CHAPTER)
    broken["paragraphs"] = CHAPTER["paragraphs"] + [{"para_no": 4, "text": "# 這一行會被當成標題"}]
    (work / "chapters" / "ch001.json").write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    comic.create_project("demo", "ch001", "大夜班", config=config)
    with pytest.raises(comic.ComicError, match="段落對不上"):
        comic.write_source("demo", "ch001", 1, config)


def test_from_cast_writes_bible_cards(env):
    config, _ = env
    info = comic.create_project("demo", "ch001", "大夜班", config=config)
    root = Path(info["path"])
    result = comic.from_cast("demo", "ch001", config=config)
    # written＝進定案目錄的卡（含佔位）；placeholder 另外標出哪幾張是佔位
    assert result["written"] == ["ellie", "old_zhang"]
    assert result["drafts"] == []
    assert result["placeholder"] == ["old_zhang"]
    assert result["skipped_ids"] == ["Bad ID"]
    ellie = (root / "bible" / "characters" / "ellie.md").read_text(encoding="utf-8")
    assert "sections:" in ellie and "a navy school uniform" in ellie
    assert "sneakers" not in ellie                 # 不再把 sprite_prompt 抄成 look_en
    zhang = (root / "bible" / "characters" / "old_zhang.md").read_text(encoding="utf-8")
    assert comic.PLACEHOLDER_LOOK in zhang
    marker = json.loads((root / comic.PLACEHOLDERS_FILE).read_text(encoding="utf-8"))
    assert marker["placeholder"] == ["old_zhang"]
    project = comic.open_project("demo", "ch001", config)
    assert set(project.characters) == {"ellie", "old_zhang"}


def test_from_cast_keeps_existing_cards(env):
    config, _ = env
    info = comic.create_project("demo", "ch001", "大夜班", config=config)
    card = Path(info["path"]) / "bible" / "characters" / "ellie.md"
    card.write_text("---\nid: ellie\nlook_en: hand written look\n---\n", encoding="utf-8")
    comic.from_cast("demo", "ch001", config=config)
    assert "hand written look" in card.read_text(encoding="utf-8")


def test_compile_first_panel(env):
    config, _ = env
    info = comic.create_project("demo", "ch001", "大夜班", config=config)
    root = Path(info["path"])
    comic.from_cast("demo", "ch001", config=config)
    (root / "storyboard").mkdir(parents=True, exist_ok=True)
    (root / "storyboard" / "ch001.json").write_text(json.dumps(STORYBOARD, ensure_ascii=False), encoding="utf-8")
    project = comic.open_project("demo", "ch001", config)
    job = t2c_workspace.compile_one(project, 1, "p001_k1")
    assert job["id"] == "p001_k1"
    assert job["prompt"].strip()
    assert job["width"] > 0 and job["height"] > 0


def test_assert_inside_blocks_escape(env):
    config, _ = env
    info = comic.create_project("demo", "ch001", "大夜班", config=config)
    root = Path(info["path"])
    inside = comic.assert_inside(root, "source/ch001.txt")
    assert inside == (root / "source" / "ch001.txt").resolve()
    for bad in ("../cast.json", "../../../../config.yaml"):
        with pytest.raises(comic.ComicError, match="跑出漫畫專案之外"):
            comic.assert_inside(root, bad)


def test_comic_cfg_uses_config_base_url():
    cfg = comic.comic_cfg()
    assert cfg["comfy"]["url"] == load_config().comfyui_base_url
    assert comic.render_profile()["workflow"].is_file()


def test_project_style_and_loras_override_and_inject(env):
    from dataclasses import replace

    config, _ = env
    config = replace(
        config,
        comfy_default_profile="anima",
        comfy_profiles={
            "anima": {
                "kind": "anima", "workflow": "",
                "models": {"unet": "u.safetensors", "clip": "c.safetensors", "vae": "v.safetensors"},
                "negative": "", "default_preset": "", "presets": {},
                "loras": [{"name": "global_style.safetensors", "strength": 0.6, "on": True}],
            },
        },
    )
    comic.create_project("demo", "ch001", "大夜班", config=config)

    # 先存字型，確認之後存畫風與 LoRA 不會蓋掉字型
    comic.save_settings("demo", "ch001", {"dialogue_pt": 11.0}, config)
    out = comic.save_settings(
        "demo", "ch001", None, config,
        project={
            "comfy_profile": "anima",
            "style_preset": "color",
            "style": "watercolor manga",
            "meta": "full color, soft light",
            "look": "水彩彩漫",
            "sfx_mode": "overlay",
        },
        loras=[
            {"name": "proj_ink.safetensors", "strength": 0.8, "on": True},
            {"name": "", "strength": 1.0, "on": True},   # 空白檔名自動略過
        ],
        lora_mode="append",
    )
    assert out["lettering"]["dialogue_pt"] == 11.0
    assert out["project"]["style_preset"] == "color"
    assert out["project"]["comfy_profile"] == "anima"
    assert out["project"]["meta"] == ["full color", "soft light"]
    assert out["loras"] == [{"name": "proj_ink.safetensors", "strength": 0.8, "on": True}]
    assert out["lora_mode"] == "append"

    # append 模式：全域 profile LoRA 在前、專案 LoRA 在後
    cfg_append = comic.comic_cfg(config, out)
    names_append = [x["name"] for x in cfg_append["comfy"]["profiles"]["anima"]["loras"]]
    assert names_append == ["global_style.safetensors", "proj_ink.safetensors"]

    # replace 模式：只用專案 LoRA
    out_replace = comic.save_settings("demo", "ch001", None, config, lora_mode="replace")
    cfg_replace = comic.comic_cfg(config, out_replace)
    names_replace = [x["name"] for x in cfg_replace["comfy"]["profiles"]["anima"]["loras"]]
    assert names_replace == ["proj_ink.safetensors"]

    # 留空覆蓋欄位 → 清回預設
    cleared = comic.save_settings("demo", "ch001", None, config, project={"comfy_profile": "", "style": "", "meta": "", "look": ""})
    assert cleared["project"]["comfy_profile"] == ""
    assert cleared["project"]["style"] == ""
    assert cleared["project"]["meta"] == []
