"""Real FFmpeg/ffprobe contracts for precision, metadata and editing."""

import json
import subprocess

import pytest
from app.audio.converter import AudioConverter
from app.audio.media import track_identifier


def command(*args):
    return subprocess.run(list(args), check=True, capture_output=True, timeout=15)


def probe(path):
    return json.loads(
        command(
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ).stdout
    )


def pcm(path, representation="f64le"):
    return command(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-f",
        representation,
        "-",
    ).stdout


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.wav"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=997:sample_rate=48000:duration=2",
        "-metadata",
        "title=Title 東京 🎵",
        "-metadata",
        "artist=Artist",
        str(path),
    )
    return path


def convert(source, output, settings):
    engine = AudioConverter()
    assert engine.convert_file(str(source), str(output), settings), engine.last_result
    return engine.last_result


@pytest.mark.parametrize(
    "codec", ["pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le"]
)
def test_wav_preserves_every_supported_pcm_representation(tmp_path, codec):
    source = tmp_path / "input.wav"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.123456789*sin(2*PI*997*t):s=48000:d=0.1",
        "-c:a",
        codec,
        str(source),
    )
    result = convert(source, tmp_path / "output.wav", {"format": "wav"})
    output = result.outputs[0]
    assert probe(output)["streams"][0]["codec_name"] == codec
    assert pcm(source) == pcm(output)


@pytest.mark.parametrize(
    "codec,bits", [("pcm_s16le", 16), ("pcm_s24le", 24), ("pcm_s32le", 32)]
)
def test_flac_preserves_integer_pcm_without_silent_24_bit_truncation(
    tmp_path, codec, bits
):
    source = tmp_path / "input.wav"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.123456789*sin(2*PI*997*t):s=48000:d=0.1",
        "-c:a",
        codec,
        str(source),
    )
    result = convert(source, tmp_path / "output.flac", {"format": "flac"})
    output = result.outputs[0]
    assert int(probe(output)["streams"][0]["bits_per_raw_sample"]) >= bits
    assert pcm(source) == pcm(output)


@pytest.mark.parametrize("codec", ["pcm_f32le", "pcm_f64le"])
def test_float_to_flac_requires_explicit_precision_reduction(tmp_path, codec):
    source = tmp_path / "input.wav"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.123456789*sin(2*PI*997*t):s=48000:d=0.1",
        "-c:a",
        codec,
        str(source),
    )
    engine = AudioConverter()
    output = tmp_path / "output.flac"
    assert not engine.convert_file(str(source), str(output), {"format": "flac"})
    assert not output.exists()
    result = convert(
        source, output, {"format": "flac", "allow_precision_reduction": True}
    )
    assert int(probe(output)["streams"][0]["bits_per_raw_sample"]) == 24
    assert result.warnings


@pytest.mark.parametrize(
    "format,codec",
    [
        ("mp3", "mp3"),
        ("flac", "flac"),
        ("ogg", "vorbis"),
        ("wav", "pcm_s16le"),
        ("aac", "aac"),
        ("opus", "opus"),
    ],
)
def test_every_advertised_encoding_profile(source, tmp_path, format, codec):
    result = convert(
        source, tmp_path / f"output.{format}", {"format": format, "bitrate": "192k"}
    )
    output = probe(result.outputs[0])
    audio = next(s for s in output["streams"] if s["codec_type"] == "audio")
    assert audio["codec_name"] == codec and audio["channels"] == 1
    assert audio["sample_rate"] == "48000"
    assert abs(float(output["format"]["duration"]) - 2) < 0.15
    if format != "aac":
        tags = output["format"].get("tags", {}) | audio.get("tags", {})
        assert {k.lower(): v for k, v in tags.items()}["title"] == "Title 東京 🎵"


@pytest.mark.parametrize("format,codec", [("mp3", "libmp3lame"), ("flac", "flac")])
@pytest.mark.parametrize("cut,merge", [(False, False), (True, False), (True, True)])
def test_supported_cover_and_tags_survive_full_cut_and_merged_output(
    source, tmp_path, format, codec, cut, merge
):
    picture = tmp_path / "cover.png"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=blue:s=32x32",
        "-frames:v",
        "1",
        "-threads",
        "1",
        str(picture),
    )
    tagged = tmp_path / f"tagged.{format}"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(source),
        "-i",
        str(picture),
        "-map",
        "0:a:0",
        "-map",
        "1:v:0",
        "-c:a",
        codec,
        "-c:v",
        "copy",
        "-disposition:v",
        "attached_pic",
        str(tagged),
    )
    options = {
        "format": format,
        "cut_enabled": cut,
        "cut_merge": merge,
        "file_markers": {
            str(tagged): [{"start": 0.1, "stop": 0.4}, {"start": 0.7, "stop": 1.1}]
        },
    }
    result = convert(tagged, tmp_path / f"edited.{format}", options)
    for path in result.outputs:
        info = probe(path)
        assert info["format"]["tags"]["title"] == "Title 東京 🎵"
        assert info["format"]["tags"]["artist"] == "Artist"
        assert any(
            stream.get("disposition", {}).get("attached_pic")
            for stream in info["streams"]
        )
        cover = command(
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-f",
            "image2pipe",
            "-",
        ).stdout
        assert cover == picture.read_bytes()


