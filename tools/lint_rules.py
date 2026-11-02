"""檢查 text_2_visual_novel 專案規則的 lint 工具（任務 t_s1_lint_rules）。

回報這幾類違規：

- ``non_ascii_identifier``：Python 識別字（函式、類別、參數、變數、屬性、import、
  呼叫的關鍵字參數名）含非 ASCII 字元。
- ``non_ascii_key``：Python／JSON／YAML 的 key 含非 ASCII 字元。
- ``hardcoded_path``：Python 字串常數是本機絕對路徑。
- ``missing_encoding``：開文字檔沒指定 ``encoding``。
- ``non_ascii_filename``：檔案路徑含非 ASCII 字元。
- ``syntax_error``：Python 或 JSON 無法解析。

中文出現在字串值、註解、docstring 都不算違規。

用法::

    python tools/lint_rules.py [root] [--allow PATH ...] [--json]

有違規回傳 1，沒有回傳 0。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]

RULE_NON_ASCII_IDENTIFIER = "non_ascii_identifier"
RULE_NON_ASCII_KEY = "non_ascii_key"
RULE_HARDCODED_PATH = "hardcoded_path"
RULE_MISSING_ENCODING = "missing_encoding"
RULE_NON_ASCII_FILENAME = "non_ascii_filename"
RULE_SYNTAX_ERROR = "syntax_error"

# 這個檔案本身就是例外：欄位對照檔的 key 依規格一定是中文。
DEFAULT_ALLOW: tuple[str, ...] = ("config/source_field_map.json",)

# Windows 磁碟路徑、/home/、/Users/、UNC 路徑。
PATH_PATTERN = re.compile(r"^([A-Za-z]:[\\/]|/(home|Users)/|\\\\)")
# YAML 逐行判斷用：key 不含 :、#、引號，後面接冒號加空白或行尾。
YAML_KEY_PATTERN = re.compile(r"""^([^:#'"]+?):(\s|$)""")

OPEN_FUNCTION_NAME = "open"
KEY_METHODS = frozenset({"get", "pop", "setdefault"})
TEXT_FILE_METHODS = frozenset({"read_text", "write_text"})
ENCODING_KEYWORD = "encoding"

EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "vendor",
        "build",
        "out",
        "works",
        "reports",
        "_qa_review_2",
        "library",
        "docs",
        ".claude",
        ".pytest_cache",
    }
)
TESTS_DIR_NAME = "tests"
FIXTURES_DIR_NAME = "fixtures"

PYTHON_SUFFIX = ".py"
JSON_SUFFIX = ".json"
YAML_SUFFIXES = frozenset({".yaml", ".yml"})
UTF8 = "utf-8"


@dataclass(frozen=True)
class Violation:
    """一筆違規。``line`` 從 1 起算，無法定位時為 0。"""

    rule: str
    path: str
    line: int
    message: str


# ----------------------------------------------------------------------- helpers


def _is_non_ascii(text: str) -> bool:
    """字串裡是否有任何字元 ``ord(c) > 127``。"""
    return any(ord(char) > 127 for char in text)


def _normalize_rel(path: str | Path) -> str:
    """把路徑正規化成以 "/" 分隔的相對路徑字串。"""
    return Path(str(path).replace("\\", "/")).as_posix()


def _has_keyword(node: ast.Call, name: str) -> bool:
    """這個呼叫是否帶了指定名稱的關鍵字參數。"""
    return any(keyword.arg == name for keyword in node.keywords)


def _call_mode(node: ast.Call) -> str | None:
    """取 ``open()`` 的模式：第 2 個位置參數或 ``mode=``，取不到回傳 None。"""
    mode: str | None = None
    if len(node.args) >= 2:
        candidate = node.args[1]
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            mode = candidate.value
    for keyword in node.keywords:
        if keyword.arg == "mode":
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                mode = value.value
    return mode


def _is_excluded(rel_path: str) -> bool:
    """依 §5.5 判斷這個相對路徑是否要排除。"""
    parts = rel_path.split("/")
    directories = parts[:-1]
    if any(name in EXCLUDED_DIR_NAMES for name in directories):
        return True
    if not directories or parts[0] != TESTS_DIR_NAME:
        return False
    return FIXTURES_DIR_NAME in directories


def _first_occurrence_line(text: str, key: str) -> int:
    """找 key 在原文中第一次出現的行號：先試不跳脫寫法，再試 \\uXXXX 寫法。"""
    for needle in (json.dumps(key, ensure_ascii=False), json.dumps(key)):
        index = text.find(needle)
        if index >= 0:
            return text.count("\n", 0, index) + 1
    return 0


