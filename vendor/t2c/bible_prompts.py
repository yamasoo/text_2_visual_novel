"""視覺聖經抽取的提示詞（S6）：原著 → ① 掃描（每章一次）→ ② 成卡（每個角色、地點一次）。

設計原則（細節見 docs/04_提示詞設計.md 第 7 節）：

1. **外觀只能有依據**：掃描階段每一條外觀都要附原文引文，程式會逐字比對（`bible_check.normalize_scan`）。
   模型編不出沒有依據的外觀；成卡階段補上的部分一律列在 `invented`，讓人一眼看出哪些是補的。
2. **規則寫在程式裡**：look_en／outfits 的規則由 `bible_check` 檢查，提示詞只是把同一套規則講給模型聽（詞表也是同一份）。
3. **分兩步**：掃描一章只要看一章（便宜、可續跑、可平行），成卡只看整理過的證據，不必再把整本小說塞進去。
4. **作者的設定優先**：有作者寫的設定檔（`--notes`）就一起給；和原文衝突時，以原文為準，並把衝突列在 `conflicts`。
5. 範例角色是虛構的（阿杰、小晴、拱廊），測試保證範例本身能通過檢查。
"""
import json

from . import bible_check as bc

SYSTEM = (
    "你是日本漫畫的角色設計師兼設定考證編輯，負責從小說整理出漫畫用的視覺聖經（角色外觀、服裝、場景）。"
    "你的輸出會被程式直接讀取，所以格式必須完全照指示；不要加前言、說明、結語，也不要用 ``` 圍欄。"
    "用繁體中文（台灣用語）；指定要寫英文的欄位才寫英文。"
)


def _words(vocab):
    return "、".join(vocab)


# ---------------------------------------------------------------------------
# ① 掃描
# ---------------------------------------------------------------------------
SCAN_RULES = """# 任務：視覺聖經掃描（一次一章）
讀下面這一章小說，列出本章**實際出場**的角色與地點，以及原文**明寫**的外觀線索。這一步只收集證據，不設計外觀。

## 規則
1. **只列本章出場的**：有動作、說話、或被描寫在場的人才算。只被提到、沒有出場的人（例如被別人轉述的上司、已離職的前輩）不要放進 characters，把名字列在 mentioned_only。
2. **名字用原文的寫法**；同一個人有不同稱呼（全名、暱稱、職稱）時，用最常用的當 name，其餘放 aliases。已出現過的名稱見下面「已知名稱」，同一個人請沿用同一個 name。
3. **looks（永久外觀）**：年齡、性別、髮型、髮色、眼睛、膚色、體型、臉部特徵、疤痕、眼鏡等**不會因場景改變**的特徵。每一條有兩個欄位：
   - en：英文短語（名詞片語，不是句子），例如 "a scar across the back of his right hand"、"a woman"、"middle-aged"。
   - quote：**原文逐字**的一小段（5–30 字），證明這個外觀是原文寫的。性別、年齡若只能從代名詞（他／她）或稱謂看出來，就引那一句。
   原文沒寫的**不要列**，也不要憑常識推測；沒有線索就讓 looks 是空清單。
4. **outfits（服裝與隨身配件）**：衣服、制服、耳機、工具袋、筆記本這類**會換的東西**。每一條有 label（中文服裝名，例如「工作連身服」）、en（英文短語，例如 "dark blue work coveralls with rolled-up sleeves and a utility belt"）、quote（原文逐字）。穿著沒有變化就只列一條；明顯換了一套才另列。
5. **不要記**：表情、情緒、動作、姿勢、暫時的狀態（累、髒、流汗、受傷的血）。這些由分鏡決定。
6. **地點 locations**：本章的場景。looks 記空間的形狀、材質、光線、主要設施（en＋quote，同上）。地點裡不要寫人。
7. **quote 會被程式逐字比對**：複製原文，不要改寫、不要翻譯、不要加省略號。找不到會被退回。
8. en 一律英文；name、aliases、role、label 用繁體中文。

## 輸出格式（只輸出這個 JSON）
{
  "characters": [
    {"name": "名字", "aliases": ["別名"], "role": "一句話：這個人在本章做什麼",
     "looks": [{"en": "英文短語", "quote": "原文逐字"}],
     "outfits": [{"label": "中文服裝名", "en": "英文短語", "quote": "原文逐字"}]}
  ],
  "locations": [
    {"name": "地點名", "aliases": [], "role": "一句話：這裡發生什麼",
     "looks": [{"en": "英文短語", "quote": "原文逐字"}]}
  ],
  "mentioned_only": ["只被提到、沒出場的人名"]
}
本章沒有的項目給空清單 []。"""

