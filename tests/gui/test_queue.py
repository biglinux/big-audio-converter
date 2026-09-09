"""Real GTK queue tests with delayed probes and concurrent row mutations."""

import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="An X11 graphical session is required")
from test_window import pump


@pytest.fixture
def media_queue(tmp_path):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    from app.ui.file_queue import FileQueue
    from app.audio.converter import AudioConverter
    from app.audio.media_probe import clear_probe_cache
    Adw.init()
    clear_probe_cache()
    converter = AudioConverter()
    queue = FileQueue(converter)
    window = Gtk.Window(default_width=700, default_height=450)
    window.set_child(queue)
    window.present()
    try:
        yield queue
    finally:
        queue.cleanup()
        window.close()
        converter.cleanup()
        pump(seconds=0.1)


def audio(path, frequency=997):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=1:sample_rate=44100", str(path)], check=True, timeout=10)
    return path


def test_add_reorder_remove_during_slow_metadata(media_queue, tmp_path, monkeypatch):
    import app.audio.media_tasks as tasks
    from gi.repository import GLib
    original = tasks.probe_media
    threads = []
    def slow_probe(*args):
        threads.append(threading.current_thread())
        time.sleep(0.15)
        return original(*args)
    monkeypatch.setattr(tasks, "probe_media", slow_probe)
    paths = [audio(tmp_path / f"item{index}.wav", 400 + index * 100) for index in range(3)]
    beats = []
    source = GLib.timeout_add(10, lambda: beats.append(time.monotonic()) or True)
    try:
        for path in paths:
            assert media_queue.add_file(str(path))
        assert not media_queue.file_rows[0].metadata_ready
        assert media_queue.move_file(2, 0)
        assert media_queue.remove_file(2)
        assert pump(lambda: all(row.metadata_ready for row in media_queue.file_rows), seconds=5)
        assert media_queue.get_files() == [str(paths[2]), str(paths[0])]
        assert all("44100 Hz" in row.get_subtitle() for row in media_queue.file_rows)
        assert len(beats) >= 10
        assert all(thread is not threading.main_thread() for thread in threads)
        assert len(set(threads)) == 1
    finally:
        GLib.source_remove(source)


def test_video_audio_tracks_expand_at_the_original_position(media_queue, tmp_path):
    before = audio(tmp_path / "before.wav")
    after = audio(tmp_path / "after.wav")
    video = tmp_path / "video.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=16x16:d=1",
        "-f", "lavfi", "-i", "sine=frequency=400:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=800:duration=1",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1", "-c:a", "flac", str(video)], check=True, timeout=10)
    for path in (before, video, after):
        assert media_queue.add_file(str(path))
    assert pump(lambda: len(media_queue.file_rows) == 4 and all(row.metadata_ready for row in media_queue.file_rows), seconds=5)
    assert media_queue.files[0] == str(before)
    assert media_queue.files[-1] == str(after)
    assert [row.media_source.stream_index for row in media_queue.file_rows[1:3]] == [1, 2]
    assert all(not row._delete_action.get_enabled() for row in media_queue.file_rows[1:3])
    media_queue.set_active_file(2)
    media_queue.set_currently_playing(2)
    media_queue.move_file(2, 0)
    assert media_queue.active_file_index == 0
    assert media_queue.currently_playing_index == 0


def test_clear_queue_releases_pending_callbacks(media_queue, tmp_path, monkeypatch):
    import app.audio.media_tasks as tasks
    original = tasks.probe_media
    def slow(*args):
        time.sleep(0.1)
        return original(*args)
    monkeypatch.setattr(tasks, "probe_media", slow)
    path = audio(tmp_path / "clear.wav")
    media_queue.add_file(str(path))
    media_queue.clear_queue()
    pump(seconds=0.3)
    assert not media_queue.files
    assert not media_queue._rows_by_id
    assert not media_queue._media_tasks._callbacks
    assert not media_queue.track_metadata
