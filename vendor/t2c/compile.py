"""分鏡 JSON → 每一格的 MOSS-Image layout prompt。

設計重點（依據見 docs/01_架構與流程.md）：
1. 一格一張圖。整頁一次生成時角色容易跑錯格，解析度也被壓縮。
2. 角色外觀只從視覺聖經取，**逐字**貼進該格；LLM 寫分鏡時只寫角色 id，不寫外觀。
3. 每格的 summary 只描述這一格，並寫明這格有幾個人；不在場的角色完全不出現。
4. 台詞、旁白預設不交給 MOSS 畫（之後由嵌字程式疊上）；狀聲詞可選擇交給 MOSS（sfx_mode = "moss"）。
5. 比例座標只在這裡換成 MOSS 的 bbox（見 layout.to_moss_bbox）。
"""
import hashlib
import json
import re

from . import cast, layout, look, styles

SHOT = {
    "extreme_wide": "extreme wide establishing shot",
    "wide": "wide shot",
    "full": "full body shot",
    "medium": "medium shot",
    "close": "close-up shot",
    "extreme_close": "extreme close-up shot",
}
ANGLE = {
    "eye": "eye-level view",
    "high": "high-angle view looking down",
    "low": "low-angle view looking up",
    "bird": "bird's-eye view from directly above",
    "dutch": "tilted dutch angle",
    "over_shoulder": "over-the-shoulder view",
}
BUBBLE_KINDS = {"speech", "thought", "shout", "whisper", "radio", "narration"}
OFFSCREEN_KINDS = {"speech", "shout", "whisper", "radio"}   # 可以 offscreen 的種類（M1 修正）
SFX_MODES = {"overlay", "moss"}
NUMBERS = {1: "one person", 2: "two people", 3: "three people", 4: "four people", 5: "five people"}


class CompileError(Exception):
    """給人看的錯誤訊息（會列出是哪一頁哪一格）。"""


def _sentence(text):
    text = " ".join(str(text or "").split())
    if text and text[-1] not in ".!?":
        text += "."
    return text[:1].upper() + text[1:]


def _digest(text):
    return hashlib.sha1(" ".join(str(text or "").split()).encode("utf-8")).hexdigest()[:12]


def _seed(panel_id, index, base=0):
    """同一格同一個候選編號永遠得到同一個 seed，重跑結果可重現。"""
    digest = hashlib.sha256(("%s#%d" % (panel_id, index)).encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) + int(base)) % 2_147_483_647


def cast_sentence(count):
    if count == 0:
        return "There are no people in this panel."
    who = NUMBERS.get(count, "%d people" % count)
    return "This panel shows exactly %s, and each person appears only once." % who


NO_FRAME = "The image has no border or frame."
# 臨時角色（D9）：沒有卡片時的通用外觀。不猜性別、髮型、年齡——猜錯比空泛更貴（S1 實測）。
EXTRA_LOOK = "An unnamed minor character with a plain generic appearance"
NO_LETTERING = "The artwork has no speech bubbles, captions or lettering."


def side_of_frame(box):
    """比例框的水平位置：left／center／right（以中心點判斷）。"""
    center = (box[0] + box[2]) / 2.0
    if center < 0.4:
        return "left"
    if center > 0.6:
        return "right"
    return "center"


def _subject(card):
    """從整段外觀判斷用 She／He／This character（S1 實測：多角色時要寫明左右，見 docs/qa/S1_出圖實測.md）。
    外觀可能是 sections（步驟 2 起）或舊的 look_en，一律走 look.look_en()。"""
    words = set(re.findall(r"[a-z]+", look.look_en(card).lower()))
    if words & {"woman", "girl", "female", "lady"}:
        return "She"
    if words & {"man", "boy", "male", "guy"}:
        return "He"
    return "This character"


def _side_text(side):
    return "in the center of the frame" if side == "center" else "on the %s side of the frame" % side


def side_sentence(card, side):
    return "%s is %s." % (_subject(card), _side_text(side))


def character_caption(card, entry, side=None, shot=None):
    """角色 item 的 caption：（多角色時的左右位置）＋這一格用得上的外觀段（逐字）＋服裝＋姿勢、表情。
    哪幾段由分鏡的 shot 決定（look.caption_parts）；舊卡片只有 look_en 時就是整段。"""
    parts = [side_sentence(card, side)] if side else []
    outfits = card.get("outfits") or {}
    outfit_id = entry.get("outfit") or "default"
    if outfit_id != "default" and outfit_id not in outfits:
        raise KeyError("服裝「%s」不在角色「%s」的 outfits 裡" % (outfit_id, card.get("id")))
    for part in look.caption_parts(card, shot, outfit_id):
        parts.append(_sentence(part["text"]))
    if entry.get("pose"):
        parts.append(_sentence(entry["pose"]))
    if entry.get("expression"):
        parts.append(_sentence("Facial expression: " + entry["expression"]))
    return " ".join(p for p in parts if p)