SCAN_EXAMPLE_TEXT = "阿杰把濕透的帽T掛在椅背上，揉了揉那頭亂翹的短髮。小晴戴著黑框眼鏡，從拱廊盡頭的書店探出頭來：「又忘了帶傘？」"
SCAN_EXAMPLE = {
    "characters": [
        {"name": "阿杰", "aliases": [], "role": "淋濕了跑進拱廊",
         "looks": [{"en": "unruly short hair", "quote": "那頭亂翹的短髮"}],
         "outfits": [{"label": "帽T", "en": "a soaked hooded sweatshirt", "quote": "濕透的帽T"}]},
        {"name": "小晴", "aliases": [], "role": "從書店探頭調侃他",
         "looks": [{"en": "black-rimmed glasses", "quote": "戴著黑框眼鏡"}], "outfits": []}],
    "locations": [{"name": "拱廊", "aliases": [], "role": "兩人碰面的地方", "looks": []}],
    "mentioned_only": []}


def scan_messages(chapter_label, chapter_text, known_names=(), notes_block=""):
    parts = [SCAN_RULES, "", "## 格式範例（人物與場景是虛構的，只示範寫法）", "範例原文：", SCAN_EXAMPLE_TEXT, "範例輸出：",
             json.dumps(SCAN_EXAMPLE, ensure_ascii=False)]
    if notes_block:
        parts += ["", notes_block]
    if known_names:
        parts += ["", "## 已知名稱（前面章節已出現；同一個人請沿用）", "、".join(known_names)]
    parts += ["", "---", "## 本章：%s" % chapter_label, "<<<原文開始", chapter_text.strip(), "原文結束>>>",
              "", "請輸出本章的掃描 JSON。"]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(parts)}]


# ---------------------------------------------------------------------------
# ② 成卡
# ---------------------------------------------------------------------------
LOOK_RULES = """## sections（固定外觀，分五段）的規則
每一段是**英文短語清單**（不是句子、不加句號），只放永久特徵：
- head（頭部）：年齡、性別、種族或膚色、髮型與髮色、眼睛與臉部特徵。**每張卡都要有 head**，性別（woman／man／girl／boy）與年齡放這裡。
- body（身形）：體型、身高、骨架。
- upper（上半身）：鎖骨、肩線這類上半身的身體詞。
- lower（下半身）：腰、臀、腿這類下半身的身體詞。
- accessory（飾品）：眼鏡、耳環、項鍊、髮飾這類**固定**配件（會換的放 outfits）。
- 例：head 放 ["A 22-year-old East Asian man", "short, unruly black hair", "a square jaw", "thick eyebrows"]、body 放 ["a lean, athletic build"]。
- **不准寫**：
  - 服裝：%(clothing)s
  - 表情與情緒：%(expression)s
  - 動作與姿勢：%(pose)s
  這些會被程式擋下。服裝寫進 outfits，表情與動作由分鏡決定。
- 分鏡的特寫只貼 head、中景貼 head＋upper＋accessory、全身與遠景多貼 body 與 lower，所以 head 要能單獨成立（一眼認得出這個人）。
- 這幾段會**逐字貼進提示詞**，之後不會再改。""" % {
    "clothing": _words(bc.CLOTHING_WORDS), "expression": _words(bc.EXPRESSION_WORDS), "pose": _words(bc.POSE_WORDS)}

