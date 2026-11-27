"""從 novel-studio v3.1+ 書庫的 ``bible/visual-cards/*.md`` 單向匯入角色視覺卡。

規則：
- 對書庫目錄嚴格唯讀；所有寫入經 ``assert_writable`` 限制在 ``works/<book_id>/``。
- 一次無損填滿 ``art/appearance/<char_id>.json``（``headshot_prompt``、``sprite_prompt``、
  ``expression_prompts``、``t2c.sections``、``t2c.outfits``、``t2c.source_sha1``）與
  ``art/style.json``，並同步補齊 ``cast.json``。
- 預設保守（``overwrite=False``）：只補尚未建立的外型卡，已存在的跳過；
  ``overwrite=True`` 時先備份舊卡到 ``art/appearance/_history/`` 再更新。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

import yaml

from . import appearance_card, art_cards, history, source_reader
from .atomic import write_atomic
from .errors import SourceError, T2vnError
from .schema_check import validate_data

UTF8 = "utf-8"
_FM_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n(.*))?$", re.S)
_NON_ID_RE = re.compile(r"[^a-z0-9_]+")
_VALID_CAST_ROLES = {"protagonist", "main", "supporting", "antagonist", "minor"}


class VisualCardImportError(T2vnError):
    """視覺卡解析或匯入錯誤。"""


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in re.split(r"[\n;]+", value) if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = [str(part).strip() for part in value if str(part).strip()]
    else:
        text = str(value).strip()
        items = [text] if text else []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        low = item.lower()
        if low not in seen:
            seen.add(low)
            out.append(item)
    return out


def _dedupe_join(*groups: list[str]) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            text = re.sub(r"\s+", " ", str(raw or "")).strip()
            if not text:
                continue
            low = text.lower()
            if low not in seen:
                seen.add(low)
                out.append(text)
    return ", ".join(out)


def normalize_char_id(raw_id: Any) -> str:
    """把 novel-studio 的視覺卡 id 正規化成符合 ``cast.schema.json``（``^[a-z][a-z0-9_]{1,62}$``）。"""
    text = _NON_ID_RE.sub("_", str(raw_id or "").strip().lower()).strip("_")
    if not text:
        raise VisualCardImportError(f"visual card id cannot be normalized: {raw_id!r}")
    if not text[0].isalpha():
        text = f"c_{text}"
    if len(text) < 2:
        text = f"{text}_"
    return text[:63]


def parse_visual_card(path: Path) -> dict[str, Any]:
    """解析單張 novel-studio 視覺卡 Markdown（YAML frontmatter + 備註本文）。"""
    try:
        raw = path.read_text(encoding=UTF8)
    except OSError as exc:
        raise VisualCardImportError(f"cannot read visual card {path.name}: {exc}") from exc
    match = _FM_RE.match(raw.lstrip("\ufeff"))
    if match is None:
        raise VisualCardImportError(f"{path.name}: missing YAML frontmatter")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise VisualCardImportError(f"{path.name}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise VisualCardImportError(f"{path.name}: frontmatter must be a mapping")

    raw_id = str(meta.get("id") or path.stem).strip()
    name = str(meta.get("name") or raw_id).strip()
    aliases = _str_list(meta.get("aliases"))
    status = str(meta.get("status") or "draft").strip().lower()
    source = str(meta.get("source") or "").strip().replace("\\", "/")

    raw_sec = meta.get("sections") if isinstance(meta.get("sections"), dict) else {}
    sections = {key: _str_list(raw_sec.get(key)) for key in appearance_card.T2C_SECTIONS}

    raw_outfits = meta.get("outfits") if isinstance(meta.get("outfits"), dict) else {}
    outfits: dict[str, dict[str, list[str]]] = {}
    for oid, oval in raw_outfits.items():
        if not isinstance(oval, dict):
            continue
        okey = str(oid or "").strip().lower()
        if not appearance_card.OUTFIT_ID.match(okey):
            continue
        parts = {part: _str_list(oval.get(part)) for part in appearance_card.T2C_OUTFIT_PARTS}
        parts = {part: items for part, items in parts.items() if items}
        if parts:
            outfits[okey] = parts

    raw_vn = meta.get("vn") if isinstance(meta.get("vn"), dict) else {}
    avatar_accessories = _str_list(raw_vn.get("avatar_accessories"))
    sprite_pose = str(raw_vn.get("sprite_pose") or "").strip()
    raw_expr = raw_vn.get("expressions") if isinstance(raw_vn.get("expressions"), dict) else {}
    expressions = {
        str(k).strip().lower(): str(v).strip()
        for k, v in raw_expr.items()
        if isinstance(k, str) and str(v or "").strip()
    }

    return {
        "raw_id": raw_id,
        "name": name,
        "aliases": aliases,
        "status": status,
        "source": source,
        "sections": sections,
        "outfits": outfits,
        "vn": {
            "avatar_accessories": avatar_accessories,
            "sprite_pose": sprite_pose,
            "expressions": expressions,
        },
        "body": (match.group(2) or "").strip(),
        "file": path.name,
    }


def convert_visual_card(
    parsed: dict[str, Any],
    char_id: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把解析後的 novel-studio 視覺卡轉成 t2vn 外型卡 dict（含 ``t2c`` 與 ``expression_prompts``）。"""
    sections = parsed["sections"]
    outfits = parsed["outfits"]
    vn = parsed["vn"]

    headshot_prompt = _dedupe_join(sections["head"], vn["avatar_accessories"])

    default_fit = outfits.get("default") or (next(iter(outfits.values())) if outfits else {})
    outfit_phrases = _str_list(default_fit.get("set")) + _str_list(default_fit.get("top")) + _str_list(default_fit.get("bottom"))
    pose_list = [vn["sprite_pose"]] if vn["sprite_pose"] else []
    sprite_prompt = _dedupe_join(sections["body"], sections["upper"], sections["lower"], outfit_phrases, pose_list)

    expr_list = list(existing["expressions"]) if isinstance(existing, dict) and existing.get("expressions") else ["neutral"]
    t2c_sections = {key: list(items) for key, items in sections.items() if items}
    t2c_payload: dict[str, Any] = {}
    if t2c_sections:
        t2c_payload["sections"] = t2c_sections
    if outfits:
        t2c_payload["outfits"] = {oid: dict(parts) for oid, parts in outfits.items()}
    if t2c_payload:
        t2c_payload["source_sha1"] = appearance_card.prompt_fingerprint(
            {"headshot_prompt": headshot_prompt, "sprite_prompt": sprite_prompt}
        )

    draft_card: dict[str, Any] = {
        "char_id": char_id,
        "confirmed": False,
        "headshot_prompt": headshot_prompt,
        "sprite_prompt": sprite_prompt,
        "expressions": expr_list,
        "notes": parsed["body"],
    }
    if vn["expressions"]:
        draft_card["expression_prompts"] = dict(vn["expressions"])
    if t2c_payload:
        draft_card["t2c"] = t2c_payload

    normalized = appearance_card.normalize_card(draft_card, char_id)
    if parsed["status"] == "confirmed" and appearance_card.sprite_tags(normalized):
        normalized["confirmed"] = True
    return normalized


