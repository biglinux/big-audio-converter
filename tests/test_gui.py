"""Native GTK integration contracts, run under Xvfb and a session bus."""

import importlib.util
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from conftest import pump

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="A graphical display is required",
)


def start_window():
    """A running application window, configured under $XDG_CONFIG_HOME."""
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
    app.player._mpv_options["ao"] = "null"
    app.register(None)
    app.activate()
    win = app.get_active_window()
    assert win is not None
    win._test_errors = []
    win.file_queue.on_probe_error = lambda path, message: win._test_errors.append(
        (path, message)
    )
    return win


def stop_window(win):
    app = win.get_application()
    win.cleanup()
    win.destroy()
    app.config.close()
    app.quit()


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    win = start_window()
    yield win
    stop_window(win)


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
    assert queue.file_rows[0].get_subtitle() == "0:02 · WAV · 44.1 kHz"
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


def test_join_offered_for_several_files_and_not_in_fast_copy(window, audio, tmp_path):
    queue = window.file_queue
    second = tmp_path / "second.wav"
    second.write_bytes(audio.read_bytes())
    assert queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    assert not window.join_row.get_sensitive()
    assert queue.add_file(str(second))
    pump(lambda: not queue.pending)
    assert window.join_row.get_sensitive()
    window.join_row.set_active(True)
    assert window.convert_button.get_label() == "Join 2 files"
    assert window._collect_conversion_settings()["join_files"]
    window.format_row.set_selected(window._format_list.index("copy"))
    assert not window.join_row.get_sensitive()
    assert window.convert_button.get_label() == "Convert 2 files"
    assert not window._collect_conversion_settings()["join_files"]
    window.format_row.set_selected(window._format_list.index("flac"))
    window.on_convert(None)
    pump(lambda: window.conversion.last_batch is not None)
    [output] = window.conversion.last_batch.outputs
    assert output.endswith("-joined.flac")


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


def test_bitrate_preview_and_waveform_marks(window, audio):
    assert window.format_row.get_selected_item().get_string()
    assert window._bitrate_list[window.bitrate_row.get_selected()] == "192k"
    window.bitrate_row.set_selected(window._bitrate_list.index("128k"))
    assert window.app.config.get("conversion_bitrate") == "128k"
    window.format_row.set_selected(window._format_list.index("flac"))
    assert not window.bitrate_row.get_sensitive()
    window.format_row.set_selected(window._format_list.index("copy"))
    assert not window.volume_spin.is_sensitive()
    assert not window.compressor_tile.is_sensitive()
    assert not window.voice_scale.get_sensitive()
    assert window.format_row.get_sensitive()
    window.format_row.set_selected(window._format_list.index("flac"))
    assert window.volume_spin.is_sensitive()
    # Back on when the noise plugin is installed, which CI machines lack.
    assert window.voice_scale.get_sensitive() == window.noise_available
    window.compressor_tile.steps.set_selected(1)
    window.original_preview.set_active_name("original")
    assert window.player.effects_bypassed
    window.format_row.set_selected(window._format_list.index("mp3"))
    assert window.player.effects_bypassed
    window.original_preview.set_active_name("adjusted")
    assert not window.player.effects_bypassed
    window.compressor_tile.steps.set_selected(0)
    window.file_queue.add_file(str(audio))
    vis = window.visualizer
    pump(lambda: vis.waveform_data is not None and vis.get_width() > 0)
    assert window.segment_edit_button.get_visible()
    assert not window.cut_order_row.get_sensitive()
    width, duration = vis.get_width(), vis.duration

    # A press that does not move is a click: listening moves there.
    vis.on_click_handler(None, 1, width * 0.5, 20)
    vis.on_drag_end(None, 0, 0)
    assert vis.position == pytest.approx(duration * 0.5, abs=0.01)
    assert not vis.marker_pairs

    # A drag marks a segment, here from right to left.
    class Drag:
        def get_start_point(self):
            return True, width * 0.8, 20

    vis.on_click_handler(None, 1, width * 0.8, 20)
    vis.on_drag_update(Drag(), -width * 0.6, 0)
    vis.on_release_handler(None, 1, width * 0.2, 20)
    vis.on_drag_end(None, -width * 0.6, 0)
    assert window.cut_order_row.get_sensitive()
    assert window.play_selection_switch.get_visible()
    settings = window._collect_conversion_settings()
    [segment] = settings["file_markers"][window.active_audio_id]
    assert segment["start"] == pytest.approx(duration * 0.2, abs=0.01)
    assert segment["stop"] == pytest.approx(duration * 0.8, abs=0.01)

    # Removing the marked part exports and previews what lies around it.
    window.cut_mode_row.set_selected(1)
    assert vis.remove_marked
    assert not window.cut_order_row.get_sensitive()
    assert not window.cut_output_row.get_sensitive()
    assert window._collect_conversion_settings()["cut_remove"]
    before, after = window._load_selection_segments()
    assert before == (0, pytest.approx(duration * 0.2, abs=0.01))
    assert after == (pytest.approx(duration * 0.8, abs=0.01), duration)


