"""視覺聖經（S6）：從小說抽角色與地點 → look_en 草稿 → 人工確認 → 定裝圖 → 服裝矩陣。

    builder = Builder(project, prov_for=lambda stage: llm.find_provider(cfg, stage=stage), should_stop=...)
    builder.scan(load_sources(["E:/novel/正文"]))     # ① 每章一次：誰出場、原文寫了什麼外觀（附引文，程式逐字比對）
    builder.draft(min_chapters=2)                     # ② 每個角色、地點一次：look_en／outfits 草稿 → bible/drafts/
    accept(project, ["chen_jiahe"])                   # 人看過、改過之後搬進 bible/characters/（先過規則檢查）
    jobs, manifest = sheet_jobs(project)              # ③ 定裝圖：每角色每套服裝一張全身、每地點一張空鏡
    render_sheets(project, jobs, manifest, cfg)       #    → renders/bible/；contact_sheet() 拼成比較表
    pick(project, "ref_c_lin_default_full_s123.png")  #    人挑一張，記進 bible/picks.json
    status(project)                                   # ④ 服裝矩陣：誰有哪幾套、哪套有定裝圖、各章用了哪套

檔案（格式見 docs/02_資料格式.md 第 3、14 節）：
    bible/drafts/characters|locations/<id>.md   草稿（人要看過；不會被 Project 讀進來）
    bible/characters|locations/<id>.md          定案
    bible/picks.json                            定裝圖的選擇（人的決定，進版控）
    build/bible/scan/chNNN.json、entities.json  掃描結果與合併後的證據（不進版控）
    renders/bible/*.png                         定裝圖候選（不進版控）

核心不依賴 FastAPI／fekit：停止靠 should_stop（收到 llm.Cancelled 由呼叫端處理），進度靠 on_event。模型呼叫可替換（測試用）。
"""
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from . import cast

import yaml

from . import bible_check as bc, look
from . import bible_prompts as bp
from . import comfy, layout, llm
from .compile import compile_chapter
from .project import FRONT, ID_RE, ProjectError, read_front_matter
from .prompts import repair_message
from .script import Issue, errors

SOURCE_EXT = {".md", ".txt"}
NUM_RE = re.compile(r"第\s*0*(\d+)\s*[章話话回]|ch0*(\d+)", re.I)
SKIP_DIRS = {"草稿", "drafts", "draft", "_範本", ".studio", "插圖"}
KINDS = {"character": "characters", "location": "locations"}


class BibleError(Exception):
    """給人看的錯誤訊息（回修多次仍不過、缺前置檔案、找不到角色…）。"""


# ---------------------------------------------------------------------------
# 讀原著與作者設定
# ---------------------------------------------------------------------------
def _clean_text(raw):
    text = raw.replace("\r\n", "\n").lstrip("﻿")
    match = FRONT.match(text)
    return (text[match.end():] if match else text).strip()


def _natural_key(path):
    m = NUM_RE.search(path.stem)
    return (int(m.group(1) or m.group(2)) if m else 10 ** 9, path.name)


def load_sources(inputs, project=None):
    """原著章節。inputs：資料夾或檔案（.md／.txt）的清單；空的就讀專案的 source/chNNN。
    回傳 [{"n": 章號, "label": 檔名, "path": 路徑, "text": 內文}]，依章號排序。章號從檔名的「第 N 章」或 chN 取，沒有就依序編號。"""
    files = []
    for item in inputs or []:
        path = Path(item)
        if path.is_dir():
            files += sorted((p for p in path.iterdir() if p.is_file() and p.suffix.lower() in SOURCE_EXT and not p.name.startswith(("_", "."))), key=_natural_key)
        elif path.is_file():
            files.append(path)
        else:
            raise BibleError("找不到原著：%s" % path)
    if not files and project is not None:
        src = project.root / "source"
        files = sorted((p for p in src.glob("*") if p.suffix.lower() in SOURCE_EXT), key=_natural_key) if src.is_dir() else []
    if not files:
        raise BibleError("沒有原著可以讀：請用 --from 指定小說章節的資料夾或檔案，或把原著放在專案的 source/")
    out, used = [], set()
    for index, path in enumerate(files, 1):
        m = NUM_RE.search(path.stem)
        n = int(m.group(1) or m.group(2)) if m else index
        if n in used:
            raise BibleError("章號 %d 重複了（%s）：檔名請寫成「第 N 章」或 chN，或分開掃描" % (n, path.name))
        used.add(n)
        text = _clean_text(path.read_text(encoding="utf-8"))
        if text:
            out.append({"n": n, "label": path.stem, "path": str(path), "text": text})
    return sorted(out, key=lambda s: s["n"])


def load_notes(inputs, per_file=4000):
    """作者的設定檔（角色、世界觀…）。資料夾會遞迴讀 .md／.txt，略過「草稿」等資料夾。
    回傳 [{"label": 相對路徑, "stem": 檔名, "text": 內文}]。「（正文未明確）」這類沒資訊的行會被去掉。"""
    out = []
    for item in inputs or []:
        path = Path(item)
        if path.is_dir():
            files = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SOURCE_EXT
                           and not p.name.startswith(("_", ".")) and not (set(p.relative_to(path).parts[:-1]) & SKIP_DIRS))
            base = path
        elif path.is_file():
            files, base = [path], path.parent
        else:
            raise BibleError("找不到設定檔：%s" % path)
        for p in files:
            text = _clean_text(p.read_text(encoding="utf-8"))
            text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
            text = "\n".join(l for l in text.split("\n") if "正文未明確" not in l and l.strip() != "-").strip()
            if text:
                out.append({"label": p.relative_to(base).as_posix(), "stem": p.stem, "text": text[:per_file]})
    return out


def notes_for(entity, notes):
    """和這個角色／地點有關的設定檔：檔名等於名字或別名，或標題行含有名字。回傳 [(標籤, 文字)]。"""
    names = {entity["name"], *entity.get("aliases", [])}
    out = []
    for note in notes or []:
        title = next((l for l in note["text"].split("\n") if l.startswith("#")), "")
        if note["stem"] in names or any(n and n in title for n in names):
            out.append((note["label"], note["text"]))
    return out