def _lookup_source_meta(book_dir: Path, source_rel: str) -> tuple[str, str | None]:
    """嘗試從原角色設定檔讀定位與首次登場章號；讀不到或失敗就安全退回 ``("supporting", None)``。"""
    if not source_rel:
        return "supporting", None
    target = (book_dir / source_rel).resolve()
    try:
        target.relative_to(book_dir.resolve())
    except ValueError:
        return "supporting", None
    if not target.is_file():
        return "supporting", None
    try:
        card = source_reader.read_character(target)
        role = card.role if card.role in _VALID_CAST_ROLES else "supporting"
        first_ch = f"ch{card.first_appearance:03d}" if isinstance(card.first_appearance, int) and 1 <= card.first_appearance <= 999 else None
        return role, first_ch
    except SourceError:
        return "supporting", None


def _match_existing_cast_id(characters: dict[str, Any], char_id: str, name: str, aliases: list[str]) -> str | None:
    if char_id in characters:
        return char_id
    wanted = {x.strip().lower() for x in [name, *aliases] if x and x.strip()}
    if not wanted:
        return None
    for cid, entry in characters.items():
        if not isinstance(entry, dict) or entry.get("role") == "player":
            continue
        names = [v for v in (entry.get("names") or {}).values() if isinstance(v, str)]
        entry_aliases = [v for v in (entry.get("aliases") or []) if isinstance(v, str)]
        pool = {x.strip().lower() for x in [*names, *entry_aliases] if x.strip()}
        if pool & wanted:
            return cid
    return None