def bubble_error(project, bubble, cast):
    """對話框與這一格角色的一致性（不合就丟 KeyError）。cast：這一格 characters 的 id。
    說話者必須在這一格；`offscreen: true` 時反過來，必須是聖經角色、而且**不在**這一格（資料格式第 5 節）。"""
    kind = bubble.get("kind", "speech")
    if kind not in BUBBLE_KINDS:
        raise KeyError("對話框種類「%s」不認得（可用：%s）" % (kind, "、".join(sorted(BUBBLE_KINDS))))
    speaker, offscreen = bubble.get("speaker"), bubble.get("offscreen", False)
    if not isinstance(offscreen, bool):
        raise KeyError("offscreen 只能是 true 或 false")
    if offscreen:
        if kind not in OFFSCREEN_KINDS:
            raise KeyError("%s 的對話框不能 offscreen（可以的種類：%s）" % (kind, "、".join(sorted(OFFSCREEN_KINDS))))
        if not speaker:
            raise KeyError("offscreen 的對話框也要寫 speaker")
        if speaker not in project.characters:
            raise KeyError("說話的角色「%s」不在 bible/characters" % speaker)
        if speaker in cast:
            raise KeyError("「%s」標了 offscreen，但他列在這一格的 characters（會被畫出來）；畫面外就從 characters 拿掉" % speaker)
    elif speaker and speaker not in cast and kind != "narration":
        raise KeyError("說話的角色「%s」不在這一格（人在畫面外或透過通訊說話，對話框加 \"offscreen\": true）" % speaker)


def compile_panel(project, page, panel, template_box, settings):
    """回傳一格的生圖工作：{id, page, slot, width, height, layout, prompt, negative, seeds}。"""
    pid = panel.get("id") or "p%03d_k%d" % (page["page"], panel["slot"])
    where = "第 %s 頁第 %s 格（%s）" % (page["page"], panel.get("slot"), pid)
    try:
        page_cfg = settings["page"]
        rect = layout.panel_rect_px(page_cfg, template_box)
        aspect = (rect[2] - rect[0]) / float(rect[3] - rect[1])
        width, height = layout.gen_size(aspect, settings.get("megapixels", 1.0))

        chars = panel.get("characters") or []
        caption = [
            _sentence(settings["style"]),
            _sentence("%s, %s" % (SHOT.get(panel.get("shot"), SHOT["medium"]), ANGLE.get(panel.get("angle"), ANGLE["eye"]))),
        ]
        location = None
        if panel.get("location"):
            card = project.locations.get(panel["location"])
            if card is None:
                raise KeyError("地點「%s」不在 bible/locations" % panel["location"])
            location = card
            caption.append(_sentence("Setting: " + look.look_en(card)))
        caption.append(_sentence(panel.get("scene", "")))
        caption.append(cast_sentence(len(chars)))
        caption.append(NO_FRAME)
        caption.append(NO_LETTERING)

        items = []
        if location is not None:
            items.append({"label": "object", "caption": _sentence(look.look_en(location)) + " Background of the whole panel.",
                          "bbox": layout.to_moss_bbox([0, 0, 1, 1], width, height)})
        seen = set()
        shot = panel.get("shot")
        # 探索用（D2）＝待補的具名角色；臨時角色（D9）＝標了 extra 的龍套，可以出圖與選圖；
        # 帳單軌（D1）的指紋。這三件事以前混成一種，微5 把它們分開。
        explore, unresolved, fingerprints = [], [], {}
        for entry in chars:
            raw = entry.get("id")
            if raw in seen:
                raise KeyError("角色「%s」在同一格出現兩次" % raw)
            seen.add(raw)
            cid, grade, suggests = cast.grade(project, entry)
            if cid is not None:
                seen.add(cid)                    # 對話框的 speaker 寫的是卡片 id
            box = entry.get("box", [0.25, 0.1, 0.75, 1])
            side = side_of_frame(layout.check_box(box)) if len(chars) >= 2 else None
            if grade == "missing":               # 解析失敗又沒有旗標：照樣算人數，沒有外觀可貼（探索用）
                explore.append(raw)
                unresolved.append({"id": raw, "suggest": suggests})
                continue
            if grade == "extra":                 # 臨時角色：通用外觀，指紋記常數所以不會永遠算過期
                caption = [EXTRA_LOOK] + (["This character is " + _side_text(side)] if side else [])
                caption += [entry["pose"]] if entry.get("pose") else []
                caption += ["Facial expression: " + entry["expression"]] if entry.get("expression") else []
                fingerprints["extra:%s" % raw] = {"outfit": "extra", "parts": {"extra": ""}}
                items.append({"label": "character", "caption": " ".join(_sentence(c) for c in caption),
                              "bbox": layout.to_moss_bbox(box, width, height)})
                continue
            card = project.characters[cid]
            outfit_id = entry.get("outfit") or "default"
            fingerprints[cid] = {"outfit": outfit_id, "parts": look.fingerprint(card, shot, outfit_id)}
            items.append({"label": "character", "caption": character_caption(card, entry, side, shot),
                          "bbox": layout.to_moss_bbox(box, width, height)})
        for obj in panel.get("objects") or []:
            items.append({"label": "object", "caption": _sentence(obj["caption"]),
                          "bbox": layout.to_moss_bbox(obj["box"], width, height)})
        if settings.get("sfx_mode") == "moss":
            for sfx in panel.get("sfx") or []:
                items.append({"label": "text", "caption": settings.get("sfx") or styles.resolve()["sfx"],
                              "text": sfx["text"], "bbox": layout.to_moss_bbox(sfx["box"], width, height)})
        for bubble in panel.get("bubbles") or []:
            bubble_error(project, bubble, seen)
    except (KeyError, layout.LayoutError) as error:
        raise CompileError("%s：%s" % (where, error.args[0] if error.args else error))

    doc = {
        "summary": {"meta": list(settings.get("meta", ["manga panel"])), "caption": " ".join(c for c in caption if c)},
        "items": items,
        "bbox_coord_space": layout.MOSS_COORD_SPACE,
        "bbox_format": layout.MOSS_BBOX_FORMAT,
    }
    count = int(settings.get("candidates", 1))
    seeds = list(panel.get("seeds") or [])[:count]
    seeds += [_seed(pid, i, settings.get("seed_base", 0)) for i in range(len(seeds), count)]
    prompt = json.dumps(doc, ensure_ascii=False)
    return {
        "id": pid, "page": page["page"], "slot": panel["slot"],
        "rect_px": list(rect), "width": width, "height": height,
        "layout": doc, "prompt": prompt,
        "negative": settings.get("negative", ""), "seeds": seeds,
        "explore": sorted(explore),
        # 待補的具名角色與「你是不是要對應到 X」的建議（D11 只給建議，不自動套用）
        "unresolved": unresolved,
        # 帳單軌：這一格用了哪幾段、每段的指紋（卡片改了就算得出哪幾格過期）
        "fingerprint": {
            "style": _digest("%s|%s|%s" % (settings["style"], settings.get("meta"), settings.get("sfx"))),
            "shot": look.shot_key(shot),
            "characters": fingerprints,
            "location": (_digest(look.look_en(location)) if location is not None else ""),
            "prompt": _digest(prompt),
        },
    }


