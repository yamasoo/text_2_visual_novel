"""角色解析（微5／docs/06 決策 D9–D12）：名字、別名或 id → 角色 id，全專案只有這一份。

分鏡（compile）、劇本（script）、聖經（bible）三邊都用同一套比對，所以收在這裡（D12）。
比對順序：精確 id → 卡片姓名 → 別名 → 正規化（NFKC、大小寫、空白、連字號與底線）。
繁簡與不同拼法（hynesen／heinessen）救不到，那是「一鍵對應」的工作（D11）：suggest() 只給建議，
呼叫端不要自動套用。

只用標準函式庫。
"""
import difflib
import re
import unicodedata

_SEP = re.compile(r"[\s_\-·・.,，、]+")


def normalize(text):
    """比對用的正規化（D11：只做機械差異，不做語意猜測）。"""
    if text is None:
        return ""
    return _SEP.sub("", unicodedata.normalize("NFKC", str(text)).casefold())


def _names(cid, card):
    """這張卡片所有的寫法：id、姓名、別名（空的去掉）。"""
    out = [str(cid), str(card.get("name") or "")]
    out += [str(a) for a in card.get("aliases") or []]
    return [n for n in out if n]


def cards(project, kind="character"):
    return project.characters if kind == "character" else project.locations


def resolve(project, raw, kind="character"):
    """→ (卡片 id, 比對方式)；找不到回 (None, None)。
    比對方式：id／name／alias／normalized（前三種是逐字相同，最後一種是正規化後才相同）。"""
    if raw is None or not str(raw).strip():
        return None, None
    text = str(raw).strip()
    table = cards(project, kind)
    if text in table:
        return text, "id"
    for cid, card in table.items():
        if text == str(card.get("name") or ""):
            return cid, "name"
    for cid, card in table.items():
        if text in [str(a) for a in card.get("aliases") or []]:
            return cid, "alias"
    wanted = normalize(text)
    if wanted:
        for cid, card in table.items():
            if wanted in {normalize(n) for n in _names(cid, card)}:
                return cid, "normalized"
    return None, None


def suggest(project, raw, kind="character", limit=3, cutoff=0.6):
    """解析失敗時的建議卡片 id（D11：只給建議，不自動套用）。"""
    key = normalize(raw)
    if not key:
        return []
    pool = {}
    for cid, card in cards(project, kind).items():
        for name in _names(cid, card):
            pool.setdefault(normalize(name), str(cid))
    out = []
    for hit in difflib.get_close_matches(key, list(pool), n=limit, cutoff=cutoff):
        cid = pool[hit]
        if cid not in out:
            out.append(cid)
    return out


def grade(project, entry):
    """分鏡 characters[] 的一項 → (卡片 id, 等級, 建議清單)。
    等級（D9）：card＝有卡；extra＝標了 extra: true 的臨時角色；missing＝待補的具名角色（維持探索用）。"""
    cid, _how = resolve(project, entry.get("id"))
    if cid is not None:
        return cid, "card", []
    if entry.get("extra") is True:
        return None, "extra", []
    return None, "missing", suggest(project, entry.get("id"))

