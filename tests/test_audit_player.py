"""Exercise real libmpv playback through a null audio device, never a fake player."""

from pathlib import Path
import threading
import time

import pytest
from test_audit_audio import tone, run
from test_audit_waveform import pump
from app.audio.player import AudioPlayer


@pytest.fixture
def player():
    instance = AudioPlayer(audio_output="null")
    assert instance.mpv_instance is not None, instance.initialization_error
    errors = []
    def error_callback(sender, message):
        assert threading.current_thread() is threading.main_thread()
        errors.append(message)
    instance.connect("error", error_callback)
    yield instance, errors
    instance.cleanup()


def test_native_play_pause_seek_and_cleanup(player, tone):
    instance, errors = player
    events = []
    def position(sender, current, duration):
        assert threading.current_thread() is threading.main_thread()
        events.append(current)
    instance.connect("position-updated", position)
    assert instance.load(str(tone))
    assert instance.play()
    pump(lambda: instance._loaded and instance._position > 0.1)
    assert instance.seek(1.5)
    pump(lambda: instance._position >= 1.5)
    assert instance.pause()
    assert instance.position_timer_id is None
    assert not instance.is_playing()
    instance.cleanup()
    instance.cleanup()
    assert instance.mpv_instance is None
    assert not instance._sources._sources
    assert not instance._events._sources
    assert not errors
    assert events


def test_rapid_file_changes_deliver_the_latest_load(player, tone, tmp_path):
    instance, errors = player
    other = tmp_path / "other.wav"
    run("ffmpeg", "-v", "error", "-i", str(tone), "-t", "1", str(other))
    for index in range(100):
        assert instance.load(str(tone if index % 2 else other))
    assert instance.load(str(other))
    instance.play()
    pump(lambda: instance._loaded and instance.duration > 0)
    assert instance.current_actual_file == str(other)
    assert instance.duration == pytest.approx(1, abs=0.02)
    assert not errors


def test_ffprobe_stream_index_is_mapped_to_mpv_audio_id(player, tmp_path):
    path = tmp_path / "tracks.mkv"
    run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=16x16:d=2",
        "-f", "lavfi", "-i", "sine=frequency=400:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=800:duration=2",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1", "-c:a", "flac", str(path))
    instance, errors = player
    identifier = str(path) + "::track-2"
    assert instance.load(identifier, {identifier: {"source_video": str(path), "track_index": 2}})
    instance.play()
    pump(lambda: instance._loaded)
    selected = next(track for track in instance.mpv_instance.track_list if track.get("type") == "audio" and track.get("selected"))
    assert selected["ff-index"] == 2
    assert not errors


def test_preview_controls_preserve_linear_gain_and_low_speed(player, tone):
    instance, errors = player
    instance.set_volume(2)
    instance.set_playback_speed(0.1)
    assert instance._preview_settings()["volume"] == 2
    assert "volume=2" in instance._last_filter_chain
    assert "min-speed=0.1" in instance._last_filter_chain
    assert instance.mpv_instance.speed == pytest.approx(0.1)
    assert instance.load(str(tone))
    instance.play()
    pump(lambda: instance._position > 0.025)
    assert not errors


def test_copy_preview_does_not_destroy_effect_preferences(player):
    instance, errors = player
    instance.set_volume(2)
    instance.set_playback_speed(2)
    instance.set_bypass_processing(True)
    assert instance.volume == 2
    assert instance.speed == 2
    assert instance.mpv_instance.speed == 1
    assert instance._last_filter_chain == ""
    instance.set_bypass_processing(False)
    assert instance.mpv_instance.speed == 2
    assert "volume=2" in instance._last_filter_chain


def test_eof_is_signalled_once_and_replay_works(player, tone):
    instance, errors = player
    calls = []
    instance.connect("eos", lambda sender: calls.append(1))
    instance.set_playback_speed(5)
    instance.load(str(tone))
    instance.play()
    pump(lambda: bool(calls))
    assert instance.position_timer_id is None
    assert calls == [1]
    instance.play()
    pump(lambda: len(calls) == 2)
    assert not errors