# ---------------------------------------------------------------------------
# 合併各章的掃描結果
# ---------------------------------------------------------------------------
def _names(entry):
    return {entry["name"], *entry.get("aliases", [])}


def merge_scans(scans, project=None):
    """scans：[(章號, 該章的乾淨掃描結果)] → {"characters": [...], "locations": [...], "mentioned_only": [...]}。
    同一個人（名字或別名有交集）合併；外觀線索與服裝線索依英文去重，保留最早出現的章節與引文。"""
    result = {"characters": [], "locations": []}
    mentioned = Counter()
    for plural in result:
        groups = []                                  # 每個 group：{"seen": Counter(名字), "items": [(章號, entry)]}
        for n, clean in sorted(scans, key=lambda s: s[0]):
            for entry in clean.get(plural, []):
                hit = next((g for g in groups if set(g["seen"]) & _names(entry)), None)
                if hit is None:
                    hit = {"seen": Counter(), "items": []}
                    groups.append(hit)
                hit["seen"].update([entry["name"]])
                for alias in entry.get("aliases", []):
                    hit["seen"].setdefault(alias, 0)
                hit["items"].append((n, entry))
        for g in groups:
            name = max(g["seen"], key=lambda k: (g["seen"][k], -list(g["seen"]).index(k)))
            entity = {"kind": plural[:-1], "name": name, "aliases": sorted(set(g["seen"]) - {name}),
                      "chapters": sorted({n for n, _ in g["items"]}), "roles": [], "looks": [], "outfits": []}
            seen_look, seen_outfit = set(), set()
            for n, entry in g["items"]:
                if entry.get("role"):
                    entity["roles"].append({"chapter": n, "role": entry["role"]})
                for x in entry.get("looks", []):
                    if x["en"].lower() not in seen_look:
                        seen_look.add(x["en"].lower())
                        entity["looks"].append({"chapter": n, **x})
                for x in entry.get("outfits", []):
                    key = (x["label"], x["en"].lower())
                    if key not in seen_outfit:
                        seen_outfit.add(key)
                        entity["outfits"].append({"chapter": n, **x})
            if plural == "locations":
                entity.pop("outfits")
            result[plural].append(entity)
        result[plural].sort(key=lambda e: (-len(e["chapters"]), e["chapters"][0]))
    for _, clean in scans:
        mentioned.update(clean.get("mentioned_only", []))
    known_names = {n for e in result["characters"] for n in _names(e)}
    result["mentioned_only"] = sorted(n for n in mentioned if n not in known_names)
    if project is not None:
        for plural in ("characters", "locations"):
            for entity in result[plural]:
                entity["known"] = find_card(project, entity["name"], entity["aliases"], KINDS_REV[plural])
    return result


KINDS_REV = {v: k for k, v in KINDS.items()}


def find_card(project, name, aliases=(), kind="character"):
    """名字、別名或 id 對得上的定案卡片 id（沒有就是 None）。
    比對只有一份（D12）：這裡只是 cast.resolve() 的薄包裝，多個寫法取第一個對得上的。"""
    for raw in [name, *aliases]:
        cid, _how = cast.resolve(project, raw, kind)
        if cid is not None:
            return cid
    return None


# ---------------------------------------------------------------------------
# 卡片檔：寫出、列出、確認
# ---------------------------------------------------------------------------
def cards_dir(project, kind, drafts=False):
    base = project.root / "bible"
    return (base / "drafts" if drafts else base) / KINDS[kind]


def _q(text):
    return json.dumps(str(text), ensure_ascii=False)      # JSON 的雙引號字串同時是合法的 YAML


def _fold(text, indent, width=96):
    lines, cur = [], ""
    for word in str(text).split():
        if cur and len(cur) + 1 + len(word) > width - indent:
            lines.append(cur)
            cur = word
        else:
            cur = (cur + " " + word) if cur else word
    if cur:
        lines.append(cur)
    return "\n".join(" " * indent + l for l in lines)


# ---------------------------------------------------------------------------
# 舊卡片（只有 look_en）→ 分段草稿：文字規則先拆，人再看過
# ---------------------------------------------------------------------------
BODY_WORDS = ("build", "frame", "physique", "height", "tall", "short stature", "lean", "slim", "slender",
              "athletic", "muscular", "stocky", "sturdy", "compact", "petite", "heavy-set", "lanky", "wiry")
UPPER_WORDS = ("shoulder", "shoulders", "clavicle", "chest", "bust", "arms", "arm")
LOWER_WORDS = ("waist", "hips", "hip", "legs", "leg", "thighs", "thigh", "knees")
ACCESSORY_WORDS = ("glasses", "spectacles", "earring", "earrings", "necklace", "pendant", "hairpin", "hair clip",
                   "watch", "bracelet", "ring", "piercing", "choker", "bandana")
HEAD_WORDS = ("hair", "haired", "eyes", "eye", "face", "facial", "jaw", "chin", "cheek", "cheeks", "brow", "brows",
               "eyebrow", "eyebrows", "nose", "lips", "mouth", "teeth", "beard", "mustache", "moustache", "skin",
               "complexion", "scar", "mole", "freckles", "bald", "shaved", "ponytail", "bun", "braid", "bangs",
               "fringe", "crew cut", "mohawk", "man", "woman", "boy", "girl", "male", "female")
TOP_WORDS = bc.CLOTHING_WORDS[:bc.CLOTHING_WORDS.index("skirt")] if "skirt" in bc.CLOTHING_WORDS else bc.CLOTHING_WORDS
BOTTOM_WORDS = ("skirt", "trousers", "pants", "jeans", "shorts", "boots", "shoes", "sneakers", "socks",
                "leggings", "sandals", "trainers")


def _clauses(text):
    """把一句外觀拆成短語：切逗號與連接詞（and／with／over），去掉開頭冠詞與句尾句號。
    「A 34-year-old woman with short black hair」→「A 34-year-old woman」「short black hair」。"""
    out = []
    for chunk in re.split(r"[,;]|\s+(?:and|with|over|plus)\s+", str(text or "")):
        chunk = " ".join(chunk.split()).strip(" .")
        chunk = re.sub(r"^(?:a|an|the)\s+", "", chunk, flags=re.I)
        if chunk:
            out.append(chunk)
    return out


