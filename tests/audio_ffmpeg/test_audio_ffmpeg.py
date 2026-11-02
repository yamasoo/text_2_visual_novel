import json
import shutil
import subprocess

import pytest

from t2vn import audio_ffmpeg as m


def test_loudnorm_filter():
    assert m.loudnorm_filter() == "loudnorm=I=-18:TP=-1.5:LRA=11"
    assert m.loudnorm_filter(-16.5) == "loudnorm=I=-16.5:TP=-1.5:LRA=11"
    for bad in (-41, -4.9, True, "x", None):
        with pytest.raises(ValueError):
            m.loudnorm_filter(bad)  # type: ignore[arg-type]


def test_loop_filter_text():
    assert m.loop_filter(60, 2) == (
        "[0:a]atrim=0:2,asetpts=PTS-STARTPTS,afade=t=in:d=2:curve=qsin[a];"
        "[0:a]atrim=2:58,asetpts=PTS-STARTPTS[b];"
        "[0:a]atrim=58:60,asetpts=PTS-STARTPTS,afade=t=out:d=2:curve=qsin[c];"
        "[c][a]amix=inputs=2:duration=longest:normalize=0[ca];"
        "[b][ca]concat=n=2:v=0:a=1[out]"
    )
    assert "atrim=0:1.5" in m.loop_filter(30.25, 1.5) and "atrim=28.75:30.25" in m.loop_filter(30.25, 1.5)


@pytest.mark.parametrize("duration,crossfade", [(4, 2), (3, 2), (0, 1), (-5, 1), (60, 0), (60, -1), (True, 1), (60, True), ("60", 2)])
def test_loop_filter_errors(duration, crossfade):
    with pytest.raises(ValueError):
        m.loop_filter(duration, crossfade)


def test_convert_command_plain():
    cmd = m.convert_command("in.wav", "out.opus")
    assert cmd == [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", "in.wav",
        "-af", "loudnorm=I=-18:TP=-1.5:LRA=11",
        "-ar", "48000", "-c:a", "libopus", "-b:a", "96k", "out.opus",
    ]


def test_convert_command_options(tmp_path):
    cmd = m.convert_command(tmp_path / "a.flac", tmp_path / "b.ogg", ffmpeg="/bin/ff", bitrate="128k", lufs=-20, sample_rate=44100, channels=1)
    assert cmd[0] == "/bin/ff" and cmd[6] == str(tmp_path / "a.flac") and cmd[-1] == str(tmp_path / "b.ogg")
    assert "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "1" and cmd[cmd.index("-ar") + 1] == "44100"
    assert cmd[cmd.index("-b:a") + 1] == "128k" and "loudnorm=I=-20" in cmd[cmd.index("-af") + 1]


def test_convert_command_loop():
    cmd = m.convert_command("in.wav", "out.opus", loop_duration=60, crossfade=2)
    assert "-af" not in cmd
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph == m.loop_filter(60, 2) + ";[out]loudnorm=I=-18:TP=-1.5:LRA=11[final]"
    assert cmd[cmd.index("-map") + 1] == "[final]"
    assert cmd.index("-map") < cmd.index("-ar") < cmd.index("-c:a")


@pytest.mark.parametrize("kw", [
    {"dst": "out.mp3"}, {"dst": "out"}, {"dst": ""}, {"bitrate": "96"}, {"bitrate": "9999k"}, {"bitrate": 96},
    {"sample_rate": 100}, {"sample_rate": True}, {"sample_rate": 48000.0}, {"channels": 3}, {"channels": True},
    {"lufs": 0}, {"loop_duration": 3, "crossfade": 2},
])
def test_convert_command_errors(kw):
    args = {"src": "in.wav", "dst": "out.opus"}
    args.update(kw)
    with pytest.raises(ValueError):
        m.convert_command(**args)
    with pytest.raises(ValueError):
        m.convert_command("", "out.opus")


def test_probe_command():
    assert m.probe_command("a.opus") == ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", "a.opus"]
    assert m.probe_command("a.opus", ffprobe="fp")[0] == "fp"
    with pytest.raises(ValueError):
        m.probe_command("")


def test_parse_probe():
    text = json.dumps({
        "streams": [{"codec_type": "video"}, {"codec_type": "audio", "codec_name": "opus", "channels": 2, "sample_rate": "48000", "duration": "9.9"}],
        "format": {"duration": "10.005"},
    })
    assert m.parse_probe(text) == {"duration": 10.005, "codec": "opus", "channels": 2, "sample_rate": 48000}
    no_format = json.dumps({"streams": [{"codec_type": "audio", "codec_name": "pcm_s16le", "channels": 1, "sample_rate": 44100, "duration": "2.5"}]})
    assert m.parse_probe(no_format)["duration"] == 2.5


@pytest.mark.parametrize("text", [
    "", "not json", "[]", "{}", '{"streams": []}', '{"streams": [{"codec_type": "video"}]}',
    '{"streams": [{"codec_type": "audio", "channels": 2, "sample_rate": "48000"}]}',
    '{"streams": [{"codec_type": "audio", "channels": "x", "sample_rate": "48000", "duration": "1"}]}',
])
def test_parse_probe_errors(text):
    with pytest.raises(ValueError):
        m.parse_probe(text)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="需要 ffmpeg 與 ffprobe")
def test_real_ffmpeg_roundtrip(tmp_path):
    src, dst = tmp_path / "tone.wav", tmp_path / "tone.opus"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=12", str(src)], check=True)
    probe = m.parse_probe(subprocess.run(m.probe_command(src), check=True, capture_output=True, text=True).stdout)
    assert abs(probe["duration"] - 12.0) < 0.1
    subprocess.run(m.convert_command(src, dst, loop_duration=probe["duration"], crossfade=2), check=True)
    out = m.parse_probe(subprocess.run(m.probe_command(dst), check=True, capture_output=True, text=True).stdout)
    assert out["codec"] == "opus" and out["sample_rate"] == 48000
    assert abs(out["duration"] - (12.0 - 2.0)) < 0.2  # 中段 8 秒 + 交叉淡化 2 秒
