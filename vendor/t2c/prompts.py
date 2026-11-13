"""文字端三階段的提示詞（S5）：改編規劃 → 漫畫劇本 → 分鏡 JSON。

設計原則（細節見 docs/04_提示詞設計.md）：

1. **規則放在程式裡，不靠模型記得**：外觀、座標換算、id、格子編號都由程式處理或驗證；提示詞只要求模型做「需要理解故事」的事。
2. **每階段只看需要的東西**：規劃看整章原著；劇本逐頁產生（看整章原著＋本頁規劃＋上一頁劇本）；分鏡逐頁產生，
   只看**劇本**（不看原著），所以人改過劇本再重跑分鏡，結果會跟著改。
3. **可快取的前綴放前面**：同一階段裡每次呼叫的開頭（規則、聖經、原著）完全相同，只有最後的「本頁任務」不同，
   DeepSeek 與 llama.cpp 的前綴快取才會生效。所以每次呼叫只有 system＋一則 user，不拆成多則 user。
4. **範例角色是虛構的**（阿杰、小晴），以免和專案的角色 id 混在一起。
5. 提示詞用繁體中文；要生圖的欄位（scene、pose…）要求英文。

只用標準函式庫，不依賴 FastAPI／fekit。
"""
import json

from . import layout, look, styles
from .compile import ANGLE, BUBBLE_KINDS, SHOT
from .project import read_front_matter
from .script import ANGLE_WORDS, MARKS, MAX_BUBBLES, OFFSCREEN_MARKS, RADIO_MARKS, SHOT_WORDS

SYSTEM = (
    "你是日本漫畫的責任編輯兼分鏡師，負責把小說改編成{look}（由右往左閱讀）。"
    "你的輸出會被程式直接讀取，所以格式必須完全照指示；不要加前言、說明、結語，也不要用 ``` 圍欄。"
    "用繁體中文（台灣用語）；指定要寫英文的欄位才寫英文。"
)


def system(look=None):
    """組好的 system 提示詞；look 省略時用預設畫風（mono）。"""
    if look is None:
        look = styles.resolve()["look"]
    return SYSTEM.format(look=look)


def _look(project):
    """這個專案的畫風描述（styles.resolve 的 look）。"""
    return styles.resolve(project.config)["look"]


# ---------------------------------------------------------------------------
# 共用區塊
# ---------------------------------------------------------------------------
def _clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def bible_block(project, with_body=True, with_look=True):
    """視覺聖經摘要。with_look：附上 look_en（只供辨識性別年齡，不准複述）。"""
    lines = ["## 角色（只能用這些 id）"]
    for cid, card in project.characters.items():
        parts = ["id=%s" % cid, "姓名：%s" % card.get("name", cid)]
        if card.get("aliases"):
            parts.append("別名：" + "、".join(str(a) for a in card["aliases"]))
        outfits = list((card.get("outfits") or {}))
        if outfits:
            parts.append("服裝 id：" + "、".join(outfits))
        if with_look:
            parts.append("外觀（只供辨識性別年齡，禁止在 scene／pose 複述）：" + _clip(look.look_en(card), 160))
        lines.append("- " + "｜".join(parts))
        if with_body:
            body = card_summary(read_front_matter(card["_path"])[1]) if card.get("_path") else ""
            if body:
                lines.append("  設定：" + _clip(body, 200))
    if not project.characters:
        lines.append("（沒有角色卡）")
    lines += ["", "## 地點（只能用這些 id；沒有合適的就省略 location）"]
    for lid, card in project.locations.items():
        lines.append("- id=%s｜名稱：%s｜%s" % (lid, card.get("name", lid), _clip(look.look_en(card), 120)))
    if not project.locations:
        lines.append("（沒有地點卡）")
    return "\n".join(lines)


def card_summary(body):
    """卡片本文的第一段（M1 修正 L-5）：略過開頭的標題與清單，取到第一個空行或標題為止。
    S6 草稿後面的「## 待確認」「## 原文依據」是給人看的，不進提示詞。"""
    para = []
    for line in (body or "").split("\n"):
        text = line.strip()
        if text.startswith("#") or (not text and para):
            if para:
                break
            continue
        if not text or text.startswith(("-", "*", ">")):
            if para:
                break
            continue
        para.append(text)
    return " ".join(para)


