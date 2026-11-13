"""視覺聖經的規則檢查（S6）：角色卡、地點卡、掃描結果。

目的和 check.py 一樣：把問題變成一份完整、說得出「哪個欄位哪裡不對」的清單。LLM 草稿用它回修，
人手寫的卡片用 `tools/bible.py check` 檢查，兩邊是同一套規則。

規則的來源是 CLAUDE.md 規則 3（角色外觀只有一個來源）與 docs/02_資料格式.md 第 3 節：
`look_en` 是固定外觀（年齡、性別、髮型、臉部特徵、體型），不寫表情、動作、服裝；服裝只寫在 `outfits`。
`compile.character_caption()` 會把 look_en 逐字貼進每一格，所以夾了表情或動作，每一格都會被污染。

只用標準函式庫，不依賴 FastAPI／fekit。
"""
import re

from . import look
from .check import CJK
from .project import ID_RE
from .script import Issue

FEMALE = {"woman", "girl", "female", "lady"}
MALE = {"man", "boy", "male", "guy"}

# look_en 不准出現的詞。三組分開列，錯誤訊息才能告訴模型「該搬去哪裡」。
CLOTHING_WORDS = (
    "wear wears wearing wore dressed dress uniform coverall coveralls jumpsuit jacket coat shirt t-shirt blouse sweater hoodie "
    "vest suit skirt trousers pants jeans shorts boots shoes sneakers gloves belt hat helmet headset headphones scarf cape cloak "
    "armor apron robe necktie insignia"
).split()
EXPRESSION_WORDS = (
    "smile smiles smiling smirk smirking grin grinning frown frowning scowl scowling angry sad happy cheerful crying laughing "
    "surprised shocked scared nervous worried annoyed irritated tired exhausted sleepy weary expression expressionless"
).split()
POSE_WORDS = (
    "standing sitting holding looking leaning walking running crouching kneeling gazing staring glancing pointing carrying "
    "facing posing crossed"
).split()
PEOPLE_WORDS = "man woman men women people person boy girl he she they his her their crowd figure figures worker workers".split()

# 服裝句子不准有的（表情、動作）；服裝本身當然可以有衣物
OUTFIT_FORBIDDEN = EXPRESSION_WORDS + POSE_WORDS

AGE = re.compile(
    r"\b(\d{1,2}[- ]years?[- ]old|teenage|teen|young|middle-aged|elderly|old|child|kid|"
    r"in (?:his|her|their) (?:early |mid-?|late )?(?:teens|twenties|thirties|forties|fifties|sixties|seventies|eighties))\b", re.I)
HAIR = re.compile(r"\b(hair|haired|bald|shaved|buzz|buzzcut|ponytail|bun|braid|braids|bangs|fringe|crew cut|mohawk)\b", re.I)
OUTFIT_START = re.compile(r"^(He|She|They)\s+(wears?|is wearing|are wearing)\b")

MIN_LOOK_WORDS = 6
WARN_LOOK_WORDS = 60
WARN_LOCATION_WORDS = 90


def words(text):
    return re.findall(r"[A-Za-z][A-Za-z'-]*", str(text or ""))


def _hits(text, vocabulary):
    """text 裡用到的詞（整字比對，不分大小寫），依出現順序、不重複。"""
    vocab = set(vocabulary)
    seen = []
    for w in re.findall(r"[A-Za-z][A-Za-z'-]*", str(text or "").lower()):
        if w in vocab and w not in seen:
            seen.append(w)
    return seen


def gender(look_en):
    """'she' | 'he' | None（兩邊都有或都沒有）。compile._subject 也是看這幾個字。"""
    found = set(re.findall(r"[a-z]+", str(look_en or "").lower()))
    female, male = bool(found & FEMALE), bool(found & MALE)
    if female and not male:
        return "she"
    if male and not female:
        return "he"
    return None


def _where(label, key=""):
    return ("%s／%s" % (label, key)) if key else label


