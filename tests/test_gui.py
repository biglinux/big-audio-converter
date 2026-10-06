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


def test_join_offered_for_several_files_and_not_in_fast_copy(window, audio, tmp_path):
    queue = window.file_queue
    second = tmp_path / "second.wav"
    second.write_bytes(audio.read_bytes())
    assert queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    assert not window.join_row.get_visible()
    assert queue.add_file(str(second))
    pump(lambda: not queue.pending)
    assert window.join_row.get_visible()
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
    assert not window.bitrate_row.get_visible()
    window.format_row.set_selected(window._format_list.index("copy"))
    assert not window.volume_spin.is_sensitive()
    assert not window.gate_row.scale.is_sensitive()
    assert not window.noise_scale.get_sensitive()
    assert window.format_row.get_sensitive()
    window.format_row.set_selected(window._format_list.index("flac"))
    assert window.volume_spin.is_sensitive()
    # Back on when the noise plugin is installed, which CI machines lack.
    assert window.noise_scale.get_sensitive() == window.noise_available
    window.original_preview.set_active(True)
    assert window.player.effects_bypassed
    window.format_row.set_selected(window._format_list.index("mp3"))
    assert window.player.effects_bypassed
    window.original_preview.set_active(False)
    assert not window.player.effects_bypassed
    window.file_queue.add_file(str(audio))
    vis = window.visualizer
    pump(lambda: vis.waveform_data is not None and vis.get_width() > 0)
    assert window.segment_edit_button.get_visible()
    assert not window.cut_order_row.get_visible()
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
    assert window.cut_order_row.get_visible()
    assert window.play_selection_switch.get_visible()
    settings = window._collect_conversion_settings()
    [segment] = settings["file_markers"][window.active_audio_id]
    assert segment["start"] == pytest.approx(duration * 0.2, abs=0.01)
    assert segment["stop"] == pytest.approx(duration * 0.8, abs=0.01)

    # Removing the marked part exports and previews what lies around it.
    window.cut_mode_row.set_selected(1)
    assert vis.remove_marked
    assert not window.cut_order_row.get_visible()
    assert not window.cut_output_row.get_visible()
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
        window.noise_model_row.set_selected(index)
        window.noise_scale.set_value(25)
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
    assert not window.noise_scale.get_sensitive()
    assert "dpdfnet-native" in window.noise_status.get_label()
    assert window.noise_status.get_visible()
    assert window.noise_model_row.get_visible()
    window.noise_model_row.set_selected(0)
    assert window.noise_scale.get_sensitive()
    assert player.noise_reduction and player.noise_engine == "dfn3"
    assert window.noise_value_label.get_text() == "Clean voice · 25%"


def test_reset_settings_restores_first_run_values(window, tmp_path):
    config = window.app.config
    window.format_row.set_selected(window._format_list.index("opus"))
    window.bitrate_row.set_selected(window._bitrate_list.index("128k"))
    window.channels_row.set_selected(1)
    window._set_output_folder(str(tmp_path))
    window.cut_order_row.set_selected(1)
    window.volume_spin.set_value(250)
    window.speed_spin.set_value(1.5)
    window.gate_row.scale.set_value(40)
    window.hpf_row.scale.set_value(120)
    window.normalize_row.set_active(True)
    window.eq_panel.band_scales[window.eq_panel.BANDS[0][1]].set_value(6)
    window.auto_advance_switch.set_active(False)

    assert window.equalizer_row.get_subtitle() == "Custom"
    window._on_reset_settings_response(None, "cancel")
    assert window.gate_row.scale.get_value() == 40

    window._on_reset_settings_response(None, "reset")
    assert window._format_list[window.format_row.get_selected()] == "mp3"
    assert window._bitrate_list[window.bitrate_row.get_selected()] == "192k"
    assert window.bitrate_row.get_visible()
    assert window.channels_row.get_selected() == 0
    assert window.output_directory == ""
    assert window.cut_order_row.get_selected() == 0
    assert window.volume_spin.get_value() == 100
    assert window.speed_spin.get_value() == 1.0
    assert window.gate_row.scale.get_value() == 0
    assert window.hpf_row.scale.get_value() == 0
    assert not window.normalize_row.get_active()
    assert all(s.get_value() == 0 for s in window.eq_panel.band_scales.values())
    assert window.auto_advance_switch.get_active()
    assert not window.player.normalize
    assert window.equalizer_row.get_subtitle() == "Natural (No Effects)"
    for key, value in (
        ("conversion_format", "mp3"),
        ("audio_channels", "0"),
        ("output_directory", ""),
        ("cut_audio_mode", "0"),
        ("gate_enabled", "false"),
        ("hpf_enabled", "false"),
        ("normalize_enabled", "false"),
        ("auto_advance_enabled", True),
    ):
        assert config.get(key) == value, key
    # A first-run value that was never changed is simply absent.
    assert config.get("conversion_bitrate", "192k") == "192k"
    assert float(config.get("conversion_volume")) == 100