def _part_of(clause):
    """這一個短語屬於哪一段（用字判斷；判斷不到就放 head，外觀文字大多是臉部特徵）。"""
    words = set(re.findall(r"[a-z-]+", clause.lower()))
    if words & set(ACCESSORY_WORDS):
        return "accessory"
    if words & set(BODY_WORDS):
        return "body"
    if words & set(UPPER_WORDS):
        return "upper"
    if words & set(LOWER_WORDS):
        return "lower"
    return "head"


def split_look(card):
    """舊卡片（只有 look_en／整句服裝）→ 分段草稿 {"sections", "outfits", "notes"}。
    這是**文字規則**拆的，不是模型：拆完請人看過再按「儲存分段」。分不出來的短語一律放 head。"""
    card = card or {}
    sections = {name: [] for name in look.SECTIONS}
    for clause in _clauses(look.look_en(card)):
        sections[_part_of(clause)].append(clause)
    outfits = {}
    notes = card.get("outfit_notes") or {}
    for oid in (card.get("outfits") or {}):
        fit = look.outfit(card, oid)
        entry = {"top": [], "bottom": [], "set": [], "notes": str(notes.get(oid) or fit.get("notes") or "").strip()}
        if fit["sentence"]:
            sentence = re.sub(r"^(he|she|they)\s+(wears?|is wearing|are wearing)\s+", "", fit["sentence"], flags=re.I)
            for clause in _clauses(sentence):
                words = set(re.findall(r"[a-z-]+", clause.lower()))
                entry["bottom" if words & set(BOTTOM_WORDS) else "top"].append(clause)
        else:
            for name in look.OUTFIT_PARTS:
                entry[name] = list(fit["parts"].get(name) or ())
            entry["set"] = list(fit["parts"].get("set") or ())
        outfits[oid] = entry
    return {"sections": {k: v for k, v in sections.items() if v}, "outfits": outfits,
            "note": "這是用文字規則拆的草稿（不是模型），請看過、改過再按「儲存分段」；分不出來的短語放在 head。"}


def apply_sections(project, kind, cid, sections=None, outfits=None, draft=False, write=True):
    """分段編輯器存檔：把 sections／outfits 寫回卡片的 frontmatter，本文照舊。
    回 {"path", "text", "issues", "stale"}；stale＝定案卡片的外觀改了，挑好的定裝圖要重畫重挑。"""
    path = cards_dir(project, kind, drafts=draft) / ("%s.md" % cid)
    if not path.is_file():
        raise BibleError("找不到卡片：%s" % path)
    meta, body = read_front_matter(path)
    before = card_signature(meta, "")
    if sections is not None:
        if not isinstance(sections, dict):
            raise BibleError("sections 要是 {段名: [英文短語…]}")
        unknown = sorted(k for k in sections if k not in look.SECTIONS)
        if unknown:
            raise BibleError("不認得的段名：%s（可用：%s）" % ("、".join(unknown), "、".join(look.SECTIONS)))
        meta["sections"] = {name: list(look._phrases(sections.get(name))) for name in look.SECTIONS
                            if look._phrases(sections.get(name))}
        meta.pop("look_en", None)                       # 有 sections 就不必再留舊的整段（look.look_en 會接起來）
    if outfits is not None:
        if not isinstance(outfits, dict) or not outfits:
            raise BibleError("outfits 要是 {服裝 id: {top: […], bottom: […], set: […], notes: 中文}}")
        meta["outfits"] = {str(oid): {name: list(look._phrases((value or {}).get(name))) for name in look.OUTFIT_PARTS
                                      if look._phrases((value or {}).get(name))}
                           | ({"notes": str((value or {}).get("notes")).strip()} if (value or {}).get("notes") else {})
                           for oid, value in outfits.items()}
        meta.pop("outfit_notes", None)
    meta.pop("_path", None)
    issues = bc.check_card(meta, kind)
    text = render_card(meta, body, status=str(meta.get("status") or ("draft" if draft else "confirmed")))
    picked = load_picks(project).get("characters" if kind == "character" else "locations", {}).get(cid)
    stale = bool((not draft) and picked and before != card_signature(meta, ""))
    if write:
        path.write_text(text, encoding="utf-8")
    return {"path": str(path), "text": text, "issues": issues, "stale": stale}


def add_alias(project, cid, alias, kind="character"):
    """把一個寫法記進卡片的別名（D11：一鍵對應時人按了才記，不自動）。回 (路徑, 別名清單)。"""
    alias = str(alias or "").strip()
    if not alias:
        raise BibleError("別名不能是空的")
    path = cards_dir(project, kind) / ("%s.md" % cid)
    if not path.is_file():
        raise BibleError("找不到卡片：%s" % path)
    meta, body = read_front_matter(path)
    aliases = [str(a) for a in meta.get("aliases") or []]
    if alias in aliases or alias == str(cid) or alias == str(meta.get("name") or ""):
        return path, aliases
    aliases.append(alias)
    meta["aliases"] = aliases
    meta.pop("_path", None)
    path.write_text(render_card(meta, body, status=str(meta.get("status") or "confirmed")), encoding="utf-8")
    return path, aliases


def _list_block(items, indent):
    return ["%s- %s" % (" " * indent, _q(item)) for item in items]


def render_card(card, body="", status="draft"):
    """卡片 → Markdown（YAML frontmatter＋人看的本文）。
    有 sections 就寫分段（步驟 2），沒有就寫整段 look_en（舊寫法）；服裝同樣支援兩種格式。"""
    out = ["---", "id: %s" % card["id"], "name: %s" % _q(card["name"]), "aliases: [%s]" % ", ".join(_q(a) for a in card.get("aliases") or []),
           "status: %s" % status]
    parts = look.sections(card)
    if parts:
        out.append("sections:")
        for name in look.SECTIONS:
            items = parts.get(name) or ()
            out.append("  %s:%s" % (name, "" if items else " []"))
            out += _list_block(items, 4)
    else:
        out += ["look_en: >-", _fold(card.get("look_en") or "", 2)]
    outfits = card.get("outfits") or {}
    if outfits:
        out.append("outfits:")
        for oid in outfits:
            fit = look.outfit(card, oid)
            if fit["sentence"]:
                out += ["  %s: >-" % oid, _fold(fit["sentence"], 4)]
                continue
            out.append("  %s:" % oid)
            for name in look.OUTFIT_PARTS:
                items = fit["parts"].get(name) or ()
                if not items and name != "top":
                    continue
                out.append("    %s:%s" % (name, "" if items else " []"))
                out += _list_block(items, 6)
            if fit["notes"]:
                out.append("    notes: %s" % _q(fit["notes"]))
    legacy_notes = {oid: t for oid, t in (card.get("outfit_notes") or {}).items()
                    if oid not in outfits or look.outfit(card, oid)["legacy"]}
    if legacy_notes:
        out.append("outfit_notes:")
        out += ["  %s: %s" % (oid, _q(t)) for oid, t in legacy_notes.items()]
    out += ["---", "", body.strip(), ""]
    return "\n".join(out)


