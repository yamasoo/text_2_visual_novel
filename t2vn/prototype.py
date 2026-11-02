"""把 scene graph（scenes/chNNN.json 或 gold/chNNN.json）轉成 ruxi script.json，並建成單檔 HTML 原型。

- 轉換：``to_ruxi_script``（純函式，方便測試）。
- 建置：``build_prototype`` 用 vendor/ruxi 的 ``validate`` 驗證，套用本專案的模板補丁後注入。
- 演出風格（配色、地點名稱、玩家介紹）放在 ``works/<book_id>/prototype/style.json``，不寫死在程式裡。

本專案對 ruxi 的修改都在這個檔案，說明見 ``vendor/ruxi/VENDORED.md``。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from vendor.ruxi import build_html as ruxi_build
from vendor.ruxi import walk_script as ruxi_walk

from .errors import T2vnError

REPO_ROOT = Path(__file__).resolve().parents[1]
RUXI_TEMPLATE = REPO_ROOT / "vendor" / "ruxi" / "template.html"
PLACEHOLDER = "/*__SCRIPT_JSON__*/null"

PD_AUTHOR_OWNED = "author-owned"
PLAYER_ID = "player"
RUXI_PLAYER_ID = "me"
SOURCE_QUOTE_MAX = 60

# 本專案的 expr（8 種）對到 ruxi 的 mood（4 種姿態）。
EXPR_TO_MOOD = {
    "neutral": "calm",
    "serious": "calm",
    "smile": "soft",
    "sad": "soft",
    "embarrassed": "soft",
    "worried": "tense",
    "surprised": "tense",
    "angry": "hard",
}

_FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+TC:wght@400;600&display=swap" rel="stylesheet">'
)

# (原字串, 新字串)。原字串必須在 vendor 模板裡恰好出現一次。
TEMPLATE_PATCHES: list[tuple[str, str]] = [
    ('<html lang="zh">', '<html lang="zh-Hant-TW">'),
    ("<title>入戏</title>", "<title>入戲</title>" + _FONT_LINK),
    (
        '--serif:"Songti SC","STSong","Noto Serif CJK SC","Source Han Serif SC","Source Han Serif CN",Georgia,serif;',
        '--serif:"Noto Serif TC","Source Han Serif TC","Noto Serif CJK TC","Songti TC","PMingLiU",Georgia,serif;',
    ),
    ('id="btn-start">入 戏</button>', 'id="btn-start">開 始</button>'),
    ('id="btn-continue" hidden>继 续</button>', 'id="btn-continue" hidden>繼 續</button>'),
    ('id="btn-reset" hidden>从头再来</button>', 'id="btn-reset" hidden>從頭再來</button>'),
    ('id="btn-again">再入戏一遍</button>', 'id="btn-again">再讀一遍</button>'),
    ("`你将是——${S.meta.player.role}`", "`你將是——${S.meta.player.role}`"),
    ("'原著仍在版权保护期 · 本产物仅限本地自娱，请勿传播'", "'原著仍在版權保護期 · 本產物僅限本地自娛，請勿傳播'"),
    ("'📖 正典结局 · ':'— 偏离结局 · '", "'📖 正典結局 · ':'— 偏離結局 · '"),
    ("h+=`<h2>结 算 · 你读懂了多少</h2>`;", "if(Object.keys(S.verdict).length)h+=`<h2>結 算 · 你讀懂了多少</h2>`;"),
    ("h+=`<h2>你抵达的结局</h2>", "h+=`<h2>你抵達的結局</h2>"),
    ("📖 这就是书里写的", "📖 這就是書裡寫的"),
    ("书里不是这样的——这一条路，是你自己走出来的。", "書裡不是這樣的——這一條路，是你自己走出來的。"),
    ("h+=`<h2>本周目 · 你踩过的原文</h2>`;", "h+=`<h2>本週目 · 你踩過的原文</h2>`;"),
    ("' · 仅限本地自娱'", "' · 僅限本地自娛'"),
]


class PrototypeError(T2vnError):
    """原型轉換或建置失敗。"""


# ---------------------------------------------------------------- 轉換


def _quote(text: str, from_end: bool = False) -> str:
    """取原文短句當出處引文（ruxi 規定 ≤ 60 字）。"""
    if len(text) <= SOURCE_QUOTE_MAX:
        return text
    return "…" + text[-(SOURCE_QUOTE_MAX - 1):] if from_end else text[: SOURCE_QUOTE_MAX - 1] + "…"


def _loc(labels: dict[str, str], chapter_no: int, first: int, last: int) -> str:
    key = "source_loc_one" if first == last and "source_loc_one" in labels else "source_loc"
    return labels[key].format(chapter_no=chapter_no, first=first, last=last)


def _para_range(lines: list[dict[str, Any]]) -> tuple[int, int]:
    paras = [line["source_para"] for line in lines if "source_para" in line]
    if not paras:
        raise PrototypeError("scene has no line with source_para")
    return min(paras), max(paras)


def _convert_line(line: dict[str, Any], scene_id: str) -> dict[str, Any] | None:
    kind = line["type"]
    if kind == "narration":
        return {"who": None, "text": line["text"]}
    if kind == "dialogue":
        who = RUXI_PLAYER_ID if line["speaker"] == PLAYER_ID else line["speaker"]
        out: dict[str, Any] = {"who": who, "text": line["text"]}
        if who != RUXI_PLAYER_ID:
            out["mood"] = EXPR_TO_MOOD[line.get("expr", "neutral")]
        return out
    if kind == "direction":
        return None  # ruxi 沒有演出指令；原型略過
    raise PrototypeError(f"{scene_id}: line type {kind!r} is not supported in the prototype yet")


def _build_cast(scene_doc: dict[str, Any], cast_doc: dict[str, Any], style: dict[str, Any]) -> dict[str, Any]:
    cast: dict[str, Any] = {}
    for char_id, char in cast_doc["characters"].items():
        if char_id == PLAYER_ID:
            continue
        for required in ("hue", "shape"):
            if required not in char:
                raise PrototypeError(f"cast.json: {char_id} needs {required!r} for the prototype")
        cast[char_id] = {"name": char["names"][scene_doc["lang"]], "hue": char["hue"], "shape": char["shape"]}
    player = style["player"]
    cast[RUXI_PLAYER_ID] = {"name": player["name"], "hue": player["hue"], "shape": player["shape"]}
    return cast


def _chapter_scenes(scene_doc: dict[str, Any], chapter_doc: dict[str, Any], style: dict[str, Any]) -> list[dict[str, Any]]:
    """一章的 ruxi scenes；章末場景的 next 保持 None，由呼叫端接下一章或結局。"""
    paragraphs = {p["para_no"]: p["text"] for p in chapter_doc["paragraphs"]}
    chapter_no = chapter_doc["chapter_no"]
    labels = style["labels"]
    scenes = []
    for scene in scene_doc["scenes"]:
        sid = scene["scene_id"]
        first, last = _para_range(scene["lines"])
        location = style["locations"].get(scene["location_id"])
        time = style["times"].get(scene["time_of_day"])
        if location is None or time is None:
            raise PrototypeError(f"{sid}: style.json has no entry for location {scene['location_id']!r} or time {scene['time_of_day']!r}")
        lines = [converted for line in scene["lines"] if (converted := _convert_line(line, sid)) is not None]
        scenes.append({
            "id": sid,
            "title": scene.get("title", {}).get(scene_doc["lang"], sid),
            "bg": {
                "c1": location.get("c1", time["c1"]),
                "c2": location.get("c2", time["c2"]),
                "label": f"{location['label']}・{time['label']}",
            },
            "source": {
                "loc": _loc(labels, chapter_no, first, last),
                "zh": _quote(paragraphs[first]),
            },
            "lines": lines,
            "next": scene.get("next"),
        })
    return scenes


def to_ruxi_script_multi(
    chapters: list[tuple[dict[str, Any], dict[str, Any]]],
    cast_doc: dict[str, Any],
    style: dict[str, Any],
) -> dict[str, Any]:
    """多章 scene graph → 一份連續的 ruxi script.json。chapters 是 [(scene_doc, chapter_doc)]，依章序。

    章末場景（next 為 None）接到下一章第一場；最後一章接正典結局。
    """
    if not chapters:
        raise PrototypeError("no chapters to convert")
    labels = style["labels"]
    first_scene_doc, first_chapter = chapters[0]
    last_scene_doc, last_chapter = chapters[-1]
    cast = _build_cast(first_scene_doc, cast_doc, style)

    scenes: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, (scene_doc, chapter_doc) in enumerate(chapters):
        part = _chapter_scenes(scene_doc, chapter_doc, style)
        for item in part:
            if item["id"] in ids:
                raise PrototypeError(f"duplicate scene id {item['id']!r} across chapters")
            ids.add(item["id"])
        if index + 1 < len(chapters):
            following = chapters[index + 1][0]["scenes"][0]["scene_id"]
            for item in part:
                if item["next"] is None:
                    item["next"] = following
        scenes.extend(part)

    chapter_no = last_chapter["chapter_no"]
    chapter_title = last_chapter["title"]
    ending_id = f"end_{last_scene_doc['chapter_id']}"
    last_para = max(_para_range(s["lines"])[1] for s in last_scene_doc["scenes"])
    for item in scenes:
        if item["next"] is None:
            item["next"] = ending_id
    paragraphs = {p["para_no"]: p["text"] for p in last_chapter["paragraphs"]}
    ending = {
        "id": ending_id,
        "canon": True,
        "title": labels["ending_title"].format(chapter_no=chapter_no, title=chapter_title),
        "lines": [{"who": None, "text": labels["ending_line"].format(chapter_no=chapter_no, title=chapter_title)}],
        "source": {
            "loc": _loc(labels, chapter_no, last_para, last_para),
            "zh": _quote(paragraphs[last_para], from_end=True),
        },
    }

    if len(chapters) == 1:
        book_title = labels["book_line"].format(chapter_no=chapter_no, title=chapter_title)
    else:
        book_title = labels.get("book_line_range", "第 {first_no}–{last_no} 章").format(
            first_no=first_chapter["chapter_no"], last_no=chapter_no)
    player = style["player"]
    return {
        "meta": {
            "title": style["title"],
            "book": {
                "title": book_title,
                "author": style["subtitle"],
                "year": style["version"],
                "lang": first_scene_doc["lang"],
                "pd_status": PD_AUTHOR_OWNED,
            },
            "player": {"name": player["name"], "role": player["role"]},
            "palette": style["palette"],
            "credits": style["credits"],
        },
        "cast": cast,
        "scenes": scenes,
        "endings": [ending],
        "verdict": {},
    }


def to_ruxi_script(
    scene_doc: dict[str, Any],
    cast_doc: dict[str, Any],
    chapter_doc: dict[str, Any],
    style: dict[str, Any],
) -> dict[str, Any]:
    """單章版（含一個正典結局）。"""
    return to_ruxi_script_multi([(scene_doc, chapter_doc)], cast_doc, style)


# ---------------------------------------------------------------- 預設風格（沒有 style.json 的書）

DEFAULT_PALETTE = {"bg": "#11161c", "panel": "#1a222b", "text": "#d7dde3", "dim": "#7c8794", "accent": "#c99a62"}
DEFAULT_TIMES = {
    "dawn": {"label": "清晨", "c1": "#1b2533", "c2": "#4d5a68"},
    "morning": {"label": "上午", "c1": "#26384a", "c2": "#5d7184"},
    "day": {"label": "白天", "c1": "#2c4258", "c2": "#6f8597"},
    "dusk": {"label": "黃昏", "c1": "#2a1f2a", "c2": "#7a5546"},
    "night": {"label": "夜晚", "c1": "#07090f", "c2": "#1a2233"},
}


def _hsl_hex(hue: int, sat: float, light: float) -> str:
    import colorsys

    r, g, b = colorsys.hls_to_rgb(hue / 360, light, sat)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def default_style(book: dict[str, Any], cast_doc: dict[str, Any]) -> dict[str, Any]:
    """新書的預設 style.json：書名、預設配色與時段、玩家（內心獨白）資料。"""
    lang = book.get("lang", "zh-TW")
    title = (book.get("display_name") or {}).get(lang) or book.get("book_id", "")
    protagonist = next((c for c in cast_doc["characters"].values() if c.get("role") == "protagonist"), None)
    return {
        "title": title, "subtitle": "視覺小說原型", "version": "v0.1",
        "credits": f"改編自《{title}》。引擎：入戲 ruxi-skill（MIT）。原型僅供內部試玩。",
        "palette": dict(DEFAULT_PALETTE),
        "player": {"name": "主角（心聲）", "role": (protagonist["names"].get(lang, "主角") if protagonist else "主角"),
                   "hue": (protagonist or {}).get("hue", 28), "shape": (protagonist or {}).get("shape", "small")},
        "labels": {
            "source_loc": "第 {chapter_no} 章 ¶{first}–¶{last}", "source_loc_one": "第 {chapter_no} 章 ¶{first}",
            "book_line": "第 {chapter_no} 章　{title}", "book_line_range": "第 {first_no}–{last_no} 章",
            "ending_title": "第 {chapter_no} 章　{title}・完", "ending_line": "——第 {chapter_no} 章　{title}　完——",
        },
        "locations": {}, "times": json.loads(json.dumps(DEFAULT_TIMES)),
    }


def complete_style(style: dict[str, Any], scene_docs: list[dict[str, Any]]) -> list[str]:
    """補上 style 缺的地點與時段（顏色由 id 決定，名稱先用 id，之後可在 style.json 改成中文）。回傳新增的項目。"""
    import hashlib

    added: list[str] = []
    locations = style.setdefault("locations", {})
    times = style.setdefault("times", {})
    for doc in scene_docs:
        for scene in doc.get("scenes", []):
            location, moment = scene["location_id"], scene["time_of_day"]
            if location not in locations:
                hue = int(hashlib.md5(location.encode("utf-8")).hexdigest()[:4], 16) % 360
                locations[location] = {"label": location.replace("_", " "), "c1": _hsl_hex(hue, 0.25, 0.1), "c2": _hsl_hex(hue, 0.22, 0.28)}
                added.append(f"location:{location}")
            if moment not in times:
                times[moment] = dict(DEFAULT_TIMES.get(moment) or {"label": moment, "c1": "#222222", "c2": "#444444"})
                added.append(f"time:{moment}")
    return added


# ---------------------------------------------------------------- 驗證與建置


def validate_script(script: dict[str, Any]) -> tuple[list[str], list[str]]:
    """用 ruxi 的 validate 驗證（加上 author-owned）。回傳 (錯誤, 警告)。"""
    ruxi_build.PD = set(ruxi_build.PD) | {PD_AUTHOR_OWNED}
    return ruxi_build.validate(script)


def walk(script: dict[str, Any]) -> list[tuple[list, str, dict]]:
    """窮舉所有路徑（ruxi walk_script）。回傳 [(選擇序列, 結局 id, 理解度)]。"""
    paths, _ = ruxi_walk.walk(script)
    return paths


def patched_template(template_path: str | Path = RUXI_TEMPLATE) -> str:
    """讀 vendor 模板並套用 TEMPLATE_PATCHES；任何一條對不上就丟 PrototypeError。"""
    html = Path(template_path).read_text(encoding="utf-8")
    for old, new in TEMPLATE_PATCHES:
        count = html.count(old)
        if count != 1:
            raise PrototypeError(f"template patch expects exactly one match, found {count}: {old[:40]!r}")
        html = html.replace(old, new)
    if PLACEHOLDER not in html:
        raise PrototypeError("template placeholder not found")
    return html


def render_html(script: dict[str, Any], template_path: str | Path = RUXI_TEMPLATE) -> str:
    """把 script 注入補丁後的模板（同 ruxi build() 的注入方式）。"""
    payload = json.dumps(script, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return patched_template(template_path).replace(PLACEHOLDER, payload)


def _write_outputs(script: dict[str, Any], out: Path, stem: str | None) -> dict[str, Any]:
    errors, warnings = validate_script(script)
    if errors:
        raise PrototypeError("ruxi validation failed:\n" + "\n".join(errors))
    paths = walk(script)
    canon = [e["id"] for e in script["endings"] if e.get("canon")]
    if not any(end in canon for _, end, _ in paths):
        raise PrototypeError("canon ending is not reachable")
    out.mkdir(parents=True, exist_ok=True)
    json_name, html_name = (f"{stem}.json", f"{stem}.html") if stem else ("script.json", "index.html")
    (out / json_name).write_text(json.dumps(script, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / html_name).write_text(render_html(script), encoding="utf-8")
    texts = [line["text"] for s in script["scenes"] for line in s["lines"]]
    return {
        "out_dir": str(out),
        "html": html_name,
        "scenes": len(script["scenes"]),
        "lines": len(texts),
        "chars": sum(len(t) for t in texts),
        "paths": len(paths),
        "warnings": warnings,
    }


def build_prototype(
    scenes_path: str | Path,
    work_dir: str | Path,
    out_dir: str | Path | None = None,
    style_path: str | Path | None = None,
) -> dict[str, Any]:
    """單章：讀資料、轉換、驗證、寫出 script.json 與 index.html。回傳摘要。"""
    work = Path(work_dir)
    out = Path(out_dir) if out_dir is not None else work / "prototype"
    style_file = Path(style_path) if style_path is not None else out / "style.json"
    scene_doc = _load(scenes_path)
    chapter_doc = _load(work / "chapters" / f"{scene_doc['chapter_id']}.json")
    script = to_ruxi_script(scene_doc, _load(work / "cast.json"), chapter_doc, _load(style_file))
    return _write_outputs(script, out, None)


def build_book_prototype(
    scenes_paths: list[str | Path],
    work_dir: str | Path,
    out_dir: str | Path | None = None,
    style_path: str | Path | None = None,
    per_chapter: bool = True,
) -> list[dict[str, Any]]:
    """多章：寫 index.html（全部章節連續）＋ 每章 chNNN.html。回傳各檔摘要（第一個是合集）。"""
    work = Path(work_dir)
    out = Path(out_dir) if out_dir is not None else work / "prototype"
    style_file = Path(style_path) if style_path is not None else out / "style.json"
    style = _load(style_file)
    cast_doc = _load(work / "cast.json")
    pairs = []
    for path in scenes_paths:
        scene_doc = _load(path)
        pairs.append((scene_doc, _load(work / "chapters" / f"{scene_doc['chapter_id']}.json")))
    pairs.sort(key=lambda pair: pair[1]["chapter_no"])
    summaries = [_write_outputs(to_ruxi_script_multi(pairs, cast_doc, style), out, None)]
    if per_chapter and len(pairs) > 1:
        for pair in pairs:
            summaries.append(_write_outputs(to_ruxi_script_multi([pair], cast_doc, style), out, pair[0]["chapter_id"]))
    return summaries


def _load(path: str | Path) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise PrototypeError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PrototypeError(f"invalid JSON in {path}: {exc}") from exc


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.prototype SCENES --work-dir works/sample_book [--out DIR] [--style FILE]``。"""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Build a single-file HTML prototype (ruxi engine) from a scene graph.")
    parser.add_argument("scenes", nargs="+", help="scenes/chNNN.json or gold/chNNN.json (several = one combined book + per-chapter files)")
    parser.add_argument("--work-dir", required=True, help="works/<book_id>")
    parser.add_argument("--out", default=None, help="output folder (default: <work-dir>/prototype)")
    parser.add_argument("--style", default=None, help="style.json (default: <out>/style.json)")
    args = parser.parse_args(argv)
    try:
        if len(args.scenes) == 1:
            summaries = [build_prototype(args.scenes[0], args.work_dir, args.out, args.style)]
        else:
            summaries = build_book_prototype(args.scenes, args.work_dir, args.out, args.style)
    except T2vnError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for summary in summaries:
        for warning in summary["warnings"]:
            print(f"warning: {warning}")
        print(f"ok: {summary['html']}: {summary['scenes']} scenes, {summary['lines']} lines, "
              f"{summary['chars']} chars, {summary['paths']} path(s) -> {summary['out_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
