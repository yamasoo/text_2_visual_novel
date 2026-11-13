"""文字端流程（S5）：小說一章 → 改編規劃 → 漫畫劇本 → 分鏡 JSON。

    adapter = Adapter(project, llm.find_provider(cfg, stage="adapt"), should_stop=lambda: job.stopping)
    adapter.run_plan(1)             # plan/ch001.json
    adapter.run_script(1)           # script/ch001.md      （人可以在這裡改）
    adapter.run_storyboard(1)       # storyboard/ch001.json（保證能 compile_chapter）

每一階段：產生 → 驗證（check.py／script.py）→ 沒過就把**全部**錯誤回給模型修（最多 max_fix 次）→ 還是沒過丟 `AdaptError`，
最後一次的輸出存在 `build/adapt/chNNN/` 方便看。JSON 語法錯誤由 `llm.chat_json` 先處理一次。

核心不依賴 FastAPI／fekit：停止靠 `should_stop`（收到 `llm.Cancelled` 後由呼叫端自己 `job.checkpoint()`），
進度靠 `on_event`。模型呼叫可以替換（`chat`／`chat_json`），測試用。
"""
import json

from . import annotate, check, layout, llm, prompts, script as sc
from .compile import CompileError, compile_chapter

PANEL_FIELDS = ("slot", "shot", "angle", "location", "scene", "characters", "objects", "sfx", "bubbles")
STAGES = ("annotate", "plan", "script", "storyboard")
STAGE_KEY = {"annotate": "annotate", "plan": "adapt"}   # config.json 的 stage_providers／reasoning 用的階段名稱


class AdaptError(Exception):
    """給人看的錯誤訊息（驗證回修多次仍不過、缺前置檔案、檔案已存在…）。"""


