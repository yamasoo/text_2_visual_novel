"""把 scene graph（scenes/chNNN.json 或 gold/chNNN.json）匯出成 Ren'Py 專案的 ``game/*.rpy``。

規格見 ``docs/30_RenPy匯出規格.md``。純閱讀、無選項；所有 Ren'Py 名稱只用 ``[a-z0-9_]``，中文只在字串值。

- ``render_export``：純函式（不碰檔案系統，素材是否存在由呼叫端傳入）。
- ``export_book``：讀作品資料夾、複製真素材、寫檔、（可選）補佔位圖並跑前置檢查。
- CLI：``python -m t2vn.export_renpy --book sample_book --chapters 1-5 --project <Ren'Py 專案資料夾>``
"""
from __future__ import annotations

import argparse
import colorsys
import json
import re
import shutil
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import sprite_fallback
from .errors import T2vnError, WriteProtectedError

SCHEMA_VERSION = "0.1"
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PLAYER_ID = "player"
SHAPES = ("tall", "broad", "small", "bent", "round")
AUDIO_EXTS = (".ogg", ".opus", ".mp3", ".wav", ".flac")
TITLE_BG_ID = "bg_title"    # 標題畫面背景（素材頁的「標題圖」）
TITLE_BGM_ID = "bgm_title"  # 標題畫面音樂
POS_ORDER = {"left": 0, "center": 1, "right": 2}
SLOT_LEFT, SLOT_RIGHT = 0.12, 0.88  # 多人同場時的站位範圍（xalign）


CROWD_ZOOM = {4: 0.85, 5: 0.65}  # 4 人以上同場時把立繪縮小，免得身體互疊；6 人以上用 0.55
CROWD_ZOOM_MIN = 0.55
SLOT_NAME_RE = re.compile(r"^vn_x(\d+)(?:_c(\d+))?$")


def crowd_zoom(count: int) -> float:
    """同場人數對應的額外縮放（3 人以下不縮）。"""
    if count <= 3:
        return 1.0
    return CROWD_ZOOM.get(count, CROWD_ZOOM_MIN)


def spread_positions(cast: list[dict[str, Any]]) -> tuple[list[str], set[str]]:
    """每個角色一個站位名。站位全都不重複時照原樣；有人重疊（例如兩人都在 center）就依 left→center→right 的順序平均攤開。

    回傳 ``(站位名清單, 用到的自訂站位名集合)``；攤開時站位名是 ``vn_x<百分比>``，
    4 人以上同場時是 ``vn_x<百分比>_c<人數>``（theme 裡對應的 transform 會把立繪縮小，見 :func:`crowd_zoom`）。
    """
    given = [str(m.get("pos") or "center") for m in cast]
    if len(set(given)) == len(given):
        return given, set()
    count = len(cast)
    order = sorted(range(count), key=lambda i: (POS_ORDER.get(given[i], 1), i))
    names = [""] * count
    used: set[str] = set()
    for rank, idx in enumerate(order):
        percent = round((SLOT_LEFT + (SLOT_RIGHT - SLOT_LEFT) * rank / (count - 1)) * 100)
        names[idx] = f"vn_x{percent}" + (f"_c{count}" if count >= 4 else "")
        used.add(names[idx])
    return names, used


SPRITE_ZOOM = 0.62  # 立繪（800×1600）縮到約 92% 螢幕高（1080），頭才不會被切掉
SIDE_ZOOM = 0.3  # 頭像（864×1152）縮到對話旁的大小
FONT_EXTS = (".ttf", ".otf")
TIME_LABELS = {"dawn": "清晨", "morning": "上午", "day": "白天", "dusk": "黃昏", "night": "夜晚"}
DIRECTION_LINES = {
    "shake": ["with vpunch"],
    "flash": ["with vn_flash"],
}


class ExportError(T2vnError):
    """匯出前就能確定的問題（缺檔、不支援的語句、名稱不合法）。"""


# ------------------------------------------------------------------ 小工具

def escape_text(text: str) -> str:
    """Ren'Py 字串字面值的跳脫：``\\``、``"``、``[``、``{``；換行與 Tab 換成空白。"""
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")  # 不用 split()：它會吃掉全形空白 U+3000
    backslash = chr(92)
    return (text.replace(backslash, backslash * 2).replace('"', backslash + '"').replace("[", "[[").replace("{", "{{"))


