"""字型 subset：只留書裡用到的字，並回報缺字（S5 打包前）。

Ren'Py 遊戲要附一個繁體中文字型，完整 CJK 字型動輒十幾 MB。這個模組從場景檔與角色表
蒐集所有會顯示的字，用 fontTools 把字型縮到只含這些字，並回報字型裡找不到的字（缺字）。

用法::

    python -m t2vn.fontsubset --font <來源字型> --out <輸出字型> \\
        --scenes <ch001.json> [<ch002.json> ...] --cast <cast.json> [--extra <字>]
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterator

from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

FONT_SUFFIXES = (".ttf", ".otf")
UTF8 = "utf-8"
OPTIONS_LAYOUT_FEATURES = ["*"]
OPTIONS_NAME_IDS = ["*"]

# 介面與基本文字一定會用到的字（ASCII 可見字元＋中文標點＋章節數字用字）。
BASE_CHARS = (
    "".join(chr(code) for code in range(0x20, 0x7F))
    + "　，。、；：？！…—─·．～「」『』（）《》〈〉【】〔〕“”‘’"
    + "第章場一二三四五六七八九十百零〇完"
)


def _strings_in(value: Any) -> Iterator[str]:
    """遞迴取 dict／list 裡的所有字串值（「所有語言」）。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings_in(item)


def _is_control(char: str) -> bool:
    """換行、tab 之類的控制字元不收（空白要收）。"""
    return unicodedata.category(char) == "Cc"


def collect_chars(
    scene_docs: list[dict[str, Any]], cast_doc: dict[str, Any], extra: str = ""
) -> str:
    """收集要留的字，回傳「不重複、依字碼排序」的字串。

    來源：每一場的 ``title``（所有語言）、每一行的 ``text``、cast 角色的 ``names``
    （所有語言）與 ``aliases``、``extra``、以及 :data:`BASE_CHARS`。控制字元不收，
    空白（U+0020、U+3000）要收。
    """
    chars: set[str] = set(BASE_CHARS)
    if isinstance(extra, str):
        chars.update(extra)

    for doc in scene_docs or []:
        if not isinstance(doc, dict):
            continue
        scenes = doc.get("scenes")
        if not isinstance(scenes, list):
            continue
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            for text in _strings_in(scene.get("title")):
                chars.update(text)
            lines = scene.get("lines")
            if not isinstance(lines, list):
                continue
            for line in lines:
                if not isinstance(line, dict):
                    continue
                text = line.get("text")
                if isinstance(text, str):
                    chars.update(text)

    if isinstance(cast_doc, dict):
        characters = cast_doc.get("characters")
        if isinstance(characters, dict):
            for entry in characters.values():
                if not isinstance(entry, dict):
                    continue
                for text in _strings_in(entry.get("names")):
                    chars.update(text)
                for text in _strings_in(entry.get("aliases")):
                    chars.update(text)

    return "".join(sorted(char for char in chars if not _is_control(char)))


def _validate_paths(src: Path, dst: Path) -> None:
    """檢查來源與輸出路徑（不存在、副檔名、是不是同一個檔）。"""
    if not src.is_file():
        raise FileNotFoundError(f"找不到字型：{src}")
    if src.suffix.lower() not in FONT_SUFFIXES:
        raise ValueError(
            f"來源字型必須是 {'／'.join(FONT_SUFFIXES)}：{src.name}"
        )
    if dst.suffix.lower() != src.suffix.lower():
        raise ValueError(
            f"輸出副檔名必須與來源相同（{src.suffix}）：{dst.name}"
        )
    try:
        if src.resolve() == dst.resolve():
            raise ValueError(f"輸出不可覆蓋來源字型：{dst}")
    except OSError as exc:
        raise ValueError(f"無法解析路徑：{exc}") from exc


def subset_font(src: Path, dst: Path, chars: str) -> dict[str, Any]:
    """把 ``src`` 字型縮成只含 ``chars`` 的字，寫到 ``dst``，回傳統計。

    回傳 ``{"chars_requested", "chars_missing", "glyphs_after", "bytes_before",
    "bytes_after"}``；``chars_missing`` 是要求但來源字型沒有的字（不含空白），
    依字碼排序。``src`` 不存在 → ``FileNotFoundError``；``chars`` 空、``dst`` 與
    ``src`` 相同、副檔名不是 .ttf／.otf 或兩者不同、來源不是單一字型檔或讀不了 →
    ``ValueError``。
    """
    src_path = Path(src)
    dst_path = Path(dst)
    _validate_paths(src_path, dst_path)
    if not chars:
        raise ValueError("chars 不可為空")

    try:
        font = TTFont(str(src_path))
    except Exception as exc:  # fontTools 對壞檔會丟各種例外
        raise ValueError(f"讀不了字型（不是單一字型檔？）：{src_path}") from exc

    try:
        cmap = font.getBestCmap() or {}
        requested = sorted(set(chars))
        missing = "".join(
            char for char in requested if ord(char) not in cmap and not char.isspace()
        )
        bytes_before = src_path.stat().st_size

        options = Options()
        options.layout_features = OPTIONS_LAYOUT_FEATURES
        options.name_IDs = OPTIONS_NAME_IDS
        options.notdef_outline = True
        options.hinting = True
        subsetter = Subsetter(options=options)
        subsetter.populate(text=chars)
        subsetter.subset(font)
        glyphs_after = len(font.getGlyphOrder())

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        font.save(str(dst_path))
    finally:
        font.close()

    return {
        "chars_requested": len(set(chars)),
        "chars_missing": missing,
        "glyphs_after": glyphs_after,
        "bytes_before": bytes_before,
        "bytes_after": dst_path.stat().st_size,
    }


def _load_scene_docs(paths: list[str]) -> list[dict[str, Any]]:
    """讀 --scenes 的檔案：每個檔可以是單一章 doc，也可以是 doc 的清單。"""
    docs: list[dict[str, Any]] = []
    for raw in paths:
        data = json.loads(Path(raw).read_text(encoding=UTF8))
        if isinstance(data, list):
            docs.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            docs.append(data)
    return docs


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.fontsubset --font ... --out ... --scenes ... --cast ...``。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="fontsubset.py",
        description="把字型縮成只含書裡用到的字，並列出缺字。",
    )
    parser.add_argument("--font", required=True, help="來源字型（.ttf／.otf）")
    parser.add_argument("--out", required=True, help="輸出字型（副檔名要與來源相同）")
    parser.add_argument(
        "--scenes", required=True, nargs="+", help="場景檔（一個或多個）"
    )
    parser.add_argument("--cast", required=True, help="角色表 cast.json")
    parser.add_argument("--extra", default="", help="額外要留的字")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return 0 if code == 0 else 1

    try:
        scene_docs = _load_scene_docs(args.scenes)
        cast_doc = json.loads(Path(args.cast).read_text(encoding=UTF8))
        if not isinstance(cast_doc, dict):
            cast_doc = {}
        chars = collect_chars(scene_docs, cast_doc, extra=args.extra)
        report = subset_font(Path(args.font), Path(args.out), chars)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["chars_missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