def test_information_size_is_the_file_unless_other_tracks_share_it(window, audio):
    from app.ui.file_queue import shares_file

    track = {"index": 0, "codec_type": "audio", "duration": "2.0"}
    cover = {"index": 1, "codec_type": "video", "disposition": {"attached_pic": 1}}
    video = {"index": 1, "codec_type": "video", "disposition": {"attached_pic": 0}}
    assert not shares_file({"streams": [track]}, track)
    assert not shares_file({"streams": [track, cover]}, track)
    assert shares_file({"streams": [track, video]}, track)

    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    row = queue.file_rows[0]

    def size(stream, shares):
        info = {"streams": [stream], "format": {}}
        return dict(row._extract_audio_props(stream, info, shares, str(audio)))["Size"]

    assert size(track, False) == row._format_size(audio.stat().st_size)
    assert size(track, True) == "Unknown"
    assert size({**track, "bit_rate": "128000"}, True) == row._format_size(32000)


def test_panning_the_waveform_moves_the_seek_bar_window(window):
    window.visualizer.zoom_level = 4.0
    window.visualizer.viewport_offset = 0.5
    assert window.seekbar._viewport_offset == 0.5
    window.visualizer.viewport_offset = 0.25
    assert window.seekbar._viewport_offset == 0.25


def test_each_correction_is_off_at_zero_and_set_by_its_slider(window):
    config = window.app.config
    window.gate_row.scale.set_value(40)
    window.compressor_row.scale.set_value(75)
    window.hpf_row.scale.set_value(5)
    settings = window._collect_conversion_settings()
    assert settings["gate_enabled"] and settings["gate_intensity"] == 0.4
    assert settings["compressor_enabled"] and settings["compressor_intensity"] == 0.75
    # The filter starts at 20 Hz: the first step above off means that.
    assert settings["hpf_enabled"] and settings["hpf_frequency"] == 20
    assert window.hpf_row.value_label.get_text() == "20 Hz"
    assert window.player.gate_enabled
    assert config.get("compressor_intensity") == "0.75"

    window.gate_row.scale.set_value(0)
    assert not window._collect_conversion_settings()["gate_enabled"]
    assert window.gate_row.value_label.get_text() == "Off"
    assert not window.player.gate_enabled
    # Off keeps the strength for the next session's restore of a later choice.
    assert config.get("gate_enabled") == "false"
    assert config.get("gate_intensity") == "0.4"

    window._restore_conversion_settings()
    assert window.gate_row.scale.get_value() == 0
    assert window.compressor_row.scale.get_value() == 75


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
    assert not window.cut_mode_row.get_visible()

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


def test_custom_equalizer_is_named_and_kept_between_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    win = start_window()
    try:
        panel = win.eq_panel
        panel.preset_dropdown.set_selected(panel.PRESET_KEYS.index("podcast"))
        panel.preset_dropdown.set_selected(panel.PRESET_KEYS.index("custom"))
        assert win.equalizer_row.get_subtitle() == "Custom"
        panel.band_scales[1000].set_value(7)
    finally:
        stop_window(win)
    win = start_window()
    try:
        assert win.eq_panel.band_scales[1000].get_value() == 7
        assert win.eq_panel.band_scales[31].get_value() == 5  # from Radio Host
        assert win.equalizer_row.get_subtitle() == "Custom"
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


def test_found_pauses_say_what_they_replace_and_undo_restores_it(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    window.visualizer.restore_markers([{"start": 0.1, "stop": 0.3}])
    popover = window.pause_popover
    popover.levels = [-6.0] * 50 + [-80.0] * 100 + [-6.0] * 50
    popover._show_state()
    assert "replaces the 1 mark" in popover.summary.get_label()

    popover._on_apply(None)
    assert marks(window) == [(0.65, 1.35)]
    assert window.removes_marked()
    popover.undo_apply()
    assert marks(window) == [(0.1, 0.3)]
    assert not window.removes_marked()


def test_pauses_past_the_end_of_the_file_are_reported(window, audio, monkeypatch):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    shown = []
    monkeypatch.setattr(window, "_show_message", lambda *args: shown.append(args))
    popover = window.pause_popover
    popover.levels = [-6.0] * 300 + [-80.0] * 100  # 4 s measured, 2 s on screen
    popover._show_state()
    popover._on_apply(None)
    assert shown and shown[0][0] == "The pauses could not be marked"
    assert marks(window) == []


def test_installing_the_denoiser_is_noticed_without_a_restart(
    window, audio, monkeypatch
):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    popover = window.pause_popover
    monkeypatch.setattr("shutil.which", lambda _name: None)
    popover._show_state()
    assert popover.stack.get_visible_child_name() == "missing"

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    popover.popup()
    pump(lambda: popover.get_visible())
    assert popover.stack.get_visible_child_name() == "start"
    popover.popdown()