def _check_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ExportError(f"{what} {value!r} is not a valid ASCII id ([a-z][a-z0-9_]*)")
    return value


def _hue_color(hue: int, lightness: float = 0.72, saturation: float = 0.55) -> str:
    r, g, b = (round(255 * v) for v in colorsys.hls_to_rgb(hue / 360, lightness, saturation))
    return f"#{r:02x}{g:02x}{b:02x}"


def _default_hue(char_id: str) -> int:
    return zlib.crc32(char_id.encode("ascii")) % 360


def _name_of(entry: dict[str, Any] | None, lang: str, fallback: str) -> str:
    names = (entry or {}).get("names") or {}
    if isinstance(names, dict) and names:
        return str(names.get(lang) or next(iter(names.values())))
    return fallback


@dataclass
class ChapterInput:
    """一章的輸入：章 label、章號、標題、scene graph。"""

    chapter_id: str
    number: int
    title: str
    doc: dict[str, Any]


@dataclass
class Rendered:
    files: dict[str, str] = field(default_factory=dict)  # 相對 game/ 的 .rpy 檔 → 內容
    manifest: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 渲染

def render_export(
    *,
    book_id: str,
    title: str,
    lang: str,
    chapters: list[ChapterInput],
    cast_doc: dict[str, Any],
    style: dict[str, Any] | None,
    exists: Callable[[str], bool],
    audio_file: Callable[[str, str], str | None],
    font_file: str | None = None,
    captions: bool = True,
) -> Rendered:
    """產生所有 .rpy 檔的內容與 ``vn_export.json``。

    ``exists(rel)``：相對 game/ 的檔案是否存在（背景、立繪用）；
    ``audio_file(kind, asset_id)``：回傳相對 game/ 的音檔路徑（kind 為 bgm／ambience），沒有檔回傳 None。
    """
    if not chapters:
        raise ExportError("no chapters to export")
    chapters = sorted(chapters, key=lambda c: c.number)
    style = style or {}
    locations = style.get("locations") or {}
    times = style.get("times") or {}
    characters = cast_doc.get("characters") or {}
    out = Rendered()
    warn = out.warnings.append
    chapter_ids = [c.chapter_id for c in chapters]
    for cid in chapter_ids:
        if not re.match(r"^ch[0-9]{3}$", cid):
            raise ExportError(f"bad chapter id {cid!r}")
    scene_ids = {s["scene_id"] for c in chapters for s in c.doc.get("scenes", [])}

    speakers: dict[str, None] = {}
    backgrounds: dict[str, dict[str, Any]] = {}
    sprites: dict[tuple[str, str], None] = {}
    slot_names_used: set[str] = set()
    audio_ids: dict[tuple[str, str], None] = {}

    playing: dict[str, str | None] = {"bgm": None, "ambience": None}
    labels_seen: set[str] = set()

    for index, chapter in enumerate(chapters):
        next_chapter = chapters[index + 1].chapter_id if index + 1 < len(chapters) else None
        lines: list[str] = []
        emit = lines.append
        number_text = f"第 {chapter.number} 章" + (f"　{chapter.title}" if chapter.title else "")
        if chapter.chapter_id in labels_seen:
            raise ExportError(f"duplicate chapter {chapter.chapter_id}")
        labels_seen.add(chapter.chapter_id)
        emit(f"label {chapter.chapter_id}:")
        emit("    scene black")
        emit(f'    centered "{{size=+10}}{escape_text(number_text)}{{/size}}"')
        emit("")

        for scene in chapter.doc.get("scenes", []):
            sid = _check_id(scene["scene_id"], "scene_id")
            if sid in labels_seen:
                raise ExportError(f"duplicate scene label {sid}")
            labels_seen.add(sid)
            bg_id = _check_id(scene["bg"]["asset_id"], f"{sid}: bg.asset_id")
            loc_id = _check_id(scene["location_id"], f"{sid}: location_id")
            time_id = scene["time_of_day"]
            backgrounds.setdefault(bg_id, {"asset_id": bg_id, "file": f"images/bg/{bg_id}.png", "location_id": loc_id,
                                           "time_of_day": time_id,
                                           "label": str((locations.get(loc_id) or {}).get("label") or loc_id)})
            # D8：weather 在 v1 不演出
            emit(f"label {sid}:")
            emit(f"    scene {bg_id}")
            emit("    with dissolve")
            if captions:
                place = str((locations.get(loc_id) or {}).get("label") or loc_id)
                when = str((times.get(time_id) or {}).get("label") or TIME_LABELS.get(time_id, time_id))
                emit(f'    show screen vn_caption("{escape_text(place + "・" + when)}")')

            shown: dict[str, tuple[str, str]] = {}  # char_id → (expr, pos)
            cast_list = scene.get("cast") or []
            slot_names, slot_used = spread_positions(cast_list)
            slot_names_used |= slot_used
            for member, pos in zip(cast_list, slot_names, strict=True):
                cid = _check_id(member["char_id"], f"{sid}: cast char_id")
                expr = member.get("expr") or "neutral"
                shown[cid] = (expr, pos)
                sprites.setdefault((cid, expr), None)
                emit(f"    show {cid} {expr} at {pos}")
                if cid not in characters:
                    warn(f"{sid}: cast member {cid} is not in cast.json")

            for kind in ("bgm", "ambience"):
                if kind not in scene:
                    continue  # 沒寫＝沿用
                want = scene[kind]
                if want is not None:
                    _check_id(want, f"{sid}: {kind}")
                    audio_ids.setdefault((kind, want), None)
                path = audio_file(kind, want) if want else None
                target = want if path else None
                if want and not path:
                    emit(f"    # {kind} {want}: file not found, skipped")
                if target != playing[kind]:
                    channel = "music" if kind == "bgm" else "ambience"
                    if target:
                        emit(f'    play {channel} "{path}" fadein 1.0')
                    else:
                        emit(f"    stop {channel} fadeout 1.0")
                    playing[kind] = target

            blacked_state: dict[str, tuple[str, str]] | None = None
            for line in scene["lines"]:
                kind = line["type"]
                if kind == "choice":
                    raise ExportError(f"{sid}: choice lines are not supported in v1 (D1)")
                if kind == "direction":
                    effect = line["effect"]
                    if effect in DIRECTION_LINES:
                        lines.extend(f"    {s}" for s in DIRECTION_LINES[effect])
                    elif effect == "fade_out":
                        blacked_state = dict(shown)
                        shown = {}
                        emit("    scene black")
                        emit("    with fade")
                    elif effect == "fade_in":
                        if blacked_state is not None:
                            emit(f"    scene {bg_id}")
                            for cid, (expr, pos) in blacked_state.items():
                                emit(f"    show {cid} {expr} at {pos}")
                            shown, blacked_state = dict(blacked_state), None
                        emit("    with fade")
                    continue
                text = escape_text(line["text"])
                if "%" in text:
                    warn(f"{sid} p{line['source_para']}: text contains '%'")
                tail = f'  # p{line["source_para"]}'
                if kind == "narration":
                    emit(f'    "{text}"{tail}')
                    continue
                speaker = _check_id(line["speaker"], f"{sid}: speaker")
                speakers.setdefault(speaker, None)
                if speaker != PLAYER_ID and speaker in shown:
                    expr = line.get("expr")
                    if expr and expr != shown[speaker][0]:
                        emit(f"    show {speaker} {expr}")
                        sprites.setdefault((speaker, expr), None)
                        shown[speaker] = (expr, shown[speaker][1])
                emit(f'    c_{speaker} "{text}"{tail}')

            target = scene.get("next")
            if target and target.startswith("end_"):
                target = "vn_end"
            elif target and target not in scene_ids:
                warn(f"{sid}: next {target} is not in the exported chapters; jumping to the end")
                target = "vn_end"
            elif not target:
                target = next_chapter or "vn_end"
            emit(f"    jump {target}")
            emit("")

        out.files[f"vn_{chapter.chapter_id}.rpy"] = "\n".join(lines).rstrip("\n") + "\n"

    # ---- 表情圖退回（立繪只有 neutral、頭像各表情；缺的退回 neutral）
    sprite_have = {cid: {e for e in sprite_fallback.KNOWN_EXPRESSIONS if exists(f"{sprite_fallback.SPRITE_DIR}/{cid}_{e}.png")}
                   for cid in {c for c, _ in sprites}}
    head_have = {cid: {e for e in sprite_fallback.KNOWN_EXPRESSIONS if exists(f"{sprite_fallback.HEADSHOT_DIR}/{cid}_{e}.png")}
                 for cid in {c for c, _ in sprites}}
    image_plan = sprite_fallback.plan_images(list(sprites), sprite_have, head_have)
    side_chars = {str(item["char_id"]) for item in image_plan if item["headshot_expr"]}

    # ---- 角色
    char_lines = ["# 由 t2vn.export_renpy 產生；請勿手改（重新匯出會覆蓋）。"]
    for cid in speakers:
        if cid == PLAYER_ID:
            continue
        entry = characters.get(cid)
        if entry is None:
            warn(f"speaker {cid} is not in cast.json; using the id as the name")
        hue = (entry or {}).get("hue")
        hue = hue if isinstance(hue, int) and not isinstance(hue, bool) and 0 <= hue <= 359 else _default_hue(cid)
        side_arg = f', image="{cid}"' if cid in side_chars else ""
        char_lines.append(f'define c_{cid} = Character("{escape_text(_name_of(entry, lang, cid))}", who_color="{_hue_color(hue)}"{side_arg})')
    char_lines.append("define c_player = Character(None, what_italic=True)")
    out.files["vn_characters.rpy"] = "\n".join(char_lines) + "\n"

    # ---- 圖片
    image_lines = ["# 由 t2vn.export_renpy 產生；請勿手改。"]
    for bg in backgrounds.values():
        image_lines.append(f'image {bg["asset_id"]} = "{bg["file"]}"')
    sprite_items = []
    for item in image_plan:
        cid, expr = str(item["char_id"]), str(item["expr"])
        entry = characters.get(cid)
        hue = (entry or {}).get("hue")
        hue = hue if isinstance(hue, int) and not isinstance(hue, bool) and 0 <= hue <= 359 else _default_hue(cid)
        shape = (entry or {}).get("shape")
        shape = shape if shape in SHAPES else SHAPES[zlib.crc32(cid.encode("ascii")) % len(SHAPES)]
        file = f"{sprite_fallback.SPRITE_DIR}/{cid}_{item['sprite_expr'] or expr}.png"
        head = f"{sprite_fallback.HEADSHOT_DIR}/{cid}_{item['headshot_expr']}.png" if item["headshot_expr"] else None
        sprite_items.append({"char_id": cid, "expr": expr, "file": file, "name": _name_of(entry, lang, cid),
                             "hue": hue, "shape": shape, "present": exists(file),
                             "fallback": bool(item["sprite_fallback"]), "headshot": head,
                             "headshot_fallback": bool(item["headshot_fallback"])})
    for line in sprite_fallback.image_lines(image_plan):
        zoom = SIDE_ZOOM if line.startswith("image side ") else SPRITE_ZOOM
        image_lines.append(re.sub(r'^(image .+? = )("images/(?:sprite|headshot)/[^"]+")$', rf"\1Transform(\2, zoom={zoom})", line))
    out.files["vn_images.rpy"] = "\n".join(image_lines) + "\n"

    # ---- 主題
    title_bg_rel = f"images/bg/{TITLE_BG_ID}.png"
    title_music = audio_file("bgm", TITLE_BGM_ID)
    theme = ["# 由 t2vn.export_renpy 產生；請勿手改。", "",
             "define vn_flash = Fade(0.1, 0.0, 0.5, color=\"#ffffff\")"]
    late = [f"    config.name = {json.dumps(title, ensure_ascii=False)}  # 標題畫面與視窗標題顯示書名，不是專案資料夾名稱",
            f"    config.window_title = {json.dumps(title, ensure_ascii=False)}",
            '    config.version = ""  # 標題畫面不顯示 "1.0"；打包檔名仍用 build.version',
            '    build.version = "1.0"']
    if exists(title_bg_rel):
        # 一定要用 define：改成 init 1 python 賦值後，實機標題畫面背景圖沒有出現（2026-11-02 實測）。代價是 lint 會警告重複 define，可接受。
        theme.append(f'define gui.main_menu_background = "{title_bg_rel}"')
    if title_music:
        theme.append(f'define config.main_menu_music = "{title_music}"')
    theme += ["",
             'init python:',
             '    renpy.music.register_channel("ambience", mixer="sfx", loop=True)',
             '    build.classify("game/saves/**", None)  # 打包時不帶存檔、驗收測試檔、快取與測試截圖',
             '    build.classify("game/vn_verify_test.rpy*", None)',
             '    build.classify("game/cache/**", None)',
             '    build.classify("tests/**", None)',
             '    build.classify("game/images/placeholders.json", None)  # 開發端素材清單，成品不需要', "",
             "init 1 python:", *late, "",
             "# 標題選單側欄：預設圖有一條亮色直線，改成純色半透明底",
             "style main_menu_frame:",
             '    background Solid("#00000070")', "",
             "# 標題選單文字：預設灰在深色底上看起來像「灰掉」，改成明確的淺色／懸停白色",
             "style navigation_button_text:",
             '    idle_color "#d8d8d8"',
             '    hover_color "#ffffff"', "",
             "screen vn_caption(txt):",
             "    zorder 90",
             "    frame:",
             "        xalign 0.02",
             "        yalign 0.03",
             '        background Solid("#000000a0")',
             "        padding (20, 8)",
             "        text txt size 24 color \"#ffffff\"",
             "    timer 4.0 action Hide(\"vn_caption\")", ""]
    if font_file:
        theme.append("# 字型（Ren'Py 預設字型沒有中文）")
        for style_name in ("default", "gui_text", "say_label", "say_dialogue", "input", "button_text", "label_text"):
            theme += [f"style {style_name}:", f'    font "{font_file}"']
    for name in sorted(slot_names_used):
        found = SLOT_NAME_RE.match(name)
        assert found is not None
        percent, crowd = int(found.group(1)), found.group(2)
        theme += [f"transform {name}:", f"    xalign {percent / 100:.2f}", "    yalign 1.0"]
        if crowd:
            theme.append(f"    zoom {crowd_zoom(int(crowd))}")
        theme.append("")
    out.files["vn_theme.rpy"] = "\n".join(theme).rstrip("\n") + "\n"

    out.files["vn_end.rpy"] = 'label vn_end:\n    scene black\n    with fade\n    centered "{size=+10}完{/size}"\n    return\n'
    out.files["script.rpy"] = f"# 由 t2vn.export_renpy 產生；{escape_text(title)}\nlabel start:\n    jump {chapters[0].chapter_id}\n"

    audio_items = []
    for kind, aid in audio_ids:
        path = audio_file(kind, aid)
        audio_items.append({"asset_id": aid, "kind": kind, "file": path or f"audio/{kind}/{aid}.opus", "present": bool(path)})
    out.manifest = {
        "schema_version": SCHEMA_VERSION, "book_id": book_id, "title": title, "chapters": chapter_ids,
        "backgrounds": [{**bg, "present": exists(bg["file"])} for bg in backgrounds.values()],
        "sprites": sprite_items, "audio": audio_items,
        "font": {"file": font_file, "present": exists(font_file)} if font_file else None,
    }
    return out


