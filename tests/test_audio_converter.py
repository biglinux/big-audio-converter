"""Unit tests for BAC AudioConverter helper methods."""

import pytest
from app.audio.converter import AudioConverter
from app.audio.media import Segment
from app.audio.process import MediaError
from app.audio.profiles import build_audio_filters, codec_args


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


class TestBuildAudioFilters:
    def test_no_filters(self, converter):
        settings = {}
        assert build_audio_filters(settings, converter.noise_plugins) == []

    def test_volume_filter(self, converter):
        settings = {"volume": 0.5}
        filters = build_audio_filters(settings, converter.noise_plugins)
        assert "volume=0.5" in filters

    def test_speed_filter(self, converter):
        settings = {"speed": 2.0}
        filters = build_audio_filters(settings, converter.noise_plugins)
        assert "atempo=2.0" in filters

    def test_normalize_filter(self, converter):
        settings = {"normalize": True}
        filters = build_audio_filters(settings, converter.noise_plugins)
        assert any("loudnorm" in f for f in filters)

    def test_noise_reduction_filter(self, converter):
        settings = {"noise_reduction": True, "noise_strength": 80}
        filters = build_audio_filters(settings, converter.noise_plugins)
        assert any("ladspa" in f for f in filters)

    def test_noise_reduction_without_ladspa(self, converter):
        converter.noise_plugins = {}
        settings = {"noise_reduction": True}
        with pytest.raises(MediaError, match="unavailable"):
            build_audio_filters(settings, converter.noise_plugins)

    def test_hpf_filter(self, converter):
        settings = {"hpf_enabled": True, "hpf_frequency": 120}
        filters = build_audio_filters(settings, converter.noise_plugins)
        assert any("highpass" in f and "120" in f for f in filters)

    def test_hpf_default_frequency(self, converter):
        settings = {"hpf_enabled": True}
        filters = build_audio_filters(settings, converter.noise_plugins)
        assert any("highpass" in f and "80" in f for f in filters)

    def test_gate_filter(self, converter):
        settings = {
            "gate_enabled": True,
            "gate_intensity": 0.5,
        }
        filters = build_audio_filters(settings, converter.noise_plugins)
        assert any("agate" in f for f in filters)

    def test_gate_intensity_affects_params(self, converter):
        settings_low = {"gate_enabled": True, "gate_intensity": 0.1}
        settings_high = {"gate_enabled": True, "gate_intensity": 0.9}
        filters_low = build_audio_filters(settings_low, converter.noise_plugins)
        filters_high = build_audio_filters(settings_high, converter.noise_plugins)
        gate_low = next(f for f in filters_low if "agate" in f)
        gate_high = next(f for f in filters_high if "agate" in f)
        assert gate_low != gate_high

    def test_compressor_filter(self, converter):
        settings = {"compressor_enabled": True, "compressor_intensity": 0.5}
        filters = build_audio_filters(settings, converter.noise_plugins)
        assert any("acompressor" in f for f in filters)

    def test_compressor_intensity_affects_params(self, converter):
        settings_low = {"compressor_enabled": True, "compressor_intensity": 0.2}
        settings_high = {"compressor_enabled": True, "compressor_intensity": 0.9}
        filters_low = build_audio_filters(settings_low, converter.noise_plugins)
        filters_high = build_audio_filters(settings_high, converter.noise_plugins)
        comp_low = next(f for f in filters_low if "acompressor" in f)
        comp_high = next(f for f in filters_high if "acompressor" in f)
        assert comp_low != comp_high

    def test_eq_filter(self, converter):
        settings = {"eq_enabled": True, "eq_bands": "5,0,0,0,0,0,0,0,0,-3"}
        filters = build_audio_filters(settings, converter.noise_plugins)
        eq_filters = [f for f in filters if "equalizer" in f]
        assert len(eq_filters) == 2  # 31Hz +5dB and 16kHz -3dB

    def test_eq_all_zero_no_filter(self, converter):
        settings = {"eq_enabled": True, "eq_bands": "0,0,0,0,0,0,0,0,0,0"}
        filters = build_audio_filters(settings, converter.noise_plugins)
        assert not any("equalizer" in f for f in filters)

    @pytest.mark.parametrize(
        "engine,label,attenuation",
        # 50 % strength: half of each model's useful cap (24 dB and 48 dB).
        [
            ("dfn3", "deep_filter_net3_rs_mono", "c0=12.00"),
            ("dpdfnet", "dpdfnet_native_48hr", "c0=24.00"),
        ],
    )
    def test_selected_noise_model(self, converter, engine, label, attenuation):
        filters = build_audio_filters(
            {
                "noise_reduction": True,
                "noise_engine": engine,
                "noise_strength": 50,
            },
            converter.noise_plugins,
        )
        nr = next(f for f in filters if "ladspa=" in f)
        assert f"plugin={label}" in nr
        assert attenuation in nr
        assert ("c6=0" in nr) == (engine == "dfn3")

    def test_multiple_filters(self, converter):
        settings = {"volume": 0.8, "normalize": True}
        filters = build_audio_filters(settings, converter.noise_plugins)
        assert len(filters) == 2

    def test_volume_1_no_filter(self, converter):
        settings = {"volume": 1.0}
        assert build_audio_filters(settings, converter.noise_plugins) == []

    def test_speed_1_no_filter(self, converter):
        settings = {"speed": 1.0}
        assert build_audio_filters(settings, converter.noise_plugins) == []


