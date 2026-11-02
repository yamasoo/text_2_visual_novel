import copy
import json
import re
from pathlib import Path

import pytest

from t2vn.config import parse_config
from t2vn.errors import WriteProtectedError
from t2vn.export_renpy import ExportError, escape_text, export_book, main, render_export, ChapterInput
from t2vn.renpy_preflight import check_project

REPO = Path(__file__).resolve().parents[2]

CAST = {"schema_version": "0.1", "book_id": "bk", "characters": {
    "lao_zhang": {"names": {"zh-TW": "老張"}, "aliases": [], "role": "supporting", "hue": 28, "shape": "broad"},
    "ellie": {"names": {"zh-TW": "艾莉"}, "aliases": [], "role": "protagonist", "hue": 240, "shape": "tall"},
    "player": {"names": {"zh-TW": "艾莉（內心）"}, "aliases": [], "role": "player"}}}
STYLE = {"locations": {"scrapyard": {"label": "舊工坊"}, "dock": {"label": "碼頭"}}, "times": {"dawn": {"label": "清晨"}, "night": {"label": "夜晚"}}}


def scene(sid, bg="bg_scrapyard_dawn", loc="scrapyard", tod="dawn", lines=None, **extra):
    return {"scene_id": sid, "location_id": loc, "time_of_day": tod, "bg": {"asset_id": bg},
            "lines": lines or [{"type": "narration", "text": "x", "source_para": 1}], **extra}


def doc(*scenes):
    return {"schema_version": "0.1", "book_id": "bk", "chapter_id": "ch001", "lang": "zh-TW", "scenes": list(scenes)}


def render(chapters, exists=lambda r: False, audio=lambda k, a: None, **kw):
    return render_export(book_id="bk", title="書", lang="zh-TW", chapters=chapters, cast_doc=CAST, style=STYLE,
                         exists=exists, audio_file=audio, **kw)


GOLDEN = doc(
    scene("ch001_sc01", bgm="bgm_a", cast=[{"char_id": "lao_zhang", "expr": "neutral", "pos": "center"}], lines=[
        {"type": "narration", "text": "天亮了，工坊裡很安靜。", "source_para": 1},
        {"type": "dialogue", "speaker": "lao_zhang", "expr": "smile", "text": "走吧。", "source_para": 2}], next="ch001_sc02"),
    scene("ch001_sc02", lines=[{"type": "narration", "text": "完", "source_para": 3}], next=None, bgm=None))


def test_golden_matches_spec_example():
    out = render([ChapterInput("ch001", 1, "補囊", GOLDEN)], audio=lambda k, a: "audio/bgm/bgm_a.ogg")
    assert out.files["vn_ch001.rpy"] == '''label ch001:
    scene black
    centered "{size=+10}第 1 章　補囊{/size}"

label ch001_sc01:
    scene bg_scrapyard_dawn
    with dissolve
    show screen vn_caption("舊工坊・清晨")
    show lao_zhang neutral at center
    play music "audio/bgm/bgm_a.ogg" fadein 1.0
    "天亮了，工坊裡很安靜。"  # p1
    show lao_zhang smile
    c_lao_zhang "走吧。"  # p2
    jump ch001_sc02

label ch001_sc02:
    scene bg_scrapyard_dawn
    with dissolve
    show screen vn_caption("舊工坊・清晨")
    stop music fadeout 1.0
    "完"  # p3
    jump vn_end
'''


def test_escaping_and_percent_warning():
    d = doc(scene("ch001_sc01", lines=[
        {"type": "narration", "text": 'He said "hi" [x] {b} \\ 50%', "source_para": 1}]))
    out = render([ChapterInput("ch001", 1, "", d)])
    assert '"He said \\"hi\\" [[x] {{b} \\\\ 50%"  # p1' in out.files["vn_ch001.rpy"]
    assert any("'%'" in w for w in out.warnings)
    assert escape_text("a\nb\tc") == "a b c"


