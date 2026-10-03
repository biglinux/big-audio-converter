# app/ui/main_window.py

"""
Main Window for the Audio Converter application.
"""

import gettext
import weakref

import gi

gettext.textdomain("big-audio-converter")
_ = gettext.gettext
import logging
import os
from copy import deepcopy
from pathlib import Path

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from app.audio.waveform import WaveformGenerator
from app.ui.controls_bar_mixin import ControlsBarMixin
from app.ui.conversion_controller import ConversionController
from app.ui.equalizer_panel import EqualizerPanel
from app.ui.file_queue import FileQueue
from app.ui.playback_controller import PlaybackControllerMixin
from app.ui.segment_editor import SegmentEditor
from app.ui.settings_mixin import SettingsManagerMixin
from app.ui.visualizer import AudioVisualizer, SeekBar
from app.utils.main_loop import MainLoopSources, weak_callback
from app.utils.tooltip_helper import TooltipHelper

logger = logging.getLogger(__name__)


class HeaderBar(Gtk.Box):
    """
    Custom header bar with action buttons for file management and conversion.
    Encapsulates layout logic to ensure proper resizing behavior.
    """

    def __init__(self, main_window, window_buttons_left=False):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.main_window = weakref.proxy(main_window)
        self.window_buttons_left = window_buttons_left

        # Ensure the wrapper box occupies full width
        self.set_hexpand(True)

        # Create the Adw.HeaderBar
        self.header_bar = Adw.HeaderBar()
        # Ensure the inner HeaderBar occupies full width
        self.header_bar.set_hexpand(True)
        self.header_bar.set_show_title(True)

        # Configure decoration layout based on window button position
        if not window_buttons_left:
            self.header_bar.set_decoration_layout(":minimize,maximize,close")
        else:
            self.header_bar.set_decoration_layout("")

        # Add the HeaderBar to this box
        self.append(self.header_bar)

        # Create UI elements
        self._create_ui_elements()

    def _create_ui_elements(self):
        # Always create queue controls
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

        # Create menu button
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

        # Create stateful action for tooltip toggle
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

        # Organize right-side elements
        if self.window_buttons_left:
            icon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            icon_box.set_halign(Gtk.Align.END)
            icon_box.set_valign(Gtk.Align.CENTER)
            icon_box.append(menu_button)
            app_icon = Gtk.Image.new_from_icon_name("big-audio-converter")
            app_icon.set_pixel_size(20)
            app_icon.set_halign(Gtk.Align.END)
            app_icon.set_valign(Gtk.Align.CENTER)
            icon_box.append(app_icon)
            self.header_bar.pack_end(icon_box)
        else:
            self.header_bar.pack_end(menu_button)

        # Left section for queue controls
        left_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        left_controls.set_margin_start(0)
        left_controls.set_halign(Gtk.Align.START)
        left_controls.append(self.clear_queue_button)
        left_controls.append(self.queue_size_label)

        self.header_bar.pack_start(left_controls)

        # Center section for buttons
        center_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        center_box.set_halign(Gtk.Align.CENTER)

        # Add Files button
        add_files_button = Gtk.Button(label=_("Add Files"))
        add_files_button.connect(
            "clicked", weak_callback(self.main_window.on_add_files)
        )
        add_files_button.add_css_class("suggested-action")
        center_box.append(add_files_button)

        # Convert button
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

        # Initialize with loaded or default size
        super().__init__(
            title=_("Audio Converter"),
            default_width=default_width,
            default_height=default_height,
            **kwargs,
        )

        self.add_css_class("big-audio-converter")

        # Store whether window should be maximized
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

        # Initialize components
        self.player = self.app.player
        self.converter = self.app.converter
        # Add marker cache to remember markers for each file
        self.file_markers = {}  # Dictionary mapping file path to marker pairs
        self.active_audio_id = None  # Queue entry whose waveform is shown

        # Selection playback tracking
        self._playing_selection = False
        self._selection_segments = []  # List of (start, stop) tuples
        self._current_segment_index = 0
        self._play_selection_mode = False  # Whether switch is on
        self._marker_dragging = False  # Track when user is dragging markers
        self._is_transitioning_segment = (
            False  # Lock to prevent transition race conditions
        )

        # Default visualizer height - will be overridden by saved value
        self.visualizer_height = 132

        saved_height = config.get("visualizer_height")
        if saved_height:
            try:
                self.visualizer_height = max(100, int(saved_height))
            except (ValueError, TypeError):
                pass

        # Set up GUI first, creating the visualizer
        self.setup_ui()
        self.conversion = ConversionController(self)
        self.file_queue.on_probe_error = self._on_probe_error
        self.file_queue.on_pending_changed = self._on_pending_changed
        self.setup_drop_target()
        self.connect("close-request", weak_callback(self.on_close_request))

        # Connect to map event for visualizer height restoration
        self.connect("map", weak_callback(self.on_window_mapped))

        # Connect window state signals (only maximized, save size on close only)
        self.connect("notify::maximized", weak_callback(self._on_window_state_changed))

        # Connect player signals to UI
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

        # Connect file removal signal
        self.file_queue.connect_file_removed_signal(self._on_file_removed)

        # Setup window-level keyboard shortcuts
        self._setup_keyboard_shortcuts()

    def _setup_keyboard_shortcuts(self):
        """Register window-level keyboard shortcuts."""
        # Create window-level actions
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

        # Set accelerators at application level
        app = self.get_application()
        app.set_accels_for_action("win.add-files", ["<Control>o"])
        app.set_accels_for_action("win.convert", ["<Control>Return"])
        app.set_accels_for_action("win.play-pause", ["<Control>space"])
        app.set_accels_for_action("win.edit-segments", ["<Control>e"])

    def setup_ui(self):
        """Set up the user interface."""
        # Create main vertical paned container (root content)
        owner = weakref.proxy(self)
        self.vertical_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(self.vertical_paned)
        self.vertical_paned.set_vexpand(True)

        # Prevent bottom controls from being cut off when window is resized
        self.vertical_paned.set_shrink_end_child(False)
        self.vertical_paned.set_resize_start_child(True)
        self.vertical_paned.set_resize_end_child(False)

        self.split_view = Adw.OverlaySplitView(vexpand=True)
        self.split_view.set_sidebar_position(Gtk.PackType.START)
        self.split_view.set_min_sidebar_width(280)
        # Wide enough that row subtitles keep to one line; the sidebar no longer
        # has a drag handle, so the legacy sidebar_width setting is not read.
        self.split_view.set_max_sidebar_width(400)
        self.split_view.set_sidebar_width_fraction(0.34)
        # Add split_view directly to vertical_paned (top part)
        self.vertical_paned.set_start_child(self.split_view)

        # Create CSS for sidebar styling
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
        .playback-controls button:checked {
            background-color: alpha(@accent_bg_color, 0.55);
            color: #ffffff;
        }
        .queue-pane > revealer > windowhandle > headerbar,
        .queue-pane headerbar { background: none; box-shadow: none; }
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

        # RIGHT SIDE - Now contains file queue (previously on left)
        right_box = Adw.ToolbarView(css_classes=["queue-pane"])
        # Set minimum width for right content area
        right_box.set_size_request(320, -1)

        # Create header bar for right side using the dedicated class
        # This handles proper layout behavior and resizing
        self.right_header = HeaderBar(self, False)
        right_box.add_top_bar(self.right_header)

        self.clear_queue_button = self.right_header.clear_queue_button
        self.convert_button = self.right_header.convert_button

        # Create scrollable container for right content (file queue)
        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        right_scroll.set_vexpand(True)

        # Create right content container for file queue
        right_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right_content.set_margin_start(10)
        right_content.set_margin_end(10)
        right_content.set_margin_bottom(10)

        # Add the file queue directly to right content (removed queue_controls container)
        self.file_queue = FileQueue(self.converter)
        self.file_queue._parent_window = self  # Set parent window for dialogs
        self.file_queue._tooltip_helper = self.tooltip_helper  # Pass tooltip helper
        right_content.append(self.file_queue)

        # Connect queue size change handler
        self.file_queue.on_queue_size_changed = self.update_queue_size_label

        # Connect play callback for file queue
        self.file_queue.on_play_file = weak_callback(self.on_play_file)

        # Connect stop playback callback to player.stop method
        self.file_queue.on_stop_playback = self.player.stop

        # Connect callback for when file is added to empty queue
        self.file_queue.on_file_added_to_empty_queue = self.on_file_added_to_empty_queue

        # Connect callback for when file row is activated (clicked)
        self.file_queue.on_activate_file = self.on_activate_file

        # Add right content to scroll container
        right_scroll.set_child(right_content)

        # Set the right content
        right_box.set_content(right_scroll)

        # Add inline equalizer panel with Revealer at the bottom of right_box
        self.eq_panel = EqualizerPanel(self.player)
        self.eq_revealer = Gtk.Revealer()
        self.eq_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.eq_revealer.set_transition_duration(200)
        self.eq_revealer.set_reveal_child(False)
        self.eq_revealer.set_child(self.eq_panel)
        self.eq_revealer.connect(
            "notify::reveal-child", weak_callback(self._on_eq_revealer_changed)
        )
        right_box.add_bottom_bar(self.eq_revealer)

        # Add the two views to the paned container (swapped order)
        self.split_view.set_sidebar(left_box)
        self.split_view.set_content(right_box)

        # Add visualizer at the bottom spanning full width
        # Create the visualizer instance
        self.visualizer = AudioVisualizer()

        # Give visualizer reference to player for checking playback state
        self.visualizer.player = self.player

        # Create container for visualizer and zoom controls
        visualizer_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        visualizer_container.add_css_class("playback-panel")

        # Create zoom control bar
        zoom_control_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        zoom_control_box.set_margin_start(0)
        zoom_control_box.set_margin_end(0)
        zoom_control_box.add_css_class("playback-controls")

        # Add "Only the Selected Area" toggle button — icon-only, like play controls
        self.play_selection_switch = Gtk.ToggleButton()
        self.play_selection_switch.set_icon_name("selection-mode-symbolic")
        self.play_selection_switch.add_css_class("flat")
        self.play_selection_switch.add_css_class("circular")
        self.play_selection_switch.set_active(False)
        self.play_selection_switch.connect(
            "toggled", weak_callback(self._on_play_selection_switch_toggled)
        )
        self.play_selection_switch.set_valign(Gtk.Align.CENTER)
        self.play_selection_switch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Play only selected area")],
        )

        zoom_control_box.append(self.play_selection_switch)

        # Next to the waveform it edits; cutting has to be on to use it.
        self.segment_edit_button = Gtk.Button(label=_("Edit Segments…"))
        self.segment_edit_button.add_css_class("flat")
        self.segment_edit_button.set_valign(Gtk.Align.CENTER)
        self.segment_edit_button.connect(
            "clicked", weak_callback(self.on_edit_segments)
        )
        zoom_control_box.append(self.segment_edit_button)

        # Add "Auto-Advance" toggle button — icon-only
        self.auto_advance_switch = Gtk.ToggleButton()
        self.auto_advance_switch.set_icon_name("media-playlist-consecutive-symbolic")
        self.auto_advance_switch.add_css_class("flat")
        self.auto_advance_switch.add_css_class("circular")
        auto_advance_enabled = self.app.config.get("auto_advance_enabled", True)
        self.auto_advance_switch.set_active(auto_advance_enabled)
        self.auto_advance_switch.connect(
            "toggled", weak_callback(self._on_auto_advance_switch_toggled)
        )
        self.auto_advance_switch.set_valign(Gtk.Align.CENTER)
        self.auto_advance_switch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Auto-advance to next track")],
        )

        zoom_control_box.append(self.auto_advance_switch)

        # Add Equalizer toggle button — icon-only
        self.eq_toggle_btn = Gtk.ToggleButton()
        equalizer_icon = (
            Path(__file__).resolve().parents[4]
            / "icons/hicolor/symbolic/actions/equalizer-symbolic.svg"
        )
        self.eq_toggle_btn.set_child(
            Gtk.Image.new_from_gicon(
                Gio.FileIcon.new(Gio.File.new_for_path(str(equalizer_icon)))
            )
        )
        self.eq_toggle_btn.add_css_class("flat")
        self.eq_toggle_btn.add_css_class("circular")
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
        zoom_control_box.append(self.eq_toggle_btn)

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
        zoom_control_box.append(self.original_preview)

        self.audio_output_button = Gtk.MenuButton(icon_name="audio-headphones-symbolic")
        self.audio_output_button.set_tooltip_text(_("Audio output"))
        self.audio_output_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Audio output")]
        )
        output_popover = Gtk.Popover()
        output_group = Adw.PreferencesGroup(
            title=_("Listen through"),
            description=_("Only affects playback. Converted files stay unchanged."),
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
        zoom_control_box.append(self.audio_output_button)

        # MIDDLE: Playback control buttons (centered)
        # Create a container for playback buttons
        playback_controls_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=4
        )
        playback_controls_box.set_halign(Gtk.Align.CENTER)
        playback_controls_box.set_hexpand(True)

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

        # Add the centered playback controls to the main box
        zoom_control_box.append(playback_controls_box)

        # RIGHT: Volume, Speed, and Zoom controls with popover sliders
        right_controls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        right_controls_box.set_halign(Gtk.Align.END)

        # --- Volume button with popover ---
        vol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.volume_value_label = Gtk.Label(label="100")
        self.volume_value_label.add_css_class("caption")

        self.volume_btn = Gtk.Button()
        self.volume_btn.set_icon_name("audio-volume-high-symbolic")
        self.volume_btn.add_css_class("flat")
        self.volume_btn.add_css_class("circular")
        self.volume_btn.set_tooltip_text(_("Volume"))
        self.volume_btn.update_property([Gtk.AccessibleProperty.LABEL], [_("Volume")])

        self.volume_popover = Gtk.Popover()
        self.volume_popover.set_parent(self.volume_btn)
        self.volume_popover.set_position(Gtk.PositionType.TOP)

        vol_popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vol_popover_box.set_margin_start(8)
        vol_popover_box.set_margin_end(8)
        vol_popover_box.set_margin_top(8)
        vol_popover_box.set_margin_bottom(8)

        vol_title = Gtk.Label(label=_("Volume"))
        vol_title.add_css_class("caption")
        vol_popover_box.append(vol_title)

        self.volume_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.VERTICAL, 0.0, 100.0, 0.5
        )
        self.volume_scale.set_inverted(True)
        self.volume_scale.set_value(self._volume_to_slider(100.0))
        self.volume_scale.set_draw_value(False)
        self.volume_scale.set_size_request(-1, 300)
        self.volume_scale.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Volume level")],
        )
        self.volume_scale.add_mark(
            self._volume_to_slider(100.0), Gtk.PositionType.RIGHT, None
        )
        self.volume_scale.connect(
            "value-changed", weak_callback(self._on_volume_scale_changed)
        )
        vol_popover_box.append(self.volume_scale)

        self.volume_popover.set_child(vol_popover_box)

        self.volume_btn.connect("clicked", weak_callback(self._on_volume_btn_clicked))

        vol_box.append(self.volume_btn)
        vol_box.append(self.volume_value_label)
        right_controls_box.append(vol_box)

        # --- Speed button with popover ---
        speed_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.speed_value_label = Gtk.Label(label="1.00x")
        self.speed_value_label.add_css_class("caption")

        self.speed_btn = Gtk.Button()
        self.speed_btn.set_icon_name("preferences-system-time-symbolic")
        self.speed_btn.add_css_class("flat")
        self.speed_btn.add_css_class("circular")
        self.speed_btn.set_tooltip_text(_("Playback speed"))
        self.speed_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Playback speed")]
        )

        self.speed_popover = Gtk.Popover()
        self.speed_popover.set_parent(self.speed_btn)
        self.speed_popover.set_position(Gtk.PositionType.TOP)

        spd_popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        spd_popover_box.set_margin_start(8)
        spd_popover_box.set_margin_end(8)
        spd_popover_box.set_margin_top(8)
        spd_popover_box.set_margin_bottom(8)

        spd_title = Gtk.Label(label=_("Speed"))
        spd_title.add_css_class("caption")
        spd_popover_box.append(spd_title)

        self.speed_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.VERTICAL, 0.0, 100.0, 0.5
        )
        self.speed_scale.set_inverted(True)
        self.speed_scale.set_value(self._speed_to_slider(1.0))
        self.speed_scale.set_draw_value(False)
        self.speed_scale.set_size_request(-1, 300)
        self.speed_scale.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Playback speed level")],
        )
        self.speed_scale.add_mark(
            self._speed_to_slider(1.0), Gtk.PositionType.RIGHT, None
        )
        self.speed_scale.connect(
            "value-changed", weak_callback(self._on_speed_scale_changed)
        )
        spd_popover_box.append(self.speed_scale)

        self.speed_popover.set_child(spd_popover_box)

        self.speed_btn.connect("clicked", weak_callback(self._on_speed_btn_clicked))

        speed_box.append(self.speed_btn)
        speed_box.append(self.speed_value_label)
        right_controls_box.append(speed_box)

        # --- Zoom button with popover ---
        self.zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)

        # Zoom value label (shows current zoom level)
        self.zoom_value_label = Gtk.Label(label="1.0x")
        self.zoom_value_label.add_css_class("caption")

        # Zoom button (opens popover with vertical slider)
        self.zoom_btn = Gtk.Button()
        self.zoom_btn.set_icon_name("system-search-symbolic")
        self.zoom_btn.add_css_class("flat")
        self.zoom_btn.add_css_class("circular")
        self.zoom_btn.set_tooltip_text(_("Waveform zoom"))
        self.zoom_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Waveform zoom")]
        )

        # Popover with vertical slider
        self.zoom_popover = Gtk.Popover()
        self.zoom_popover.set_parent(self.zoom_btn)
        self.zoom_popover.set_position(Gtk.PositionType.TOP)

        # Vertical slider inside popover
        popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        popover_box.set_margin_start(8)
        popover_box.set_margin_end(8)
        popover_box.set_margin_top(8)
        popover_box.set_margin_bottom(8)

        zoom_title = Gtk.Label(label=_("Zoom"))
        zoom_title.add_css_class("caption")
        popover_box.append(zoom_title)

        self.zoom_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.VERTICAL, 0.0, 150.0, 0.1
        )
        self.zoom_scale.set_inverted(True)  # Higher values at top
        self.zoom_scale.set_value(0.0)
        self.zoom_scale.set_draw_value(False)
        self.zoom_scale.set_size_request(-1, 300)
        self.zoom_scale.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Waveform zoom level")],
        )

        self.zoom_scale.set_format_value_func(weak_callback(self._format_zoom_value))
        self._zoom_scale_handler = self.zoom_scale.connect(
            "value-changed", weak_callback(self._on_zoom_scale_changed)
        )
        popover_box.append(self.zoom_scale)

        self.zoom_popover.set_child(popover_box)

        self.zoom_btn.connect("clicked", weak_callback(self._on_zoom_btn_clicked))

        self.zoom_box.append(self.zoom_btn)
        self.zoom_box.append(self.zoom_value_label)
        right_controls_box.append(self.zoom_box)

        zoom_control_box.append(right_controls_box)
        # Settings were restored before these bar controls existed.
        self._set_processing_volume(self.volume_spin.get_value())
        self._set_processing_speed(self.speed_spin.get_value())

        # Store reference to visualizer container
        self.visualizer_container = visualizer_container

        # Create a frame around the visualizer for better appearance
        self.visualizer_frame = Gtk.Frame()
        self.visualizer_frame.set_margin_start(10)
        self.visualizer_frame.set_margin_end(10)
        self.visualizer_frame.set_margin_top(4)
        self.visualizer_frame.set_margin_bottom(0)

        # Set minimum height instead of fixed size
        self.visualizer_frame.set_size_request(-1, 100)  # Min height 100px

        # Connect seek handler before adding the visualizer to the frame
        self.visualizer.connect_seek_handler(self.on_visualizer_seek)

        # Connect marker drag handler to track when markers are being dragged
        self.visualizer.connect_marker_drag_handler(self._on_marker_drag_state_changed)

        # Connect zoom change handler to update slider
        self.visualizer.zoom_changed_callback = self._on_visualizer_zoom_changed
        # Panning while paused moves the window too; the seek bar follows it.
        self.visualizer.viewport_changed_callback = self._sync_seekbar_viewport

        # Connect marker update handler to refresh selection playback
        self.visualizer.marker_updated_callback = self._on_markers_updated

        # Set a modest minimum height; actual size is determined by GTK Box layout with vexpand
        self.visualizer.set_content_height(100)

        # Make sure the visualizer can receive mouse events and expand with parent
        self.visualizer.set_vexpand(True)  # Allow expansion
        self.visualizer.set_focusable(True)

        # Add the visualizer directly to the frame (no ScrolledWindow for performance)
        self.visualizer_frame.set_child(self.visualizer)

        # Layout order: waveform (top) → seekbar (middle) → button bar (bottom)
        visualizer_container.append(self.visualizer_frame)

        # Create the dedicated seekbar between waveform and controls
        self.seekbar = SeekBar()
        self.visualizer.duration_changed_callback = self.seekbar.set_duration
        self.seekbar.set_margin_start(4)
        self.seekbar.set_margin_end(4)
        self.seekbar.set_margin_bottom(0)
        self.seekbar.set_margin_top(0)
        self.seekbar.connect_seek_handler(self.on_visualizer_seek)
        self._set_copy_mode_ui(
            self._format_list[self.format_row.get_selected()] == "copy"
        )

        # Connect waveform hover to seekbar display
        self.visualizer.hover_time_callback = self.seekbar.set_waveform_hover_time

        visualizer_container.append(self.seekbar)

        visualizer_container.append(zoom_control_box)

        # Add visualizer container to the bottom part of the vertical paned
        self.vertical_paned.set_end_child(visualizer_container)
        self._fit_pending = False
        self.vertical_paned.connect(
            "notify::max-position", weak_callback(self._on_paned_allocated)
        )

        # Hide visualizer waveform initially (shown when files are added)
        # Seekbar and controls bar stay visible
        self.visualizer_frame.set_visible(False)

        # Set initial visibility for cut-mode-dependent elements
        cut_enabled = self.cut_row.get_selected() > 0
        self.play_selection_switch.set_visible(cut_enabled)
        self.segment_edit_button.set_visible(cut_enabled)
        self.zoom_box.set_visible(cut_enabled)
        self.seekbar.set_visible(True)

        # Apply tooltips to all UI elements (must be after all widgets are created)
        self._apply_tooltips()

    def update_queue_size_label(self, count, text):
        """Show queue-wide controls for the current number of entries."""
        has_files = count > 0
        has_multiple_files = count > 1
        self.right_header.update_queue_label(text)
        self.right_header.set_queue_info_visible(has_multiple_files)
        self.prev_audio_btn.set_visible(has_multiple_files)
        self.next_audio_btn.set_visible(has_multiple_files)
        self.convert_button.set_visible(has_files)
        self.pause_play_btn.set_sensitive(has_files)
        self.convert_button.set_label(
            gettext.ngettext("Convert %d file", "Convert %d files", count) % count
        )

        # Show the waveform only while cutting; seekbar and controls stay visible.
        self._set_waveform_visible(has_files and self.cut_row.get_selected() > 0)

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
            (self.noise_row, "noise_reduction"),
            (self.gate_row, "noise_gate"),
            (self.normalize_row, "normalize"),
            (self.cut_row, "cut"),
            (self.cut_output_row, "cut_output"),
            (self.channels_row, "channels"),
            (self.clear_queue_button, "clear_queue_button"),
            (self.prev_audio_btn, "prev_audio_btn"),
            (self.pause_play_btn, "pause_play_btn"),
            (self.next_audio_btn, "next_audio_btn"),
            (self.play_selection_switch, "play_selection_switch"),
            (self.auto_advance_switch, "auto_advance_switch"),
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
            "noise_reduction": self.noise_row.get_active(),
            "noise_engine": self.noise_engines[self.noise_model_row.get_selected()],
            "noise_strength": self.noise_strength_scale.get_value(),
            "gate_enabled": self.gate_row.get_active(),
            "gate_intensity": self.gate_intensity_scale.get_value(),
            "compressor_enabled": self.compressor_row.get_active(),
            "compressor_intensity": self.compressor_intensity_scale.get_value(),
            "hpf_enabled": self.hpf_row.get_active(),
            "hpf_frequency": int(self.hpf_freq_scale.get_value()),
            "eq_enabled": any(
                self.eq_panel.band_scales[f].get_value() != 0
                for _, f in self.eq_panel.BANDS
            ),
            "eq_bands": ",".join(
                str(self.eq_panel.band_scales[f].get_value())
                for _, f in self.eq_panel.BANDS
            ),
            "normalize": self.normalize_row.get_active(),
            "cut_enabled": self.cut_row.get_selected() > 0,
            "cut_merge": self.cut_output_row.get_selected() == 1,
            # The converter orders each file's segments by number or by time.
            "order_by_segment_number": self.cut_row.get_selected() == 2,
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
        self.cleanup()
        return False

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
        for popover in (self.volume_popover, self.speed_popover, self.zoom_popover):
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
                _(
                    "Enable cutting and wait for the waveform to finish before editing this file."
                ),
            )
            return
        editor = SegmentEditor(self)
        self.segment_editor = editor
        owner = weakref.proxy(self)
        editor.connect("closed", lambda dialog: setattr(owner, "segment_editor", None))
        editor.present(self)

    def _request_waveform(self, file_path, enabled=None):
        if self._closed or file_path not in self.file_queue.files:
            return
        if enabled is None:
            enabled = self.cut_row.get_selected() > 0 and self.waveform_row.get_active()
        self.waveform_generator.request(
            file_path,
            self.converter,
            self.visualizer,
            self.file_markers,
            track_metadata=self.file_queue.track_metadata,
            enabled=enabled,
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
        self.visualizer.set_markers_enabled(self.cut_row.get_selected() > 0)

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
                logger.info("Stopping playback before clearing queue")
                self.player.stop()

            # Clear the waveform visualization
            logger.info("Clearing waveform visualization")
            self.visualizer.clear_waveform()

            # Clear all markers
            self.visualizer.clear_all_markers()

            # Clear the file queue
            self.file_queue.clear_queue()

    def setup_drop_target(self):
        """Set up drag and drop support for files."""
        drop_target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop_target.connect("drop", weak_callback(self.on_drop))
        self.add_controller(drop_target)

    def on_drop(self, target, value, x, y):
        """Handle file drop events."""
        if isinstance(value, Gio.File):
            path = value.get_path()
            # Just add file without generating waveform
            self.file_queue.add_file(path)
            return True
        return False

    def on_add_files(self, button):
        """Handle adding files through dialog."""
        # Create the file dialog with native dialog support
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select Audio or Video Files"))

        # Create file filters
        media_filter = Gtk.FileFilter()
        media_filter.set_name(_("Audio and Video files"))

        # Add common audio file extensions - make them lowercase to ensure matching
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

        # Add common video file extensions (can be converted to audio)
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

        # Add all extensions to the filter
        for ext in audio_extensions + video_extensions:
            media_filter.add_suffix(ext)
            media_filter.add_suffix(ext.upper())  # Also add uppercase versions

        # Add all files filter
        all_filter = Gtk.FileFilter()
        all_filter.set_name(_("All files"))
        all_filter.add_pattern("*")

        # Add filters to the dialog
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
        """Handle file added to empty queue - generate waveform but don't play."""
        logger.info(
            f"on_file_added_to_empty_queue called for {file_path}, waveform_data is None: {self.visualizer.waveform_data is None}"
        )

        # Check if visualizer is currently empty
        if self.visualizer.waveform_data is None:
            logger.info(
                f"Waveform is empty, generating for first file without playing: {file_path}"
            )
            self._prepare_visualizer_for_new_file(file_path, index)
        else:
            logger.info(f"Waveform is not empty, skipping generation for {file_path}")

        return False  # For GLib.idle_add