def _read_text_file(path: Path) -> str | None:
    """用 UTF-8 讀檔；讀不到或不是 UTF-8 時回傳 None（略過內容檢查）。"""
    try:
        return path.read_text(encoding=UTF8)
    except (OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------- python source


def lint_python_source(source: str, rel_path: str) -> list[Violation]:
    """檢查一段 Python 原始碼，回傳違規清單（依 line、rule 排序）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [
            Violation(
                RULE_SYNTAX_ERROR,
                rel_path,
                exc.lineno or 0,
                f"Python 無法解析：{exc.msg}",
            )
        ]

    violations: list[Violation] = []
    seen_names: set[tuple[int, str]] = set()
    seen_keys: set[tuple[int, str]] = set()

    def add_name(name: str | None, line: int) -> None:
        if not name or not _is_non_ascii(name):
            return
        marker = (line, name)
        if marker in seen_names:
            return
        seen_names.add(marker)
        violations.append(
            Violation(
                RULE_NON_ASCII_IDENTIFIER,
                rel_path,
                line,
                f"識別字含非 ASCII 字元：{name}",
            )
        )

    def add_key(key: str, line: int) -> None:
        if not _is_non_ascii(key):
            return
        marker = (line, key)
        if marker in seen_keys:
            return
        seen_keys.add(marker)
        violations.append(
            Violation(
                RULE_NON_ASCII_KEY,
                rel_path,
                line,
                f"key 含非 ASCII 字元：{key!r}",
            )
        )

    # 識別字
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add_name(node.name, node.lineno)
        elif isinstance(node, ast.arg):
            add_name(node.arg, node.lineno)
        elif isinstance(node, ast.Name):
            add_name(node.id, node.lineno)
        elif isinstance(node, ast.Attribute):
            add_name(node.attr, node.lineno)
        elif isinstance(node, ast.alias):
            alias_line = getattr(node, "lineno", 0)
            add_name(node.name, alias_line)
            add_name(node.asname, alias_line)
        elif isinstance(node, ast.keyword):
            add_name(node.arg, getattr(node.value, "lineno", 0))

    # key
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key_node in node.keys:
                if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                    add_key(key_node.value, key_node.lineno)
        elif isinstance(node, ast.Subscript):
            slice_node = node.slice
            if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
                add_key(slice_node.value, node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in KEY_METHODS:
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                add_key(first.value, getattr(first, "lineno", 0))

    # 寫死的本機絕對路徑（docstring 與單獨成行的字串運算式跳過）
    standalone_strings: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            standalone_strings.add(id(node.value))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in standalone_strings:
            continue
        if PATH_PATTERN.match(node.value):
            violations.append(
                Violation(
                    RULE_HARDCODED_PATH,
                    rel_path,
                    node.lineno,
                    f"字串常數是本機絕對路徑：{node.value!r}",
                )
            )

    # 開文字檔沒指定 encoding
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == OPEN_FUNCTION_NAME:
            mode = _call_mode(node)
            if isinstance(mode, str) and "b" in mode:
                continue
            if _has_keyword(node, ENCODING_KEYWORD):
                continue
            if len(node.args) < 4:
                violations.append(
                    Violation(
                        RULE_MISSING_ENCODING,
                        rel_path,
                        node.lineno,
                        "open() 沒有指定 encoding（位置參數也不足 4 個）",
                    )
                )
        elif isinstance(func, ast.Attribute) and func.attr in TEXT_FILE_METHODS:
            if _has_keyword(node, ENCODING_KEYWORD):
                continue
            if func.attr == "read_text" and not node.args:
                violations.append(
                    Violation(
                        RULE_MISSING_ENCODING,
                        rel_path,
                        node.lineno,
                        "read_text() 沒有指定 encoding",
                    )
                )
            elif func.attr == "write_text" and len(node.args) < 2:
                violations.append(
                    Violation(
                        RULE_MISSING_ENCODING,
                        rel_path,
                        node.lineno,
                        "write_text() 沒有指定 encoding",
                    )
                )

    return sorted(violations, key=lambda item: (item.line, item.rule))


# --------------------------------------------------------------------- json text


def lint_json_text(text: str, rel_path: str) -> list[Violation]:
    """檢查 JSON 文字裡所有層級的 object key（值可以是中文）。"""
    found: list[str] = []

    def collect(pairs: list[tuple[Any, Any]]) -> dict[Any, Any]:
        for key, _value in pairs:
            if isinstance(key, str) and _is_non_ascii(key):
                found.append(key)
        return dict(pairs)

    try:
        json.loads(text, object_pairs_hook=collect)
    except json.JSONDecodeError as exc:
        return [
            Violation(
                RULE_SYNTAX_ERROR,
                rel_path,
                exc.lineno,
                f"JSON 無法解析：{exc.msg}",
            )
        ]

    violations: list[Violation] = []
    for key in dict.fromkeys(found):
        violations.append(
            Violation(
                RULE_NON_ASCII_KEY,
                rel_path,
                _first_occurrence_line(text, key),
                f"JSON key 含非 ASCII 字元：{key!r}",
            )
        )
    violations.sort(key=lambda item: item.line)
    return violations


# --------------------------------------------------------------------- yaml text


def lint_yaml_text(text: str, rel_path: str) -> list[Violation]:
    """逐行檢查 YAML 的 key（不用 PyYAML；值是中文不算違規）。"""
    violations: list[Violation] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidate = stripped
        while candidate.startswith("- "):
            candidate = candidate[2:].lstrip()
        match = YAML_KEY_PATTERN.match(candidate)
        if match is None:
            continue
        key = match.group(1).strip()
        if _is_non_ascii(key):
            violations.append(
                Violation(
                    RULE_NON_ASCII_KEY,
                    rel_path,
                    line_no,
                    f"YAML key 含非 ASCII 字元：{key!r}",
                )
            )
    return violations


# ------------------------------------------------------------------------ filenames


def lint_filename(rel_path: str) -> list[Violation]:
    """路徑的任何一段含非 ASCII 字元就回報（``line`` = 0）。"""
    normalized = rel_path.replace("\\", "/")
    if not _is_non_ascii(normalized):
        return []
    return [
        Violation(
            RULE_NON_ASCII_FILENAME,
            normalized,
            0,
            f"路徑含非 ASCII 字元：{normalized}",
        )
    ]


# ------------------------------------------------------------------------- repo


def lint_repo(root: str | Path, allow: Iterable[str] = ()) -> list[Violation]:
    """走訪整個 repo，回傳所有違規（依 path、line、rule 排序）。

    排除目錄見 §5.5；``allow`` 清單裡的檔案只做檔名檢查、不做內容檢查。
    """
    root_path = Path(root)
    allowed = set(DEFAULT_ALLOW)
    allowed.update(_normalize_rel(item) for item in allow)

    violations: list[Violation] = []
    for path in sorted(root_path.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(root_path).as_posix()
        if _is_excluded(rel_path):
            continue
        violations.extend(lint_filename(rel_path))
        if rel_path in allowed:
            continue
        suffix = path.suffix.lower()
        if suffix not in {PYTHON_SUFFIX, JSON_SUFFIX} | YAML_SUFFIXES:
            continue
        text = _read_text_file(path)
        if text is None:
            continue
        if suffix == PYTHON_SUFFIX:
            violations.extend(lint_python_source(text, rel_path))
        elif suffix == JSON_SUFFIX:
            violations.extend(lint_json_text(text, rel_path))
        else:
            violations.extend(lint_yaml_text(text, rel_path))

    return sorted(violations, key=lambda item: (item.path, item.line, item.rule))


# -------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。有違規回傳 1，沒有回傳 0。"""
    try:
        sys.stdout.reconfigure(encoding=UTF8)
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="lint_rules.py",
        description="檢查專案規則：key／識別字一律英文、不寫死路徑、開檔要指定 encoding。",
    )
    parser.add_argument(
        "root", nargs="?", default=None, help="要檢查的目錄，預設是 repo 根目錄"
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="PATH",
        help="只做檔名檢查、不做內容檢查的相對路徑（可重複）",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="只輸出 JSON 陣列"
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 自己的錯誤與 --help 都走這裡
        code = exc.code if isinstance(exc.code, int) else 1
        return 0 if code == 0 else 1

    root = Path(args.root) if args.root is not None else REPO_ROOT
    if not root.is_dir():
        print(f"錯誤：找不到目錄 {root}", file=sys.stderr)
        return 1

    violations = lint_repo(root, allow=args.allow)

    if args.as_json:
        payload = [
            {
                "rule": item.rule,
                "path": item.path,
                "line": item.line,
                "message": item.message,
            }
            for item in violations
        ]
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for item in violations:
            print(f"{item.path}:{item.line}: {item.rule}: {item.message}")
        print(f"共 {len(violations)} 項違規")

    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