def test_characters_images_theme_and_manifest():
    out = render([ChapterInput("ch001", 1, "補囊", GOLDEN)], font_file="fonts/vn_font.ttf",
                 audio=lambda k, a: "audio/bgm/bgm_a.ogg")
    assert 'define c_lao_zhang = Character("老張", who_color="#' in out.files["vn_characters.rpy"]
    assert "define c_player = Character(None, what_italic=True)" in out.files["vn_characters.rpy"]
    assert 'image bg_scrapyard_dawn = "images/bg/bg_scrapyard_dawn.png"' in out.files["vn_images.rpy"]
    assert 'image lao_zhang smile = Transform("images/sprite/lao_zhang_smile.png", zoom=0.62)' in out.files["vn_images.rpy"]
    assert 'font "fonts/vn_font.ttf"' in out.files["vn_theme.rpy"] and "screen vn_caption" in out.files["vn_theme.rpy"]
    assert out.files["script.rpy"].endswith("label start:\n    jump ch001\n")
    m = out.manifest
    assert m["chapters"] == ["ch001"] and m["font"] == {"file": "fonts/vn_font.ttf", "present": False}
    assert m["backgrounds"] == [{"asset_id": "bg_scrapyard_dawn", "file": "images/bg/bg_scrapyard_dawn.png", "location_id": "scrapyard",
                                 "time_of_day": "dawn", "label": "舊工坊", "present": False}]
    assert {(s["char_id"], s["expr"]) for s in m["sprites"]} == {("lao_zhang", "neutral"), ("lao_zhang", "smile")}
    sprite = m["sprites"][0]
    assert sprite["hue"] == 28 and sprite["shape"] == "broad" and sprite["name"] == "老張"
    assert m["audio"] == [{"asset_id": "bgm_a", "kind": "bgm", "file": "audio/bgm/bgm_a.ogg", "present": True}]


def test_no_captions_and_missing_bgm_file():
    out = render([ChapterInput("ch001", 1, "", GOLDEN)], captions=False)
    text = out.files["vn_ch001.rpy"]
    assert "vn_caption" not in text and "# bgm bgm_a: file not found, skipped" in text and "play music" not in text and "stop music" not in text
    assert out.manifest["audio"][0]["present"] is False


def test_bgm_not_replayed_when_unchanged_and_ambience_channel():
    d = doc(scene("ch001_sc01", bgm="bgm_a", ambience="amb_x"), scene("ch001_sc02", bgm="bgm_a", ambience="amb_x", next="ch001_sc03"),
            scene("ch001_sc03", bgm="bgm_b", ambience=None))
    out = render([ChapterInput("ch001", 1, "", d)], audio=lambda k, a: f"audio/{k}/{a}.ogg")
    text = out.files["vn_ch001.rpy"]
    assert text.count("play music") == 2 and text.count('play ambience "audio/ambience/amb_x.ogg"') == 1
    assert "stop ambience fadeout 1.0" in text and 'play music "audio/bgm/bgm_b.ogg" fadein 1.0' in text
    assert 'register_channel("ambience"' in out.files["vn_theme.rpy"]
    theme_text = out.files["vn_theme.rpy"]
    for pattern in ("game/saves/**", "game/vn_verify_test.rpy*", "game/cache/**", "tests/**"):
        assert f'build.classify("{pattern}", None)' in theme_text
    assert 'config.name = "書"' in theme_text and 'config.window_title = "書"' in theme_text and "init 1 python:" in theme_text
    assert 'config.version = ""' in theme_text and 'build.version = "1.0"' in theme_text
    assert "style main_menu_frame:" in theme_text and "style navigation_button_text:" in theme_text
    assert 'build.classify("game/images/placeholders.json", None)' in theme_text


def test_directions_and_fade_restore():
    lines = [{"type": "direction", "effect": "shake"}, {"type": "direction", "effect": "flash"},
             {"type": "direction", "effect": "none"}, {"type": "direction", "effect": "fade_out"},
             {"type": "dialogue", "speaker": "ellie", "expr": "sad", "text": "黑暗中", "source_para": 1},
             {"type": "direction", "effect": "fade_in"}]
    d = doc(scene("ch001_sc01", cast=[{"char_id": "ellie", "expr": "neutral", "pos": "left"}], lines=lines))
    body = render([ChapterInput("ch001", 1, "", d)]).files["vn_ch001.rpy"].split("show screen")[1]
    tail = body.split("\n", 1)[1]
    assert "with vpunch\n    with vn_flash\n    scene black\n    with fade\n" in tail
    assert 'c_ellie "黑暗中"' in tail and tail.count("show ellie sad") == 0  # 黑場中不重新顯示立繪
    assert "    scene bg_scrapyard_dawn\n    show ellie neutral at left\n    with fade\n" in tail  # fade_in 還原畫面


