"""語音管線的共用資料層：line_id、聲線設定、VoiceMap、路徑與雜湊。

純函式與 JSON 讀寫，不連 ComfyUI、不跑 ffmpeg。規格見 ``docs/33_語音管線規格.md``。

- ``line_id = <scene_id>_l<3 位行號>``（行號從 1 起算，含 narration／direction）。
- 聲線設定放 ``works/<book>/voice/cast_voice.json``；VoiceMap 放 ``works/<book>/voice/VoiceMap.json``。
- 音檔放 ``works/<book>/assets/voice/<chapter>/<speaker>_<line_id>.ogg``。
"""
from __future__ import annotations

import hashlib
import json
import re
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .errors import T2vnError

SCHEMA_VERSION = "0.1"
VOICE_DIR = "voice"
CAST_VOICE_FILE = "cast_voice.json"
VOICE_MAP_FILE = "VoiceMap.json"
LEGACY_VOICE_MAP_FILE = "voice_map.json"
ASSETS_VOICE_DIR = "assets/voice"
GAME_VOICE_DIR = "game/audio/voice"
RENPY_VOICE_DIR = "audio/voice"

LINE_RE = re.compile(r"^ch[0-9]{3}_sc[0-9]{2}_l[0-9]{3}$")
CHAPTER_RE = re.compile(r"^ch[0-9]{3}$")
SCENE_RE = re.compile(r"^ch[0-9]{3}_sc[0-9]{2}$")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")

SOURCES = ("reference_audio", "voice_design_prompt", "design_then_clone", "speaker_id")
ENGINES = ("moss-tts-local-1.7b", "moss-tts-delay-8b", "moss-voicegenerator-1.7b", "sherpa-onnx")
DEFAULT_ENGINE = "moss-tts-local-1.7b"
AUDIO_EXTS = (".wav", ".flac", ".ogg", ".opus", ".mp3")
LANGUAGES = ("auto", "zh", "en", "ja", "ko")
MAX_SEED = 2**31 - 1
DEFAULT_DESIGN_TEXT = "這是一段固定角色音色的參考錄音。"


class VoiceError(T2vnError):
    """語音資料或設定有問題。"""


@dataclass(frozen=True)
class VoiceLine:
    """scene graph 裡的一行，帶穩定 line_id。"""

    line_id: str
    chapter: str
    scene_id: str
    index: int
    type: str
    speaker: str | None
    text: str
    source_para: int | None
    expr: str | None = None


# ------------------------------------------------------------------ line id


def line_id(scene_id: str, index: int) -> str:
    """``ch001_sc02`` 的第 3 行 → ``ch001_sc02_l003``。``index`` 從 1 起算。"""
    if not SCENE_RE.match(str(scene_id)):
        raise VoiceError(f"invalid scene_id: {scene_id!r}")
    if isinstance(index, bool) or not isinstance(index, int) or index < 1 or index > 999:
        raise VoiceError(f"line index must be between 1 and 999: {index!r}")
    return f"{scene_id}_l{index:03d}"


def parse_line_id(value: str) -> tuple[str, int]:
    """回傳 ``(scene_id, index)``；格式不對丟 ``VoiceError``。"""
    if not isinstance(value, str) or not LINE_RE.match(value):
        raise VoiceError(f"invalid line_id: {value!r}")
    scene, _, number = value.rpartition("_l")
    return scene, int(number)


def chapter_of(line: str) -> str:
    """``ch001_sc02_l003`` → ``ch001``。"""
    scene, _ = parse_line_id(line)
    return scene[:5]


def chapter_number(chapter_id: str) -> int:
    if not CHAPTER_RE.match(str(chapter_id)):
        raise VoiceError(f"invalid chapter id: {chapter_id!r}")
    return int(chapter_id[2:])