def draft_body(card, entity):
    """草稿的本文：摘要、待確認清單、原文依據。這些是給人看的，程式不讀。"""
    lines = [card.get("summary") or "", ""]
    if card.get("invented") or card.get("conflicts"):
        lines.append("## 待確認（請人工看過再確認入庫）")
        lines += ["- **補上的（原文沒寫）**：%s" % x for x in card.get("invented") or []]
        lines += ["- **衝突（設定與原文不同）**：%s" % x for x in card.get("conflicts") or []]
        lines.append("")
    chapters = entity.get("chapters") or []
    if chapters:
        lines += ["## 登場章節", "第 %s 章（共 %d 章）" % ("、".join(str(c) for c in chapters), len(chapters)), ""]
    evidence = [("外觀", x) for x in entity.get("looks") or []] + [("服裝", x) for x in entity.get("outfits") or []]
    if evidence:
        lines.append("## 原文依據")
        for tag, x in evidence:
            lines.append("- 第 %s 章｜%s｜「%s」→ %s" % (x["chapter"], tag + (("：" + x["label"]) if x.get("label") else ""), x["quote"], x["en"]))
        lines.append("")
    lines += ["- `look_en` 是生圖用的固定外觀，**確認後不要再改**（會影響之後每一格）。", "- 表情、姿勢寫在分鏡裡，不要寫進 `look_en`。"]
    return "\n".join(lines)


def read_cards(folder):
    """資料夾裡的卡片 → [(路徑, frontmatter dict 或 None, [Issue])]。檔案壞掉不丟錯，回報成 issue，檢查才看得到全部問題。"""
    out = []
    for path in sorted(Path(folder).glob("*.md")) if Path(folder).is_dir() else []:
        try:
            meta, _ = read_front_matter(path)
            out.append((path, meta, []))
        except ProjectError as error:
            out.append((path, None, [Issue("error", path.name, str(error))]))
    return out


def list_drafts(project, kind="character"):
    return {str(m.get("id")): m for _, m, _ in read_cards(cards_dir(project, kind, drafts=True)) if m}


def lint(project, include_drafts=True):
    """檢查全部卡片（定案與草稿）。回傳 [(類別, 檔案, [Issue])]；沒問題的也列出，方便顯示統計。"""
    rows = []
    for kind in KINDS:
        for drafts in (False, True) if include_drafts else (False,):
            for path, meta, issues in read_cards(cards_dir(project, kind, drafts)):
                if meta is not None:
                    issues = bc.check_card(meta, kind, "%s %s" % ("角色" if kind == "character" else "地點", meta.get("id") or path.stem))
                    if meta.get("id") and meta["id"] != path.stem:
                        issues.append(Issue("warn", path.name, "檔名建議和 id 相同（id 是「%s」）" % meta["id"]))
                rows.append((kind + ("（草稿）" if drafts else ""), path, issues))
    return rows


def accept(project, names, kind=None, force=False):
    """草稿 → 定案：先過規則檢查（有 error 就擋），再搬進 bible/characters|locations/ 並把 status 改成 confirmed。
    names：id 或名字的清單；["*"] 表示全部草稿。已有同 id 的定案卡片時不覆蓋（look_en 定案後不改），要覆蓋請加 force。回傳 [(類別, id, 路徑)]。"""
    moved, matched = [], set()
    for k in ([kind] if kind else list(KINDS)):
        drafts = {str(m.get("id")): (p, m) for p, m, _ in read_cards(cards_dir(project, k, drafts=True)) if m}
        wanted = list(drafts) if "*" in names else []
        for name in names:
            hit = next((cid for cid, (_, m) in drafts.items() if name in (cid, str(m.get("name")))), None)
            if hit:
                wanted.append(hit)
                matched.add(name)
        for cid in dict.fromkeys(wanted):
            path, meta = drafts[cid]
            errs = errors(bc.check_card(meta, k))
            if errs:
                raise BibleError("「%s」還有 %d 個問題，不能確認入庫：\n%s" % (cid, len(errs), "\n".join("  - %s" % e for e in errs)))
            target = cards_dir(project, k) / path.name
            if target.exists() and not force:
                raise BibleError("%s 已經存在。look_en 定案後不要改；真的要換請加 --force（會覆蓋，之後每一格的外觀都會變）" % target)
            text = re.sub(r"^status:\s*draft\s*$", "status: confirmed", path.read_text(encoding="utf-8"), count=1, flags=re.M)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            path.unlink()
            moved.append((k, cid, target))
    missing = [n for n in names if n != "*" and n not in matched]
    if missing:
        have = [cid for k in KINDS for cid in list_drafts(project, k)]
        raise BibleError("bible/drafts 裡找不到：%s（草稿有：%s）" % ("、".join(missing), "、".join(have) or "無"))
    return moved


