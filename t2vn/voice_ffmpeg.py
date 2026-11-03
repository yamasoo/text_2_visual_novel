"""語音用的 ffmpeg 指令組裝：ComfyUI 的 FLAC → OGG Vorbis（24 kHz、單聲道、響度正規化）。

只組指令與解析 ffprobe 輸出，不執行任何程式；實際執行由呼叫端注入。規格見
``docs/33_語音管線規格.md`` 第 3.4 節。
"""
from __future__ import annotations

from pathlib import Path

from . import audio_ffmpeg

VOICE_LUFS = -16.0
VOICE_SAMPLE_RATE = 24000
VOICE_CHANNELS = 1
VOICE_QUALITY = 4
MIN_SAMPLE_RATE = 24000
MAX_SAMPLE_RATE = 48000
QUALITY_RANGE = (0, 10)
TRIM_THRESHOLD_DB = -45
TRIM_MIN_SECONDS = 0.05


def trim_silence_filter(threshold_db: int = TRIM_THRESHOLD_DB, minimum: float = TRIM_MIN_SECONDS) -> str:
    """去掉開頭與結尾的靜音（保留中間的停頓）。

    MOSS Voice Design 對短句常在頭尾各生成 2–3 秒空白；不修剪時「四個字」會變成四秒。
    先正向去掉開頭，反轉後去掉結尾，再反轉回來。
    """
    if not -80 <= int(threshold_db) <= -10:
        raise ValueError(f"threshold_db must be between -80 and -10: {threshold_db!r}")
    if not 0.01 <= float(minimum) <= 1.0:
        raise ValueError(f"minimum must be between 0.01 and 1.0: {minimum!r}")
    one = (f"silenceremove=start_periods=1:start_duration={minimum:g}:"
           f"start_threshold={int(threshold_db)}dB")
    return f"{one},areverse,{one},areverse"


def convert_command(
    src: str | Path,
    dst: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    sample_rate: int = VOICE_SAMPLE_RATE,
    channels: int = VOICE_CHANNELS,
    quality: int = VOICE_QUALITY,
    lufs: float = VOICE_LUFS,
    trim_silence: bool = True,
) -> list[str]:
    """組「轉成 OGG Vorbis 並正規化音量」的指令（字串清單）。

    - 輸出必須是 ``.ogg``；取樣率 24000–48000；聲道 1 或 2；Vorbis 品質 0–10。
    - 濾鏡固定是單次通過的 ``loudnorm``（預設 -16 LUFS）；不做循環、不做交叉淡化。
    """
    source, target = str(src or ""), str(dst or "")
    if not source.strip():
        raise ValueError("src cannot be empty")
    if not target.strip():
        raise ValueError("dst cannot be empty")
    if Path(target).suffix.lower() != ".ogg":
        raise ValueError(f"dst must end with .ogg: {target!r}")
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
        raise ValueError(f"sample_rate must be an integer: {sample_rate!r}")
    if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
        raise ValueError(f"sample_rate must be between {MIN_SAMPLE_RATE} and {MAX_SAMPLE_RATE}")
    if isinstance(channels, bool) or channels not in (1, 2):
        raise ValueError(f"channels must be 1 or 2: {channels!r}")
    if isinstance(quality, bool) or not isinstance(quality, int) or not QUALITY_RANGE[0] <= quality <= QUALITY_RANGE[1]:
        raise ValueError(f"quality must be an integer between {QUALITY_RANGE[0]} and {QUALITY_RANGE[1]}: {quality!r}")

    filters = [audio_ffmpeg.loudnorm_filter(lufs)]
    if trim_silence:
        filters.insert(0, trim_silence_filter())
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", source,
        "-af", ",".join(filters),
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-c:a", "libvorbis",
        "-q:a", str(quality),
        target,
    ]


def probe_command(src: str | Path, ffprobe: str = "ffprobe") -> list[str]:
    """ffprobe 指令（與 BGM／環境音共用解析）。"""
    return audio_ffmpeg.probe_command(src, ffprobe)


def parse_probe(text: str) -> dict:
    """解析 ffprobe JSON，回傳 ``{duration, codec, channels, sample_rate}``。"""
    return audio_ffmpeg.parse_probe(text)