def test_player_and_unknown_speaker():
    d = doc(scene("ch001_sc01", lines=[{"type": "dialogue", "speaker": "player", "text": "唉", "source_para": 1},
                                        {"type": "dialogue", "speaker": "ghost", "expr": "angry", "text": "嗚", "source_para": 2}]))
    out = render([ChapterInput("ch001", 1, "", d)])
    text = out.files["vn_ch001.rpy"]
    assert 'c_player "唉"' in text and 'c_ghost "嗚"' in text and "show ghost" not in text
    assert 'define c_ghost = Character("ghost"' in out.files["vn_characters.rpy"]
    assert any("ghost" in w for w in out.warnings)


def test_jumps_between_chapters_and_ends():
    c1 = ChapterInput("ch001", 1, "甲", doc(scene("ch001_sc01", next=None)))
    c2 = ChapterInput("ch002", 2, "乙", doc(scene("ch002_sc01", next="end_good"), scene("ch002_sc02", next="ch009_sc01")))
    out = render([c2, c1])  # 傳入順序不影響
    assert "    jump ch002\n" in out.files["vn_ch001.rpy"]
    assert out.files["script.rpy"].endswith("jump ch001\n")
    t2 = out.files["vn_ch002.rpy"]
    assert t2.count("jump vn_end") == 2 and any("ch009_sc01" in w for w in out.warnings)


def test_choice_and_bad_ids_and_duplicates_are_errors():
    choice = doc(scene("ch001_sc01", lines=[{"type": "choice", "prompt": "?", "options": []}]))
    with pytest.raises(ExportError, match="choice"):
        render([ChapterInput("ch001", 1, "", choice)])
    with pytest.raises(ExportError, match="ASCII"):
        render([ChapterInput("ch001", 1, "", doc(scene("ch001_sc01", bg="背景")))])
    with pytest.raises(ExportError, match="duplicate"):
        render([ChapterInput("ch001", 1, "", doc(scene("ch001_sc01"), scene("ch001_sc01")))])
    with pytest.raises(ExportError):
        render([])


def test_all_names_are_ascii_and_labels_unique():
    out = render([ChapterInput("ch001", 1, "補囊", GOLDEN)])
    labels = []
    for name, text in out.files.items():
        for m in re.finditer(r"^label (\S+):", text, re.M):
            assert m.group(1).isascii()
            labels.append(m.group(1))
        for m in re.finditer(r"^(?:image|define) ([^=]+?) =", text, re.M):
            assert m.group(1).isascii()
    assert len(labels) == len(set(labels)) and {"start", "vn_end", "ch001", "ch001_sc01", "ch001_sc02"} <= set(labels)


# ----------------------------------------------------------- 檔案系統

def make_env(tmp_path, scenes_dir="scenes"):
    project = tmp_path / "project"
    works = project / "works"
    work = works / "bk"
    library = tmp_path / "library"
    (library / "bookdir").mkdir(parents=True)
    (work / scenes_dir).mkdir(parents=True)
    (work / "chapters").mkdir()
    (work / "prototype").mkdir()
    (work / scenes_dir / "ch001.json").write_text(json.dumps(GOLDEN, ensure_ascii=False), encoding="utf-8")
    (work / "cast.json").write_text(json.dumps(CAST, ensure_ascii=False), encoding="utf-8")
    (work / "book.json").write_text(json.dumps({"display_name": {"zh-TW": "書名"}}, ensure_ascii=False), encoding="utf-8")
    (work / "chapters" / "ch001.json").write_text(json.dumps({"chapter_no": 1, "title": "補囊"}, ensure_ascii=False), encoding="utf-8")
    (work / "prototype" / "style.json").write_text(json.dumps(STYLE, ensure_ascii=False), encoding="utf-8")
    config = parse_config({"project_root": str(project), "book_library": {"path": str(library), "access": "read_only"},
                           "books": [{"book_id": "bk", "source_dir": "bookdir", "lang": "zh-TW", "display_name": {"zh-TW": "書名"}}]})
    renpy = tmp_path / "renpy_project"
    (renpy / "game").mkdir(parents=True)
    return config, work, renpy