# ---------------------------------------------------------------------------
# look_en、outfits
# ---------------------------------------------------------------------------
def check_character_look(look_en, label):
    issues = []
    if not isinstance(look_en, str) or not look_en.strip():
        return [Issue("error", label, "look_en 不能是空的：寫一句英文固定外觀（年齡、性別、髮型髮色、眼睛與臉部特徵、體型）")]
    if CJK.search(look_en):
        issues.append(Issue("error", label, "look_en 要寫英文（生圖提示詞用英文），目前含有中文：「%s」" % look_en[:30]))
        return issues
    n = len(words(look_en))
    if n < MIN_LOOK_WORDS:
        issues.append(Issue("error", label, "look_en 太短（%d 個字）：至少要有年齡、性別、髮型髮色、眼睛或臉部特徵、體型" % n))
    elif n > WARN_LOOK_WORDS:
        issues.append(Issue("warn", label, "look_en 有 %d 個字，偏長；每一格都會逐字貼上，越長越容易互相打架，建議 %d 字內" % (n, WARN_LOOK_WORDS)))
    for vocabulary, what, where_to in ((CLOTHING_WORDS, "服裝或配件", "outfits"),
                                       (EXPRESSION_WORDS, "表情或情緒", "分鏡的 expression"),
                                       (POSE_WORDS, "動作或姿勢", "分鏡的 pose")):
        found = _hits(look_en, vocabulary)
        if found:
            issues.append(Issue("error", label, "look_en 裡的「%s」是%s，不是固定外觀；請刪掉（%s）。look_en 只寫年齡、性別、髮型髮色、眼睛與臉部特徵、體型，永久的特徵（疤、痣、眼鏡）可以寫"
                                % ("、".join(found), what, "要寫進 %s" % where_to if where_to != "outfits" else "服裝請寫進 outfits")))
    if gender(look_en) is None:
        issues.append(Issue("error", label, "look_en 要明確寫出性別（woman／man／girl／boy 其中一個）：編譯時依它決定多角色左右句的 She／He"))
    if not AGE.search(look_en):
        issues.append(Issue("error", label, "look_en 缺少年齡（例如 a 19-year-old、a middle-aged、an elderly）"))
    if not HAIR.search(look_en):
        issues.append(Issue("warn", label, "look_en 沒有提到頭髮（髮型、髮色，或 bald）；髮型是辨識角色最重要的線索"))
    return issues


def check_sections(card, label):
    """分段卡片（sections）：每一段只寫永久外觀，服裝放 outfits、固定的配件放 accessory。
    性別／年齡／頭髮改成「整張卡加起來要有」（舊的 look_en 是一句話裡就要有）。"""
    issues = []
    parts = look.sections(card)
    for name in look.SECTIONS:
        for phrase in parts.get(name, ()):
            where = _where(label, "sections.%s" % name)
            if CJK.search(phrase):
                issues.append(Issue("error", where, "要寫英文（生圖提示詞用英文），目前含有中文：「%s」" % phrase[:30]))
                continue
            found = _hits(phrase, EXPRESSION_WORDS + POSE_WORDS)
            if found:
                issues.append(Issue("error", where, "「%s」是表情或動作，不是固定外觀；請刪掉（表情與姿勢寫在分鏡的 expression／pose）" % "、".join(found)))
            if name != "accessory":
                found = _hits(phrase, CLOTHING_WORDS)
                if found:
                    issues.append(Issue("error", where, "「%s」是服裝；服裝寫進 outfits（上著／下著／套裝），固定配件放 sections.accessory" % "、".join(found)))
    if not parts:
        return [Issue("error", label, "sections 是空的：至少要有 head")]
    text = look.look_en(card)
    n = len(words(text))
    if n < MIN_LOOK_WORDS:
        issues.append(Issue("error", label, "整張卡的外觀只有 %d 個字，太短：至少要有年齡、性別、髮型髮色、眼睛或臉部特徵、體型" % n))
    elif n > WARN_LOOK_WORDS:
        issues.append(Issue("warn", label, "整張卡的外觀有 %d 個字，偏長；越長越容易互相打架，建議 %d 字內" % (n, WARN_LOOK_WORDS)))
    if gender(text) is None:
        issues.append(Issue("error", label, "sections 要明確寫出性別（woman／man／girl／boy 其中一個，通常放 head）：編譯時依它決定多角色時左右句的 She／He"))
    if not AGE.search(text):
        issues.append(Issue("error", label, "sections 缺少年齡（例如 a 19-year-old、an elderly；通常放 head）"))
    if not HAIR.search(" ".join(parts.get("head", ()))):
        issues.append(Issue("warn", _where(label, "sections.head"), "沒有提到頭髮（髮型、髮色，或 bald）；髮型是辨識角色最重要的線索"))
    return issues