def _position(box):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    x = "右" if cx > 0.6 else "左" if cx < 0.4 else "中"
    y = "上" if cy < 0.4 else "下" if cy > 0.6 else "中"
    return y + x if x != "中" or y != "中" else "中央"


def _shape(aspect):
    if aspect >= 1.5:
        return "橫長"
    if aspect >= 1.15:
        return "略橫"
    if aspect >= 0.87:
        return "近方"
    if aspect >= 0.6:
        return "略直"
    return "直長"


def panel_shapes(pages_data, key):
    """某個模板每一格的（位置, 形狀）。"""
    page = pages_data["page"]
    left, top, right, bottom = layout.inner_rect(page)
    w, h = right - left, bottom - top
    out = []
    for box in pages_data["templates"][str(key)]["panels"]:
        out.append((_position(box), _shape((box[2] - box[0]) * w / ((box[3] - box[1]) * h))))
    return out


def template_catalog(pages_data):
    """模板清單（key、名稱、每格的位置與形狀），格子編號就是日式閱讀順序。"""
    lines = []
    for key, tpl in pages_data["templates"].items():
        shapes = panel_shapes(pages_data, key)
        cells = "；".join("%d 格 %s·%s" % (i, p, s) for i, (p, s) in enumerate(shapes, 1))
        lines.append("- 模板 %s「%s」共 %d 格：%s" % (key, tpl.get("name", ""), len(tpl["panels"]), cells))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ⓪ 切對話（N3）：小說 → 場 → 旁白／對話／演出，每一句帶 source_para
# ---------------------------------------------------------------------------
EXAMPLE_ANNOTATE = """{"scenes": [
  {"title": "屋簷下的雨", "location": "street", "setting_en": "Heavy rain pours beyond a shop awning at dusk; the pavement is soaked and the street is empty.",
   "cast": [{"id": "jie", "expression": "drenched but cheerful"}, {"id": "qing", "expression": "surprised"}],
   "ops": [{"k": "N", "p": 1},
           {"k": "x", "shot": "medium", "angle": "eye", "note": "A young man holds out a folded umbrella to a young woman under the awning."},
           {"k": "d", "p": 2, "s": "jie", "t": "「拿去，我跑一下就到了。」"},
           {"k": "n", "p": 3, "t": "小晴低頭看著手裡的傘。"},
           {"k": "d", "p": 3, "s": "qing", "m": "心聲", "t": "「……這個笨蛋。」"}]}
]}"""

ANNOTATE_RULES = """# 任務：切對話
把下面這一章小說拆成一場一場，每一場照原著順序寫出旁白、對話與演出。這一份是後面分鏡唯一的台詞來源，
所以台詞與旁白**只能照抄原著**：程式會逐字比對，抄錯就一直退回重寫。

## 什麼是一場
同一個地點、同一段時間、同一批人。地點、時間或焦點換了就是新的一場；一場最多涵蓋 35 個段落。

## 輸出格式（只輸出這個 JSON）
{"scenes": [
  {"title": "這一場的短標題（中文）",
   "location": "地點 id（見下表；沒有合適的就省略）",
   "setting_en": "英文一到兩句：這一場看得見的畫面（時間、光線、空間、天氣）",
   "cast": [{"id": "角色id", "expression": "英文表情"}],
   "ops": [這一場依原著順序的指令]}
]}

## 四種 op
- {"k": "N", "p": 3}：整段旁白，把第 3 段原文照抄成旁白。**段落裡有引號台詞時不能用 N。**
- {"k": "n", "p": 3, "t": "原文的一小段"}：段落裡的一段敘述，t 是該段原文的子句。
- {"k": "d", "p": 3, "s": "角色id", "t": "「台詞。」", "m": "心聲"}：一句對話，t 逐字照抄（含引號）；m 省略就是普通說話。
- {"k": "x", "shot": "close", "angle": "eye", "note": "英文：這一格的重點"}：演出，只影響畫面，不算台詞。

## 規則
1. **逐字**：n 與 d 的 t 只能從 p 那一段複製（可以只取一部分、可以拆成幾句），不能改寫、不能漏字、不能自己編。
2. **每一段都要用到**：p 是段落號（1–N），照原著順序，不能跳段、不能倒退。一段可以拆成多個 op（引號台詞用 d，引號外的敘述用 n）。
3. **旁白不要吞掉台詞**：段落裡有「」台詞就一定要用 d；引號外的敘述再用 n。
4. **角色**：cast 與 s 只能用下面聖經的 id。原著有、聖經沒有的名字不要放進 cast：那句改成旁白，或改由在場的人說。
5. 說話的人要列在該場 cast；程式會補，但請一律寫清楚，expression 寫這一場的表情。
6. **m（括號標記）**只能用：@MARKS@。要併寫就用「、」（例：畫面外、喊）。透過通訊器、電話、廣播的聲音標通訊，不用再加語氣；心聲不能標畫面外。
7. **英文欄位**：setting_en、expression、note 一律英文（會進生圖提示詞）。台詞、旁白、標題保持繁體中文原文。不要寫角色的長相、髮型、衣著（那是視覺聖經的事），只寫畫面、光線、動作與情緒。
8. **演出 x** 決定接下來幾格怎麼拍：shot 用 @SHOTS@；angle 用 @ANGLES@。重要情緒用 close 或 extreme_close，大場面用 wide，換場時可以換角度。
9. 不要加前言、不要用三個反引號的圍欄、不要加註解；只輸出 JSON。""".replace(
    "@SHOTS@", "、".join(SHOT)).replace("@ANGLES@", "、".join(ANGLE)).replace(
    "@MARKS@", "、".join(["心聲", "喊", "喊叫", "小聲", "悄聲"] + list(RADIO_MARKS) + list(OFFSCREEN_MARKS)))