def iter_lines(doc: dict[str, Any]) -> Iterator[VoiceLine]:
    """依 scene graph 順序產生每一行（含 narration／direction），index 從 1 起算。"""
    for scene in doc.get("scenes") or []:
        scene_id = str(scene.get("scene_id") or "")
        for index, line in enumerate(scene.get("lines") or [], 1):
            kind = str(line.get("type") or "")
            yield VoiceLine(
                line_id=line_id(scene_id, index),
                chapter=str(doc.get("chapter_id") or scene_id[:5]),
                scene_id=scene_id,
                index=index,
                type=kind,
                speaker=str(line["speaker"]) if line.get("speaker") is not None else None,
                text=str(line.get("text") or ""),
                source_para=line.get("source_para") if isinstance(line.get("source_para"), int) else None,
                expr=str(line["expr"]) if line.get("expr") is not None else None,
            )


def dialogue_lines(doc: dict[str, Any], include_narration: bool = False) -> Iterator[VoiceLine]:
    """要配音的行：預設只有 ``dialogue``（旁白要明確開啟）。"""
    for line in iter_lines(doc):
        if line.type == "dialogue" and line.speaker and line.text.strip():
            yield line
        elif include_narration and line.type == "narration" and line.text.strip():
            yield line


def collect_lines(work: Path, chapters: list[int] | None = None, include_narration: bool = False) -> list[VoiceLine]:
    """讀 ``scenes/``（優先）或 ``gold/``，回傳指定章節的配音行。"""
    wanted = set(chapters) if chapters else None
    out: list[VoiceLine] = []
    for doc in load_scene_docs(work):
        number = chapter_number(str(doc.get("chapter_id") or ""))
        if wanted is not None and number not in wanted:
            continue
        out.extend(dialogue_lines(doc, include_narration=include_narration))
    return out


# ------------------------------------------------------------------ 檔案


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VoiceError(f"cannot read {path}: {exc}") from exc


def _write_json(config: Any, path: Path, data: Any) -> None:
    config.assert_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def load_scene_docs(work: Path) -> list[dict[str, Any]]:
    """所有章節的 scene graph；同一章 ``scenes/`` 優先於 ``gold/``（與 ``art_gen`` 相同規則）。"""
    docs: dict[str, dict[str, Any]] = {}
    for folder in ("gold", "scenes"):
        for path in sorted((work / folder).glob("ch[0-9][0-9][0-9].json")):
            docs[path.name] = _read_json(path, {})
    return [docs[name] for name in sorted(docs)]


def cast_voice_path(work: Path) -> Path:
    return work / VOICE_DIR / CAST_VOICE_FILE


def voice_map_path(work: Path) -> Path:
    return work / VOICE_DIR / VOICE_MAP_FILE


def load_cast_voice(work: Path) -> dict[str, Any] | None:
    """讀聲線設定；新位置優先，舊的 ``works/<book>/cast_voice.json`` 仍可讀。"""
    for path in (cast_voice_path(work), work / CAST_VOICE_FILE):
        if path.is_file():
            data = _read_json(path, None)
            if not isinstance(data, dict):
                raise VoiceError(f"{path}: cast_voice must be a JSON object")
            return data
    return None


def save_cast_voice(config: Any, work: Path, doc: dict[str, Any]) -> Path:
    path = cast_voice_path(work)
    _write_json(config, path, doc)
    return path


def load_voice_map(work: Path) -> dict[str, Any]:
    """讀 VoiceMap；不存在時回傳空殼（不寫檔）。"""
    for path in (voice_map_path(work), work / VOICE_DIR / LEGACY_VOICE_MAP_FILE):
        if path.is_file():
            data = _read_json(path, None)
            if not isinstance(data, dict):
                raise VoiceError(f"{path}: VoiceMap must be a JSON object")
            lines = data.get("lines")
            if lines is None and any(LINE_RE.match(str(k)) for k in data):
                # 最舊的形狀：最外層就是 line_id → 條目。轉成新形狀但仍可讀。
                data = {"schema_version": SCHEMA_VERSION, "book_id": data.get("book_id", ""),
                        "engine": data.get("engine", DEFAULT_ENGINE), "lines": {k: v for k, v in data.items()
                                                                                if LINE_RE.match(str(k))}}
            data.setdefault("lines", {})
            return data
    return {"schema_version": SCHEMA_VERSION, "book_id": "", "engine": DEFAULT_ENGINE, "lines": {}}


