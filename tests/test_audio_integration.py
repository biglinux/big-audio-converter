"""Real FFmpeg/ffprobe contracts for precision, metadata and editing."""

import json

import pytest
from app.audio.converter import AudioConverter
from app.audio.media import track_identifier
from conftest import command, convert_batch, pcm, probe


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


def convert(source, settings, engine=None):
    [result] = convert_batch([str(source)], settings, engine)
    assert result.successful, result
    return result


def test_missing_output_folder_fails(tmp_path, source):
    folder = tmp_path / "unmounted drive"
    [result] = convert_batch(
        [str(source)], {"format": "flac", "output_directory": str(folder)}
    )
    assert not result.successful
    assert "could not be accessed" in result.message
    assert not folder.exists()


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
    result = convert(source, {"format": "wav"})
    output = result.outputs[0]
    assert probe(output)["streams"][0]["codec_name"] == codec
    assert pcm(source) == pcm(output)


@pytest.mark.parametrize(
    "codec,bits", [("pcm_s16le", 16), ("pcm_s24le", 24), ("pcm_s32le", 32)]
)
def test_flac_preserves_integer_pcm_exactly(tmp_path, codec, bits):
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
    result = convert(source, {"format": "flac"})
    output = result.outputs[0]
    assert int(probe(output)["streams"][0]["bits_per_raw_sample"]) >= bits
    assert pcm(source) == pcm(output)


@pytest.mark.parametrize("codec", ["pcm_f32le", "pcm_f64le"])
def test_float_flac_requires_precision_opt_in(tmp_path, codec):
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
    [refused] = convert_batch([str(source)], {"format": "flac"})
    assert not refused.successful
    assert list(tmp_path.iterdir()) == [source]
    result = convert(source, {"format": "flac", "allow_precision_reduction": True})
    assert int(probe(result.outputs[0])["streams"][0]["bits_per_raw_sample"]) == 24
    assert result.notes


@pytest.mark.parametrize(
    "format,codec",
    [
        ("mp3", "mp3"),
        ("flac", "flac"),
        ("ogg", "vorbis"),
        ("wav", "pcm_s16le"),
        ("m4a", "aac"),
        ("opus", "opus"),
    ],
)
def test_every_advertised_encoding_profile(source, tmp_path, format, codec):
    result = convert(source, {"format": format, "bitrate": "192k"})
    output = probe(result.outputs[0])
    audio = next(s for s in output["streams"] if s["codec_type"] == "audio")
    assert audio["codec_name"] == codec and audio["channels"] == 1
    assert audio["sample_rate"] == "48000"
    assert abs(float(output["format"]["duration"]) - 2) < 0.15
    tags = output["format"].get("tags", {}) | audio.get("tags", {})
    assert {k.lower(): v for k, v in tags.items()}["title"] == "Title 東京 🎵"


@pytest.mark.parametrize("format,codec", [("mp3", "libmp3lame"), ("flac", "flac")])
@pytest.mark.parametrize("cut,merge", [(False, False), (True, False), (True, True)])
def test_cover_and_tags_survive_every_output(
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
        "cut_merge": merge,
        "file_markers": {
            str(tagged): [{"start": 0.1, "stop": 0.4}, {"start": 0.7, "stop": 1.1}]
        }
        if cut
        else {},
    }
    result = convert(tagged, options)
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