def check_outfit_parts(parts, label, oid):
    """新格式的服裝：{top: […], bottom: […], set: […], notes: 中文說明}。"""
    where = _where(label, "outfits.%s" % oid)
    if not isinstance(parts, dict):
        return [Issue("error", where, "要寫成 {top: […], bottom: […], set: […]} 或一句英文（舊格式）")]
    issues = []
    unknown = sorted(k for k in parts if k not in look.OUTFIT_PARTS and k != "notes")
    if unknown:
        issues.append(Issue("error", where, "不認得的欄位：%s（只接受 top、bottom、set、notes）" % "、".join(unknown)))
    total = 0
    for name in look.OUTFIT_PARTS:
        for item in look._phrases(parts.get(name)):
            total += 1
            if CJK.search(item):
                issues.append(Issue("error", _where(where, name), "要寫英文，目前含有中文：「%s」" % item[:30]))
                continue
            found = _hits(item, OUTFIT_FORBIDDEN)
            if found:
                issues.append(Issue("error", _where(where, name), "「%s」是表情或動作，不屬於服裝；只寫穿什麼（顏色、款式、配件）" % "、".join(found)))
    if not total:
        issues.append(Issue("error", where, "至少要有一項：top（上著）、bottom（下著）或 set（一整套的名稱）"))
    return issues


def check_outfits(outfits, look_en, label):
    issues = []
    if outfits is None:
        return [Issue("error", label, "缺少 outfits：至少要有 default（預設服裝），寫成完整英文句，例如 He wears …")]
    if not isinstance(outfits, dict) or not outfits:
        return [Issue("error", label, "outfits 要是 {服裝id: 英文完整句} 的物件，至少有 default")]
    if "default" not in outfits:
        issues.append(Issue("error", label, "outfits 沒有 default：沒指定服裝的格子會用它"))
    want = gender(look_en)
    for oid, sentence in outfits.items():
        where = _where(label, "outfits.%s" % oid)
        if not ID_RE.match(str(oid)):
            issues.append(Issue("error", where, "服裝 id「%s」只能用 a-z、0-9、底線、連字號" % oid))
        if isinstance(sentence, dict):                 # 新格式：{top: […], bottom: […], set: […]}
            issues += check_outfit_parts(sentence, label, oid)
            continue
        if not isinstance(sentence, str) or not sentence.strip():
            issues.append(Issue("error", where, "不能是空的：寫一句英文，例如 He wears …"))
            continue
        if CJK.search(sentence):
            issues.append(Issue("error", where, "要寫英文，目前含有中文：「%s」" % sentence[:30]))
            continue
        m = OUTFIT_START.match(sentence.strip())
        if not m:
            issues.append(Issue("error", where, "要寫成完整句子，以 He wears／She wears 開頭（編譯時整句貼進該格）：「%s…」" % sentence.strip()[:40]))
        elif want and m.group(1).lower() not in (want, "they"):
            issues.append(Issue("error", where, "代名詞和 look_en 的性別不一致：look_en 是 %s，這句卻用 %s" % (want, m.group(1))))
        found = _hits(sentence, OUTFIT_FORBIDDEN)
        if found:
            issues.append(Issue("error", where, "「%s」是表情或動作，不屬於服裝；服裝句只寫穿什麼（顏色、款式、配件）" % "、".join(found)))
    return issues