def annotate_messages(project, paragraphs, title_hint=""):
    """切對話：整章一次產生。段落編號（〔n〕）就是 source_para 的 p。"""
    body = "\n\n".join("〔%d〕%s" % (index, text) for index, text in enumerate(paragraphs or [], 1))
    parts = [ANNOTATE_RULES, "", "## 格式範例（人物與場景是虛構的，只示範寫法）", EXAMPLE_ANNOTATE, "",
             bible_block(project, with_body=False, with_look=False), ""]
    if title_hint:
        parts += ["## 章名", title_hint, ""]
    parts += ["## 本章原著（〔n〕就是 op 的 p，共 %d 段）" % len(paragraphs or []),
              "<<<原著開始", body, "原著結束>>>"]
    return [{"role": "system", "content": system(_look(project))}, {"role": "user", "content": "\n".join(parts)}]


KIND_MARKS = {"thought": "心聲", "shout": "喊", "whisper": "小聲", "radio": "通訊"}


def scenes_ledger(scene_doc):
    """切對話的逐字帳本：給劇本階段照抄台詞用（台詞只有一個來源）。"""
    lines = []
    for index, scene in enumerate(scene_doc.get("scenes") or [], 1):
        head = "第 %d 場" % index
        if scene.get("title"):
            head += "　%s" % scene["title"]
        if scene.get("location"):
            head += "（%s）" % scene["location"]
        lines.append(head)
        for line in scene.get("lines") or []:
            if line.get("type") == "narration":
                lines.append("- 旁白：%s〔p%s〕" % (line.get("text"), line.get("source_para")))
            elif line.get("type") == "dialogue":
                marks = [KIND_MARKS[line["kind"]]] if line.get("kind") in KIND_MARKS else []
                if line.get("offscreen"):
                    marks.append("畫面外")
                mark = "（%s）" % "、".join(marks) if marks else ""
                lines.append("- %s%s：%s〔p%s〕" % (line.get("speaker"), mark, line.get("text"), line.get("source_para")))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ① 改編規劃
