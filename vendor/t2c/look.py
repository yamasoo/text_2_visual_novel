"""角色外觀的分段（docs/06 決策記錄 D4、docs/07 微調簡報步驟 2、docs/reports/01 的「卡片要分八段」）。

一個角色的外觀有兩種寫法，兩種都要能讀：
    sections: {head, body, upper, lower, accessory}      新的：每一段是英文短語清單
    look_en: 一整句英文                                  舊的：整段逐字貼進提示詞
有 sections 就用 sections，沒有就退回 look_en。服裝同理：
    outfits: {id: {top: [...], bottom: [...], set: [...], notes: 中文說明}}
    outfits: {id: "He wears …"}                          舊的：一整句

`caption_parts()` 是唯一決定「這一格要送哪幾段」的地方：分鏡的 shot 決定段，動作／表情由分鏡給。
`fingerprint()` 給帳單軌用：記下這一格用了哪幾段、每段的指紋，卡片改了就算得出哪幾格過期。
只用標準函式庫。
"""
import hashlib

SECTIONS = ("head", "body", "upper", "lower", "accessory")
OUTFIT_PARTS = ("top", "bottom", "set")
SECTION_TEXT = {"head": "頭部", "body": "身形", "upper": "上半身", "lower": "下半身", "accessory": "飾品"}
OUTFIT_TEXT = {"top": "上著", "bottom": "下著", "set": "套裝"}

# 分鏡的 shot → 送哪幾段（docs/reports/01_build_book_flow.md 的「按鏡頭挑段落」）。
# 特寫送全身外觀會稀釋提示詞；同一個角色的臉在任何鏡頭都來自同一段 head 文字。
SHOT_SECTIONS = {
    "extreme_close": ("head",),
    "close": ("head", "accessory"),
    "medium": ("head", "upper", "accessory"),
    "full": ("head", "body", "upper", "lower", "accessory"),
    "wide": ("head", "body", "upper", "lower", "accessory"),
    "extreme_wide": SECTIONS,
}
SHOT_OUTFIT = {
    "extreme_close": (),
    "close": (),
    "medium": ("top",),
    "full": ("top", "bottom"),
    "wide": ("top", "bottom"),
    "extreme_wide": OUTFIT_PARTS,
}
DEFAULT_SHOT = "medium"


def _phrases(value):
    """字串或字串清單 → 乾淨的短語清單（空白收斂、去掉空字串）。"""
    items = value if isinstance(value, (list, tuple)) else ([value] if value else [])
    out = []
    for item in items:
        text = " ".join(str(item or "").split())
        if text:
            out.append(text)
    return tuple(out)


def sections(card):
    """卡片的 sections：{段名: (短語…)}，只留真的有內容的段。沒有 sections 就是空的。"""
    raw = (card or {}).get("sections")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name in SECTIONS:
        parts = _phrases(raw.get(name))
        if parts:
            out[name] = parts
    return out


def has_sections(card):
    return bool(sections(card))


def look_en(card):
    """整段外觀文字：有 sections 就用全部段落接起來，沒有就用 look_en。
    這是相容層：分鏡提示詞以外的讀者（LLM 的上下文、卡片清單）用它就好。"""
    card = card or {}
    parts = sections(card)
    if parts:
        return ", ".join(phrase for name in SECTIONS for phrase in parts.get(name, ()))
    return " ".join(str(card.get("look_en") or "").split())


def outfit(card, outfit_id="default"):
    """某一套服裝 → {"parts": {top: (…), bottom: (…), set: (…)}, "sentence": 舊格式的整句, "notes": 中文說明}。"""
    outfits = (card or {}).get("outfits") or {}
    raw = outfits.get(outfit_id) if isinstance(outfits, dict) else None
    notes = ((card or {}).get("outfit_notes") or {}).get(outfit_id) or ""
    if isinstance(raw, str):
        return {"parts": {}, "sentence": " ".join(raw.split()), "notes": notes, "legacy": True}
    parts = {}
    if isinstance(raw, dict):
        for name in OUTFIT_PARTS:
            items = _phrases(raw.get(name))
            if items:
                parts[name] = items
        notes = " ".join(str(raw.get("notes") or notes or "").split())
    return {"parts": parts, "sentence": "", "notes": notes, "legacy": False}


def shot_key(shot):
    """不認得的 shot 當成 medium（compile 也會照 medium 寫進提示詞）。"""
    shot = str(shot or "")
    return shot if shot in SHOT_SECTIONS else DEFAULT_SHOT


def caption_parts(card, shot, outfit_id="default"):
    """這一格這一個角色要貼上的片段，依序 [{"source", "text"}]。
    source 是「哪一段」：section 名、"outfits.<id>.<part>"、"look_en"。指紋與過期清單都靠它。"
    legacy 卡片（只有 look_en）回一整段，source 是 "look_en"。"""
    card = card or {}
    shot = shot_key(shot)
    parts = sections(card)
    out = []
    if parts:
        for name in SHOT_SECTIONS[shot]:
            phrases = parts.get(name) or ()
            if phrases:
                out.append({"source": name, "text": ", ".join(phrases)})
    else:
        text = look_en(card)
        if text:
            out.append({"source": "look_en", "text": text})
    fit = outfit(card, outfit_id)
    if fit["sentence"]:
        if fit["sentence"]:
            out.append({"source": "outfits.%s" % outfit_id, "text": fit["sentence"]})
    elif fit["parts"]:
        for name in SHOT_OUTFIT[shot]:
            phrases = fit["parts"].get(name) or ()
            if phrases:
                out.append({"source": "outfits.%s.%s" % (outfit_id, name), "text": ", ".join(phrases)})
    return out


def _digest(text):
    return hashlib.sha1(" ".join(str(text or "").split()).encode("utf-8")).hexdigest()[:12]


def fingerprint(card, shot, outfit_id="default"):
    """這一格這一個角色用到的每一段 → {source: 指紋}。卡片改了哪一段，比對就知道。"""
    return {part["source"]: _digest(part["text"]) for part in caption_parts(card, shot, outfit_id)}


def all_sources(card, outfit_id="default"):
    """這張卡片全部的段（不受 shot 影響）：過期清單要說「改了哪一段」時用。"""
    sources = {}
    for shot in SHOT_SECTIONS:
        for source, digest in fingerprint(card, shot, outfit_id).items():
            sources.setdefault(source, digest)
    return sources
