"""Unit tests for BAC AudioConverter helper methods."""

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "big-audio-converter",
        "usr",
        "share",
        "biglinux",
        "audio-converter",
    ),
)

from app.audio.converter import AudioConverter


@pytest.fixture
def converter():
    """Create an AudioConverter without requiring ffmpeg."""
    conv = AudioConverter.__new__(AudioConverter)
    conv.ffmpeg_path = "/usr/bin/ffmpeg"
    conv.gtcrn_ladspa_path = "/usr/lib/ladspa/gtcrn.so"
    conv.cancel_flag = False
    conv.current_process = None
    return conv


class TestBuildAudioFilters:
    def test_no_filters(self, converter):
        settings = {}
        assert converter._build_audio_filters(settings) == []

    def test_volume_filter(self, converter):
        settings = {"volume": 0.5}
        filters = converter._build_audio_filters(settings)
        assert "volume=0.5" in filters

    def test_speed_filter(self, converter):
        settings = {"speed": 2.0}
        filters = converter._build_audio_filters(settings)
        assert "atempo=2.0" in filters

    def test_normalize_filter(self, converter):
        settings = {"normalize": True}
        filters = converter._build_audio_filters(settings)
        assert any("loudnorm" in f for f in filters)

    def test_noise_reduction_filter(self, converter):
        settings = {"noise_reduction": True, "noise_strength": 0.8}
        filters = converter._build_audio_filters(settings)
        assert any("ladspa" in f for f in filters)

    def test_noise_reduction_without_ladspa(self, converter):
        converter.gtcrn_ladspa_path = None
        settings = {"noise_reduction": True}
        filters = converter._build_audio_filters(settings)
        assert not any("ladspa" in f for f in filters)

    def test_hpf_filter(self, converter):
        settings = {"hpf_enabled": True, "hpf_frequency": 120}
        filters = converter._build_audio_filters(settings)
        assert any("highpass" in f and "120" in f for f in filters)

    def test_hpf_default_frequency(self, converter):
        settings = {"hpf_enabled": True}
        filters = converter._build_audio_filters(settings)
        assert any("highpass" in f and "80" in f for f in filters)

    def test_transient_filter(self, converter):
        settings = {"transient_enabled": True, "transient_attack": -0.3}
        filters = converter._build_audio_filters(settings)
        assert any("transient" in f for f in filters)

    def test_gate_filter(self, converter):
        settings = {
            "gate_enabled": True,
            "gate_intensity": 0.5,
        }
        filters = converter._build_audio_filters(settings)
        assert any("agate" in f for f in filters)

    def test_gate_intensity_affects_params(self, converter):
        import math
        settings_low = {"gate_enabled": True, "gate_intensity": 0.1}
        settings_high = {"gate_enabled": True, "gate_intensity": 0.9}
        filters_low = converter._build_audio_filters(settings_low)
        filters_high = converter._build_audio_filters(settings_high)
        gate_low = [f for f in filters_low if "agate" in f][0]
        gate_high = [f for f in filters_high if "agate" in f][0]
        assert gate_low != gate_high

    def test_compressor_filter(self, converter):
        settings = {"compressor_enabled": True, "compressor_intensity": 0.5}
        filters = converter._build_audio_filters(settings)
        assert any("acompressor" in f for f in filters)

    def test_compressor_intensity_affects_params(self, converter):
        settings_low = {"compressor_enabled": True, "compressor_intensity": 0.2}
        settings_high = {"compressor_enabled": True, "compressor_intensity": 0.9}
        filters_low = converter._build_audio_filters(settings_low)
        filters_high = converter._build_audio_filters(settings_high)
        comp_low = [f for f in filters_low if "acompressor" in f][0]
        comp_high = [f for f in filters_high if "acompressor" in f][0]
        assert comp_low != comp_high

    def test_eq_filter(self, converter):
        settings = {"eq_enabled": True, "eq_bands": "5,0,0,0,0,0,0,0,0,-3"}
        filters = converter._build_audio_filters(settings)
        eq_filters = [f for f in filters if "equalizer" in f]
        assert len(eq_filters) == 2  # 31Hz +5dB and 16kHz -3dB

    def test_eq_all_zero_no_filter(self, converter):
        settings = {"eq_enabled": True, "eq_bands": "0,0,0,0,0,0,0,0,0,0"}
        filters = converter._build_audio_filters(settings)
        assert not any("equalizer" in f for f in filters)

    def test_noise_reduction_with_model(self, converter):
        settings = {
            "noise_reduction": True,
            "noise_strength": 0.7,
            "noise_model": 1,
            "noise_speech_strength": 0.8,
            "noise_lookahead": 50,
            "noise_voice_enhance": 0.5,
            "noise_model_blend": True,
        }
        filters = converter._build_audio_filters(settings)
        nr = [f for f in filters if "gtcrn" in f][0]
        assert "c2=1" in nr  # Model VCTK
        assert "c3=0.8" in nr  # speech_strength
        assert "c4=50" in nr  # lookahead
        assert "c5=0.5" in nr  # voice_enhance
        assert "c6=1" in nr  # model_blend on

    def test_multiple_filters(self, converter):
        settings = {"volume": 0.8, "normalize": True}
        filters = converter._build_audio_filters(settings)
        assert len(filters) == 2

    def test_volume_1_no_filter(self, converter):
        settings = {"volume": 1.0}
        assert converter._build_audio_filters(settings) == []

    def test_speed_1_no_filter(self, converter):
        settings = {"speed": 1.0}
        assert converter._build_audio_filters(settings) == []