# ---------------------------------------------------------------------------
_PLAN_RULES = """# 任務：改編規劃
把下面這一章小說規劃成%(look)s。這一步只決定「分幾頁、每頁用哪個模板、每格演什麼」，不寫台詞。

## 改編原則
1. **忠於原著**：不新增原著沒有的情節、角色、設定；因果關係與關鍵台詞要保留。可以刪、可以併，不可以改劇情。
2. **漫畫靠畫面說故事**：原著的心理描寫、環境描寫，能變成表情、動作、場景的就畫出來；必要時才用旁白或心聲。
3. **一頁一個小節奏**：每頁有它的作用（建立場景／衝突升高／轉折／收尾），頁末最好留一個讓人想翻頁的點（懸念、轉折、情緒停頓）。
4. **格數看節奏**：對話密集、情緒細膩的頁用 4–6 格；大場面、重要轉折用 1–3 格；不要整章都用同一個模板。
5. **一格只放一個瞬間**：一格裡的動作與台詞要能在同一個畫面裡成立，台詞每格最多 %(max)d 個對話框。
6. **角色只能用聖經裡的 id**。原著出現、但聖經沒有角色卡的人物，不要放進格子，把名字列在 unlisted_characters。
7. 被你刪掉或大幅濃縮的原著內容，簡短列在 omitted（一句話寫明刪了什麼、為什麼），方便人工檢查。

## 輸出格式（只輸出這個 JSON）
{
  "title": "本章標題（沿用原著章名；沒有就取一個）",
  "summary": "一句話：這一章發生什麼",
  "pages": [
    {
      "page": 1,
      "template": "模板 key（見下面的清單）",
      "purpose": "這一頁在章裡的作用，一句話",
      "hook": "頁末的翻頁點，一句話；沒有就留空字串",
      "panels": [
        {"slot": 1, "beat": "這一格發生什麼（中文，一到兩句，只寫看得見的事）", "source": "原著依據：引原文一小段（20 字內）", "characters": ["角色id"], "location": "地點id 或省略"}
      ]
    }
  ],
  "omitted": ["刪掉的內容與理由"],
  "unlisted_characters": ["原著有、聖經沒有的人物名字"]
}
pages 依閱讀順序，page 從 1 開始連號；每頁 panels 的 slot 要剛好是 1 到該模板的格數。"""


def plan_rules(look):
    """改編規劃的規則；畫風描述跟著 look 走。"""
    return _PLAN_RULES % {"look": look, "max": MAX_BUBBLES}


def plan_messages(project, source, pages_data, want_pages=None, title_hint=""):
    if want_pages:
        size = "請規劃成剛好 **%d 頁**。" % want_pages
    else:
        size = "頁數由你決定：大約每 400–600 字原著一頁；對話密集或動作場面可以多給幾頁，純敘述可以濃縮。"
    parts = [plan_rules(_look(project)), "", "## 頁面模板（格子編號就是閱讀順序：右上先、同排由右到左、再往下一排）", template_catalog(pages_data), "",
             bible_block(project, with_body=True, with_look=False), "", "## 篇幅", size]
    if title_hint:
        parts += ["", "章名：" + title_hint]
    parts += ["", "## 本章原著", "<<<原著開始", source.strip(), "原著結束>>>"]
    return [{"role": "system", "content": system(_look(project))}, {"role": "user", "content": "\n".join(parts)}]


# ---------------------------------------------------------------------------
# ② 漫畫劇本
# ---------------------------------------------------------------------------
EXAMPLE_SCRIPT = """## 第 3 頁（模板 2h：上下兩格）

**1 格**｜中景、平視｜屋簷下大雨，阿杰把折好的傘遞給站在屋簷下的小晴，自己半邊肩膀已經淋濕。
- 阿杰：拿去，我跑一下就到了。

**2 格**｜特寫｜小晴低頭看著手裡的傘，握緊傘柄；阿杰已經跑出畫面。
- 阿杰（畫面外、喊）：明天見！
- 小晴（心聲）：……這個笨蛋。
- 狀聲詞：嘩啦嘩啦"""