def sync_visual_cards(
    book_dir: Path,
    work: Path,
    assert_writable: Callable[[Path], Any],
    *,
    lang: str = "zh-TW",
    overwrite: bool = False,
) -> dict[str, Any]:
    """掃描書庫的 ``bible/visual-cards/*.md`` 並同步到 ``cast.json``、外型卡與 ``art/style.json``。"""
    paths = source_reader.source_paths_for(book_dir)
    vc_dir = book_dir / paths.get("visual_card_dir", "bible/visual-cards")
    char_dir = book_dir / paths.get("character_dir", "bible/characters")
    lib_char_stems = [p.stem for p in sorted(char_dir.glob("*.md"))] if char_dir.is_dir() else []
    report: dict[str, Any] = {
        "found": 0,
        "created": [],
        "updated": [],
        "skipped": [],
        "renamed": {},
        "cast_added": [],
        "cast_updated": [],
        "library_characters": len(lib_char_stems),
        "missing_visual_cards": list(lib_char_stems),
        "errors": [],
    }
    if not vc_dir.is_dir():
        return report

    files = sorted(vc_dir.glob("*.md"))
    report["found"] = len(files)
    if not files:
        return report

    cast_path = work / "cast.json"
    cast_doc: dict[str, Any] | None = None
    cast_dirty = False
    if cast_path.is_file():
        try:
            loaded = json.loads(cast_path.read_text(encoding=UTF8))
            if isinstance(loaded, dict) and isinstance(loaded.get("characters"), dict):
                cast_doc = loaded
        except (OSError, ValueError):
            cast_doc = None

    seen_ids: dict[str, str] = {}
    covered_names: set[str] = set()
    for md_path in files:
        try:
            parsed = parse_visual_card(md_path)
            target_id = normalize_char_id(parsed["raw_id"])
        except VisualCardImportError as exc:
            report["errors"].append(str(exc))
            continue

        covered_names.add(parsed["name"].strip().lower())
        if parsed["source"]:
            covered_names.add(Path(parsed["source"]).stem.strip().lower())

        if cast_doc is not None:
            matched = _match_existing_cast_id(
                cast_doc["characters"], target_id, parsed["name"], parsed["aliases"]
            )
            if matched is not None:
                target_id = matched

        if target_id in seen_ids:
            report["errors"].append(
                f"{md_path.name}: char_id collision on {target_id!r} (already mapped from {seen_ids[target_id]})"
            )
            continue
        seen_ids[target_id] = md_path.name
        if target_id != parsed["raw_id"]:
            report["renamed"][parsed["raw_id"]] = target_id

        # 同步 cast.json
        if cast_doc is not None:
            chars = cast_doc["characters"]
            role, first_ch = _lookup_source_meta(book_dir, parsed["source"])
            if target_id not in chars:
                new_entry: dict[str, Any] = {
                    "names": {lang: parsed["name"] or target_id},
                    "aliases": [a for a in parsed["aliases"] if a != parsed["name"]],
                    "role": role,
                }
                if first_ch:
                    new_entry["first_chapter"] = first_ch
                chars[target_id] = new_entry
                cast_dirty = True
                report["cast_added"].append(target_id)
            else:
                entry = chars[target_id]
                existing_aliases = list(entry.get("aliases") or [])
                existing_names = set((entry.get("names") or {}).values())
                changed_entry = False
                for alias in parsed["aliases"]:
                    if alias not in existing_aliases and alias not in existing_names:
                        existing_aliases.append(alias)
                        changed_entry = True
                if changed_entry:
                    entry["aliases"] = existing_aliases
                if first_ch and not entry.get("first_chapter"):
                    entry["first_chapter"] = first_ch
                    changed_entry = True
                if changed_entry:
                    cast_dirty = True
                    report["cast_updated"].append(target_id)

        # 同步外型卡
        cpath = art_cards.card_path(work, target_id)
        exists = cpath.is_file()
        if exists and not overwrite:
            report["skipped"].append(target_id)
            continue

        existing_card: dict[str, Any] | None = None
        if exists:
            try:
                existing_card = appearance_card.load_card(cpath)
            except (OSError, ValueError):
                existing_card = None
            try:
                history._write_backup(cpath, cpath.read_text(encoding=UTF8), None)
                history.prune_history(cpath, history.DEFAULT_MAX_HISTORY)
            except OSError:
                pass

        converted = convert_visual_card(parsed, target_id, existing=existing_card)
        try:
            art_cards.save_card(assert_writable, work, target_id, converted)
        except art_cards.ArtCardsError as exc:
            report["errors"].append(f"{md_path.name}: {exc}")
            continue
        (report["updated"] if exists else report["created"]).append(target_id)

    if cast_dirty and cast_doc is not None:
        issues = validate_data(cast_doc, "cast")
        if issues:
            report["errors"].append(
                "cast.json validation failed: " + "; ".join(f"{i.path or '/'} {i.message}" for i in issues[:3])
            )
        else:
            assert_writable(cast_path)
            if cast_path.is_file():
                try:
                    history._write_backup(cast_path, cast_path.read_text(encoding=UTF8), None)
                    history.prune_history(cast_path, history.DEFAULT_MAX_HISTORY)
                except OSError:
                    pass
            write_atomic(cast_path, json.dumps(cast_doc, ensure_ascii=False, indent=2) + "\n", encoding=UTF8)

    report["missing_visual_cards"] = [
        stem for stem in lib_char_stems if stem.strip().lower() not in covered_names
    ]
    return report


_NON_PLACE_CATEGORIES = {
    "power-system", "history", "economy", "religion", "culture",
    "technology", "vehicles-weapons", "ecology", "society-politics",
}


