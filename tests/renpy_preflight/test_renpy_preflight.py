import json
from pathlib import Path

import pytest

from t2vn.renpy_preflight import build_distribute_command, build_lint_command, check_project, has_errors, main

SCRIPT = 'label start:\n    jump ch001\n'
GOOD_CH = '''label ch001:
    scene black
    centered "{size=+10}第 1 章　補囊{/size}"

label ch001_sc01:
    scene bg_dock_dawn
    with dissolve
    show screen vn_caption("碼頭・清晨")
    show ellie neutral at center
    play music "audio/bgm/bgm_a.ogg" fadein 1.0
    "天亮了，工坊裡很安靜。"  # p1
    show ellie smile
    c_ellie "走吧。[[笑]"  # p2
    jump vn_end
'''
END = 'label vn_end:\n    return\n'
CHARS = 'define c_ellie = Character("艾莉")\ndefine c_player = Character(None)\n'
IMAGES = 'image bg_dock_dawn = "images/bg/bg_dock_dawn.png"\nimage ellie neutral = "images/sprite/ellie_neutral.png"\nimage ellie smile = "images/sprite/ellie_smile.png"\n'
MANIFEST = {"schema_version": "0.1", "book_id": "demo", "title": "t", "chapters": ["ch001"], "backgrounds": [], "sprites": [], "audio": [], "font": None}


def make(tmp_path, **override):
    game = tmp_path / "game"
    files = {"script.rpy": SCRIPT, "vn_ch001.rpy": GOOD_CH, "vn_end.rpy": END, "vn_characters.rpy": CHARS, "vn_images.rpy": IMAGES,
             "vn_export.json": json.dumps(MANIFEST)}
    files.update(override)
    for name, text in files.items():
        if text is None:
            continue
        (game / name).parent.mkdir(parents=True, exist_ok=True)
        (game / name).write_text(text, encoding="utf-8")
    for rel in ("images/bg/bg_dock_dawn.png", "images/sprite/ellie_neutral.png", "images/sprite/ellie_smile.png", "audio/bgm/bgm_a.ogg"):
        (game / rel).parent.mkdir(parents=True, exist_ok=True)
        (game / rel).write_bytes(b"x")
    return tmp_path


def codes(issues, level=None):
    return sorted(i["code"] for i in issues if level is None or i["level"] == level)


def test_clean_project_has_no_issues(tmp_path):
    assert check_project(make(tmp_path)) == []


def test_missing_game_dir(tmp_path):
    assert codes(check_project(tmp_path)) == ["E_NO_GAME"]


def test_duplicate_and_bad_label_names(tmp_path):
    bad = GOOD_CH + "\nlabel ch001_sc01:\n    return\n\nlabel 第一章:\n    return\n"
    result = codes(check_project(make(tmp_path, **{"vn_ch001.rpy": bad})))
    assert "E_DUP_LABEL" in result and "E_LABEL_NAME" in result


def test_jump_target_and_required_labels(tmp_path):
    bad = GOOD_CH.replace("jump vn_end", "jump ch099")
    result = check_project(make(tmp_path, **{"vn_ch001.rpy": bad, "vn_end.rpy": "", "script.rpy": "label begin:\n    return\n"}))
    c = codes(result)
    assert c.count("E_NO_LABEL") == 2 and "E_JUMP_TARGET" in c
    assert any("ch099" in i["message"] for i in result)


def test_indentation(tmp_path):
    bad = GOOD_CH.replace('    "天亮了', '\t"天亮了').replace('    show ellie smile', '  show ellie smile')
    c = codes(check_project(make(tmp_path, **{"vn_ch001.rpy": bad})))
    assert "E_TAB" in c and "E_INDENT" in c


def test_unclosed_string_and_bracket(tmp_path):
    bad = GOOD_CH.replace('"天亮了，工坊裡很安靜。"', '"天亮了').replace('"走吧。[[笑]"', '"走吧。[笑]"')
    c = codes(check_project(make(tmp_path, **{"vn_ch001.rpy": bad})))
    assert "E_STRING" in c and "E_BRACKET" in c


def test_escaped_quote_and_comment_hash_inside_string_are_fine(tmp_path):
    ok = GOOD_CH.replace('"天亮了，工坊裡很安靜。"', '"他說：\\"好 # 嗎\\"。"')
    assert check_project(make(tmp_path, **{"vn_ch001.rpy": ok})) == []


