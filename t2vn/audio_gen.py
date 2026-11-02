"""BGM／環境音生成管線（S7）：擬提示詞 → YuE2 出候選 → 挑選後轉成遊戲用 opus。

候選音檔放 ``assets/_candidates/<asset_id>/<seed>.<ext>``（旁邊 ``<seed>.json`` 記提示詞與種子）；
提示詞存在 ``art.json`` 的 ``audio.<asset_id>``（``style``、``lyrics``）。
ComfyUI、AI、ffmpeg 都由呼叫端注入，測試用假的。純邏輯在 ``audio_prompts``／``audio_workflow``／
``audio_ffmpeg``／``audio_scan``。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from . import art_gen, audio_ffmpeg, audio_prompts, audio_scan, audio_workflow
from .errors import JobStopped, T2vnError
from .llm import LlmFn, call_llm

DEFAULT_TEMPLATE = art_gen.REPO_ROOT / "workflows" / "yue2_bgm_api.json"
AUDIO_KINDS = audio_prompts.KINDS
CANDIDATE_EXTS = (".opus", ".ogg", ".mp3", ".flac", ".wav")
DEFAULT_SECONDS = {"bgm": 120, "ambience": 90}
LOOP_SECONDS = {"bgm": 90.0, "ambience": 60.0}   # 轉檔時循環成品長度上限（需比來源短）
LICENSE_TEXT = "self-generated (YuE2)"


class AudioGenError(T2vnError):
    """音樂設定或資料有問題。"""


class AudioFailure(T2vnError):
    """單首音樂失敗（ComfyUI 回報的錯誤）；該項記進 failed，其餘繼續。"""


class AudioBackend(Protocol):
    """``run`` 送出 workflow 並等待，回傳 ``[(副檔名含點, bytes)]``。"""

    def template(self) -> dict[str, Any]: ...
    def run(self, workflow: dict[str, Any], should_stop: Callable[[], bool]) -> list[tuple[str, bytes]]: ...


class VendorAudioBackend:
    """包裝 ``vendor.novel_studio.comfy``：送 workflow、輪詢歷史、用 ``/view`` 取音檔。"""

    def __init__(self, base_url: str, template_path: Path | str | None = None, timeout: int = 3600) -> None:
        from vendor.novel_studio import comfy

        self.comfy = comfy
        self.base_url = base_url
        self.template_path = Path(template_path) if template_path else DEFAULT_TEMPLATE
        self.timeout = timeout

    def template(self) -> dict[str, Any]:
        try:
            return json.loads(self.template_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AudioGenError(f"cannot load workflow {self.template_path}: {exc}") from exc

    def run(self, workflow: dict[str, Any], should_stop: Callable[[], bool]) -> list[tuple[str, bytes]]:
        import urllib.parse

        comfy, base = self.comfy, self.base_url
        try:
            pid = comfy.submit(base, workflow)
            start = time.time()
            while True:
                if should_stop():
                    comfy.interrupt(base, pid)
                    raise JobStopped("stopped")
                entry = comfy.history(base, pid)
                if entry:
                    err = comfy.status_error(entry)
                    if err:
                        raise AudioFailure(err)
                    if (entry.get("status") or {}).get("completed") or entry.get("outputs"):
                        break
                if time.time() - start > self.timeout:
                    comfy.interrupt(base, pid)
                    raise AudioFailure(f"等太久了（超過 {self.timeout // 60} 分鐘）")
                time.sleep(1.0)
            files = audio_workflow.output_files(entry.get("outputs"))
            if not files:
                raise AudioFailure("ComfyUI 完成了，但沒有輸出音檔")
            result = []
            for item in files:
                query = urllib.parse.urlencode({k: item[k] for k in ("filename", "subfolder", "type")})
                result.append((Path(item["filename"]).suffix.lower() or ".opus",
                               comfy._req(base, "/view?" + query, timeout=300, raw=True)))
            return result
        except comfy.ComfyError as exc:
            raise AudioFailure(str(exc)) from exc


# ------------------------------------------------------------------ 資料

def _entries(work: Path, kinds: tuple[str, ...] = AUDIO_KINDS) -> list[dict[str, Any]]:
    manifest = art_gen.load_manifest(work)
    if manifest is None:
        raise AudioGenError("manifest.json not found; refresh the manifest first")
    return [a for a in manifest["assets"] if a["kind"] in kinds]


def _title(value: Any) -> str:
    if isinstance(value, dict):
        return str(next(iter(value.values()), "")) if value else ""
    return str(value or "")


def scenes_for(work: Path, asset: dict[str, Any]) -> list[dict[str, str]]:
    """用到這個音檔的場景，攤平成 ``audio_prompts.build_prompt`` 要的欄位。"""
    field = "bgm" if asset["kind"] == "bgm" else "ambience"
    out: list[dict[str, str]] = []
    for doc in art_gen.load_scene_docs(work):
        for scene in doc.get("scenes") or []:
            if scene.get(field) == asset["asset_id"]:
                out.append({"scene_id": str(scene.get("scene_id", "")), "title": _title(scene.get("title")),
                            "location": str(scene.get("location_id", "")), "time_of_day": str(scene.get("time_of_day", "")),
                            "summary": str(scene.get("summary", ""))})
    return out


def load_prompts(work: Path) -> dict[str, dict[str, str]]:
    return dict(art_gen.load_art(work).get("audio") or {})


def save_prompt(config: Any, work: Path, asset_id: str, style: str, lyrics: str) -> None:
    art = art_gen.load_art(work)
    art.setdefault("audio", {})[asset_id] = {"style": style, "lyrics": lyrics}
    art_gen.save_art(config, work, art)


def draft_prompt(config: Any, work: Path, asset_id: str, llm: LlmFn, retries: int = 1) -> tuple[dict[str, str], list[str]]:
    """請 AI 擬一首的曲風＋歌詞（純配樂），存進 ``art.json``；回傳 ``(提示詞, 警告)``。"""
    asset = next((a for a in _entries(work) if a["asset_id"] == asset_id), None)
    if asset is None:
        raise AudioGenError(f"{asset_id}: not a bgm/ambience asset in the manifest")
    entry = {"id": asset_id, "label": _title(asset.get("names")), "note": asset.get("note", ""),
             "prompt": asset.get("prompt_en", "")}
    current = load_prompts(work).get(asset_id)
    base = audio_prompts.build_prompt(asset["kind"], entry, scenes_for(work, asset), current)
    prompt, last = base, ""
    for attempt in range(max(0, retries) + 1):
        text = call_llm(llm, [{"role": "user", "content": prompt}], None, attempt + 1).text
        try:
            fields, warns = audio_prompts.parse_reply(text, asset["kind"])
        except ValueError as exc:
            last = str(exc)
            prompt = base + f"\n\nYour previous reply was rejected: {last}. Reply again with only the JSON object."
            continue
        save_prompt(config, work, asset_id, fields["style"], fields["lyrics"])
        return fields, warns
    raise AudioGenError(f"{asset_id}: AI 回覆無法解析：{last}")


# ------------------------------------------------------------------ 候選

def candidate_dir(work: Path, asset_id: str) -> Path:
    return art_gen.candidate_dir(work, asset_id)


def list_candidates(work: Path, asset_id: str) -> list[dict[str, Any]]:
    folder = candidate_dir(work, asset_id)
    out = []
    for path in sorted(folder.iterdir()) if folder.is_dir() else []:
        if path.suffix.lower() in CANDIDATE_EXTS:
            meta = art_gen._read_json(path.with_suffix(".json"), {})
            out.append({"name": path.name, "seed": meta.get("seed"), "file": path.relative_to(work).as_posix(), "meta": meta})
    return out


def _seed(asset_id: str, attempt: int) -> int:
    import zlib

    return (zlib.crc32(asset_id.encode()) * 7919 + attempt * 104729) % (2**31)


def generate(config: Any, work: Path, backend: AudioBackend, *, candidates: int = 1, only: list[str] | None = None,
             regenerate: bool = False, should_stop: Callable[[], bool] = lambda: False,
             progress: Callable[[dict[str, Any]], None] | None = None) -> art_gen.GenReport:
    """逐首出候選。沒有提示詞的項目記進 ``failed``（請先擬稿）。``regenerate`` 同圖片：再加 N 首。"""
    emit = progress or (lambda event: None)
    if candidates < 1 or candidates > 8:
        raise AudioGenError("candidates must be between 1 and 8")
    manifest = art_gen.load_manifest(work)
    if manifest is None:
        raise AudioGenError("manifest.json not found; refresh the manifest first")
    wanted_ids = set(only) if only else None
    todo = [a for a in manifest["assets"] if a["kind"] in AUDIO_KINDS
            and (a["asset_id"] in wanted_ids if wanted_ids else a["status"] == "planned")]
    report = art_gen.GenReport()
    prompts = load_prompts(work)
    template = backend.template() if todo else {}
    for index, asset in enumerate(todo, 1):
        asset_id = asset["asset_id"]
        if should_stop():
            report.stopped = True
            break
        fields = prompts.get(asset_id) or {}
        if not fields.get("style"):
            report.failed[asset_id] = "還沒有提示詞，請先擬稿"
            continue
        folder = candidate_dir(work, asset_id)
        have = len(list_candidates(work, asset_id))
        need = candidates if regenerate else max(0, candidates - have)
        made, attempt, stopped = 0, (have if regenerate else 0), False
        known = {c["seed"] for c in list_candidates(work, asset_id)}
        try:
            while made < need:
                seed = _seed(asset_id, attempt)
                attempt += 1
                if seed in known:
                    continue
                if should_stop():
                    stopped = True
                    break
                emit({"event": "audio", "asset_id": asset_id, "index": index, "total": len(todo), "n": have + made + 1, "of": have + need})
                workflow = audio_workflow.fill_workflow(
                    template, style=fields["style"], lyrics=fields["lyrics"], seed=seed,
                    max_duration=DEFAULT_SECONDS[asset["kind"]], filename_prefix=f"audio/{asset_id}")
                files = backend.run(workflow, should_stop)
                if not files:
                    raise AudioFailure("ComfyUI returned no audio")
                suffix, data = files[0]
                art_gen._write_bytes(config, folder / f"{seed}{suffix}", data)
                art_gen._write_json(config, folder / f"{seed}.json", {"seed": seed, "asset_id": asset_id, "style": fields["style"],
                                                                      "lyrics": fields["lyrics"]})
                made += 1
        except JobStopped:
            stopped = True
        except (AudioFailure, ValueError) as exc:
            report.failed[asset_id] = str(exc)
        if list_candidates(work, asset_id) and asset_id not in report.failed and not stopped:
            if asset["status"] == "planned":
                asset["status"] = "generated"
            (report.generated if made else report.skipped).append(asset_id)
        art_gen._write_json(config, work / "manifest.json", manifest)
        emit({"event": "asset_done", "asset_id": asset_id, "index": index, "total": len(todo)})
        if stopped:
            report.stopped = True
            break
    return report


# ------------------------------------------------------------------ 挑選

def run_ffmpeg(command: list[str]) -> tuple[int, str]:
    """預設的指令執行器；找不到 ffmpeg 回 ``(127, 說明)``。"""
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except FileNotFoundError:
        return 127, "找不到 ffmpeg，請先安裝並加入 PATH"
    return done.returncode, (done.stderr or done.stdout or "").strip()


def select_candidate(config: Any, work: Path, asset_id: str, name: str,
                     runner: Callable[[list[str]], tuple[int, str]] = run_ffmpeg, loop: bool = True) -> dict[str, Any]:
    """候選 ``name`` → 轉成 ``assets/<kind>/<asset_id>.opus``（正規化音量，``loop`` 時做無縫循環），
    狀態改 ``processed``，授權空白時填 ``self-generated (YuE2)``。"""
    manifest = art_gen.load_manifest(work)
    asset = next((a for a in (manifest or {}).get("assets", []) if a["asset_id"] == asset_id and a["kind"] in AUDIO_KINDS), None)
    if asset is None:
        raise AudioGenError(f"{asset_id}: not a bgm/ambience asset in the manifest")
    if "/" in name or "\\" in name or Path(name).suffix.lower() not in CANDIDATE_EXTS:
        raise AudioGenError(f"invalid candidate name {name!r}")
    source = candidate_dir(work, asset_id) / name
    if not source.is_file():
        raise AudioGenError(f"candidate {name} of {asset_id} does not exist")
    target = work / "assets" / audio_scan.FOLDERS[asset["kind"]] / f"{asset_id}.opus"
    config.assert_writable(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    for old in target.parent.glob(f"{asset_id}.*"):   # 換檔時清掉同 id 的舊音檔（保留旁註 json）
        if old.suffix.lower() in CANDIDATE_EXTS and old != target:
            config.assert_writable(old)
            old.unlink()
    seconds = LOOP_SECONDS[asset["kind"]] if loop else None
    code, text = runner(audio_ffmpeg.convert_command(source, target, loop_duration=seconds))
    if code != 0 or not target.is_file():
        raise AudioGenError(f"{asset_id}: ffmpeg 轉檔失敗：{text[:300]}")
    asset["file"] = target.relative_to(work).as_posix()
    asset["status"] = "processed"
    meta = art_gen._read_json(source.with_suffix(".json"), {})
    if meta.get("seed") is not None:
        asset["seed"] = meta["seed"]
    if not asset.get("license"):
        asset["license"] = LICENSE_TEXT
    art_gen._write_json(config, work / "manifest.json", manifest)
    return asset


# ------------------------------------------------------------------ 給網頁

def overview(work: Path) -> dict[str, Any]:
    """音樂頁要的資料：每個 bgm／ambience 項目帶提示詞、候選、用到它的場景數、現有成品。"""
    manifest = art_gen.load_manifest(work)
    if manifest is None:
        return {"ready": False, "items": []}
    prompts = load_prompts(work)
    items = []
    for asset in manifest["assets"]:
        if asset["kind"] not in AUDIO_KINDS:
            continue
        item = dict(asset)
        item["prompt"] = prompts.get(asset["asset_id"]) or {"style": "", "lyrics": ""}
        item["candidates"] = list_candidates(work, asset["asset_id"])
        item["scene_count"] = len(scenes_for(work, asset))
        item["has_file"] = any(f.is_file() for f in (work / "assets" / audio_scan.FOLDERS[asset["kind"]]).glob(f"{asset['asset_id']}.*")
                               if f.suffix.lower() in CANDIDATE_EXTS)
        items.append(item)
    return {"ready": True, "items": items}


def audio_file(work: Path, rel: str) -> Path:
    """網頁試聽用：只准 ``assets/`` 底下的音檔，不准跳出資料夾。"""
    base = (work / "assets").resolve()
    target = (work / rel).resolve()
    if target.suffix.lower() not in CANDIDATE_EXTS or base not in target.parents or not target.is_file():
        raise AudioGenError("audio not found")
    return target


def scan(config: Any, work: Path) -> dict[str, Any]:
    """掃描 ``assets/bgm|ambience`` 裡手動放入的音檔，同步 manifest 狀態與授權；回傳 ``{updated, orphans}``。"""
    manifest = art_gen.load_manifest(work)
    if manifest is None:
        raise AudioGenError("manifest.json not found; refresh the manifest first")
    root = work / "assets"
    results = audio_scan.scan_audio(root, manifest["assets"])
    new_assets = audio_scan.apply_scan(manifest["assets"], results)
    by_id = {r["asset_id"]: r for r in results}
    updated = []
    for old, new in zip(manifest["assets"], new_assets):
        res = by_id.get(old["asset_id"])
        if res and res["file"] and not (work / old.get("file", "")).is_file():
            new["file"] = f"assets/{res['file']}"
        if old != new:
            updated.append(old["asset_id"])
    manifest["assets"] = new_assets
    art_gen._write_json(config, work / "manifest.json", manifest)
    return {"updated": updated, "orphans": audio_scan.find_orphans(root, new_assets)}