@pytest.mark.skipif(
    not shutil.which("xdotool") or os.environ.get("GDK_BACKEND") != "x11",
    reason="xdotool supplies native keys to X11 windows",
)
# GTK deprecated its whole X11 API without a replacement; xdotool needs the XID.
@pytest.mark.filterwarnings("ignore:GdkX11.X11Surface.get_xid:DeprecationWarning")
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
        window.noise_model.set_selected(index)
        window.noise_adjustment.set_value(25)
        # 25 % of the model's useful cap: 6 dB for DFN3, 12 dB for DPDFNet-2.
        attenuation = {"dfn3": "c0=6.00", "dpdfnet": "c0=12.00"}[engine]
        pump(
            lambda engine=engine, attenuation=attenuation: (
                engine == player.noise_engine
                and attenuation in (player._last_filter_graph or "")
            )
        )
        assert player.noise_reduction
        settings = window._collect_conversion_settings()
        assert settings["noise_engine"] == engine
        assert settings["noise_strength"] == 25
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
    assert not player.noise_reduction
    assert not window.voice_scale.get_sensitive()
    assert "dpdfnet-native" in window.noise_status.get_label()
    assert window.noise_status.get_visible()
    assert window.noise_model.get_sensitive()
    window.noise_model.set_selected(0)
    assert window.voice_scale.get_sensitive()
    assert player.noise_reduction and player.noise_engine == "dfn3"
    assert window.voice_value.get_label() == "25"


def test_reset_settings_restores_first_run_values(window, tmp_path):
    config = window.app.config
    window.format_row.set_selected(window._format_list.index("opus"))
    window.bitrate_row.set_selected(window._bitrate_list.index("128k"))
    window.channels_row.set_selected(1)
    window._set_output_folder(str(tmp_path))
    window.cut_order_row.set_selected(1)
    window.volume_spin.set_value(250)
    window.speed_spin.set_value(1.5)
    window.compressor_tile.steps.set_selected(2)
    window.normalize_tile.set_active(True)
    window.eq_panel.band_scales[window.eq_panel.BANDS[0][1]].set_value(6)
    window.auto_advance_switch.set_active(False)

    assert window.equalizer_button.has_css_class("applied")
    window._on_reset_settings_response(None, "cancel")
    assert window.compressor_tile.steps.get_selected() == 2

    window._on_reset_settings_response(None, "reset")
    assert window._format_list[window.format_row.get_selected()] == "mp3"
    assert window._bitrate_list[window.bitrate_row.get_selected()] == "192k"
    assert window.bitrate_row.get_sensitive()
    assert window.channels_row.get_selected() == 0
    assert window.output_directory == ""
    assert window.cut_order_row.get_selected() == 0
    assert window.volume_spin.get_value() == 100
    assert window.speed_spin.get_value() == 1.0
    assert window.compressor_tile.steps.get_selected() == 0
    assert not window.normalize_tile.get_active()
    assert all(s.get_value() == 0 for s in window.eq_panel.band_scales.values())
    assert window.auto_advance_switch.get_active()
    assert not window.player.normalize
    assert not window.equalizer_button.has_css_class("applied")
    for key, value in (
        ("conversion_format", "mp3"),
        ("audio_channels", "0"),
        ("output_directory", ""),
        ("cut_audio_mode", "0"),
        ("compressor_enabled", "false"),
        ("normalize_enabled", "false"),
        ("auto_advance_enabled", True),
    ):
        assert config.get(key) == value, key
    # A first-run value that was never changed is simply absent.
    assert config.get("conversion_bitrate", "192k") == "192k"
    assert float(config.get("conversion_volume")) == 100


def test_information_size_is_the_file_unless_other_tracks_share_it(audio):
    from app.ui.file_info import audio_properties, format_size, shares_file

    track = {"index": 0, "codec_type": "audio", "duration": "2.0"}
    cover = {"index": 1, "codec_type": "video", "disposition": {"attached_pic": 1}}
    video = {"index": 1, "codec_type": "video", "disposition": {"attached_pic": 0}}
    assert not shares_file({"streams": [track]}, track)
    assert not shares_file({"streams": [track, cover]}, track)
    assert shares_file({"streams": [track, video]}, track)

    def size(stream, shares):
        info = {"streams": [stream], "format": {}}
        return dict(audio_properties(stream, info, shares, str(audio)))["Size"]

    assert size(track, False) == format_size(audio.stat().st_size)
    assert size(track, True) == "Unknown"
    assert size({**track, "bit_rate": "128000"}, True) == format_size(32000)


def test_panning_the_waveform_moves_the_seek_bar_window(window):
    window.visualizer.zoom_level = 4.0
    window.visualizer.viewport_offset = 0.5
    assert window.seekbar._viewport_offset == 0.5
    window.visualizer.viewport_offset = 0.25
    assert window.seekbar._viewport_offset == 0.25


