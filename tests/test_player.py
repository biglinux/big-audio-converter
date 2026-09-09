"""Native libmpv lifecycle contracts; no audio device is required."""
import subprocess
import threading
import time

import pytest
from app.audio.player import AudioPlayer
from gi.repository import GLib


def pump_until(predicate, timeout=5):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Native playback did not reach the expected state")


@pytest.fixture
def tracks(tmp_path):
    filename = tmp_path / "tracks.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=4", "-f", "lavfi", "-i",
                    "sine=frequency=880:duration=4", "-map", "0:a", "-map", "1:a", "-c:a", "flac",
                    str(filename)], check=True, timeout=10)
    return filename


@pytest.fixture
def player():
    instance = AudioPlayer(mpv_options={"ao": "null"})
    yield instance
    instance.cleanup()


def test_player_initialization_is_lazy(player):
    assert player.mpv_instance is None
    player.set_volume(2)
    player.set_playback_speed(.1)
    assert player.mpv_instance is None


def test_native_track_selection_seek_and_cleanup(player, tracks):
    errors, callback_threads = [], []
    player.connect("error", errors.append)
    player.connect("duration-changed", lambda *args: callback_threads.append(threading.get_ident()))
    identifier = f"{tracks}::track2.flac"
    assert player.load(identifier, {identifier: {"source_video": str(tracks), "track_index": 1}})
    pump_until(lambda: player._loaded or errors)
    assert not errors
    assert player.mpv_instance.aid == 2
    assert player.mpv_instance.pause
    assert callback_threads == [threading.get_ident()]
    player.set_volume(2)
    player.set_playback_speed(.1)
    pump_until(lambda: "volume=2.0" in (player._last_filter_graph or ""))
    assert player.mpv_instance.volume == 100
    assert player.mpv_instance.speed == .1
    assert player.play()
    pump_until(lambda: player.mpv_instance.time_pos is not None and player.mpv_instance.time_pos > .02)
    player.pause()
    player.seek(2)
    pump_until(lambda: abs((player.mpv_instance.time_pos or 0) - 2) < .05)
    player.cleanup()
    assert player.mpv_instance is None
    assert not player._sources._sources
    assert not any(t.name.startswith("MPVEventHandlerThread") for t in threading.enumerate())


def test_repeated_replacement_ignores_old_events(player, tracks):
    errors = []
    player.connect("error", errors.append)
    for _ in range(100):
        assert player.load(str(tracks))
    pump_until(lambda: player._loaded or errors)
    assert not errors
    assert player.duration >= 3.9
    current = player._entry_id
    player._file_ended(current - 1, 0)
    player._file_loaded(current - 1)
    assert player._loaded
    player.cleanup()
    pump_until(lambda: not player._sources._sources)


def test_missing_input_reports_error(player, tmp_path):
    errors = []
    player.connect("error", errors.append)
    assert not player.load(str(tmp_path / "missing.wav"))
    assert errors
