"""Native GTK integration contracts, run under Xvfb and a session bus."""

import importlib.util
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"), reason="A graphical display is required"
)


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
        time.sleep(0.005)
    raise AssertionError("GTK did not reach the requested state")


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = (
        Path(__file__).resolve().parents[1]
        / "big-audio-converter/usr/share/biglinux/audio-converter"
    )
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
    win.file_queue.on_probe_error = lambda path, message: win._test_errors.append(
        (path, message)
    )
    yield win
    win.cleanup()
    win.destroy()
    app.config.close()
    app.quit()


@pytest.fixture
def audio(tmp_path):
    result = tmp_path / 'speech 東京 :: [sample] " & ;.wav'
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=44100:duration=2",
            str(result),
        ],
        check=True,
        timeout=10,
    )
    return result


def test_queue_probe_lifecycle_and_numeric_editor(window, audio):
    queue = window.file_queue
    assert queue.add_file(str(audio))
    assert queue.pending
    assert not window.convert_button.get_sensitive()
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    assert not window._test_errors
    assert len(queue.files) == 1
    assert window.seekbar.duration == pytest.approx(2)
    assert "44100" in queue.file_rows[0].get_subtitle()
    assert window.convert_button.get_sensitive()
    window.on_edit_segments()
    editor = window.segment_editor
    editor.add_segment(0.1, 0.11)
    editor.apply()
    assert window.visualizer.get_marker_pairs()[0]["stop"] == pytest.approx(0.11)
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


def test_handed_over_cuts_follow_each_track_of_a_multi_track_file(window, tmp_path):
    video = tmp_path / "multi.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=2",
            "-map",
            "0",
            "-map",
            "1",
            "-c:a",
            "flac",
            str(video),
        ],
        check=True,
        timeout=10,
    )
    queue = window.file_queue
    assert queue.add_file(str(video))
    # main.py records a player's cuts while the file is still being inspected.
    window.file_markers[str(video)] = [{"start": 0.5, "stop": 1.0}]
    pump(lambda: not queue.pending)
    assert len(queue.files) == 2
    assert str(video) not in window.file_markers
    for identifier in queue.files:
        (cut,) = window.file_markers[identifier]
        assert (cut["start"], cut["stop"]) == pytest.approx((0.5, 1.0))


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
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            "-map",
            "0:a",
            "-map",
            "1:a",
            "-c:a",
            "flac",
            str(video),
        ],
        check=True,
        timeout=10,
    )
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
    assert row.more_button.get_popover() is None
    row.more_button.popup()
    assert row.more_button.get_popover() is not None
    assert row.activate_action("row.info", None)
    row.more_button.popdown()
    assert time.monotonic() - started < 0.5
    pump(lambda: not queue._probes._active)
    row.cleanup()
    assert not row._info_dialogs


def test_quality_preview_and_mark_buttons(window, audio):
    assert window.format_row.get_subtitle()
    assert window.cut_row.get_subtitle()
    assert window._bitrate_list[window.bitrate_row.get_selected()] == "192k"
    assert window.quality_row.get_selected() == 1
    window.quality_row.set_selected(2)
    assert window._bitrate_list[window.bitrate_row.get_selected()] == "320k"
    window.bitrate_row.set_selected(window._bitrate_list.index("128k"))
    assert window.quality_row.get_selected() == 3
    window.format_row.set_selected(window._format_list.index("flac"))
    assert not window.quality_row.get_visible()
    window.original_preview.set_active(True)
    assert window.player.effects_bypassed
    window.format_row.set_selected(window._format_list.index("mp3"))
    assert window.player.effects_bypassed
    window.original_preview.set_active(False)
    assert not window.player.effects_bypassed
    window.file_queue.add_file(str(audio))
    window.cut_row.set_selected(1)
    pump(lambda: window.visualizer.duration > 0 and not window.file_queue.pending)
    window.visualizer.set_position(0.2)
    window._mark_current_position(None, True)
    window.visualizer.set_position(0.8)
    window._mark_current_position(None, False)
    settings = window._collect_conversion_settings()
    assert settings["file_markers"][window.active_audio_id][0][
        "start"
    ] == pytest.approx(0.2)
    assert settings["file_markers"][window.active_audio_id][0]["stop"] == pytest.approx(
        0.8
    )


@pytest.mark.skipif(
    not shutil.which("xdotool"), reason="xdotool supplies native X11 keys"
)
def test_segment_editor_apply_is_reachable_by_keyboard(window, audio):
    import gi

    gi.require_version("GdkX11", "4.0")
    from gi.repository import GdkX11

    window.file_queue.add_file(str(audio))
    pump(lambda: window.visualizer.duration > 0 and not window.file_queue.pending)
    window.on_edit_segments()
    editor = window.segment_editor
    editor.add_segment(0.2, 0.8)
    pump(lambda: editor.get_mapped())
    surface = editor.get_native().get_surface()
    assert isinstance(surface, GdkX11.X11Surface)
    subprocess.run(
        ["xdotool", "windowfocus", "--sync", str(surface.get_xid())],
        check=True,
        timeout=5,
    )
    editor.rows[0]["stop"].grab_focus()
    root = editor.get_root()
    for _ in range(20):
        if root.get_focus() == editor.apply_button:
            break
        subprocess.run(["xdotool", "key", "Tab"], check=True, timeout=5)
        pump(lambda: True)
    assert root.get_focus() == editor.apply_button, (
        "Tab must reach Apply from the segment fields"
    )
    subprocess.run(["xdotool", "key", "Return"], check=True, timeout=5)
    pump(lambda: bool(window.file_markers.get(window.active_audio_id)))
    assert window.file_markers[window.active_audio_id][0]["stop"] == pytest.approx(0.8)


def test_noise_modes_preview_and_missing_plugin_recovery(window, audio):
    from app.audio.profiles import discover_noise_plugins

    plugins = discover_noise_plugins()
    if not all(name in plugins for name in ("dfn3", "dpdfnet")):
        pytest.skip(
            "Both optional denoisers are needed for this native preview journey"
        )
    player = window.player
    errors = []
    player.error_callback = errors.append
    assert player.load(str(audio))
    pump(lambda: player._loaded)
    for index, engine in enumerate(window.noise_engines):
        window.noise_model_row.set_selected(index)
        window.noise_switch.set_active(True)
        window.noise_strength_scale.set_value(25)
        pump(
            lambda engine=engine: (
                engine == player.noise_engine
                and "c0=25" in (player._last_filter_graph or "")
            )
        )
        assert player.noise_reduction
        settings = window._collect_conversion_settings()
        assert settings["noise_engine"] == engine
        assert settings["noise_attenuation_db"] == 25
        assert player.play()
        player.seek(0.7)
        pump(
            lambda: (
                player.mpv_instance.time_pos is not None
                and player.mpv_instance.time_pos >= 0.7
            )
        )
        player.pause()
        assert not errors
    # A missing heavy plugin must not trap the user behind a disabled chooser.
    del player.noise_plugins["dpdfnet"]
    window._update_noise_availability()
    assert not window.noise_switch.get_active()
    assert not player.noise_reduction
    assert not window.noise_switch.get_sensitive()
    assert "dpdfnet-native" in window.noise_expander.get_subtitle()
    window.noise_model_row.set_selected(0)
    assert window.noise_switch.get_sensitive()
    window.noise_switch.set_active(True)
    assert player.noise_reduction and player.noise_engine == "dfn3"