CHARACTER_RULES = """# 任務：角色卡
依下面整理好的證據，寫出這個角色的視覺聖經卡片。

## 資料的優先順序
1. 小說原文的證據（looks、outfits；每條都有引文）。
2. 作者的設定檔（若有；可能含給別的繪圖模型用的標籤，把它們當成「作者想要的外觀」轉成英文短語）。
3. 兩者衝突時**以原文為準**，並把衝突寫進 conflicts（一項一句中文：哪個欄位、設定怎麼寫、原文怎麼寫、你採用哪個），讓作者決定。
4. 必要欄位（年齡、性別、種族或膚色、髮型髮色、眼睛、體型）證據裡沒有時，**用平實、容易畫出辨識度的預設補上**，並逐項列在 invented（一項一句中文：補了什麼、為什麼）。種族或膚色沒有線索時，依姓名的文化背景補。不要補花俏或戲劇化的特徵。

""" + LOOK_RULES + """

## outfits（服裝）的規則
- 物件 {服裝id: {top: […], bottom: […], set: […], notes: 中文說明}}，**一定要有 default**（最常穿的那套）。服裝 id 用英文小寫、底線，例如 work_coveralls、dress_uniform。
- top（上著）、bottom（下著）、set（一整套的名字，例如 school uniform）：每一項是英文短語，寫顏色與款式，例如 "navy school blazer"、"grey pleated skirt"；不要寫成句子、不要寫表情或動作。
- 分鏡的特寫不貼服裝、中景貼 top、全身與遠景貼 top＋bottom，大遠景連 set 一起貼。
- 只記**明顯不同的套裝**；髒了、捲起袖子、拉上拉鍊不算另一套。證據只有一種穿著就只給 default。
- notes 寫中文一句話，說明什麼時候穿（可寫章節）。

## 輸出格式（只輸出這個 JSON）
{
  "id": "小寫英文 id，用姓名的漢語拼音（例如 chen_jiahe）；只能用 a-z、0-9、底線、連字號",
  "name": "顯示名稱",
  "aliases": ["別名"],
  "sections": {"head": ["…"], "body": ["…"], "upper": [], "lower": [], "accessory": []},
  "outfits": {"default": {"top": ["…"], "bottom": ["…"], "set": [], "notes": "中文：什麼時候穿"}},
  "summary": "中文一到兩句：這個人是誰、在故事裡做什麼（給人看的）",
  "invented": ["補上的、原文沒寫的項目"],
  "conflicts": ["設定與原文衝突的項目"]
}"""

EXAMPLE_CHARACTER = {
    "id": "jie", "name": "阿杰", "aliases": [],
    "sections": {
        "head": ["A 22-year-old East Asian man", "short, unruly black hair", "a square jaw", "thick eyebrows"],
        "body": ["a lean, athletic build"],
        "upper": [], "lower": [], "accessory": [],
    },
    "outfits": {"default": {"top": ["a faded gray hooded sweatshirt", "a white T-shirt"], "bottom": ["dark jeans", "worn white sneakers"], "set": [], "notes": "日常穿著（第 1 章起）"}},
    "summary": "愛逞強的大學生，常忘了帶傘。",
    "invented": ["年齡與種族（原文沒寫，依姓名補）", "方下巴與粗眉（為了好辨識而補）"], "conflicts": []}