def _loc_id_for(name: str, stem: str) -> str:
    import hashlib

    raw = re.sub(r"[^a-z0-9_]+", "_", (stem or name).strip().lower()).strip("_")
    if raw and raw[0].isalpha() and len(raw) >= 2:
        return raw[:48]
    digest = hashlib.sha1((name or stem or "loc").encode("utf-8")).hexdigest()[:6]
    return f"loc_{digest}"


def _extract_one_line(body: str) -> str:
    lines = body.splitlines()
    for idx, raw in enumerate(lines):
        if raw.startswith("## "):
            for nxt in lines[idx + 1 :]:
                s = nxt.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    break
                return s.lstrip("-* ").strip()[:160]
    return ""


def _extract_art_prompt_block(body: str) -> tuple[str, str]:
    """從世界觀檔的繪圖提示段落抽出 (tags, description)，沒有就回空字串。"""
    tags, desc = "", ""
    in_art = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            in_art = ("繪圖提示" in line) or ("art prompt" in line.lower()) or ("illustration" in line.lower())
            continue
        if not in_art or not line:
            continue
        low = line.lower()
        if low.startswith("tags:") or low.startswith("- tags:"):
            tags = line.split(":", 1)[1].strip()
        elif low.startswith("description:") or low.startswith("- description:"):
            desc = line.split(":", 1)[1].strip()
    return tags, desc


def sync_world_locations(
    book_dir: Path,
    work: Path,
    assert_writable: Callable[[Path], Any],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """掃描書庫的 ``bible/world/*.md``（或 v2 世界觀目錄），把地點條目帶入 ``bible.json`` 與 ``art/style.json``。"""
    from . import bible as bible_mod

    paths = source_reader.source_paths_for(book_dir)
    world_dir = book_dir / paths.get("world_dir", "bible/world")
    report: dict[str, Any] = {"found": 0, "created": [], "updated": [], "skipped": [], "errors": []}
    if not world_dir.is_dir():
        return report

    files = sorted(world_dir.glob("*.md"))
    report["found"] = len(files)
    if not files:
        return report

    registry = bible_mod.load_registry(work)
    existing_locs = registry.get("locations") or {}
    label_to_id = {
        str(v.get("label") or "").strip().lower(): k
        for k, v in existing_locs.items()
        if isinstance(v, dict) and str(v.get("label") or "").strip()
    }

    style_path = work / "art" / "style.json"
    style_doc: dict[str, Any] = {}
    if style_path.is_file():
        try:
            loaded = json.loads(style_path.read_text(encoding=UTF8))
            if isinstance(loaded, dict):
                style_doc = loaded
        except (OSError, ValueError):
            style_doc = {}
    style_locs = style_doc.get("locations") if isinstance(style_doc.get("locations"), dict) else {}
    style_dirty = False

    for md_path in files:
        try:
            raw = md_path.read_text(encoding=UTF8)
        except OSError as exc:
            report["errors"].append(f"{md_path.name}: {exc}")
            continue
        meta: dict[str, Any] = {}
        body = raw
        match = _FM_RE.match(raw.lstrip("\ufeff"))
        if match is not None:
            try:
                loaded_fm = yaml.safe_load(match.group(1))
                if isinstance(loaded_fm, dict):
                    meta = loaded_fm
            except yaml.YAMLError:
                meta = {}
            body = match.group(2) or ""
        cat = str(meta.get("category") or "").strip().lower()
        if cat in _NON_PLACE_CATEGORIES:
            report["skipped"].append(md_path.stem)
            continue

        label = str(meta.get("name") or md_path.stem).strip() or md_path.stem
        note = _extract_one_line(body)
        loc_id = label_to_id.get(label.lower()) or _loc_id_for(label, md_path.stem)
        existed = loc_id in existing_locs
        if existed and not overwrite:
            report["skipped"].append(loc_id)
            continue

        assert_writable(work / bible_mod.REGISTRY_FILE)
        bible_mod.set_entry(work, "locations", loc_id, {"label": label, "note": note})
        existing_locs[loc_id] = {"label": label, "note": note}
        label_to_id[label.lower()] = loc_id
        (report["updated"] if existed else report["created"]).append(loc_id)

        tags, desc = _extract_art_prompt_block(body)
        if tags or desc:
            entry = dict(style_locs.get(loc_id) or {})
            if tags:
                entry["tags"] = tags
            if desc:
                entry["description"] = desc
            entry.setdefault("notes", note)
            style_locs[loc_id] = entry
            style_dirty = True

    if style_dirty:
        style_doc["locations"] = style_locs
        assert_writable(style_path)
        write_atomic(style_path, json.dumps(style_doc, ensure_ascii=False, indent=2) + "\n", encoding=UTF8)

    return report
