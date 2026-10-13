"""Native GTK integration contracts, run under Xvfb and a session bus."""

import os
import shutil
import subprocess

import pytest
from conftest import pump, start_window, stop_window

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="A graphical display is required",
)


def test_bitrate_preview_and_waveform_marks(window, audio):
    assert window.format_row.get_selected_item().get_string()
    assert window._bitrate_list[window.bitrate_row.get_selected()] == "192k"
    window.bitrate_row.set_selected(window._bitrate_list.index("128k"))
    assert window.app.config.get("conversion_bitrate") == "128k"
    window.format_row.set_selected(window._format_list.index("flac"))
    assert not window.bitrate_row.get_sensitive()
    window.format_row.set_selected(window._format_list.index("copy"))
    assert not window.volume_spin.is_sensitive()
    assert not window.compressor_row.is_sensitive()
    assert not window.voice_scale.get_sensitive()
    assert window.format_row.get_sensitive()
    window.format_row.set_selected(window._format_list.index("flac"))
    assert window.volume_spin.is_sensitive()
    # Back on when the noise plugin is installed, which CI machines lack.
    assert window.voice_scale.get_sensitive() == window.noise_available
    window.compressor_row.steps.set_selected(1)
    window.original_preview.set_active_name("original")
    assert window.player.effects_bypassed
    window.format_row.set_selected(window._format_list.index("mp3"))
    assert window.player.effects_bypassed
    window.original_preview.set_active_name("adjusted")
    assert not window.player.effects_bypassed
    window.compressor_row.steps.set_selected(0)
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
def test_segment_editor_apply_reachable_by_keyboard(window, audio):
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


def test_noise_modes_preview_and_plugin_recovery(window, audio):
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


def test_volume_drag_settles_on_magnets(window):
    from gi.repository import Gtk

    scale = window.volume_scale
    hundred = window._volume_to_slider(100)
    scale.emit("change-value", Gtk.ScrollType.JUMP, hundred + 2)
    assert window.volume_spin.get_value() == 100
    scale.emit("change-value", Gtk.ScrollType.STEP_FORWARD, hundred + 0.5)
    assert window.volume_spin.get_value() > 100
    window.volume_spin.set_value(100)
    window.volume_spin.spin(Gtk.SpinType.STEP_FORWARD, 1)
    assert window.volume_spin.get_value() == 101


def test_reset_settings_restores_first_run_values(window, tmp_path):
    config = window.app.config
    window.format_row.set_selected(window._format_list.index("opus"))
    window.bitrate_row.set_selected(window._bitrate_list.index("128k"))
    window.channels_row.set_selected(1)
    window._set_output_folder(str(tmp_path))
    window.cut_order_row.set_selected(1)
    window.volume_spin.set_value(250)
    window.speed_spin.set_value(1.5)
    window.compressor_row.steps.set_selected(2)
    window.normalize_row.set_active(True)
    window.eq_panel.band_scales[window.eq_panel.BANDS[0][1]].set_value(6)
    window.activate_action("win.auto-advance", None)
    assert window.app.config.get("auto_advance_enabled") is False
    window.delete_originals_switch.set_active(True)
    assert window._collect_conversion_settings()["delete_originals"]
    window.voice_only_scale.set_value(5)
    window.voice_only_switch.set_active(True)
    assert window.player.voice_only and window.player.voice_only_sensitivity == 5
    assert window.app.config.get("voice_only_enabled") == "true"
    settings = window._collect_conversion_settings()
    assert settings["voice_only"] and settings["voice_only_sensitivity"] == 5

    volume, speed = (
        b.get_parent() for b in (window.volume_button, window.speed_button)
    )
    assert window.equalizer_button.has_css_class("attention")
    assert volume.has_css_class("attention") and speed.has_css_class("attention")
    window._on_reset_settings_response(None, "cancel")
    assert window.compressor_row.steps.get_selected() == 2

    window._on_reset_settings_response(None, "reset")
    assert window._format_list[window.format_row.get_selected()] == "mp3"
    assert window._bitrate_list[window.bitrate_row.get_selected()] == "192k"
    assert window.bitrate_row.get_sensitive()
    assert window.channels_row.get_selected() == 0
    assert window.output_directory == ""
    assert window.cut_order_row.get_selected() == 0
    assert window.volume_spin.get_value() == 100
    assert window.speed_spin.get_value() == 1.0
    assert window.compressor_row.steps.get_selected() == 0
    assert not window.normalize_row.get_active()
    assert all(s.get_value() == 0 for s in window.eq_panel.band_scales.values())
    assert window.auto_advance_action.get_state().get_boolean()
    assert not window.player.normalize
    assert not window.equalizer_button.has_css_class("attention")
    assert not window.delete_originals_switch.get_active()
    assert not window.voice_only_switch.get_active()
    assert not window.player.voice_only
    assert window.app.config.get("delete_originals") is False
    assert not volume.has_css_class("attention")
    assert not speed.has_css_class("attention")
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


