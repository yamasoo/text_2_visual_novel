"""解析 ``renpy lint`` 的輸出（S8）：版本、統計數字、問題清單、是否當掉。

純函式，不碰檔案系統。
"""
from __future__ import annotations

import re

VERSION_RE = re.compile(r"^\W*Ren'Py\s+(\S+)\s+lint report", re.I)
ISSUE_RE = re.compile(r"^(?P<file>[^\s:]+\.rpym?):(?P<line>\d+)\s+(?P<msg>\S.*)$")
ERROR_WORDS = re.compile(
    r"not found|could not|can't|cannot|undefined|not defined|does not exist|unknown|invalid|error|exception|failed|missing",
    re.I,
)
CRASH_RE = re.compile(
    r"Traceback \(most recent call last\)|^\s*(?:\w+\.)*\w*(?:Error|Exception):", re.M
)
FOOTER_RE = re.compile(r"^Lint is not a substitute", re.I)

DIALOGUE_RE = re.compile(
    r"The game contains ([\d,]+) dialogue blocks?, containing ([\d,]+) words and ([\d,]+) characters"
)
MENUS_RE = re.compile(r"([\d,]+) menus?\b")
IMAGES_RE = re.compile(r"([\d,]+) images?\b")
SCREENS_RE = re.compile(r"([\d,]+) screens?\b")
BOM = "\ufeff"


def _int(value: str) -> int:
    """``18,027`` → ``18027``。"""
    return int(value.replace(",", ""))


def classify(message: str) -> str:
    """問題等級：訊息含 ``ERROR_WORDS`` 任一詞（不分大小寫）→ ``"error"``，否則 ``"warning"``。"""
    return "error" if ERROR_WORDS.search(message or "") else "warning"


def parse_statistics(text: str) -> dict[str, int]:
    """從 ``The game contains …`` 那段（可能跨行）取數字。

    可能含 ``dialogue_blocks``、``words``、``characters``、``menus``、``images``、``screens``；
    找不到的不放。數字可含千分位逗號。
    """
    flat = " ".join(str(text or "").split())
    stats: dict[str, int] = {}
    match = DIALOGUE_RE.search(flat)
    if match is not None:
        stats["dialogue_blocks"] = _int(match.group(1))
        stats["words"] = _int(match.group(2))
        stats["characters"] = _int(match.group(3))
    for key, pattern in (("menus", MENUS_RE), ("images", IMAGES_RE), ("screens", SCREENS_RE)):
        found = pattern.search(flat)
        if found is not None:
            stats[key] = _int(found.group(1))
    return stats


def parse_issues(text: str) -> list[dict[str, object]]:
    """取出 ``<檔案>.rpy:<行號> <訊息>`` 形式的問題。

    縮排開頭且緊接在問題之後的行是訊息延續（以一個空白接上）；空行結束延續。
    """
    if not text:
        return []
    issues: list[dict[str, object]] = []
    continuation = False
    for raw in str(text).lstrip(BOM).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continuation = False
            continue
        match = ISSUE_RE.match(line)
        if match is not None:
            issues.append(
                {
                    "file": match.group("file"),
                    "line": int(match.group("line")),
                    "message": match.group("msg").strip(),
                    "level": "warning",
                }
            )
            continuation = True
            continue
        if continuation and line[:1].isspace() and issues:
            issues[-1]["message"] = f"{issues[-1]['message']} {line.strip()}"
            continue
        continuation = False
    for issue in issues:
        issue["level"] = classify(str(issue["message"]))
    return issues


def parse_lint(text: str) -> dict[str, object]:
    """解析整份 lint 輸出，回傳 ``{"version", "statistics", "issues", "crashed", "clean"}``。"""
    empty = {"version": "", "statistics": {}, "issues": [], "crashed": False, "clean": False}
    if not isinstance(text, str) or not text.strip():
        return empty

    body = text.lstrip(BOM)
    match = VERSION_RE.match(body)
    version = match.group(1) if match is not None else ""
    statistics = parse_statistics(body)
    issues = parse_issues(body)
    crashed = CRASH_RE.search(body) is not None
    return {
        "version": version,
        "statistics": statistics,
        "issues": issues,
        "crashed": crashed,
        "clean": bool(statistics) and not issues and not crashed,
    }


def render_markdown(result: dict[str, object]) -> str:
    """報告用 Markdown（第一行 ``## Lint``）。"""
    version = str(result.get("version") or "")
    statistics = result.get("statistics") or {}
    issues = result.get("issues") or []
    crashed = bool(result.get("crashed"))

    lines = ["## Lint", f"- 版本：{version or '未知'}"]
    if statistics:
        lines.append(
            "- 統計：對白 {} 段、{} 字、{} 個選單、{} 張圖、{} 個畫面".format(
                statistics.get("dialogue_blocks", 0),
                statistics.get("characters", 0),
                statistics.get("menus", 0),
                statistics.get("images", 0),
                statistics.get("screens", 0),
            )
        )
    if crashed:
        lines.append("- [錯誤] Lint 執行時當掉，請看 lint.txt")
    for issue in issues:
        level = "錯誤" if issue.get("level") == "error" else "警告"
        lines.append(
            f"- [{level}] {issue.get('message', '')}（{issue.get('file', '')}:{issue.get('line', 0)}）"
        )
    if result.get("clean"):
        lines.append("- 沒有發現問題")
    return "\n".join(lines) + "\n"
