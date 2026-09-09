"""Native GTK integration contracts, run under Xvfb and a session bus."""
import importlib.util
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="A graphical display is required")


def pump(predicate, timeout=8):
    from gi.repository import GLib
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        context = GLib.MainContext.default()
        for _ in range(200):
            if not context.pending():
                break
            context.iteration(False)
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("GTK did not reach the requested state")


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = Path(__file__).resolve().parents[1] / "big-audio-converter/usr/share/biglinux/audio-converter"
    spec = importlib.util.spec_from_file_location("bac_test_main", root / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from gi.repository import Gio
    app = module.Application()
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    app.set_application_id("br.com.biglinux.audio.converter.Test" + uuid.uuid4().hex)
    app.config.set("show_welcome_dialog", False)
    app.player._mpv_options["ao"] = "null"
    app.register(None)
    app.activate()
    win = app.get_active_window()
    assert win is not None
    win._test_errors = []
    win.file_queue.on_probe_error = lambda path, message: win._test_errors.append((path, message))
    yield win
    win.cleanup()
    win.destroy()
    app.config.close()
    app.quit()


@pytest.fixture
def audio(tmp_path):
    result = tmp_path / 'speech 東京 :: [sample] " & ;.wav'
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=880:sample_rate=44100:duration=2", str(result)], check=True, timeout=10)
    return result


def test_queue_probe_lifecycle_and_numeric_editor(window, audio):
    queue = window.file_queue
    assert queue.add_file(str(audio))
    assert queue.pending
    assert not window.convert_button.get_sensitive()
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    assert not window._test_errors
    assert len(queue.files) == 1
    assert "44100" in queue.file_rows[0].get_subtitle()
    assert window.convert_button.get_sensitive()
    window.on_edit_segments()
    editor = window.segment_editor
    editor.add_segment(.1, .11)
    editor.apply()
    assert window.visualizer.get_marker_pairs()[0]["stop"] == pytest.approx(.11)
    window.format_row.set_selected(window._format_list.index("flac"))
    window.on_convert(None)
    pump(lambda: window.conversion.last_batch is not None)
    assert window.conversion.last_batch.items[0].successful
    output = Path(window.conversion.last_batch.outputs[0])
    assert output.is_file()
    assert queue.files == [str(audio)]  # Repeating an operation keeps the input.
    queue.remove_file(0)
    assert not queue.track_metadata
    assert not queue.pending


def test_removed_pending_row_never_receives_stale_result(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    queue.remove_file(0)
    pump(lambda: not queue._probes._active)
    assert queue.files == []
    assert queue.file_rows == []
    assert window.active_audio_id is None


def test_multitrack_is_expanded_without_gtk_thread_probes(window, tmp_path):
    video = tmp_path / "tracks.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=880:duration=2", "-map", "0:a", "-map", "1:a", "-c:a", "flac", str(video)], check=True, timeout=10)
    queue = window.file_queue
    assert queue.add_file(str(video))
    pump(lambda: not queue.pending)
    assert not window._test_errors
    assert len(queue.files) == 2
    assert {value["track_index"] for value in queue.track_metadata.values()} == {0, 1}
    assert not queue.add_file(str(video))
    queue.clear_queue()
    assert queue.track_metadata == {}
    assert queue.active_file_index is None
    assert queue.currently_playing_index is None


def test_information_dialog_is_nonblocking_and_preserves_rate(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    row = queue.file_rows[0]
    started = time.monotonic()
    row._on_show_info(None, None)
    assert time.monotonic() - started < .5
    pump(lambda: not queue._probes._active)
    row.cleanup()
    assert not row._info_dialogs