def test_reset_restores_pause_choices(window):
    panel = window.pause_panel
    panel.length_buttons[2].set_active(True)
    panel.sensitivity.set_value(5)
    window._on_reset_settings_response(None, "reset")
    assert panel.length_buttons[1].get_active()
    assert panel.sensitivity_label.get_text() == "Normal"
    assert window.app.config.get("pause_length") == 1.0
    assert window.app.config.get("voice_sensitivity") == 3


def test_waveform_panning_moves_the_seekbar_window(window):
    window.visualizer.zoom_level = 4.0
    window.visualizer.viewport_offset = 0.5
    assert window.seekbar._viewport_offset == 0.5
    window.visualizer.viewport_offset = 0.25
    assert window.seekbar._viewport_offset == 0.25


def test_compressor_uses_named_steps_starting_off(window):
    config = window.app.config
    window.compressor_row.steps.set_selected(2)
    settings = window._collect_conversion_settings()
    assert settings["compressor_enabled"] and settings["compressor_intensity"] == 0.65
    assert window.player.compressor_enabled
    assert window.compressor_row.value_label.get_text() == "Medium"
    assert config.get("compressor_intensity") == "0.65"

    window.compressor_row.steps.set_selected(0)
    assert not window._collect_conversion_settings()["compressor_enabled"]
    assert window.compressor_row.value_label.get_text() == "Off"
    assert not window.player.compressor_enabled
    # Off keeps the strength for the next session's restore of a later choice.
    assert config.get("compressor_enabled") == "false"
    assert config.get("compressor_intensity") == "0.65"

    # A strength saved by the former slider comes back as the nearest step.
    config.set("compressor_enabled", "true")
    config.set("compressor_intensity", "0.75")
    window._restore_conversion_settings()
    assert window.compressor_row.steps.get_selected() == 2
    config.set("compressor_enabled", "false")
    window._restore_conversion_settings()
    assert window.compressor_row.steps.get_selected() == 0


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


def test_removed_or_cleared_files_forget_cuts(window, audio):
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


def test_failed_load_keeps_playback_stopped(window, audio, monkeypatch):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    played = []
    window.player.current_file = None
    monkeypatch.setattr(window.player, "load", lambda *args: False)
    monkeypatch.setattr(window.player, "play", lambda: played.append(True))
    window._on_pause_play_clicked(None)
    assert played == []


def test_closing_during_a_conversion_asks_first(window, monkeypatch):
    monkeypatch.setattr(type(window.converter), "busy", property(lambda self: True))
    assert window.on_close_request(window) is True
    assert not window._closed
    window._on_close_while_converting(None, "keep")
    assert not window._closed
    window._on_close_while_converting(None, "stop")
    assert window._closed


def test_application_icons_resolve_by_name(window):
    from gi.repository import Gtk

    theme = Gtk.IconTheme.get_for_display(window.get_display())
    for name in (
        "big-audio-converter-equalizer-symbolic",
        "big-audio-converter-remove-pauses-symbolic",
    ):
        assert theme.has_icon(name), name


