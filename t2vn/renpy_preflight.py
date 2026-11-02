"""Ren'Py 專案的前置檢查與打包指令（S5）。

開發環境沒有 Ren'Py SDK，所以用純 Python 抓最常見的語法與引用錯誤：label 重複、
jump 目標不存在、字串沒關、台詞用到沒 define 的角色、用到沒宣告的圖、音檔不存在……
語意最終仍以使用者執行 Ren'Py 啟動器的 Lint 為準。

用法::

    python -m t2vn.renpy_preflight --project <專案> [--sdk <SDK> [--package win]]

這個模組不執行 Ren'Py、不啟動外部程式、也不修改任何專案檔。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import TypedDict

SCRIPT_FILE = "script.rpy"
VN_PREFIX = "vn_"
RPY_SUFFIX = ".rpy"
EXPORT_FILE = "vn_export.json"
GAME_DIR_NAME = "game"
REQUIRED_LABELS = ("start", "vn_end")
PACKAGES = ("win", "mac", "linux", "market")
LAUNCHER_NAMES = ("renpy.exe", "renpy.sh")
BLACK_IMAGE = "black"
INDENT_WIDTH = 4
UTF8 = "utf-8"

LABEL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LABEL_LINE_PATTERN = re.compile(r"^label\s+([^\s:]+)\s*:")
JUMP_PATTERN = re.compile(r"^jump\s+(\S+)")
IMAGE_DECL_PATTERN = re.compile(r'^image\s+(.+?)\s*=\s*(?:Transform\()?"')
IMAGE_WORD_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
DEFINE_PATTERN = re.compile(r"^define\s+([A-Za-z_][A-Za-z0-9_]*)\s*=")
CHAR_LINE_PATTERN = re.compile(r'^(c_[A-Za-z0-9_]*)\s+"')
PLAY_PATTERN = re.compile(r"^play\s+(?:music|ambience|sound)\s+")
SCENE_SHOW_PATTERN = re.compile(r"^(scene|show)\s+(.*)$")
DIALOGUE_PATTERN = re.compile(r'^(?:"|[A-Za-z_][A-Za-z0-9_]*\s+"|centered\s+")')
KEYWORD_PREFIXES = frozenset(
    {"image", "define", "play", "scene", "show", "label", "jump", "python", "$"}
)
CLAUSE_WORDS = frozenset({"with", "at", "onlayer", "behind", "zorder"})


class Issue(TypedDict):
    """一個問題。"""

    level: str
    code: str
    file: str
    line: int
    message: str


def _issue(level: str, code: str, file: str, line: int, message: str) -> Issue:
    """組一筆問題。"""
    return {"level": level, "code": code, "file": file, "line": line, "message": message}


# ------------------------------------------------------------------------ 掃描一行


def _scan(line: str) -> tuple[str, list[str], bool]:
    """拆出一行：回傳 (去掉註解後的程式碼, 字串內容清單, 字串是否全部關閉)。

    ``#`` 在字串外才是註解開頭；字串內以 ``\\`` 跳脫下一個字元。
    """
    strings: list[str] = []
    length = len(line)
    index = 0
    start = 0
    in_string = False
    comment_at = length
    while index < length:
        char = line[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                strings.append(line[start + 1 : index])
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            start = index
            index += 1
            continue
        if char == "#":
            comment_at = index
            break
        index += 1
    return line[:comment_at], strings, not in_string


def _is_dialogue(body: str) -> bool:
    """是不是台詞／旁白／centered 行（而不是輸出指令）。"""
    if DIALOGUE_PATTERN.match(body) is None:
        return False
    first = body.split()[0]
    return first not in KEYWORD_PREFIXES


def _has_single_bracket(text: str) -> bool:
    """字串裡是否有單獨的 ``[``（``[[`` 是合法跳脫）。"""
    index = 0
    while index < len(text):
        if text[index] == "[":
            if index + 1 < len(text) and text[index + 1] == "[":
                index += 2
                continue
            return True
        index += 1
    return False


def _scene_target(body: str) -> tuple[str, str] | None:
    """``scene``／``show`` 行 → (指令, 去掉結尾子句後的圖名)；不是就回傳 None。"""
    match = SCENE_SHOW_PATTERN.match(body)
    if match is None:
        return None
    keyword = match.group(1)
    rest = match.group(2).split()
    if keyword == "show" and rest and rest[0] == "screen":
        return None
    kept: list[str] = []
    for word in rest:
        if word in CLAUSE_WORDS:
            break
        kept.append(word)
    return keyword, " ".join(kept)


# ------------------------------------------------------------------------ 主檢查


def _rpy_files(game: Path) -> list[Path]:
    """要檢查的檔案：``script.rpy`` 與 ``vn_*.rpy``，依檔名排序。"""
    files: list[Path] = []
    for path in game.iterdir():
        if not path.is_file() or path.suffix != RPY_SUFFIX:
            continue
        if path.name == SCRIPT_FILE or path.name.startswith(VN_PREFIX):
            files.append(path)
    return sorted(files, key=lambda item: item.name)


def _check_layout(name: str, raw: str, line_no: int) -> list[Issue]:
    """縮排檢查：Tab 或不是 4 的倍數。"""
    indent = raw[: len(raw) - len(raw.lstrip())]
    if "\t" in indent:
        return [_issue("error", "E_TAB", name, line_no, "行首縮排含 Tab")]
    if len(indent) % INDENT_WIDTH != 0:
        return [
            _issue(
                "error",
                "E_INDENT",
                name,
                line_no,
                f"縮排 {len(indent)} 個空格不是 {INDENT_WIDTH} 的倍數",
            )
        ]
    return []


def _check_manifest(game: Path, labels: set[str]) -> list[Issue]:
    """``vn_export.json`` 的檢查：可讀性、章 label、字型檔。"""
    path = game / EXPORT_FILE
    try:
        data = json.loads(path.read_text(encoding=UTF8))
    except (OSError, ValueError) as exc:
        return [
            _issue("error", "E_EXPORT", EXPORT_FILE, 0, f"讀不到或不是合法 JSON：{exc}")
        ]
    if not isinstance(data, dict):
        return [
            _issue("error", "E_EXPORT", EXPORT_FILE, 0, "vn_export.json 最外層必須是物件")
        ]

    issues: list[Issue] = []
    chapters = data.get("chapters")
    if isinstance(chapters, list):
        for chapter in chapters:
            if isinstance(chapter, str) and chapter not in labels:
                issues.append(
                    _issue(
                        "error",
                        "E_CHAPTER_LABEL",
                        EXPORT_FILE,
                        0,
                        f"清單裡的章 label 不存在：{chapter}",
                    )
                )
    font = data.get("font")
    if isinstance(font, dict) and font.get("present") is True:
        font_file = font.get("file")
        if not isinstance(font_file, str) or not (game / font_file).is_file():
            issues.append(
                _issue(
                    "error",
                    "E_FONT",
                    EXPORT_FILE,
                    0,
                    f"font.present 為 true 但檔案不存在：{font_file!r}",
                )
            )
    return issues


def check_project(project_dir: Path | str) -> list[Issue]:
    """檢查 ``<project>/game``，回傳問題清單（依檔名、行號、code 排序）。

    error：缺 ``start``／``vn_end`` label、label 重複或名稱不合法、``jump`` 目標不存在、
    縮排用 Tab 或不是 4 的倍數、字串沒關、單獨的 ``[``、台詞用到沒 ``define`` 的
    ``c_*``、``scene``／``show`` 用到沒宣告的圖、``play`` 的音檔不存在、``image``
    名稱含非 ASCII、``vn_export.json`` 讀不到或壞掉、清單裡的章 label 不存在、清單裡
    font 標 present 但檔案不存在。
    warning：``image`` 指向的圖檔不存在（可用佔位素材補）、台詞含 ``%``。
    """
    game = Path(project_dir) / GAME_DIR_NAME
    if not game.is_dir():
        return [
            _issue("error", "E_NO_GAME", SCRIPT_FILE, 0, f"找不到 game/ 資料夾：{game}")
        ]

    issues: list[Issue] = []
    parsed: list[tuple[str, int, str, list[str]]] = []
    labels: dict[str, tuple[str, int]] = {}
    defined: set[str] = set()
    declared: set[str] = {BLACK_IMAGE}
    tags: set[str] = {BLACK_IMAGE}

    for path in _rpy_files(game):
        name = path.name
        try:
            text = path.read_text(encoding="utf-8-sig")  # Ren'Py 也接受帶 BOM 的檔
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, raw in enumerate(text.splitlines(), start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue

            issues.extend(_check_layout(name, raw, line_no))
            code, strings, closed = _scan(raw)
            if not closed:
                issues.append(
                    _issue("error", "E_STRING", name, line_no, "字串沒有關閉")
                )
                continue
            body = code.strip()
            if not body:
                continue
            parsed.append((name, line_no, body, strings))

            label_match = LABEL_LINE_PATTERN.match(body)
            if label_match is not None:
                label_name = label_match.group(1)
                if LABEL_NAME_PATTERN.match(label_name) is None:
                    issues.append(
                        _issue(
                            "error",
                            "E_LABEL_NAME",
                            name,
                            line_no,
                            f"label 名稱不合法：{label_name}",
                        )
                    )
                if label_name in labels:
                    first_file, first_line = labels[label_name]
                    issues.append(
                        _issue(
                            "error",
                            "E_DUP_LABEL",
                            name,
                            line_no,
                            f"label 重複：{label_name}"
                            f"（第一次在 {first_file}:{first_line}）",
                        )
                    )
                else:
                    labels[label_name] = (name, line_no)

            define_match = DEFINE_PATTERN.match(body)
            if define_match is not None:
                defined.add(define_match.group(1))

            image_match = IMAGE_DECL_PATTERN.match(body)
            if image_match is not None:
                image_name = " ".join(image_match.group(1).split())
                words = image_name.split()
                if not words or any(
                    IMAGE_WORD_PATTERN.match(word) is None for word in words
                ):
                    issues.append(
                        _issue(
                            "error",
                            "E_IMAGE_NAME",
                            name,
                            line_no,
                            f"image 名稱含非 ASCII 或非法字元：{image_name}",
                        )
                    )
                declared.add(image_name)
                if words:
                    tags.add(words[0])
                image_path = strings[0] if strings else ""
                if image_path and not (game / image_path).is_file():
                    issues.append(
                        _issue(
                            "warning",
                            "W_MISSING_IMAGE",
                            name,
                            line_no,
                            f"image 指向的圖檔不存在：{image_path}",
                        )
                    )

    for name, line_no, body, strings in parsed:
        jump_match = JUMP_PATTERN.match(body)
        if jump_match is not None:
            target = jump_match.group(1)
            if target != "expression" and target not in labels:
                issues.append(
                    _issue(
                        "error",
                        "E_JUMP_TARGET",
                        name,
                        line_no,
                        f"jump 目標不存在：{target}",
                    )
                )

        scene_target = _scene_target(body)
        if scene_target is not None:
            keyword, image_ref = scene_target
            if image_ref and image_ref not in declared and image_ref not in tags:
                issues.append(
                    _issue(
                        "error",
                        "E_IMAGE",
                        name,
                        line_no,
                        f"{keyword} 用到沒宣告的圖：{image_ref}",
                    )
                )

        if PLAY_PATTERN.match(body) is not None and strings:
            audio_path = strings[0]
            if not (game / audio_path).is_file():
                issues.append(
                    _issue(
                        "error",
                        "E_MISSING_AUDIO",
                        name,
                        line_no,
                        f"音檔不存在：{audio_path}",
                    )
                )

        if _is_dialogue(body) and strings:
            text = strings[0]
            char_match = CHAR_LINE_PATTERN.match(body)
            if char_match is not None and char_match.group(1) not in defined:
                issues.append(
                    _issue(
                        "error",
                        "E_CHAR",
                        name,
                        line_no,
                        f"台詞用到沒有 define 的角色：{char_match.group(1)}",
                    )
                )
            if _has_single_bracket(text):
                issues.append(
                    _issue(
                        "error",
                        "E_BRACKET",
                        name,
                        line_no,
                        "台詞有單獨的 '['（要顯示 [ 請寫 [[）",
                    )
                )
            if "%" in text:
                issues.append(
                    _issue(
                        "warning",
                        "W_PERCENT",
                        name,
                        line_no,
                        "台詞含 '%'（Ren'Py 會當成插值語法）",
                    )
                )

    for required in REQUIRED_LABELS:
        if required not in labels:
            issues.append(
                _issue(
                    "error",
                    "E_NO_LABEL",
                    SCRIPT_FILE,
                    0,
                    f"全部檔案都沒有 {required} label",
                )
            )

    issues.extend(_check_manifest(game, set(labels)))
    return sorted(issues, key=lambda item: (item["file"], item["line"], item["code"]))


def has_errors(issues: list[Issue]) -> bool:
    """問題清單裡有沒有 error（warning 不算）。"""
    return any(issue["level"] == "error" for issue in issues)


# ------------------------------------------------------------------------ 指令


def _launcher(sdk_dir: Path | str) -> str:
    """SDK 的啟動檔：``renpy.exe`` 優先，其次 ``renpy.sh``。"""
    sdk = Path(sdk_dir)
    for name in LAUNCHER_NAMES:
        candidate = sdk / name
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        f"在 {sdk} 找不到 {' 或 '.join(LAUNCHER_NAMES)}"
    )


def build_lint_command(sdk_dir: Path | str, project_dir: Path | str) -> list[str]:
    """組出 lint 指令：``[<sdk>/renpy.*, <project>, "lint"]``（未在真實 SDK 上驗證）。"""
    return [_launcher(sdk_dir), str(project_dir), "lint"]


def build_distribute_command(
    sdk_dir: Path | str, project_dir: Path | str, package: str = "win"
) -> list[str]:
    """組出打包指令；``package`` 只收 win／mac／linux／market（未在真實 SDK 上驗證）。"""
    if package not in PACKAGES:
        raise ValueError(f"package 必須是 {'／'.join(PACKAGES)} 之一：{package!r}")
    return [
        _launcher(sdk_dir),
        "launcher",
        "distribute",
        str(project_dir),
        "--package",
        package,
    ]


# -------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.renpy_preflight --project <專案> [--sdk <SDK> [--package win]]``。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="renpy_preflight.py",
        description="檢查 Ren'Py 專案（label、jump、圖、音、清單），並印出 lint／打包指令。",
    )
    parser.add_argument("--project", required=True, help="Ren'Py 專案資料夾")
    parser.add_argument("--sdk", default=None, help="Ren'Py SDK 資料夾（給了才印指令）")
    parser.add_argument("--package", default="win", help="打包目標：win／mac／linux／market")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return 0 if code == 0 else 1

    issues = check_project(Path(args.project))
    for issue in issues:
        print(
            f"{issue['file']}:{issue['line']}: "
            f"{issue['level']} {issue['code']}: {issue['message']}"
        )
    errors = sum(1 for issue in issues if issue["level"] == "error")
    warnings = len(issues) - errors
    print(f"{errors} error(s), {warnings} warning(s)")

    if args.sdk:
        try:
            lint_command = build_lint_command(args.sdk, args.project)
            distribute_command = build_distribute_command(
                args.sdk, args.project, args.package
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print("lint: " + " ".join(lint_command))
        print("distribute: " + " ".join(distribute_command))

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