LOCATION_RULES = """# 任務：地點卡
依下面整理好的證據，寫出這個地點的視覺聖經卡片。

## 資料的優先順序
同角色卡：原文證據 → 作者設定檔 → 衝突以原文為準並列在 conflicts；必要的視覺要素（空間形狀、材質、光線、主要設施）證據沒有時，用平實的預設補上並列在 invented。

## look_en（固定場景描述）的規則
- 一句英文（約 25–60 字）：空間的形狀與大小、牆面地面材質、光線與色調、最重要的 2–3 個設施或陳設。
- 例：A narrow covered shopping arcade with a glass roof, rows of small shuttered shops on both sides, a worn tile floor and hanging paper lanterns
- **不要有人**（%(people)s 這類字會被擋下）：人物由角色卡提供，這樣同一個地點才能放進任何人數的格子。
- 不寫事件、不寫動作，只寫「這個地方長什麼樣子」。

## 輸出格式（只輸出這個 JSON）
{
  "id": "小寫英文 id（a-z、0-9、底線、連字號），例如 shelter_level",
  "name": "顯示名稱",
  "aliases": ["別名"],
  "look_en": "英文固定場景描述",
  "summary": "中文一句：這個地方是什麼、故事裡在這裡發生什麼",
  "invented": ["補上的、原文沒寫的項目"],
  "conflicts": ["設定與原文衝突的項目"]
}""" % {"people": _words(bc.PEOPLE_WORDS[:10])}

EXAMPLE_LOCATION = {
    "id": "arcade", "name": "拱廊", "aliases": [],
    "look_en": "A narrow covered shopping arcade with a glass roof, rows of small shuttered shops on both sides, a worn tile floor and hanging paper lanterns",
    "summary": "兩人每天碰面的老商店街。", "invented": ["玻璃屋頂與紙燈籠（原文只說是拱廊，為了有辨識度而補）"], "conflicts": []}


def _evidence_block(entity, is_character):
    lines = ["## 這個%s的證據（掃描整章後合併）" % ("角色" if is_character else "地點"),
             "名字：%s" % entity["name"]]
    if entity.get("aliases"):
        lines.append("別名：" + "、".join(entity["aliases"]))
    chapters = entity.get("chapters") or []
    lines.append("出場章節：" + "、".join(str(c) for c in chapters) + "（共 %d 章）" % len(chapters))
    if entity.get("roles"):
        lines.append("各章作用：")
        lines += ["  - 第 %s 章：%s" % (r["chapter"], r["role"]) for r in entity["roles"] if r.get("role")][:12]
    lines.append("外觀線索（looks）：" if entity.get("looks") else "外觀線索（looks）：（原文沒有任何明寫的外觀）")
    lines += ["  - 第 %s 章｜%s｜原文：「%s」" % (x["chapter"], x["en"], x["quote"]) for x in entity.get("looks") or []]
    if is_character:
        lines.append("服裝線索（outfits）：" if entity.get("outfits") else "服裝線索（outfits）：（原文沒有明寫的服裝）")
        lines += ["  - 第 %s 章｜%s｜%s｜原文：「%s」" % (x["chapter"], x["label"], x["en"], x["quote"]) for x in entity.get("outfits") or []]
    return "\n".join(lines)


def card_messages(kind, entity, notes_block="", taken_ids=()):
    is_character = kind == "character"
    rules = CHARACTER_RULES if is_character else LOCATION_RULES
    example = EXAMPLE_CHARACTER if is_character else EXAMPLE_LOCATION
    parts = [rules, "", "## 格式範例（人物與場景是虛構的，只示範寫法；範例的 id 不要用在這個專案）",
             json.dumps(example, ensure_ascii=False, indent=2), ""]
    if taken_ids:
        parts += ["## 已經被用掉的 id（不能重複）", "、".join(sorted(taken_ids)), ""]
    parts += ["---", _evidence_block(entity, is_character)]
    if notes_block:
        parts += ["", notes_block]
    parts += ["", "請輸出這個%s的卡片 JSON。" % ("角色" if is_character else "地點")]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(parts)}]


def notes_block(notes):
    """notes：[(標籤, 文字)] → 給模型看的「作者設定」區塊。沒有就回傳空字串。"""
    if not notes:
        return ""
    lines = ["## 作者的設定檔（可能含給別的繪圖模型用的英文標籤；只當成「作者想要的外觀」參考，和原文衝突時以原文為準）"]
    for label, text in notes:
        lines += ["<<<設定：%s" % label, text.strip(), "設定結束>>>"]
    return "\n".join(lines)