def test_the_compressor_is_set_in_named_steps_and_off_at_the_first(window):
    config = window.app.config
    window.compressor_tile.steps.set_selected(2)
    settings = window._collect_conversion_settings()
    assert settings["compressor_enabled"] and settings["compressor_intensity"] == 0.65
    assert window.player.compressor_enabled
    assert window.compressor_tile.value_label.get_text() == "Medium"
    assert config.get("compressor_intensity") == "0.65"

    window.compressor_tile.steps.set_selected(0)
    assert not window._collect_conversion_settings()["compressor_enabled"]
    assert window.compressor_tile.value_label.get_text() == "Off"
    assert not window.player.compressor_enabled
    # Off keeps the strength for the next session's restore of a later choice.
    assert config.get("compressor_enabled") == "false"
    assert config.get("compressor_intensity") == "0.65"

    # A strength saved by the former slider comes back as the nearest step.
    config.set("compressor_enabled", "true")
    config.set("compressor_intensity", "0.75")
    window._restore_conversion_settings()
    assert window.compressor_tile.steps.get_selected() == 2
    config.set("compressor_enabled", "false")
    window._restore_conversion_settings()
    assert window.compressor_tile.steps.get_selected() == 0


def marks(win):
    return [(m["start"], m["stop"]) for m in win.visualizer.get_marker_pairs()]


def two_files(window, audio, tmp_path):
    second = tmp_path / "second.wav"
    second.write_bytes(audio.read_bytes())
    queue = window.file_queue
    queue.add_files([str(audio), str(second)])
    pump(lambda: not queue.pending and len(queue.files) == 2)
    return queue.files


def test_auto_advance_keeps_each_files_marks(window, audio, tmp_path):
    first, second = two_files(window, audio, tmp_path)
    window.on_activate_file(first, 0)
    pump(lambda: window.active_audio_id == first and window.visualizer.duration > 0)
    window.visualizer.restore_markers([{"start": 0.2, "stop": 0.5}])
    window.file_markers[second] = [{"start": 1.0, "stop": 1.5}]

    window._play_next_file(second, 1)
    pump(lambda: window.visualizer.duration > 0 and marks(window))

    assert window.active_audio_id == second
    assert marks(window) == [(1.0, 1.5)]
    assert [(m["start"], m["stop"]) for m in window.file_markers[first]] == [(0.2, 0.5)]


