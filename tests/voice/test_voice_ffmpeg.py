import pytest

from t2vn import voice_ffmpeg


def test_convert_command_is_ogg_vorbis_with_loudnorm_and_trim():
    cmd = voice_ffmpeg.convert_command("in.flac", "out.ogg")
    assert cmd[:6] == ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i"]
    filters = cmd[cmd.index("-af") + 1]
    assert "loudnorm=I=-16" in filters and "silenceremove" in filters
    assert cmd[cmd.index("-ar") + 1] == "24000"
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-c:a") + 1] == "libvorbis"
    assert cmd[-1] == "out.ogg"


def test_convert_can_disable_trim():
    cmd = voice_ffmpeg.convert_command("in.flac", "out.ogg", trim_silence=False)
    assert "silenceremove" not in cmd[cmd.index("-af") + 1]


def test_trim_silence_filter_has_both_ends():
    text = voice_ffmpeg.trim_silence_filter()
    assert text.count("silenceremove") == 2 and text.count("areverse") == 2
    with pytest.raises(ValueError):
        voice_ffmpeg.trim_silence_filter(threshold_db=-100)
    with pytest.raises(ValueError):
        voice_ffmpeg.trim_silence_filter(minimum=2)


def test_convert_rejects_non_ogg():
    with pytest.raises(ValueError):
        voice_ffmpeg.convert_command("in.flac", "out.mp3")


def test_convert_rejects_low_sample_rate():
    with pytest.raises(ValueError):
        voice_ffmpeg.convert_command("in.flac", "out.ogg", sample_rate=16000)


@pytest.mark.parametrize("channels", [0, 3, True])
def test_convert_rejects_bad_channels(channels):
    with pytest.raises(ValueError):
        voice_ffmpeg.convert_command("in.flac", "out.ogg", channels=channels)


def test_convert_rejects_bad_quality():
    with pytest.raises(ValueError):
        voice_ffmpeg.convert_command("in.flac", "out.ogg", quality=11)


def test_parse_probe_uses_audio_stream():
    text = '{"streams":[{"codec_type":"audio","codec_name":"vorbis","channels":1,"sample_rate":"24000",' \
           '"duration":"2.5"}],"format":{"duration":"2.5"}}'
    info = voice_ffmpeg.parse_probe(text)
    assert info == {"duration": 2.5, "codec": "vorbis", "channels": 1, "sample_rate": 24000}


def test_parse_probe_rejects_no_audio():
    with pytest.raises(ValueError):
        voice_ffmpeg.parse_probe('{"streams":[]}')