def test_export_book_writes_files_and_preflight_is_clean(tmp_path):
    config, work, renpy = make_env(tmp_path)
    result = export_book(config, "bk", [1], renpy, placeholders=True)
    game = renpy / "game"
    for name in ("script.rpy", "vn_theme.rpy", "vn_characters.rpy", "vn_images.rpy", "vn_ch001.rpy", "vn_end.rpy", "vn_export.json"):
        assert (game / name).is_file()
    assert b"\r\n" not in (game / "vn_ch001.rpy").read_bytes()
    assert result.placeholders and (game / "images/bg/bg_scrapyard_dawn.png").is_file()
    assert [i for i in result.preflight if i["level"] == "error"] == []
    assert check_project(renpy) == []  # 補了佔位圖，連警告都沒有
    manifest = json.loads((game / "vn_export.json").read_text(encoding="utf-8"))
    assert manifest["title"] == "書名" and manifest["backgrounds"][0]["present"] is False  # 匯出當下尚未補圖


def test_real_assets_are_copied_and_audio_played(tmp_path):
    config, work, renpy = make_env(tmp_path)
    (work / "assets" / "bg").mkdir(parents=True)
    (work / "assets" / "bg" / "bg_scrapyard_dawn.png").write_bytes(b"png")
    (work / "assets" / "bgm").mkdir()
    (work / "assets" / "bgm" / "bgm_a.mp3").write_bytes(b"mp3")
    result = export_book(config, "bk", [1], renpy)
    game = renpy / "game"
    assert (game / "images/bg/bg_scrapyard_dawn.png").read_bytes() == b"png"
    assert 'play music "audio/bgm/bgm_a.mp3" fadein 1.0' in (game / "vn_ch001.rpy").read_text(encoding="utf-8")
    manifest = json.loads((game / "vn_export.json").read_text(encoding="utf-8"))
    assert manifest["backgrounds"][0]["present"] is True and manifest["audio"][0]["file"] == "audio/bgm/bgm_a.mp3"
    assert not list(work.rglob("vn_*"))  # 作品資料夾不被改動


def test_gold_fallback_and_missing_chapter(tmp_path):
    config, work, renpy = make_env(tmp_path, scenes_dir="gold")
    export_book(config, "bk", [1], renpy)
    with pytest.raises(ExportError, match="chapter 2"):
        export_book(config, "bk", [1, 2], renpy)


def test_font_copy_and_validation(tmp_path):
    config, work, renpy = make_env(tmp_path)
    font = tmp_path / "SomeFont.ttf"
    font.write_bytes(b"font")
    export_book(config, "bk", [1], renpy, font=font)
    assert (renpy / "game/fonts/vn_font.ttf").read_bytes() == b"font"
    assert json.loads((renpy / "game/vn_export.json").read_text(encoding="utf-8"))["font"] == {"file": "fonts/vn_font.ttf", "present": True}
    with pytest.raises(ExportError):
        export_book(config, "bk", [1], renpy, font=tmp_path / "x.woff2")


def test_clean_and_stale_chapter_warning(tmp_path):
    config, work, renpy = make_env(tmp_path)
    (renpy / "game" / "vn_ch007.rpy").write_text("label ch007:\n    return\n", encoding="utf-8")
    (renpy / "game" / "keep.rpy").write_text("# mine\n", encoding="utf-8")
    result = export_book(config, "bk", [1], renpy)
    assert any("vn_ch007.rpy" in w for w in result.warnings) and (renpy / "game/vn_ch007.rpy").exists()
    export_book(config, "bk", [1], renpy, clean=True)
    assert not (renpy / "game/vn_ch007.rpy").exists() and (renpy / "game/keep.rpy").exists()


def test_project_without_game_dir_is_rejected_and_standalone_mode(tmp_path):
    config, work, renpy = make_env(tmp_path)
    with pytest.raises(ExportError, match="launcher"):
        export_book(config, "bk", [1], tmp_path / "empty")
    result = export_book(config, "bk", [1])
    assert result.game_dir == work / "build" / "renpy" / "game" and (result.game_dir / "vn_ch001.rpy").is_file()