def test_removed_and_cleared_files_forget_their_cuts(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    window.visualizer.restore_markers([{"start": 0.2, "stop": 0.5}])
    window._save_current_file_state()

    queue.remove_file(0)
    assert window.file_markers == {}
    assert not window.cut_mode_row.get_sensitive()

    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    assert marks(window) == []

    window.visualizer.restore_markers([{"start": 0.2, "stop": 0.5}])
    window._save_current_file_state()
    window._on_clear_queue_response(None, "clear")
    assert window.file_markers == {}


def test_play_does_not_resume_another_file_when_loading_fails(
    window, audio, monkeypatch
):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    played = []
    window.player.current_file = None
    monkeypatch.setattr(window.player, "load", lambda *args: False)
    monkeypatch.setattr(window.player, "play", lambda: played.append(True))
    window._on_pause_play_clicked(None)
    assert played == []


def test_command_line_files_resolve_in_the_callers_directory(
    window, audio, tmp_path, monkeypatch
):
    from gi.repository import Gio, GLib

    app = window.get_application()
    shutil.copyfile(audio, tmp_path / "relative.wav")
    line = Gio.ApplicationCommandLine(
        arguments=GLib.Variant(
            "aay",
            [b"big-audio-converter\0", b"relative.wav\0", b"bac-test://host/a.wav\0"],
        ),
        platform_data=GLib.Variant(
            "a{sv}", {"cwd": GLib.Variant("ay", str(tmp_path).encode() + b"\0")}
        ),
    )
    opened = []
    monkeypatch.setattr(app, "do_open", lambda files, count, hint: opened.extend(files))
    assert app.do_command_line(line) == 0
    assert [f.get_path() for f in opened] == [str(tmp_path / "relative.wav"), None]


def test_files_dropped_on_the_window_are_all_added(window, audio, tmp_path):
    from gi.repository import Gdk, Gio, Gtk

    second = tmp_path / "second.wav"
    second.write_bytes(audio.read_bytes())
    files = Gdk.FileList.new_from_list(
        [
            Gio.File.new_for_path(str(audio)),
            Gio.File.new_for_path(str(second)),
            Gio.File.new_for_uri("bac-test://host/remote.wav"),
        ]
    )
    target = next(
        c for c in window.observe_controllers() if isinstance(c, Gtk.DropTarget)
    )
    assert target.emit("drop", files, 0, 0)
    pump(lambda: len(window.file_queue.files) == 2 and not window.file_queue.pending)
    assert [path for path, _message in window._test_errors] == [
        "bac-test://host/remote.wav"
    ]


def test_a_failed_trash_is_not_reported_as_a_failed_add(window, audio, monkeypatch):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    shown = []
    monkeypatch.setattr(window, "_show_message", lambda *args: shown.append(args))
    audio.unlink()  # Trash then fails: the file is already gone.
    queue._on_delete_response(None, "trash", str(audio))
    pump(lambda: shown)
    assert shown[0][0] == "Could not move the file to Trash"
    assert window._test_errors == []


def test_closing_during_a_conversion_asks_first(window, monkeypatch):
    monkeypatch.setattr(type(window.converter), "busy", property(lambda self: True))
    assert window.on_close_request(window) is True
    assert not window._closed
    window._on_close_while_converting(None, "keep")
    assert not window._closed
    window._on_close_while_converting(None, "stop")
    assert window._closed


def test_quitting_during_a_conversion_asks_first(window, monkeypatch):
    from gi.repository import Adw

    app = window.get_application()
    quits, asked = [], []
    monkeypatch.setattr(type(window.converter), "busy", property(lambda self: True))
    monkeypatch.setattr(app, "quit", lambda: quits.append(True))
    monkeypatch.setattr(
        Adw.AlertDialog,
        "present",
        lambda self, _parent: asked.append(self.get_heading()),
    )
    app.on_quit_action()
    assert asked == ["Stop the conversion?"]
    assert not quits and not window._closed


def test_custom_equalizer_is_named_and_kept_between_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    win = start_window()
    try:
        panel = win.eq_panel
        panel.preset_dropdown.set_selected(panel.PRESET_KEYS.index("podcast"))
        panel.preset_dropdown.set_selected(panel.PRESET_KEYS.index("custom"))
        assert panel.preset_dropdown.get_selected_item().get_string() == "Custom"
        panel.band_scales[1000].set_value(7)
    finally:
        stop_window(win)
    win = start_window()
    try:
        assert win.eq_panel.band_scales[1000].get_value() == 7
        assert win.eq_panel.band_scales[31].get_value() == 5  # from Radio Host
        assert win.eq_panel.preset_dropdown.get_selected_item().get_string() == "Custom"
        assert win.equalizer_button.has_css_class("applied")
    finally:
        stop_window(win)


def test_unreadable_settings_are_announced_once(tmp_path, monkeypatch):
    from gi.repository import Adw

    config = tmp_path / "config" / "audio-converter"
    config.mkdir(parents=True)
    (config / "config.json").write_text("{not json")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    toasts = []
    monkeypatch.setattr(
        Adw.ToastOverlay, "add_toast", lambda self, toast: toasts.append(toast)
    )
    win = start_window()
    try:
        pump(lambda: toasts)
        assert toasts[0].get_title() == "Settings could not be read and were reset"
    finally:
        stop_window(win)


def test_found_pauses_mark_the_speech_at_once_and_undo_restores_it(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    window.visualizer.restore_markers([{"start": 0.1, "stop": 0.3}])
    panel = window.pause_panel
    levels = [-6.0] * 50 + [-80.0] * 100 + [-6.0] * 50  # a 1 s pause
    panel.length_buttons[0].set_active(True)
    panel._on_found(None, levels)
    # What is marked is exported: the speech, kept in order and joined.
    assert marks(window) == [(0.0, 0.65), (1.35, 2.0)]
    assert not window.removes_marked()
    assert window.cut_order_row.get_selected() == 0
    assert window.cut_output_row.get_selected() == 1
    # One row says it all: what was marked, from what, and that it undoes.
    assert not panel.status_row.get_visible()
    assert panel.undo_row.get_visible()
    assert panel.undo_row.get_title() == "Speech marked in 2 parts"
    assert panel.undo_row.get_subtitle().split("\n") == [
        "1 pause · 0:00 shorter",
        "This replaces the 1 mark already on the file.",
        "Nothing is deleted from the original file.",
    ]
    assert not panel.actions.get_visible()
    # Another length replaces the panel's marks, never the person's.
    panel.length_buttons[2].set_active(True)  # 2 s: no pause that long
    assert marks(window) == [(0.1, 0.3)]
    assert not panel.undo_row.get_visible()
    panel.length_buttons[0].set_active(True)
    assert marks(window) == [(0.0, 0.65), (1.35, 2.0)]
    panel.undo_apply()
    assert marks(window) == [(0.1, 0.3)]
    assert window.cut_output_row.get_selected() == 0
    assert not panel.undo_row.get_visible()


def test_marks_edited_after_finding_stay_and_finding_again_is_offered(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    panel = window.pause_panel
    panel.length_buttons[0].set_active(True)
    panel._on_found(None, [-6.0] * 50 + [-80.0] * 100 + [-6.0] * 50)
    window.visualizer.restore_markers([{"start": 0.0, "stop": 0.5}])
    assert marks(window) == [(0.0, 0.5)]
    assert not panel.undo_row.get_visible()
    assert panel.actions.get_visible()
    assert panel.actions.get_visible_child_name() == "find"


def test_pauses_past_the_end_of_the_file_are_reported(window, audio, monkeypatch):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    shown = []
    monkeypatch.setattr(window, "_show_message", lambda *args: shown.append(args))
    panel = window.pause_panel
    panel._on_found(None, [-6.0] * 300 + [-80.0] * 100)  # 4 s measured, 2 s shown
    assert shown and shown[0][0] == "The pauses could not be marked"
    assert marks(window) == []


def test_installing_the_denoiser_is_noticed_without_a_restart(
    window, audio, monkeypatch
):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    panel = window.pause_panel
    monkeypatch.setattr("shutil.which", lambda _name: None)
    panel._show_state()
    assert "dpdfnet-native" in panel.status_row.get_title()
    assert not panel.actions.get_visible()

    # Showing a file again looks again.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    panel.sync()
    assert panel.actions.get_visible_child_name() == "find"


def test_marked_parts_are_kept_in_a_new_session(tmp_path, monkeypatch):
    """Marks do not outlive the session, so neither does removing them: a
    saved mode would silently turn the next session's marks into cuts."""
    config = tmp_path / "config" / "audio-converter"
    config.mkdir(parents=True)
    (config / "config.json").write_text('{"cut_remove": true}')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    win = start_window()
    try:
        assert not win.removes_marked() and not win.visualizer.remove_marked
        win.set_removes_marked(True)
        assert win.visualizer.remove_marked
    finally:
        stop_window(win)
    win = start_window()
    try:
        assert not win.removes_marked()
    finally:
        stop_window(win)


def test_saved_volume_and_speed_come_back_in_a_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    win = start_window()
    try:
        win.volume_spin.set_value(250)
        win.speed_spin.set_value(1.5)
    finally:
        stop_window(win)
    win = start_window()
    try:
        assert win.volume_spin.get_value() == 250
        assert win.speed_spin.get_value() == 1.5
        assert win.volume_value.get_label() == "250"
    finally:
        stop_window(win)


def test_a_tile_card_opens_for_the_keyboard_and_shows_the_amount(window):
    tile = window.compressor_tile
    window.present()
    pump(lambda: tile.get_mapped())
    tile.activate()
    assert tile.popover.get_visible() and tile.popover.get_autohide()
    pump(lambda: tile.steps.buttons[0].has_focus())
    tile.steps.set_selected(3)
    assert tile.value_label.get_text() == "Strong"
    assert tile.card_value.get_text() == "Strong"
    [count] = [c for tiles, c in window.effect_cards.groups if tile in tiles]
    assert count.get_text() == "1 active"
    # Off is the default of a correction: there is nothing to reset to.
    assert tile.reset_button is None
    tile.popover.popdown()
    assert not tile.popover.get_autohide()


def test_a_card_opens_beside_the_sidebar_clear_of_the_other_tiles(window):
    from types import SimpleNamespace

    from gi.repository import Graphene

    popovers = window.effect_cards.popovers
    left, beside = window.normalize_tile, window.compressor_tile
    root = window
    window.present()
    pump(lambda: left.get_mapped())
    popovers.open(left)
    pump(lambda: left.popover.get_mapped() and left.popover.get_width())
    # The card starts past the sidebar's edge, over the queue.
    scrolled = window.effect_cards.scrolled
    _found, edge = scrolled.compute_point(
        root, Graphene.Point().init(scrolled.get_width(), 0)
    )
    surface = left.popover.get_surface()
    offset_x, _offset_y = root.get_surface_transform()
    shadow_x, _shadow_y = left.popover.get_surface_transform()
    _found, card = left.popover.get_child().compute_bounds(left.popover)
    card_left = surface.get_position_x() - offset_x + shadow_x + card.get_x()
    assert card_left >= edge.x

    def pointer_on(tile, x, y):
        controller = SimpleNamespace(get_widget=lambda: tile)
        popovers._on_anchor_pointer(controller, x, y, tile)

    # Across the neighbour on the way to the card: kept.
    pointer_on(beside, beside.get_width() - 4, beside.get_height() / 2)
    assert left.popover.get_visible() and not beside.popover.get_visible()
    # Resting on the neighbour: that one takes over.
    pump(lambda: beside.popover.get_visible())
    assert not left.popover.get_visible()
    beside.popover.popdown()


def test_the_bar_equalizer_button_shows_and_hides_its_panel(window):
    button = window.equalizer_button
    button.set_active(True)
    assert window.eq_revealer.get_reveal_child()
    # The panel's own close button releases the toggle too.
    window.eq_panel._on_close(None)
    assert not button.get_active()
    # A shaped curve is an adjustment like any other.
    window.eq_panel.band_scales[1000].set_value(4)
    assert window.original_preview.get_visible()
    assert window.adjustments_label.get_label() == "1 adjustment"


def test_a_switch_tile_flips_on_activation_without_a_card(window):
    tile = window.normalize_tile
    tile.activate()
    assert tile.get_active() and window.player.normalize
    assert tile.value_label.get_text() == "On"
    assert not tile.popover.get_visible()
    tile.activate()
    assert not tile.get_active()


def test_copy_mode_closes_the_card_and_disables_the_tiles(window):
    window.present()
    pump(lambda: window.compressor_tile.get_mapped())
    window.compressor_tile.activate()
    assert window.compressor_tile.popover.get_visible()
    window.format_row.set_selected(window._format_list.index("copy"))
    assert not window.compressor_tile.popover.get_visible()
    assert not window.voice_scale.get_sensitive()
    assert not window.compressor_tile.is_sensitive()
    # The tile says why, where its value was.
    reason = "The original format keeps the audio as it is."
    assert window.compressor_tile.value_label.get_text() == reason
    window.format_row.set_selected(window._format_list.index("mp3"))
    assert window.compressor_tile.value_label.get_text() == "Off"


def test_pause_progress_shows_on_the_tile(window, audio, monkeypatch):
    from gi.repository import Gtk

    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    panel = window.pause_panel
    # The panel is the card of a tile in the Cuts section.
    assert panel.get_ancestor(Gtk.Popover) is window.pause_tile.popover
    assert window.pause_tile.value_label.get_text() == "Off"
    running = [True]  # an analysis in progress
    monkeypatch.setattr(type(panel.finder), "busy", property(lambda _f: running[0]))
    panel._show_state("busy")
    panel._on_progress(0.4)
    assert panel.progress_row.get_visible()
    assert panel.status_row.get_title() == "Finding pauses: 40%"
    # The tile follows it with the card closed.
    assert window.pause_tile.value_label.get_text() == "40%"
    assert panel.actions.get_visible_child_name() == "cancel"
    running[0] = False
    panel._on_cancel()
    assert not panel.progress_row.get_visible()
    assert window.pause_tile.value_label.get_text() == "Off"


def test_the_original_preview_shows_only_while_an_adjustment_is_on(window):
    preview = window.original_preview
    assert not preview.get_visible()
    window.compressor_tile.steps.set_selected(1)
    assert preview.get_visible()
    preview.set_active_name("original")
    assert window.player.effects_bypassed
    # Hiding it must never leave the preview silently bypassed.
    window.compressor_tile.steps.set_selected(0)
    assert not preview.get_visible()
    assert preview.get_active_name() == "adjusted"
    assert not window.player.effects_bypassed


def test_the_bar_shows_the_time_and_zooms_with_buttons(window, audio):
    window.file_queue.add_file(str(audio))
    pump(lambda: window.visualizer.duration > 0)
    assert (window.elapsed_label.get_label(), window.total_label.get_label()) == (
        "0:00",
        "0:02",
    )
    window.seekbar.set_position(1.4)
    assert window.elapsed_label.get_label() == "0:01"
    assert not window.zoom_out_button.get_sensitive()
    window.zoom_out_button.get_parent().get_last_child().emit("clicked")
    assert window.visualizer.zoom_level > 1
    assert window.zoom_out_button.get_sensitive()
    window.zoom_reset_button.emit("clicked")
    assert window.visualizer.zoom_level == 1
    assert window.zoom_reset_button.get_label() == "1×"


def test_an_empty_queue_offers_adding_files_and_no_player(window, audio):
    queue = window.file_queue
    assert queue.stack.get_visible_child_name() == "empty"
    assert not window.visualizer_container.get_visible()
    assert not window.lookup_action("clear-queue").get_enabled()
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    assert queue.stack.get_visible_child_name() == "files"
    assert window.visualizer_container.get_visible()
    assert window.queue_label.get_label() == "1 file · 0:02"
    # Cuts are work: clearing them asks first.
    window.visualizer.restore_markers([{"start": 0.2, "stop": 0.5}])
    window.activate_action("win.clear-queue", None)
    assert queue.files == [str(audio)]
    window.visualizer.clear_all_markers()
    window.activate_action("win.clear-queue", None)
    assert queue.files == []
    assert queue.stack.get_visible_child_name() == "empty"
    assert not window.visualizer_container.get_visible()


def test_delete_removes_the_focused_entry_and_keeps_the_file(window, audio, tmp_path):
    queue = window.file_queue
    second = tmp_path / "second.wav"
    second.write_bytes(audio.read_bytes())
    queue.add_files([str(audio), str(second)])
    pump(lambda: len(queue.files) == 2 and not queue.pending)
    pump(lambda: window.visualizer.duration > 0)
    window.visualizer.restore_markers([{"start": 0.2, "stop": 0.5}])
    window._update_cut_rows()
    assert queue.file_rows[0].get_subtitle().endswith(" · 1 part")
    queue.file_rows[0].grab_focus()
    assert queue._remove_focused_row()
    assert queue.files == [str(second)]
    assert audio.is_file()
    assert queue.file_list.get_focus_child() is queue.file_rows[0]


def test_converting_locks_the_queue_and_reports_in_a_banner(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    window.format_row.set_selected(window._format_list.index("flac"))
    window.activate_action("win.convert", None)
    assert window.converter.busy
    assert window.convert_button.get_label() == "Cancel"
    assert not window.options_scroll.get_sensitive()
    assert not window.lookup_action("add-files").get_enabled()
    assert not queue.file_rows[0].remove_button.get_sensitive()
    assert window.queue_label.get_label().startswith("Converting 1 of 1")
    pump(lambda: window.conversion.last_batch is not None)
    assert window.convert_button.get_label() == "Convert 1 file"
    assert window.options_scroll.get_sensitive()
    assert queue.file_rows[0].remove_button.get_sensitive()
    assert window.banner.get_revealed()
    assert window.banner.get_title() == "1 file converted"
    assert queue.file_rows[0].result_box.get_first_child() is not None
    assert window.queue_label.get_label() == "1 file · 0:02"


def test_cancelling_leaves_no_partial_output_and_offers_retry(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    window.format_row.set_selected(window._format_list.index("flac"))
    before = set(audio.parent.iterdir())
    window.activate_action("win.convert", None)
    window.activate_action("win.cancel-conversion", None)
    pump(lambda: window.conversion.last_batch is not None)
    assert window.conversion.last_batch.items[0].status == "cancelled"
    assert set(audio.parent.iterdir()) == before
    assert window.banner.get_title() == "1 file was not converted"
    assert window.banner.get_button_label() == "Try Again"


def test_marked_speech_exports_as_one_file_without_the_pauses(window, audio):
    import json
    import subprocess

    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    panel = window.pause_panel
    panel.length_buttons[0].set_active(True)
    panel._on_found(None, [-6.0] * 50 + [-80.0] * 100 + [-6.0] * 50)
    window.format_row.set_selected(window._format_list.index("flac"))
    window.on_convert(None)
    pump(lambda: window.conversion.last_batch is not None)
    [item] = window.conversion.last_batch.items
    assert item.successful and len(item.outputs) == 1
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-of", "json", item.outputs[0]],
        capture_output=True,
        check=True,
        text=True,
    )
    # 0.65 s + 0.65 s of speech; the 0.7 s pause is gone.
    assert float(json.loads(probe.stdout)["format"]["duration"]) == pytest.approx(
        1.3, abs=0.05
    )


def test_volume_speed_and_voice_sit_in_the_playback_bar(window):
    from gi.repository import Gtk

    preview = window.original_preview
    window.volume_spin.set_value(150)
    assert window.volume_value.get_label() == "150"
    assert window.volume_button.get_icon_name() == "audio-volume-high-symbolic"
    # A bar adjustment counts like a tile: the original can be heard.
    assert preview.get_visible()
    assert window.adjustments_label.get_label() == "1 adjustment"
    window.speed_spin.set_value(1.25)
    assert window.speed_value.get_label() == "1.25x"
    assert window.adjustments_label.get_label() == "2 adjustments"
    window.volume_spin.set_value(100)
    window.speed_spin.set_value(1.0)
    assert not preview.get_visible()
    # The voice and its model are in the bar's card, not in the sidebar.
    assert window.voice_value.get_label() == "0"
    assert window.noise_model.get_ancestor(Gtk.Popover) is not None


def test_bar_controls_keep_their_width_whatever_their_cards_hold(window, audio):
    import time

    from gi.repository import GLib

    window.file_queue.add_file(str(audio))
    pump(lambda: not window.file_queue.pending)
    window.present()
    voice = window.voice_button.get_parent()
    pump(lambda: voice.get_mapped() and voice.get_width())
    closed = voice.get_width()
    # An open card's expanding choices must not widen the control it points
    # at, which would aim the card's arrow past the button.
    window.bar_popovers.open("voice")
    card = window.bar_popovers.popovers["voice"]
    pump(lambda: card.get_mapped())
    # The next layouts would widen it; give them a few frames.
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        context.iteration(False)
    assert voice.get_width() == closed
    card.popdown()


def test_moving_along_the_bar_hands_the_card_to_the_next_control(window, audio):
    from types import SimpleNamespace

    from gi.repository import Gdk, Gtk

    # Themes such as the BigLinux one give cards a wide shadow.
    shadow = Gtk.CssProvider()
    shadow.load_from_string(
        "popover > contents { box-shadow: 0 6px 40px 12px alpha(black, 0.3); }"
    )
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), shadow, Gtk.STYLE_PROVIDER_PRIORITY_USER
    )
    window.file_queue.add_file(str(audio))
    pump(lambda: not window.file_queue.pending)
    window.present()
    popovers = window.bar_popovers
    speed, voice = (b.get_parent() for b in (window.speed_button, window.voice_button))
    pump(lambda: speed.get_mapped())
    popovers.open("speed")
    pump(lambda: popovers.popovers["speed"].get_mapped())
    # The microphone lies under the speed card's shadow, not under the card.
    controller = SimpleNamespace(get_widget=lambda: voice)
    popovers._on_anchor_pointer(controller, 4, voice.get_height() / 2, "voice")
    assert popovers.popovers["voice"].get_visible()
    assert not popovers.popovers["speed"].get_visible()
    popovers.close_all()
    Gtk.StyleContext.remove_provider_for_display(Gdk.Display.get_default(), shadow)


def test_a_tile_card_opens_once_the_pointer_rests_on_the_tile(window):
    import time
    from types import SimpleNamespace

    popovers = window.effect_cards.popovers
    tile = window.compressor_tile
    window.present()
    pump(lambda: tile.get_mapped())
    controller = SimpleNamespace(get_widget=lambda: tile)
    # Passing over: in and out within the wait opens nothing.
    popovers._on_anchor_pointer(controller, 10, 10, tile)
    assert not tile.popover.get_visible()
    popovers._on_anchor_leave(controller, tile)
    started = time.monotonic()
    pump(lambda: time.monotonic() - started > 0.3)
    assert not tile.popover.get_visible()
    # Resting: it opens after the wait.
    popovers._on_anchor_pointer(controller, 10, 10, tile)
    started = time.monotonic()
    pump(lambda: tile.popover.get_visible())
    assert time.monotonic() - started >= 0.14
    tile.popover.popdown()


def test_output_and_encoding_choices_are_tiles_and_count_as_no_adjustment(
    window, tmp_path
):
    format_tile, quality = window.format_row, window.bitrate_row
    assert format_tile.value_label.get_text() == "MP3"
    assert quality.value_label.get_text() == "Very good · 192 kbps"
    # Neighbouring bitrates share a quality word; the bitrate tells them apart.
    names = [
        quality._model.get_string(i).partition(" — ")[0]
        for i in range(quality._model.get_n_items())
    ]
    assert len(set(names)) == len(names) and names[0] == "32 kbps"
    format_tile.set_selected(window._format_list.index("flac"))
    assert format_tile.value_label.get_text() == "FLAC"
    assert not quality.get_sensitive()
    assert quality.value_label.get_text() == "Lossless"
    assert window.precision_list.get_visible()
    window.channels_row.set_selected(1)
    assert window.channels_row.value_label.get_text() == "Mono"
    # Choosing what the file becomes is no adjustment of its sound.
    assert window.effect_cards.active_count() == 0
    # The destination sits under the queue and follows the folder in use.
    window._set_output_folder(str(tmp_path))
    assert window.destination_choice.get_selected() == 1
    assert window.destination_button.get_visible()
    window.destination_choice.set_selected(0)
    assert window.output_directory == ""
    assert not window.destination_button.get_visible()


def test_part_choices_appear_with_marks_and_joining_with_two_files(
    window, audio, tmp_path
):
    queue = window.file_queue
    # Every tile is in place from the start; unavailable ones say why on
    # the tile, where the value goes.
    assert not window.pause_tile.get_sensitive()
    assert window.pause_tile.value_label.get_text() == "Add a file first."
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    assert window.pause_tile.get_sensitive()
    assert not window.cut_mode_row.get_sensitive()
    assert window.cut_mode_row.unavailable == "Mark a part on the waveform first."
    assert window.cut_mode_row.value_label.get_text() == window.cut_mode_row.unavailable
    assert window.join_row.unavailable == "Add another file to join them."
    assert not window.join_row.get_sensitive()
    window.visualizer.restore_markers([{"start": 0.2, "stop": 0.5}])
    assert window.cut_mode_row.get_sensitive()
    assert window.cut_mode_row.value_label.get_text() == "Keep them"
    second = tmp_path / "second.wav"
    second.write_bytes(audio.read_bytes())
    queue.add_file(str(second))
    pump(lambda: not queue.pending and len(queue.files) == 2)
    assert window.join_row.get_sensitive()
    window.join_row.set_active(True)
    # Joining decides what the files become; it adjusts no sound.
    assert window.effect_cards.active_count() == 0


def test_a_long_option_in_a_card_wraps_instead_of_widening_it(window):
    # FLAC adds the 24-bit row, whose explanation is one long line.
    tile = window.format_row
    tile.set_selected(window._format_list.index("flac"))
    window.present()
    pump(lambda: tile.get_mapped())
    window.effect_cards.popovers.open(tile)
    pump(lambda: tile.popover.get_mapped() and tile.popover.get_width())
    assert tile.popover.get_width() <= 380
    tile.popover.popdown()