SCRIPT_RULES = """# 任務：漫畫劇本（一次寫一頁）
依「改編規劃」把指定的那一頁寫成漫畫劇本。劇本是給人讀、給人改的，也是下一步分鏡的唯一依據，所以要具體、能直接畫。

## 格式（只輸出這一頁，從「## 第 N 頁」那一行開始）
```
## 第 N 頁（模板 K：模板名稱）

**1 格**｜鏡頭、角度｜畫面描述（中文，只寫畫面裡看得見的東西）。
- 旁白：……
- 角色名：台詞
- 角色名（心聲）：……
- 狀聲詞：碰！
```
- 鏡頭詞從這些選一個：%(shots)s。角度詞可省略（預設平視），可用：%(angles)s。例：`遠景、俯視`。
- 沒有台詞的格寫 `- （無台詞）`。
- 台詞的語氣只能是這些括號標記：%(marks)s；一般說話不加括號。
- **說話的人不在這一格的畫面裡**時要標出來：透過通訊器、電話、廣播傳來的聲音標 %(radio)s（不能再加語氣）；人在附近、只是這一格沒畫到他，標（畫面外），要加語氣就寫成（畫面外、喊）。例：`- 阿杰（通訊）：收到。`
- 角色名用聖經的姓名（或別名）。**每一格最多 %(max)d 個對話框**（旁白也算；狀聲詞不算）。

## 寫作原則
1. **台詞盡量沿用原著原句**：可以刪減、可以拆成兩個對話框，但不要改寫語氣、不要自己編新台詞。原著沒有說出口的內容，用表情、動作、畫面表現。
2. **台詞要短**：一個對話框最好 20 字內；長句拆開、或拆到下一格。
3. **旁白只用在**：時間地點、轉場、原著裡真的需要文字交代的資訊；不要重複畫面已經看得到的事。
4. **畫面描述只寫看得見的**：場景、人物動作、表情、重要物件、光線。不要寫角色的外觀（髮型、衣服、長相——那由視覺聖經負責）。每格一個瞬間。
5. **格內角色的左右關係寫清楚**（例如「阿杰在左、小晴在右」），分鏡會依這個擺位置。隔著通訊對話時，每一格只畫其中一邊的人，另一邊的台詞標（通訊）；不要把不在同一個地方的人畫進同一格。
6. 照規劃的 beat 演，不要多演、少演；同一頁的鏡頭要有變化（遠近交替），重要的情緒用近景或特寫。
7. 格子的位置與形狀（橫長、直長…）已列在本頁任務裡，鏡頭與構圖要配合：橫長格適合遠景、並排的人；直長格適合單人、仰視或俯視。""" % {
    "shots": "、".join(SHOT_WORDS), "angles": "、".join(ANGLE_WORDS),
    "marks": "、".join("（%s）" % m for m in MARKS), "max": MAX_BUBBLES,
    "radio": "／".join("（%s）" % m for m in RADIO_MARKS)}


def _plan_page_text(plan_page, shapes):
    lines = ["第 %s 頁｜模板 %s｜作用：%s%s" % (plan_page["page"], plan_page["template"], plan_page.get("purpose", ""),
                                          ("｜頁末翻頁點：" + plan_page["hook"]) if plan_page.get("hook") else "")]
    for p in plan_page["panels"]:
        pos, shape = shapes[p["slot"] - 1] if 0 < p["slot"] <= len(shapes) else ("?", "?")
        who = "、".join(p.get("characters") or []) or "無人"
        lines.append("- %d 格（%s·%s）：%s　〔原著：%s｜角色：%s%s〕" % (
            p["slot"], pos, shape, p.get("beat", ""), p.get("source", ""), who,
            ("｜地點：" + p["location"]) if p.get("location") else ""))
    return "\n".join(lines)


def script_messages(project, source, plan, plan_page, pages_data, prev_script="", scenes=None):
    """scenes：切對話的結果（scenes/chNNN.json）。有給就附上逐字台詞帳本，台詞只從帳本挑。"""
    shapes = panel_shapes(pages_data, plan_page["template"])
    tpl_name = pages_data["templates"][str(plan_page["template"])].get("name", "")
    parts = [SCRIPT_RULES, "", "## 格式範例（人物與場景是虛構的，只示範寫法）", "```", EXAMPLE_SCRIPT, "```", "",
             bible_block(project, with_body=True, with_look=False), "",
             "## 章：%s" % (plan.get("title") or ""), "全章共 %d 頁。本章概要：%s" % (len(plan["pages"]), plan.get("summary", "")), "",
             "## 本章原著", "<<<原著開始", source.strip(), "原著結束>>>"]
    if scenes:
        parts += ["", "## 切對話帳本（台詞與旁白只能從這裡逐字照抄，順序照帳本；〔pN〕是原著段落號）",
                  scenes_ledger(scenes)]
    task = ["", "---", "## 本頁任務", _plan_page_text(plan_page, shapes), ""]
    if prev_script.strip():
        task += ["上一頁的劇本（接續它，不要重複）：", prev_script.strip(), ""]
    task += ["請寫第 %s 頁的劇本。標題行請寫成：## 第 %s 頁（模板 %s：%s）" % (plan_page["page"], plan_page["page"], plan_page["template"], tpl_name)]
    return [{"role": "system", "content": system(_look(project))}, {"role": "user", "content": "\n".join(parts + task)}]