def test_percent_is_warning_only(tmp_path):
    ok = GOOD_CH.replace("工坊裡很安靜", "工坊裡 50% 很安靜")
    result = check_project(make(tmp_path, **{"vn_ch001.rpy": ok}))
    assert codes(result) == ["W_PERCENT"] and not has_errors(result)


def test_undefined_character_and_image(tmp_path):
    bad = GOOD_CH.replace("c_ellie \"走吧", "c_ghost \"走吧").replace("scene bg_dock_dawn", "scene bg_nowhere").replace("show ellie smile", "show ellie angry")
    c = codes(check_project(make(tmp_path, **{"vn_ch001.rpy": bad})))
    assert c.count("E_IMAGE") == 2 and "E_CHAR" in c


def test_show_tag_only_is_ok(tmp_path):
    ok = GOOD_CH.replace("show ellie smile", "show ellie")
    assert check_project(make(tmp_path, **{"vn_ch001.rpy": ok})) == []


def test_audio_missing_is_error_image_missing_is_warning(tmp_path):
    root = make(tmp_path)
    (root / "game/audio/bgm/bgm_a.ogg").unlink()
    (root / "game/images/sprite/ellie_smile.png").unlink()
    result = check_project(root)
    assert codes(result, "error") == ["E_MISSING_AUDIO"] and codes(result, "warning") == ["W_MISSING_IMAGE"]


def test_non_ascii_image_name(tmp_path):
    result = check_project(make(tmp_path, **{"vn_images.rpy": IMAGES + 'image 艾莉 smile = "images/sprite/x.png"\n'}))
    assert "E_IMAGE_NAME" in codes(result)


def test_manifest_checks(tmp_path):
    assert "E_EXPORT" in codes(check_project(make(tmp_path, **{"vn_export.json": "{bad"})))
    manifest = {**MANIFEST, "chapters": ["ch001", "ch002"], "font": {"file": "fonts/vn_font.ttf", "present": True}}
    c = codes(check_project(make(tmp_path, **{"vn_export.json": json.dumps(manifest)})))
    assert "E_CHAPTER_LABEL" in c and "E_FONT" in c


def test_non_vn_rpy_files_are_ignored(tmp_path):
    root = make(tmp_path, **{"other.rpy": "label bad name:\n\tjump nowhere\n"})
    assert check_project(root) == []


def test_issues_are_sorted_and_shaped(tmp_path):
    bad = GOOD_CH.replace("jump vn_end", "jump a1").replace('"走吧。[[笑]"', '"走吧。[笑]"')
    result = check_project(make(tmp_path, **{"vn_ch001.rpy": bad}))
    for i in result:
        assert set(i) == {"level", "code", "file", "line", "message"} and i["level"] in ("error", "warning")
    keys = [(i["file"], i["line"], i["code"]) for i in result]
    assert keys == sorted(keys)


def test_commands(tmp_path):
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    with pytest.raises(FileNotFoundError):
        build_lint_command(sdk, tmp_path / "p")
    (sdk / "renpy.sh").write_text("", encoding="utf-8")
    assert build_lint_command(sdk, tmp_path / "p") == [str(sdk / "renpy.sh"), str(tmp_path / "p"), "lint"]
    assert build_distribute_command(sdk, tmp_path / "p", "mac") == [str(sdk / "renpy.sh"), "launcher", "distribute", str(tmp_path / "p"), "--package", "mac"]
    with pytest.raises(ValueError):
        build_distribute_command(sdk, tmp_path / "p", "dos")
    (sdk / "renpy.exe").write_text("", encoding="utf-8")
    assert build_lint_command(sdk, tmp_path / "p")[0] == str(sdk / "renpy.exe")  # exe 優先


def test_cli(tmp_path, capsys):
    root = make(tmp_path / "ok")
    assert main(["--project", str(root)]) == 0
    assert "0 error(s), 0 warning(s)" in capsys.readouterr().out
    bad = make(tmp_path / "bad", **{"vn_ch001.rpy": GOOD_CH.replace("jump vn_end", "jump zz")})
    assert main(["--project", str(bad)]) == 1
    assert "E_JUMP_TARGET" in capsys.readouterr().out
    assert main(["--project", str(root), "--sdk", str(tmp_path / "nosdk")]) == 1


def test_crlf_and_bom_files_are_accepted(tmp_path):
    root = make(tmp_path)
    for f in (root / "game").glob("*.rpy"):
        f.write_bytes(b"\xef\xbb\xbf" + f.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    assert check_project(root) == []
