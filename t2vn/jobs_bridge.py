"""任務的專案層：``Jobs``（轉移計畫 §8.1）。

任務本身、佇列、權杖、串流與端點都是 fekit 的 ``JobRunner``／``Job``；這裡只留 t2vn
需要的東西：一本書同時一個任務、任務標題、進度文字、章節項目，以及任務紀錄的格式
（``jobrecords``）。

``Jobs.start()`` 的 ``target(fe_job)`` 收到的是 fekit 的任務物件（不再有第二份鏡射），
回傳的 dict 就是任務的 ``result``。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from fekit import JobError as FeJobError
from fekit import JobRunner
from fekit import JobStopped as FeJobStopped

from .errors import JobStopped as LegacyJobStopped

FE_JOBS_BASE = "/api/fe-jobs"

TITLES = {
    "annotate": "AI 標註", "extract": "抽取章綱與角色", "art": "出圖（ComfyUI）",
    "tts": "語音生成", "export": "匯出 Ren'Py", "verify": "自動驗收", "package": "打包 Windows 版",
}

PHASE_TEXT = {
    "starting": "準備中", "waiting": "已送出，等待模型開始回應", "streaming": "模型輸出中",
    "thinking": "模型思考中", "checking": "檢查輸出是否符合規則", "repair": "輸出有問題，請模型修正",
    "idle": "待命",
}

ITEM_STATE = {"pending": "pending", "running": "running", "done": "done",
              "skipped": "skipped", "error": "error", "stopped": "stopped"}


class Jobs:
    """一本書同時一個任務；任務本身是 fekit 的 ``Job``。

    ``self.meta`` 依任務 id 記住 t2vn 自己的東西（書、章節、執行者、權杖數、
    紀錄用的狀態與時間、進度游標），不掛在 fekit 的物件上。
    """

    def __init__(self, runner: JobRunner) -> None:
        self.runner = runner
        self.meta: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    # ---- 查詢
    def latest_for(self, book_id: str) -> Any | None:
        """這本書最近的任務（重新整理後接回還在跑／剛結束的任務）。"""
        rows = [(meta["started"], job_id) for job_id, meta in self.meta.items() if meta["book_id"] == book_id]
        if not rows:
            return None
        return self.runner.get(max(rows)[1])

    # ---- 建立
    def start(self, book_id: str, kind: str, chapters: list[int], executor: str,
              target: Callable[[Any], Any], on_finish: Callable[[Any], None] | None = None) -> Any:
        """開始一個任務，回傳 fekit 的 ``Job``（回應給前端時用 ``summary()``）。"""
        meta: dict[str, Any] = {
            "book_id": book_id, "chapters": list(chapters), "kind": kind, "executor": executor,
            "tokens": 0, "status_hint": "", "seg": 0, "cursor": {},
            "started": time.time(), "ended": None,
        }
        fe = self.runner.start(self._wrap(meta, target, on_finish), kind=kind,
                               title="%s · %s" % (TITLES.get(kind, kind), book_id))
        with self.lock:
            self.meta[fe.id] = meta
        return fe

    def _wrap(self, meta: dict[str, Any], target: Callable[[Any], Any],
              on_finish: Callable[[Any], None] | None) -> Callable[[Any], Any]:
        def run(fe: Any) -> Any:
            with self.lock:  # 任務執行緒一開始就登記，後面的進度回呼才查得到 meta
                self.meta[fe.id] = meta
            rows = [{"key": "ch%03d" % n, "label": "第 %d 章" % n, "state": "pending", "note": ""}
                    for n in meta["chapters"]]
            if rows:  # 還沒跑到的章節也要先在清單上（狀態 pending）
                fe.set(items=rows)
            try:
                result = target(fe)
            except LegacyJobStopped:
                fe.say("stopped")
                self.finish(fe, "stopped", on_finish)
                raise FeJobStopped() from None
            except Exception as exc:  # noqa: BLE001 — 任何失敗都要留下紀錄
                message = str(exc) or type(exc).__name__
                fe.say("error: %s" % exc, "error")
                self.finish(fe, "error", on_finish)
                if isinstance(exc, FeJobError):
                    raise
                raise FeJobError(message) from exc
            result = result if isinstance(result, dict) else {}
            if fe.stop_requested or result.get("stopped"):
                self.finish(fe, "stopped", on_finish, result)
                raise FeJobStopped()
            if result.get("failed"):
                self.finish(fe, "error", on_finish, result)
                raise FeJobError("有項目沒有完成，詳細原因請看任務紀錄。")
            self.finish(fe, "done", on_finish, result)
            return result

        return run

    def _meta(self, fe: Any) -> dict[str, Any]:
        meta = self.meta.get(fe.id)
        if meta is None:
            raise RuntimeError("任務 %s 沒有登記 meta（只能由 Jobs.start 建立）" % fe.id)
        return meta

    # ---- 進度（舊的 set_item／current.update）
    def item(self, fe: Any, number: int, state: str | None = None, note: str | None = None) -> None:
        """舊的 ``set_item(number, status, note)``：改成 fekit 的 ``item(key, ...)``。"""
        meta = self._meta(fe)
        fields: dict[str, Any] = {"label": "第 %d 章" % number}
        if state is not None:
            fields["state"] = ITEM_STATE.get(state, "pending")
        if note is not None:
            fields["note"] = note
        fe.item("ch%03d" % number, **fields)
        if state == "running":
            # 舊行為：換章時把游標換成新的一份（不留下上一章的 chunk）。
            meta["cursor"] = {"chapter": number, "phase": "starting", "round": 0, "chars": 0, "since": time.time()}
            self._sync(fe, meta)

    def update(self, fe: Any, values: dict[str, Any] | None = None, **fields: Any) -> None:
        """舊的 ``job.current.update(...)``：寫成 fekit 的 phase／note／progress。"""
        meta = self._meta(fe)
        cursor = meta["cursor"]
        if values:
            cursor.update(values)
        cursor.update(fields)
        self._sync(fe, meta)

    def _sync(self, fe: Any, meta: dict[str, Any]) -> None:
        cursor = meta["cursor"]
        fields: dict[str, Any] = {"phase": str(cursor.get("phase") or "starting"), "note": self.note_for(meta)}
        total = cursor.get("total") or 0
        if total:
            done = max(0, (cursor.get("index") or 1) - 1 + (1 if cursor.get("event") == "asset_done" else 0))
            fields["progress"] = (done, total)
        fe.set(**fields)

    def note_for(self, meta: dict[str, Any]) -> str:
        """面板上的進度文字（舊的 progress_note）。"""
        cursor, kind = meta["cursor"], meta["kind"]
        if kind == "art":
            total = cursor.get("total") or 0
            if not total:
                return "準備中"
            text = "%s（第 %s/%s 項" % (cursor.get("asset") or "出圖", cursor.get("index") or 1, total)
            if cursor.get("of"):
                text += "，這項第 %s/%s 張" % (cursor.get("n") or 1, cursor["of"])
            return text + "）"
        if kind == "tts":
            if not cursor.get("line_id"):
                return "準備中"
            return "%s（第 %s/%s 句%s）" % (cursor["line_id"], cursor.get("index") or "?", cursor.get("total") or "?",
                                          "，" + cursor["speaker"] if cursor.get("speaker") else "")
        chapter = cursor.get("chapter")
        if chapter is None:
            return PHASE_TEXT.get(cursor.get("phase"), "")
        parts = ["第 %d 章" % chapter]
        if cursor.get("chunk"):
            parts.append("第 %s 塊" % cursor["chunk"])
        if cursor.get("round"):
            parts.append("第 %s 輪" % cursor["round"])
        if cursor.get("phase") == "streaming" and cursor.get("chars"):
            parts.append("%s 字" % format(cursor["chars"], ","))
        return " · ".join(parts)

    # ---- 模型輸出（RunHooks.on_event，§8.1-5）
    def hooks(self, fe: Any) -> Any:
        from .llm import RunHooks
        return RunHooks(should_stop=lambda: fe.stop_requested, on_event=lambda kind, info: self.event(fe, kind, info))

    def _seg(self, meta: dict[str, Any]) -> str:
        return "s%d" % meta["seg"]

    def event(self, fe: Any, kind: str, info: dict[str, Any]) -> None:
        """模型串流：fekit 的 begin／write／think／end，加上 phase／note。"""
        meta = self._meta(fe)
        cursor = meta["cursor"]
        if kind == "request":
            cursor.update(phase="waiting", round=info.get("round", 0), chars=0, since=time.time())
            meta["seg"] += 1
            chapter, chunk = cursor.get("chapter"), cursor.get("chunk")
            where = "第 %d 章" % chapter if chapter else ""
            if where and chunk:
                where += " · 第 %s 塊" % chunk
            label = " · ".join(x for x in (where, "第 %s 輪" % cursor["round"] if cursor.get("round") else "") if x)
            fe.begin(self._seg(meta), label or "輸出")
            self._sync(fe, meta)
        elif kind == "chunk":
            cursor.update(chunk="%s/%s" % (info["index"], info["total"]), chunk_range=[info["first"], info["last"]])
            self._sync(fe, meta)
        elif kind == "delta":
            cursor.update(phase="streaming", chars=info.get("chars", 0))
            fe.write(self._seg(meta), info.get("text", ""))
            self._sync(fe, meta)
        elif kind == "thinking":
            cursor.update(phase="thinking")
            fe.think(self._seg(meta), info.get("text", ""))
            self._sync(fe, meta)
        elif kind == "response":
            usage = info.get("usage") or {}
            meta["tokens"] += int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
            fe.end(self._seg(meta))
            cursor.update(phase="checking", since=time.time())
            self._sync(fe, meta)
        elif kind == "repair":
            cursor.update(phase="repair", count=info.get("count", 0), since=time.time())
            self._sync(fe, meta)

    # ---- 紀錄
    def finish(self, fe: Any, status: str, on_finish: Callable[[Any], None] | None,
               result: dict[str, Any] | None = None) -> None:
        """先寫 result，再存紀錄。

        兩件事都必須在 fekit 把狀態改成終態之前做完：record() 讀的是當下的 patch，
        而 fekit 之後才設 result（stopped／error 時還會被清成 null）。
        """
        meta = self._meta(fe)
        meta["status_hint"] = status
        meta["ended"] = time.time()
        if result:
            try:
                fe.set(result=result)
            except (TypeError, ValueError) as exc:  # noqa: BLE001 — 鏡射失敗不能讓任務狀態壞掉
                fe.say("could not mirror result: %s" % exc, "warn")
        if on_finish is not None:
            try:
                on_finish(fe)
            except Exception as exc:  # noqa: BLE001 — 存不了紀錄不能讓任務狀態壞掉
                fe.say("could not save job record: %s" % exc, "warn")

    def record(self, fe: Any) -> dict[str, Any]:
        """任務紀錄：從 fekit 的 ``patch(0)`` 組出來（§8.1-4）。"""
        meta = self._meta(fe)
        snap = fe.patch(0)
        return {
            "id": snap["id"], "book_id": meta["book_id"], "kind": snap["kind"], "status": meta["status_hint"],
            "started": snap["started_at"], "ended": meta["ended"],
            "chapters": list(meta["chapters"]), "executor": meta["executor"],
            "tokens": meta["tokens"], "items": snap["items"], "summary": snap.get("result") or {},
            "log": [entry["text"] for entry in snap["log"]],
        }