# ---------------------------------------------------------------------------
# ③ 分鏡 JSON
# ---------------------------------------------------------------------------
EXAMPLE_STORYBOARD = {"panels": [
    {"slot": 1, "shot": "medium", "angle": "eye",
     "scene": "Heavy rain falls beyond a shop awning. A young man holds out a folded umbrella to a young woman standing under the awning; one of his shoulders is soaked.",
     "characters": [
         {"id": "jie", "pose": "Holding out a folded umbrella with one arm, rain falling on his shoulder", "expression": "Reassuring smile", "box": [0.08, 0.25, 0.5, 1.0]},
         {"id": "qing", "pose": "Standing under the awning with her hands in front of her", "expression": "Surprised", "box": [0.55, 0.2, 0.95, 1.0]}],
     "bubbles": [{"kind": "speech", "speaker": "jie", "text": "拿去，\n我跑一下就到了。", "anchor": [0.3, 0.1]}]},
    {"slot": 2, "shot": "extreme_close", "angle": "eye",
     "scene": "Close-up of the young woman looking down at the umbrella in her hands, her fingers tightening around the handle.",
     "characters": [{"id": "qing", "pose": "Looking down at an umbrella held in both hands", "expression": "Touched, trying to hide a smile", "box": [0.15, 0.2, 0.75, 1.0]}],
     "objects": [{"caption": "A folded umbrella gripped in both hands", "box": [0.2, 0.65, 0.7, 1.0]}],
     "sfx": [{"text": "嘩啦嘩啦", "box": [0.02, 0.02, 0.3, 0.45]}],
     "bubbles": [{"kind": "shout", "speaker": "jie", "offscreen": True, "text": "明天見！", "anchor": [0.86, 0.12]},
                 {"kind": "thought", "speaker": "qing", "text": "……這個笨蛋。", "anchor": [0.86, 0.55]}]}]}

def example_json():
    """範例分鏡：一格一行，比 indent 排版省 token。"""
    rows = ",\n  ".join(json.dumps(p, ensure_ascii=False) for p in EXAMPLE_STORYBOARD["panels"])
    return '{"panels": [\n  %s\n]}' % rows


STORYBOARD_RULES = """# 任務：分鏡 JSON（一次一頁）
把指定那一頁的**劇本**轉成分鏡。程式會再把它編譯成每一格的生圖提示詞並嵌字，所以欄位與規則必須完全照下面來。
台詞、旁白、狀聲詞以劇本為準——你的工作是決定構圖：鏡頭、角色站位、物件、對話框位置。

## 輸出格式（只輸出這個 JSON；panels 的順序與劇本一致）
{"panels": [ { 一格 }, … ]}

一格：
{
  "slot": 1,                       // 格編號，和劇本一致
  "shot": "%(shots)s",
  "angle": "%(angles)s",
  "location": "地點 id（可省略）",
  "scene": "英文一到兩句：這一格看得見的畫面",
  "characters": [ {"id": "角色id", "outfit": "服裝id（可省略＝default）", "pose": "英文姿勢", "expression": "英文表情", "box": [x0, y0, x1, y1]} ],
  "objects": [ {"caption": "英文：重要物件", "box": [x0, y0, x1, y1]} ],
  "sfx": [ {"text": "狀聲詞原文", "box": [x0, y0, x1, y1]} ],
  "bubbles": [ {"kind": "%(kinds)s", "speaker": "角色id（旁白省略）", "text": "台詞", "anchor": [x, y], "offscreen": true（只有說話的人不在畫面上時才寫）} ]
}

## 規則
1. **英文欄位**：scene、pose、expression、objects.caption 一律英文；台詞與狀聲詞保持原文（繁體中文）。
2. **不寫角色外觀**：髮型、衣服、眼鏡、年齡、長相由程式從視覺聖經貼上。scene 裡人物用代稱（the young woman、the young man、the girl、the boy…），pose／expression 只寫動作與情緒。
3. **一格一張圖**：scene 只描述這一格；characters 只列這一格畫面裡真的看得到的角色，每個角色只出現一次，不要在 scene 裡提到沒列在 characters 的人。沒有人的空鏡 characters 給 []。
4. **說話的人在畫面上，就必須列在 characters 裡。不在畫面上的人不要列進 characters**（列了就會被畫出來）：劇本標（通訊）（電話）等的台詞用 kind "radio" 並加 "offscreen": true；標（畫面外）的台詞照語氣選 speech／shout／whisper 並加 "offscreen": true。offscreen 的 speaker 仍然寫角色 id。心聲（thought）不能 offscreen。
5. **座標一律是 0–1 的比例** [x0, y0, x1, y1]（左上 0,0、右下 1,1）、相對於這一格的圖；x0<x1、y0<y1。不准寫像素。
6. **角色框**：單人全身／中景大約 [0.25, 0.1, 0.75, 1.0]；近景、特寫的框要大，框頂靠近上緣；遠景的人要小。**兩人以上必須左右分開**（左邊的人框中心 x<0.4、右邊的 x>0.6），框不要互相重疊。劇本寫了誰在左、誰在右就照辦。若 angle 用 over_shoulder，背對鏡頭的角色框放在左邊。
7. **對話框 bubbles** 依閱讀順序（日漫：先右後左、先上後下）排列。text 逐字照劇本（見規則 8）；kind 由劇本決定：（心聲）→ thought、（喊）→ shout、（小聲）→ whisper、（通訊）（電話）（廣播）（無線電）→ radio、旁白 → narration（不寫 speaker）；沒標語氣的一般台詞用 speech，但明顯是大喊或耳語時可以改用 shout／whisper。
8. **台詞逐字照劇本，不增不減不改**，唯一能做的是加換行 "\\n"。直排嵌字沒有中文斷詞，所以**每一行請控制在 8–10 個字內**，在詞與詞之間（或句讀後）斷開，例如 "對不起！\\n排隊排到一半\\n被教授抓去搬東西！"；短句不用換行。
9. **anchor 是對話框中心的希望位置**：放在畫面裡沒有重要東西的空白處（常見是上緣兩個角落），**不要放在角色的臉上**（臉大約在角色框的上 1/4）。先讀的對話框放右上，後讀的往左、往下排；多個對話框的位置要錯開。
10. **狀聲詞 sfx** 逐字照劇本；box 放在動作發生的地方旁邊，不要壓住角色的臉。沒有就省略。
11. 鏡頭與角度對照劇本的鏡頭詞：%(shot_map)s；%(angle_map)s。可依畫面微調。
12. 同一個角色在連續幾格的服裝要一致；沒有特別說明就用 default（省略 outfit 即可）。
13. 只輸出 JSON，不要註解、不要加說明。""" % {
    "shots": "|".join(SHOT), "angles": "|".join(ANGLE), "kinds": "|".join(sorted(BUBBLE_KINDS)),
    "shot_map": "、".join("%s→%s" % kv for kv in SHOT_WORDS.items()),
    "angle_map": "、".join("%s→%s" % kv for kv in ANGLE_WORDS.items())}