def test_reordered_adjacent_overlapping_segments_have_exact_lossless_samples(
    source, tmp_path
):
    segments = [
        {"start": 1, "stop": 1.5, "segment_index": 1},
        {"start": 0, "stop": 0.5, "segment_index": 2},
        {"start": 0.5, "stop": 0.75, "segment_index": 3},
        {"start": 0.25, "stop": 0.5, "segment_index": 4},
    ]
    result = convert(
        source,
        tmp_path / "joined.flac",
        {
            "format": "flac",
            "cut_enabled": True,
            "cut_merge": True,
            "order_by_segment_number": True,
            "file_markers": {str(source): segments},
        },
    )
    original = pcm(source, "s16le")
    expected = b"".join(
        original[round(item["start"] * 48000) * 2 : round(item["stop"] * 48000) * 2]
        for item in segments
    )
    assert pcm(result.outputs[0], "s16le") == expected


@pytest.mark.parametrize(
    "name",
    [
        "-option.wav",
        "a b.wav",
        'a"b.wav',
        "a'b.wav",
        "[]()&$;.wav",
        "line\nbreak.wav",
        "🎵東京שלום.wav",
        "real::name.wav",
    ],
)
def test_special_filenames_are_not_shell_syntax(source, tmp_path, name):
    path = tmp_path / name
    path.write_bytes(source.read_bytes())
    result = convert(path, tmp_path / "export.flac", {"format": "flac"})
    assert pcm(path) == pcm(result.outputs[0])


def test_explicit_second_track_remains_second_after_opaque_identifier(source, tmp_path):
    video = tmp_path / "multi.mkv"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(source),
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.25:s=48000:d=2",
        "-map",
        "0:a:0",
        "-map",
        "1:a:0",
        "-c:a",
        "flac",
        str(video),
    )
    identifier = track_identifier(video, 1)
    metadata = {
        identifier: {
            "source_video": str(video),
            "track_index": 1,
            "output_name": "track-2.flac",
        }
    }
    result = convert(
        identifier,
        tmp_path / "track.flac",
        {"format": "flac", "track_metadata": metadata},
    )
    expected = command(
        "ffmpeg", "-v", "error", "-i", str(video), "-map", "0:a:1", "-f", "s16le", "-"
    ).stdout
    assert pcm(result.outputs[0], "s16le") == expected


def test_invalid_batch_entry_does_not_prevent_next_file(source, tmp_path):
    engine = AudioConverter()
    engine.start_batch(
        ["", str(source)],
        {"format": "flac", "output_directory": str(tmp_path / "output")},
        None,
        lambda: None,
    ).join(30)
    assert [item.status for item in engine.last_batch_result.items] == [
        "failed",
        "success",
    ]
    engine.cleanup()


@pytest.mark.parametrize("engine_name", ["dfn3", "dpdfnet"])
@pytest.mark.parametrize("rate,channels", [(48000, 2), (44100, 1), (96000, 2)])
def test_denoiser_preserves_timing_channels_and_cut_tail(
    tmp_path, engine_name, rate, channels
):
    import numpy as np
    from app.audio.profiles import discover_noise_plugins

    plugins = discover_noise_plugins()
    if engine_name not in plugins:
        pytest.skip(f"Optional {engine_name} LADSPA plugin is not installed")
    source = tmp_path / "source.wav"
    # Different signals in both channels expose downmixing and channel swaps.
    expression = "0.15*sin(2*PI*719*t)"
    if channels == 2:
        expression += "|0.09*sin(2*PI*1283*t)"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc={expression}:s={rate}:d=0.5",
        "-c:a",
        "pcm_f32le",
        str(source),
    )
    settings = {
        "format": "wav",
        "noise_reduction": True,
        "noise_engine": engine_name,
        # Exercise the real plugin's delay without changing the expected samples.
        "noise_attenuation_db": 0,
    }
    converter = AudioConverter(noise_plugins=plugins)
    try:
        reference = np.frombuffer(pcm(source, "f32le"), "<f4").reshape(-1, channels)
        for cut in (False, True):
            request = dict(settings)
            if cut:
                request.update(
                    cut_enabled=True,
                    file_markers={str(source): [{"start": 0.1, "stop": 0.4}]},
                )
            target = tmp_path / f"output-{cut}.wav"
            assert converter.convert_file(str(source), str(target), request), (
                converter.last_result
            )
            info = probe(target)["streams"][0]
            assert int(info["sample_rate"]) == rate
            assert info["channels"] == channels
            actual = np.frombuffer(pcm(target, "f32le"), "<f4").reshape(-1, channels)
            expected = (
                reference[int(rate * 0.1) : int(rate * 0.4)] if cut else reference
            )
            assert len(actual) == len(expected)
            # Resampling boundaries can ring; both the opening and the final
            # 50 ms must still contain the expected channel, in the right place.
            for a, b in (
                (int(rate * 0.003), int(rate * 0.05)),
                (-int(rate * 0.05), -int(rate * 0.003)),
            ):
                assert np.max(np.abs(actual[a:b] - expected[a:b])) < 0.005
    finally:
        converter.cleanup()


