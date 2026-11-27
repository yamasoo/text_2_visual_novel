"""N5 反推（排程層）：完成的稿子 → 逐章抽取 → 本機累積 → 視覺聖經草稿。

    sources = bible.load_sources([], project)          # 專案的 source/（或 tools/reverse.py --from）
    rev = Reverser(project, prov=llm.find_provider(cfg, stage="bible"), should_stop=lambda: job.stopping)
    rev.run(upto=30)                                   # 分批 →（斷點續跑）→ 累積 → 卡片草稿

做的是 novel-studio 的 `studio/reverse.py` 排程層：章節分批（每批最多 3 章）、token 預算估算、
斷點續跑、逐章抽取後累積。抽取與卡片提示詞不重寫，一律走 `bible.Builder`（scan／draft）。

落點（`build/` 不進版控）：
    build/reverse/state.json                 跑到哪裡、這次跑了幾批
    build/reverse/accumulated.json           跨批累積：已完成的章、角色、地點
    build/reverse/batches/第NNN-NNN章.json   每批的原始掃描輸出
    bible/drafts/characters|locations/<id>.md  卡片草稿（人看過按「採用」才搬進 bible/characters/）

核心不依賴 FastAPI／fekit：停止靠 should_stop（乾淨停下，不丟例外），進度靠 on_event，模型呼叫可替換（測試用）。
"""
import hashlib
import json
import os
import time

from . import bible, llm

MAX_PER_BATCH = 3          # 一批最多幾章（模型一次要讀完的正文上限）
STATE_NAME = "state.json"
ACC_NAME = "accumulated.json"


class ReverseError(Exception):
    """給人看的錯誤訊息（沒有原著、抽取失敗、累積檔壞掉…）。"""


# ---------------------------------------------------------------------------
# 檔案
# ---------------------------------------------------------------------------
def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 估算與分批
# ---------------------------------------------------------------------------
def plan_batches(sources, upto=None, budget=32768, out_reserve=8192, start=1):
    """依上下文預算把第 start～upto 章分批，每批最多 3 章；至少一批一章（預算再小也不會空手）。

    sources：`bible.load_sources()` 的結果（每章要有 n 與 text）；upto：只排到第幾章（預設全部）；
    budget／out_reserve：模型的上下文與輸出預留，可用的輸入空間是 budget - out_reserve。
    回傳 [[1, 2, 3], [4, 5], …]（章號，依章節順序）。
    """
    room = int(budget) - int(out_reserve)
    batches, cur, used = [], [], 0
    for source in sorted(sources, key=lambda s: int(s["n"])):
        n = int(source["n"])
        if upto is not None and n > int(upto):
            break
        if n < int(start):
            continue
        cost = llm.estimate_tokens(source.get("text") or "")
        if cur and (len(cur) >= MAX_PER_BATCH or used + cost > room):
            batches.append(cur)
            cur, used = [], 0
        cur.append(n)
        used += cost
    if cur:
        batches.append(cur)
    return batches


# ---------------------------------------------------------------------------
# 累積（跨批合併，不重讀正文）
# ---------------------------------------------------------------------------
def _entity_entry(bucket, entity, chapter):
    name = str(entity.get("name") or "").strip()
    if not name:
        return
    entry = bucket.setdefault(name, {"name": name, "chapters": [], "aliases": [], "looks": [], "outfits": []})
    entry["chapters"] = sorted(set(entry["chapters"]) | {int(chapter)})
    for key in ("aliases", "looks", "outfits"):
        raw = entity.get(key) or []
        values = [str(x) if key == "aliases" else str((x or {}).get("en") or "") for x in raw]
        entry[key] = list(dict.fromkeys([v for v in entry[key] + values if v]))