def test_window_cleanup_keeps_shared_player_converter(window):
    app = window.get_application()
    window.cleanup()
    assert not app.player._closed
    assert not app.converter._disposed


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


def test_custom_equalizer_persists_across_sessions(tmp_path, monkeypatch):
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
        assert win.equalizer_button.has_css_class("attention")
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


def test_found_pauses_mark_speech_with_undo(window, audio):
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
    assert panel.undo_row.get_title() == "Speech marked in 2 segments"
    assert panel.undo_row.get_subtitle().split("\n") == [
        "1 pause · 0:00 shorter",
        "This replaces the 1 segment already marked on the file.",
        "Nothing is deleted from the original file.",
    ]
    assert not panel.actions.get_visible()
    # Another length replaces the panel's segments, never the user's.
    panel.length_buttons[2].set_active(True)  # 2 s: no pause that long
    assert marks(window) == [(0.1, 0.3)]
    assert not panel.undo_row.get_visible()
    panel.length_buttons[0].set_active(True)
    assert marks(window) == [(0.0, 0.65), (1.35, 2.0)]
    panel.undo_apply()
    assert marks(window) == [(0.1, 0.3)]
    assert window.cut_output_row.get_selected() == 0
    assert not panel.undo_row.get_visible()


def test_voice_detection_step_measures_again(window, audio, monkeypatch):
    from app.audio.media import MediaSource
    from app.audio.pauses import PauseFinder

    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    panel = window.pause_panel
    assert panel.sensitivity.get_value() == 3
    assert panel.sensitivity_label.get_text() == "Normal"
    started = []
    monkeypatch.setattr(
        PauseFinder, "start", lambda _f, key, *args: started.append((key, args))
    )
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    levels = [-6.0] * 50 + [-80.0] * 100 + [-6.0] * 50
    panel.length_buttons[0].set_active(True)
    panel._on_found(None, levels)
    assert marks(window) == [(0.0, 0.65), (1.35, 2.0)]

    # The levels were heard at Normal: another step drops their marks and
    # measures the file at that step.
    panel.sensitivity.set_value(5)
    assert window.app.config.get("voice_sensitivity") == 5
    assert panel.sensitivity_label.get_text() == "Very strict"
    assert marks(window) == []
    [(key, args)] = started
    assert key[-1] == 5 and args[3] == 5
    assert panel.progress_row.get_visible()

    # Each step has its own cache entry: Normal marks again without measuring.
    source = MediaSource.resolve(window.active_audio_id, queue.track_metadata)
    panel.finder._cache[PauseFinder.identity(source, 3)] = levels
    monkeypatch.setattr(type(panel.finder), "busy", property(lambda _f: True))
    panel.sensitivity.set_value(3)
    assert len(started) == 1
    assert marks(window) == [(0.0, 0.65), (1.35, 2.0)]


def test_editing_found_marks_reoffers_finding(window, audio):
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


def test_pauses_past_file_end_are_reported(window, audio, monkeypatch):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    shown = []
    monkeypatch.setattr(window, "_show_message", lambda *args: shown.append(args))
    panel = window.pause_panel
    panel._on_found(None, [-6.0] * 300 + [-80.0] * 100)  # 4 s measured, 2 s shown
    assert shown and shown[0][0] == "The pauses could not be marked"
    assert marks(window) == []


def test_installed_denoiser_is_noticed_live(window, audio, monkeypatch):
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


def test_new_session_keeps_marked_parts(tmp_path, monkeypatch):
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


def test_volume_and_speed_persist_across_sessions(tmp_path, monkeypatch):
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


def test_equalizer_button_toggles_its_panel(window):
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