def test_cannot_write_into_read_only_library(tmp_path):
    config, work, renpy = make_env(tmp_path)
    bad = tmp_path / "library" / "bookdir"
    (bad / "game").mkdir()
    with pytest.raises(WriteProtectedError):
        export_book(config, "bk", [1], bad)


def test_cli(tmp_path, monkeypatch, capsys):
    config, work, renpy = make_env(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"project_root: {json.dumps(str(config.project_root))}\nbook_library:\n  path: {json.dumps(str(config.book_library))}\n  access: read_only\n"
                   "books:\n  - book_id: bk\n    source_dir: bookdir\n    lang: zh-TW\n    display_name:\n      zh-TW: 書\n", encoding="utf-8")
    assert main(["--book", "bk", "--chapters", "1", "--project", str(renpy), "--config", str(cfg), "--placeholders"]) == 0
    assert "preflight 0 error(s), 0 warning(s)" in capsys.readouterr().out
    assert main(["--book", "bk", "--chapters", "3", "--project", str(renpy), "--config", str(cfg)]) == 1


@pytest.mark.skipif(not (REPO / "works/sample_book/gold/ch001.json").is_file(), reason="needs the sample_book gold set")
def test_sample_book_gold_chapter_one_exports_cleanly(tmp_path):
    work = tmp_path / "project" / "works" / "sample_book"
    for rel in ("gold/ch001.json", "cast.json", "book.json", "chapters/ch001.json", "prototype/style.json"):
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_bytes((REPO / "works/sample_book" / rel).read_bytes())
    library = tmp_path / "library"
    (library / "b").mkdir(parents=True)
    config = parse_config({"project_root": str(tmp_path / "project"), "book_library": {"path": str(library), "access": "read_only"},
                           "books": [{"book_id": "sample_book", "source_dir": "b", "lang": "zh-TW", "display_name": {"zh-TW": "示範"}}]})
    renpy = tmp_path / "rp"
    (renpy / "game").mkdir(parents=True)
    result = export_book(config, "sample_book", [1], renpy, placeholders=True)
    assert [i for i in result.preflight if i["level"] == "error"] == []
    text = (renpy / "game/vn_ch001.rpy").read_text(encoding="utf-8")
    assert text.count("\n    jump ") == 4 and text.count("# p") == 82 + 33
    assert 'label ch001_sc04:' in text and text.rstrip().endswith("jump vn_end")
    source = json.loads((REPO / "works/sample_book/gold/ch001.json").read_text(encoding="utf-8"))
    spoken = [l["text"] for s in source["scenes"] for l in s["lines"] if "text" in l]
    assert len(spoken) == 115 and all(t.replace('"', '\\"') in text or escape_text(t) in text for t in spoken)


def test_expression_falls_back_to_neutral_sprite_and_side_headshot():
    have = {"images/sprite/lao_zhang_neutral.png", "images/headshot/lao_zhang_neutral.png", "images/headshot/lao_zhang_smile.png"}
    out = render([ChapterInput("ch001", 1, "", GOLDEN)], exists=lambda r: r in have)
    images = out.files["vn_images.rpy"]
    assert 'image lao_zhang neutral = Transform("images/sprite/lao_zhang_neutral.png", zoom=0.62)' in images
    assert 'image lao_zhang smile = Transform("images/sprite/lao_zhang_neutral.png", zoom=0.62)' in images  # 缺 smile 立繪 → 退回 neutral
    assert 'image side lao_zhang smile = Transform("images/headshot/lao_zhang_smile.png", zoom=0.3)' in images
    assert 'image side lao_zhang neutral = Transform("images/headshot/lao_zhang_neutral.png", zoom=0.3)' in images
    assert 'image=\"lao_zhang\"' in out.files["vn_characters.rpy"]
    smile = next(s for s in out.manifest["sprites"] if s["expr"] == "smile")
    assert smile["fallback"] is True and smile["present"] is True and smile["headshot"].endswith("lao_zhang_smile.png")


def test_no_headshot_means_no_side_image_and_plain_character():
    out = render([ChapterInput("ch001", 1, "", GOLDEN)])
    assert "image side" not in out.files["vn_images.rpy"]
    assert "image=" not in out.files["vn_characters.rpy"]


