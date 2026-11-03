"""語音生成管線（MOSS-TTS／ComfyUI）：聲線設計 → 逐句合成 → OGG 轉檔 → VoiceMap。

規格見 ``docs/33_語音管線規格.md``。ComfyUI、ffmpeg 都由呼叫端注入，測試用假的後端；
純邏輯在 ``voice_lines``／``voice_workflow``／``voice_ffmpeg``。

用法::

    python -m t2vn.voice_gen --book sample_book --chapters 1-2
    python -m t2vn.voice_gen --book sample_book --chapters 1 --only ch001_sc02_l003 --force
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from . import sherpa_tts, voice_ffmpeg, voice_lines, voice_workflow
from .errors import JobStopped, T2vnError

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLONE_TEMPLATE = REPO_ROOT / "workflows" / "moss_tts_clone_api.json"
DEFAULT_DESIGN_TEMPLATE = REPO_ROOT / "workflows" / "moss_tts_design_api.json"
DESIGN_DIR = "voice/refs"
TEMP_DIR = "voice/_tmp"
DEFAULT_TIMEOUT = 1800


class VoiceGenError(T2vnError):
    """語音設定或資料有問題（不是單句失敗）。"""


class VoiceFailure(T2vnError):
    """單句／單個設計參考音失敗；記進 failed，其餘繼續。"""


class TtsBackend(Protocol):
    """``template`` 取 workflow 樣板；``upload_reference`` 把參考音放進 ComfyUI；
    ``run`` 送出 workflow 並回傳 ``[(副檔名含點, bytes)]``。"""

    def template(self, kind: str) -> dict[str, Any]: ...

    def upload_reference(self, path: Path, name: str) -> str: ...

    def run(self, workflow: dict[str, Any], should_stop: Callable[[], bool]) -> list[tuple[str, bytes]]: ...


class VendorTtsBackend:
    """包裝 ``vendor.novel_studio.comfy``：送 workflow、輪詢歷史、用 ``/view`` 取音檔。"""

    def __init__(self, base_url: str, clone_template: Path | str | None = None,
                 design_template: Path | str | None = None, timeout: int = DEFAULT_TIMEOUT) -> None:
        from vendor.novel_studio import comfy

        self.comfy = comfy
        self.base_url = base_url
        self.paths = {"clone": Path(clone_template or DEFAULT_CLONE_TEMPLATE),
                      "design": Path(design_template or DEFAULT_DESIGN_TEMPLATE)}
        self.timeout = timeout
        self._uploaded: dict[str, str] = {}

    def template(self, kind: str) -> dict[str, Any]:
        path = self.paths.get(kind)
        if path is None:
            raise VoiceGenError(f"unknown workflow kind {kind!r}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise VoiceGenError(f"cannot load workflow {path}: {exc}") from exc

    # -- 上傳參考音 -------------------------------------------------
    def upload_reference(self, path: Path, name: str) -> str:
        cached = self._uploaded.get(name)
        if cached is not None:
            return cached
        source = Path(path)
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise VoiceGenError(f"cannot read reference audio {source}: {exc}") from exc
        result = self._upload(name, data)
        self._uploaded[name] = result
        return result

    def _upload(self, name: str, data: bytes) -> str:
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []

        def field(key: str, value: str) -> None:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode("utf-8"))

        field("type", "input")
        field("overwrite", "true")
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8") + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/upload/image", data=b"".join(parts), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001 — 網路錯誤一律轉成中文說明
            raise VoiceFailure(f"上傳參考音到 ComfyUI 失敗：{exc}") from exc
        uploaded = str(payload.get("name") or name)
        subfolder = payload.get("subfolder")
        return f"{subfolder}/{uploaded}" if isinstance(subfolder, str) and subfolder else uploaded

    # -- 執行 -------------------------------------------------------
    def _submit(self, workflow: dict[str, Any]) -> str:
        """同 ``comfy.submit``，但帶空的 ``widget_idx_map``，避免 focus_nodes 之類的節點在終端印錯誤。"""
        comfy = self.comfy
        reply = comfy._req(self.base_url, "/prompt", {
            "prompt": workflow, "client_id": uuid.uuid4().hex,
            "extra_data": {"extra_pnginfo": {"workflow": {"nodes": [], "widget_idx_map": {}}}},
        }, timeout=30)
        if reply.get("node_errors"):
            raise comfy.ComfyError(comfy._explain_error(400, json.dumps(reply)))
        pid = reply.get("prompt_id")
        if not pid:
            raise comfy.ComfyError("ComfyUI 沒有回傳任務編號：" + json.dumps(reply, ensure_ascii=False)[:300])
        return pid

    def run(self, workflow: dict[str, Any], should_stop: Callable[[], bool]) -> list[tuple[str, bytes]]:
        comfy, base = self.comfy, self.base_url
        try:
            pid = self._submit(workflow)
            start = time.time()
            while True:
                if should_stop():
                    comfy.interrupt(base, pid)
                    raise JobStopped("stopped")
                entry = comfy.history(base, pid)
                if entry:
                    error = comfy.status_error(entry)
                    if error:
                        raise VoiceFailure(error)
                    if (entry.get("status") or {}).get("completed") or entry.get("outputs"):
                        break
                if time.time() - start > self.timeout:
                    comfy.interrupt(base, pid)
                    raise VoiceFailure(f"等太久了（超過 {self.timeout // 60} 分鐘）")
                time.sleep(1.0)
            files = voice_workflow.output_audio_paths(entry.get("outputs"))
            if not files:
                raise VoiceFailure("ComfyUI 完成了，但沒有輸出音檔")
            result: list[tuple[str, bytes]] = []
            for item in files:
                query = urllib.parse.urlencode({k: item[k] for k in ("filename", "subfolder", "type")})
                suffix = Path(item["filename"]).suffix.lower() or ".flac"
                result.append((suffix, comfy._req(base, "/view?" + query, timeout=300, raw=True)))
            return result
        except comfy.ComfyError as exc:
            if str(exc) == "已停止":
                raise JobStopped("stopped") from exc
            raise VoiceFailure(str(exc)) from exc


# ------------------------------------------------------------------ 回報


@dataclass
class VoiceReport:
    generated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    designed: list[str] = field(default_factory=list)
    unvoiced: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stopped: bool = False


# ------------------------------------------------------------------ 工具


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VoiceGenError(f"cannot read {path}: {exc}") from exc


def _write_bytes(config: Any, path: Path, data: bytes) -> None:
    config.assert_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def design_reference_path(work: Path, char_id: str) -> Path:
    return work / DESIGN_DIR / f"{char_id}_design.flac"


def design_reference_candidates(work: Path, char_id: str) -> list[Path]:
    """同一個角色可能先產出 .flac 或 .wav；兩種都找。"""
    base = work / DESIGN_DIR / f"{char_id}_design"
    return [base.with_suffix(ext) for ext in (".flac", ".wav", ".ogg")]


def temp_path(work: Path, line: str, suffix: str) -> Path:
    suffix = suffix if suffix.lower() in (".flac", ".wav", ".ogg", ".opus", ".mp3") else ".flac"
    return work / TEMP_DIR / f"{line}{suffix}"


def _duration_and_rate(runner: Callable[[list[str]], tuple[int, str]], path: Path) -> tuple[float | None, int | None]:
    code, text = runner(voice_ffmpeg.probe_command(path))
    if code != 0:
        return None, None
    try:
        info = voice_ffmpeg.parse_probe(text)
    except ValueError:
        return None, None
    return info.get("duration"), info.get("sample_rate")


def _effective_spec(doc: dict[str, Any], char_id: str) -> dict[str, Any] | None:
    spec = (doc.get("characters") or {}).get(char_id)
    if not isinstance(spec, dict) or spec.get("enabled", True) is False:
        return None
    return spec


def _config_hash(work: Path, spec: dict[str, Any], engine: str, design_files: dict[str, Path]) -> str:
    char_id = str(spec.get("_char_id") or "")
    reference = None
    if spec.get("source") == "reference_audio":
        reference = voice_lines.reference_path(work, str(spec.get("reference_audio") or ""))
    design = design_files.get(char_id)
    if spec.get("source") == "design_then_clone" and spec.get("design_reference"):
        design = voice_lines.reference_path(work, str(spec.get("design_reference") or ""))
    return voice_lines.config_hash(spec, engine, reference=reference, design_reference=design)


def _entry_valid(work: Path, entry: dict[str, Any] | None, line: voice_lines.VoiceLine,
                 expected_hash: str, seed: int) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("text_hash") != voice_lines.text_hash(line.text):
        return False
    if entry.get("config_hash") != expected_hash or entry.get("seed") != seed:
        return False
    rel = entry.get("work_file") or ""
    if not rel or not (work / rel).is_file():
        return False
    return True


# ------------------------------------------------------------------ 設計參考音


def _design_one(config: Any, work: Path, backend: TtsBackend, char_id: str, spec: dict[str, Any],
                should_stop: Callable[[], bool], reuse_hash: str | None = None) -> tuple[Path, bool]:
    """Voice Design 產生一段參考音，回傳 ``(路徑, 是否新產生)``。

    ``reuse_hash`` 是 VoiceMap ``designs[char_id].config_hash`` 比對用的指紋；只有指紋相同
    才重用既有檔案。描述或種子改了就會重新設計。
    """
    seed = voice_lines.character_seed(char_id, spec.get("seed"))
    instruction = str(spec.get("voice_design_prompt") or "").strip()
    if not instruction:
        raise VoiceGenError(f"{char_id}: voice_design_prompt is empty")
    text = str(spec.get("design_text") or voice_lines.DEFAULT_DESIGN_TEXT).strip()
    if not spec.get("_force_design") and reuse_hash is not None:
        for existing in design_reference_candidates(work, char_id):
            if existing.is_file():
                return existing, False
    template = backend.template("design")
    workflow = voice_workflow.fill_design(
        template, text=text, instruction=instruction, seed=seed,
        language=str(spec.get("language") or "zh"),
        params={k: spec[k] for k in ("temperature", "top_p", "top_k", "repetition_penalty", "max_new_tokens") if k in spec},
        filename_prefix=f"voice/design/{char_id}", audio_format="flac")
    files = backend.run(workflow, should_stop)
    if not files:
        raise VoiceFailure("Voice Design returned no audio")
    suffix, data = files[0]
    if suffix.lower() not in (".flac", ".wav", ".ogg", ".opus", ".mp3"):
        suffix = ".flac"
    target = design_reference_path(work, char_id).with_suffix(suffix)
    _write_bytes(config, target, data)
    return target, True


# ------------------------------------------------------------------ 主流程


def generate(config: Any, work: Path, backend: TtsBackend | None, *, chapters: list[int] | None = None,
             only: list[str] | None = None, regenerate: bool = False, include_narration: bool = False,
             design_only: bool = False, should_stop: Callable[[], bool] = lambda: False,
             progress: Callable[[dict[str, Any]], None] | None = None,
             runner: Callable[[list[str]], tuple[int, str]] | None = None,
             sherpa: Any | None = None) -> VoiceReport:
    """逐句生成語音。單句失敗不中斷；每完成一句就寫 VoiceMap。"""
    emit = progress or (lambda event: None)
    run = runner or run_ffmpeg
    doc = voice_lines.load_cast_voice(work)
    if doc is None:
        raise VoiceGenError("voice/cast_voice.json not found; add one voice definition per character first")
    cast_doc = _read_json(work / "cast.json", None)
    errors = voice_lines.validate_cast_voice(doc, work, cast_doc)
    if errors:
        raise VoiceGenError("cast_voice.json is invalid:\n" + "\n".join(errors))
    engine = str(doc.get("engine") or voice_lines.DEFAULT_ENGINE)
    characters = doc.get("characters") or {}
    lines = voice_lines.collect_lines(work, chapters=chapters, include_narration=include_narration)
    if only:
        wanted = set(only)
        lines = [line for line in lines if line.line_id in wanted]
        if not lines:
            raise VoiceGenError("no voice lines matched --only")
    needs_sherpa = any((_effective_spec(doc, line.speaker or "") or {}).get("source") == "speaker_id"
                       for line in lines)
    if needs_sherpa and sherpa is None:
        sherpa = sherpa_tts.SherpaTts(doc.get("sherpa_model"))
    report = VoiceReport()
    voice_map = voice_lines.load_voice_map(work)
    voice_map["book_id"] = str(doc.get("book_id") or voice_map.get("book_id") or "")
    voice_map["engine"] = engine
    designs = voice_map.setdefault("designs", {})

    # 先把 design_then_clone 的參考音準備好；失敗的角色整批記 failed。
    needed_chars = set(characters) if design_only else {line.speaker for line in lines if line.speaker}
    design_files: dict[str, Path] = {}
    failed_chars: dict[str, str] = {}
    todo_designs = [cid for cid, spec in characters.items()
                    if isinstance(spec, dict) and spec.get("enabled", True) is not False
                    and spec.get("source") == "design_then_clone" and cid in needed_chars]
    if todo_designs and backend is None:
        raise VoiceGenError("design_then_clone needs a ComfyUI backend")
    for char_id in todo_designs:
        spec = characters[char_id]
        if spec.get("design_reference"):
            design_files[char_id] = voice_lines.reference_path(work, str(spec["design_reference"]))
            continue
        try:
            design_key = _config_hash(work, {**spec, "_char_id": char_id}, engine, {})
            meta = designs.get(char_id)
            reuse_hash = design_key if isinstance(meta, dict) and meta.get("config_hash") == design_key else None
            path, created = _design_one(config, work, backend, char_id, {**spec, "_char_id": char_id,
                                                                         "_force_design": regenerate}, should_stop,
                                        reuse_hash=reuse_hash)
            design_files[char_id] = path
            designs[char_id] = {"file": path.relative_to(work).as_posix(), "seed": voice_lines.character_seed(char_id, spec.get("seed")),
                                "config_hash": design_key,
                                "created": datetime.now().astimezone().isoformat(timespec="seconds")}
            if created:
                report.designed.append(char_id)
                emit({"event": "design", "char_id": char_id, "index": len(report.designed), "total": len(todo_designs)})
        except JobStopped:
            report.stopped = True
            break
        except (VoiceFailure, VoiceGenError, ValueError) as exc:
            failed_chars[char_id] = str(exc)
            report.failed[char_id] = str(exc)
    voice_lines.save_voice_map(config, work, voice_map)
    if report.stopped:
        return report
    if design_only:
        return report

    total = len(lines)
    for index, line in enumerate(lines, 1):
        if should_stop():
            report.stopped = True
            break
        spec = _effective_spec(doc, line.speaker or "")
        if spec is None:
            if (line.speaker or "") not in report.unvoiced:
                report.unvoiced.append(line.speaker or "")
            report.skipped.append(line.line_id)
            continue
        if (line.speaker or "") in failed_chars:
            report.failed[line.line_id] = failed_chars[line.speaker or ""]
            continue
        spec = {**spec, "_char_id": line.speaker}
        is_sherpa = spec.get("source") == "speaker_id"
        if is_sherpa:
            spec["sherpa_model"] = doc.get("sherpa_model")
        line_engine = voice_lines.engine_for(spec, engine)
        if is_sherpa:
            seed = int(spec.get("speaker_id") or 0)
        else:
            base_seed = voice_lines.character_seed(str(line.speaker), spec.get("seed"))
            # 克隆類用每句不同的種子；Voice Design 直接發聲時固定角色種子，音色才不會漂。
            seed = base_seed if spec.get("source") == "voice_design_prompt" else voice_lines.line_seed(base_seed, line.line_id)
        expected_hash = _config_hash(work, spec, line_engine, design_files)
        entry = (voice_map.get("lines") or {}).get(line.line_id)
        if not regenerate and _entry_valid(work, entry, line, expected_hash, seed):
            report.skipped.append(line.line_id)
            continue
        target = voice_lines.work_audio_path(work, line.chapter, str(line.speaker), line.line_id)
        paths = voice_lines.voice_paths(line.chapter, str(line.speaker), line.line_id)
        try:
            emit({"event": "voice", "line_id": line.line_id, "index": index, "total": total,
                  "speaker": line.speaker, "scene_id": line.scene_id})
            if is_sherpa:
                if sherpa is None:
                    sherpa = sherpa_tts.SherpaTts(doc.get("sherpa_model"))
                wav = sherpa.wav_bytes(line.text, speaker_id=int(spec["speaker_id"]),
                                       speed=float(spec.get("speed") or 1.0))
                temp = temp_path(work, line.line_id, ".wav")
                _write_bytes(config, temp, wav)
            else:
                if backend is None:
                    raise VoiceGenError("MOSS voice lines need a ComfyUI backend")
                workflow = _workflow_for(work, backend, line, spec, seed, engine, design_files)
                files = backend.run(workflow, should_stop)
                if not files:
                    raise VoiceFailure("ComfyUI returned no audio")
                suffix, data = files[0]
                temp = temp_path(work, line.line_id, suffix)
                _write_bytes(config, temp, data)
            config.assert_writable(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                code, output = run(voice_ffmpeg.convert_command(temp, target))
                if code != 0 or not target.is_file():
                    raise VoiceFailure(f"ffmpeg 轉檔失敗：{output[:300]}")
            finally:
                try:
                    temp.unlink()
                except OSError:
                    pass
            duration, sample_rate = _duration_and_rate(run, target)
            entry = {
                "line_id": line.line_id,
                "speaker": str(line.speaker),
                "chapter": line.chapter,
                "scene_id": line.scene_id,
                "text": line.text,
                **paths,
                "source": str(spec.get("source")),
                "engine": line_engine,
                "seed": seed,
                "text_hash": voice_lines.text_hash(line.text),
                "config_hash": expected_hash,
                "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            if duration is not None:
                entry["duration"] = round(float(duration), 3)
            if sample_rate is not None:
                entry["sample_rate"] = int(sample_rate)
            voice_map.setdefault("lines", {})[line.line_id] = entry
            voice_lines.save_voice_map(config, work, voice_map)
            report.generated.append(line.line_id)
            emit({"event": "line_done", "line_id": line.line_id, "index": index, "total": total})
        except JobStopped:
            report.stopped = True
            break
        except (VoiceFailure, VoiceGenError, sherpa_tts.SherpaTtsError, ValueError, OSError) as exc:
            report.failed[line.line_id] = str(exc)
            emit({"event": "line_failed", "line_id": line.line_id, "index": index, "total": total, "error": str(exc)})
    voice_lines.save_voice_map(config, work, voice_map)
    return report


def _workflow_for(work: Path, backend: TtsBackend, line: voice_lines.VoiceLine, spec: dict[str, Any],
                  seed: int, engine: str, design_files: dict[str, Path]) -> dict[str, Any]:
    char_id = str(line.speaker)
    language = str(spec.get("language") or "zh")
    params = {k: spec[k] for k in ("temperature", "top_p", "top_k", "repetition_penalty", "max_new_tokens") if k in spec}
    prefix = f"voice/{line.chapter}/{line.line_id}"
    if spec.get("source") == "voice_design_prompt":
        return voice_workflow.fill_design(
            backend.template("design"), text=line.text, instruction=str(spec.get("voice_design_prompt") or ""),
            seed=seed, language=language, params=params, filename_prefix=prefix, audio_format="flac")
    if spec.get("source") == "reference_audio":
        reference = voice_lines.reference_path(work, str(spec.get("reference_audio") or ""))
    elif spec.get("source") == "design_then_clone":
        reference = design_files.get(char_id)
        if reference is None:
            raise VoiceGenError(f"{char_id}: design reference was not generated")
    else:
        raise VoiceGenError(f"{char_id}: unknown voice source {spec.get('source')!r}")
    name = voice_workflow.reference_filename(str(work.name), char_id, reference)
    uploaded = backend.upload_reference(reference, name)
    return voice_workflow.fill_clone(
        backend.template("clone"), text=line.text, seed=seed, reference_audio=uploaded,
        engine=engine, language=language, params=params, filename_prefix=prefix, audio_format="flac")


# ------------------------------------------------------------------ 給網頁


def overview(work: Path, cast_doc: dict[str, Any] | None = None) -> dict[str, Any]:
    """語音頁要的資料：每個角色的聲線、句數、已生成／待生成、驗證錯誤。"""
    doc = voice_lines.load_cast_voice(work)
    if doc is None:
        return {"ready": False, "message": "還沒有 voice/cast_voice.json", "characters": [], "roster": [],
                "totals": {}, "errors": [], "lines": []}
    cast = cast_doc or _read_json(work / "cast.json", None)
    errors = voice_lines.validate_cast_voice(doc, work, cast)
    characters = doc.get("characters") or {}
    voice_map = voice_lines.load_voice_map(work)
    entries = voice_map.get("lines") or {}
    lines = voice_lines.collect_lines(work)
    engine = str(doc.get("engine") or voice_lines.DEFAULT_ENGINE)
    by_char: dict[str, dict[str, Any]] = {}
    design_files: dict[str, Path] = {}
    for char_id, spec in characters.items():
        by_char[char_id] = {"char_id": char_id, "spec": spec, "total": 0, "generated": 0, "stale": 0,
                            "engine": voice_lines.engine_for(spec, engine) if isinstance(spec, dict) else engine}
        if isinstance(spec, dict) and spec.get("source") == "design_then_clone":
            given = spec.get("design_reference")
            if given:
                design_files[char_id] = voice_lines.reference_path(work, str(given))
            else:
                existing = next((p for p in design_reference_candidates(work, char_id) if p.is_file()), None)
                if existing is not None:
                    design_files[char_id] = existing
    missing: list[str] = []
    line_items: list[dict[str, Any]] = []
    for line in lines:
        item = {"line_id": line.line_id, "speaker": line.speaker, "chapter": line.chapter, "scene_id": line.scene_id,
                "text": line.text, "status": "missing"}
        info = by_char.get(line.speaker or "")
        if info is None:
            if line.speaker and line.speaker not in missing:
                missing.append(line.speaker)
        else:
            info["total"] += 1
            entry = entries.get(line.line_id)
            work_file = work / str(entry.get("work_file") or "") if isinstance(entry, dict) else None
            spec = info.get("spec") if isinstance(info.get("spec"), dict) else {}
            hash_spec = {**spec, "_char_id": line.speaker}
            if spec.get("source") == "speaker_id":
                hash_spec["sherpa_model"] = doc.get("sherpa_model")
            expected_hash = _config_hash(work, hash_spec, voice_lines.engine_for(spec, engine), design_files)
            if isinstance(entry, dict) and entry.get("text_hash") == voice_lines.text_hash(line.text) \
                    and entry.get("config_hash") == expected_hash \
                    and work_file is not None and work_file.is_file():
                item.update({"status": "generated", "audio": entry.get("audio"), "work_file": entry.get("work_file"),
                             "seed": entry.get("seed")})
                info["generated"] += 1
            elif isinstance(entry, dict):
                item.update({"status": "stale", "audio": entry.get("audio"), "work_file": entry.get("work_file")})
                info["stale"] += 1
            else:
                item["status"] = "pending"
        line_items.append(item)
    chars = []
    for value in by_char.values():
        value["missing"] = value["total"] - value["generated"]
        chars.append(value)

    # 給聲線表單用：主角與主要配角即使還沒設定聲線也要出現。
    stats: dict[str, dict[str, int]] = {}
    for item in line_items:
        speaker = item.get("speaker") or ""
        row = stats.setdefault(speaker, {"total": 0, "generated": 0, "stale": 0})
        row["total"] += 1
        if item.get("status") == "generated":
            row["generated"] += 1
        elif item.get("status") == "stale":
            row["stale"] += 1
    roster = []
    role_order = {"protagonist": 0, "main": 1, "supporting": 2, "antagonist": 3}
    for char_id, info in ((cast or {}).get("characters") or {}).items():
        if not isinstance(info, dict) or info.get("role") not in role_order:
            continue
        spec = characters.get(char_id) if isinstance(characters.get(char_id), dict) else {}
        row = stats.get(char_id, {"total": 0, "generated": 0, "stale": 0})
        names = info.get("names") or {}
        roster.append({
            "char_id": char_id,
            "name": names.get("zh-TW") or next(iter(names.values()), char_id),
            "role": info.get("role"),
            "configured": spec.get("source") == "speaker_id",
            "speaker_id": spec.get("speaker_id"),
            "speed": spec.get("speed", 1.0),
            "total": row["total"],
            "generated": row["generated"],
            "stale": row["stale"],
        })
    roster.sort(key=lambda item: (role_order.get(item["role"], 9), item["char_id"]))
    return {
        "ready": True, "book_id": doc.get("book_id"), "engine": engine,
        "characters": sorted(chars, key=lambda c: c["char_id"]),
        "roster": roster,
        "missing_characters": missing,
        "totals": {"lines": len(lines), "generated": sum(1 for x in line_items if x["status"] == "generated"),
                   "stale": sum(1 for x in line_items if x["status"] == "stale"),
                   "pending": sum(1 for x in line_items if x["status"] == "pending"),
                   "missing": sum(1 for x in line_items if x["status"] == "missing")},
        "errors": errors, "lines": line_items,
    }


def audio_file(work: Path, rel: str) -> Path:
    """試聽用：只准 ``assets/voice/`` 下的音檔，不准跳出資料夾。

    也接受 VoiceMap 的 ``game/audio/voice/...`` 路徑，內部換成 ``assets/voice/...``。
    """
    text = str(rel or "").replace("\\", "/")
    if text.startswith("game/audio/"):
        text = "assets/" + text[len("game/audio/"):]
    base = (work / "assets" / "voice").resolve()
    target = (work / text).resolve()
    if target.suffix.lower() not in (".ogg", ".opus") or base not in target.parents or not target.is_file():
        raise VoiceGenError("voice audio not found")
    return target


# ------------------------------------------------------------------ 指令


def run_ffmpeg(command: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except FileNotFoundError:
        return 127, "找不到 ffmpeg／ffprobe，請先安裝並加入 PATH"
    return done.returncode, (done.stderr or done.stdout or "").strip()


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.voice_gen --book sample_book --chapters 1-2 [--only ID] [--force] [--design-only]``"""
    sys.stdout.reconfigure(encoding="utf-8")
    from .annotate import parse_chapter_range
    from .config import load_config

    parser = argparse.ArgumentParser(description="Generate voice lines with MOSS-TTS via ComfyUI.")
    parser.add_argument("--book", required=True)
    parser.add_argument("--chapters", default=None, help="chapter number or range, e.g. 2 or 1-5 (default: all)")
    parser.add_argument("--only", action="append", default=None, help="line_id (repeatable)")
    parser.add_argument("--force", action="store_true", help="regenerate even if VoiceMap has a matching entry")
    parser.add_argument("--design-only", action="store_true", help="only generate design_then_clone references")
    parser.add_argument("--include-narration", action="store_true")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        work = config.work_dir(args.book)
        chapters = parse_chapter_range(args.chapters) if args.chapters else None
        backend = VendorTtsBackend(config.comfyui_base_url)
        report = generate(config, work, backend, chapters=chapters, only=args.only, regenerate=args.force,
                          include_narration=args.include_narration, design_only=args.design_only)
    except (T2vnError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"designed {len(report.designed)}, generated {len(report.generated)}, skipped {len(report.skipped)}, "
          f"failed {len(report.failed)}" + (", stopped" if report.stopped else ""))
    for line_id, reason in report.failed.items():
        print(f"failed: {line_id}: {reason}")
    if report.unvoiced:
        print("no voice config: " + ", ".join(report.unvoiced))
    return 0 if not report.failed and not report.stopped else 1


if __name__ == "__main__":
    raise SystemExit(main())
