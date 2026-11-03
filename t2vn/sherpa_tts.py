"""sherpa-onnx 中文 TTS 後端（固定 speaker id，取代 MOSS 的 Voice Design 漂移）。

模型預設放在 ``models/sherpa/vits-zh-hf-fanchen-C``；用 ``cast_voice.json`` 的
``source: speaker_id`` 指定每個角色的 speaker id。純本機、CPU、不需要 ComfyUI。

用法::

    from t2vn.sherpa_tts import SherpaTts
    tts = SherpaTts()
    wav = tts.wav_bytes("你來了。", speaker_id=20)

套件：``pip install sherpa-onnx``（專案 venv）。
"""
from __future__ import annotations

import array
import csv
import io
import json
import os
import re
import sys
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

from .errors import T2vnError

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_ROOT = REPO_ROOT / "models" / "sherpa"
DEFAULT_MODEL_DIR = MODELS_ROOT / "vits-zh-hf-fanchen-C"
MODEL_FILE = "vits-zh-hf-fanchen-C.onnx"
MIN_SPEED = 0.5
MAX_SPEED = 2.0

# 聲線標籤是選配資料，放在 models/sherpa/speaker_presets.json（不進 git）。
# 公開版不含這個檔案，因此不會把測試分類結果帶出去。
PRESETS_FILE = MODELS_ROOT / "speaker_presets.json"


def load_speaker_presets(path: str | Path | None = None) -> dict[str, list[dict[str, Any]]]:
    file = Path(path) if path is not None else PRESETS_FILE
    if not file.is_file():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, list)}


SPEAKER_PRESETS = load_speaker_presets()

# 已知模型的中文／粵語說明；沒有列到的目錄仍會出現在選單，只是標籤用資料夾名。
MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "vits-zh-hf-fanchen-C": {"label": "fanchen-C（中文，187 人）", "speakers": 187, "language": "zh"},
    "vits-zh-hf-fanchen-wnj": {"label": "fanchen-wnj（中文，1 人）", "speakers": 1, "language": "zh"},
    "vits-zh-hf-fanchen-unity": {"label": "fanchen-unity（中文）", "language": "zh"},
    "vits-zh-hf-theresa": {"label": "theresa（中文，804 人）", "speakers": 804, "language": "zh"},
    "vits-zh-hf-eula": {"label": "eula（中文，804 人）", "speakers": 804, "language": "zh"},
    "sherpa-onnx-vits-zh-ll": {"label": "zh-ll（中文，5 人）", "speakers": 5, "language": "zh"},
    "vits-zh-aishell3": {"label": "aishell3（中文，174 人）", "speakers": 174, "language": "zh"},
    "vits-icefall-zh-aishell3": {"label": "icefall aishell3（中文，174 人）", "speakers": 174, "language": "zh"},
    "vits-cantonese-hf-xiaomaiiwn": {"label": "粵語 xiaomaiiwn（香港粵語，1 人）", "speakers": 1, "language": "yue"},
    "vits-melo-tts-zh_en": {"label": "MeloTTS（中英，1 人，44.1 kHz）", "speakers": 1, "language": "zh"},
}


def model_files(model_dir: str | Path) -> list[Path]:
    return sorted(Path(model_dir).glob("*.onnx"))


def model_info(model_dir: str | Path) -> dict[str, Any] | None:
    """回傳單一模型資料夾的資訊；不是可用的 sherpa TTS 目錄就回 None。"""
    path = Path(model_dir)
    onnx = model_files(path)
    if not onnx or not (path / "tokens.txt").is_file():
        return None
    known = MODEL_CATALOG.get(path.name, {})
    return {
        "id": path.name,
        "path": path.relative_to(REPO_ROOT).as_posix() if REPO_ROOT in path.parents else str(path),
        "label": known.get("label", path.name),
        "speakers": known.get("speakers"),
        "language": known.get("language", ""),
        "model_file": onnx[0].name,
        "has_lexicon": (path / "lexicon.txt").is_file(),
        "presets": SPEAKER_PRESETS.get(path.name, []),
    }


