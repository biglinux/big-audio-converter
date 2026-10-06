"""The main window: header, settings sidebar, queue and playback panel."""

import gettext
import logging
import os
import weakref
from copy import deepcopy
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from app.audio.waveform import WaveformGenerator
from app.ui.controls_bar_mixin import ControlsBarMixin
from app.ui.conversion_controller import ConversionController
from app.ui.equalizer_panel import EqualizerPanel
from app.ui.file_queue import FileQueue
from app.ui.pause_popover import PausePopover
from app.ui.playback_controller import PlaybackControllerMixin
from app.ui.segment_editor import SegmentEditor
from app.ui.settings_mixin import SettingsManagerMixin
from app.ui.visualizer import AudioVisualizer, SeekBar
from app.utils.main_loop import MainLoopSources, weak_callback
from app.utils.tooltip_helper import TooltipHelper

_ = gettext.gettext
logger = logging.getLogger(__name__)


class HeaderBar(Gtk.Box):
    """Queue controls, Add Files and Convert above the queue."""

    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.main_window = weakref.proxy(main_window)
        self.header_bar = Adw.HeaderBar(hexpand=True, show_title=True)
        self.header_bar.set_decoration_layout(":minimize,maximize,close")
        self.append(self.header_bar)
        self._create_ui_elements()

    def _create_ui_elements(self):
        # Named in words: the rows' own remove buttons use the delete icon.
        self.clear_queue_button = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="edit-clear-all-symbolic", label=_("Clear queue")
            )
        )
        self.clear_queue_button.add_css_class("flat")
        self.clear_queue_button.set_valign(Gtk.Align.CENTER)
        self.clear_queue_button.connect(
            "clicked", weak_callback(self.main_window.on_clear_queue)
        )
        self.clear_queue_button.set_visible(False)
        self.clear_queue_button.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Clear queue")],
        )

        self.queue_size_label = Gtk.Label(label=_("0 files"))
        self.queue_size_label.add_css_class("caption")
        self.queue_size_label.add_css_class("dim-label")
        self.queue_size_label.set_visible(False)
        self.queue_size_label.set_margin_start(4)
        self.queue_size_label.set_margin_end(8)
        self.queue_size_label.set_valign(Gtk.Align.CENTER)

        menu = Gio.Menu()
        menu.append(_("Show help on hover"), "app.toggle-tips")
        menu.append(_("Show Welcome Screen"), "app.show-welcome")
        settings_section = Gio.Menu()
        settings_section.append(_("Reset All Settings…"), "win.reset-settings")
        menu.append_section(None, settings_section)
        about_section = Gio.Menu()
        about_section.append(_("About"), "app.about")
        menu.append_section(None, about_section)
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        menu_button.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Main menu")],
        )

        app = self.main_window.app
        tips_enabled = (
            str(app.config.get("show_mouseover_tips", "true")).lower() == "true"
        )
        tips_action = Gio.SimpleAction.new_stateful(
            "toggle-tips", None, GLib.Variant.new_boolean(tips_enabled)
        )
        tips_action.connect(
            "change-state", weak_callback(self.main_window._on_tips_action_changed)
        )
        app.add_action(tips_action)

        self.header_bar.pack_end(menu_button)

        left_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        left_controls.set_margin_start(0)
        left_controls.set_halign(Gtk.Align.START)
        left_controls.append(self.clear_queue_button)
        left_controls.append(self.queue_size_label)

        self.header_bar.pack_start(left_controls)

        center_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        center_box.set_halign(Gtk.Align.CENTER)

        # Blue until the queue has files; then Convert is the next step.
        add_files_button = self.add_files_button = Gtk.Button(label=_("Add Files"))
        add_files_button.connect(
            "clicked", weak_callback(self.main_window.on_add_files)
        )
        add_files_button.add_css_class("suggested-action")
        center_box.append(add_files_button)

        self.convert_button = Gtk.Button(label=_("Convert"))
        self.convert_button.connect(
            "clicked", weak_callback(self.main_window.on_convert)
        )
        self.convert_button.add_css_class("suggested-action")
        self.convert_button.set_visible(False)
        center_box.append(self.convert_button)

        self.header_bar.set_title_widget(center_box)

    def set_queue_info_visible(self, visible):
        self.clear_queue_button.set_visible(visible)
        self.queue_size_label.set_visible(visible)

    def update_queue_label(self, text):
        self.queue_size_label.set_text(text)