def test_export_book_copies_fallback_files(tmp_path):
    config, work, renpy = make_env(tmp_path)
    (work / "assets" / "sprite").mkdir(parents=True)
    (work / "assets" / "headshot").mkdir()
    (work / "assets" / "sprite" / "lao_zhang_neutral.png").write_bytes(b"s")
    (work / "assets" / "headshot" / "lao_zhang_smile.png").write_bytes(b"h")
    export_book(config, "bk", [1], renpy)
    game = renpy / "game"
    assert (game / "images/sprite/lao_zhang_neutral.png").read_bytes() == b"s"
    assert (game / "images/headshot/lao_zhang_smile.png").read_bytes() == b"h"
    assert 'image lao_zhang smile = Transform("images/sprite/lao_zhang_neutral.png", zoom=0.62)' in (game / "vn_images.rpy").read_text(encoding="utf-8")


def test_spread_positions_keeps_distinct_and_spreads_overlaps():
    from t2vn import export_renpy as m

    names, used = m.spread_positions([{"pos": "left"}, {"pos": "right"}])
    assert names == ["left", "right"] and not used
    names, used = m.spread_positions([{"pos": "left"}, {"pos": "right"}, {"pos": "center"}, {"pos": "center"}])
    assert names == ["vn_x12_c4", "vn_x88_c4", "vn_x37_c4", "vn_x63_c4"] and used == {"vn_x12_c4", "vn_x37_c4", "vn_x63_c4", "vn_x88_c4"}
    names, used = m.spread_positions([{"pos": "center"}, {"pos": "center"}])
    assert names == ["vn_x12", "vn_x88"] and used == {"vn_x12", "vn_x88"}
    names, _ = m.spread_positions([{"pos": "center"}] * 3)
    assert names == ["vn_x12", "vn_x50", "vn_x88"]


def test_crowd_zoom_and_theme_transforms():
    from t2vn import export_renpy as m

    assert [m.crowd_zoom(n) for n in (1, 3, 4, 5, 6, 9)] == [1.0, 1.0, 0.85, 0.65, 0.55, 0.55]
    names, used = m.spread_positions([{"pos": "center"}] * 5)
    assert names[0] == "vn_x12_c5" and len(used) == 5
    cast = [{"char_id": "ellie_reed", "expr": "neutral", "pos": "center"}] * 5
    doc = {**GOLDEN, "scenes": [{**GOLDEN["scenes"][0], "cast": cast}]}
    theme = render([ChapterInput("ch001", 1, "補囊", doc)]).files["vn_theme.rpy"]
    assert "transform vn_x12_c5:" in theme and "    zoom 0.65" in theme and "transform vn_x50_c5:" in theme


def test_title_background_and_music_are_wired_only_when_present():
    chapters = [ChapterInput("ch001", 1, "補囊", GOLDEN)]
    plain = render(chapters).files["vn_theme.rpy"]
    assert "main_menu_background" not in plain and "main_menu_music" not in plain
    out = render(chapters, exists=lambda r: r == "images/bg/bg_title.png",
                 audio=lambda k, a: "audio/bgm/bgm_title.opus" if a == "bgm_title" else None)
    theme = out.files["vn_theme.rpy"]
    assert 'define gui.main_menu_background = "images/bg/bg_title.png"' in theme
    assert 'define config.main_menu_music = "audio/bgm/bgm_title.opus"' in theme


def test_export_book_copies_title_assets(tmp_path):
    config, work, renpy = make_env(tmp_path)
    (work / "assets" / "bg").mkdir(parents=True)
    (work / "assets" / "bg" / "bg_title.png").write_bytes(b"png")
    (work / "assets" / "bgm").mkdir()
    (work / "assets" / "bgm" / "bgm_title.opus").write_bytes(b"opus")
    export_book(config, "bk", [1], renpy)
    game = renpy / "game"
    assert (game / "images/bg/bg_title.png").is_file() and (game / "audio/bgm/bgm_title.opus").is_file()
    theme = (game / "vn_theme.rpy").read_text(encoding="utf-8")
    assert "main_menu_background" in theme and 'main_menu_music = "audio/bgm/bgm_title.opus"' in theme