def check_location_look(look_en, label):
    issues = []
    if not isinstance(look_en, str) or not look_en.strip():
        return [Issue("error", label, "look_en 不能是空的：寫一句英文固定場景描述")]
    if CJK.search(look_en):
        return [Issue("error", label, "look_en 要寫英文，目前含有中文：「%s」" % look_en[:30])]
    n = len(words(look_en))
    if n < MIN_LOOK_WORDS:
        issues.append(Issue("error", label, "look_en 太短（%d 個字）：寫出空間的形狀、材質、光線與主要設施" % n))
    elif n > WARN_LOCATION_WORDS:
        issues.append(Issue("warn", label, "look_en 有 %d 個字，偏長；每一格都會貼上，建議 %d 字內" % (n, WARN_LOCATION_WORDS)))
    found = _hits(look_en, PEOPLE_WORDS)
    if found:
        issues.append(Issue("error", label, "地點的 look_en 不要有人（「%s」）：人物由角色卡提供，場景只描述空間；這樣同一個地點才能放進任何人數的格子" % "、".join(found)))
    return issues


def check_card(card, kind="character", label=None):
    """角色卡或地點卡的 frontmatter（dict）→ issues。"""
    label = label or "%s %s" % ("角色" if kind == "character" else "地點", card.get("id") or card.get("name") or "?")
    issues = []
    cid = str(card.get("id") or "")
    if not ID_RE.match(cid):
        issues.append(Issue("error", label, "id 只能用 a-z、0-9、底線、連字號（目前是 %r）；建議用姓名的漢語拼音，例如 chen_jiahe" % cid))
    if not str(card.get("name") or "").strip():
        issues.append(Issue("error", label, "缺少 name（顯示名稱）"))
    aliases = card.get("aliases")
    if aliases is not None and (not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases)):
        issues.append(Issue("error", label, "aliases 要是字串清單"))
    if kind == "character":
        if look.has_sections(card):                    # 步驟 2：有 sections 就檢查分段，沒有就退回整段 look_en
            issues += check_sections(card, label)
        else:
            issues += check_character_look(card.get("look_en"), label)
        issues += check_outfits(card.get("outfits"), look.look_en(card), label)
        notes = card.get("outfit_notes")
        if notes is not None:
            if not isinstance(notes, dict):
                issues.append(Issue("error", label, "outfit_notes 要是 {服裝id: 中文說明}"))
            else:
                for oid in notes:
                    if isinstance(card.get("outfits"), dict) and oid not in card["outfits"]:
                        issues.append(Issue("warn", label, "outfit_notes 的「%s」不在 outfits 裡" % oid))
    else:
        issues += check_location_look(card.get("look_en"), label)
    return issues


# ---------------------------------------------------------------------------
# 掃描結果（每章一份，LLM 產生）
# ---------------------------------------------------------------------------
def squash(text):
    """比對引文用：去掉所有空白（含全形空白）。"""
    return re.sub(r"\s+", "", str(text or "").replace("　", ""))


def _strings(value):
    return [str(v).strip() for v in value if isinstance(v, (str, int)) and str(v).strip()] if isinstance(value, list) else []


def _evidence_list(raw, label, key, chapter_text, issues, with_label=False):
    """looks／outfits 的每一項：en 是英文短語、quote 必須是本章原文裡逐字找得到的一小段。"""
    out = []
    if raw is None:
        return out
    if not isinstance(raw, list):
        issues.append(Issue("error", label, "%s 要是清單" % key))
        return out
    squashed = squash(chapter_text)
    for i, item in enumerate(raw, 1):
        where = _where(label, "%s[%d]" % (key, i))
        if not isinstance(item, dict):
            issues.append(Issue("error", where, "每一項要是物件 {\"en\": …, \"quote\": …}"))
            continue
        en, quote = str(item.get("en") or "").strip(), str(item.get("quote") or "").strip()
        if not en or CJK.search(en):
            issues.append(Issue("error", where, "en 要寫英文短語（不是中文、不是空的）：%r" % en[:30]))
            continue
        if len(squash(quote)) < 2:
            issues.append(Issue("error", where, "quote 要引原文一小段（至少 2 個字）：外觀只能寫原文有依據的，沒有依據就不要列"))
            continue
        if squash(quote) not in squashed:
            issues.append(Issue("error", where, "quote「%s」在本章原文裡找不到。請改成原文逐字的一小段（不要改寫、不要翻譯）" % quote[:30]))
            continue
        if key == "looks":
            bad = _hits(en, EXPRESSION_WORDS + POSE_WORDS + CLOTHING_WORDS)
            if bad:
                issues.append(Issue("error", where, "looks 只記永久外觀；「%s」是表情、動作或服裝（服裝請放 outfits，暫時的狀態不要記）" % "、".join(bad)))
                continue
        entry = {"en": en, "quote": quote}
        if with_label:
            entry = {"label": str(item.get("label") or "").strip(), **entry}
            if not entry["label"]:
                issues.append(Issue("error", where, "outfits 的每一項要有 label（中文服裝名，例如「工作連身服」）"))
                continue
        out.append(entry)
    return out