def save_voice_map(config: Any, work: Path, data: dict[str, Any]) -> Path:
    data["schema_version"] = SCHEMA_VERSION
    data["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    path = voice_map_path(work)
    _write_json(config, path, data)
    return path


# ------------------------------------------------------------------ 路徑


def voice_paths(chapter: str, speaker: str, line: str) -> dict[str, str]:
    """回傳工作檔、Ren'Py 相對路徑與遊戲路徑。"""
    name = f"{speaker}_{line}.ogg"
    return {
        "work_file": f"{ASSETS_VOICE_DIR}/{chapter}/{name}",
        "file": f"{RENPY_VOICE_DIR}/{chapter}/{name}",
        "audio": f"{GAME_VOICE_DIR}/{chapter}/{name}",
    }


def work_audio_path(work: Path, chapter: str, speaker: str, line: str) -> Path:
    return work / voice_paths(chapter, speaker, line)["work_file"]


def reference_path(work: Path, value: str) -> Path:
    """解析參考音路徑：絕對路徑 → ``voice/`` → 作品根目錄。"""
    text = str(value or "").strip()
    if not text:
        raise VoiceError("reference_audio is empty")
    path = Path(text)
    if path.is_absolute():
        return path
    for base in (work / VOICE_DIR, work):
        candidate = base / path
        if candidate.is_file():
            return candidate
    return work / VOICE_DIR / path


# ------------------------------------------------------------------ 雜湊與種子


def text_hash(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def character_seed(char_id: str, explicit: int | None = None) -> int:
    """角色基準種子；有給就用（驗證範圍），否則由 char_id 決定。"""
    if explicit is not None:
        if isinstance(explicit, bool) or not isinstance(explicit, int) or not 0 <= explicit <= MAX_SEED:
            raise VoiceError(f"seed must be an integer between 0 and {MAX_SEED}: {explicit!r}")
        return explicit
    return zlib.crc32(char_id.encode("utf-8")) % (MAX_SEED + 1)


def line_seed(base_seed: int, line: str) -> int:
    """每句的種子：同一個 line_id 永遠得到同一個值。"""
    return (int(base_seed) * 7919 + zlib.crc32(str(line).encode("utf-8"))) % (MAX_SEED + 1)


def config_hash(spec: dict[str, Any], engine: str, reference: Path | None = None,
                design_reference: Path | None = None) -> str:
    """聲線設定的指紋。參考音內容也算進去，換檔就會重新生成。"""
    payload: dict[str, Any] = {
        "source": spec.get("source"),
        "engine": engine,
        "voice_design_prompt": spec.get("voice_design_prompt") or "",
        "design_text": spec.get("design_text") or "",
        "seed": spec.get("seed"),
        "speaker_id": spec.get("speaker_id"),
        "speed": spec.get("speed"),
        "sherpa_model": spec.get("sherpa_model"),
        "language": spec.get("language") or "zh",
        "temperature": spec.get("temperature"),
        "top_p": spec.get("top_p"),
        "top_k": spec.get("top_k"),
        "repetition_penalty": spec.get("repetition_penalty"),
        "max_new_tokens": spec.get("max_new_tokens"),
    }
    for key, path in (("reference_audio", reference), ("design_reference", design_reference)):
        if path is not None and Path(path).is_file():
            payload[key] = {"name": Path(path).name, "sha256": file_hash(path)}
        else:
            payload[key] = None
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 驗證


def _is_seed(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SEED


def validate_cast_voice(doc: dict[str, Any] | None, work: Path, cast_doc: dict[str, Any] | None = None) -> list[str]:
    """檢查聲線設定，回傳錯誤清單（空清單＝通過）。"""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["cast_voice must be a JSON object"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}: {doc.get('schema_version')!r}")
    book_id = doc.get("book_id")
    if not isinstance(book_id, str) or not ID_RE.match(book_id):
        errors.append(f"book_id is not a valid id: {book_id!r}")
    if doc.get("engine") not in (None, *ENGINES):
        errors.append(f"engine is not supported: {doc.get('engine')!r}")
    characters = doc.get("characters")
    if not isinstance(characters, dict):
        errors.append("characters must be an object")
        return errors
    if not characters:
        # 空的是合法中繼狀態：剛建立的書（先寫最小檔）、或只套用模型還沒配 speaker_id。
        return errors
    known = (cast_doc or {}).get("characters") or {}
    for char_id, spec in characters.items():
        where = f"characters.{char_id}"
        if not isinstance(char_id, str) or not ID_RE.match(char_id):
            errors.append(f"{where}: invalid char_id")
            continue
        if known and char_id not in known and char_id != "player":
            errors.append(f"{where}: not in cast.json")
        if not isinstance(spec, dict):
            errors.append(f"{where}: must be an object")
            continue
        source = spec.get("source")
        if source not in SOURCES:
            errors.append(f"{where}.source must be one of {', '.join(SOURCES)}: {source!r}")
        if doc.get("engine") == "sherpa-onnx" and source != "speaker_id":
            errors.append(f"{where}.source must be speaker_id when engine is sherpa-onnx")
        if spec.get("enabled", True) is False:
            continue
        if source == "reference_audio":
            value = spec.get("reference_audio")
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{where}.reference_audio is required")
            else:
                path = reference_path(work, value)
                if not path.is_file():
                    errors.append(f"{where}.reference_audio not found: {path}")
                elif path.suffix.lower() not in AUDIO_EXTS:
                    errors.append(f"{where}.reference_audio must be one of {', '.join(AUDIO_EXTS)}")
        if source in ("voice_design_prompt", "design_then_clone"):
            prompt = spec.get("voice_design_prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                errors.append(f"{where}.voice_design_prompt is required for {source}")
        if source == "speaker_id":
            speaker_id = spec.get("speaker_id")
            if isinstance(speaker_id, bool) or not isinstance(speaker_id, int) or not 0 <= speaker_id <= 100000:
                errors.append(f"{where}.speaker_id must be an integer between 0 and 100000")
            if "speed" in spec and (isinstance(spec["speed"], bool) or not isinstance(spec["speed"], (int, float))
                                    or not 0.5 <= float(spec["speed"]) <= 2.0):
                errors.append(f"{where}.speed must be a number between 0.5 and 2")
        design_reference = spec.get("design_reference")
        if design_reference is not None:
            if not isinstance(design_reference, str) or not design_reference.strip():
                errors.append(f"{where}.design_reference must be a non-empty string")
            else:
                path = reference_path(work, design_reference)
                if not path.is_file():
                    errors.append(f"{where}.design_reference not found: {path}")
                elif path.suffix.lower() not in AUDIO_EXTS:
                    errors.append(f"{where}.design_reference must be one of {', '.join(AUDIO_EXTS)}")
        if "seed" in spec and not _is_seed(spec["seed"]):
            errors.append(f"{where}.seed must be an integer between 0 and {MAX_SEED}")
        if "language" in spec and spec["language"] not in LANGUAGES:
            errors.append(f"{where}.language must be one of {', '.join(LANGUAGES)}")
        for key, low, high in (("temperature", 0, 5), ("top_p", 0, 1), ("repetition_penalty", 0.5, 2)):
            if key in spec and (isinstance(spec[key], bool) or not isinstance(spec[key], (int, float))
                                or not low <= float(spec[key]) <= high):
                errors.append(f"{where}.{key} must be a number between {low} and {high}")
        if "top_k" in spec and (isinstance(spec["top_k"], bool) or not isinstance(spec["top_k"], int)
                                or not 1 <= spec["top_k"] <= 200):
            errors.append(f"{where}.top_k must be an integer between 1 and 200")
        if "max_new_tokens" in spec and (isinstance(spec["max_new_tokens"], bool)
                                         or not isinstance(spec["max_new_tokens"], int)
                                         or not 1 <= spec["max_new_tokens"] <= 8192):
            errors.append(f"{where}.max_new_tokens must be an integer between 1 and 8192")
    return errors


def engine_for(spec: dict[str, Any], default: str | None = None) -> str:
    """這個角色實際用的引擎：speaker_id 用 sherpa-onnx、Voice Design 用 VoiceGenerator，其餘用預設。"""
    if spec.get("source") == "speaker_id":
        return "sherpa-onnx"
    engine = str(default or DEFAULT_ENGINE)
    if spec.get("source") == "voice_design_prompt":
        return "moss-voicegenerator-1.7b"
    return engine