class MainWindow(
    ControlsBarMixin,
    SettingsManagerMixin,
    PlaybackControllerMixin,
    Adw.ApplicationWindow,
):
    """Main application window."""

    def __init__(self, **kwargs):
        # Extract stored window size and maximized state (with defaults)
        default_width = 1100
        default_height = 800
        config = kwargs["application"].config

        saved_width = config.get("window_width")
        if saved_width:
            try:
                default_width = max(int(saved_width), 920)
            except (ValueError, TypeError):
                pass

        saved_height = config.get("window_height")
        if saved_height:
            try:
                default_height = max(int(saved_height), 600)
            except (ValueError, TypeError):
                pass

        is_maximized = str(config.get("window_maximized")).lower() == "true"

        super().__init__(
            title="Big Audio Converter",
            default_width=default_width,
            default_height=default_height,
            **kwargs,
        )

        self.add_css_class("big-audio-converter")

        self._should_maximize = is_maximized

        self.set_size_request(360, 480)

        self.app = kwargs.get("application")
        self._closed = False
        self._dialog_cancellable = Gio.Cancellable()
        self._probe_errors = []
        self._probe_error_dialog = None
        self._sources = MainLoopSources()
        self._playback_sources = MainLoopSources()
        self.waveform_generator = WaveformGenerator()

        self.tooltip_helper = TooltipHelper(self.app.config)

        self.player = self.app.player
        self.converter = self.app.converter
        self.file_markers = {}  # Dictionary mapping file path to marker pairs
        self.active_audio_id = None  # Queue entry whose waveform is shown

        # Selection playback tracking
        self._playing_selection = False
        self._selection_segments = []  # List of (start, stop) tuples
        self._current_segment_index = 0
        self._play_selection_mode = False  # Whether switch is on
        self._marker_dragging = False  # Track when user is dragging markers
        self._is_transitioning_segment = False  # a seek between parts is pending
        self._syncing_volume = False
        self._syncing_speed = False
        self._bar_popovers, self._bar_pointers, self._bar_scales = {}, {}, {}
        self._bar_closing, self._bar_keyboard = {}, {}

        # Default visualizer height - will be overridden by saved value
        self.visualizer_height = 132

        saved_height = config.get("visualizer_height")
        if saved_height:
            try:
                self.visualizer_height = max(100, int(saved_height))
            except (ValueError, TypeError):
                pass

        self.setup_ui()
        self.conversion = ConversionController(self)
        self.file_queue.on_probe_error = self._on_probe_error
        owner = weakref.proxy(self)
        self.file_queue.on_trash_error = lambda title, body: owner._show_message(
            title, body
        )
        self.file_queue.on_pending_changed = self._on_pending_changed
        self.setup_drop_target()
        self.connect("close-request", weak_callback(self.on_close_request))
        if self.app.config.load_warning:
            # The unreadable file is kept until a setting is changed.
            self.toast_overlay.add_toast(
                Adw.Toast(title=_("Settings could not be read and were reset"))
            )

        self.connect("map", weak_callback(self.on_window_mapped))

        # Connect window state signals (only maximized, save size on close only)
        self.connect("notify::maximized", weak_callback(self._on_window_state_changed))

        self.player.connect(
            "position-updated", weak_callback(self.on_player_position_updated)
        )
        self.player.connect(
            "duration-changed", weak_callback(self.on_player_duration_changed)
        )
        self.player.connect(
            "state-changed", weak_callback(self.on_player_state_changed)
        )
        self.player.connect("eos", weak_callback(self.on_playback_finished))
        self.player.connect("error", weak_callback(self._on_player_error))

        # Restore maximized state after window is fully initialized
        if self._should_maximize:
            self._sources.later(100, self.maximize)

        self.file_queue.connect_file_removed_signal(self._on_file_removed)

        # Setup window-level keyboard shortcuts
        self._setup_keyboard_shortcuts()

    def _setup_keyboard_shortcuts(self):
        """Register window-level keyboard shortcuts."""
        owner = weakref.proxy(self)
        add_files_action = Gio.SimpleAction.new("add-files", None)
        add_files_action.connect("activate", lambda *_: owner.on_add_files(None))
        self.add_action(add_files_action)

        convert_action = Gio.SimpleAction.new("convert", None)
        convert_action.connect("activate", lambda *_: owner.on_convert(None))
        self.add_action(convert_action)

        play_pause_action = Gio.SimpleAction.new("play-pause", None)
        play_pause_action.connect(
            "activate", lambda *_: owner._on_pause_play_clicked(None)
        )
        self.add_action(play_pause_action)

        reset = Gio.SimpleAction.new("reset-settings", None)
        reset.connect("activate", lambda *_: owner._confirm_reset_settings())
        self.add_action(reset)

        edit = Gio.SimpleAction.new("edit-segments", None)
        edit.connect("activate", lambda *_: owner.on_edit_segments())
        self.add_action(edit)

        app = self.get_application()
        app.set_accels_for_action("win.add-files", ["<Control>o"])
        app.set_accels_for_action("win.convert", ["<Control>Return"])
        app.set_accels_for_action("win.play-pause", ["<Control>space"])
        app.set_accels_for_action("win.edit-segments", ["<Control>e"])

    def setup_ui(self):
        """Set up the user interface."""
        owner = weakref.proxy(self)
        self.vertical_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay = Adw.ToastOverlay(child=self.vertical_paned)
        self.set_content(self.toast_overlay)
        self.vertical_paned.set_vexpand(True)

        # Prevent bottom controls from being cut off when window is resized
        self.vertical_paned.set_shrink_end_child(False)
        self.vertical_paned.set_resize_start_child(True)
        self.vertical_paned.set_resize_end_child(False)

        self.split_view = Adw.OverlaySplitView(vexpand=True)
        self.split_view.set_sidebar_position(Gtk.PackType.START)
        self.split_view.set_min_sidebar_width(280)
        # Wide enough that row subtitles keep to one line.
        self.split_view.set_max_sidebar_width(400)
        self.split_view.set_sidebar_width_fraction(0.34)
        self.vertical_paned.set_start_child(self.split_view)

        css_provider = self._css_provider = Gtk.CssProvider()
        css_provider.load_from_string("""
        .sidebar { background-color: @sidebar_bg_color; }
        .playback-controls { padding: 6px 10px; }
        /* The queue side is one light surface, set apart from the sidebar. */
        .queue-pane { background-color: @window_bg_color; color: @window_fg_color; }
        /* A player stays dark in both themes, matching the waveform's own
           background; flat controls follow currentColor. */
        .playback-panel { background-color: #1c1c21; color: rgba(255, 255, 255, 0.87); }
        .playback-panel frame { border-color: rgba(255, 255, 255, 0.08); }
        /* At fractional scales the content and the arrow end on different
           fractions of a pixel, leaving a seam at the arrow's base that their
           borders and the arrow's shadow darken. The content reaches a pixel
           into the arrow so the two solid fills overlap. */
        popover.bar-slider > contents { border: none; margin-bottom: -1px; }
        popover.bar-slider > arrow { border: none; box-shadow: none; }
        /* A tool on the waveform keeps the panel's own solid fill: the
           peaks behind a translucent one left its label unreadable. */
        .waveform-tool > button {
            background-color: #1c1c21;
            color: rgba(255, 255, 255, 0.87);
            box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.12);
        }
        .waveform-tool > button:hover { background-color: #2a2a31; }
        .waveform-tool > button:checked { background-color: mix(#1c1c21, @accent_bg_color, 0.55); }
        .playback-controls button:checked {
            background-color: alpha(@accent_bg_color, 0.55);
            color: #ffffff;
        }
        .queue-pane > revealer > windowhandle > headerbar,
        .queue-pane headerbar { background: none; box-shadow: none; }
        /* A spin row outlines itself while its number is being edited; in
           this dense list the caret already shows where typing goes. */
        .compact-options row.spin.focused { outline-color: transparent; }
        /* Rows at the height of the original compact list. */
        /* Only the list's own rows: a dropdown's popup is a descendant too. */
        .compact-options list > row { padding: 0; margin-top: -1px; margin-bottom: -1px; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # LEFT SIDE: conversion options. The window title already names the
        # app, so the options start at the top; empty space still moves the
        # window like a header bar would.
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_vexpand(True)
        left_scroll.add_css_class("sidebar")

        left_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_bottom=12)
        left_scroll.set_child(left_content)
        self.setup_conversion_options(left_content)
        left_box = Gtk.WindowHandle(child=left_scroll)

        # Right side: the queue.
        right_box = Adw.ToolbarView(css_classes=["queue-pane"])
        right_box.set_size_request(320, -1)

        # Create header bar for right side using the dedicated class
        # This handles proper layout behavior and resizing
        self.right_header = HeaderBar(self)
        right_box.add_top_bar(self.right_header)

        self.clear_queue_button = self.right_header.clear_queue_button
        self.convert_button = self.right_header.convert_button

        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        right_scroll.set_vexpand(True)

        right_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right_content.set_margin_start(10)
        right_content.set_margin_end(10)
        right_content.set_margin_bottom(10)

        self.file_queue = FileQueue(self.converter)
        self.file_queue._parent_window = self  # Set parent window for dialogs
        self.file_queue._tooltip_helper = self.tooltip_helper  # Pass tooltip helper
        right_content.append(self.file_queue)

        self.file_queue.on_queue_size_changed = self.update_queue_size_label

        self.file_queue.on_play_file = weak_callback(self.on_play_file)

        self.file_queue.on_stop_playback = self.player.stop

        self.file_queue.on_file_added_to_empty_queue = self.on_file_added_to_empty_queue

        self.file_queue.on_activate_file = self.on_activate_file

        right_scroll.set_child(right_content)

        right_box.set_content(right_scroll)

        self.eq_panel = EqualizerPanel(self.player, self.app.config)
        self.eq_panel.on_changed = weak_callback(self._show_equalizer_state)
        self._show_equalizer_state()
        self.eq_revealer = Gtk.Revealer()
        self.eq_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.eq_revealer.set_transition_duration(200)
        self.eq_revealer.set_reveal_child(False)
        self.eq_revealer.set_child(self.eq_panel)
        self.eq_revealer.connect(
            "notify::reveal-child", weak_callback(self._on_eq_revealer_changed)
        )
        right_box.add_bottom_bar(self.eq_revealer)

        self.split_view.set_sidebar(left_box)
        self.split_view.set_content(right_box)

        # Add visualizer at the bottom spanning full width
        # Create the visualizer instance
        self.visualizer = AudioVisualizer()

        # Give visualizer reference to player for checking playback state
        self.visualizer.player = self.player

        visualizer_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        visualizer_container.add_css_class("playback-panel")

        # The video player's bar: what shapes the sound on the left, playback
        # in the middle, the waveform and preview tools on the right.
        bar = Gtk.CenterBox(css_classes=["playback-controls"])
        start_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        end_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        bar.set_start_widget(start_box)
        bar.set_end_widget(end_box)

        # Plays only the marked parts.
        self.play_selection_switch = Gtk.ToggleButton(
            child=Adw.ButtonContent(
                icon_name="selection-mode-symbolic", label=_("Selection only")
            )
        )
        self.play_selection_switch.add_css_class("flat")
        self.play_selection_switch.set_active(False)
        self.play_selection_switch.connect(
            "toggled", weak_callback(self._on_play_selection_switch_toggled)
        )
        self.play_selection_switch.set_valign(Gtk.Align.CENTER)
        self.play_selection_switch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Play only selected area")],
        )

        end_box.append(self.play_selection_switch)

        # Next to the waveform it edits; cutting has to be on to use it.
        self.segment_edit_button = Gtk.Button(label=_("Edit Segments…"))
        self.segment_edit_button.add_css_class("flat")
        self.segment_edit_button.set_valign(Gtk.Align.CENTER)
        self.segment_edit_button.connect(
            "clicked", weak_callback(self.on_edit_segments)
        )
        end_box.append(self.segment_edit_button)

        # Rarely changed; it lives in the playback options menu below.
        self.auto_advance_switch = Adw.SwitchRow(
            title=_("Play the next file automatically"),
            active=self.app.config.get("auto_advance_enabled", True) is True,
        )
        self.auto_advance_switch.connect(
            "notify::active",
            lambda row, _pspec: owner._on_auto_advance_switch_toggled(row),
        )

        self.eq_toggle_btn = Gtk.ToggleButton()
        equalizer_icon = (
            Path(__file__).resolve().parents[4]
            / "icons/hicolor/symbolic/actions/big-audio-converter-equalizer-symbolic.svg"
        )
        equalizer_content = Gtk.Box(spacing=6)
        equalizer_content.append(
            Gtk.Image.new_from_gicon(
                Gio.FileIcon.new(Gio.File.new_for_path(str(equalizer_icon)))
            )
        )
        equalizer_content.append(Gtk.Label(label=_("Equalizer")))
        self.eq_toggle_btn.set_child(equalizer_content)
        self.eq_toggle_btn.add_css_class("flat")
        self.eq_toggle_btn.add_css_class("eq-icon-btn")
        self.eq_toggle_btn.set_active(False)
        self._eq_toggle_btn_handler = self.eq_toggle_btn.connect(
            "toggled", weak_callback(self._on_eq_toggle_clicked)
        )
        self.eq_toggle_btn.set_valign(Gtk.Align.CENTER)
        self.eq_toggle_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Equalizer")],
        )
        end_box.append(self.eq_toggle_btn)

        self.original_preview = Gtk.ToggleButton(label=_("Listen to original"))
        self.original_preview.set_tooltip_text(
            _("Temporarily listen without effects. Export settings stay unchanged.")
        )
        self.original_preview.connect(
            "toggled",
            lambda button: owner.player.set_effects_bypassed(
                button.get_active()
                or owner._format_list[owner.format_row.get_selected()] == "copy"
            ),
        )
        end_box.append(self.original_preview)

        self.audio_output_button = Gtk.MenuButton(
            icon_name="view-more-symbolic", css_classes=["flat"]
        )
        self.audio_output_button.set_tooltip_text(_("More playback options"))
        self.audio_output_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("More playback options")]
        )
        output_popover = Gtk.Popover()
        output_group = Adw.PreferencesGroup(
            title=_("Playback"),
            description=_("Only affects listening. Converted files stay unchanged."),
            width_request=320,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.audio_output_row = Adw.ComboRow(
            title=_("Audio output"), model=Gtk.StringList.new([_("System default")])
        )
        self._audio_device_names = ["auto"]
        self._audio_output_row_handler = self.audio_output_row.connect(
            "notify::selected", weak_callback(self._on_audio_output_changed)
        )
        output_group.add(self.auto_advance_switch)
        output_group.add(self.audio_output_row)
        refresh_outputs = Gtk.Button(label=_("Refresh audio outputs"), margin_top=6)
        refresh_outputs.connect(
            "clicked", lambda *_: owner.player.refresh_audio_devices()
        )
        output_group.add(refresh_outputs)
        output_popover.set_child(output_group)
        output_popover.connect(
            "notify::visible",
            lambda popover, *_: (
                owner.player.refresh_audio_devices() if popover.get_visible() else None
            ),
        )
        self.audio_output_button.set_popover(output_popover)
        self.player.devices_callback = weak_callback(self._refresh_audio_outputs)
        end_box.append(self.audio_output_button)

        # MIDDLE: Playback control buttons (centered)
        # Create a container for playback buttons
        playback_controls_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=4
        )

        # Previous audio button (left side)
        self.prev_audio_btn = Gtk.Button()
        self.prev_audio_btn.set_icon_name("media-skip-backward-symbolic")
        self.prev_audio_btn.add_css_class("flat")
        self.prev_audio_btn.add_css_class("circular")
        self.prev_audio_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Previous track")],
        )
        self.prev_audio_btn.connect(
            "clicked", lambda btn: owner._on_previous_audio_clicked()
        )
        self.prev_audio_btn.set_visible(False)  # Initially hidden
        playback_controls_box.append(self.prev_audio_btn)

        # Pause/Play button (center)
        self.pause_play_btn = Gtk.Button()
        self.pause_play_btn.set_icon_name("media-playback-start-symbolic")
        self.pause_play_btn.set_sensitive(False)  # until the queue has a file
        self.pause_play_btn.add_css_class("flat")
        self.pause_play_btn.add_css_class("circular")
        self.pause_play_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Play or pause")],
        )
        self.pause_play_btn.connect(
            "clicked", weak_callback(self._on_pause_play_clicked)
        )
        playback_controls_box.append(self.pause_play_btn)

        # Next audio button (right side)
        self.next_audio_btn = Gtk.Button()
        self.next_audio_btn.set_icon_name("media-skip-forward-symbolic")
        self.next_audio_btn.add_css_class("flat")
        self.next_audio_btn.add_css_class("circular")
        self.next_audio_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Next track")],
        )
        self.next_audio_btn.connect(
            "clicked", lambda btn: owner._on_next_audio_clicked()
        )
        self.next_audio_btn.set_visible(False)  # Initially hidden
        playback_controls_box.append(self.next_audio_btn)

        bar.set_center_widget(playback_controls_box)

        # The sliders open as the pointer reaches their control.
        self.volume_value_label = Gtk.Label(label="100%", css_classes=["caption"])
        self.volume_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.VERTICAL, 0.0, 100.0, 0.5
        )
        self.volume_scale.set_value(self._volume_to_slider(100.0))
        self.volume_scale.add_mark(
            self._volume_to_slider(100.0), Gtk.PositionType.RIGHT, None
        )
        self.volume_scale.connect(
            "value-changed", weak_callback(self._on_volume_scale_changed)
        )
        volume_box, self.volume_btn = self._bar_control(
            "volume",
            "audio-volume-high-symbolic",
            _("Volume"),
            None,
            self.volume_scale,
            self.volume_value_label,
            2,
        )
        start_box.append(volume_box)

        self.speed_value_label = Gtk.Label(label="1.00×", css_classes=["caption"])
        self.speed_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.VERTICAL, 0.0, 100.0, 0.5
        )
        self.speed_scale.set_value(self._speed_to_slider(1.0))
        self.speed_scale.add_mark(
            self._speed_to_slider(1.0), Gtk.PositionType.RIGHT, None
        )
        self.speed_scale.connect(
            "value-changed", weak_callback(self._on_speed_scale_changed)
        )
        speed_box, self.speed_btn = self._bar_control(
            "speed",
            "preferences-system-time-symbolic",
            _("Speed"),
            None,
            self.speed_scale,
            self.speed_value_label,
            2,
        )
        start_box.append(speed_box)

        voice_box, self.voice_btn = self._bar_control(
            "voice",
            "audio-input-microphone-symbolic",
            _("Clean voice"),
            _(
                "Highlights the voice and reduces other sounds. At 100%, it applies the model's maximum reduction."
            ),
            self.noise_scale,
            self.noise_value_label,
            5,
        )
        start_box.append(voice_box)

        self.zoom_value_label = Gtk.Label(label="1.0x", css_classes=["caption"])
        self.zoom_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.VERTICAL, 0.0, 150.0, 0.1
        )
        self.zoom_scale.set_value(0.0)
        self.zoom_scale.set_format_value_func(weak_callback(self._format_zoom_value))
        self._zoom_scale_handler = self.zoom_scale.connect(
            "value-changed", weak_callback(self._on_zoom_scale_changed)
        )
        self.zoom_box, self.zoom_btn = self._bar_control(
            "zoom",
            "system-search-symbolic",
            _("Waveform zoom"),
            None,
            self.zoom_scale,
            self.zoom_value_label,
            5,
        )
        end_box.append(self.zoom_box)
        # Settings were restored before these bar controls existed.
        self._set_processing_volume(self.volume_spin.get_value())
        self._set_processing_speed(self.speed_spin.get_value())

        self.visualizer_container = visualizer_container

        self.visualizer_frame = Gtk.Frame()
        self.visualizer_frame.set_margin_start(10)
        self.visualizer_frame.set_margin_end(10)
        self.visualizer_frame.set_margin_top(4)
        self.visualizer_frame.set_margin_bottom(0)

        # Set minimum height instead of fixed size
        self.visualizer_frame.set_size_request(-1, 100)  # Min height 100px

        # Connect seek handler before adding the visualizer to the frame
        self.visualizer.connect_seek_handler(self.on_visualizer_seek)

        self.visualizer.connect_marker_drag_handler(self._on_marker_drag_state_changed)

        self.visualizer.zoom_changed_callback = self._on_visualizer_zoom_changed
        # Panning while paused moves the window too; the seek bar follows it.
        self.visualizer.viewport_changed_callback = self._sync_seekbar_viewport

        self.visualizer.marker_updated_callback = self._on_markers_updated

        self.visualizer.set_content_height(100)

        self.visualizer.set_vexpand(True)  # Allow expansion
        self.visualizer.set_focusable(True)

        # Add the visualizer directly to the frame (no ScrolledWindow for performance).
        # Finding pauses sits on the waveform it marks and opens upward,
        # leaving the preview in view; the playback bar has no room left.
        self.pause_popover = PausePopover(self)
        waveform = Gtk.Overlay(child=self.visualizer)
        waveform.add_overlay(self.pause_popover.button)
        self.visualizer_frame.set_child(waveform)

        # Layout order: waveform (top) → seekbar (middle) → button bar (bottom)
        visualizer_container.append(self.visualizer_frame)

        self.seekbar = SeekBar()
        self.visualizer.duration_changed_callback = self.seekbar.set_duration
        self.seekbar.set_margin_start(4)
        self.seekbar.set_margin_end(4)
        self.seekbar.set_margin_bottom(0)
        self.seekbar.set_margin_top(0)
        self.seekbar.connect_seek_handler(self.on_visualizer_seek)

        self.visualizer.hover_time_callback = self.seekbar.set_waveform_hover_time

        visualizer_container.append(self.seekbar)

        visualizer_container.append(bar)

        self.vertical_paned.set_end_child(visualizer_container)
        self._fit_pending = False
        self.vertical_paned.connect(
            "notify::max-position", weak_callback(self._on_paned_allocated)
        )

        # Hide visualizer waveform initially (shown when files are added)
        # Seekbar and controls bar stay visible
        self.visualizer_frame.set_visible(False)

        # Editing tools appear with the first file, cut rows with a segment.
        self.segment_edit_button.set_visible(False)
        self.zoom_box.set_visible(False)
        self._update_cut_rows()
        self.seekbar.set_visible(True)

        # Apply tooltips to all UI elements (must be after all widgets are created)
        self._apply_tooltips()
        self._restore_conversion_settings()
        self.clipping_row.set_active(self._config_bool("prevent_clipping"))
        self._on_format_changed(self.format_row, None)

    def update_queue_size_label(self, count, text):
        """Show queue-wide controls for the current number of entries."""
        has_files = count > 0
        has_multiple_files = count > 1
        self.right_header.update_queue_label(text)
        self.right_header.set_queue_info_visible(has_multiple_files)
        self.prev_audio_btn.set_visible(has_multiple_files)
        self.next_audio_btn.set_visible(has_multiple_files)
        self.convert_button.set_visible(has_files)
        add_files = self.right_header.add_files_button
        (add_files.remove_css_class if has_files else add_files.add_css_class)(
            "suggested-action"
        )
        self.pause_play_btn.set_sensitive(has_files)
        self.join_row.set_visible(has_multiple_files)
        self._update_convert_label()

        self.segment_edit_button.set_visible(has_files)
        self.zoom_box.set_visible(has_files)
        self._set_waveform_visible(has_files)

    def _update_convert_label(self):
        count = len(self.file_queue.files)
        if count > 1 and self._joining():
            label = gettext.ngettext("Join %d file", "Join %d files", count)
        else:
            label = gettext.ngettext("Convert %d file", "Convert %d files", count)
        self.convert_button.set_label(label % count)

    def _set_waveform_visible(self, visible):
        if visible == self.visualizer_frame.get_visible():
            return
        self._remember_waveform_height()
        self.visualizer_frame.set_visible(visible)
        self._fit_bottom_panel()

    def _on_paned_allocated(self, *_args):
        if self._fit_pending:
            self._fit_pending = False
            self._sources.idle(self._fit_bottom_panel)

    def _remember_waveform_height(self):
        """Keep the height the person dragged the waveform to."""
        height = self.visualizer_frame.get_height()
        if self.visualizer_frame.get_visible() and height >= 100:
            self.visualizer_height = height
            self.app.config.set("visualizer_height", str(height))

    def _fit_bottom_panel(self):
        """Give the bottom panel its controls, plus the waveform while cutting."""
        paned = self.vertical_paned
        if not self.visualizer_frame.get_visible():
            # Clamped to the largest position that keeps the controls whole.
            paned.set_position(GLib.MAXINT32)
            return
        if paned.get_height() <= 0:
            # Not allocated yet: the paned's first allocation retries.
            self._fit_pending = True
            return
        # The measured minimum holds the waveform at its 100 px floor.
        panel = self.visualizer_container.measure(
            Gtk.Orientation.VERTICAL, paned.get_width()
        )[0]
        extra = self.visualizer_height - 100
        handle = (
            paned.get_height()
            - self.split_view.get_height()
            - self.visualizer_container.get_height()
        )
        paned.set_position(max(200, paned.get_height() - handle - panel - extra))

    def _refresh_audio_outputs(self):
        devices = [
            device for device in self.player.audio_devices if device["name"] != "auto"
        ]
        # Desktop audio servers expose the useful outputs without duplicate ALSA routes.
        for backend in ("pipewire/", "pulse/"):
            managed = [
                device for device in devices if device["name"].startswith(backend)
            ]
            if managed:
                devices = [
                    device
                    for device in devices
                    if device in managed or device["name"] == self.player.audio_device
                ]
                break
        self._audio_device_names = ["auto"] + [device["name"] for device in devices]
        self.audio_output_row.handler_block(self._audio_output_row_handler)
        self.audio_output_row.set_model(
            Gtk.StringList.new(
                [_("System default")] + [device["description"] for device in devices]
            )
        )
        self.audio_output_row.set_selected(
            self._audio_device_names.index(self.player.audio_device)
            if self.player.audio_device in self._audio_device_names
            else 0
        )
        self.audio_output_row.handler_unblock(self._audio_output_row_handler)

    def _on_audio_output_changed(self, row, *_args):
        index = row.get_selected()
        if index < len(self._audio_device_names) and not self.player.set_audio_device(
            self._audio_device_names[index]
        ):
            self._refresh_audio_outputs()

    def _apply_tooltips(self):
        """Register every help tooltip; the helper shows them only when enabled."""
        for widget, key in (
            (self.format_row, "format"),
            (self.bitrate_row, "bitrate"),
            (self.volume_spin, "volume"),
            (self.speed_spin, "speed"),
            (self.gate_row, "noise_gate"),
            (self.normalize_row, "normalize"),
            (self.cut_order_row, "cut"),
            (self.cut_output_row, "cut_output"),
            (self.channels_row, "channels"),
            (self.clear_queue_button, "clear_queue_button"),
            (self.prev_audio_btn, "prev_audio_btn"),
            (self.pause_play_btn, "pause_play_btn"),
            (self.next_audio_btn, "next_audio_btn"),
            (self.play_selection_switch, "play_selection_switch"),
            (self.eq_toggle_btn, "eq_toggle_btn"),
            (self.visualizer, "waveform_visualizer"),
        ):
            self.tooltip_helper.add_tooltip(widget, key)

    def _on_tips_action_changed(self, action, value):
        """Handle mouseover tips toggle from hamburger menu."""
        action.set_state(value)
        self.app.config.set(
            "show_mouseover_tips", "true" if value.get_boolean() else "false"
        )
        self.tooltip_helper.refresh()

    def on_convert(self, button):
        """Delegate job presentation to the conversion controller."""
        self.conversion.start()

    def _collect_conversion_settings(self):
        """Collect an immutable request before the worker starts."""
        self._save_current_file_state()
        settings = {
            "format": self._format_list[self.format_row.get_selected()],
            "bitrate": self._bitrate_list[self.bitrate_row.get_selected()],
            "volume": self.volume_spin.get_value() / 100,
            "speed": self.speed_spin.get_value(),
            # Channels: 0=original, 1=mono, 2=stereo
            "channels": (None, 1, 2)[self.channels_row.get_selected()],
            "sample_rate": self._sample_rate_list[self.sample_rate_row.get_selected()],
            "prevent_clipping": self.clipping_row.get_active(),
            "allow_precision_reduction": self.precision_row.get_active(),
            "output_directory": self.output_directory,
            "noise_reduction": self._noise_active(),
            "noise_engine": self._noise_engine(),
            "noise_strength": self.noise_scale.get_value(),
            # Each correction is off at zero and otherwise set by its slider.
            "gate_enabled": self.gate_row.scale.get_value() > 0,
            "gate_intensity": self.gate_row.scale.get_value() / 100,
            "compressor_enabled": self.compressor_row.scale.get_value() > 0,
            "compressor_intensity": self.compressor_row.scale.get_value() / 100,
            "hpf_enabled": self.hpf_row.scale.get_value() > 0,
            "hpf_frequency": max(int(self.hpf_row.scale.get_value()), 20),
            "eq_enabled": any(
                self.eq_panel.band_scales[f].get_value() != 0
                for _, f in self.eq_panel.BANDS
            ),
            "eq_bands": ",".join(
                str(self.eq_panel.band_scales[f].get_value())
                for _, f in self.eq_panel.BANDS
            ),
            "normalize": self.normalize_row.get_active(),
            "cut_merge": self.cut_output_row.get_selected() == 1,
            "cut_remove": self.removes_marked(),
            "join_files": self._joining(),
            # The converter orders each file's segments by number or by time.
            "order_by_segment_number": self.cut_order_row.get_selected() == 1,
            # Files without an entry here are converted in full.
            "file_markers": self.file_markers,
            # Pass track metadata from file queue for video track extraction
            "track_metadata": self.file_queue.track_metadata,
        }

        if settings["format"] == "copy":
            for key in (
                "noise_reduction",
                "hpf_enabled",
                "gate_enabled",
                "compressor_enabled",
                "eq_enabled",
                "normalize",
                "prevent_clipping",
            ):
                settings[key] = False
            settings.update(
                volume=1.0, speed=1.0, channels=None, sample_rate="original"
            )
        return deepcopy(settings)

    def _show_message(self, title, message):
        if self._closed:
            return
        dialog = Adw.AlertDialog(heading=title, body=message)
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def on_close_request(self, window):
        if not self._closed and self.converter.busy:
            dialog = Adw.AlertDialog(
                heading=_("Stop the conversion?"),
                body=_(
                    "Closing the window stops the conversion in progress. "
                    "Files that already finished are kept."
                ),
            )
            dialog.add_response("keep", _("Keep Converting"))
            dialog.add_response("stop", _("Stop and Close"))
            dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("keep")
            dialog.set_close_response("keep")
            dialog.connect("response", weak_callback(self._on_close_while_converting))
            dialog.present(self)
            return True
        self.cleanup()
        return False

    def _on_close_while_converting(self, _dialog, response):
        if response == "stop" and not self._closed:
            self.cleanup()
            self.close()

    def cleanup(self):
        """The same shutdown path is used by window close, Ctrl+Q, and app quit."""
        if self._closed:
            return
        self._closed = True
        self._dialog_cancellable.cancel()
        if not self.is_maximized():
            self._save_window_size()
        self._remember_waveform_height()
        self._sources.close()
        self._playback_sources.close()
        self.conversion.cleanup()
        self.waveform_generator.cancel()
        self.pause_popover.cleanup()
        self.converter.cancel_conversion()
        self.file_queue.cleanup()
        self.waveform_generator.cleanup()
        self.converter.cleanup()
        self.player.cleanup()
        self.visualizer.cleanup()
        self.tooltip_helper.cleanup()
        Gtk.StyleContext.remove_provider_for_display(
            self.get_display(), self._css_provider
        )
        for popover in self._bar_popovers.values():
            popover.unparent()
        self.seekbar.connect_seek_handler(None)
        self.file_queue._parent_window = None
        self.app.remove_action("toggle-tips")
        if self.app._main_window is self:
            self.app._main_window = None
        self.app.config.flush()

    def on_edit_segments(self, *_args):
        if not self.active_audio_id:
            self._show_message(
                _("No audio selected"),
                _("Add a file and select it before editing segments."),
            )
            return
        if (self.visualizer.duration or self.player.duration) <= 0:
            self._show_message(
                _("Audio duration unavailable"),
                _("Wait for the waveform to finish loading before editing this file."),
            )
            return
        editor = SegmentEditor(self)
        self.segment_editor = editor
        owner = weakref.proxy(self)
        editor.connect("closed", lambda dialog: setattr(owner, "segment_editor", None))
        editor.present(self)

    def _request_waveform(self, file_path):
        if self._closed or file_path not in self.file_queue.files:
            return
        self.pause_popover.sync()
        self.waveform_generator.request(
            file_path,
            self.converter,
            self.visualizer,
            self.file_markers,
            track_metadata=self.file_queue.track_metadata,
        )

    def _on_pending_changed(self, pending):
        if not self._closed:
            self.convert_button.set_sensitive(not pending and not self.converter.busy)

    def _on_probe_error(self, path, message):
        if self._closed:
            return
        self._probe_errors.append((os.path.basename(path), message))
        if len(self._probe_errors) > 100:
            self._probe_errors.pop(0)
        self._sources.later(150, self._show_probe_errors, key="probe-errors")

    def _show_probe_errors(self):
        if self._closed or not self._probe_errors:
            return
        body = "\n\n".join(
            name + "\n" + message for name, message in self._probe_errors[-5:]
        )
        body += "\n\n" + _(
            "Other valid files remain in the queue. Check these files and add them again."
        )
        if self._probe_error_dialog is None:
            self._probe_error_dialog = Adw.AlertDialog(
                heading=_("Some files could not be added"), body=body
            )
            self._probe_error_dialog.add_response("ok", _("OK"))
            self._probe_error_dialog.connect(
                "closed", weak_callback(self._probe_errors_closed)
            )
            self._probe_error_dialog.present(self)
        else:
            self._probe_error_dialog.set_body(body)

    def _probe_errors_closed(self, _dialog):
        self._probe_error_dialog = None
        self._probe_errors.clear()

    def _on_player_error(self, message):
        if not self._closed:
            self._show_message(_("Audio preview"), message)

    def _on_window_state_changed(self, window, param):
        """Save the maximized state and fix the layout after unmaximizing."""
        is_maximized = self.is_maximized()
        self.app.config.set("window_maximized", str(is_maximized).lower())
        if not is_maximized:
            # A small delay lets the window settle before measuring it.
            self._sources.later(200, self._restore_geometry)

    def _save_window_size(self):
        width = self.get_width()
        height = self.get_height()
        # Only save if the values are reasonable
        if width > 200 and height > 200:
            self.app.config.set("window_width", str(width))
            self.app.config.set("window_height", str(height))

    def on_window_mapped(self, widget):
        """Restore geometry once the window has its first allocation."""
        self._sources.idle(self._restore_geometry)

    def _restore_geometry(self):
        """Restore the visualizer height after the window is shown."""
        self._fit_bottom_panel()

    def _confirm_reset_settings(self):
        dialog = Adw.AlertDialog(
            heading=_("Reset all settings?"),
            body=_(
                "Format, quality, output folder, cutting, effects and equalizer "
                "return to their defaults. The queue and your source files are kept."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reset", _("Reset"))
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", weak_callback(self._on_reset_settings_response))
        dialog.present(self)

    def _on_reset_settings_response(self, _dialog, response):
        if response == "reset" and not self._closed:
            self.reset_settings()

    def on_clear_queue(self, button):
        dialog = Adw.AlertDialog(
            heading=_("Clear Queue"),
            body=_(
                "Remove the queue entries and their edits? Source files will not be deleted."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("clear", _("Clear Queue"))
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", weak_callback(self._on_clear_queue_response))
        dialog.present(self)

    def _on_clear_queue_response(self, dialog, response):
        """Handle clear queue dialog response."""
        if not self._closed and response == "clear":
            # Stop playback if currently playing
            if self.player and self.player.is_playing():
                self.player.stop()

            self.visualizer.clear_waveform()

            self.visualizer.clear_all_markers()

            self.file_queue.clear_queue()

    def setup_drop_target(self):
        """Files dropped anywhere outside the queue join it too."""
        owner = weakref.proxy(self)
        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect(
            "drop", lambda _target, value, _x, _y: owner.file_queue.add_dropped(value)
        )
        self.add_controller(drop_target)

    def on_add_files(self, button):
        """Handle adding files through dialog."""
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select Audio or Video Files"))

        media_filter = Gtk.FileFilter()
        media_filter.set_name(_("Audio and Video files"))

        audio_extensions = [
            "mp3",
            "wav",
            "ogg",
            "flac",
            "m4a",
            "aac",
            "opus",
            "wma",
            "aiff",
            "ape",
            "alac",
            "dsd",
            "dsf",
            "mka",
            "oga",
            "spx",
            "tta",
            "wv",
        ]

        video_extensions = [
            "mp4",
            "mkv",
            "avi",
            "mov",
            "wmv",
            "flv",
            "webm",
            "m4v",
            "mpg",
            "mpeg",
            "3gp",
            "ogv",
            "ts",
            "mts",
            "m2ts",
        ]

        for ext in audio_extensions + video_extensions:
            media_filter.add_suffix(ext)
            media_filter.add_suffix(ext.upper())  # Also add uppercase versions

        all_filter = Gtk.FileFilter()
        all_filter.set_name(_("All files"))
        all_filter.add_pattern("*")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(media_filter)
        filters.append(all_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(media_filter)

        # Open the dialog with multiple file selection
        dialog.open_multiple(
            parent=self,
            cancellable=self._dialog_cancellable,
            callback=self._on_open_files_complete,
        )

    def _on_open_files_complete(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
            if self._closed or files is None:
                return
            paths = [file.get_path() for file in files if file.get_path()]
            self.file_queue.add_files(paths)
            if len(paths) != files.get_n_items():
                self._show_message(
                    _("Local files only"),
                    _(
                        "Some selected files are remote. Download them before adding them."
                    ),
                )
        except GLib.Error as error:
            if (
                not self._closed
                and not error.matches(
                    Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED
                )
                and not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)
            ):
                self._show_message(_("Files could not be selected"), error.message)

    def on_file_added_to_empty_queue(self, file_path, index):
        """Show the first file added to an empty queue without playing it."""
        if self.visualizer.waveform_data is None:
            self._prepare_visualizer_for_new_file(file_path, index)
