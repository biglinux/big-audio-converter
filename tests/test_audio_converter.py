"""Unit tests for BAC AudioConverter helper methods."""

import pytest
from app.audio.converter import AudioConverter
from app.audio.media import Segment, track_identifier
from app.audio.process import MediaError
from app.audio.profiles import build_audio_filters


def test_noise_discovery_excludes_ort_and_ll(monkeypatch):
    from pathlib import Path

    from app.audio.profiles import discover_noise_plugins

    installed = {
        "libdfn3ll_ladspa.so",
        "libdpdfnet_dpdfnet2_48khz_hr_ladspa.so",
    }
    monkeypatch.setattr(Path, "is_file", lambda path: path.name in installed)
    assert discover_noise_plugins() == {}
    installed.update({"libdfn3_ladspa.so", "libdpdfnet_native.so"})
    assert discover_noise_plugins() == {
        "dfn3": "/usr/lib/ladspa/libdfn3_ladspa.so",
        "dpdfnet": "/usr/lib/ladspa/libdpdfnet_native.so",
    }


def test_failed_worker_start_releases_conversion(monkeypatch):
    import threading

    engine = AudioConverter()
    with monkeypatch.context() as patch:

        def fail_start(_thread):
            raise RuntimeError("No worker available")

        patch.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(RuntimeError, match="No worker"):
            engine.start_batch([], {}, None, lambda *_: None)
    assert not engine.busy
    worker = engine.start_batch([], {}, None, lambda *_: None)
    worker.join(2)
    assert not worker.is_alive()
    assert not engine.busy
    engine.cleanup()


@pytest.fixture
def converter(tmp_path):
    """Create an AudioConverter without requiring ffmpeg."""
    conv = AudioConverter.__new__(AudioConverter)
    conv.ffmpeg_path = "/usr/bin/ffmpeg"
    plugin = tmp_path / "plugin.so"
    plugin.touch()
    conv.noise_plugins = {"dfn3": str(plugin), "dpdfnet": str(plugin)}
    return conv


def test_noise_reduction_without_its_plugin_is_an_error(converter):
    with pytest.raises(MediaError, match="unavailable"):
        build_audio_filters({"noise_reduction": True}, {})


@pytest.mark.parametrize(
    "engine,label,attenuation",
    # 50 % strength: half of each model's useful cap (24 dB and 48 dB).
    [
        ("dfn3", "deep_filter_net3_rs_mono", "c0=12.00"),
        ("dpdfnet", "dpdfnet_native_48hr", "c0=24.00"),
    ],
)
def test_selected_noise_model(converter, engine, label, attenuation):
    filters = build_audio_filters(
        {"noise_reduction": True, "noise_engine": engine, "noise_strength": 50},
        converter.noise_plugins,
    )
    nr = next(f for f in filters if "ladspa=" in f)
    assert f"plugin={label}" in nr
    assert attenuation in nr
    assert ("c6=0" in nr) == (engine == "dfn3")


class TestGetOutputPath:
    def test_simple_conversion(self, converter, tmp_path):
        input_path = str(tmp_path / "song.wav")
        open(input_path, "w").close()
        result = converter._get_output_path(input_path, "mp3")
        assert result.endswith(".mp3")
        assert "song" in result

    def test_same_format_adds_converted(self, converter, tmp_path):
        input_path = str(tmp_path / "song.mp3")
        open(input_path, "w").close()
        result = converter._get_output_path(input_path, "mp3")
        assert "-converted" in result

    def test_copy_format_keeps_extension(self, converter, tmp_path):
        input_path = str(tmp_path / "song.flac")
        open(input_path, "w").close()
        result = converter._get_output_path(input_path, "copy")
        assert result.endswith(".flac")

    def test_virtual_track_path(self, converter, tmp_path):
        video_path = str(tmp_path / "video.mkv")
        open(video_path, "w").close()
        track = track_identifier(video_path, 1)
        metadata = {
            track: {"source_video": video_path, "output_name": "video-track1.aac"}
        }
        result = converter._get_output_path(track, "mp3", metadata)
        assert result == str(tmp_path / "video-track1.mp3")

    def test_existing_output_increments(self, converter, tmp_path):
        input_path = str(tmp_path / "song.wav")
        open(input_path, "w").close()
        # Create the expected output so it must increment
        output1 = str(tmp_path / "song.mp3")
        open(output1, "w").close()
        result = converter._get_output_path(input_path, "mp3")
        assert result != output1
        assert result.endswith(".mp3")

    def test_prevents_overwrite_same_format(self, converter, tmp_path):
        input_path = str(tmp_path / "song.mp3")
        open(input_path, "w").close()
        result = converter._get_output_path(input_path, "mp3")
        assert result != input_path


class TestSegmentValidation:
    def test_valid_segment(self):
        segment = Segment.from_mapping({"start": 0.0, "stop": 5.0})
        assert (segment.start, segment.stop) == (0.0, 5.0)

    def test_short_segment_is_not_silently_discarded(self):
        segment = Segment.from_mapping({"start": 1.0, "stop": 1.05}, 10, 48000)
        assert segment.stop - segment.start == pytest.approx(0.05)

    def test_missing_start(self):
        with pytest.raises(MediaError):
            Segment.from_mapping({"stop": 5.0})

    def test_rejects_reversed_segment(self):
        with pytest.raises(MediaError):
            Segment.from_mapping({"start": 10.0, "stop": 5.0})

    def test_end_within_a_frame_of_the_end_means_the_end(self):
        segment = Segment.from_mapping({"start": 10, "stop": 12.346}, 12.3456789, 48000)
        assert segment.stop == 12.3456789

    def test_rejects_end_clearly_beyond_the_source(self):
        with pytest.raises(MediaError):
            Segment.from_mapping({"start": 10, "stop": 12.5}, 12.3456789, 48000)


@pytest.mark.parametrize(
    "report",
    [
        "",
        "no report",
        (
            '{"input_i" : "-inf", "input_tp" : "-inf", "input_lra" : "0.00",'
            ' "input_thresh" : "-inf", "target_offset" : "inf"}'
        ),
    ],
)
def test_unusable_loudness_measurement_falls_back_to_one_pass(report):
    from app.audio.profiles import LOUDNORM
    from app.audio.segment_processor import linear_loudnorm

    assert linear_loudnorm(report) == LOUDNORM
