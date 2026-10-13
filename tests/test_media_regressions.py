"""FFmpeg regressions: real encodes and cuts checked against the files they produce."""

import subprocess
from pathlib import Path

import numpy as np
import pytest
from app.audio.converter import AudioConverter
from app.audio.waveform import WaveformGenerator
from conftest import convert_batch, pcm, probe, pump


def converter():
    return AudioConverter()


def convert(path, settings):
    [result] = convert_batch([str(path)], settings)
    return result


def run(argv):
    return subprocess.run(argv, check=True, capture_output=True, timeout=20)


def samples(path):
    return np.frombuffer(pcm(path, "f32le"), dtype="<f4")


@pytest.fixture
def tone(tmp_path):
    p = tmp_path / "tone.wav"
    run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=997:sample_rate=48000:duration=3",
            "-metadata",
            "title=Título 東京 🎵",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(p),
        ]
    )
    return p


def settings(path, **kwargs):
    return {
        "format": "wav",
        "file_markers": {
            str(path): [
                {
                    "start": 0.5,
                    "stop": 1.5,
                    "start_str": "00:00:00.500",
                    "stop_str": "00:00:01.500",
                }
            ]
        },
        **kwargs,
    }


def test_unmodified_flac_preserves_pcm(tone):
    result = convert(tone, {"format": "flac"})
    assert result.successful
    assert np.array_equal(samples(tone), samples(result.outputs[0]))


def test_cut_respects_zero_volume(tone):
    result = convert(tone, settings(tone, volume=0))
    assert result.successful
    assert np.max(np.abs(samples(result.outputs[0]))) == 0


def test_cut_respects_double_speed(tone):
    result = convert(tone, settings(tone, speed=2))
    assert result.successful
    assert (
        abs(len(samples(result.outputs[0])) - 24000) <= 480
    )  # atempo has algorithm-dependent end padding.


def test_segments_never_overwrite_unrelated_output(tone, tmp_path):
    # The first segment's name beside the output a WAV source gets.
    target = tmp_path / "tone-converted_segment1.wav"
    target.write_bytes(b"UNRELATED USER DATA")
    st = settings(tone, cut_merge=False)
    st["file_markers"][str(tone)].append(
        {"start": 1.5, "stop": 2.5, "start_str": "1.5", "stop_str": "2.5"}
    )
    convert(tone, st)
    assert target.read_bytes() == b"UNRELATED USER DATA"


def test_exhausted_numbered_names_pick_free_path(tone, tmp_path):
    (tmp_path / "tone.mp3").write_bytes(b"old")
    for i in range(1, 101):
        (tmp_path / f"tone-converted-{i}.mp3").write_bytes(b"old")
    selected = converter()._get_output_path(str(tone), "mp3")
    assert not Path(selected).exists()


def test_all_rejected_segments_fail(tone):
    st = settings(tone, cut_merge=False)
    st["file_markers"][str(tone)] = [{"start": 0, "stop": 0}, {"start": 1, "stop": 1}]
    assert not convert(tone, st).successful


def test_removing_parts_keeps_rest_in_order(tone):
    result = convert(tone, settings(tone, cut_remove=True))
    assert result.successful
    source = samples(tone)
    assert np.array_equal(
        samples(result.outputs[0]), np.concatenate([source[:24000], source[72000:]])
    )


def test_removing_every_part_publishes_nothing(tone, tmp_path):
    st = settings(tone, cut_remove=True)
    st["file_markers"][str(tone)] = [{"start": 0, "stop": 2}, {"start": 1.5, "stop": 3}]
    assert not convert(tone, st).successful
    assert list(tmp_path.iterdir()) == [tone]


def test_partial_merge_fails(tone):
    st = settings(tone, cut_merge=True)
    st["file_markers"][str(tone)].append(
        {"start": 100, "stop": 101, "start_str": "100", "stop_str": "101"}
    )
    assert not convert(tone, st).successful


def test_real_filename_with_double_colon_works(tone, tmp_path):
    source = tmp_path / "real::name.wav"
    source.write_bytes(tone.read_bytes())
    assert convert(source, {"format": "flac"}).successful


def test_normalization_preserves_requested_original_rate(tone):
    result = convert(tone, {"format": "wav", "normalize": True})
    assert result.successful
    assert probe(result.outputs[0])["streams"][0]["sample_rate"] == "48000"


def test_cut_retains_title_metadata(tone):
    result = convert(tone, settings(tone))
    assert result.successful
    tags = probe(result.outputs[0])["format"].get("tags", {})
    assert tags.get("title") == "Título 東京 🎵"


def test_waveform_represents_right_channel(tmp_path):
    source = tmp_path / "right.wav"
    run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0|0.25*sin(2*PI*997*t):s=48000:d=2",
            "-y",
            str(source),
        ]
    )

    class Sink:
        data = None

        def set_loading(self, *args):
            pass

        def set_waveform(self, data, duration):
            self.data = data

        def queue_draw(self):
            pass

    sink = Sink()
    generator = WaveformGenerator()
    generator.request(str(source), converter(), sink)
    try:
        pump(lambda: sink.data is not None)
    finally:
        generator.cleanup()
    assert np.max(np.abs(sink.data["peaks"])) > 0.01


def test_short_segments_produce_actual_outputs(tone):
    """A 10 ms cut still produces a file."""
    st = settings(tone, cut_merge=False)
    st["file_markers"][str(tone)] = [
        {"start": 0, "stop": 0.01},
        {"start": 1, "stop": 1.01},
    ]
    result = convert(tone, st)
    assert result.successful
    assert len(result.outputs) == 2
    for output in result.outputs:
        assert len(samples(output)) == 480


def test_segments_arg_parses_ordered_ranges():
    from app.audio.media import parse_segments_arg

    segments = parse_segments_arg("1.5-3, 10-12.25")
    assert [(s["start"], s["stop"], s["segment_index"]) for s in segments] == [
        (1.5, 3.0, 1),
        (10.0, 12.25, 2),
    ]


@pytest.mark.parametrize(
    "text", ["", "5", "3-1", "2-2", "-1-4", "a-b", "1-inf", "nan-3"]
)
def test_segments_arg_rejects_malformed_ranges(text):
    from app.audio.media import parse_segments_arg
    from app.audio.process import MediaError

    with pytest.raises((MediaError, ValueError)):
        parse_segments_arg(text)