def storyboard_messages(project, script_page, page_no, template_key, pages_data, prev_scenes=""):
    """script_page：這一頁劇本的原始文字（人改過的也行）。prev_scenes：上一頁各格的 scene 摘要，維持場景連續。"""
    shapes = panel_shapes(pages_data, template_key)
    tpl = pages_data["templates"][str(template_key)]
    parts = [STORYBOARD_RULES, "", "## 格式範例（人物與場景是虛構的，只示範寫法；範例的角色 id 不要用在這個專案）",
             "劇本：", "```", EXAMPLE_SCRIPT, "```", "分鏡：", "```", example_json(), "```", "",
             bible_block(project, with_body=False, with_look=True)]
    task = ["", "---", "## 本頁任務",
            "第 %s 頁，模板 %s「%s」，共 %d 格。各格的位置與形狀（構圖要配合）：" % (page_no, template_key, tpl.get("name", ""), len(tpl["panels"])),
            "；".join("%d 格 %s·%s" % (i, p, s) for i, (p, s) in enumerate(shapes, 1)), ""]
    if prev_scenes.strip():
        task += ["上一頁的畫面摘要（場景與角色位置要接得上）：", prev_scenes.strip(), ""]
    task += ["本頁劇本：", "```", script_page.strip(), "```", "", "請輸出本頁的分鏡 JSON（只有 {\"panels\": [...]}，共 %d 格）。" % len(tpl["panels"])]
    return [{"role": "system", "content": system(_look(project))}, {"role": "user", "content": "\n".join(parts + task)}]


# ---------------------------------------------------------------------------
# 回修
# ---------------------------------------------------------------------------
def repair_message(issues, as_json=True):
    """驗證沒過：把全部錯誤列給模型（附在它上一次的輸出之後）。"""
    items = "\n".join("%d. %s" % (i, issue) for i, issue in enumerate(issues, 1))
    tail = "請只修正這些問題，其餘照舊，輸出修正後的**完整** %s，不要加說明。" % ("JSON" if as_json else "內容（同樣從「## 第 N 頁」開始）")
    return {"role": "user", "content": "你上一次的輸出沒有通過檢查：\n%s\n\n%s" % (items, tail)}