def normalize_scan(data, chapter_text, chapter_label=""):
    """一章的掃描輸出 → (乾淨的結構, issues)。只留已知欄位；名字沒出現在本章的、引文找不到的都是 error。"""
    issues = []
    head = chapter_label or "本章"
    if not isinstance(data, dict):
        return None, [Issue("error", "", "最外層要是 {\"characters\": [...], \"locations\": [...], \"mentioned_only\": [...]}")]
    squashed = squash(chapter_text)
    clean = {"characters": [], "locations": [], "mentioned_only": _strings(data.get("mentioned_only"))}
    for kind, plural in (("character", "characters"), ("location", "locations")):
        raw = data.get(plural)
        if raw is None:
            continue
        if not isinstance(raw, list):
            issues.append(Issue("error", head, "%s 要是清單" % plural))
            continue
        for item in raw:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                issues.append(Issue("error", head, "%s 的每一項要是物件，而且有 name" % plural))
                continue
            name = str(item["name"]).strip()
            label = "%s「%s」" % ("角色" if kind == "character" else "地點", name)
            aliases = [a for a in _strings(item.get("aliases")) if a != name]
            if not any(squash(n) in squashed for n in [name] + aliases):
                issues.append(Issue("error", label, "名字和別名都沒有出現在本章原文裡；只列本章實際出現的%s（名字要用原文的寫法）" % ("人物" if kind == "character" else "地點")))
                continue
            entry = {"name": name, "aliases": aliases, "role": str(item.get("role") or "").strip(),
                     "looks": _evidence_list(item.get("looks"), label, "looks", chapter_text, issues)}
            if kind == "character":
                entry["outfits"] = _evidence_list(item.get("outfits"), label, "outfits", chapter_text, issues, with_label=True)
            clean[plural].append(entry)
    return clean, issues


# ---------------------------------------------------------------------------
# 成卡輸出（每個角色或地點一次呼叫）
# ---------------------------------------------------------------------------
def normalize_card(data, kind):
    """模型的成卡輸出 → (乾淨的 dict, issues)。這裡只檢查結構；內容規則交給 check_card。"""
    if not isinstance(data, dict):
        return None, [Issue("error", "", "最外層要是 JSON 物件")]
    keys = ("id", "name", "aliases", "look_en", "sections", "summary", "invented", "conflicts")
    if kind == "character":
        keys += ("outfits", "outfit_notes")
    card = {k: data[k] for k in keys if k in data}
    for k in ("invented", "conflicts"):
        card[k] = _strings(card.get(k))
    card["aliases"] = _strings(card.get("aliases"))
    card["summary"] = str(card.get("summary") or "").strip()
    card["id"] = str(card.get("id") or "").strip()
    if isinstance(card.get("sections"), dict):        # 分段（步驟 2）：每一段都是英文短語清單
        card["sections"] = {str(k): [str(x).strip() for x in v if str(x).strip()]
                            for k, v in card["sections"].items() if isinstance(v, list)}
        if not card.get("look_en"):
            card["look_en"] = look.look_en(card)
    if kind == "character":
        outfits = card.get("outfits")
        if isinstance(outfits, dict):
            card["outfits"] = {str(k).strip(): (v.strip() if isinstance(v, str) else v) for k, v in outfits.items()}
        notes = card.get("outfit_notes")
        card["outfit_notes"] = {str(k).strip(): str(v).strip() for k, v in notes.items()} if isinstance(notes, dict) else {}
    return card, []
