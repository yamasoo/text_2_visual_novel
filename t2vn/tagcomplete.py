"""繪圖 Prompt 即時標籤補全（類 sd-webui-tagcomplete-neo）。

吃本機 Danbooru 標籤 CSV（danbooru.csv）與中文對照 CSV（danbooru-0-zh.csv 等），
支援英文前綴／子字串、別名、中文反查、畫師 @ 優先。只用標準庫。
"""
from __future__ import annotations

import os
import re
from typing import Any

_TAGS: dict[str, Any] = {"path": None, "mtime": None, "tags": {}, "alias": {}, "sorted": []}
_ZH: dict[str, Any] = {"path": None, "mtime": None, "pairs": [], "en2zh": {}}
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _load_tags(path: str) -> dict[str, Any] | None:
    """讀取 danbooru.csv（格式：name,category,count,"aliases"），依 mtime 快取。"""
    if not path or not os.path.isfile(path):
        return None
    mt = os.path.getmtime(path)
    if _TAGS["path"] == path and _TAGS["mtime"] == mt:
        return _TAGS
    tags: dict[str, tuple[str, int]] = {}
    alias: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split(",", 3)
            if len(parts) < 3 or not parts[0]:
                continue
            name = parts[0].strip().replace("_", " ")
            cat = parts[1].strip()
            try:
                cnt = int(parts[2].strip())
            except ValueError:
                continue
            tags[name] = (cat, cnt)
            if len(parts) == 4 and parts[3]:
                for raw in parts[3].strip().strip('"').split(","):
                    al = raw.strip().replace("_", " ")
                    if al and not al.startswith("/"):
                        alias.setdefault(al, name)
    sorted_tags = sorted(tags.items(), key=lambda x: -x[1][1])
    _TAGS.update(path=path, mtime=mt, tags=tags, alias=alias, sorted=sorted_tags)
    return _TAGS


def _load_zh(path: str) -> dict[str, Any] | None:
    """讀取中文對照 CSV（格式：tag,category,中文1|中文2…），依 mtime 快取。"""
    if not path or not os.path.isfile(path):
        return None
    mt = os.path.getmtime(path)
    if _ZH["path"] == path and _ZH["mtime"] == mt:
        return _ZH
    pairs: list[tuple[str, str]] = []
    en2zh: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split(",", 2)
            if len(parts) < 3:
                continue
            tag = parts[0].replace("_", " ").strip()
            cat = parts[1].strip()
            for zh in parts[2].split("|"):
                zh = zh.strip()
                if not zh or not _CJK_RE.search(zh):
                    continue
                if tag and tag not in en2zh:
                    en2zh[tag] = zh
                if cat in ("0",) and len(zh) >= 2:
                    pairs.append((zh, tag))
    pairs.sort(key=lambda x: -len(x[0]))
    _ZH.update(path=path, mtime=mt, pairs=pairs, en2zh=en2zh)
    return _ZH


def resolve_dict_paths(tag_csv: str = "", tag_zh_csv: str = "", forge_dir: str = "") -> tuple[str, str]:
    """回傳實際存在的 (tag_csv, tag_zh_csv)；未填時從 forge_dir 偵測 tagcomplete／tagcomplete-neo。"""
    tag_path = str(tag_csv or "").strip()
    zh_path = str(tag_zh_csv or "").strip()
    fd = str(forge_dir or "").strip()
    bases: list[str] = []
    if fd:
        for ext in ("sd-webui-tagcomplete-neo", "a1111-sd-webui-tagcomplete"):
            cand = os.path.join(fd, "extensions", ext, "tags")
            if os.path.isdir(cand):
                bases.append(cand)
    if not tag_path:
        for base in bases:
            for name in ("danbooru.csv", "danbooru_e621_merged.csv"):
                cand = os.path.join(base, name)
                if os.path.isfile(cand):
                    tag_path = cand
                    break
            if tag_path:
                break
    if not zh_path:
        for base in bases:
            for name in ("danbooru-0-zh.csv", "zh_cn.csv", "danbooru-zh.csv", "danbooru_zh.csv", "danbooru_zh_tw.csv"):
                cand = os.path.join(base, name)
                if os.path.isfile(cand):
                    zh_path = cand
                    break
            if zh_path:
                break
    return (tag_path if os.path.isfile(tag_path) else ""), (zh_path if os.path.isfile(zh_path) else "")