def _merge(acc, nos, scans):
    """把這一批的掃描結果併進累積檔。nos：章號；scans：build/bible/scan/chNNN.json 的內容（可能缺）。"""
    acc = dict(acc or {})
    acc["version"] = 1
    acc["chapters"] = sorted(set(int(n) for n in acc.get("chapters") or []) | {int(n) for n in nos})
    chars, locs = dict(acc.get("characters") or {}), dict(acc.get("locations") or {})
    for n, doc in zip(nos, scans):
        result = (doc or {}).get("result") or {}
        for entity in result.get("characters") or []:
            _entity_entry(chars, entity, n)
        for entity in result.get("locations") or []:
            _entity_entry(locs, entity, n)
    acc["characters"], acc["locations"] = chars, locs
    acc["updated"] = time.time()
    return acc


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
class Reverser:
    """逐章抽取 → 累積 → 成卡草稿。已完成的章（累積檔有、掃描快取還在）直接跳過，中斷了從斷的地方接。

    prov／prov_for：同 `bible.Builder`（prov_for(stage_key) 依階段挑 provider，這裡只有 bible）。
    sources：明確指定原著（`bible.load_sources()` 的結果）；不給就讀專案的 source/。
    chat_json：換掉模型呼叫（測試用）。
    """

    def __init__(self, project, prov=None, prov_for=None, should_stop=None, on_event=None,
                 budget=32768, out_reserve=8192, force=False, sources=None, chat_json=None, min_chapters=None):
        if prov is None and prov_for is None:
            raise ReverseError("需要指定 provider（prov 或 prov_for）")
        self.project = project
        self.should_stop, self.on_event = should_stop, on_event
        self.budget, self.out_reserve = int(budget), int(out_reserve)
        self.force = bool(force)
        self.min_chapters = int(min_chapters) if min_chapters else None
        self.sources = list(sources) if sources else None
        self.builder = bible.Builder(project, prov=prov, prov_for=prov_for, should_stop=should_stop,
                                     on_event=on_event, chat_json=chat_json)
        self.usage = self.builder.usage

    # --- 路徑與事件 ---------------------------------------------------------
    def dir(self):
        return self.project.root / "build" / "reverse"

    def state_path(self):
        return self.dir() / STATE_NAME

    def accumulated_path(self):
        return self.dir() / ACC_NAME

    def batch_path(self, nos):
        return self.dir() / "batches" / ("第%03d-%03d章.json" % (min(nos), max(nos)))

    def emit(self, kind, **info):
        if self.on_event:
            self.on_event(dict(info, kind=kind))

    def _stopping(self):
        return bool(self.should_stop and self.should_stop())

    # --- 原著／快取 ---------------------------------------------------------
    def all_sources(self):
        """這次要處理的原著：明確給的，或專案 source/ 裡的章節。"""
        return self.sources if self.sources is not None else bible.load_sources([], self.project)

    def _scan_doc(self, chapter):
        path = self.builder.scan_path(chapter)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def _has_scan(self, source):
        """這一章有可用的掃描快取（檔案在、原文沒變）→ 續跑時不必再呼叫模型。"""
        doc = self._scan_doc(source["n"])
        return doc.get("sha1") == hashlib.sha1(str(source.get("text") or "").encode("utf-8")).hexdigest()

    # --- 跑一輪 -------------------------------------------------------------
    def run(self, upto=None, min_chapters=None):
        """跑到第 upto 章（預設全部）。回傳 {"chapters", "batches", "scanned", "skipped", "drafts", "rare"}：
        chapters 是這次認定的章數、batches 是實際跑完的批數、scanned 是這次處理的章數、
        skipped 是已完成而跳過的章數、drafts 是這次寫出的草稿 id、rare 是出現章數太少而沒成卡的名字。
        被 should_stop 叫停就回傳完成的部分。"""
        if min_chapters is not None:
            self.min_chapters = int(min_chapters)
        sources = self.all_sources()
        by_no = {int(s["n"]): s for s in sources}
        if not by_no:
            raise ReverseError("沒有原著可以讀：請用 --from 指定小說章節的資料夾或檔案，或把原著放在專案的 source/")
        upto = max(by_no) if upto is None else min(int(upto), max(by_no))
        wanted = [by_no[n] for n in sorted(by_no) if n <= upto]

        acc = {} if self.force else _load(self.accumulated_path(), {})
        done = sorted(int(n) for n in acc.get("chapters") or []
                      if int(n) in by_no and self._has_scan(by_no[int(n)]))
        skipped = len([n for n in done if n <= upto])
        finished = set(done)                                      # 中間缺章的掃描快取就不算完成，會被重新排進批次
        pending = [by_no[n] for n in sorted(by_no) if n <= upto and n not in finished]
        start = pending[0]["n"] if pending else 0
        batches = plan_batches(pending, upto=upto, budget=self.budget, out_reserve=self.out_reserve)
        self.emit("plan", chapters=len(wanted), upto=upto, batches=len(batches), start=start, skipped=skipped)

        scanned = ran = 0
        stopped = False
        for index, nos in enumerate(batches, 1):
            if self._stopping():
                stopped = True
                break
            label = ("第%d～%d章" % (min(nos), max(nos))) if len(nos) > 1 else ("第%d章" % nos[0])
            self.emit("batch", index=index, total=len(batches), chapters=list(nos), label=label)
            try:
                self.builder.scan([by_no[n] for n in nos], force=self.force, only=set(nos))
            except llm.Cancelled:
                stopped = True
                break
            except bible.BibleError as error:
                raise ReverseError("抽取失敗（第 %d／%d 批，%s）：%s" % (index, len(batches), label, error)) from error
            scans = [self._scan_doc(n) for n in nos]
            _save(self.batch_path(nos), {"version": 1, "batch": index, "chapters": list(nos),
                                         "created": time.time(), "scans": scans})
            acc = _merge(acc, nos, scans)
            _save(self.accumulated_path(), acc)
            scanned, ran = scanned + len(nos), ran + 1
            self.emit("saved", path=str(self.batch_path(nos)), chapters=list(nos))

        if not stopped and self._stopping():
            stopped = True
        drafts, rare = [], []
        if not stopped and acc.get("chapters"):
            todos = [by_no[int(n)] for n in acc["chapters"] if int(n) in by_no and self._has_scan(by_no[int(n)])]
            self.builder.scan(todos)                 # 合併全部章節的證據；都命中快取，不呼叫模型
            # 只有 1 章可用時，Builder 預設的「出現 2 章以上才建卡」會讓所有角色都落空；門檻降到章數
            usable = len({int(n) for n in acc["chapters"]
                          if int(n) in by_no and int(n) <= upto and self._has_scan(by_no[int(n)])})
            try:
                threshold = self.min_chapters or min(2, usable)          # --min-chapters 可以讓只出現一章的配角也成卡
                report = self.builder.draft(force=self.force, min_chapters=threshold)
            except llm.Cancelled:
                stopped, report = True, {"drafted": [], "rare": []}
            drafts = [cid for _kind, cid, _path in report["drafted"]]
            rare = list(report.get("rare") or [])
            if not stopped:
                self.emit("drafts", ids=list(drafts), total=len(drafts), rare=list(rare))
        if stopped:
            self.emit("stopped", scanned=scanned)

        _save(self.state_path(), {"version": 1, "upto": upto, "chapters": list(acc.get("chapters") or []),
                                  "batches": ran, "scanned": scanned, "skipped": skipped,
                                  "stopped": stopped, "updated": time.time()})
        return {"chapters": len(wanted), "batches": ran, "scanned": scanned, "skipped": skipped,
                "drafts": drafts, "rare": rare}
