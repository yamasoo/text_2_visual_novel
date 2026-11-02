"""Ren'Py 劇本可達性檢查（S8）：label 跳轉圖、死路、到不了的場景、結局、引用檔案是否存在。

純文字分析，不執行 Ren'Py。只有 :func:`scan_existing`、:func:`check_game` 會讀資料夾（只讀、不寫）。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

START_LABEL = "start"
END_LABEL = "vn_end"
SPECIAL_LABELS = frozenset(
    {
        "start",
        "splashscreen",
        "after_load",
        "quit",
        "main_menu",
        "after_warp",
        "before_main_menu",
    }
)
LABEL_RE = re.compile(r"^label\s+([a-z0-9_]+)\s*(?:\([^)]*\))?\s*:")
JUMP_RE = re.compile(r"^\s+jump\s+([a-z0-9_]+)\s*(?:#.*)?$")
CALL_RE = re.compile(r"^\s+call\s+([a-z0-9_]+)")
CALL_SCREEN_RE = re.compile(r"^\s+call\s+screen\b")
RETURN_RE = re.compile(r"^\s+return\b")
FILE_REF_RE = re.compile(
    r'"((?:images|audio)/[^"\\\n]+\.(?:png|jpg|jpeg|webp|opus|ogg|mp3|wav|flac))"'
)
SKIP_NAMES = ("vn_verify_test.rpy",)


def _label_body(lines: list[str], start: int, stop: int) -> list[str]:
    """label 的內容行（跳過空行與註解；遇到頂層敘述就結束）。"""
    body: list[str] = []
    for line in lines[start:stop]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            break
        body.append(line)
    return body


def parse_labels(files: dict[str, str]) -> tuple[list[dict[str, object]], list[str]]:
    """解析所有 label。

    ``files``：``{檔名: 檔案文字}``，依檔名排序處理。只認第 0 欄開頭的 ``label name:``。
    回傳 ``(labels, duplicates)``：``labels`` 每項
    ``{"name", "file", "line", "jumps", "calls", "terminal", "fallthrough"}``。
    ``terminal`` 是 label 內最後一個有效敘述的種類：``"jump"``、``"return"`` 或 ``None``；
    ``fallthrough`` 是 ``terminal`` 為 ``None`` 時，同一檔案中下一個 label 的名稱。
    重複的 label 名稱（第二次以後出現）收進 ``duplicates``，且不再放進 ``labels``。
    """
    labels: list[dict[str, object]] = []
    duplicates: list[str] = []
    seen: set[str] = set()

    for name in sorted(files):
        lines = (files[name] or "").splitlines()
        starts = [
            (index, match.group(1))
            for index, line in enumerate(lines)
            if (match := LABEL_RE.match(line)) is not None
        ]
        for position, (index, label_name) in enumerate(starts):
            stop = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
            body = _label_body(lines, index + 1, stop)
            jumps = [match.group(1) for line in body if (match := JUMP_RE.match(line))]
            calls = [
                match.group(1)
                for line in body
                if not CALL_SCREEN_RE.match(line) and (match := CALL_RE.match(line))
            ]
            last = body[-1] if body else ""
            if last and JUMP_RE.match(last):
                terminal: str | None = "jump"
            elif last and RETURN_RE.match(last):
                terminal = "return"
            else:
                terminal = None
            fallthrough = None
            if terminal is None and position + 1 < len(starts):
                fallthrough = starts[position + 1][1]

            if label_name in seen:
                if label_name not in duplicates:
                    duplicates.append(label_name)
                continue
            seen.add(label_name)
            labels.append(
                {
                    "name": label_name,
                    "file": name,
                    "line": index + 1,
                    "jumps": jumps,
                    "calls": calls,
                    "terminal": terminal,
                    "fallthrough": fallthrough,
                }
            )
    return labels, duplicates


def _issue(
    level: str,
    kind: str,
    message: str,
    label: str = "",
    file: str = "",
    line: int = 0,
) -> dict[str, object]:
    """建立一筆問題（給 :func:`analyze` 與 :func:`check_game` 用）。"""
    return {
        "level": level,
        "kind": kind,
        "label": label,
        "file": file,
        "line": line,
        "message": message,
    }


def analyze(
    files: dict[str, str], start: str = START_LABEL, end: str = END_LABEL
) -> dict[str, object]:
    """檢查跳轉圖。回傳 ``{"labels", "reachable", "unreachable", "end_reachable", "issues", "ok"}``。

    邊 = ``jump`` 目標 + ``call`` 目標 + 無終結敘述時的 ``fallthrough``。從 ``start`` 走訪。
    ``issues`` 每項 ``{"level", "kind", "label", "file", "line", "message"}``；``ok`` 為真代表
    沒有任何 ``error``。
    """
    labels, duplicates = parse_labels(files)
    by_name = {item["name"]: item for item in labels}
    issues: list[dict[str, object]] = []

    if start not in by_name:
        issues.append(_issue("error", "start_missing", f"找不到 start label「{start}」"))
    for name in duplicates:
        issues.append(
            _issue("error", "duplicate_label", f"label「{name}」重複定義", name)
        )

    edges: dict[str, list[str]] = {}
    for item in labels:
        targets = list(item["jumps"]) + list(item["calls"])  # type: ignore[arg-type]
        if item["terminal"] is None and item["fallthrough"]:
            targets.append(str(item["fallthrough"]))
        edges[str(item["name"])] = targets

    for item in labels:
        for target in list(item["jumps"]) + list(item["calls"]):  # type: ignore[arg-type]
            if target not in by_name:
                issues.append(
                    _issue(
                        "error",
                        "missing_target",
                        f"跳到不存在的 label「{target}」",
                        str(item["name"]),
                        str(item["file"]),
                        int(item["line"]),  # type: ignore[arg-type]
                    )
                )

    reachable: set[str] = set()
    if start in by_name:
        queue = [start]
        reachable.add(start)
        while queue:
            current = queue.pop(0)
            for target in edges.get(current, []):
                if target in by_name and target not in reachable:
                    reachable.add(target)
                    queue.append(target)

    for item in labels:
        name = str(item["name"])
        if name in reachable and item["terminal"] is None and not item["fallthrough"]:
            issues.append(
                _issue(
                    "error",
                    "dead_end",
                    f"label「{name}」沒有 jump／return 結尾，劇情會斷在這裡",
                    name,
                    str(item["file"]),
                    int(item["line"]),  # type: ignore[arg-type]
                )
            )

    end_reachable = end in reachable
    if start in by_name and not end_reachable:
        issues.append(_issue("error", "end_unreachable", f"從「{start}」走不到結局「{end}」"))

    unreachable = sorted(
        name
        for name in by_name
        if name not in reachable and name not in SPECIAL_LABELS and not name.startswith("_")
    )
    for name in unreachable:
        item = by_name[name]
        issues.append(
            _issue(
                "warning",
                "unreachable",
                f"label「{name}」走不到",
                name,
                str(item["file"]),
                int(item["line"]),  # type: ignore[arg-type]
            )
        )

    return {
        "labels": len(labels),
        "reachable": sorted(reachable),
        "unreachable": unreachable,
        "end_reachable": end_reachable,
        "issues": issues,
        "ok": not any(issue["level"] == "error" for issue in issues),
    }


def referenced_files(files: dict[str, str]) -> list[dict[str, object]]:
    """劇本裡引用的圖片／音檔路徑（``"images/…"``、``"audio/…"`` 字串，副檔名限圖片與音訊）。

    略過整行都是 ``#`` 註解的行。回傳每項 ``{"path", "file", "line"}``，依檔名、行號排序；
    同一路徑可出現多次。
    """
    refs: list[dict[str, object]] = []
    for name in sorted(files):
        for number, line in enumerate((files[name] or "").splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            for match in FILE_REF_RE.finditer(line):
                refs.append({"path": match.group(1), "file": name, "line": number})
    return refs


def missing_files(
    refs: Iterable[dict[str, object]], existing: Iterable[str]
) -> list[dict[str, object]]:
    """``refs`` 中路徑不在 ``existing`` 裡的項目；每個路徑只回報第一次出現。"""
    known = set(existing)
    missing: list[dict[str, object]] = []
    reported: set[str] = set()
    for ref in refs:
        path = str(ref.get("path", ""))
        if not path or path in known or path in reported:
            continue
        reported.add(path)
        missing.append(ref)
    return missing


def scan_existing(game: Path) -> set[str]:
    """``game/images``、``game/audio`` 底下所有檔案的相對路徑（``/`` 分隔）。資料夾不存在給空集合。"""
    root = Path(game)
    found: set[str] = set()
    for sub in ("images", "audio"):
        folder = root / sub
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_file():
                found.add(path.relative_to(root).as_posix())
    return found


def check_game(game: Path) -> dict[str, object]:
    """讀 ``game/*.rpy``（不含 ``vn_verify_test.rpy``）做完整檢查。

    回傳 :func:`analyze` 的結果，再加 ``"missing_files"``；每個缺檔另外加進 ``issues``：
    ``{"level": "error", "kind": "missing_file", ...}``，並讓 ``ok`` 跟著變。
    ``game`` 不是資料夾 → ``ValueError``。
    """
    root = Path(game)
    if not root.is_dir():
        raise ValueError(f"不是資料夾：{game}")
    files: dict[str, str] = {}
    for path in sorted(root.glob("*.rpy")):
        if path.name in SKIP_NAMES:
            continue
        files[path.name] = path.read_text(encoding="utf-8")

    result = analyze(files)
    refs = referenced_files(files)
    missing = missing_files(refs, scan_existing(root))
    issues = list(result["issues"])  # type: ignore[arg-type]
    for ref in missing:
        issues.append(
            _issue(
                "error",
                "missing_file",
                f"找不到檔案：{ref.get('path', '')}",
                "",
                str(ref.get("file", "")),
                int(ref.get("line", 0)),  # type: ignore[arg-type]
            )
        )
    result["issues"] = issues
    result["missing_files"] = missing
    result["ok"] = not any(issue["level"] == "error" for issue in issues)
    return result


def render_markdown(result: dict[str, object]) -> str:
    """把 :func:`check_game` 或 :func:`analyze` 的結果轉成報告用 Markdown。"""
    reachable = result.get("reachable") or []
    lines = [
        "## 劇本可達性",
        f"- label 數：{result.get('labels', 0)}，可達：{len(reachable)}，"
        f"結局：{'可達' if result.get('end_reachable') else '走不到'}",
    ]
    issues = result.get("issues") or []
    for issue in issues:
        level = "錯誤" if issue.get("level") == "error" else "警告"
        file_name = str(issue.get("file", ""))
        where = f"（{file_name}:{issue.get('line', 0)}）" if file_name else ""
        lines.append(f"- [{level}] {issue.get('message', '')}{where}")
    if not issues:
        lines.append("- 沒有發現問題")
    return "\n".join(lines) + "\n"