class TestFilterChainOrder:
    """Verify the filter chain order: HPF → Transient → Compressor → NR → Gate → EQ → Volume → Speed → Normalize"""

    def test_full_chain_order(self, converter):
        settings = {
            "hpf_enabled": True,
            "hpf_frequency": 80,
            "transient_enabled": True,
            "transient_attack": -0.5,
            "noise_reduction": True,
            "noise_strength": 1.0,
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
        filters = converter._build_audio_filters(settings)

        def find_idx(keyword):
            for i, f in enumerate(filters):
                if keyword in f:
                    return i
            return -1

        idx_hpf = find_idx("highpass")
        idx_trans = find_idx("transient")
        idx_comp = find_idx("acompressor")
        idx_nr = find_idx("gtcrn")
        idx_gate = find_idx("agate")
        idx_eq = find_idx("equalizer")
        idx_vol = find_idx("volume")
        idx_speed = find_idx("atempo")
        idx_norm = find_idx("loudnorm")

        assert idx_hpf < idx_trans < idx_comp < idx_nr < idx_gate < idx_eq < idx_vol < idx_speed < idx_norm

    def test_partial_chain_preserves_order(self, converter):
        settings = {
            "noise_reduction": True,
            "noise_strength": 0.8,
            "compressor_enabled": True,
            "compressor_intensity": 0.5,
            "volume": 0.5,
        }
        filters = converter._build_audio_filters(settings)

        def find_idx(keyword):
            for i, f in enumerate(filters):
                if keyword in f:
                    return i
            return -1

        idx_comp = find_idx("acompressor")
        idx_nr = find_idx("gtcrn")
        idx_vol = find_idx("volume")
        assert idx_comp < idx_nr < idx_vol


class TestBuildCodecArgs:
    def test_mp3_format(self, converter):
        settings = {"format": "mp3"}
        args = converter._build_codec_args(settings)
        assert "-f" in args
        assert "mp3" in args

    def test_aac_format(self, converter):
        settings = {"format": "aac"}
        args = converter._build_codec_args(settings)
        assert "-c:a" in args
        assert "aac" in args
        assert "-f" in args
        assert "adts" in args

    def test_bitrate(self, converter):
        settings = {"format": "mp3", "bitrate": "320k"}
        args = converter._build_codec_args(settings)
        assert "-b:a" in args
        assert "320k" in args

    def test_channels(self, converter):
        settings = {"format": "mp3"}
        args = converter._build_codec_args(settings, channels=2)
        assert "-ac" in args
        assert "2" in args

    def test_channels_ignored_for_copy(self, converter):
        settings = {"format": "copy"}
        args = converter._build_codec_args(settings, channels=2)
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


class TestSegmentProcessorValidation:
    """Test SegmentProcessor._validate_segments and _format_time."""

    @pytest.fixture
    def processor(self):
        from app.audio.segment_processor import SegmentProcessor
        return SegmentProcessor("/usr/bin/ffmpeg")

    def test_valid_segment(self, processor):
        segments = [{"start": 0.0, "stop": 5.0, "start_str": "0:00", "stop_str": "0:05"}]
        result = processor._validate_segments(segments)
        assert len(result) == 1

    def test_too_short_segment(self, processor):
        segments = [{"start": 1.0, "stop": 1.05, "start_str": "0:01", "stop_str": "0:01.05"}]
        result = processor._validate_segments(segments)
        assert len(result) == 0

    def test_missing_start(self, processor):
        segments = [{"stop": 5.0, "stop_str": "0:05"}]
        result = processor._validate_segments(segments)
        assert len(result) == 0

    def test_swaps_reversed_segment(self, processor):
        segments = [{"start": 10.0, "stop": 5.0, "start_str": "0:10", "stop_str": "0:05"}]
        result = processor._validate_segments(segments)
        assert len(result) == 1
        assert result[0]["start"] == 5.0
        assert result[0]["stop"] == 10.0

    def test_format_time(self, processor):
        result = processor._format_time(3661.5)
        assert result.startswith("01:01:")

    def test_format_time_none(self, processor):
        assert processor._format_time(None) == ""

    def test_missing_time_strings_generated(self, processor):
        segments = [{"start": 0.0, "stop": 5.0}]
        result = processor._validate_segments(segments)
        assert len(result) == 1
        assert result[0]["start_str"] != ""
        assert result[0]["stop_str"] != ""