def test_switch_row_flips_on_activation(window):
    row = window.normalize_row
    row.activate()
    assert row.get_active() and window.player.normalize
    assert row.value_label.get_text() == "On"
    assert not window.effect_cards.hover_cards.is_open(row)
    row.activate()
    assert not row.get_active()


def test_copy_mode_disables_the_effect_rows(window):
    window.present()
    pump(lambda: window.compressor_row.get_mapped())
    cards = window.effect_cards.hover_cards
    window.compressor_row.activate()
    assert cards.is_open(window.compressor_row)
    window.format_row.set_selected(window._format_list.index("copy"))
    assert not cards.is_open(window.compressor_row)
    assert not window.voice_scale.get_sensitive()
    assert not window.compressor_row.is_sensitive()
    # The row says why, where its value was.
    reason = "The original format keeps the audio as it is."
    assert window.compressor_row.reason_label.get_text() == reason
    assert not window.compressor_row.value_label.get_visible()
    window.format_row.set_selected(window._format_list.index("mp3"))
    assert window.compressor_row.value_label.get_text() == "Off"
    assert not window.compressor_row.reason_label.get_visible()


def test_pause_progress_shows_on_the_row(window, audio, monkeypatch):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    panel = window.pause_panel
    # The panel is the card of a row in the Cuts section.
    card = window.effect_cards.hover_cards.panels[window.pause_row]
    assert panel.is_ancestor(card)
    assert window.pause_row.value_label.get_text() == "Off"
    running = [True]  # an analysis in progress
    monkeypatch.setattr(type(panel.finder), "busy", property(lambda _f: running[0]))
    panel._show_state("busy")
    panel._on_progress(0.4)
    assert panel.progress_row.get_visible()
    assert panel.status_row.get_title() == "Finding pauses: 40%"
    # The row follows it with the card closed.
    assert window.pause_row.value_label.get_text() == "40%"
    assert panel.actions.get_visible_child_name() == "cancel"
    running[0] = False
    panel._on_cancel()
    assert not panel.progress_row.get_visible()
    assert window.pause_row.value_label.get_text() == "Off"


def test_original_preview_needs_an_active_adjustment(window):
    preview = window.original_preview
    assert not preview.get_visible()
    window.compressor_row.steps.set_selected(1)
    assert preview.get_visible()
    preview.set_active_name("original")
    assert window.player.effects_bypassed
    # Hiding it must never leave the preview silently bypassed.
    window.compressor_row.steps.set_selected(0)
    assert not preview.get_visible()
    assert preview.get_active_name() == "adjusted"
    assert not window.player.effects_bypassed


def test_bar_shows_time_and_zoom_slider(window, audio):
    window.file_queue.add_file(str(audio))
    pump(lambda: window.visualizer.duration > 0)
    assert (window.elapsed_label.get_label(), window.total_label.get_label()) == (
        "0:00",
        "0:02",
    )
    window.seekbar.set_position(1.4)
    assert window.elapsed_label.get_label() == "0:01"
    assert not window.zoom_reset_button.get_sensitive()
    # Halfway up the slider is the square root of the deepest zoom.
    window.zoom_adjustment.set_value(50)
    assert round(window.visualizer.zoom_level) == round(window.visualizer.max_zoom**0.5)
    assert window.zoom_value.get_label() == "32×"
    window.zoom_adjustment.set_value(100)
    assert window.zoom_value.get_label() == "1000×"
    window.zoom_adjustment.set_value(5)
    assert window.zoom_value.get_label() == "1.4×"
    window.zoom_adjustment.set_value(50)
    assert window.zoom_reset_button.get_sensitive()
    # Zooming on the waveform moves the slider with it.
    window.visualizer.zoom_out()
    assert window.zoom_adjustment.get_value() < 50
    window.zoom_reset_button.emit("clicked")
    assert window.visualizer.zoom_level == 1
    assert window.zoom_adjustment.get_value() == 0
    assert window.zoom_value.get_label() == "1×"