def test_missing_noise_plugin_never_publishes_output(tmp_path):
    source = tmp_path / "source.wav"
    command(
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.1", str(source)
    )
    converter = AudioConverter(noise_plugins={})
    try:
        target = tmp_path / "output.wav"
        assert not converter.convert_file(
            str(source),
            str(target),
            {
                "format": "wav",
                "noise_reduction": True,
                "noise_engine": "dpdfnet",
            },
        )
        assert "unavailable" in converter.last_result.message
        assert not target.exists()
    finally:
        converter.cleanup()


@pytest.mark.parametrize("cut", [False, True])
def test_dpdfnet_missing_model_never_publishes_output(tmp_path, monkeypatch, cut):
    from app.audio.profiles import discover_noise_plugins

    plugins = discover_noise_plugins()
    if "dpdfnet" not in plugins:
        pytest.skip("Optional DPDFNet LADSPA plugin is not installed")
    source = tmp_path / "source.wav"
    command(
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.1", str(source)
    )
    monkeypatch.setenv("DPDFNET_NATIVE_MODEL", str(tmp_path / "missing-model"))
    converter = AudioConverter(noise_plugins=plugins)
    try:
        target = tmp_path / "output.wav"
        assert not converter.convert_file(
            str(source),
            str(target),
            {
                "format": "wav",
                "noise_reduction": True,
                "noise_engine": "dpdfnet",
                "cut_enabled": cut,
                "file_markers": {str(source): [{"start": 0.01, "stop": 0.09}]},
            },
        )
        assert "could not be processed" in converter.last_result.message
        assert not target.exists()
    finally:
        converter.cleanup()


@pytest.mark.parametrize("engine_name", ["dfn3", "dpdfnet"])
def test_native_denoiser_does_not_need_onnx_runtime(tmp_path, monkeypatch, engine_name):
    import numpy as np
    from app.audio.profiles import discover_noise_plugins

    plugins = discover_noise_plugins()
    if engine_name not in plugins:
        pytest.skip(f"Optional {engine_name} LADSPA plugin is not installed")
    monkeypatch.setenv("ORT_DYLIB_PATH", str(tmp_path / "missing-runtime.so"))
    source = tmp_path / "noise.wav"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "anoisesrc=sample_rate=48000:duration=0.5:amplitude=0.1:seed=552",
        "-c:a",
        "pcm_f32le",
        str(source),
    )
    converter = AudioConverter(noise_plugins=plugins)
    try:
        target = tmp_path / "output.wav"
        assert converter.convert_file(
            str(source),
            str(target),
            {
                "format": "wav",
                "noise_reduction": True,
                "noise_engine": engine_name,
                # Keep a dry floor: pure noise may be fully suppressed at 100 dB.
                "noise_attenuation_db": 20,
            },
        ), converter.last_result
        original = np.frombuffer(pcm(source, "f32le"), "<f4")
        actual = np.frombuffer(pcm(target, "f32le"), "<f4")
        assert len(actual) == len(original)
        assert np.isfinite(actual).all()
        # A successful exit must represent denoising, not a dry fallback or mute.
        assert 0 < np.mean(actual**2) < 0.5 * np.mean(original**2)
    finally:
        converter.cleanup()


def duration(path):
    return float(probe(path)["format"]["duration"])


def test_cut_to_the_container_end_is_accepted_when_the_audio_ends_earlier(tmp_path):
    # Players hand over the container's end: 2.0 s, while this track ends at 1.9 s.
    source = tmp_path / "two.mov"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=997:sample_rate=48000:duration=1.9",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.1:s=48000:d=2",
        "-map",
        "0",
        "-map",
        "1",
        "-c:a",
        "pcm_s16le",
        str(source),
    )
    result = convert(
        source,
        tmp_path / "end.flac",
        {
            "format": "flac",
            "cut_enabled": True,
            "file_markers": {str(source): [{"start": 1, "stop": 2.0}]},
        },
    )
    assert duration(result.outputs[0]) == pytest.approx(0.9)


