"""Real FFmpeg/ffprobe contracts for precision, metadata and editing."""
import json
import subprocess

import pytest
from app.audio.converter import AudioConverter
from app.audio.media import track_identifier


def command(*args):
    return subprocess.run(list(args), check=True, capture_output=True, timeout=15)


def probe(path):
    return json.loads(command("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)).stdout)


def pcm(path, representation="f64le"):
    return command("ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", representation, "-").stdout


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.wav"
    command("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=997:sample_rate=48000:duration=2", "-metadata", "title=Title 東京 🎵", "-metadata", "artist=Artist", str(path))
    return path


def convert(source, output, settings):
    engine = AudioConverter()
    assert engine.convert_file(str(source), str(output), settings), engine.last_result
    return engine.last_result


@pytest.mark.parametrize("codec", ["pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le"])
def test_wav_preserves_every_supported_pcm_representation(tmp_path, codec):
    source = tmp_path / "input.wav"
    command("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "aevalsrc=0.123456789*sin(2*PI*997*t):s=48000:d=0.1", "-c:a", codec, str(source))
    result = convert(source, tmp_path / "output.wav", {"format": "wav"})
    output = result.outputs[0]
    assert probe(output)["streams"][0]["codec_name"] == codec
    assert pcm(source) == pcm(output)


@pytest.mark.parametrize("codec,bits", [("pcm_s16le", 16), ("pcm_s24le", 24), ("pcm_s32le", 32)])
def test_flac_preserves_integer_pcm_without_silent_24_bit_truncation(tmp_path, codec, bits):
    source = tmp_path / "input.wav"
    command("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "aevalsrc=0.123456789*sin(2*PI*997*t):s=48000:d=0.1", "-c:a", codec, str(source))
    result = convert(source, tmp_path / "output.flac", {"format": "flac"})
    output = result.outputs[0]
    assert int(probe(output)["streams"][0]["bits_per_raw_sample"]) >= bits
    assert pcm(source) == pcm(output)


@pytest.mark.parametrize("codec", ["pcm_f32le", "pcm_f64le"])
def test_float_to_flac_requires_explicit_precision_reduction(tmp_path, codec):
    source = tmp_path / "input.wav"
    command("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "aevalsrc=0.123456789*sin(2*PI*997*t):s=48000:d=0.1", "-c:a", codec, str(source))
    engine = AudioConverter()
    output = tmp_path / "output.flac"
    assert not engine.convert_file(str(source), str(output), {"format": "flac"})
    assert not output.exists()
    result = convert(source, output, {"format": "flac", "allow_precision_reduction": True})
    assert int(probe(output)["streams"][0]["bits_per_raw_sample"]) == 24
    assert result.warnings


@pytest.mark.parametrize("format,codec", [("mp3", "mp3"), ("flac", "flac"), ("ogg", "vorbis"), ("wav", "pcm_s16le"), ("aac", "aac"), ("opus", "opus")])
def test_every_advertised_encoding_profile(source, tmp_path, format, codec):
    result = convert(source, tmp_path / f"output.{format}", {"format": format, "bitrate": "192k"})
    output = probe(result.outputs[0])
    audio = [s for s in output["streams"] if s["codec_type"] == "audio"][0]
    assert audio["codec_name"] == codec and audio["channels"] == 1
    assert audio["sample_rate"] == "48000"
    assert abs(float(output["format"]["duration"]) - 2) < .15
    if format != "aac":
        tags = output["format"].get("tags", {}) | audio.get("tags", {})
        assert {k.lower(): v for k, v in tags.items()}["title"] == "Title 東京 🎵"


@pytest.mark.parametrize("format,codec", [("mp3", "libmp3lame"), ("flac", "flac")])
@pytest.mark.parametrize("cut,merge", [(False, False), (True, False), (True, True)])
def test_supported_cover_and_tags_survive_full_cut_and_merged_output(source, tmp_path, format, codec, cut, merge):
    picture = tmp_path / "cover.png"
    command("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=32x32", "-frames:v", "1", "-threads", "1", str(picture))
    tagged = tmp_path / f"tagged.{format}"
    command("ffmpeg", "-v", "error", "-i", str(source), "-i", str(picture), "-map", "0:a:0", "-map", "1:v:0", "-c:a", codec, "-c:v", "copy", "-disposition:v", "attached_pic", str(tagged))
    options = {"format": format, "cut_enabled": cut, "cut_merge": merge,
               "file_markers": {str(tagged): [{"start": .1, "stop": .4}, {"start": .7, "stop": 1.1}]}}
    result = convert(tagged, tmp_path / f"edited.{format}", options)
    for path in result.outputs:
        info = probe(path)
        assert info["format"]["tags"]["title"] == "Title 東京 🎵"
        assert info["format"]["tags"]["artist"] == "Artist"
        assert any(stream.get("disposition", {}).get("attached_pic") for stream in info["streams"])
        cover = command("ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-c:v", "copy", "-f", "image2pipe", "-").stdout
        assert cover == picture.read_bytes()


def test_reordered_adjacent_overlapping_segments_have_exact_lossless_samples(source, tmp_path):
    segments = [{"start": 1, "stop": 1.5, "segment_index": 1},
                {"start": 0, "stop": .5, "segment_index": 2},
                {"start": .5, "stop": .75, "segment_index": 3},
                {"start": .25, "stop": .5, "segment_index": 4}]
    result = convert(source, tmp_path / "joined.flac", {"format": "flac", "cut_enabled": True, "cut_merge": True,
                     "order_by_segment_number": True, "file_markers": {str(source): segments}})
    original = pcm(source, "s16le")
    expected = b"".join(original[round(item["start"] * 48000) * 2:round(item["stop"] * 48000) * 2] for item in segments)
    assert pcm(result.outputs[0], "s16le") == expected


@pytest.mark.parametrize("name", ['-option.wav', 'a b.wav', 'a"b.wav', "a'b.wav", "[]()&$;.wav", "line\nbreak.wav", "🎵東京שלום.wav", "real::name.wav"])
def test_special_filenames_are_not_shell_syntax(source, tmp_path, name):
    path = tmp_path / name
    path.write_bytes(source.read_bytes())
    result = convert(path, tmp_path / "export.flac", {"format": "flac"})
    assert pcm(path) == pcm(result.outputs[0])


def test_explicit_second_track_remains_second_after_opaque_identifier(source, tmp_path):
    video = tmp_path / "multi.mkv"
    command("ffmpeg", "-v", "error", "-i", str(source), "-f", "lavfi", "-i", "aevalsrc=0.25:s=48000:d=2", "-map", "0:a:0", "-map", "1:a:0", "-c:a", "flac", str(video))
    identifier = track_identifier(video, 1)
    metadata = {identifier: {"source_video": str(video), "track_index": 1, "output_name": "track-2.flac"}}
    result = convert(identifier, tmp_path / "track.flac", {"format": "flac", "track_metadata": metadata})
    expected = command("ffmpeg", "-v", "error", "-i", str(video), "-map", "0:a:1", "-f", "s16le", "-").stdout
    assert pcm(result.outputs[0], "s16le") == expected


def test_invalid_batch_entry_does_not_prevent_next_file(source, tmp_path):
    engine = AudioConverter()
    engine.convert_all_files(["", str(source)], {"format": "flac", "output_directory": str(tmp_path / "output")}, None, lambda *args: None)
    assert [item.status for item in engine.last_batch_result.items] == ["failed", "success"]
    engine.cleanup()