class Adapter:
    def __init__(self, project, prov=None, pages_data=None, should_stop=None, on_event=None, on_delta=None,
                 max_fix=3, chat=None, chat_json=None, prov_for=None):
        """prov：三個階段共用的 provider；prov_for(stage_key)：依階段挑 provider（stage_key 是 adapt／script／storyboard）。"""
        if prov is None and prov_for is None:
            raise AdaptError("需要指定 provider（prov 或 prov_for）")
        self.project = project
        self._prov_for = prov_for or (lambda stage_key: prov)
        self.pages_data = pages_data or project.pages()
        self.should_stop = should_stop
        self.on_event = on_event
        self.on_delta = on_delta
        self.max_fix = int(max_fix)
        self._chat = chat or llm.chat
        self._chat_json = chat_json or llm.chat_json
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.warnings = []          # 驗證的 warn，結束時一起報告

    # --- 小工具 -----------------------------------------------------------
    def emit(self, kind, **info):
        if self.on_event:
            self.on_event(dict(info, kind=kind))

    def _stop_check(self):
        if self.should_stop and self.should_stop():
            raise llm.Cancelled()

    def _dump(self, chapter, name, text):
        try:
            folder = self.project.out_dir("build", "adapt", "ch%03d" % int(chapter))
            (folder / name).write_text(text, encoding="utf-8")
        except OSError:
            pass            # 除錯用的紀錄，寫不出來不影響流程

    def _call(self, stage, messages, as_json):
        key = STAGE_KEY.get(stage, stage)
        prov = self._prov_for(key)
        llm.check_context(prov, messages)
        kw = dict(stage=key, should_stop=self.should_stop, on_delta=self.on_delta,
                  on_retry=lambda n, e, w: self.emit("retry", stage=stage, attempt=n, error=str(e), wait=w))
        if as_json:
            data, reply = self._chat_json(prov, messages, **kw)
        else:
            reply = self._chat(prov, messages, **kw)
            if reply.finish == "length":
                raise llm.LLMError("輸出被截斷（超過 max_tokens）。請調高 max_output 或把工作切小。")
            data = reply.text
        self.usage["calls"] += 1
        for key in ("prompt_tokens", "completion_tokens"):
            self.usage[key] += int((reply.usage or {}).get(key) or 0)
        return data, reply

    def _generate(self, chapter, stage, label, messages, validate, as_json=True):
        """產生 → 驗證 → 回修。validate(結果) → (正規化後的結果, issues)。回傳驗證通過的結果。"""
        tag = "%s_%s" % (stage, label)
        msgs, last = list(messages), None
        for attempt in range(self.max_fix + 1):
            self._stop_check()
            self.emit("call", stage=stage, label=label, attempt=attempt)
            data, reply = self._call(stage, msgs, as_json)
            self._dump(chapter, "%s.txt" % tag, reply.text)
            value, issues = validate(data)
            errs = sc.errors(issues)
            self.warnings += ["%s：%s" % (label, i) for i in issues if i.level == "warn"]
            if not errs:
                return value
            last = errs
            self.emit("fix", stage=stage, label=label, attempt=attempt, errors=[str(e) for e in errs])
            msgs = list(messages) + [{"role": "assistant", "content": reply.text}, prompts.repair_message(errs, as_json)]
        raise AdaptError("%s（%s）：模型修了 %d 次仍有 %d 個問題：\n%s\n最後一次的輸出存在 %s"
                         % (label, stage, self.max_fix, len(last), "\n".join("  - %s" % e for e in last),
                            self.project.root / "build" / "adapt" / ("ch%03d" % int(chapter)) / ("%s.txt" % tag)))

    def _guard_overwrite(self, path, force, partial=False):
        if path.exists() and not force and not partial:
            raise AdaptError("%s 已經存在。要重新產生請加 --force（會覆蓋；建議先用 git 提交目前的版本）" % path)

    @staticmethod
    def _write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    # --- ① 改編規劃 ---------------------------------------------------------
    def plan_prompt(self, chapter, want_pages=None, title_hint=""):
        return prompts.plan_messages(self.project, self.project.load_source(chapter), self.pages_data, want_pages, title_hint)

    def run_plan(self, chapter, want_pages=None, title_hint="", force=False):
        path = self.project.plan_path(chapter)
        self._guard_overwrite(path, force)
        source = self.project.load_source(chapter)
        if not source:
            raise AdaptError("原著是空的：%s" % self.project.source_path(chapter))

        def validate(data):
            issues = check.check_plan(self.project, data, self.pages_data, want_pages, source=source)
            return data, issues

        messages = prompts.plan_messages(self.project, source, self.pages_data, want_pages, title_hint)
        plan = self._generate(chapter, "plan", "整章", messages, validate)
        plan = {"version": 1, "chapter": int(chapter), "title": str(plan.get("title") or title_hint or "第 %d 章" % int(chapter)),
                "summary": str(plan.get("summary") or ""), "pages": plan["pages"],
                "omitted": [str(x) for x in plan.get("omitted") or []],
                "unlisted_characters": [str(x) for x in plan.get("unlisted_characters") or []]}
        self._write(path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        self.emit("saved", stage="plan", path=str(path))
        return plan

    # --- ② 漫畫劇本 ---------------------------------------------------------
    def script_prompt(self, chapter, page_no):
        plan = self.project.load_plan(chapter)
        pp = self._plan_page(plan, page_no)
        return prompts.script_messages(self.project, self.project.load_source(chapter), plan, pp, self.pages_data,
                                       self._existing_page_text(chapter, page_no - 1), self._scenes(chapter))

    @staticmethod
    def _plan_page(plan, page_no):
        for p in plan["pages"]:
            if p["page"] == page_no:
                return p
        raise AdaptError("改編規劃裡沒有第 %s 頁" % page_no)

    def _existing_page_text(self, chapter, page_no):
        path = self.project.script_path(chapter)
        if page_no < 1 or not path.is_file():
            return ""
        return dict(sc.split_pages(sc.split_front(path.read_text(encoding="utf-8"))[1])).get(page_no, "")

    def run_script(self, chapter, only_pages=None, force=False):
        plan = self.project.load_plan(chapter)
        source = self.project.load_source(chapter)
        path = self.project.script_path(chapter)
        self._guard_overwrite(path, force, partial=bool(only_pages))
        blocks = {}
        if only_pages:                                        # 只重寫指定的頁：其餘維持原樣（含人改過的）
            if not path.is_file():
                raise AdaptError("%s 還不存在，無法只重寫部分頁。請先產生整章劇本" % path)
            blocks = dict(sc.split_pages(sc.split_front(path.read_text(encoding="utf-8"))[1]))
        scenes = self._scenes(chapter)                        # 切對話的逐字台詞帳本（沒有就不附）
        want = sorted(set(only_pages)) if only_pages else [p["page"] for p in plan["pages"]]
        for page_no in want:
            plan_page = self._plan_page(plan, page_no)
            tpl_key = str(plan_page["template"])
            n_panels = len(self.pages_data["templates"][tpl_key]["panels"])
            tpl_name = self.pages_data["templates"][tpl_key].get("name", "")
            prev = blocks.get(page_no - 1, "")
            messages = prompts.script_messages(self.project, source, plan, plan_page, self.pages_data, prev, scenes)

            def validate(text, page_no=page_no, tpl_key=tpl_key, n_panels=n_panels, tpl_name=tpl_name):
                body = sc.strip_fence(text)
                pages = sc.split_pages(body)
                if len(pages) != 1:
                    return None, [sc.Issue("error", "", "請只輸出第 %d 頁一頁，目前有 %d 個「## 第 N 頁」標題" % (page_no, len(pages)))]
                page, issues = sc.parse_page(pages[0][1])
                issues += sc.check_page(self.project, page, page_no, tpl_key, n_panels, strict=True)
                lines = pages[0][1].split("\n")
                lines[0] = "## 第 %d 頁（模板 %s：%s）" % (page_no, tpl_key, tpl_name)       # 標題行由程式統一
                return "\n".join(lines).strip(), issues

            blocks[page_no] = self._generate(chapter, "script", "第 %d 頁" % page_no, messages, validate, as_json=False)
            self.emit("page", stage="script", page=page_no)
        text = self._render_script(chapter, plan, blocks)
        self._write(path, text)
        self.emit("saved", stage="script", path=str(path))
        report = sc.fidelity(source, sc.parse_script(text)[1])
        self._write(self.project.out_dir("build", "adapt") / ("ch%03d_fidelity.md" % int(chapter)), sc.fidelity_markdown(report, plan.get("title", "")))
        return text, report

    def _render_script(self, chapter, plan, blocks):
        src = self.project.source_path(chapter)
        head = ["---", "chapter: %d" % int(chapter), "title: %s" % json.dumps(plan.get("title", ""), ensure_ascii=False),
                "source: %s" % (("source/" + src.name) if src else ""), "pages: %d" % len(blocks), "---", "",
                "# 第 %d 話　%s" % (int(chapter), plan.get("title", "")), ""]
        return "\n".join(head) + "\n\n".join(blocks[k] for k in sorted(blocks)) + "\n"

    # --- ③ 分鏡 -------------------------------------------------------------
    # --- ⓪ 切對話 ----------------------------------------------------------
    def _scenes(self, chapter):
        """scenes/chNNN.json（有就回傳，壞掉或沒有回傳 None）：劇本階段靠它拿到逐字台詞帳本。"""
        try:
            return self.project.load_scenes(chapter)
        except Exception:
            return None

    def annotate_prompt(self, chapter, title_hint=""):
        paragraphs = annotate.split_paragraphs(self.project.load_source(chapter))
        return prompts.annotate_messages(self.project, paragraphs, title_hint), paragraphs

    def run_annotate(self, chapter, title_hint="", force=False, repair=True):
        """小說 → 場 → 旁白／對話／演出（scenes/chNNN.json）＋忠實度報告。回傳 (scenes, report)。"""
        path = self.project.scenes_path(chapter)
        self._guard_overwrite(path, force)
        source = self.project.load_source(chapter)
        paragraphs = annotate.split_paragraphs(source)
        if not paragraphs:
            raise AdaptError("原著是空的：%s" % self.project.source_path(chapter))
        messages = prompts.annotate_messages(self.project, paragraphs, title_hint)

        def validate(data):
            expanded = annotate.expand(data, self.project, chapter, paragraphs, title_hint)
            issues = list(expanded.issues)
            if not issues:                       # 展開沒問題 → 先確認轉成分鏡後不會被擋
                try:
                    board = annotate.to_storyboard(self.project, expanded.scene_doc, self.pages_data,
                                                   compile_check=False)
                except annotate.AnnotateError as error:
                    issues.append(sc.Issue("error", "", str(error)))
                else:
                    for page in board["pages"]:
                        issues += check.check_page_panels(self.project, page["page"], page["template"],
                                                          page["panels"], self.pages_data)
            return expanded, issues

        expanded = self._generate(chapter, "annotate", "整章", messages, validate)
        scene_doc = expanded.scene_doc
        added = annotate.repair_omissions(scene_doc, annotate.paragraph_map(paragraphs)) if repair else 0
        report = annotate.fidelity(scene_doc, source, added)
        self._write(path, json.dumps(scene_doc, ensure_ascii=False, indent=2) + "\n")
        self._write(self.project.out_dir("build", "adapt") / ("ch%03d_scenes_fidelity.md" % int(chapter)),
                    annotate.fidelity_markdown(report, scene_doc.get("title") or title_hint))
        self.emit("saved", stage="annotate", path=str(path))
        return scene_doc, report

    def _load_script(self, chapter):
        path = self.project.script_path(chapter)
        if not path.is_file():
            raise AdaptError("找不到劇本：%s（先執行 script 階段）" % path)
        meta, pages, issues = sc.parse_script(path.read_text(encoding="utf-8"))
        if not pages:
            raise AdaptError("%s 裡沒有任何「## 第 N 頁」" % path)
        bad = sc.errors(issues)
        if bad:
            raise AdaptError("劇本有格式問題，請先修正再產生分鏡：\n%s" % "\n".join("  - %s" % i for i in bad))
        return meta, pages

    def _template_for(self, chapter, page):
        key = page.template
        if not key:
            try:
                key = str(self._plan_page(self.project.load_plan(chapter), page.page)["template"])
            except Exception:
                raise AdaptError("第 %d 頁的劇本標題沒有寫模板，也找不到改編規劃可以對照。標題請寫成「## 第 %d 頁（模板 5a）」" % (page.page, page.page))
        if key not in self.pages_data["templates"]:
            raise AdaptError("第 %d 頁：沒有這個格子模板「%s」（見 t2c/data/pages.json）" % (page.page, key))
        return key

    @staticmethod
    def _scene_summary(page_dict):
        return "\n".join("- %d 格：%s" % (p["slot"], p.get("scene", "")) for p in page_dict.get("panels", []))

    def storyboard_prompt(self, chapter, page_no, prev_scenes=""):
        _, pages = self._load_script(chapter)
        page = next((p for p in pages if p.page == page_no), None)
        if page is None:
            raise AdaptError("劇本裡沒有第 %s 頁" % page_no)
        return prompts.storyboard_messages(self.project, page.raw, page.page, self._template_for(chapter, page), self.pages_data, prev_scenes)

    def run_storyboard(self, chapter, only_pages=None, force=False):
        meta, pages = self._load_script(chapter)
        path = self.project.storyboard_path(chapter)
        self._guard_overwrite(path, force, partial=bool(only_pages))
        done = {}
        if only_pages and path.is_file():
            for p in self.project.load_storyboard(chapter).get("pages", []):
                done[p["page"]] = p
        title = str(meta.get("title") or "")
        for page in pages:
            if only_pages and page.page not in only_pages:
                continue
            key = self._template_for(chapter, page)
            prev = self._scene_summary(done[page.page - 1]) if (page.page - 1) in done else ""
            messages = prompts.storyboard_messages(self.project, page.raw, page.page, key, self.pages_data, prev)

            def validate(data, page=page, key=key):
                panels, issues = self._normalize_panels(data, page.page)
                if panels is None:
                    return None, issues
                issues += check.check_page_panels(self.project, page.page, key, panels, self.pages_data)
                issues += check.check_against_script(self.project, page.page, panels, page)
                return panels, issues

            panels = self._generate(chapter, "storyboard", "第 %d 頁" % page.page, messages, validate)
            done[page.page] = {"page": page.page, "template": key, "panels": panels}
            self.emit("page", stage="storyboard", page=page.page)
        storyboard = {"version": 1, "chapter": int(chapter), "title": title, "pages": [done[k] for k in sorted(done)]}
        try:                                           # 最後一道關卡：能不能整章編譯
            compile_chapter(self.project, storyboard, self.pages_data)
        except CompileError as error:
            self._dump(chapter, "storyboard_final.json", json.dumps(storyboard, ensure_ascii=False, indent=2))
            raise AdaptError("分鏡通過了檢查，卻無法編譯：%s（已存到 build/adapt/ch%03d/storyboard_final.json）" % (error, int(chapter)))
        self._write(path, json.dumps(storyboard, ensure_ascii=False, indent=2) + "\n")
        self.emit("saved", stage="storyboard", path=str(path))
        return storyboard

    def _normalize_panels(self, data, page_no):
        """模型的輸出 → 這一頁的 panels（只留已知欄位、補上 id）。回傳 (panels | None, issues)。"""
        if isinstance(data, dict) and isinstance(data.get("pages"), list) and len(data["pages"]) == 1 and isinstance(data["pages"][0], dict):
            data = data["pages"][0]                                # 模型多包了一層，容忍
        raw = data.get("panels") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return None, [sc.Issue("error", "", "最外層要是 {\"panels\": [ … ]}")]
        panels, issues = [], []
        for p in raw:
            if not isinstance(p, dict):
                return None, [sc.Issue("error", "", "panels 的每一項要是物件")]
            clean = {k: p[k] for k in PANEL_FIELDS if k in p}
            if isinstance(clean.get("slot"), str) and clean["slot"].isdigit():
                clean["slot"] = int(clean["slot"])
            if isinstance(clean.get("slot"), int):
                clean["id"] = "p%03d_k%d" % (page_no, clean["slot"])        # 一律用預設 id（檔名、挑圖都靠它）
            for key in set(p) - set(PANEL_FIELDS) - {"id", "seeds"}:
                issues.append(sc.Issue("warn", "第 %s 頁第 %s 格" % (page_no, p.get("slot")), "多餘的欄位「%s」已忽略" % key))
            for key in ("characters", "objects", "sfx", "bubbles"):
                if isinstance(clean.get(key), list):
                    clean[key] = [self._strip(item, key) for item in clean[key]]
            panels.append(self._order(clean))
        return panels, issues

    @staticmethod
    def _strip(item, kind):
        if not isinstance(item, dict):
            return item
        keep = {"characters": check.CHAR_KEYS, "bubbles": check.BUBBLE_KEYS, "objects": {"caption", "box"}, "sfx": {"text", "box"}}[kind]
        out = {k: v for k, v in item.items() if k in keep}
        if kind == "bubbles" and out.get("offscreen") is False:
            del out["offscreen"]                                   # 預設值不寫，檔案比較好讀
        return out

    @staticmethod
    def _order(panel):
        """欄位順序固定，存出來的 JSON 才好讀、好比對。"""
        order = ("slot", "id") + PANEL_FIELDS[1:]
        return {k: panel[k] for k in order if k in panel}

    # --- 全流程 -------------------------------------------------------------
    def run_all(self, chapter, want_pages=None, title_hint="", force=False):
        plan = self.run_plan(chapter, want_pages, title_hint, force)
        text, report = self.run_script(chapter, force=force)
        storyboard = self.run_storyboard(chapter, force=force)
        return plan, text, report, storyboard
