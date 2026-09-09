"""Real FFmpeg regression tests for audio integrity and segment contracts."""

import json
from pathlib import Path
import subprocess
import sys
import wave

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "big-audio-converter/usr/share/biglinux/audio-converter"))
from app.audio.converter import AudioConverter


def run(*args):
    return subprocess.run(args, capture_output=True, check=True, timeout=20).stdout


def probe(path):
    return json.loads(run("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)))


def pcm(path):
    return np.frombuffer(run("ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "f64le", "-"), dtype="<f8")


@pytest.fixture
def tone(tmp_path):
    path = tmp_path / "source.wav"
    t = np.arange(144000) / 48000
    data = np.rint(np.sin(2 * np.pi * 997 * t) * 8192).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(data.tobytes())
    return path


def options(path, **changes):
    settings = {"format": "wav", "volume": 1.0, "speed": 1.0,
                "cut_enabled": True, "cut_merge": True,
                "file_markers": {str(path): [{"start": 0.5, "stop": 1.5}]}}
    settings.update(changes)
    return settings


@pytest.mark.parametrize("fmt", ["mp3", "flac", "ogg", "wav", "aac", "opus"])
def test_offered_formats(tone, tmp_path, fmt):
    converter = AudioConverter()
    output = tmp_path / ("result." + fmt)
    assert converter.convert_file(str(tone), str(output), {"format": fmt, "bitrate": "192k"})
    assert any(stream["codec_type"] == "audio" for stream in probe(output)["streams"])


def test_segment_volume_is_applied(tone, tmp_path):
    output = tmp_path / "muted.wav"
    assert AudioConverter().convert_file(str(tone), str(output), options(tone, volume=0))
    assert len(pcm(output)) == 48000
    assert np.max(np.abs(pcm(output))) == 0


def test_segment_speed_is_applied(tone, tmp_path):
    output = tmp_path / "fast.wav"
    assert AudioConverter().convert_file(str(tone), str(output), options(tone, speed=2))
    assert len(pcm(output)) / 48000 == pytest.approx(0.5, abs=0.05)


@pytest.mark.parametrize("segments", [[{"start": 0, "stop": 0.01}], [{"start": 0, "stop": 1}, {"start": 100, "stop": 101}]])
def test_incomplete_segment_request_is_not_success(tone, tmp_path, segments):
    output = tmp_path / "incomplete.wav"
    converter = AudioConverter()
    assert not converter.convert_file(str(tone), str(output), options(tone, file_markers={str(tone): segments}))
    assert not output.exists()
    assert not list(tmp_path.glob(".bac-*"))


def test_normalization_preserves_output_rate(tone, tmp_path):
    output = tmp_path / "normalized.wav"
    assert AudioConverter().convert_file(str(tone), str(output), {"format": "wav", "normalize": True})
    assert int(probe(output)["streams"][0]["sample_rate"]) == 48000


@pytest.mark.parametrize("fmt", ["wav", "flac"])
def test_24_bit_pcm_is_preserved(tmp_path, fmt):
    source = tmp_path / "source24.wav"
    run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "aevalsrc=0.2*sin(2*PI*997*t):s=48000:d=1", "-c:a", "pcm_s24le", str(source))
    output = tmp_path / ("preserved." + fmt)
    assert AudioConverter().convert_file(str(source), str(output), {"format": fmt})
    assert np.array_equal(pcm(source), pcm(output))


def test_cut_preserves_unicode_metadata(tone, tmp_path):
    tagged = tmp_path / "tagged.wav"
    run("ffmpeg", "-v", "error", "-i", str(tone), "-c:a", "copy", "-metadata", "title=Hello 漢字", "-metadata", "artist=Artist Δ", str(tagged))
    output = tmp_path / "cut.wav"
    assert AudioConverter().convert_file(str(tagged), str(output), options(tagged))
    assert probe(output)["format"]["tags"]["title"] == "Hello 漢字"