def suggest_tags(q: str, tag_path: str, zh_path: str = "", limit: int = 15, mode: str = "all") -> dict[str, Any]:
    """即時標籤補全：支援英文前綴／子字串、別名、中文反查、畫師 @ 優先。"""
    q_raw = str(q or "").strip()
    if not q_raw:
        return {"items": [], "has_dict": bool(tag_path or zh_path)}
    limit = max(1, min(40, int(limit or 15)))
    artist_only = (mode == "artist") or q_raw.startswith("@")
    q_clean = q_raw.lstrip("@").strip().replace("_", " ").lower()
    if not q_clean:
        return {"items": [], "has_dict": bool(tag_path or zh_path)}

    db = _load_tags(tag_path) if tag_path else None
    zdb = _load_zh(zh_path) if zh_path else None
    en2zh = zdb["en2zh"] if zdb else {}
    if not (db or zdb):
        return {"items": [], "has_dict": False}

    seen: set[str] = set()
    items: list[dict[str, Any]] = []

    if _CJK_RE.search(q_clean) and zdb:
        prefix_zh, sub_zh = [], []
        for zh, tag in zdb["pairs"]:
            if tag in seen:
                continue
            if zh.startswith(q_clean):
                prefix_zh.append((zh, tag))
            elif q_clean in zh:
                sub_zh.append((zh, tag))
        scored = []
        for zh, tag in (prefix_zh + sub_zh):
            if tag in seen:
                continue
            seen.add(tag)
            cat, cnt = db["tags"].get(tag, ("0", 0)) if db else ("0", 0)
            if artist_only and cat != "1":
                continue
            exact = 1 if zh == q_clean else (0 if zh.startswith(q_clean) else -1)
            scored.append((exact, cnt, tag, cat, zh))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        for _, cnt, tag, cat, zh in scored[:limit]:
            items.append({
                "tag": tag,
                "insert": ("@" + tag) if (artist_only or cat == "1") else tag,
                "cat": cat,
                "count": cnt,
                "zh": zh,
            })
        return {"items": items, "has_dict": True}

    if db:
        prefix_hits, sub_hits = [], []
        for name, (cat, cnt) in db["sorted"]:
            if artist_only and cat != "1":
                continue
            low = name.lower()
            if low.startswith(q_clean):
                prefix_hits.append((name, cat, cnt))
                if len(prefix_hits) >= limit:
                    break
            elif q_clean in low and len(sub_hits) < limit:
                sub_hits.append((name, cat, cnt))
        for name, cat, cnt in (prefix_hits + sub_hits):
            if name in seen:
                continue
            seen.add(name)
            items.append({
                "tag": name,
                "insert": ("@" + name) if (artist_only or (mode == "artist" and cat == "1")) else name,
                "cat": cat,
                "count": cnt,
                "zh": en2zh.get(name, ""),
            })
            if len(items) >= limit:
                break
        if len(items) < limit:
            alias_hits = []
            for al, canon in db["alias"].items():
                if canon in seen:
                    continue
                if q_clean in al.lower():
                    cat, cnt = db["tags"].get(canon, ("0", 0))
                    if artist_only and cat != "1":
                        continue
                    alias_hits.append((al.lower().startswith(q_clean), cnt, al, canon, cat))
            alias_hits.sort(key=lambda x: (-int(x[0]), -x[1]))
            for _, cnt, al, canon, cat in alias_hits:
                if canon in seen:
                    continue
                seen.add(canon)
                items.append({
                    "tag": canon,
                    "insert": ("@" + canon) if (artist_only or (mode == "artist" and cat == "1")) else canon,
                    "cat": cat,
                    "count": cnt,
                    "alias": al,
                    "zh": en2zh.get(canon, ""),
                })
                if len(items) >= limit:
                    break
    return {"items": items, "has_dict": True}