# ------------------------------------------------------------------ 檔案系統

@dataclass
class ExportResult:
    game_dir: Path
    files: list[str]
    warnings: list[str]
    manifest: dict[str, Any]
    preflight: list[dict[str, Any]] = field(default_factory=list)
    placeholders: dict[str, list[str]] | None = None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportError(f"cannot read {path}: {exc}") from exc


def _find_scenes(work: Path, number: int) -> Path:
    for folder in ("scenes", "gold"):
        path = work / folder / f"ch{number:03d}.json"
        if path.is_file():
            return path
    raise ExportError(f"chapter {number}: no scenes/ch{number:03d}.json or gold/ch{number:03d}.json under {work}")


def _chapter_title(work: Path, number: int) -> str:
    path = work / "chapters" / f"ch{number:03d}.json"
    if path.is_file():
        return str(_read_json(path).get("title") or "")
    index = work / "chapters" / "index.json"
    if index.is_file():
        for entry in _read_json(index).get("chapters", []):
            if entry.get("chapter_no") == number:
                return str(entry.get("title") or "")
    return ""


def _check_target(config: Any, path: Path) -> None:
    if config.is_write_protected(path):
        raise WriteProtectedError(f"refusing to write inside a read-only directory: {path}")


def _write(config: Any, path: Path, text: str) -> None:
    _check_target(config, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # 固定 LF


def _copy(config: Any, src: Path, dst: Path) -> None:
    _check_target(config, dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def export_book(
    config: Any,
    book_id: str,
    chapter_numbers: list[int],
    project_dir: Path | str | None = None,
    font: Path | str | None = None,
    captions: bool = True,
    clean: bool = False,
    placeholders: bool = False,
) -> ExportResult:
    """匯出到 Ren'Py 專案的 ``game/``。``project_dir`` 沒給就用 ``works/<book>/build/renpy``（獨立模式）。"""
    book = config.book(book_id)
    work = config.work_dir(book_id)
    if project_dir is None:
        project = work / "build" / "renpy"
        (project / "game").mkdir(parents=True, exist_ok=True)
    else:
        project = Path(project_dir)
        if not (project / "game").is_dir():
            raise ExportError(f"{project} has no game/ folder; create the project with the Ren'Py launcher first")
    game = project / "game"
    _check_target(config, game)

    cast_doc = _read_json(work / "cast.json")
    book_doc = _read_json(work / "book.json") if (work / "book.json").is_file() else {}
    title = str((book_doc.get("display_name") or {}).get(book.lang) or next(iter((book_doc.get("display_name") or {"": book_id}).values())))
    style_path = work / "prototype" / "style.json"
    style = _read_json(style_path) if style_path.is_file() else None

    chapters: list[ChapterInput] = []
    for number in sorted(set(chapter_numbers)):
        doc = _read_json(_find_scenes(work, number))
        chapters.append(ChapterInput(f"ch{number:03d}", number, _chapter_title(work, number), doc))
    lang = str(chapters[0].doc.get("lang") or book.lang)

    if clean:
        for stale in game.glob("vn_*.rpy"):
            _check_target(config, stale)
            stale.unlink()

    font_rel: str | None = None
    if font is not None:
        font_path = Path(font)
        if font_path.suffix.lower() not in FONT_EXTS or not font_path.is_file():
            raise ExportError(f"--font must be an existing .ttf or .otf file: {font_path}")
        font_rel = f"fonts/vn_font{font_path.suffix.lower()}"

    assets = work / "assets"

    def source_for(rel: str) -> Path | None:
        # game/images/bg/x.png ← assets/bg/x.png；game/audio/bgm/x.opus ← assets/bgm/x.opus
        parts = rel.split("/")
        candidate = assets.joinpath(*parts[1:])
        return candidate if candidate.is_file() else None

    # 先複製真素材，之後 exists / audio_file 只看 game/
    def need_images() -> list[str]:
        rels: list[str] = []
        pairs: list[tuple[str, str]] = []
        for chapter in chapters:
            for scene in chapter.doc.get("scenes", []):
                rels.append(f"images/bg/{scene['bg']['asset_id']}.png")
                for member in scene.get("cast") or []:
                    pairs.append((member["char_id"], member.get("expr") or "neutral"))
                for line in scene.get("lines", []):
                    if line.get("type") == "dialogue" and line.get("expr") and line.get("speaker") != PLAYER_ID:
                        pairs.append((line["speaker"], line["expr"]))
        sprites_have, heads_have = sprite_fallback.scan_available(assets)
        rels += sprite_fallback.needed_files(sprite_fallback.plan_images(pairs, sprites_have, heads_have))
        rels.append(f"images/bg/{TITLE_BG_ID}.png")  # 標題背景：有就帶，沒有就用 Ren'Py 預設
        return rels

    copied: list[str] = []
    for rel in dict.fromkeys(need_images()):
        src = source_for(rel)
        if src is not None:
            _copy(config, src, game / rel)
            copied.append(rel)
    for chapter in chapters:
        for scene in chapter.doc.get("scenes", []):
            for kind in ("bgm", "ambience"):
                aid = scene.get(kind)
                if not aid:
                    continue
                for ext in AUDIO_EXTS:
                    src = assets / kind / f"{aid}{ext}"
                    if src.is_file():
                        _copy(config, src, game / "audio" / kind / f"{aid}{ext}")
                        copied.append(f"audio/{kind}/{aid}{ext}")
                        break
    for ext in AUDIO_EXTS:  # 標題音樂：有就帶
        src = assets / "bgm" / f"{TITLE_BGM_ID}{ext}"
        if src.is_file():
            _copy(config, src, game / "audio" / "bgm" / f"{TITLE_BGM_ID}{ext}")
            copied.append(f"audio/bgm/{TITLE_BGM_ID}{ext}")
            break
    if font_rel:
        _copy(config, Path(font), game / font_rel)

    def audio_file(kind: str, aid: str) -> str | None:
        for ext in AUDIO_EXTS:
            rel = f"audio/{kind}/{aid}{ext}"
            if (game / rel).is_file():
                return rel
        return None

    rendered = render_export(
        book_id=book_id, title=title, lang=lang, chapters=chapters, cast_doc=cast_doc, style=style,
        exists=lambda rel: (game / rel).is_file(), audio_file=audio_file, font_file=font_rel, captions=captions)

    written: list[str] = []
    for name, text in rendered.files.items():
        _write(config, game / name, text)
        written.append(name)
    _write(config, game / "vn_export.json", json.dumps(rendered.manifest, ensure_ascii=False, indent=2) + "\n")
    written.append("vn_export.json")

    current = {f"vn_{c.chapter_id}.rpy" for c in chapters}
    for stale in sorted(game.glob("vn_ch[0-9][0-9][0-9].rpy")):
        if stale.name not in current:
            rendered.warnings.append(f"{stale.name} is left over from an earlier export (use --clean to remove it)")

    result = ExportResult(game, written + copied, rendered.warnings, rendered.manifest)
    if placeholders:
        from .placeholders import make_placeholders

        for item in rendered.manifest["backgrounds"] + rendered.manifest["sprites"]:
            _check_target(config, game / item["file"])
        result.placeholders = make_placeholders(rendered.manifest, game)
    from .renpy_preflight import check_project

    result.preflight = check_project(project)
    return result


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.export_renpy --book sample_book --chapters 1-5 [--project DIR] [--font FILE] [--no-captions] [--clean] [--placeholders]``"""
    sys.stdout.reconfigure(encoding="utf-8")
    from .annotate import parse_chapter_range
    from .config import load_config

    parser = argparse.ArgumentParser(description="Export scene graphs to a Ren'Py project.")
    parser.add_argument("--book", required=True)
    parser.add_argument("--chapters", required=True, help="chapter number or range, e.g. 2 or 1-5")
    parser.add_argument("--project", default=None, help="Ren'Py project folder created by the launcher (default: works/<book>/build/renpy)")
    parser.add_argument("--font", default=None, help="OFL Traditional-Chinese font (.ttf/.otf), copied to game/fonts/vn_font.*")
    parser.add_argument("--no-captions", action="store_true")
    parser.add_argument("--clean", action="store_true", help="delete earlier game/vn_*.rpy first")
    parser.add_argument("--placeholders", action="store_true", help="draw placeholder images for missing backgrounds and sprites")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        result = export_book(config, args.book, parse_chapter_range(args.chapters), args.project, args.font,
                             captions=not args.no_captions, clean=args.clean, placeholders=args.placeholders)
    except (T2vnError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"warning: {warning}")
    if result.placeholders is not None:
        print(f"placeholders: created {len(result.placeholders['created'])}, skipped {len(result.placeholders['skipped'])}")
    for issue in result.preflight:
        print(f"{issue['level']:7} {issue['code']:16} {issue['file']}:{issue['line']}  {issue['message']}")
    errors = sum(1 for i in result.preflight if i["level"] == "error")
    print(f"ok: {len(result.files)} file(s) -> {result.game_dir}; preflight {errors} error(s), {len(result.preflight) - errors} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