def list_models(root: str | Path | None = None) -> list[dict[str, Any]]:
    """掃描 models/sherpa 下的模型資料夾（有 .onnx 與 tokens.txt 才算）。"""
    base = Path(root) if root is not None else MODELS_ROOT
    if not base.is_dir():
        return []
    found = []
    for child in sorted(base.iterdir()):
        if child.is_dir():
            info = model_info(child)
            if info is not None and info["has_lexicon"]:
                found.append(info)
    return found


def speaker_catalog(model_dir: str | Path | None = None,
                    samples_root: str | Path | None = None) -> dict[str, Any]:
    """讀取 batch 分類的 demographics.csv，給 UI 的「性別／年齡／ID」串接選單用。"""
    path = resolve_model_dir(model_dir)
    base = Path(samples_root) if samples_root is not None else MODELS_ROOT / "samples" / "batch"
    candidates = [base / path.name / "demographics.csv", base / "demographics.csv"]
    csv_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    result: dict[str, Any] = {"model": path.name, "available": False, "speakers": [],
                              "message": "", "source": ""}
    if csv_path is None:
        result["message"] = "尚無分類資料；直接輸入 speaker id"
        return result
    speakers = []
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("model") and row.get("model") != path.name:
                continue
            try:
                speaker_id = int(str(row.get("id", "")).strip())
            except ValueError:
                continue
            gender = str(row.get("gender", "")).strip().lower()
            age_label = str(row.get("age_label", "")).strip()
            try:
                age_years = float(row.get("age_years", ""))
            except ValueError:
                age_years = None
            speakers.append({
                "id": speaker_id,
                "gender": gender,
                "gender_label": {"female": "女", "male": "男", "child": "小孩"}.get(gender, gender),
                "age_label": age_label,
                "age_years": age_years,
                "confidence": str(row.get("gender_confidence", "")).strip(),
            })
    speakers.sort(key=lambda item: item["id"])
    result.update({
        "available": bool(speakers),
        "speakers": speakers,
        "source": csv_path.relative_to(REPO_ROOT).as_posix() if REPO_ROOT in csv_path.parents else str(csv_path),
    })
    return result


_STDERR_LOCK = threading.Lock()
_OOV_RE = re.compile(r"Ignore OOV '([^']*)'?")


def _generate_capturing_oov(tts: Any, text: str, speaker_id: int, speed: float) -> tuple[Any, list[str]]:
    """執行 sherpa 生成，攔掉 C++ 的 Ignore OOV 洗版；回傳 (audio, oov 字元)。

    sherpa-onnx 的 C++ 字元詞典會直接寫 stderr（例如 ``Ignore OOV '繚'``），
    Python 的 redirect_stderr 攔不到，所以暫時把 fd 2 導到暫存檔。只吞掉 OOV 行，
    其他原生錯誤訊息仍會印出來。
    """
    captured = ""
    audio = None
    error: BaseException | None = None
    with _STDERR_LOCK:
        with tempfile.TemporaryFile() as sink:
            saved = os.dup(2)
            try:
                os.dup2(sink.fileno(), 2)
                try:
                    audio = tts.generate(text, sid=speaker_id, speed=speed)
                except BaseException as exc:  # 保留例外，先恢復 stderr 再拋
                    error = exc
            finally:
                os.dup2(saved, 2)
                os.close(saved)
            sink.seek(0)
            captured = sink.read().decode("utf-8", "replace")

    oov: list[str] = []
    leftovers: list[str] = []
    for line in captured.splitlines():
        match = _OOV_RE.search(line)
        if match:
            if match.group(1):
                oov.append(match.group(1))
        elif line.strip():
            leftovers.append(line)
    if os.environ.get("T2VN_SHERPA_OOV") == "1" and oov:
        try:
            print("sherpa-onnx: 忽略字典外字元：" + "、".join(dict.fromkeys(oov)), file=sys.stderr, flush=True)
        except UnicodeEncodeError:
            print("sherpa-onnx: ignored OOV characters", file=sys.stderr, flush=True)
    for line in leftovers:
        print(line, file=sys.stderr, flush=True)
    if error is not None:
        raise error
    return audio, oov


