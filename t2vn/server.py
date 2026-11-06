"""校對／編輯介面的本機網頁服務。

``python -m t2vn.server`` 會在 127.0.0.1 開服務並打開瀏覽器。只收本機連線（檢查 Host 與 Origin），
所有寫入都經 ``Config.assert_writable``；金鑰不經過網頁（用 ``python -m t2vn.llm set-key``）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .annotate import annotate_chapter_to_disk
from .config import Config, load_config
from .errors import T2vnError
from .extract import extract_book
from . import art_cards, art_cards_studio, art_gen, art_studio, audio_gen, bible, candidate_files, export_studio, history, jobrecords, overview, style_edit
from . import sherpa_tts, voice_gen, voice_lines
from .chunked import chunk_chars_for
from .llm import LlmError, LlmFn, key_status, make_executor
from .settings import apply_settings, check_connection, read_settings, store_key
from .studio import Studio, StudioError

REPO_ROOT = Path(__file__).resolve().parents[1]
# fekit 的 Python 輔助是 vendor/fekit/py 裡的複製品；要在 import fekit 之前加進 sys.path。
sys.path.insert(0, str(REPO_ROOT / "vendor" / "fekit" / "py"))

from fekit import JobRunner, install  # noqa: E402

from .jobs_bridge import FE_JOBS_BASE, Jobs  # noqa: E402

UI_DIR = REPO_ROOT / "ui"
FEKIT_DIR = REPO_ROOT / "vendor" / "fekit"
TEMPLATES = REPO_ROOT / "templates"

ALLOWED_EXTS = {".txt", ".md", ".markdown", ".text", ".docx", ".epub"}
JOB_KINDS = ("annotate", "extract")
ART_JOB_KIND = "art"
JOB_RECORDS_KEEP = 50
STYLE_REL = Path(bible.STYLE_FILE)


def tts_executor(doc: dict[str, Any] | None) -> str:
    """TTS 任務面板顯示的執行者：sherpa-onnx 不經過 ComfyUI。"""
    return "sherpa-onnx" if (doc or {}).get("engine") == "sherpa-onnx" else "ComfyUI"


class Context:
    """可以在執行中替換的設定與邏輯（儲存「設定」頁之後重建）。"""

    def __init__(self, config: Config, config_path: Path | None):
        self.config_path = config_path
        self.set_config(config)

    def set_config(self, config: Config) -> None:
        self.config = config
        self.studio = Studio(config)


def create_app(config: Config, llm_factory: Callable[[], LlmFn] | None = None, config_path: Path | None = None,
               port: int | None = None, comfy_factory: Callable[[], Any] | None = None,
               audio_backend_factory: Callable[[], Any] | None = None, audio_runner: Callable[[list[str]], tuple[int, str]] | None = None,
               tts_backend_factory: Callable[[], Any] | None = None, tts_runner: Callable[[list[str]], tuple[int, str]] | None = None,
               wildcard_dir: Path | None = None, token: str | None = None):
    ctx = Context(config, config_path)
    wild_dir = Path(wildcard_dir) if wildcard_dir is not None else REPO_ROOT / "vendor" / "wildcards"
    app = FastAPI(title="text_2_visual_novel studio")

    runner = JobRunner()                       # 同時 1 個任務；滿了回 409（舊的是同書 400）
    jobs = Jobs(runner)
    # 本機保護（Host、Origin、X-FE-Token）、錯誤格式、/api/fe-jobs 與 /api/fs/list 端點。
    fe = install(
        app, app_name="t2vn", jobs=runner, jobs_base=FE_JOBS_BASE,
        fs=True,
        token=token or os.environ.get("T2VN_FE_TOKEN") or None,
    )

    def get_llm(executor: str | None = None) -> LlmFn:
        if llm_factory is not None:
            return llm_factory()
        return make_executor(ctx.config, executor or None)

    def save_record(work: Path, config_now: Config) -> Callable[[Any], None]:
        """任務結束時的存檔回呼：寫一份 work/_jobs/<id>.json 並修剪舊的。"""
        def on_finish(job: Any) -> None:
            config_now.assert_writable(work / jobrecords.JOBS_DIR_NAME / f"{job.id}.json")
            jobrecords.save_record(work, jobs.record(job))
            jobrecords.prune_records(work, JOB_RECORDS_KEEP)
        return on_finish

    @app.middleware("http")
    async def no_cache_ui(request: Request, call_next):
        """前端檔每次都向伺服器確認有沒有更新，避免改版後瀏覽器還在用舊的 JS。"""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith(("/ui/", "/fekit/")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(ValueError)
    async def handle_value_error(request: Request, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(art_cards.ArtCardsError)
    async def handle_cards_error(request: Request, exc: art_cards.ArtCardsError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(T2vnError)
    async def handle_error(request: Request, exc: T2vnError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    # ---- 狀態與設定
    @app.get("/api/state")
    def state():
        c = ctx.config
        found, where = key_status(c.llm_provider) if llm_factory is None else (True, "test")
        return {"default_executor": c.llm_default, "library": str(c.book_library), "library_exists": c.book_library.is_dir(), "works_dir": str(c.works_dir),
                "llm": {"provider": c.llm_provider, "model": c.llm_model, "thinking": c.llm_thinking, "key_ok": found,
                        "message": "" if found else f"no API key for {c.llm_provider}; set it on the Settings page"}}

    @app.get("/api/settings")
    def get_settings():
        return read_settings(ctx.config, ctx.config_path, port)

    @app.put("/api/settings")
    async def put_settings(request: Request):
        if ctx.config_path is None:
            raise StudioError("this server was started without a config file; settings cannot be saved")
        changes = await request.json()
        old_port = ctx.config.server_port
        ctx.set_config(apply_settings(ctx.config_path, changes))
        result = read_settings(ctx.config, ctx.config_path, port)
        result["restart_required"] = ctx.config.server_port != (port or old_port)
        return result

    @app.post("/api/settings/key")
    async def put_key(request: Request):
        body = await request.json()
        where = store_key(body.get("provider", ""), body.get("key", ""), ctx.config)
        return {"saved_to": where}

    @app.post("/api/settings/test")
    async def test_llm(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        return check_connection(ctx.config, (body or {}).get("executor") or None)

    # ---- 書架
    @app.get("/api/books")
    def books():
        return {"books": ctx.studio.list_books(), "library": ctx.studio.library_candidates()}

    @app.post("/api/import/library")
    async def import_library(request: Request):
        body = await request.json()
        return ctx.studio.import_library_book(body.get("dir_name", ""), body.get("book_id", ""), body.get("display_name", ""),
                                              body.get("lang") or "zh-TW")

    @app.post("/api/import/preview")
    async def import_preview(request: Request):
        body = await request.json()
        return ctx.studio.preview_import(_payload(body), body.get("opts") or {})

    @app.post("/api/import/commit")
    async def import_commit(request: Request):
        body = await request.json()
        return ctx.studio.commit_import(body.get("book_id", ""), body.get("display_name", ""), _payload(body), body.get("opts") or {})

    # ---- 章節、場景、角色、章綱
    @app.get("/api/books/{book_id}/chapters")
    def chapters(book_id: str):
        return {"chapters": ctx.studio.chapters(book_id), "book": ctx.studio.book_json(book_id)}

    @app.get("/api/books/{book_id}/chapters/{number}")
    def chapter(book_id: str, number: int):
        return ctx.studio.get_chapter(book_id, number)

    @app.post("/api/books/{book_id}/chapters/{number}/check")
    async def check(book_id: str, number: int, request: Request):
        body = await request.json()
        return ctx.studio.check_scenes(book_id, number, body["doc"])

    @app.put("/api/books/{book_id}/chapters/{number}/scenes")
    async def save_scenes(book_id: str, number: int, request: Request):
        body = await request.json()
        return ctx.studio.save_scenes(book_id, number, body["doc"], body.get("status"))

    @app.put("/api/books/{book_id}/chapters/{number}/status")
    async def set_status(book_id: str, number: int, request: Request):
        body = await request.json()
        ctx.studio.set_status(book_id, number, body.get("status", ""))
        return {"ok": True}

    @app.get("/api/books/{book_id}/cast")
    def get_cast(book_id: str):
        return ctx.studio.get_cast(book_id)

    @app.put("/api/books/{book_id}/cast")
    async def put_cast(book_id: str, request: Request):
        return ctx.studio.save_cast(book_id, await request.json())

    @app.put("/api/books/{book_id}/chapters/{number}/outline")
    async def put_outline(book_id: str, number: int, request: Request):
        body = await request.json()
        return ctx.studio.save_outline(book_id, number, body.get("summary", ""), body.get("plot_points", ""), body.get("cast_names", ""))

    # ---- 背景任務
    @app.post("/api/books/{book_id}/jobs")
    async def start_job(book_id: str, request: Request):
        body = await request.json()
        kind = body.get("kind")
        if kind not in JOB_KINDS:
            raise StudioError(f"job kind must be one of {JOB_KINDS}")
        studio = ctx.studio
        work = studio.work(book_id)
        numbers = sorted({int(n) for n in body.get("chapters") or []})
        if not numbers:
            raise StudioError("choose at least one chapter")
        executor = str(body.get("executor") or "").strip() or None
        try:
            llm = get_llm(executor)
        except LlmError as exc:
            raise StudioError(str(exc)) from exc
        force = bool(body.get("force"))
        config_now = ctx.config
        chunk_chars = chunk_chars_for(config_now, executor)
        lang = studio.book_json(book_id).get("lang", "zh-TW")

        def run(j: Any) -> dict[str, Any]:
            hooks = jobs.hooks(j)
            if kind == "extract":
                return extract_book(work, numbers, llm, book_id, lang, force, j.say, lambda: j.stop_requested,
                                    hooks=hooks, on_chapter=lambda n, state, note: jobs.item(j, n, state, note))
            outlines, notes, roles = studio.annotation_context(book_id)
            done, failed = [], []
            for number in numbers:
                if j.stop_requested:
                    j.say("stopped")
                    break
                jobs.item(j, number, "running")
                result = annotate_chapter_to_disk(config_now, work, number, llm, book_id, lang, outlines, notes, roles,
                                                  force=force, log=j.say, hooks=hooks, chunk_chars=chunk_chars)
                note = f"{len(result.scene_doc['scenes'])} scenes" if result.scene_doc else result.note
                jobs.item(j, number, {"ok": "done", "skip": "skipped", "missing": "error", "error": "error", "stopped": "stopped"}.get(result.status, "error"), note)
                (done if result.status == "ok" else failed if result.status in ("error", "missing") else []).append(number)
            return {"done": done, "failed": failed}

        return jobs.start(book_id, kind, numbers, executor or config_now.llm_default, run,
                          save_record(work, config_now)).summary()

    @app.get("/api/books/{book_id}/jobs/active")
    def active_book_job(book_id: str):
        """重新整理後，讓這一頁接回自己書上還在跑的任務（素材／音樂／語音／輸出頁用）。"""
        job = jobs.latest_for(book_id)
        return {"job": job.summary() if job is not None else None}

    # ---- 任務紀錄
    @app.get("/api/books/{book_id}/job-records")
    def job_records(book_id: str, limit: int = 50):
        return {"records": jobrecords.list_records(ctx.studio.work(book_id), limit)}

    @app.get("/api/books/{book_id}/job-records/{job_id}")
    def job_record(book_id: str, job_id: str):
        record = jobrecords.load_record(ctx.studio.work(book_id), job_id)
        if record is None:
            raise StudioError("job record not found")
        return record

    # ---- 總覽
    @app.get("/api/books/{book_id}/overview")
    def book_overview(book_id: str):
        return overview.book_overview(ctx.studio.work(book_id))

    # ---- 設定集（地點／BGM 註冊表）
    @app.get("/api/books/{book_id}/bible")
    def get_bible(book_id: str):
        return bible.merged_view(ctx.studio.work(book_id))

    @app.put("/api/books/{book_id}/bible/{kind}/{entry_id}")
    async def put_bible_entry(book_id: str, kind: str, entry_id: str, request: Request):
        work = ctx.studio.work(book_id)
        ctx.config.assert_writable(work / bible.REGISTRY_FILE)
        entry = bible.set_entry(work, kind, entry_id, await request.json())
        return {"entry": entry}

    @app.delete("/api/books/{book_id}/bible/{kind}/{entry_id}")
    def delete_bible_entry(book_id: str, kind: str, entry_id: str):
        work = ctx.studio.work(book_id)
        ctx.config.assert_writable(work / bible.REGISTRY_FILE)
        return {"removed": bible.remove_entry(work, kind, entry_id)}

    # ---- 原型風格（style.json）
    @app.get("/api/books/{book_id}/style")
    def get_style(book_id: str):
        path = ctx.studio.work(book_id) / STYLE_REL
        if not path.is_file():
            raise StudioError("no style.json yet; build the prototype first")
        return {"style": style_edit.load_style(path)}

    @app.put("/api/books/{book_id}/style")
    async def put_style(book_id: str, request: Request):
        work = ctx.studio.work(book_id)
        path = work / STYLE_REL
        if not path.is_file():
            raise StudioError("no style.json yet; build the prototype first")
        merged = style_edit.merge_style(style_edit.load_style(path), await request.json())
        ctx.config.assert_writable(path)
        ctx.studio._backup(path, path.parent / "_history")
        style_edit.save_style(path, merged)
        return {"style": merged}

    # ---- 場景檔的歷史版本
    def scene_target(book_id: str, number: int) -> Path:
        work = ctx.studio.work(book_id)
        return work / "scenes" / f"ch{number:03d}.json"

    @app.get("/api/books/{book_id}/chapters/{number}/history")
    def scene_history(book_id: str, number: int):
        return {"history": history.list_history(scene_target(book_id, number))}

    @app.post("/api/books/{book_id}/chapters/{number}/history/restore")
    async def scene_restore(book_id: str, number: int, request: Request):
        target = scene_target(book_id, number)
        ctx.config.assert_writable(target)
        ctx.config.assert_writable(target.parent / history.HISTORY_DIR_NAME / "x")
        name = (await request.json()).get("name", "")
        saved = history.restore_history(target, name)
        return {"backup": saved}

    # ---- 素材（美術）
    def art_work(book_id: str) -> Path:
        return ctx.studio.work(book_id)

    def book_lang(book_id: str) -> str:
        return ctx.studio.book_json(book_id).get("lang", "zh-TW")

    @app.get("/api/books/{book_id}/art/assets")
    def art_assets(book_id: str):
        return art_studio.list_assets(art_work(book_id))

    @app.post("/api/books/{book_id}/art/refresh")
    def art_refresh(book_id: str):
        art_gen.refresh_manifest(ctx.config, art_work(book_id), book_id, book_lang(book_id))
        return art_studio.list_assets(art_work(book_id))

    @app.get("/api/books/{book_id}/art/file")
    def art_file(book_id: str, path: str):
        return FileResponse(art_studio.asset_file(art_work(book_id), path))

    @app.get("/api/books/{book_id}/art/style")
    def art_get_style(book_id: str):
        return art_studio.get_style(art_work(book_id))

    @app.put("/api/books/{book_id}/art/style")
    async def art_put_style(book_id: str, request: Request):
        return art_studio.save_style(ctx.config, art_work(book_id), await request.json())

    @app.get("/api/books/{book_id}/art/appearance")
    def art_appearance_list(book_id: str):
        return art_studio.appearance_items(art_work(book_id), ctx.studio.library_dir(book_id), book_lang(book_id))

    @app.put("/api/books/{book_id}/art/appearance/{kind}/{item_id}")
    async def art_appearance_save(book_id: str, kind: str, item_id: str, request: Request):
        return art_studio.save_appearance(ctx.config, art_work(book_id), kind, item_id, await request.json())

    @app.post("/api/books/{book_id}/art/appearance/{kind}/{item_id}/draft")
    async def art_appearance_draft(book_id: str, kind: str, item_id: str, request: Request):
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        executor = str(body.get("executor") or "").strip() or None
        try:
            llm = get_llm(executor)
        except LlmError as exc:
            raise StudioError(str(exc)) from exc
        try:
            return art_studio.draft_appearance(art_work(book_id), ctx.studio.library_dir(book_id), kind, item_id, llm, book_lang(book_id))
        except LlmError as exc:
            raise StudioError(str(exc)) from exc

    @app.post("/api/books/{book_id}/art/generate")
    async def art_generate(book_id: str, request: Request):
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        work = art_work(book_id)
        config_now = ctx.config
        only = [str(x) for x in body["only"]] if body.get("only") else None
        candidates = int(body["candidates"]) if body.get("candidates") else None
        regenerate = bool(body.get("regenerate"))
        backend = comfy_factory() if comfy_factory else art_gen.VendorBackend(config_now.comfyui_base_url)

        def run(j: Any) -> dict[str, Any]:
            def progress(event: dict[str, Any]) -> None:
                jobs.update(j, phase="streaming", asset=event["asset_id"], index=event["index"], total=event["total"],
                            n=event.get("n"), of=event.get("of"), event=event["event"])
            result = art_studio.run_generation(config_now, work, backend, only=only, regenerate=regenerate, candidates=candidates,
                                               should_stop=lambda: j.stop_requested, on_progress=progress, log=j.say)
            if result.get("stopped"):
                j.request_stop()  # 出圖途中被中斷，任務狀態記為「已停止」
            return result

        return jobs.start(book_id, ART_JOB_KIND, [], "ComfyUI", run, save_record(work, config_now)).summary()

    # ---- 外觀（外型卡、頭像、畫風庫、wildcard）
    def wild_library() -> dict[str, list[str]]:
        return art_cards_studio.load_wildcards(wild_dir)[0]

    @app.get("/api/books/{book_id}/art/cards")
    def cards_list(book_id: str):
        return art_cards_studio.overview(art_work(book_id), ctx.studio.library_dir(book_id), book_lang(book_id))

    @app.put("/api/books/{book_id}/art/cards/{char_id}")
    async def cards_save(book_id: str, char_id: str, request: Request):
        work = art_work(book_id)
        card = art_cards.save_card(ctx.config.assert_writable, work, char_id, await request.json())
        try:  # 勾選的頭像表情要在素材清單裡有對應的列；還沒標註場景時不能掃描，略過即可
            art_gen.refresh_manifest(ctx.config, work, book_id, book_lang(book_id))
        except art_gen.ArtGenError:
            pass
        return card

    @app.post("/api/books/{book_id}/art/cards/roll")
    async def cards_roll(book_id: str, request: Request):
        body = await request.json()
        work = art_work(book_id)
        card, warnings = art_cards.roll_card(body.get("card") or {}, wild_library(), art_cards.load_enabled(work))
        return {"card": card, "warnings": warnings}

    @app.post("/api/books/{book_id}/art/cards/generate")
    async def cards_generate(book_id: str, request: Request):
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        work = art_work(book_id)
        config_now = ctx.config
        lang = book_lang(book_id)
        char_id = str(body.get("char_id") or "")
        headshot, sprite = bool(body.get("headshot")), bool(body.get("sprite"))
        candidates = int(body.get("candidates") or 1)
        if not char_id:
            raise StudioError("沒有選角色")
        if not (headshot or sprite):
            raise StudioError("頭像與立繪至少要勾一個")
        if not 1 <= candidates <= 16:
            raise StudioError("張數必須在 1 到 16 之間")
        wild, enabled = wild_library(), art_cards.load_enabled(work)
        backend = comfy_factory() if comfy_factory else art_gen.VendorBackend(config_now.comfyui_base_url)

        def run(j: Any) -> dict[str, Any]:
            def progress(event: dict[str, Any]) -> None:
                jobs.update(j, phase="streaming", asset=event.get("asset_id") or event.get("char_id"), index=event["index"],
                            total=event["total"], n=event.get("n"), of=event.get("of"), event=event["event"])
            result = art_cards_studio.run_cards_generation(
                config_now, work, backend, book_id=book_id, lang=lang, char_id=char_id, headshot=headshot, sprite=sprite,
                candidates=candidates, wild=wild, enabled=enabled, should_stop=lambda: j.stop_requested,
                on_progress=progress, log=j.say)
            if result.get("stopped"):
                j.request_stop()
            return result

        return jobs.start(book_id, ART_JOB_KIND, [], "ComfyUI", run, save_record(work, config_now)).summary()

    @app.post("/api/books/{book_id}/art/cards/{char_id}/draft")
    async def cards_draft(book_id: str, char_id: str, request: Request):
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        executor = str(body.get("executor") or "").strip() or None
        try:
            llm = get_llm(executor)
        except LlmError as exc:
            raise StudioError(str(exc)) from exc
        work = art_work(book_id)
        try:
            return art_cards_studio.draft(work, ctx.studio.library_dir(book_id), book_lang(book_id), char_id, wild_library(),
                                          art_cards.load_enabled(work), llm)
        except LlmError as exc:
            raise StudioError(str(exc)) from exc

    @app.get("/api/books/{book_id}/art/wildcards")
    def wildcards_list(book_id: str):
        library, warnings = art_cards_studio.load_wildcards(wild_dir)
        return {"wildcards": art_cards.wildcard_overview(library, art_cards.load_enabled(art_work(book_id))), "warnings": warnings}

    @app.put("/api/books/{book_id}/art/wildcards/enabled")
    async def wildcards_enable(book_id: str, request: Request):
        names = [str(x) for x in (await request.json()).get("names") or []]
        library = wild_library()
        enabled = art_cards.save_enabled(ctx.config.assert_writable, art_work(book_id), names, library)
        return {"enabled": enabled, "wildcards": art_cards.wildcard_overview(library, enabled)}

    @app.get("/api/books/{book_id}/art/styles")
    def styles_list(book_id: str):
        return {"styles": art_cards_studio.load_catalog(ctx.config.project_root),
                "current": art_cards.style_state(art_work(book_id))["style_id"]}

    @app.post("/api/books/{book_id}/art/styles/import")
    async def styles_import(book_id: str, request: Request):
        path = str((await request.json()).get("path", "")).strip().strip('"')
        if not path.lower().endswith(art_cards_studio.XLSX_SUFFIXES):
            raise StudioError("只能匯入 .xlsx 畫風表")
        count = art_cards.import_styles(ctx.config.assert_writable, path, art_cards_studio.catalog_path(ctx.config.project_root))
        return {"count": count}

    @app.post("/api/books/{book_id}/art/styles/upload")
    async def styles_upload(book_id: str, request: Request):
        """上傳畫風表：請求本體就是 .xlsx 檔案內容，檔名在 ``X-Filename`` 標頭。"""
        name = unquote(request.headers.get("x-filename", ""))
        if not name.lower().endswith(art_cards_studio.XLSX_SUFFIXES):
            raise StudioError("只能匯入 .xlsx 畫風表")
        data = await request.body()
        if not data:
            raise StudioError("檔案是空的")
        scratch = ctx.config.project_root / "config" / "_upload_styles.xlsx"
        ctx.config.assert_writable(scratch)
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_bytes(data)
        try:
            count = art_cards.import_styles(ctx.config.assert_writable, scratch, art_cards_studio.catalog_path(ctx.config.project_root))
        except zipfile.BadZipFile as exc:
            raise StudioError("這不是有效的 xlsx 檔案") from exc
        finally:
            scratch.unlink(missing_ok=True)
        return {"count": count}

    @app.post("/api/books/{book_id}/art/candidates/delete")
    async def art_candidates_delete(book_id: str, request: Request):
        body = await request.json()
        asset_id = str(body.get("asset_id", ""))
        work = art_work(book_id)
        manifest = art_gen.load_manifest(work)
        asset = next((a for a in (manifest or {}).get("assets", []) if a["asset_id"] == asset_id), None)
        if asset is None or asset["kind"] not in art_gen.GEN_KINDS:
            raise StudioError(f"找不到素材：{asset_id}")
        protect = [c["name"] for c in art_studio.candidates_with_selected(work, asset) if c["selected"]]
        try:
            result = candidate_files.delete_candidates(ctx.config.assert_writable, art_gen.candidate_dir(work, asset_id),
                                                       [str(n) for n in body.get("names") or []], protect)
        except ValueError as exc:
            raise StudioError(str(exc)) from exc
        if asset["status"] == "generated" and not art_gen.list_candidates(work, asset_id):
            art_gen.set_status(ctx.config, work, asset_id, "planned")  # 全刪光 → 回到「規劃中」
        return result

    @app.post("/api/books/{book_id}/art/styles/apply")
    async def styles_apply(book_id: str, request: Request):
        style_id = str((await request.json()).get("style_id", ""))
        return art_cards.apply_style(ctx.config.assert_writable, art_work(book_id), art_cards_studio.load_catalog(ctx.config.project_root), style_id)

    @app.post("/api/books/{book_id}/art/styles/clear")
    def styles_clear(book_id: str):
        return art_cards.clear_style(ctx.config.assert_writable, art_work(book_id))

    @app.get("/api/books/{book_id}/art/candidates/{asset_id}")
    def art_candidates(book_id: str, asset_id: str):
        return {"candidates": art_gen.list_candidates(art_work(book_id), asset_id)}

    @app.post("/api/books/{book_id}/art/select")
    async def art_select(book_id: str, request: Request):
        body = await request.json()
        return art_gen.select_candidate(ctx.config, art_work(book_id), str(body.get("asset_id", "")), str(body.get("name", "")))

    # ---- 音樂（BGM／環境音：YuE2）
    @app.get("/api/books/{book_id}/art/audio")
    def audio_overview(book_id: str):
        return audio_gen.overview(art_work(book_id))

    @app.get("/api/books/{book_id}/art/audio/file")
    def audio_file(book_id: str, path: str):
        return FileResponse(audio_gen.audio_file(art_work(book_id), path))

    @app.put("/api/books/{book_id}/art/audio/{asset_id}/prompt")
    async def audio_save_prompt(book_id: str, asset_id: str, request: Request):
        body = await request.json()
        audio_gen.save_prompt(ctx.config, art_work(book_id), asset_id, str(body.get("style", "")).strip(), str(body.get("lyrics", "")).strip())
        return {"ok": True}

    @app.post("/api/books/{book_id}/art/audio/{asset_id}/draft")
    async def audio_draft(book_id: str, asset_id: str, request: Request):
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        try:
            llm = get_llm(str(body.get("executor") or "").strip() or None)
            fields, warnings = audio_gen.draft_prompt(ctx.config, art_work(book_id), asset_id, llm)
        except LlmError as exc:
            raise StudioError(str(exc)) from exc
        return {"prompt": fields, "warnings": warnings}

    @app.post("/api/books/{book_id}/art/audio/generate")
    async def audio_generate(book_id: str, request: Request):
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        work = art_work(book_id)
        config_now = ctx.config
        only = [str(x) for x in body["only"]] if body.get("only") else None
        candidates = int(body.get("candidates") or 1)
        regenerate = bool(body.get("regenerate"))
        backend = audio_backend_factory() if audio_backend_factory else audio_gen.VendorAudioBackend(config_now.comfyui_base_url)

        def run(j: Any) -> dict[str, Any]:
            def progress(event: dict[str, Any]) -> None:
                jobs.update(j, phase="streaming", asset=event["asset_id"], index=event["index"], total=event["total"],
                            n=event.get("n"), of=event.get("of"), event=event["event"])
            report = audio_gen.generate(config_now, work, backend, candidates=candidates, only=only, regenerate=regenerate,
                                        should_stop=lambda: j.stop_requested, progress=progress)
            for asset_id, reason in report.failed.items():
                j.say(f"failed: {asset_id}: {reason}")
            if report.stopped:
                j.request_stop()
            return {"generated": report.generated, "skipped": report.skipped, "failed": report.failed, "stopped": report.stopped}

        return jobs.start(book_id, ART_JOB_KIND, [], "ComfyUI", run, save_record(work, config_now)).summary()

    @app.post("/api/books/{book_id}/art/audio/select")
    async def audio_select(book_id: str, request: Request):
        body = await request.json()
        return audio_gen.select_candidate(ctx.config, art_work(book_id), str(body.get("asset_id", "")), str(body.get("name", "")),
                                          runner=audio_runner or audio_gen.run_ffmpeg, loop=bool(body.get("loop", True)))

    @app.post("/api/books/{book_id}/art/audio/scan")
    def audio_scan_route(book_id: str):
        return audio_gen.scan(ctx.config, art_work(book_id))

    # ---- 語音（MOSS-TTS：聲線設定、逐句生成、VoiceMap）
    def tts_cast_doc(work: Path) -> dict[str, Any] | None:
        path = work / "cast.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def tts_chapters(work: Path, body: dict[str, Any]) -> list[int]:
        chosen = body.get("chapters")
        if chosen:
            return sorted({int(x) for x in chosen})
        return sorted({voice_lines.chapter_number(str(doc.get("chapter_id") or ""))
                       for doc in voice_lines.load_scene_docs(work)})

    def start_tts(book_id: str, body: dict[str, Any]) -> dict[str, Any]:
        work = art_work(book_id)
        chapters = tts_chapters(work, body)
        if not chapters:
            raise StudioError("這本書還沒有 scene graph，先標註章節")
        doc = voice_lines.load_cast_voice(work)
        if doc is None:
            raise StudioError("還沒有 voice/cast_voice.json，先替角色設定聲線")
        errors = voice_lines.validate_cast_voice(doc, work, tts_cast_doc(work))
        if errors:
            raise StudioError("cast_voice.json 有問題：\n" + "\n".join(errors))
        only = [str(x) for x in body["only"]] if body.get("only") else None
        regenerate = bool(body.get("regenerate"))
        design_only = bool(body.get("design_only"))
        config_now = ctx.config
        backend = tts_backend_factory() if tts_backend_factory else voice_gen.VendorTtsBackend(config_now.comfyui_base_url)

        def run(j: Any) -> dict[str, Any]:
            def progress(event: dict[str, Any]) -> None:
                update = {"phase": "streaming", "event": event.get("event"), "index": event.get("index"),
                          "total": event.get("total"), "line_id": event.get("line_id"),
                          "speaker": event.get("speaker"), "char_id": event.get("char_id")}
                jobs.update(j, {k: v for k, v in update.items() if v is not None})
                line_id = event.get("line_id")
                if not line_id:
                    return
                chapter = int(voice_lines.chapter_of(line_id)[2:])
                if chapter not in chapters:
                    return
                jobs.item(j, chapter, note=line_id)
                if event.get("event") == "voice":
                    jobs.item(j, chapter, "running")
            report = voice_gen.generate(
                config_now, work, backend, chapters=chapters, only=only, regenerate=regenerate,
                include_narration=bool(body.get("include_narration")), design_only=design_only,
                should_stop=lambda: j.stop_requested, progress=progress,
                runner=tts_runner or voice_gen.run_ffmpeg)
            for line_id, reason in report.failed.items():
                j.say(f"failed: {line_id}: {reason}")
            for chapter in chapters:
                prefix = f"ch{chapter:03d}_"
                jobs.item(j, chapter, "error" if any(x.startswith(prefix) for x in report.failed) else "done")
            if report.stopped:
                j.request_stop()
            return {"generated": report.generated, "skipped": report.skipped, "failed": report.failed,
                    "designed": report.designed, "unvoiced": report.unvoiced, "stopped": report.stopped}

        return jobs.start(book_id, "tts", chapters, tts_executor(doc), run, save_record(work, config_now)).summary()

    @app.get("/api/books/{book_id}/tts")
    def tts_overview(book_id: str):
        work = art_work(book_id)
        return voice_gen.overview(work, tts_cast_doc(work))

    @app.get("/api/books/{book_id}/tts/cast")
    def tts_get_cast(book_id: str):
        work = art_work(book_id)
        doc = voice_lines.load_cast_voice(work)
        if doc is None:
            doc = {"schema_version": voice_lines.SCHEMA_VERSION, "book_id": book_id,
                   "engine": voice_lines.DEFAULT_ENGINE, "characters": {}}
            return {"exists": False, "cast_voice": doc, "path": None}
        return {"exists": True, "cast_voice": doc,
                "path": voice_lines.cast_voice_path(work).relative_to(work).as_posix()}

    @app.get("/api/books/{book_id}/tts/models")
    def tts_models(book_id: str):
        work = art_work(book_id)
        doc = voice_lines.load_cast_voice(work) or {}
        return {"models": sherpa_tts.list_models(),
                "current": doc.get("sherpa_model") or "",
                "root": "models/sherpa"}

    @app.get("/api/books/{book_id}/tts/speakers")
    def tts_speakers(book_id: str, model: str = ""):
        work = art_work(book_id)
        doc = voice_lines.load_cast_voice(work) or {}
        return sherpa_tts.speaker_catalog(model or doc.get("sherpa_model"))

    @app.put("/api/books/{book_id}/tts/cast")
    async def tts_put_cast(book_id: str, request: Request):
        work = art_work(book_id)
        body = await request.json()
        doc = body.get("cast_voice") if isinstance(body.get("cast_voice"), dict) else body
        errors = voice_lines.validate_cast_voice(doc, work, tts_cast_doc(work))
        if errors:
            raise StudioError("cast_voice.json 有問題：\n" + "\n".join(errors))
        path = voice_lines.save_cast_voice(ctx.config, work, doc)
        return {"ok": True, "path": path.relative_to(work).as_posix()}

    @app.get("/api/books/{book_id}/tts/file")
    def tts_file(book_id: str, path: str):
        return FileResponse(voice_gen.audio_file(art_work(book_id), path))

    @app.post("/api/books/{book_id}/tts/sample")
    async def tts_sample(book_id: str, request: Request):
        """用目前模型即時生成一句範例（不寫 VoiceMap），回傳 data URL 供試聽。"""
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        work = art_work(book_id)
        doc = voice_lines.load_cast_voice(work) or {}
        text = str((body or {}).get("text") or "").strip()
        if not text:
            raise StudioError("text is required")
        try:
            speaker_id = int((body or {}).get("speaker_id") or 0)
            speed = float((body or {}).get("speed") or 1.0)
            model = str((body or {}).get("model") or doc.get("sherpa_model") or "").strip() or None
            wav = sherpa_tts.SherpaTts(model).wav_bytes(text, speaker_id=speaker_id, speed=speed)
        except (TypeError, ValueError, sherpa_tts.SherpaTtsError) as exc:
            raise StudioError(f"範例語音失敗：{exc}") from exc
        return {"audio": "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")}

    @app.post("/api/books/{book_id}/tts/generate")
    async def tts_generate(book_id: str, request: Request):
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        return start_tts(book_id, body or {})

    @app.post("/api/books/{book_id}/tts/design")
    async def tts_design(book_id: str, request: Request):
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        return start_tts(book_id, {**(body or {}), "design_only": True})

    @app.post("/tts/generate")
    async def tts_generate_compat(request: Request):
        """任務卡指定的端點；book_id 放在 body。"""
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        book_id = str((body or {}).get("book_id") or "").strip()
        if not book_id:
            raise StudioError("book_id is required")
        payload = {k: v for k, v in body.items() if k != "book_id"}
        return start_tts(book_id, payload)

    # ---- 輸出：標題素材、匯出、自動驗收
    @app.get("/api/books/{book_id}/export")
    def export_overview(book_id: str):
        return export_studio.overview(art_work(book_id), book_id, ctx.studio.chapters(book_id))

    @app.put("/api/books/{book_id}/export/settings")
    async def export_save_settings(book_id: str, request: Request):
        body = await request.json()
        return export_studio.save_settings(ctx.config, art_work(book_id), book_id, str(body.get("sdk", "")), str(body.get("project", "")))

    @app.put("/api/books/{book_id}/export/title")
    async def export_save_title(book_id: str, request: Request):
        body = await request.json()
        return export_studio.save_title_prompt(ctx.config, art_work(book_id), str(body.get("tags", "")), str(body.get("description", "")))

    def start_export_job(book_id: str, label: str, work_fn: Callable[[], dict[str, Any]]):
        work = art_work(book_id)
        config_now = ctx.config

        def run(_job: Any) -> dict[str, Any]:
            return work_fn()

        return jobs.start(book_id, label, [], "Ren'Py", run, save_record(work, config_now)).summary()

    @app.post("/api/books/{book_id}/export/run")
    async def export_run(book_id: str, request: Request):
        body = await request.json()
        numbers = [int(n) for n in body.get("chapters") or []]
        work = art_work(book_id)
        return start_export_job(book_id, "export", lambda: export_studio.run_export(
            ctx.config, work, book_id, numbers, placeholders=bool(body.get("placeholders")), clean=bool(body.get("clean"))))

    @app.post("/api/books/{book_id}/export/verify")
    def export_verify(book_id: str):
        work = art_work(book_id)
        return start_export_job(book_id, "verify", lambda: export_studio.run_verify(ctx.config, work, book_id))

    @app.post("/api/books/{book_id}/export/package")
    def export_package(book_id: str):
        work = art_work(book_id)
        return start_export_job(book_id, "package", lambda: export_studio.run_package(ctx.config, work, book_id))

    @app.get("/api/books/{book_id}/export/report")
    def export_report(book_id: str, name: str):
        return {"name": name, "text": export_studio.read_report(art_work(book_id), name)}

    @app.get("/api/books/{book_id}/export/shot")
    def export_shot(book_id: str, report: str, name: str):
        return FileResponse(export_studio.report_shot(art_work(book_id), report, name))

    @app.post("/api/books/{book_id}/art/status")
    async def art_set_status(book_id: str, request: Request):
        body = await request.json()
        return art_gen.set_status(ctx.config, art_work(book_id), str(body.get("asset_id", "")), str(body.get("status", "")), body.get("license"))

    # ---- 原型
    @app.post("/api/books/{book_id}/prototype")
    async def build_prototype(book_id: str, request: Request):
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        return ctx.studio.build_prototype(book_id, [int(n) for n in body.get("chapters") or []])

    @app.get("/api/books/{book_id}/prototype/{name}")
    def prototype_file(book_id: str, name: str):
        work = ctx.studio.work(book_id)
        if "/" in name or "\\" in name or not name.endswith((".html", ".json")):
            raise StudioError("invalid file name")
        path = work / "prototype" / name
        if not path.is_file():
            raise StudioError("not built yet")
        return FileResponse(path)

    @app.get("/")
    def index():
        return fe.page(TEMPLATES / "index.html")

    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")
    for sub in ("css", "fe", "vendor"):
        if (FEKIT_DIR / sub).is_dir():
            app.mount("/fekit/" + sub, StaticFiles(directory=FEKIT_DIR / sub), name="fekit-" + sub)
    return app


def _payload(body: dict[str, Any]) -> dict[str, Any]:
    """前端送來的 {files:[{name,data}], text, path}；path 是本機路徑（服務只在本機，讀取只限副檔名白名單）。"""
    payload: dict[str, Any] = {"files": list(body.get("files") or []), "text": body.get("text") or ""}
    for raw in body.get("paths") or []:
        path = Path(raw).expanduser()
        if path.suffix.lower() not in ALLOWED_EXTS or not path.is_file():
            raise StudioError(f"not a supported file: {raw}")
        payload["files"].append({"name": path.name, "data": base64.b64encode(path.read_bytes()).decode("ascii")})
    return payload


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.server [--config config.yaml] [--port 7866] [--no-browser]``"""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Local web studio for reviewing and editing visual-novel annotations.")
    parser.add_argument("--config", default=None)
    parser.add_argument("--port", type=int, default=None, help="default: server.port in config.yaml (7866)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except T2vnError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    import uvicorn

    port = args.port or config.server_port
    url = f"http://127.0.0.1:{port}/"
    print(f"studio: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    from .config import DEFAULT_CONFIG_PATH

    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    uvicorn.run(create_app(config, config_path=config_path, port=port), host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
