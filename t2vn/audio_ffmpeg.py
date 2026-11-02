"""ffmpeg／ffprobe 指令組裝與探測結果解析（S7）：轉 opus、音量正規化、無縫循環。

只組指令（字串清單，可直接給 ``subprocess.run``）和解析輸出，**不執行任何程式**，所以不需要
安裝 ffmpeg 就能測試。實際執行由呼叫端做。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DEFAULT_LUFS = -18.0
TRUE_PEAK = -1.5
LOUDNESS_RANGE = 11.0
DEFAULT_BITRATE = "96k"
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CROSSFADE = 2.0
OUTPUT_SUFFIXES = (".opus", ".ogg")
BITRATE = re.compile(r"^[0-9]{2,3}k$")

MIN_LUFS = -40.0
MAX_LUFS = -5.0
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 192000
ALLOWED_CHANNELS = (1, 2)


def _num(value: float) -> str:
    """數字轉成最短的字串（-18.0 → ``-18``，1.5 → ``1.5``）。"""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _number(value: Any, name: str) -> float:
    """檢查是數字（bool 不算），回傳 float。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必須是數字：{value!r}")
    return float(value)


def loudnorm_filter(lufs: float = DEFAULT_LUFS) -> str:
    """單次通過的響度正規化濾鏡，例如 ``loudnorm=I=-18:TP=-1.5:LRA=11``。

    ``lufs`` 要在 -40 到 -5 之間（含），否則 ``ValueError``。
    """
    level = _number(lufs, "lufs")
    if not MIN_LUFS <= level <= MAX_LUFS:
        raise ValueError(f"lufs 必須在 {_num(MIN_LUFS)} 到 {_num(MAX_LUFS)} 之間：{lufs!r}")
    return f"loudnorm=I={_num(level)}:TP={_num(TRUE_PEAK)}:LRA={_num(LOUDNESS_RANGE)}"


def loop_filter(duration: float, crossfade: float = DEFAULT_CROSSFADE) -> str:
    """把一段 ``duration`` 秒的音訊做成可無縫循環的 filter_complex，輸出標籤是 ``[out]``。

    做法：頭 ``crossfade`` 秒 A、中段 B、尾 ``crossfade`` 秒 C；輸出 = B + 交叉淡化
    (C → A)（C 淡出、A 淡入，等功率曲線，用 ``amix`` 疊起來）。``crossfade`` 必須大於 0，
    且 ``duration`` 必須大於 ``2 × crossfade``，否則 ``ValueError``。
    """
    total = _number(duration, "duration")
    fade = _number(crossfade, "crossfade")
    if fade <= 0:
        raise ValueError(f"crossfade 必須大於 0：{crossfade!r}")
    if total <= 2 * fade:
        raise ValueError(
            f"duration（{_num(total)}）必須大於 2 × crossfade（{_num(2 * fade)}）"
        )

    head_end = _num(fade)
    tail_start = _num(total - fade)
    return (
        f"[0:a]atrim=0:{head_end},asetpts=PTS-STARTPTS,"
        f"afade=t=in:d={head_end}:curve=qsin[a];"
        f"[0:a]atrim={head_end}:{tail_start},asetpts=PTS-STARTPTS[b];"
        f"[0:a]atrim={tail_start}:{_num(total)},asetpts=PTS-STARTPTS,"
        f"afade=t=out:d={head_end}:curve=qsin[c];"
        f"[c][a]amix=inputs=2:duration=longest:normalize=0[ca];"
        f"[b][ca]concat=n=2:v=0:a=1[out]"
    )


def _path_arg(value: Any, name: str) -> str:
    """路徑參數：不能是空字串。"""
    text = str(value) if value is not None else ""
    if not text.strip():
        raise ValueError(f"{name} 不可為空")
    return text