class SherpaTtsError(T2vnError):
    """sherpa-onnx 模型或參數有問題。"""


def resolve_model_dir(value: str | Path | None = None) -> Path:
    """相對路徑以專案根目錄為基準；沒給就用預設模型。"""
    if value is None or str(value).strip() == "":
        return DEFAULT_MODEL_DIR
    path = Path(str(value))
    return path if path.is_absolute() else REPO_ROOT / path


class SherpaTts:
    """延遲載入模型，第一次合成時才 import／載入 sherpa-onnx。"""

    def __init__(self, model_dir: str | Path | None = None, threads: int = 2, provider: str = "cpu") -> None:
        self.model_dir = resolve_model_dir(model_dir)
        self.threads = max(1, int(threads))
        self.provider = provider
        self._tts: Any = None
        self.last_oov: list[str] = []

    def _load(self) -> Any:
        if self._tts is not None:
            return self._tts
        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover - 只有沒裝套件時
            raise SherpaTtsError(
                "找不到 sherpa-onnx；請在專案 venv 執行 `pip install sherpa-onnx`") from exc
        candidates = model_files(self.model_dir)
        model = candidates[0] if candidates else self.model_dir / MODEL_FILE
        lexicon = self.model_dir / "lexicon.txt"
        tokens = self.model_dir / "tokens.txt"
        missing = [p for p in (model, lexicon, tokens) if not p.is_file()]
        if missing:
            raise SherpaTtsError(
                "sherpa-onnx 模型檔不完整：" + "、".join(str(p) for p in missing) +
                "；請下載 vits-zh-hf-fanchen-C 到 " + str(self.model_dir))
        rules = [str(path) for path in sorted(self.model_dir.glob("*.fst"))]
        try:
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=str(model), lexicon=str(lexicon), tokens=str(tokens),
                        data_dir="", dict_dir=""),
                    num_threads=self.threads, debug=False, provider=self.provider),
                rule_fsts=",".join(rules),
                max_num_sentences=1)
            self._tts = sherpa_onnx.OfflineTts(config)
        except Exception as exc:  # noqa: BLE001 - sherpa 的錯誤類型不固定
            raise SherpaTtsError(f"載入 sherpa-onnx 模型失敗：{exc}") from exc
        return self._tts

    @property
    def sample_rate(self) -> int:
        return int(self._load().sample_rate)

    @property
    def num_speakers(self) -> int:
        return int(self._load().num_speakers)

    def wav_bytes(self, text: str, speaker_id: int, speed: float = 1.0) -> bytes:
        """合成一句，回傳 16-bit PCM 的 WAV bytes。"""
        clean = str(text or "").strip()
        if not clean:
            raise SherpaTtsError("text cannot be empty")
        if isinstance(speaker_id, bool) or not isinstance(speaker_id, int) or speaker_id < 0:
            raise SherpaTtsError(f"speaker_id must be a non-negative integer: {speaker_id!r}")
        try:
            speed_value = float(speed)
        except (TypeError, ValueError) as exc:
            raise SherpaTtsError(f"speed must be a number: {speed!r}") from exc
        if not MIN_SPEED <= speed_value <= MAX_SPEED:
            raise SherpaTtsError(f"speed must be between {MIN_SPEED} and {MAX_SPEED}: {speed!r}")
        tts = self._load()
        count = int(tts.num_speakers) or 1  # 單人模型可能回報 0
        if speaker_id >= count:
            raise SherpaTtsError(
                f"speaker_id {speaker_id} 超出模型 {self.model_dir.name} 的範圍（0..{count - 1}）")
        audio, self.last_oov = _generate_capturing_oov(tts, clean, speaker_id, speed_value)
        samples = array.array("h", (max(-32768, min(32767, int(s * 32767))) for s in audio.samples))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(audio.sample_rate))
            wf.writeframes(samples.tobytes())
        return buffer.getvalue()