def test_output_choices_count_as_no_adjustment(window, tmp_path):
    format_choice, quality = window.format_row, window.bitrate_row
    assert format_choice.value_label.get_text() == "MP3"
    assert quality.value_label.get_text() == "Very good · 192 kbps"
    # Neighbouring bitrates share a quality word; the bitrate tells them apart.
    names = [
        quality._model.get_string(i).partition(" — ")[0]
        for i in range(quality._model.get_n_items())
    ]
    assert len(set(names)) == len(names) and names[0] == "32 kbps"
    format_choice.set_selected(window._format_list.index("flac"))
    assert format_choice.value_label.get_text() == "FLAC"
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


def test_part_choices_unlock_when_usable(window, audio, tmp_path):
    queue = window.file_queue
    # Every row is in place from the start; unavailable ones say why on
    # the row, where the value goes.
    assert not window.pause_row.get_sensitive()
    assert window.pause_row.reason_label.get_text() == "Add a file first."
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    assert window.pause_row.get_sensitive()
    assert not window.cut_mode_row.get_sensitive()
    assert window.cut_mode_row.unavailable == "Mark a segment on the waveform first."
    assert (
        window.cut_mode_row.reason_label.get_text() == window.cut_mode_row.unavailable
    )
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


def test_hover_help_toggles_every_tooltip_card(window):
    from big_gtk_kit import tooltip
    from gi.repository import GLib

    tips = window.get_application().lookup_action("toggle-tips")
    tips.change_state(GLib.Variant.new_boolean(False))
    assert not tooltip._enabled and not window.visualizer.show_hint
    # Screen readers keep the text as the button's description.
    assert window.segment_edit_button.get_tooltip_text()
    tips.change_state(GLib.Variant.new_boolean(True))
    assert tooltip._enabled and window.visualizer.show_hint


def test_kept_parts_draw_green_with_edges():
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    import cairo
    import numpy as np
    from app.ui.visualizer import AudioVisualizer

    view = AudioVisualizer()
    view.set_waveform({"peaks": np.full(6000, 0.8, dtype=np.float32), "rate": 1000}, 6)
    view.duration = 6.0
    # A two-second part and one of 10 ms, two pixels at this width.
    assert view.restore_markers([{"start": 1, "stop": 3}, {"start": 4, "stop": 4.01}])
    width, height = 1200, 120
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    view.on_draw(view, cairo.Context(surface), width, height)
    surface.flush()
    data, stride = surface.get_data(), surface.get_stride()

    def rgb(x, y):
        b, g, r = data[y * stride + 4 * x : y * stride + 4 * x + 3]
        return r, g, b

    inside, outside = rgb(400, 60), rgb(100, 60)
    assert inside[1] > inside[0] + 60 and inside[1] > inside[2] + 60
    assert not outside[1] > outside[2] + 60
    start, stop = rgb(799, 60), rgb(802, 60)
    assert start[0] > start[1] + 60  # red start edge
    assert stop[1] > stop[0] + 60  # green end edge


def test_seekbar_lines_up_with_waveform(window, audio):
    window.file_queue.add_file(str(audio))
    pump(lambda: window.visualizer.duration > 0)
    pump()

    def span(widget):
        found, box = widget.compute_bounds(window)
        assert found
        return box.get_x(), box.get_x() + box.get_width()

    # The track, inset for the thumb, covers the waveform's width exactly.
    seek_x0, _seek_x1 = span(window.seekbar)
    track_x0 = seek_x0 + window.seekbar._track_x
    track = (track_x0, track_x0 + window.seekbar._track_w)
    assert track == span(window.visualizer)
    # "Only the selection" on the left, "Edit Segments" on the right, before
    # the zoom.
    _sel_x0, sel_x1 = span(window.play_selection_switch)
    play_x0, play_x1 = span(window.pause_play_btn)
    edit_x0, edit_x1 = span(window.segment_edit_button)
    assert sel_x1 < play_x0 and play_x1 < edit_x0
    assert edit_x1 < span(window.zoom_button)[0]
