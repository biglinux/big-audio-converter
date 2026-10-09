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
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from app.audio.waveform import WaveformGenerator
from app.ui.conversion_controller import ConversionController
from app.ui.effect_cards import EffectCards
from app.ui.equalizer_panel import EqualizerPanel
from app.ui.file_queue import FileQueue
from app.ui.hover_popovers import HoverPopovers
from app.ui.playback_controller import PlaybackControllerMixin
from app.ui.segment_editor import SegmentEditor
from app.ui.settings_mixin import COMPRESSOR_STEPS, SettingsManagerMixin
from app.ui.visualizer import AudioVisualizer, SeekBar
from app.utils.main_loop import MainLoopSources, weak_callback
from app.utils.time_formatter import format_time_display
from app.utils.tooltip_helper import TooltipHelper

_ = gettext.gettext
logger = logging.getLogger(__name__)


class MainWindow(
    SettingsManagerMixin,
    PlaybackControllerMixin,
    Adw.ApplicationWindow,
):
    """Main application window."""

    def __init__(self, **kwargs):
        default_width = 1100
        default_height = 800
        config = kwargs["application"].config

        saved_width = config.get("window_width")
        if saved_width:
            try:
                default_width = int(saved_width)
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

        # The narrowest layout: the sidebar over the queue, the playback
        # controls on two lines.
        self.set_size_request(440, 480)

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

        cancel = Gio.SimpleAction.new("cancel-conversion", None)
        cancel.connect("activate", lambda *_: owner.conversion.cancel())
        self.add_action(cancel)

        clear = Gio.SimpleAction.new("clear-queue", None)
        clear.set_enabled(False)
        clear.connect("activate", lambda *_: owner.on_clear_queue())
        self.add_action(clear)

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
        # Shown over the whole window while files are dragged in.
        self.drop_overlay = Adw.StatusPage(
            icon_name="list-add-symbolic",
            title=_("Drop to add"),
            css_classes=["drop-overlay"],
            can_target=False,
            visible=False,
        )
        self.toast_overlay = Adw.ToastOverlay(
            child=Gtk.Overlay(child=self.vertical_paned)
        )
        self.toast_overlay.get_child().add_overlay(self.drop_overlay)
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

        # Left: what the files become.
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_vexpand(True)
        left_scroll.add_css_class("sidebar")

        left_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_bottom=12)
        left_scroll.set_child(left_content)
        self.effect_cards = EffectCards(left_scroll)
        # The pause panel in the sidebar previews on the waveform.
        self.visualizer = AudioVisualizer()
        self.visualizer.player = self.player
        self.setup_conversion_options(left_content)
        self.options_scroll = left_scroll
        # The window title already names the app, so the options start at the
        # top; empty space still moves the window like a header bar would.
        left_box = Gtk.WindowHandle(child=left_scroll)

        # Right: the queue, with adding and converting in the header's middle.
        right_box = Adw.ToolbarView(css_classes=["queue-pane"])
        right_box.set_size_request(320, -1)
        queue_header = Adw.HeaderBar()
        queue_header.set_decoration_layout(":minimize,maximize,close")
        # Shown once the sidebar folds away; it names how many adjustments
        # the files will get, where there is room for it.
        self.adjustments_label = Gtk.Label(margin_start=6, visible=False)
        self.adjustments_slot = Gtk.Box()
        self.adjustments_slot.append(self.adjustments_label)
        sidebar_content = Gtk.Box()
        sidebar_content.append(Gtk.Image(icon_name="sidebar-show-symbolic"))
        sidebar_content.append(self.adjustments_slot)
        self.sidebar_button = Gtk.ToggleButton(
            child=sidebar_content, visible=False, tooltip_text=_("Show settings")
        )
        self.sidebar_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Show settings")]
        )
        self.split_view.bind_property(
            "show-sidebar",
            self.sidebar_button,
            "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        queue_header.pack_start(self.sidebar_button)
        # Named in words: the rows' own remove buttons use an icon. Both
        # leave the header when the window narrows; the menu clears too.
        self.queue_info = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.clear_queue_button = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="edit-clear-all-symbolic", label=_("Clear queue")
            ),
            action_name="win.clear-queue",
            css_classes=["flat"],
            visible=False,
        )
        self.clear_queue_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Clear queue")]
        )
        self.queue_info.append(self.clear_queue_button)
        self.queue_label = Gtk.Label(
            css_classes=["caption", "dim-label"],
            margin_start=4,
            margin_end=8,
            visible=False,
        )
        self.queue_info.append(self.queue_label)
        queue_header.pack_start(self.queue_info)

        # Blue until the queue has files; then Convert is the next step.
        # In the narrowest window adding shows as a plus.
        self.add_files_face = Gtk.Stack(hhomogeneous=False, interpolate_size=True)
        self.add_files_face.add_named(Gtk.Label(label=_("Add Files")), "label")
        self.add_files_face.add_named(Gtk.Image(icon_name="list-add-symbolic"), "icon")
        self.add_files_button = Gtk.Button(
            child=self.add_files_face,
            action_name="win.add-files",
            css_classes=["suggested-action"],
        )
        self.add_files_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Add Files")]
        )
        self.convert_button = Gtk.Button(
            action_name="win.convert", css_classes=["suggested-action"], visible=False
        )
        center = Gtk.Box(spacing=8)
        center.append(self.add_files_button)
        center.append(self.convert_button)
        queue_header.set_title_widget(center)

        menu = Gio.Menu()
        menu.append(_("Show help on hover"), "app.toggle-tips")
        queue_section = Gio.Menu()
        queue_section.append(_("Clear Queue"), "win.clear-queue")
        queue_section.append(_("Reset All Settings…"), "win.reset-settings")
        menu.append_section(None, queue_section)
        about_section = Gio.Menu()
        about_section.append(_("About"), "app.about")
        menu.append_section(None, about_section)
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic", menu_model=menu, primary=True
        )
        menu_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Main menu")])
        queue_header.pack_end(menu_button)
        right_box.add_top_bar(queue_header)
        # A finished batch: what happened, and the next step.
        self.banner = Adw.Banner()
        self.banner.connect(
            "button-clicked", lambda *_: owner.conversion.banner_activated()
        )
        right_box.add_top_bar(self.banner)

        self.file_queue = FileQueue(
            self.converter, self.tooltip_helper, self.file_markers
        )

        self.file_queue.on_queue_size_changed = self._show_queue_summary

        self.file_queue.on_play_file = weak_callback(self.on_play_file)

        self.file_queue.on_stop_playback = self.player.stop

        self.file_queue.on_file_added_to_empty_queue = self.on_file_added_to_empty_queue

        self.file_queue.on_activate_file = self.on_activate_file

        right_box.set_content(self.file_queue)

        self.eq_panel = EqualizerPanel(self.player, self.app.config)
        self.eq_panel.on_changed = weak_callback(self._show_equalizer_state)
        self.eq_revealer = Gtk.Revealer()
        self.eq_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.eq_revealer.set_transition_duration(200)
        self.eq_revealer.set_reveal_child(False)
        self.eq_revealer.set_child(self.eq_panel)
        right_box.add_bottom_bar(self.eq_revealer)
        right_box.add_bottom_bar(self.destination_bar)

        self.split_view.set_sidebar(left_box)
        self.split_view.set_content(right_box)

        visualizer_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        visualizer_container.add_css_class("playback-panel")

        # As in the BigLinux players: volume, speed and voice on the left, the
        # transport in the middle, the preview and waveform tools on the right.
        # Their sliders open as the pointer reaches them.
        self.bar_popovers = HoverPopovers(self._sources)
        adjust = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.volume_button, self.volume_value, _scale = self._bar_control(
            adjust,
            "volume",
            "audio-volume-medium-symbolic",
            _("Volume"),
            self.volume_adjustment,
            (self.volume_spin,),
        )
        self.speed_button, self.speed_value, _scale = self._bar_control(
            adjust,
            "speed",
            "power-profile-performance-symbolic",
            _("Speed"),
            self.speed_adjustment,
            (self.speed_spin,),
        )
        quality = Gtk.Label(
            label=_("Cleaning quality"), css_classes=["caption", "dim-label"]
        )
        self.voice_button, self.voice_value, self.voice_scale = self._bar_control(
            adjust,
            "voice",
            "audio-input-microphone-symbolic",
            _("Clean voice"),
            self.noise_adjustment,
            (quality, self.noise_model, self.noise_status),
        )
        self.voice_value.set_label("0")
        self._update_noise_availability()
        end_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)

        # Hear the same moment with and without the sidebar's adjustments.
        self.original_preview = Adw.ToggleGroup(
            valign=Gtk.Align.CENTER, visible=False, css_classes=["round"]
        )
        self.original_preview.add(Adw.Toggle(name="adjusted", label=_("Adjusted")))
        self.original_preview.add(Adw.Toggle(name="original", label=_("Original")))
        self.original_preview.set_active_name("adjusted")
        self.original_preview.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Listen with or without adjustments")]
        )
        self.original_preview.connect(
            "notify::active-name", lambda *_: owner._apply_effects_bypass()
        )
        end_box.append(self.original_preview)
        self.effect_cards.on_changed = weak_callback(self._update_preview_toggle)

        # Plays only the marked parts.
        self.selection_content = Adw.ButtonContent(
            icon_name="selection-mode-symbolic", label=_("Selection only")
        )
        self.play_selection_switch = Gtk.ToggleButton(child=self.selection_content)
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

        # The keyboard's way to the first mark, so it shows with any file.
        self.segment_edit_button = Gtk.Button(label=_("Edit Segments…"))
        self.segment_edit_button.add_css_class("flat")
        self.segment_edit_button.set_valign(Gtk.Align.CENTER)
        self.segment_edit_button.connect(
            "clicked", weak_callback(self.on_edit_segments)
        )
        end_box.append(self.segment_edit_button)

        # The readout between the zoom buttons resets the zoom.
        self.zoom_box = Gtk.Box(valign=Gtk.Align.CENTER)
        zoom_buttons = []
        for icon, label, action in (
            ("zoom-out-symbolic", _("Zoom out"), "zoom_out"),
            (None, _("Reset zoom"), "reset_zoom"),
            ("zoom-in-symbolic", _("Zoom in"), "zoom_in"),
        ):
            button = Gtk.Button(css_classes=["flat"], tooltip_text=label)
            if icon:
                button.set_icon_name(icon)
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            button.connect(
                "clicked", lambda _b, name=action: getattr(owner.visualizer, name)()
            )
            self.zoom_box.append(button)
            zoom_buttons.append(button)
        self.zoom_out_button, self.zoom_reset_button, _zoom_in = zoom_buttons
        self.zoom_reset_button.add_css_class("numeric")
        self.zoom_reset_button.set_label("1×")
        self.zoom_out_button.set_sensitive(False)
        self.zoom_reset_button.set_sensitive(False)
        end_box.append(self.zoom_box)

        # Bottom right, as in the players: it opens the equalizer above the
        # queue's foot and lights while the curve is not flat.
        # The app's own icon, outside the theme's indexed folders: by file.
        icon = Gio.FileIcon.new(
            Gio.File.new_for_path(
                str(
                    Path(__file__).resolve().parents[4]
                    / "icons/hicolor/symbolic/actions"
                    / "big-audio-converter-equalizer-symbolic.svg"
                )
            )
        )
        self.equalizer_button = Gtk.ToggleButton(
            child=Gtk.Image(gicon=icon),
            tooltip_text=_("Equalizer"),
            valign=Gtk.Align.CENTER,
            css_classes=["flat"],
        )
        self.equalizer_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Equalizer")]
        )
        self.eq_revealer.bind_property(
            "reveal-child",
            self.equalizer_button,
            "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        end_box.append(self.equalizer_button)

        # Rarely changed; it lives in the playback options menu below.
        self.auto_advance_switch = Adw.SwitchRow(
            title=_("Play the next file automatically"),
            active=self.app.config.get("auto_advance_enabled", True) is True,
        )
        self.auto_advance_switch.connect(
            "notify::active",
            lambda row, _pspec: owner._on_auto_advance_switch_toggled(row),
        )
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
        output_popover.set_child(output_group)
        # Outputs are looked up again each time the menu opens.
        output_popover.connect(
            "notify::visible",
            lambda popover, *_: (
                owner.player.refresh_audio_devices() if popover.get_visible() else None
            ),
        )
        self.audio_output_button.set_popover(output_popover)
        self.player.devices_callback = weak_callback(self._refresh_audio_outputs)

        playback_controls_box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.prev_audio_btn = Gtk.Button(
            icon_name="media-skip-backward-symbolic",
            css_classes=["flat", "circular"],
            visible=False,
        )
        self.prev_audio_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Previous track")]
        )
        self.prev_audio_btn.connect(
            "clicked", lambda btn: owner._on_previous_audio_clicked()
        )
        playback_controls_box.append(self.prev_audio_btn)

        self.pause_play_btn = Gtk.Button(
            icon_name="media-playback-start-symbolic",
            css_classes=["circular", "suggested-action", "play-button"],
        )
        self.pause_play_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Play or pause")]
        )
        self.pause_play_btn.connect(
            "clicked", weak_callback(self._on_pause_play_clicked)
        )
        playback_controls_box.append(self.pause_play_btn)

        self.next_audio_btn = Gtk.Button(
            icon_name="media-skip-forward-symbolic",
            css_classes=["flat", "circular"],
            visible=False,
        )
        self.next_audio_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Next track")]
        )
        self.next_audio_btn.connect(
            "clicked", lambda btn: owner._on_next_audio_clicked()
        )
        playback_controls_box.append(self.next_audio_btn)

        # One set of controls, laid out on one line or, when narrow, on two.
        self.controls_layout = Adw.MultiLayoutView()
        for name, widget in (
            ("adjust", adjust),
            ("transport", playback_controls_box),
            ("tools", end_box),
            ("menu", self.audio_output_button),
        ):
            self.controls_layout.set_child(name, widget)
        tools_and_menu = Gtk.Box(spacing=6)
        tools_and_menu.append(Adw.LayoutSlot(id="tools"))
        tools_and_menu.append(Adw.LayoutSlot(id="menu"))
        wide = Gtk.CenterBox(
            css_classes=["playback-controls"],
            start_widget=Adw.LayoutSlot(id="adjust"),
            center_widget=Adw.LayoutSlot(id="transport"),
            end_widget=tools_and_menu,
        )
        narrow = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            css_classes=["playback-controls"],
        )
        narrow.append(
            Gtk.CenterBox(
                start_widget=Adw.LayoutSlot(id="adjust"),
                center_widget=Adw.LayoutSlot(id="transport"),
                end_widget=Adw.LayoutSlot(id="menu"),
            )
        )
        narrow.append(Adw.LayoutSlot(id="tools", halign=Gtk.Align.CENTER))
        self.controls_layout.add_layout(Adw.Layout(name="wide", content=wide))
        self.controls_layout.add_layout(Adw.Layout(name="narrow", content=narrow))

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

        self.visualizer.zoom_changed_callback = weak_callback(
            self._on_visualizer_zoom_changed
        )
        # Panning while paused moves the window too; the seek bar follows it.
        self.visualizer.viewport_changed_callback = self._sync_seekbar_viewport

        self.visualizer.marker_updated_callback = self._on_markers_updated

        self.visualizer.set_content_height(100)

        self.visualizer.set_vexpand(True)  # Allow expansion
        self.visualizer.set_focusable(True)

        self.visualizer_frame.set_child(self.visualizer)

        # Layout order: waveform (top) → seekbar (middle) → button bar (bottom)
        visualizer_container.append(self.visualizer_frame)

        # The time read at both ends of the seek bar.
        self.elapsed_label = Gtk.Label(label="0:00", css_classes=["numeric", "caption"])
        self.total_label = Gtk.Label(label="0:00", css_classes=["numeric", "caption"])
        self.seekbar = SeekBar(self.elapsed_label, self.total_label)
        self.seekbar.set_hexpand(True)
        self.visualizer.duration_changed_callback = self.seekbar.set_duration
        self.seekbar.connect_seek_handler(self.on_visualizer_seek)
        self.visualizer.hover_time_callback = self.seekbar.set_waveform_hover_time
        seek_row = Gtk.Box(spacing=10, margin_start=12, margin_end=12)
        for widget in (self.elapsed_label, self.seekbar, self.total_label):
            seek_row.append(widget)
        visualizer_container.append(seek_row)

        visualizer_container.append(self.controls_layout)

        self.vertical_paned.set_end_child(visualizer_container)
        self._fit_pending = False
        self.vertical_paned.connect(
            "notify::max-position", weak_callback(self._on_paned_allocated)
        )

        # The player arrives with the first file; cut rows with a segment.
        visualizer_container.set_visible(False)
        self._update_cut_rows()

        # Narrow windows fold the sidebar over the queue; narrower ones also
        # stack the playback controls. One breakpoint applies at a time, so
        # the narrower repeats the wider one's changes.
        # Secondary controls leave first; their actions stay reachable (the
        # wheel zooms, the menu clears, the Convert label counts).
        compact = [
            (self.zoom_box, "visible", False),
            (self.selection_content, "label", ""),
            (self.queue_info, "visible", False),
        ]
        folded = [
            (self.split_view, "collapsed", True),
            (self.sidebar_button, "visible", True),
        ]
        stacked = [
            (self.controls_layout, "layout-name", "narrow"),
            (self.add_files_face, "visible-child-name", "icon"),
            (self.adjustments_slot, "visible", False),
        ]
        for width, setters in (
            (1080, compact),
            (860, compact + folded),
            (830, compact + folded + stacked),
        ):
            breakpoint = Adw.Breakpoint.new(
                Adw.BreakpointCondition.parse(f"max-width: {width}sp")
            )
            for widget, name, value in setters:
                breakpoint.add_setter(widget, name, value)
            self.add_breakpoint(breakpoint)

        self._show_equalizer_state()

        # Apply tooltips to all UI elements (must be after all widgets are created)
        self._apply_tooltips()
        self._restore_conversion_settings()
        self._on_format_changed(self.format_row, None)

    def _bar_control(self, parent, name, icon, title, adjustment, extras):
        """A button and readout whose slider opens on hover, as in the players.

        The popover's heading names what the slider changes. A click opens it
        for the keyboard; the wheel over the control moves the slider.
        """
        owner = weakref.proxy(self)
        button = Gtk.Button(
            icon_name=icon, valign=Gtk.Align.CENTER, css_classes=["flat", "circular"]
        )
        button.update_property([Gtk.AccessibleProperty.LABEL], [title])
        value = Gtk.Label(css_classes=["numeric"])
        box = Gtk.Box(spacing=2)
        box.append(button)
        box.append(value)
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.VERTICAL,
            adjustment=adjustment,
            inverted=True,
            draw_value=False,
            height_request=180,
            halign=Gtk.Align.CENTER,
        )
        scale.update_property([Gtk.AccessibleProperty.LABEL], [title])
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=10,
            margin_bottom=10,
            margin_start=10,
            margin_end=10,
        )
        content.append(Gtk.Label(label=title, css_classes=["heading"]))
        content.append(scale)
        for widget in extras:
            widget.set_halign(Gtk.Align.CENTER)
            content.append(widget)
        # A popover is its anchor's child, so a control in it that expands
        # would widen the anchor and aim the arrow past the button.
        popover = Gtk.Popover(
            child=content,
            position=Gtk.PositionType.TOP,
            hexpand=False,
            vexpand=False,
            css_classes=["bar-slider"],
        )
        popover.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.bar_popovers.attach(name, box, popover, focus=scale)
        button.connect("clicked", lambda _b: owner.bar_popovers.open_for_keyboard(name))
        wheel = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL
            | Gtk.EventControllerScrollFlags.DISCRETE
        )

        def scrolled(_controller, _dx, dy):
            if not dy or not scale.get_sensitive():
                return False
            adjustment.set_value(adjustment.get_value() - 2 * dy)
            return True

        wheel.connect("scroll", scrolled)
        box.add_controller(wheel)
        parent.append(box)
        return button, value, scale

    def _show_queue_summary(self, count):
        """The queue's size and length, and the controls that need files."""
        if not self.converter.busy:  # a conversion shows its progress there
            summary = gettext.ngettext("{count} file", "{count} files", count).format(
                count=count
            )
            total = self.file_queue.total_duration()
            if total:
                summary += " · " + format_time_display(total)
            self.queue_label.set_label(summary)
            self.queue_label.set_visible(count > 1)
            self.clear_queue_button.set_visible(count > 1)
        self.banner.set_revealed(False)
        self.lookup_action("clear-queue").set_enabled(count > 0)
        self._sync_convert_action()
        add = self.add_files_button
        (add.remove_css_class if count else add.add_css_class)("suggested-action")
        self.prev_audio_btn.set_visible(count > 1)
        self.next_audio_btn.set_visible(count > 1)
        self.convert_button.set_visible(count > 0)
        self._sync_join()
        self._update_convert_label()
        self._set_player_visible(count > 0)

    def _on_visualizer_zoom_changed(self, zoom_level):
        self.zoom_reset_button.set_label(f"{zoom_level:.3g}×")
        self.zoom_out_button.set_sensitive(zoom_level > 1.0)
        self.zoom_reset_button.set_sensitive(zoom_level > 1.0)
        self._sync_seekbar_viewport()

    def _sync_seekbar_viewport(self):
        """Show the waveform's visible window on the seek bar."""
        self.seekbar.set_zoom_viewport(
            self.visualizer.zoom_level, self.visualizer.viewport_offset
        )

    def _sync_convert_action(self):
        queue = self.file_queue
        self.lookup_action("convert").set_enabled(
            bool(queue.files) and not queue.pending and not self.converter.busy
        )

    def _update_convert_label(self):
        count = len(self.file_queue.files)
        if count > 1 and self._joining():
            label = gettext.ngettext("Join %d file", "Join %d files", count)
        else:
            label = gettext.ngettext("Convert %d file", "Convert %d files", count)
        self.convert_button.set_label(label % count)

    def _set_player_visible(self, visible):
        """Without files the queue has the whole window."""
        if visible == self.visualizer_container.get_visible():
            return
        self._remember_waveform_height()
        self.visualizer_container.set_visible(visible)
        if visible:
            self._fit_bottom_panel()

    def _on_paned_allocated(self, *_args):
        if self._fit_pending:
            self._fit_pending = False
            self._sources.idle(self._fit_bottom_panel)

    def _remember_waveform_height(self):
        """Keep the height the person dragged the waveform to."""
        height = self.visualizer_frame.get_height()
        if self.visualizer_container.get_visible() and height >= 100:
            self.visualizer_height = height
            self.app.config.set("visualizer_height", str(height))

    def _fit_bottom_panel(self):
        """Give the bottom panel its controls, plus the waveform while cutting."""
        paned = self.vertical_paned
        if not self.visualizer_container.get_visible():
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
            (self.prev_audio_btn, "prev_audio_btn"),
            (self.pause_play_btn, "pause_play_btn"),
            (self.next_audio_btn, "next_audio_btn"),
            (self.play_selection_switch, "play_selection_switch"),
            (self.visualizer, "waveform_visualizer"),
        ):
            self.tooltip_helper.add_tooltip(widget, key)

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
            "allow_precision_reduction": self.precision_row.get_active(),
            "output_directory": self.output_directory,
            "noise_reduction": self._noise_active(),
            "noise_engine": self._noise_engine(),
            "noise_strength": self.noise_adjustment.get_value(),
            # Each correction is off at zero and otherwise set by its slider.
            "compressor_enabled": self.compressor_tile.steps.get_selected() > 0,
            "compressor_intensity": COMPRESSOR_STEPS[
                self.compressor_tile.steps.get_selected()
            ],
            "eq_enabled": any(
                self.eq_panel.band_scales[f].get_value() != 0
                for _, f in self.eq_panel.BANDS
            ),
            "eq_bands": ",".join(
                str(self.eq_panel.band_scales[f].get_value())
                for _, f in self.eq_panel.BANDS
            ),
            "normalize": self.normalize_tile.get_active(),
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
                "compressor_enabled",
                "eq_enabled",
                "normalize",
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
        self.pause_panel.cleanup()
        self.effect_cards.cleanup()
        self.bar_popovers.unparent_all()
        self.converter.cancel_conversion()
        self.file_queue.cleanup()
        self.waveform_generator.cleanup()
        self.converter.cleanup()
        self.player.cleanup()
        self.visualizer.cleanup()
        self.tooltip_helper.cleanup()
        self.seekbar.connect_seek_handler(None)
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
        self.pause_panel.sync()
        self.waveform_generator.request(
            file_path,
            self.converter,
            self.visualizer,
            self.file_markers,
            track_metadata=self.file_queue.track_metadata,
        )

    def _on_pending_changed(self, pending):
        if not self._closed:
            self._sync_convert_action()

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

    def on_clear_queue(self):
        """Cuts are work; a queue without them is cleared at once."""
        self._save_current_file_state()
        if not self.file_markers:
            self._on_clear_queue_response(None, "clear")
            return
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
        """Files dropped anywhere in the window join the queue."""
        owner = weakref.proxy(self)
        overlay = self.drop_overlay

        def entered(*_args):
            if owner.converter.busy:
                return 0
            overlay.set_visible(True)
            return Gdk.DragAction.COPY

        def dropped(_target, value, _x, _y):
            overlay.set_visible(False)
            return not owner.converter.busy and owner.file_queue.add_dropped(value)

        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("enter", entered)
        drop_target.connect("leave", lambda *_: overlay.set_visible(False))
        drop_target.connect("drop", dropped)
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