def test_reordered_overlapping_segments_stay_lossless(source, tmp_path):
    segments = [
        {"start": 1, "stop": 1.5, "segment_index": 1},
        {"start": 0, "stop": 0.5, "segment_index": 2},
        {"start": 0.5, "stop": 0.75, "segment_index": 3},
        {"start": 0.25, "stop": 0.5, "segment_index": 4},
    ]
    result = convert(
        source,
        {
            "format": "flac",
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
def test_special_filenames_convert_verbatim(source, tmp_path, name):
    path = tmp_path / name
    path.write_bytes(source.read_bytes())
    result = convert(path, {"format": "flac"})
    assert pcm(path) == pcm(result.outputs[0])


def test_track_identifier_keeps_second_track(source, tmp_path):
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
    result = convert(identifier, {"format": "flac", "track_metadata": metadata})
    expected = command(
        "ffmpeg", "-v", "error", "-i", str(video), "-map", "0:a:1", "-f", "s16le", "-"
    ).stdout
    assert pcm(result.outputs[0], "s16le") == expected


def test_batch_continues_after_invalid_entry(source, tmp_path):
    (tmp_path / "output").mkdir()
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
def test_denoiser_keeps_timing_channels_and_tail(tmp_path, engine_name, rate, channels):
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
        "noise_strength": 0,
    }
    converter = AudioConverter(noise_plugins=plugins)
    try:
        reference = np.frombuffer(pcm(source, "f32le"), "<f4").reshape(-1, channels)
        for cut in (False, True):
            request = dict(settings)
            if cut:
                request.update(
                    file_markers={str(source): [{"start": 0.1, "stop": 0.4}]},
                )
            target = convert(source, request, converter).outputs[0]
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
    converter.offline_denoiser = None
    try:
        [result] = convert_batch(
            [str(source)],
            {
                "format": "wav",
                "noise_reduction": True,
                "noise_engine": "dpdfnet",
            },
            converter,
        )
        assert "unavailable" in result.message
        assert list(tmp_path.iterdir()) == [source]
    finally:
        converter.cleanup()


@pytest.mark.parametrize("cut", [False, True])
def test_offline_dpdfnet_matches_plugin_exactly(tmp_path, cut):
    import numpy as np
    from app.audio.profiles import discover_noise_plugins

    plugins = discover_noise_plugins()
    converter = AudioConverter(noise_plugins=plugins)
    if "dpdfnet" not in plugins or not converter.offline_denoiser:
        converter.cleanup()
        pytest.skip("DPDFNet plugin and dpdfnet-enhance are not both installed")
    source = tmp_path / "source.wav"
    command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.2*sin(2*PI*220*t)*sin(2*PI*3*t)+0.05*(random(0)-0.5)"
        "|0.1*sin(2*PI*330*t)+0.05*(random(1)-0.5):s=44100:d=3",
        "-c:a",
        "pcm_f32le",
        str(source),
    )
    settings = {
        "format": "wav",
        "noise_reduction": True,
        "noise_engine": "dpdfnet",
        "noise_strength": 70,
        "compressor_enabled": True,
        "normalize": True,
        "file_markers": {
            str(source): [{"start": 0.5, "stop": 1.2}, {"start": 2, "stop": 2.8}]
        }
        if cut
        else {},
    }
    tool = converter.offline_denoiser
    outputs = []
    try:
        for denoiser in (None, tool):
            converter.offline_denoiser = denoiser
            target = convert(source, settings, converter).outputs[0]
            outputs.append(np.frombuffer(pcm(target, "f32le"), "<f4"))
        assert outputs[0].size and np.array_equal(outputs[0], outputs[1])
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
        [result] = convert_batch(
            [str(source)],
            {
                "format": "wav",
                "noise_reduction": True,
                "noise_engine": "dpdfnet",
                "file_markers": {str(source): [{"start": 0.01, "stop": 0.09}]}
                if cut
                else {},
            },
            converter,
        )
        assert "could not be processed" in result.message
        assert list(tmp_path.iterdir()) == [source]
    finally:
        converter.cleanup()


@pytest.mark.parametrize("engine_name", ["dfn3", "dpdfnet"])
def test_each_installed_model_attenuates_noise(tmp_path, engine_name):
    import numpy as np
    from app.audio.profiles import discover_noise_plugins

    plugins = discover_noise_plugins()
    if engine_name not in plugins:
        pytest.skip(f"Optional {engine_name} LADSPA plugin is not installed")
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
        settings = {
            "format": "wav",
            "noise_reduction": True,
            "noise_engine": engine_name,
            # Keep a dry floor: pure noise may be fully suppressed at 100 dB.
            "noise_strength": 20,
        }
        target = convert(source, settings, converter).outputs[0]
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


def test_cut_to_container_end_is_accepted(tmp_path):
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
        {
            "format": "flac",
            "file_markers": {str(source): [{"start": 1, "stop": 2.0}]},
        },
    )
    assert duration(result.outputs[0]) == pytest.approx(0.9)


@pytest.mark.parametrize("merge", [False, True])
def test_cut_output_drops_source_chapters(tmp_path, merge):
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

    full = convert(source, {"format": "mp3"})
    assert chapter_count(full.outputs[0]) == 3
    cut = convert(
        source,
        {
            "format": "mp3",
            "cut_merge": merge,
            "file_markers": {
                str(source): [{"start": 1.2, "stop": 1.8}, {"start": 2.2, "stop": 2.5}]
            },
        },
    )
    assert [chapter_count(path) for path in cut.outputs] == [0] * len(cut.outputs)


def test_conversion_survives_a_corrupt_frame(tmp_path):
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
    result = convert(damaged, {"format": "flac"})
    assert duration(result.outputs[0]) > 9.5


def test_joined_cuts_normalize_at_source_rate(tmp_path):
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
    engine.start_batch(
        [str(source)],
        {
            "format": "wav",
            "normalize": True,
            "file_markers": {
                str(source): [{"start": 1, "stop": 5}, {"start": 6, "stop": 11}]
            },
        },
        lambda _index, _path, fraction: fractions.append(fraction),
        lambda: None,
    ).join(120)
    [result] = engine.last_batch_result.items
    engine.cleanup()
    assert result.successful, result
    output = result.outputs[0]
    assert probe(output)["streams"][0]["sample_rate"] == "44100"
    assert duration(output) == pytest.approx(9)
    summary = command(
        "ffmpeg", "-nostats", "-i", output, "-af", "ebur128", "-f", "null", "-"
    ).stderr.decode()
    loudness = float(summary.rsplit("I:", 1)[1].split()[0])
    assert loudness == pytest.approx(-16, abs=0.5)
    # A cut job reports progress while FFmpeg runs, not only when it ends.
    assert any(0 < fraction < 1 for fraction in fractions)


