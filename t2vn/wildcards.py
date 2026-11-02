"""wildcard 文字檔載入與展開（S6）。

使用者的 wildcard 檔案來自網路上各種 wildcard 包：檔名有大寫、空白、連字號，內容有
CRLF、BOM、整句描述、``(標籤:1.3)`` 權重寫法、空行與尾逗號。這個模組把它們讀成
「名稱 → 選項清單」，並把文字中的 ``__名稱__`` 依固定種子展開（同一個種子永遠得到
同一組選項，方便重現）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

TOKEN = re.compile(r"__([a-z0-9_]+?)__")
MAX_DEPTH = 5
TXT_SUFFIX = ".txt"
COMMENT_PREFIX = "#"
NAME_PREFIX = "w_"
NON_NAME_RUN = re.compile(r"[^a-z0-9]+")
UTF8 = "utf-8-sig"


def normalize_name(stem: str) -> str:
    """檔名主檔名 → wildcard 名稱：小寫、非 ``[a-z0-9]`` 換 ``_``、去頭尾 ``_``。

    結果空白 → ``ValueError``；開頭不是字母則前面補 ``w_``（例如 ``4040`` → ``w_4040``）。
    """
    if not isinstance(stem, str):
        raise ValueError(f"檔名必須是字串：{stem!r}")
    name = NON_NAME_RUN.sub("_", stem.lower()).strip("_")
    if not name:
        raise ValueError(f"檔名無法轉成 wildcard 名稱：{stem!r}")
    if not name[0].isalpha():
        name = NAME_PREFIX + name
    return name


def _read_options(path: Path) -> list[str]:
    """讀一個 wildcard 檔的選項：BOM、CRLF、註解、空行、尾逗號、重複都處理掉。"""
    text = path.read_bytes().decode(UTF8, errors="replace")
    seen: set[str] = set()
    options: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(COMMENT_PREFIX):
            continue
        line = line.rstrip(",").strip()
        if not line or line in seen:
            continue
        seen.add(line)
        options.append(line)
    return options


def load_library(directory: str | Path) -> tuple[dict[str, list[str]], list[str]]:
    """讀資料夾**直接底下**的 ``*.txt``（不分大小寫），回傳 ``(library, warnings)``。

    依檔名不分大小寫排序處理；名稱衝突時保留先讀到的、發出警告；沒有選項的檔案發出
    警告並略過。資料夾不存在 → ``FileNotFoundError``。
    """
    folder = Path(directory)
    if not folder.is_dir():
        raise FileNotFoundError(f"找不到 wildcard 資料夾：{folder}")

    library: dict[str, list[str]] = {}
    sources: dict[str, str] = {}
    warnings_list: list[str] = []
    files = sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() == TXT_SUFFIX
        ),
        key=lambda path: path.name.lower(),
    )
    for path in files:
        try:
            name = normalize_name(path.stem)
        except ValueError:
            warnings_list.append(f"檔名無法轉成名稱，略過：{path.name}")
            continue
        options = _read_options(path)
        if not options:
            warnings_list.append(f"沒有選項，略過：{path.name}")
            continue
        if name in library:
            warnings_list.append(
                f"名稱衝突：{path.name} 與 {sources[name]} 都是 {name}，保留 {sources[name]}"
            )
            continue
        library[name] = options
        sources[name] = path.name
    return library, warnings_list


def describe_library(
    library: dict[str, list[str]], sample: int = 3, width: int = 40
) -> list[dict[str, Any]]:
    """給提示詞用的 wildcard 摘要：依名稱排序，列出名稱、選項數與前幾個樣本。"""
    return [
        {
            "name": name,
            "count": len(options),
            "sample": [option[:width] for option in options[:sample]],
        }
        for name, options in sorted(library.items())
    ]


def pick(
    library: dict[str, list[str]], name: str, seed: int | str, salt: str = ""
) -> str:
    """用固定種子挑一個選項：``sha256(f"{seed}:{name}:{salt}")`` 前 8 bytes 取餘數。"""
    options = library[name]
    digest = hashlib.sha256(f"{seed}:{name}:{salt}".encode("utf-8")).digest()
    return options[int.from_bytes(digest[:8], "big") % len(options)]


def expand(
    text: str,
    library: dict[str, list[str]],
    seed: int | str,
    max_depth: int = MAX_DEPTH,
) -> tuple[str, list[str]]:
    """展開文字中的 ``__名稱__``，回傳 ``(結果, warnings)``。

    每個實際展開的 token 依序用 ``salt=str(k)`` 挑選（``k`` 從 0 起、遞迴時繼續累加），
    選到的選項若還有 token 就遞迴展開。名稱不在 library 或深度已達 ``max_depth`` 的
    token 原樣保留並警告（同一訊息只出現一次）。
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else "", []

    warnings_list: list[str] = []
    seen_warnings: set[str] = set()
    counter = [0]

    def note(message: str) -> None:
        if message in seen_warnings:
            return
        seen_warnings.add(message)
        warnings_list.append(message)

    def expand_text(value: str, depth: int) -> str:
        if TOKEN.search(value) is None:
            return value

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if depth >= max_depth:
                note(f"展開太深，保留原樣：{name}")
                return match.group(0)
            if name not in library:
                note(f"未知的 wildcard：{name}")
                return match.group(0)
            index = counter[0]
            counter[0] += 1
            chosen = pick(library, name, seed, salt=str(index))
            return expand_text(chosen, depth + 1)

        return TOKEN.sub(replace, value)

    return expand_text(text, 0), warnings_list