class TestFilterChainOrder:
    """Verify the filter chain order: HPF → Compressor → NR → Gate → EQ → Volume → Speed → Normalize"""

    def test_full_chain_order(self, converter):
        settings = {
            "hpf_enabled": True,
            "hpf_frequency": 80,
            "noise_reduction": True,
            "noise_strength": 100,
            "gate_enabled": True,
            "gate_intensity": 0.5,
            "compressor_enabled": True,
            "compressor_intensity": 0.5,
            "eq_enabled": True,
            "eq_bands": "5,0,0,0,0,0,0,0,0,0",
            "volume": 0.8,
            "speed": 1.5,
            "normalize": True,
        }
        filters = build_audio_filters(settings, converter.noise_plugins)

        def find_idx(keyword):
            for i, f in enumerate(filters):
                if keyword in f:
                    return i
            return -1

        idx_hpf = find_idx("highpass")
        idx_comp = find_idx("acompressor")
        idx_nr = find_idx("ladspa=")
        idx_gate = find_idx("agate")
        idx_eq = find_idx("equalizer")
        idx_vol = find_idx("volume")
        idx_speed = find_idx("atempo")
        idx_norm = find_idx("loudnorm")

        assert (
            idx_hpf
            < idx_comp
            < idx_nr
            < idx_gate
            < idx_eq
            < idx_vol
            < idx_speed
            < idx_norm
        )

    def test_partial_chain_preserves_order(self, converter):
        settings = {
            "noise_reduction": True,
            "noise_strength": 80,
            "compressor_enabled": True,
            "compressor_intensity": 0.5,
            "volume": 0.5,
        }
        filters = build_audio_filters(settings, converter.noise_plugins)

        def find_idx(keyword):
            for i, f in enumerate(filters):
                if keyword in f:
                    return i
            return -1

        idx_comp = find_idx("acompressor")
        idx_nr = find_idx("ladspa=")
        idx_vol = find_idx("volume")
        assert idx_comp < idx_nr < idx_vol


class TestBuildCodecArgs:
    def test_mp3_format(self, converter):
        settings = {"format": "mp3"}
        args = codec_args(settings)
        assert "-f" in args
        assert "mp3" in args

    def test_aac_format(self, converter):
        settings = {"format": "aac"}
        args = codec_args(settings)
        assert "-c:a" in args
        assert "aac" in args
        assert "-f" in args
        assert "adts" in args

    def test_bitrate(self, converter):
        settings = {"format": "mp3", "bitrate": "320k"}
        args = codec_args(settings)
        assert "-b:a" in args
        assert "320k" in args

    def test_channels(self, converter):
        settings = {"format": "mp3"}
        args = codec_args(settings, channels=2)
        assert "-ac" in args
        assert "2" in args

    def test_channels_ignored_for_copy(self, converter):
        settings = {"format": "copy"}
        args = codec_args(settings, channels=2)
        assert "-ac" not in args


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
        input_path = f"{video_path}::track1.aac"
        result = converter._get_output_path(input_path, "mp3")
        assert "video" in result
        assert "track1" in result
        assert result.endswith(".mp3")

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