def compile_chapter(project, storyboard, pages_data=None, only_page=None, overrides=None):
    """整章（或 only_page 指定的那一頁）→ 生圖工作清單。"""
    pages_data = pages_data or project.pages()
    cfg = project.config
    look = styles.resolve(cfg)          # 畫風預設集（N5）：style／meta／sfx 由 project.yaml 的 style_preset 決定
    settings = {
        "page": pages_data["page"],
        "style": look["style"],
        "meta": look["meta"],
        "sfx": look["sfx"],
        "megapixels": cfg.get("megapixels", 1.0),
        "negative": cfg.get("negative", ""),
        "sfx_mode": cfg.get("sfx_mode", "overlay"),
        "candidates": cfg.get("candidates", 1),
        "seed_base": cfg.get("seed_base", 0),
    }
    settings.update(overrides or {})
    if settings["sfx_mode"] not in SFX_MODES:
        raise CompileError("sfx_mode 只能是 %s" % "、".join(sorted(SFX_MODES)))
    if not str(settings["style"]).strip():
        raise CompileError("project.yaml 缺少 style（整部作品共用的畫風描述）；畫風預設集「%s」也沒有 style" % look["preset"])

    jobs = []
    for page in storyboard.get("pages") or []:
        if only_page is not None and int(page["page"]) != int(only_page):
            continue
        tpl = pages_data["templates"].get(str(page.get("template")))
        if tpl is None:
            raise CompileError("第 %s 頁：沒有這個格子模板「%s」（見 t2c/data/pages.json）" % (page.get("page"), page.get("template")))
        boxes = tpl["panels"]
        slots = [p.get("slot") for p in page.get("panels") or []]
        if sorted(slots) != list(range(1, len(boxes) + 1)):
            raise CompileError("第 %s 頁：模板「%s」有 %d 格，分鏡的 slot 要剛好是 1–%d，目前是 %s"
                               % (page["page"], page["template"], len(boxes), len(boxes), slots))
        for panel in sorted(page["panels"], key=lambda p: p["slot"]):
            jobs.append(compile_panel(project, page, panel, boxes[panel["slot"] - 1], settings))
    if only_page is not None and not jobs:
        raise CompileError("分鏡裡沒有第 %s 頁" % only_page)
    return jobs


def save_job(build_dir, job):
    """把一格的工作寫成 build/<章>/<格>.prompt.json（layout 已展開，方便人看；prompt 字串不重複存）。"""
    path = build_dir / ("%s.prompt.json" % job["id"])
    path.write_text(json.dumps({k: v for k, v in job.items() if k != "prompt"}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path