@pytest.mark.parametrize("merge", [False, True])
def test_cut_output_drops_source_chapters_at_unedited_times(tmp_path, merge):
    chapters = tmp_path / "chapters.txt"
    chapters.write_text(
        ";FFMETADATA1\n"
        + "".join(
            f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={n * 1000}\nEND={n * 1000 + 1000}\n"
            for n in range(3)
        )
    )
    source = tmp_path / "chapters.mka"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=997:sample_rate=48000:duration=3",
        "-i",
        str(chapters),
        "-map",
        "0",
        "-map_chapters",
        "1",
        str(source),
    )

    def chapter_count(path):
        return len(
            json.loads(
                command(
                    "ffprobe", "-v", "error", "-show_chapters", "-of", "json", path
                ).stdout
            )["chapters"]
        )

    full = convert(source, tmp_path / "full.mp3", {"format": "mp3"})
    assert chapter_count(full.outputs[0]) == 3
    cut = convert(
        source,
        tmp_path / "cut.mp3",
        {
            "format": "mp3",
            "cut_enabled": True,
            "cut_merge": merge,
            "file_markers": {
                str(source): [{"start": 1.2, "stop": 1.8}, {"start": 2.2, "stop": 2.5}]
            },
        },
    )
    assert [chapter_count(path) for path in cut.outputs] == [0] * len(cut.outputs)


def test_one_corrupt_frame_does_not_abort_the_conversion(tmp_path):
    source = tmp_path / "clean.mp3"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100:duration=10",
        "-c:a",
        "libmp3lame",
        str(source),
    )
    data = bytearray(source.read_bytes())
    middle = len(data) // 2
    for offset in range(middle, middle + 3000, 7):
        data[offset] ^= 0xFF
    damaged = tmp_path / "damaged.mp3"
    damaged.write_bytes(data)
    result = convert(damaged, tmp_path / "damaged.flac", {"format": "flac"})
    assert duration(result.outputs[0]) > 9.5


def test_normalization_measures_the_joined_cuts_and_keeps_the_rate(tmp_path):
    source = tmp_path / "quiet.wav"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.05*sin(2*PI*440*t)*(1+0.8*sin(2*PI*0.5*t)):s=44100:d=12",
        "-c:a",
        "pcm_s16le",
        str(source),
    )
    fractions = []
    engine = AudioConverter()
    assert engine.convert_file(
        str(source),
        str(tmp_path / "loud.wav"),
        {
            "format": "wav",
            "normalize": True,
            "prevent_clipping": True,
            "cut_enabled": True,
            "file_markers": {
                str(source): [{"start": 1, "stop": 5}, {"start": 6, "stop": 11}]
            },
        },
        fractions.append,
    ), engine.last_result
    output = engine.last_result.outputs[0]
    assert probe(output)["streams"][0]["sample_rate"] == "44100"
    assert duration(output) == pytest.approx(9)
    summary = command(
        "ffmpeg", "-nostats", "-i", output, "-af", "ebur128", "-f", "null", "-"
    ).stderr.decode()
    loudness = float(summary.rsplit("I:", 1)[1].split()[0])
    assert loudness == pytest.approx(-16, abs=0.5)
    # A cut job reports progress while FFmpeg runs, not only when it ends.
    assert any(0 < fraction < 1 for fraction in fractions)


def test_hundreds_of_cuts_are_joined_with_exact_samples(source, tmp_path):
    segments = [{"start": n * 0.005, "stop": n * 0.005 + 0.002} for n in range(300)]
    result = convert(
        source,
        tmp_path / "many.flac",
        {
            "format": "flac",
            "cut_enabled": True,
            "file_markers": {str(source): segments},
        },
    )
    original = pcm(source, "s16le")
    expected = b"".join(
        original[round(item["start"] * 48000) * 2 : round(item["stop"] * 48000) * 2]
        for item in segments
    )
    assert pcm(result.outputs[0], "s16le") == expected
    assert probe(result.outputs[0])["format"]["tags"]["title"] == "Title 東京 🎵"


def test_batch_cuts_only_the_file_that_has_markers(source, tmp_path):
    other = tmp_path / "other.wav"
    other.write_bytes(source.read_bytes())
    engine = AudioConverter()
    engine.start_batch(
        [str(source), str(other)],
        {
            "format": "flac",
            "output_directory": str(tmp_path / "output"),
            "cut_enabled": True,
            "file_markers": {str(source): [{"start": 0.5, "stop": 1}]},
        },
        None,
        lambda: None,
    ).join(30)
    first, second = engine.last_batch_result.items
    engine.cleanup()
    assert duration(first.outputs[0]) == pytest.approx(0.5)
    assert duration(second.outputs[0]) == pytest.approx(duration(other))