def test_hundreds_of_cuts_join_exactly(source, tmp_path):
    segments = [{"start": n * 0.005, "stop": n * 0.005 + 0.002} for n in range(300)]
    result = convert(
        source,
        {
            "format": "flac",
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


def test_batch_cuts_only_marked_files(source, tmp_path):
    other = tmp_path / "other.wav"
    other.write_bytes(source.read_bytes())
    (tmp_path / "output").mkdir()
    engine = AudioConverter()
    engine.start_batch(
        [str(source), str(other)],
        {
            "format": "flac",
            "output_directory": str(tmp_path / "output"),
            "file_markers": {str(source): [{"start": 0.5, "stop": 1}]},
        },
        None,
        lambda: None,
    ).join(30)
    first, second = engine.last_batch_result.items
    engine.cleanup()
    assert duration(first.outputs[0]) == pytest.approx(0.5)
    assert duration(second.outputs[0]) == pytest.approx(duration(other))


def tone(path, *filters):
    command(
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        *filters, "-ar", "48000", str(path),
    )  # fmt: skip
    return path


@pytest.mark.parametrize("cuts_per_run", [None, 2])
def test_queue_joins_into_one_ordered_file(source, tmp_path, monkeypatch, cuts_per_run):
    if cuts_per_run:
        # Three inputs then pass through lossless parts.
        monkeypatch.setattr("app.audio.segment_renderer.CUTS_PER_RUN", cuts_per_run)
    other = tone(tmp_path / "other.wav")
    cuts = [{"start": 1.5, "stop": 2}, {"start": 0.25, "stop": 0.5}]
    (tmp_path / "output").mkdir()
    engine = AudioConverter()
    engine.start_batch(
        [str(source), str(other)],
        {
            "format": "flac",
            "output_directory": str(tmp_path / "output"),
            "order_by_segment_number": False,
            "file_markers": {str(source): cuts},
            "join_files": True,
        },
        None,
        lambda: None,
    ).join(30)
    first, second = engine.last_batch_result.items
    engine.cleanup()
    [output] = first.outputs
    assert output.endswith("/source-joined.flac")
    assert (second.status, second.outputs) == ("success", ())
    assert second.message == "Joined into source-joined.flac"
    original = pcm(source, "s16le")
    expected = original[24000:48000] + original[144000:192000] + pcm(other, "s16le")
    assert pcm(output, "s16le") == expected
    tags = probe(output)["format"]["tags"]
    assert "title" not in {key.lower() for key in tags}
    assert tags["artist"] == "Artist"


def test_joining_keeps_stereo_after_mono(source, tmp_path):
    import numpy as np

    stereo = tone(tmp_path / "wide.wav", "-af", "pan=stereo|c0=c0|c1=-1*c0")
    engine = AudioConverter()
    engine.start_batch(
        [str(source), str(stereo)],
        {"format": "wav", "output_directory": str(tmp_path), "join_files": True},
        None,
        lambda: None,
    ).join(30)
    [output] = engine.last_batch_result.items[0].outputs
    engine.cleanup()
    audio = np.frombuffer(pcm(output, "f32le"), "<f4").reshape(-1, 2)
    wide = audio[96000:]
    assert len(wide) == 48000
    # Folding the file to mono first would cancel its opposite channels.
    assert np.abs(wide[:, 0]).max() > 0.1
    assert np.allclose(wide[:, 0], -wide[:, 1], atol=1e-4)


def test_fast_copy_refuses_to_join(source, tmp_path):
    engine = AudioConverter()
    engine.start_batch(
        [str(source), str(tone(tmp_path / "other.wav"))],
        {
            "format": "copy",
            "output_directory": str(tmp_path / "out"),
            "join_files": True,
        },
        None,
        lambda: None,
    ).join(30)
    items = engine.last_batch_result.items
    engine.cleanup()
    assert [item.status for item in items] == ["failed", "failed"]
    assert "Keeping the original format cannot join" in items[1].message
    assert not (tmp_path / "out").exists()


def test_mono_gets_half_the_stereo_bitrate(source, tmp_path):
    def bitrate(result):
        output = probe(result.outputs[0])
        audio = next(s for s in output["streams"] if s["codec_type"] == "audio")
        return int(audio["bit_rate"])

    settings = {"format": "mp3", "bitrate": "192k", "bitrate_per_channel": True}
    assert bitrate(convert(source, settings)) == 96000
    settings["bitrate_per_channel"] = False
    assert bitrate(convert(source, settings)) == 192000