# ---------------------------------------------------------------------------
# 抽取流程（LLM）
# ---------------------------------------------------------------------------
class Builder:
    """掃描 → 成卡。每個呼叫：產生 → 驗證 → 沒過就把全部錯誤回給模型修（最多 max_fix 次）→ 還是沒過丟 BibleError。
    （和 adapt.Adapter 同一套做法；那邊的 _generate 綁定章節與 build/adapt，所以這裡獨立一份，不動 S5。）"""

    def __init__(self, project, prov=None, prov_for=None, should_stop=None, on_event=None, on_delta=None,
                 max_fix=3, chat_json=None):
        if prov is None and prov_for is None:
            raise BibleError("需要指定 provider（prov 或 prov_for）")
        self.project = project
        self._prov_for = prov_for or (lambda stage_key: prov)
        self.should_stop, self.on_event, self.on_delta = should_stop, on_event, on_delta
        self.max_fix = int(max_fix)
        self._chat_json = chat_json or llm.chat_json
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.warnings = []

    # --- 小工具 ------------------------------------------------------------
    def emit(self, kind, **info):
        if self.on_event:
            self.on_event(dict(info, kind=kind))

    def _dump(self, name, text):
        try:
            (self.project.out_dir("build", "bible", "raw") / name).write_text(text, encoding="utf-8")
        except OSError:
            pass

    def _generate(self, stage, label, messages, validate):
        tag = re.sub(r"[^\w一-鿿-]+", "_", "%s_%s" % (stage, label))
        msgs, last = list(messages), []
        for attempt in range(self.max_fix + 1):
            if self.should_stop and self.should_stop():
                raise llm.Cancelled()
            self.emit("call", stage=stage, label=label, attempt=attempt)
            prov = self._prov_for("bible")
            llm.check_context(prov, msgs)
            data, reply = self._chat_json(prov, msgs, stage="bible", should_stop=self.should_stop, on_delta=self.on_delta,
                                          on_retry=lambda n, e, w: self.emit("retry", stage=stage, attempt=n, error=str(e), wait=w))
            self.usage["calls"] += 1
            for key in ("prompt_tokens", "completion_tokens"):
                self.usage[key] += int((reply.usage or {}).get(key) or 0)
            self._dump(tag + ".txt", reply.text)
            value, issues = validate(data)
            errs = errors(issues)
            self.warnings += ["%s：%s" % (label, i) for i in issues if i.level == "warn"]
            if not errs:
                return value
            last = errs
            self.emit("fix", stage=stage, label=label, attempt=attempt, errors=[str(e) for e in errs])
            msgs = list(messages) + [{"role": "assistant", "content": reply.text}, repair_message(errs, True)]
        raise BibleError("%s（%s）：模型修了 %d 次仍有 %d 個問題：\n%s\n最後一次的輸出存在 %s"
                         % (label, stage, self.max_fix, len(last), "\n".join("  - %s" % e for e in last),
                            self.project.root / "build" / "bible" / "raw" / (tag + ".txt")))

    # --- ① 掃描 ------------------------------------------------------------
    def scan_path(self, n):
        return self.project.root / "build" / "bible" / "scan" / ("ch%03d.json" % int(n))

    def scan_prompt(self, source, known_names=()):
        return bp.scan_messages(source["label"], source["text"], known_names)

    def scan(self, sources, force=False, only=None, inputs=None, notes_inputs=None):
        """每章一次，結果存在 build/bible/scan/chNNN.json（原文沒變就沿用，可以中斷後續跑）。
        掃完把各章合併成 build/bible/entities.json 並回傳。"""
        results, known = [], []
        for source in sources:
            if only and source["n"] not in only:
                cached = self._load_scan(source)
                if cached:
                    results.append((source["n"], cached))
                    known += [n for e in cached["characters"] for n in _names(e)]
                continue
            cached = None if force else self._load_scan(source)
            if cached is not None:
                self.emit("cached", stage="scan", label=source["label"])
            else:
                def validate(data, source=source):
                    return bc.normalize_scan(data, source["text"], source["label"])
                cached = self._generate("scan", source["label"], self.scan_prompt(source, list(dict.fromkeys(known))[:60]), validate)
                path = self.scan_path(source["n"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"version": 1, "chapter": source["n"], "label": source["label"],
                                            "sha1": hashlib.sha1(source["text"].encode("utf-8")).hexdigest(), "result": cached},
                                           ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self.emit("scanned", stage="scan", label=source["label"], characters=len(cached["characters"]), locations=len(cached["locations"]))
            results.append((source["n"], cached))
            known += [n for e in cached["characters"] for n in _names(e)]
        entities = merge_scans(results, self.project)
        entities.update({"version": 1, "inputs": [str(i) for i in inputs or []], "notes": [str(i) for i in notes_inputs or []]})
        out = self.project.out_dir("build", "bible") / "entities.json"
        out.write_text(json.dumps(entities, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.emit("saved", stage="scan", path=str(out))
        return entities

    def _load_scan(self, source):
        path = self.scan_path(source["n"])
        if not path.is_file():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if doc.get("sha1") != hashlib.sha1(source["text"].encode("utf-8")).hexdigest():
            return None
        return doc.get("result")

    # --- ② 成卡 ------------------------------------------------------------
    def load_entities(self):
        path = self.project.root / "build" / "bible" / "entities.json"
        if not path.is_file():
            raise BibleError("找不到 %s：請先執行 scan（掃描原著）" % path)
        return json.loads(path.read_text(encoding="utf-8"))

    def select(self, entities, only=None, min_chapters=2):
        """要成卡的角色與地點。only：名字或別名（強制，不受 min_chapters 與已有卡片限制）。
        回傳 (選中的 [(kind, entity)], 因只出現一章而略過的名字, 已有定案卡片而略過的名字)。"""
        pool = [("character", e) for e in entities["characters"]] + [("location", e) for e in entities["locations"]]
        if only:
            chosen = []
            for name in only:
                hit = next(((k, e) for k, e in pool if name in _names(e)), None)
                if hit is None:
                    raise BibleError("掃描結果裡沒有「%s」（有：%s）" % (name, "、".join(e["name"] for _, e in pool)))
                chosen.append(hit)
            return chosen, [], []
        chosen, rare, known = [], [], []
        for kind, e in pool:
            if e.get("known"):
                known.append(e["name"])
            elif len(e["chapters"]) < min_chapters:
                rare.append(e["name"])
            else:
                chosen.append((kind, e))
        return chosen, rare, known

    def card_prompt(self, kind, entity, notes=None):
        taken = set(self.project.characters if kind == "character" else self.project.locations) | set(list_drafts(self.project, kind))
        return bp.card_messages(kind, entity, bp.notes_block(notes_for(entity, notes)), taken)

    def draft(self, only=None, min_chapters=2, notes=None, force=False):
        """每個角色、地點一次呼叫。回傳 {"drafted": [(類別, id, 路徑)], "rare": [...], "known": [...], "has_draft": [...], "mentioned_only": [...]}。"""
        entities = self.load_entities()
        chosen, rare, known = self.select(entities, only, min_chapters)
        report = {"drafted": [], "rare": rare, "known": known, "has_draft": [], "mentioned_only": entities.get("mentioned_only", [])}
        for kind, entity in chosen:
            old = next((cid for cid, m in list_drafts(self.project, kind).items()
                        if {str(m.get("name")), *(str(a) for a in m.get("aliases") or [])} & _names(entity)), None)
            if old and not force:
                report["has_draft"].append(entity["name"])
                continue
            label = "%s「%s」" % ("角色" if kind == "character" else "地點", entity["name"])
            taken = (set(self.project.characters if kind == "character" else self.project.locations)
                     | (set(list_drafts(self.project, kind)) - {old}))

            def validate(data, kind=kind, label=label, taken=taken):
                card, issues = bc.normalize_card(data, kind)
                if card is None:
                    return None, issues
                issues += bc.check_card(card, kind, label)
                if card["id"] in taken:
                    issues.append(Issue("error", label, "id「%s」已經被用掉了（%s）；換一個不重複的" % (card["id"], "、".join(sorted(taken)))))
                return card, issues

            messages = bp.card_messages(kind, entity, bp.notes_block(notes_for(entity, notes)), taken)
            card = self._generate("card", entity["name"], messages, validate)
            card["name"] = entity["name"]                                           # 名字以原文為準，不讓模型改
            card["aliases"] = [a for a in dict.fromkeys(entity.get("aliases", []) + card["aliases"]) if a != card["name"]]
            if old and old != card["id"]:
                (cards_dir(self.project, kind, drafts=True) / ("%s.md" % old)).unlink(missing_ok=True)
            path = cards_dir(self.project, kind, drafts=True) / ("%s.md" % card["id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_card(card, draft_body(card, entity)), encoding="utf-8")
            report["drafted"].append((kind, card["id"], path))
            self.emit("drafted", stage="card", label=label, id=card["id"], path=str(path))
        return report


# ---------------------------------------------------------------------------
# 定裝圖
# ---------------------------------------------------------------------------
VIEWS = {
    "full": {"shot": "full", "box": [0.2, 0.03, 0.8, 0.98],
             "scene": "Character design reference sheet: a single person stands upright and faces the viewer against a plain white background, full body visible from head to toe, no props and no scenery.",
             "pose": "Standing straight with the arms relaxed at the sides, facing the viewer"},
    "bust": {"shot": "close", "box": [0.1, 0.04, 0.9, 1.0],
             "scene": "Character design reference portrait: a single person faces the viewer against a plain white background, head and shoulders in frame, no props and no scenery.",
             "pose": "Facing the viewer with the shoulders square"},
}


def _sheet_pages(width_mm, height_mm):
    """一格鋪滿的合成模板（沒有頁邊與白邊），讓 compile_chapter 算出和漫畫格同樣的生圖尺寸。"""
    return {"page": {"width_mm": width_mm, "height_mm": height_mm, "width_px": 1200, "margin": [0, 0, 0, 0], "gutter_px": 0, "frame_px": 0},
            "templates": {"sheet": {"name": "定裝圖", "panels": [[0, 0, 1, 1]]}}}


def _match(card, wanted):
    return not wanted or bool({str(card.get("id")), str(card.get("name"))} & set(wanted))


def sheet_jobs(project, ids=None, outfits=None, views=("full",), kinds=("character", "location"), candidates=None, megapixels=None):
    """定裝圖的生圖工作。回傳 (jobs, manifest)：jobs 同 compile_chapter（可直接送 comfy），
    manifest[job id] = {"kind", "id", "outfit", "view"}（挑選與矩陣靠它知道哪張圖是誰）。
    角色：每個角色每套服裝每個 view 一格（3:4 直式）；地點：每個地點一格空鏡（4:3 橫式）。"""
    bad = [v for v in views if v not in VIEWS]
    if bad:
        raise BibleError("不認得的視角「%s」（可用：%s）" % ("、".join(bad), "、".join(VIEWS)))
    overrides = {k: v for k, v in (("candidates", candidates), ("megapixels", megapixels)) if v}
    jobs, manifest = [], {}
    if "character" in kinds:
        pages = []
        for cid, card in project.characters.items():
            if not _match(card, ids):
                continue
            for oid in (card.get("outfits") or {"default": ""}):
                if outfits and oid not in outfits:
                    continue
                for view in views:
                    spec = VIEWS[view]
                    jid = "ref_c_%s_%s_%s" % (cid, oid, view)
                    manifest[jid] = {"kind": "character", "id": cid, "outfit": oid, "view": view}
                    pages.append({"page": len(pages) + 1, "template": "sheet", "panels": [{
                        "slot": 1, "id": jid, "shot": spec["shot"], "angle": "eye", "scene": spec["scene"],
                        "characters": [{"id": cid, "outfit": oid, "pose": spec["pose"], "expression": "Neutral", "box": spec["box"]}]}]})
        if pages:
            jobs += compile_chapter(project, {"pages": pages}, _sheet_pages(3, 4), overrides=overrides)
    if "location" in kinds:
        pages = []
        for lid, card in project.locations.items():
            if not _match(card, ids):
                continue
            jid = "ref_l_%s" % lid
            manifest[jid] = {"kind": "location", "id": lid, "outfit": "", "view": "wide"}
            pages.append({"page": len(pages) + 1, "template": "sheet", "panels": [{
                "slot": 1, "id": jid, "shot": "wide", "angle": "eye", "location": lid, "characters": [],
                "scene": "Establishing reference view of the empty location, shown clearly as a whole; no people are present."}]})
        if pages:
            jobs += compile_chapter(project, {"pages": pages}, _sheet_pages(4, 3), overrides=overrides)
    if ids and not jobs:
        raise BibleError("找不到角色或地點「%s」（定案的卡片在 bible/characters、bible/locations；草稿要先 accept）" % "、".join(ids))
    return jobs, manifest


def manifest_path(project):
    return project.root / "build" / "bible" / "sheets.json"


def save_sheet_jobs(project, jobs, manifest):
    """把定裝圖的工作寫成 build/bible/<job id>.prompt.json（和漫畫格一樣，方便人看送出去的提示詞），並更新 manifest。"""
    from .compile import save_job
    folder = project.out_dir("build", "bible")
    for job in jobs:
        save_job(folder, job)
    merged = load_manifest(project)
    merged.update(manifest)
    manifest_path(project).write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_manifest(project):
    try:
        return json.loads(manifest_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def renders_dir(project):
    return project.root / "renders" / "bible"


def resolve_preset(cfg, name=None, profile=None):
    """出圖預設：(名稱, 內容, 預設名稱)。預設表來自出圖 profile（N4）；和 tools/panels.py 同一套規則。"""
    try:
        prof = profile if profile is not None else comfy.resolve_profile(cfg)
        return comfy.resolve_preset(prof, name)
    except comfy.ComfyError as error:
        raise BibleError(str(error))


def render_sheets(project, jobs, manifest, cfg, preset_name=None, force=False, should_stop=None, on_event=None, only_seeds=None,
                  profile_name=None):
    """送 ComfyUI 出圖，檔名 renders/bible/<job id>_s<seed>[_<預設>][__<profile>].png。已存在的跳過。回傳寫出的檔名清單。"""
    try:
        profile = comfy.resolve_profile(cfg, name=profile_name, project_config=project.config)
        workflow = comfy.load_workflow(profile["workflow"])
        default_profile = comfy.default_profile_name(cfg)
    except comfy.ComfyError as error:
        raise BibleError(str(error))
    chosen, preset, default = resolve_preset(cfg, preset_name, profile)
    base = profile["base_url"]
    folder = renders_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    save_sheet_jobs(project, jobs, manifest)
    todo =[(job, seed) for job in jobs for seed in job["seeds"] if not only_seeds or seed in only_seeds]
    written = []
    for i, (job, seed) in enumerate(todo, 1):
        if should_stop and should_stop():
            raise llm.Cancelled()
        name = comfy.render_name(job["id"], seed, chosen, default, profile["name"], default_profile)
        target = folder / name
        if target.exists() and not force:
            if on_event:
                on_event({"kind": "skip", "done": i, "total": len(todo), "file": name})
            continue
        negative = job.get("negative") or profile.get("negative") or ""
        wf, out_id = comfy.prepare(workflow, job["prompt"], negative, seed, job["width"], job["height"],
                                   models=profile.get("models"), prefix="text2comics/bible", preset=preset,
                                   loras=profile.get("loras"), profile_name=profile["name"])
        try:
            data = comfy.run(base, wf, out_id, should_stop=should_stop or (lambda: False))
        except comfy.ComfyError as error:
            raise BibleError("%s（%s seed %d，profile %s）" % (error, job["id"], seed, profile["name"]))
        target.write_bytes(data)
        written.append(name)
        if on_event:
            on_event({"kind": "render", "done": i, "total": len(todo), "file": name, "size": (job["width"], job["height"])})
    return written


RENDER_RE = re.compile(r"^(?P<job>.+)_s(?P<seed>\d+)(?:_(?P<preset>[A-Za-z0-9-]+))?(?:__(?P<profile>[A-Za-z0-9-]+))?\.png$")


def render_files(project, kind, cid):
    """某個角色或地點目前有哪些定裝圖：[(檔名, manifest 項目, seed)]，依服裝、視角、檔名排序。"""
    manifest, out = load_manifest(project), []
    folder = renders_dir(project)
    for path in sorted(folder.glob("*.png")) if folder.is_dir() else []:
        m = RENDER_RE.match(path.name)
        info = manifest.get(m.group("job")) if m else None
        if info and info["kind"] == kind and info["id"] == cid:
            out.append((path.name, info, int(m.group("seed"))))
    return sorted(out, key=lambda x: (x[1]["outfit"], x[1]["view"], x[0]))


def contact_sheet(project, kind, cid, thumb=300):
    """某個角色或地點的候選圖拼成一張比較表（每張標上服裝、視角、seed），存在 build/bible/contact_<kind>_<id>.jpg。沒有圖回傳 None。"""
    from PIL import Image, ImageDraw
    files = render_files(project, kind, cid)
    if not files:
        return None
    cols = min(4, len(files))
    rows = (len(files) + cols - 1) // cols
    cells = []
    for name, info, seed in files:
        img = Image.open(renders_dir(project) / name).convert("RGB")
        cells.append((img.resize((thumb, int(img.height * thumb / img.width))), "%s/%s s%d" % (info["outfit"] or "-", info["view"], seed)))
    cell_h = max(c[0].height for c in cells) + 22
    sheet = Image.new("RGB", (cols * (thumb + 8) + 8, rows * (cell_h + 8) + 8), "white")
    draw = ImageDraw.Draw(sheet)
    for i, (img, label) in enumerate(cells):
        x, y = 8 + (i % cols) * (thumb + 8), 8 + (i // cols) * (cell_h + 8)
        sheet.paste(img, (x, y))
        draw.text((x, y + img.height + 4), label, fill="black")
    out = project.out_dir("build", "bible") / ("contact_%s_%s.jpg" % (kind, cid))
    sheet.save(out, quality=88)
    return out


# ---------------------------------------------------------------------------
# 定裝圖的選擇
# ---------------------------------------------------------------------------
def picks_path(project):
    return project.root / "bible" / "picks.json"


def load_picks(project):
    try:
        doc = json.loads(picks_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("version", 1)
    doc.setdefault("characters", {})
    doc.setdefault("locations", {})
    return doc


def card_signature(card, outfit=""):
    """外觀（sections 或 look_en）與該套服裝的指紋。挑圖時記下來；之後改了，狀態會標成「已過期」，提醒要重畫、重挑。"""
    fit = json.dumps(look.outfit(card, outfit) if outfit else {}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(("%s|%s" % (look.look_en(card), fit)).encode("utf-8")).hexdigest()[:10]


def pick(project, image, cid=None, outfit=None):
    """把 renders/bible/ 裡的一張圖記為「這個角色（這套服裝、這個視角）的定裝圖」。回傳 (類別, id, 服裝, 視角)。"""
    name = Path(image).name
    m = RENDER_RE.match(name)
    info = load_manifest(project).get(m.group("job")) if m else None
    if not info:
        raise BibleError("%s 不是定裝圖（檔名要像 ref_c_<角色>_<服裝>_<視角>_s<seed>.png；先執行 sheet --render）" % name)
    if not (renders_dir(project) / name).is_file():
        raise BibleError("找不到 %s" % (renders_dir(project) / name))
    if cid and cid != info["id"]:
        raise BibleError("%s 是「%s」的圖，不是「%s」" % (name, info["id"], cid))
    if outfit and outfit != info["outfit"]:
        raise BibleError("%s 是服裝「%s」，不是「%s」" % (name, info["outfit"], outfit))
    cards = project.characters if info["kind"] == "character" else project.locations
    sig = card_signature(cards.get(info["id"], {}), info["outfit"])
    doc = load_picks(project)
    if info["kind"] == "character":
        doc["characters"].setdefault(info["id"], {}).setdefault(info["outfit"], {})[info["view"]] = {"image": name, "sig": sig}
    else:
        doc["locations"][info["id"]] = {"image": name, "sig": sig}
    picks_path(project).parent.mkdir(parents=True, exist_ok=True)
    picks_path(project).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return info["kind"], info["id"], info["outfit"], info["view"]


# ---------------------------------------------------------------------------
# 服裝矩陣與狀態
# ---------------------------------------------------------------------------
def outfit_usage(project):
    """各章分鏡用了哪個角色的哪套服裝：{角色 id: {服裝 id: {章號: 格數}}}。"""
    usage = {}
    folder = project.root / "storyboard"
    for path in sorted(folder.glob("ch*.json")) if folder.is_dir() else []:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        chapter = int(doc.get("chapter") or re.sub(r"\D", "", path.stem) or 0)
        for page in doc.get("pages", []):
            for panel in page.get("panels", []):
                for entry in panel.get("characters") or []:
                    cell = usage.setdefault(entry.get("id"), {}).setdefault(entry.get("outfit") or "default", {})
                    cell[chapter] = cell.get(chapter, 0) + 1
    return usage


def _unlisted(project):
    names = set()
    for kind in ("character",):
        for cid, card in project.characters.items():
            names |= {cid, str(card.get("name")), *(str(a) for a in card.get("aliases") or [])}
        for meta in list_drafts(project, kind).values():
            names |= {str(meta.get("id")), str(meta.get("name")), *(str(a) for a in meta.get("aliases") or [])}
    out = {}
    plans = project.root / "plan"
    for path in sorted(plans.glob("ch*.json")) if plans.is_dir() else []:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for name in doc.get("unlisted_characters") or []:
            if name not in names:
                out.setdefault(name, []).append(int(doc.get("chapter") or 0))
    return out


def status(project):
    """視覺聖經的現況：每個角色的服裝（有沒有定裝圖、是否過期、各章用量）、地點、待確認的草稿、規劃裡列出但沒有卡片的人物。"""
    picks, usage = load_picks(project), outfit_usage(project)
    rows = []
    for cid, card in project.characters.items():
        outfits = card.get("outfits") or {"default": ""}
        notes = card.get("outfit_notes") or {}
        row = {"id": cid, "name": card.get("name", cid), "outfits": []}
        for oid in outfits:
            got = (picks["characters"].get(cid) or {}).get(oid) or {}
            full = got.get("full")
            state = "none" if not got else ("stale" if any(v.get("sig") != card_signature(card, oid) for v in got.values()) else "ok")
            row["outfits"].append({"id": oid, "note": notes.get(oid, ""), "picked": sorted(got), "state": state,
                                   "image": (full or {}).get("image", ""), "used": usage.get(cid, {}).get(oid, {})})
        for oid, cells in usage.get(cid, {}).items():
            if oid not in outfits:
                row["outfits"].append({"id": oid, "note": "（分鏡用了，卡片裡沒有）", "picked": [], "state": "missing", "image": "", "used": cells})
        rows.append(row)
    places = []
    for lid, card in project.locations.items():
        got = picks["locations"].get(lid)
        places.append({"id": lid, "name": card.get("name", lid), "state": "none" if not got else ("ok" if got.get("sig") == card_signature(card) else "stale"), "image": (got or {}).get("image", "")})
    return {"characters": rows, "locations": places,
            "drafts": {"characters": sorted(list_drafts(project, "character")), "locations": sorted(list_drafts(project, "location"))},
            "unlisted": _unlisted(project)}


STATE_MARK = {"ok": "✓ 已挑", "stale": "⚠ 已過期", "none": "－ 未挑", "missing": "✗ 缺卡"}


def _chapters(used):
    return "、".join("第 %d 章×%d" % (n, c) for n, c in sorted(used.items())) if used else "沒用到"


def matrix_markdown(st):
    lines = ["# 視覺聖經：服裝矩陣", "",
             "定裝圖狀態：✓ 已挑（look_en 與服裝都沒變）、⚠ 已過期（挑圖之後 look_en 或服裝改過，要重畫重挑）、－ 未挑、✗ 分鏡用了卡片裡沒有的服裝。", "",
             "## 角色", "", "| 角色 | 服裝 | 定裝圖 | 說明 | 各章用量 |", "|---|---|---|---|---|"]
    for row in st["characters"]:
        for i, o in enumerate(row["outfits"]):
            lines.append("| %s | %s | %s | %s | %s |" % ("%s（%s）" % (row["name"], row["id"]) if i == 0 else "", o["id"], STATE_MARK[o["state"]], o["note"] or "", _chapters(o["used"])))
    if not st["characters"]:
        lines.append("| （沒有角色卡） | | | | |")
    lines += ["", "## 地點", "", "| 地點 | 定裝圖 |", "|---|---|"]
    lines += ["| %s（%s） | %s |" % (p["name"], p["id"], STATE_MARK[p["state"]]) for p in st["locations"]] or ["| （沒有地點卡） | |"]
    pending = st["drafts"]["characters"] + st["drafts"]["locations"]
    if pending:
        lines += ["", "## 待確認的草稿（bible/drafts/）", "", "、".join(pending), "", "確認入庫：`python tools/bible.py accept <專案> <id>`"]
    if st["unlisted"]:
        lines += ["", "## 改編規劃裡列出、但還沒有卡片的人物", ""]
        lines += ["- %s（第 %s 章）" % (name, "、".join(str(c) for c in sorted(set(chs)))) for name, chs in st["unlisted"].items()]
    return "\n".join(lines) + "\n"