def convert_command(
    src: str | Path,
    dst: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    bitrate: str = DEFAULT_BITRATE,
    lufs: float = DEFAULT_LUFS,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int | None = None,
    loop_duration: float | None = None,
    crossfade: float = DEFAULT_CROSSFADE,
) -> list[str]:
    """組「轉成 opus 並正規化音量」的 ffmpeg 指令（字串清單）。

    順序固定：``ffmpeg -hide_banner -loglevel error -y -i <src>``，接濾鏡，再
    ``-ar <取樣率> [-ac <聲道>] -c:a libopus -b:a <位元率> <dst>``。``loop_duration`` 有給
    時先做 :func:`loop_filter`（``-filter_complex``，接 ``;[out]<響度濾鏡>[final]``，
    ``-map [final]``），沒給時只用 ``-af <響度濾鏡>``。
    """
    source = _path_arg(src, "src")
    target = _path_arg(dst, "dst")
    if Path(target).suffix.lower() not in OUTPUT_SUFFIXES:
        raise ValueError(f"dst 副檔名必須是 {'／'.join(OUTPUT_SUFFIXES)}：{target!r}")
    if not isinstance(bitrate, str) or BITRATE.match(bitrate) is None:
        raise ValueError(f"bitrate 形如 96k：{bitrate!r}")
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
        raise ValueError(f"sample_rate 必須是整數：{sample_rate!r}")
    if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
        raise ValueError(f"sample_rate 必須在 {MIN_SAMPLE_RATE} 到 {MAX_SAMPLE_RATE} 之間")

    if channels is not None:
        if isinstance(channels, bool) or not isinstance(channels, int):
            raise ValueError(f"channels 只能是 1 或 2：{channels!r}")
        if channels not in ALLOWED_CHANNELS:
            raise ValueError(f"channels 只能是 1 或 2：{channels!r}")

    loudness = loudnorm_filter(lufs)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", source]
    if loop_duration is None:
        command.extend(["-af", loudness])
    else:
        graph = f"{loop_filter(loop_duration, crossfade)};[out]{loudness}[final]"
        command.extend(["-filter_complex", graph, "-map", "[final]"])
    command.extend(["-ar", str(sample_rate)])
    if channels is not None:
        command.extend(["-ac", str(channels)])
    command.extend(["-c:a", "libopus", "-b:a", bitrate, target])
    return command


def probe_command(src: str | Path, ffprobe: str = "ffprobe") -> list[str]:
    """組 ffprobe 指令，輸出 JSON：``ffprobe -v error -show_format -show_streams -of json <src>``。"""
    source = _path_arg(src, "src")
    return [
        ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        source,
    ]


def parse_probe(text: str) -> dict[str, Any]:
    """解析 ffprobe 的 JSON，回傳 ``{"duration", "codec", "channels", "sample_rate"}``。

    取第一個 ``codec_type`` 為 ``audio`` 的串流；長度優先用 ``format.duration``，沒有再用
    串流的 ``duration``。找不到音訊串流、沒有長度、或不是合法 JSON → ``ValueError``。
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ffprobe 輸出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("ffprobe 輸出必須是 JSON 物件")

    streams = data.get("streams")
    if not isinstance(streams, list):
        raise ValueError("ffprobe 輸出沒有 streams")
    stream = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "audio"
        ),
        None,
    )
    if stream is None:
        raise ValueError("找不到音訊串流")

    duration: Any = None
    container = data.get("format")
    if isinstance(container, dict):
        duration = container.get("duration")
    if duration is None:
        duration = stream.get("duration")
    try:
        seconds = float(duration)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"沒有可用的長度：{duration!r}") from exc

    try:
        channels = int(stream.get("channels"))
        sample_rate = int(stream.get("sample_rate"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"串流缺少聲道或取樣率：{exc}") from exc

    codec = stream.get("codec_name")
    return {
        "duration": seconds,
        "codec": codec if isinstance(codec, str) else "",
        "channels": channels,
        "sample_rate": sample_rate,
    }
