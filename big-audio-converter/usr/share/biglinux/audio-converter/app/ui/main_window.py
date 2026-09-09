# app/ui/main_window.py

"""
Main Window for the Audio Converter application.
"""

import gettext

import gi

gettext.textdomain("big-audio-converter")
_ = gettext.gettext
import logging
import os
from copy import deepcopy

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from app.audio.waveform import WaveformGenerator
from app.ui.controls_bar_mixin import ControlsBarMixin
from app.ui.conversion_controller import ConversionController
from app.ui.equalizer_panel import EqualizerPanel
from app.ui.file_queue import FileQueue
from app.ui.playback_controller import PlaybackControllerMixin
from app.ui.segment_editor import SegmentEditor
from app.ui.settings_mixin import SettingsManagerMixin
from app.ui.visualizer import AudioVisualizer, SeekBar
from app.utils.main_loop import MainLoopSources
from app.utils.tooltip_helper import TooltipHelper

logger = logging.getLogger(__name__)


class HeaderBar(Gtk.Box):
    """
    Custom header bar with action buttons for file management and conversion.
    Encapsulates layout logic to ensure proper resizing behavior.
    """

    def __init__(self, main_window, window_buttons_left=False):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.main_window = main_window
        self.window_buttons_left = window_buttons_left

        # Ensure the wrapper box occupies full width
        self.set_hexpand(True)

        # Create the Adw.HeaderBar
        self.header_bar = Adw.HeaderBar()
        # Ensure the inner HeaderBar occupies full width
        self.header_bar.set_hexpand(True)
        self.header_bar.set_show_title(True)

        # Configure decoration layout based on window button position
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
        self.clear_queue_button = Gtk.Button()
        self.clear_queue_button.set_icon_name("edit-delete-symbolic")
        self.clear_queue_button.add_css_class("flat")
        self.clear_queue_button.add_css_class("circular")
        self.clear_queue_button.set_valign(Gtk.Align.CENTER)
        self.clear_queue_button.connect("clicked", self.main_window.on_clear_queue)
        self.clear_queue_button.add_css_class("destructive-action")
        self.clear_queue_button.set_visible(False)
        self.clear_queue_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Clear queue")],
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
        menu.append(_("About"), "app.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        menu_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Main menu")],
        )

        # Create stateful action for tooltip toggle
        tips_enabled = True
        app = self.main_window.app
        if hasattr(app, "config") and app.config:
            tips_enabled = (
                str(app.config.get("show_mouseover_tips", "true")).lower() == "true"
            )
        tips_action = Gio.SimpleAction.new_stateful(
            "toggle-tips", None, GLib.Variant.new_boolean(tips_enabled)
        )
        tips_action.connect("change-state", self.main_window._on_tips_action_changed)
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
        add_files_button.connect("clicked", self.main_window.on_add_files)
        # Keep Convert as the single primary action once files are ready.
        center_box.append(add_files_button)

        # Convert button
        self.convert_button = Gtk.Button(label=_("Convert"))
        self.convert_button.connect("clicked", self.main_window.on_convert)
        self.convert_button.add_css_class("suggested-action")
        self.convert_button.set_visible(False)
        center_box.append(self.convert_button)

        self.header_bar.set_title_widget(center_box)

    def set_queue_info_visible(self, visible):
        self.clear_queue_button.set_visible(visible)
        self.queue_size_label.set_visible(visible)

    def update_queue_label(self, text):
        self.queue_size_label.set_text(text)

    def set_convert_button_visible(self, visible):
        self.convert_button.set_visible(visible)


class MainWindow(
    ControlsBarMixin,
    SettingsManagerMixin,
    PlaybackControllerMixin,
    Adw.ApplicationWindow,
):
    def _window_buttons_on_left(self):
        """Respect GTK decoration preferences without requiring a GNOME schema."""
        settings = Gtk.Settings.get_default()
        layout = settings.get_property("gtk-decoration-layout") if settings else ""
        return "close" in (layout or "").split(":", 1)[0] if ":" in (layout or "") else False

    """Main application window."""

    def __init__(self, **kwargs):
        # Extract stored window size and maximized state (with defaults)
        default_width = 1100
        default_height = 800
        is_maximized = False

        # Try to load saved window state
        if hasattr(kwargs.get("application", None), "config"):
            config = kwargs.get("application").config
            if config:
                # Load window size from config
                saved_width = config.get("window_width")
                if saved_width:
                    try:
                        loaded_width = int(saved_width)
                        # Ensure width is not smaller than minimum
                        default_width = max(loaded_width, 920)
                    except (ValueError, TypeError):
                        pass

                saved_height = config.get("window_height")
                if saved_height:
                    try:
                        loaded_height = int(saved_height)
                        # Ensure height is not smaller than minimum
                        default_height = max(loaded_height, 600)
                    except (ValueError, TypeError):
                        pass

                # Load window maximized state from config
                saved_maximized = config.get("window_maximized")
                if saved_maximized:
                    is_maximized = str(saved_maximized).lower() == "true"

        # Initialize with loaded or default size
        super().__init__(
            title=_("Audio Converter"),
            default_width=default_width,
            default_height=default_height,
            **kwargs,
        )

        # Store whether window should be maximized
        self._should_maximize = is_maximized

        # Set minimum window size to prevent controls from being cut off
        # Left sidebar (300px) + right content (620px) = 920px minimum width
        self.set_size_request(360, 480)
        # For debouncing window size save
        self._size_save_timeout_id = None

        self.app = kwargs.get("application")
        self._closed = False
        self._dialog_cancellable = Gio.Cancellable()
        self._probe_errors = []
        self._probe_error_dialog = None
        self._sources = MainLoopSources()
        self._playback_sources = MainLoopSources()
        self.waveform_generator = WaveformGenerator()

        # Initialize tooltip helper
        if hasattr(self.app, "config") and self.app.config:
            self.tooltip_helper = TooltipHelper(self.app.config)
        else:
            self.tooltip_helper = None

        # Initialize components
        self.player = self.app.player
        self.converter = self.app.converter
        # Add marker cache to remember markers for each file
        self.file_markers = {}  # Dictionary mapping file path to marker pairs

        # Track if copy mode info dialog has been shown (show only once per session)
        self._copy_info_shown = False

        # Track if we're in initialization to avoid showing dialogs on startup
        self._initializing = True

        # Selection playback tracking
        self._playing_selection = False
        self._selection_segments = []  # List of (start, stop) tuples
        self._current_segment_index = 0
        self._play_selection_mode = False  # Whether switch is on
        self._segment_seek_in_progress = False  # Prevent seek loops
        self._last_segment_end_time = 0  # Track when we last transitioned
        self._marker_dragging = False  # Track when user is dragging markers
        self._is_transitioning_segment = (
            False  # Lock to prevent transition race conditions
        )

        # For debouncing sidebar width save
        self._sidebar_save_timeout_id = None

        # Default sidebar width - will be overridden by saved value if available
        self.sidebar_width = 380

        # Default visualizer height - will be overridden by saved value
        self.visualizer_height = 132

        # Try to load saved sidebar width
        if hasattr(self.app, "config") and self.app.config:
            # Fix: match the parameter pattern used in the save method
            saved_width = self.app.config.get("sidebar_width")
            if saved_width:
                try:
                    self.sidebar_width = max(150, int(saved_width))
                except (ValueError, TypeError):
                    pass  # Use default if conversion fails

            # Load saved visualizer height
            saved_height = self.app.config.get("visualizer_height")
            if saved_height:
                try:
                    self.visualizer_height = max(100, int(saved_height))
                except (ValueError, TypeError):
                    pass  # Use default if conversion fails

        # Reuse the preferred width as a bound, not as a minimum window width.
        self.sidebar_width = min(420, max(280, self.sidebar_width))

        # Set up GUI first, creating the visualizer
        self.setup_ui()
        self.conversion = ConversionController(self)
        self.file_queue.on_probe_error = self._on_probe_error
        self.file_queue.on_pending_changed = self._on_pending_changed
        self.setup_drop_target()
        self.connect("close-request", self.on_close_request)

        # Setup visualizer tooltip after UI is fully created
        if self.tooltip_helper and hasattr(self, "visualizer"):
            self._sources.idle(self._setup_visualizer_tooltip)

        # Connect to map event for visualizer height restoration
        self.connect("map", self.on_window_mapped)

        # Connect window state signals (only maximized, save size on close only)
        self.connect("notify::maximized", self._on_window_state_changed)

        # Connect player signals to UI
        self.player.connect("position-updated", self.on_player_position_updated)
        self.player.connect("duration-changed", self.on_player_duration_changed)
        self.player.connect("state-changed", self.on_player_state_changed)
        self.player.connect("eos", self.on_playback_finished)
        self.player.connect("error", self._on_player_error)

        # Restore maximized state after window is fully initialized
        if self._should_maximize:
            self._sources.later(100, self.maximize)

        # Connect file removal signal
        self.file_queue.connect_file_removed_signal(self._on_file_removed)

        # Store the currently active audio ID
        self.active_audio_id = None

        # Setup window-level keyboard shortcuts
        self._setup_keyboard_shortcuts()

    def _setup_keyboard_shortcuts(self):
        """Register window-level keyboard shortcuts."""
        # Create window-level actions
        add_files_action = Gio.SimpleAction.new("add-files", None)
        add_files_action.connect("activate", lambda *_: self.on_add_files(None))
        self.add_action(add_files_action)

        convert_action = Gio.SimpleAction.new("convert", None)
        convert_action.connect("activate", lambda *_: self.on_convert(None))
        self.add_action(convert_action)

        play_pause_action = Gio.SimpleAction.new("play-pause", None)
        play_pause_action.connect("activate", lambda *_: self._on_pause_play_clicked(None))
        self.add_action(play_pause_action)

        edit = Gio.SimpleAction.new("edit-segments", None)
        edit.connect("activate", lambda *_: self.on_edit_segments())
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
        self.split_view.set_max_sidebar_width(self.sidebar_width)
        self.split_view.set_sidebar_width_fraction(.34)
        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 850sp"))
        compact.add_setter(self.split_view, "collapsed", True)
        self.add_breakpoint(compact)
        # Add split_view directly to vertical_paned (top part)
        self.vertical_paned.set_start_child(self.split_view)

        # Create CSS for sidebar styling
        css_provider = Gtk.CssProvider()
        css_provider.load_from_string("""
        .sidebar { background-color: @sidebar_bg_color; }
        .playback-controls { padding: 6px 10px; }
        .playback-panel { background-color: @window_bg_color; color: @window_fg_color; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Prepare queue controls, but only add to one headerbar (never both)
        window_buttons_left = self._window_buttons_on_left()
        self.clear_queue_button = Gtk.Button()
        self.clear_queue_button.set_icon_name("trash-symbolic")
        self.clear_queue_button.add_css_class("circular")
        self.clear_queue_button.connect("clicked", self.on_clear_queue)
        self.clear_queue_button.add_css_class("destructive-action")
        self.clear_queue_button.set_visible(False)  # Initially hidden
        self.clear_queue_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Clear queue")],
        )
        self.header_queue_size_label = Gtk.Label(label=_("0 files"))
        self.header_queue_size_label.add_css_class("caption")
        self.header_queue_size_label.add_css_class("dim-label")
        self.header_queue_size_label.set_visible(False)
        self.header_queue_size_label.set_margin_start(4)
        self.header_queue_size_label.set_margin_end(8)
        self.header_queue_size_label.set_valign(Gtk.Align.CENTER)

        # LEFT SIDE - Now contains conversion options (previously on right)
        left_box = Adw.ToolbarView()
        left_box.add_css_class("sidebar")
        # Set minimum width for left sidebar
        left_box.set_size_request(-1, -1)

        # Create header bar for left side
        left_header = Adw.HeaderBar()
        left_header.add_css_class("sidebar")
        left_header.set_show_title(True)
        # Configure left header bar based on window button layout
        left_header.set_show_start_title_buttons(False)
        left_header.set_show_end_title_buttons(False)

        # Create title box with label and (optionally) app icon
        if not window_buttons_left:
            # App icon on left if window buttons are on right, text truly centered
            center_box = Gtk.CenterBox()
            center_box.set_hexpand(True)
            app_icon = Gtk.Image.new_from_icon_name("big-audio-converter")
            app_icon.set_pixel_size(20)
            app_icon.set_halign(Gtk.Align.START)
            app_icon.set_valign(Gtk.Align.START)
            # Do not expand icon
            app_icon.set_hexpand(False)
            center_box.set_start_widget(app_icon)
            title_label = Gtk.Label(label=_("Audio Converter"))
            title_label.set_halign(Gtk.Align.CENTER)
            title_label.set_valign(Gtk.Align.START)
            title_label.set_hexpand(True)
            center_box.set_center_widget(title_label)
            # No end widget
            left_header.set_title_widget(center_box)
        else:
            title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title_label = Gtk.Label(label=_("Audio Converter"))
            title_box.append(title_label)
            # Add an expanding box to push controls to the left
            expander = Gtk.Box()
            expander.set_hexpand(True)
            title_box.append(expander)
            left_header.set_title_widget(title_box)
        left_box.add_top_bar(left_header)

        # Create scrollable container for left content
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_vexpand(True)

        # Create left content container
        left_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left_content.add_css_class("sidebar")
        left_scroll.set_child(left_content)

        # Create middle container for conversion options (moved from right)
        middle_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        middle_container.set_valign(Gtk.Align.FILL)
        left_content.append(middle_container)

        # Add conversion options to middle container (this stays the same)
        self.setup_conversion_options(middle_container)

        # Set the left content
        left_box.set_content(left_scroll)

        # RIGHT SIDE - Now contains file queue (previously on left)
        right_box = Adw.ToolbarView()
        # Set minimum width for right content area
        right_box.set_size_request(320, -1)

        # Create header bar for right side using the dedicated class
        # This handles proper layout behavior and resizing
        self.right_header = HeaderBar(self, False)
        right_box.add_top_bar(self.right_header)

        # Compatibility aliases for existing code references
        self.clear_queue_button = self.right_header.clear_queue_button
        self.convert_button = self.right_header.convert_button
        self.header_queue_size_label = self.right_header.queue_size_label

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
        self.file_queue.on_play_file = self.on_play_file

        # Connect stop playback callback to player.stop method
        self.file_queue.on_stop_playback = self.player.stop

        # Connect callback for when file is added to empty queue
        self.file_queue.on_file_added_to_empty_queue = self.on_file_added_to_empty_queue

        # Connect callback for when file row is activated (clicked)
        self.file_queue.on_activate_file = self.on_activate_file

        # Initialize queue size label
        self.update_queue_size_label(0, "0 files")

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
        self.eq_revealer.connect("notify::reveal-child", self._on_eq_revealer_changed)
        right_box.add_bottom_bar(self.eq_revealer)

        # Add the two views to the paned container (swapped order)
        self.split_view.set_sidebar(left_box)
        self.split_view.set_content(right_box)

        settings_button = Gtk.ToggleButton(icon_name="emblem-system-symbolic", active=True)
        settings_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Conversion Settings")])
        settings_button.set_tooltip_text(_("Show conversion settings"))
        self.settings_button = settings_button
        self.split_view.bind_property("show-sidebar", settings_button, "active", GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self.right_header.header_bar.pack_start(settings_button)
        close_sidebar = Gtk.Button(icon_name="go-previous-symbolic")
        close_sidebar.update_property([Gtk.AccessibleProperty.LABEL], [_("Close conversion settings")])
        close_sidebar.connect("clicked", lambda *_: self.split_view.set_show_sidebar(False))
        self.split_view.bind_property("collapsed", close_sidebar, "visible", GObject.BindingFlags.SYNC_CREATE)
        left_header.pack_start(close_sidebar)


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
            "toggled", self._on_play_selection_switch_toggled
        )
        self.play_selection_switch.set_valign(Gtk.Align.CENTER)
        self.play_selection_switch.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Play only selected area")],
        )

        # Store reference for tooltip
        self.play_selection_box = self.play_selection_switch

        zoom_control_box.append(self.play_selection_switch)

        # Add "Auto-Advance" toggle button — icon-only
        self.auto_advance_switch = Gtk.ToggleButton()
        self.auto_advance_switch.set_icon_name("media-playlist-consecutive-symbolic")
        self.auto_advance_switch.add_css_class("flat")
        self.auto_advance_switch.add_css_class("circular")
        auto_advance_enabled = self.app.config.get("auto_advance_enabled", True)
        self.auto_advance_switch.set_active(auto_advance_enabled)
        self.auto_advance_switch.connect(
            "toggled", self._on_auto_advance_switch_toggled
        )
        self.auto_advance_switch.set_valign(Gtk.Align.CENTER)
        self.auto_advance_switch.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Auto-advance to next track")],
        )

        # Store reference for tooltip
        self.auto_advance_box = self.auto_advance_switch

        zoom_control_box.append(self.auto_advance_switch)

        # Add Equalizer toggle button — icon-only
        self.eq_toggle_btn = Gtk.ToggleButton()
        # Load custom equalizer SVG icon
        eq_icon_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            "..",
            "..",
            "..",
            "icons",
            "hicolor",
            "symbolic",
            "actions",
            "equalizer-symbolic.svg",
        )
        if os.path.isfile(eq_icon_path):
            eq_icon = Gtk.Image.new_from_file(eq_icon_path)
            eq_icon.set_pixel_size(16)
            self.eq_toggle_btn.set_child(eq_icon)
        else:
            self.eq_toggle_btn.set_icon_name("media-eq-symbolic")
        self.eq_toggle_btn.add_css_class("flat")
        self.eq_toggle_btn.add_css_class("circular")
        self.eq_toggle_btn.add_css_class("eq-icon-btn")
        self.eq_toggle_btn.set_active(False)
        self.eq_toggle_btn.connect("toggled", self._on_eq_toggle_clicked)
        self.eq_toggle_btn.set_valign(Gtk.Align.CENTER)
        self.eq_toggle_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Equalizer")],
        )
        zoom_control_box.append(self.eq_toggle_btn)

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
            [Gtk.AccessibleProperty.LABEL], [_("Previous track")],
        )
        self.prev_audio_btn.connect(
            "clicked", lambda btn: self._on_previous_audio_clicked()
        )
        self.prev_audio_btn.set_visible(False)  # Initially hidden
        playback_controls_box.append(self.prev_audio_btn)

        # Pause/Play button (center)
        self.pause_play_btn = Gtk.Button()
        self.pause_play_btn.set_icon_name("media-playback-start-symbolic")
        self.pause_play_btn.add_css_class("flat")
        self.pause_play_btn.add_css_class("circular")
        self.pause_play_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Play or pause")],
        )
        self.pause_play_btn.connect("clicked", self._on_pause_play_clicked)
        playback_controls_box.append(self.pause_play_btn)

        # Next audio button (right side)
        self.next_audio_btn = Gtk.Button()
        self.next_audio_btn.set_icon_name("media-skip-forward-symbolic")
        self.next_audio_btn.add_css_class("flat")
        self.next_audio_btn.add_css_class("circular")
        self.next_audio_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Next track")],
        )
        self.next_audio_btn.connect(
            "clicked", lambda btn: self._on_next_audio_clicked()
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
        self.volume_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Volume")],
        )

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
            [Gtk.AccessibleProperty.LABEL], [_("Volume level")],
        )
        self.volume_scale.add_mark(
            self._volume_to_slider(100.0), Gtk.PositionType.RIGHT, None
        )
        self.volume_scale.connect("value-changed", self._on_volume_scale_changed)
        vol_popover_box.append(self.volume_scale)

        self.volume_popover.set_child(vol_popover_box)



        self.volume_btn.connect("clicked", self._on_volume_btn_clicked)

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
        self.speed_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Playback speed")],
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
            [Gtk.AccessibleProperty.LABEL], [_("Playback speed level")],
        )
        self.speed_scale.add_mark(
            self._speed_to_slider(1.0), Gtk.PositionType.RIGHT, None
        )
        self.speed_scale.connect("value-changed", self._on_speed_scale_changed)
        spd_popover_box.append(self.speed_scale)

        self.speed_popover.set_child(spd_popover_box)



        self.speed_btn.connect("clicked", self._on_speed_btn_clicked)

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
        self.zoom_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Waveform zoom")],
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
            [Gtk.AccessibleProperty.LABEL], [_("Waveform zoom level")],
        )

        self.zoom_scale.set_format_value_func(self._format_zoom_value)
        self.zoom_scale.connect("value-changed", self._on_zoom_scale_changed)
        popover_box.append(self.zoom_scale)

        self.zoom_popover.set_child(popover_box)




        self.zoom_btn.connect("clicked", self._on_zoom_btn_clicked)

        self.zoom_box.append(self.zoom_btn)
        self.zoom_box.append(self.zoom_value_label)
        right_controls_box.append(self.zoom_box)

        zoom_control_box.append(right_controls_box)

        # Store reference to zoom control box for showing/hiding
        self.zoom_control_box = zoom_control_box

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

        # Connect viewport change handler to sync seekbar during panning
        self.visualizer.viewport_changed_callback = self._on_visualizer_viewport_changed

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
        self.seekbar.set_margin_start(4)
        self.seekbar.set_margin_end(4)
        self.seekbar.set_margin_bottom(0)
        self.seekbar.set_margin_top(0)
        self.seekbar.connect_seek_handler(self.on_visualizer_seek)
        self._set_copy_mode_ui(self._format_list[self.format_row.get_selected()] == "copy")

        # Connect waveform hover to seekbar display
        self.visualizer.hover_time_callback = self.seekbar.set_waveform_hover_time

        visualizer_container.append(self.seekbar)

        visualizer_container.append(zoom_control_box)

        # Add visualizer container to the bottom part of the vertical paned
        self.vertical_paned.set_end_child(visualizer_container)

        # Hide visualizer waveform initially (shown when files are added)
        # Seekbar and controls bar stay visible
        self.visualizer_frame.set_visible(False)

        # Set initial visibility for cut-mode-dependent elements
        cut_enabled = self.cut_row.get_selected() > 0
        self.play_selection_switch.set_visible(cut_enabled)
        self.zoom_box.set_visible(cut_enabled)
        self.seekbar.set_visible(True)
        self.visualizer_frame.set_visible(cut_enabled)

        # Set initial position, but don't rely on it for final height
        # We'll adjust this after the window is mapped
        self.vertical_paned.set_position(300)  # Use a reasonable initial position

        # Connect to position changes to save visualizer height
        self.vertical_paned.connect(
            "notify::position", self._on_visualizer_height_changed
        )

        # Apply tooltips to all UI elements (must be after all widgets are created)
        self._apply_tooltips()

    def update_queue_size_label(self, count=None, text=None):
        """Update the queue size label in the header."""
        # Get count if not provided
        if count is None and hasattr(self.file_queue, "get_queue_size"):
            count = self.file_queue.get_queue_size()

        # Get text if not provided
        if text is None:
            text = self.file_queue.get_queue_size_text()

        # Update the label text
        if hasattr(self, "right_header"):
            try:
                has_multiple_files = count > 1
            except (ValueError, TypeError):
                has_multiple_files = False

            self.right_header.update_queue_label(text)
            self.right_header.set_queue_info_visible(has_multiple_files)

            # Update conversion button visibility
            has_files = count > 0
            self.right_header.set_convert_button_visible(has_files)

        # Show/hide elements based on file count
        has_files = count > 0
        has_multiple_files = count >= 2

        # Show/hide waveform based on file count (seekbar+controls always visible)
        if hasattr(self, "visualizer_container"):
            if (
                has_files
                and hasattr(self, "cut_row")
                and self.cut_row.get_selected() > 0
            ):
                self.visualizer_frame.set_visible(True)
            else:
                self.visualizer_frame.set_visible(False)
            # Collapse paned to show only seekbar+controls when no waveform
            if not has_files or (
                hasattr(self, "cut_row") and self.cut_row.get_selected() == 0
            ):
                self._sources.idle(self._update_paned_for_cut_mode, False)

        # Queue size label only shows when there are 2 or more files (matching clear button)
        self.header_queue_size_label.set_visible(has_multiple_files)

        # Show/hide navigation buttons - only visible with multiple files
        if hasattr(self, "prev_audio_btn"):
            self.prev_audio_btn.set_visible(has_multiple_files)
        if hasattr(self, "next_audio_btn"):
            self.next_audio_btn.set_visible(has_multiple_files)

        # Clear queue button only shows when there are 2 or more files
        if hasattr(self, "clear_queue_button"):
            self.clear_queue_button.set_visible(has_multiple_files)

        # Convert button shows when there's at least one file
        if hasattr(self, "convert_button"):
            self.convert_button.set_visible(has_files)

    def _apply_tooltips(self):
        """Apply tooltips to UI elements."""
        if not self.tooltip_helper:
            return

        # Add tooltips to format combo parent row
        # Add tooltip to format row (Adw.ComboRow is the row itself)
        if hasattr(self, "format_row"):
            self.tooltip_helper.add_tooltip(self.format_row, "format")

        # Add tooltip to bitrate row
        if hasattr(self, "bitrate_row"):
            self.tooltip_helper.add_tooltip(self.bitrate_row, "bitrate")

        # Add tooltip to volume spin
        if hasattr(self, "volume_spin"):
            self.tooltip_helper.add_tooltip(self.volume_spin, "volume")

        # Add tooltip to speed spin
        if hasattr(self, "speed_spin"):
            self.tooltip_helper.add_tooltip(self.speed_spin, "speed")

        # Add tooltip to noise row (Adw.SwitchRow is the row itself)
        if hasattr(self, "noise_expander"):
            self.tooltip_helper.add_tooltip(self.noise_expander, "noise_reduction")

        # Add tooltip to noise strength spin
        if hasattr(self, "noise_strength_row"):
            self.tooltip_helper.add_tooltip(
                self.noise_strength_row, "noise_reduction_strength"
            )

        # Add tooltips to noise gate
        if hasattr(self, "gate_expander"):
            self.tooltip_helper.add_tooltip(self.gate_expander, "noise_gate")
        if hasattr(self, "gate_threshold_spin"):
            self.tooltip_helper.add_tooltip(self.gate_threshold_spin, "gate_threshold")
        if hasattr(self, "gate_range_spin"):
            self.tooltip_helper.add_tooltip(self.gate_range_spin, "gate_range")
        if hasattr(self, "gate_attack_spin"):
            self.tooltip_helper.add_tooltip(self.gate_attack_spin, "gate_attack")
        if hasattr(self, "gate_release_spin"):
            self.tooltip_helper.add_tooltip(self.gate_release_spin, "gate_release")

        # Add tooltip to loudness normalization row
        if hasattr(self, "normalize_row"):
            self.tooltip_helper.add_tooltip(self.normalize_row, "normalize")

        # Add tooltip to cut row (Adw.ComboRow is the row itself)
        if hasattr(self, "cut_row"):
            self.tooltip_helper.add_tooltip(self.cut_row, "cut")

        # Add tooltip to cut output row
        if hasattr(self, "cut_output_row"):
            self.tooltip_helper.add_tooltip(self.cut_output_row, "cut_output")

        # Add tooltip to channels row
        if hasattr(self, "channels_row"):
            self.tooltip_helper.add_tooltip(self.channels_row, "channels")

        # Add tooltips to headerbar controls
        if hasattr(self, "clear_queue_button"):
            self.tooltip_helper.add_tooltip(
                self.clear_queue_button, "clear_queue_button"
            )
        if hasattr(self, "prev_audio_btn"):
            self.tooltip_helper.add_tooltip(self.prev_audio_btn, "prev_audio_btn")
        if hasattr(self, "pause_play_btn"):
            self.tooltip_helper.add_tooltip(self.pause_play_btn, "pause_play_btn")
        if hasattr(self, "next_audio_btn"):
            self.tooltip_helper.add_tooltip(self.next_audio_btn, "next_audio_btn")
        # Apply tooltips to toggle buttons (now icon-only)
        if hasattr(self, "play_selection_switch"):
            self.tooltip_helper.add_tooltip(
                self.play_selection_switch, "play_selection_switch"
            )
        if hasattr(self, "auto_advance_switch"):
            self.tooltip_helper.add_tooltip(
                self.auto_advance_switch, "auto_advance_switch"
            )
        if hasattr(self, "eq_toggle_btn"):
            self.tooltip_helper.add_tooltip(self.eq_toggle_btn, "eq_toggle_btn")

    def _on_tips_action_changed(self, action, value):
        """Handle mouseover tips toggle from hamburger menu."""
        state = value.get_boolean()
        action.set_state(value)

        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("show_mouseover_tips", "true" if state else "false")

        if state:
            # When enabling tooltips, re-apply all of them
            self._apply_tooltips()
            # Also setup visualizer tooltip
            if hasattr(self, "visualizer"):
                self._sources.idle(self._setup_visualizer_tooltip)
        else:
            # When disabling tooltips, hide current
            if self.tooltip_helper:
                self.tooltip_helper.hide(immediate=True)
            # Hide visualizer tooltip
            if hasattr(self, "visualizer"):
                self._hide_visualizer_tooltip()

    def _setup_visualizer_tooltip(self):
        """Setup tooltip for the waveform visualizer using the standard TooltipHelper."""
        if not self.tooltip_helper or not self.tooltip_helper.is_enabled():
            return False

        if not hasattr(self, "visualizer"):
            return False

        # Use TooltipHelper with y_offset to position above the controls bar
        # Negative offset moves the tooltip up above the bar
        bar_height = 40
        self.tooltip_helper.add_tooltip(
            self.visualizer, "waveform_visualizer", y_offset=-bar_height
        )

        return False  # Don't repeat idle_add

    def _hide_visualizer_tooltip(self):
        """Hide visualizer tooltip."""
        if self.tooltip_helper:
            self.tooltip_helper.hide(immediate=True)

    def on_convert(self, button):
        """Delegate job presentation to the conversion controller."""
        self.conversion.start()

    def _collect_conversion_settings(self):
        self._save_current_file_state()
        # Collect an immutable request before the worker starts.
        # Channels: 0=original, 1=mono, 2=stereo
        channels_sel = (
            self.channels_row.get_selected() if hasattr(self, "channels_row") else 0
        )
        channels_map = {0: None, 1: 1, 2: 2}

        settings = {
            "format": self._format_list[self.format_row.get_selected()],
            "bitrate": self._bitrate_list[self.bitrate_row.get_selected()],
            "volume": self.volume_spin.get_value() / 100,
            "speed": self.speed_spin.get_value(),
            "channels": channels_map.get(channels_sel),
            "sample_rate": self._sample_rate_list[self.sample_rate_row.get_selected()],
            "prevent_clipping": self.clipping_row.get_active(),
            "allow_precision_reduction": self.precision_row.get_active(),
            "output_directory": self.output_directory,
            "noise_reduction": self.noise_switch.get_active(),
            "noise_strength": self.noise_strength_scale.get_value(),
            "noise_model": 0 if self.noise_model_row.get_selected() != 1 else 1,
            "noise_speech_strength": self.noise_speech_strength_scale.get_value(),
            "noise_lookahead": int(self.noise_lookahead_scale.get_value()),
            "noise_voice_enhance": self.noise_voice_enhance_scale.get_value(),
            "noise_model_blend": self.noise_model_row.get_selected() == 2,
            "gate_enabled": self.gate_switch.get_active(),
            "gate_intensity": self.gate_intensity_scale.get_value(),
            "compressor_enabled": self.compressor_switch.get_active(),
            "compressor_intensity": self.compressor_intensity_scale.get_value(),
            "hpf_enabled": self.hpf_row.get_active(),
            "hpf_frequency": int(self.hpf_freq_scale.get_value()),
            "transient_enabled": self.transient_row.get_active(),
            "transient_attack": self.transient_attack_scale.get_value(),
            "eq_enabled": hasattr(self, "eq_panel") and any(
                self.eq_panel.band_scales[f].get_value() != 0
                for _, f in self.eq_panel.BANDS
            ),
            "eq_bands": ",".join(
                str(self.eq_panel.band_scales[f].get_value())
                for _, f in self.eq_panel.BANDS
            ) if hasattr(self, "eq_panel") else "0,0,0,0,0,0,0,0,0,0",
            "normalize": self.normalize_row.get_active(),
            "cut_enabled": self.cut_row.get_selected() > 0,
            "cut_merge": hasattr(self, "cut_output_row")
            and self.cut_output_row.get_selected() == 1,
            # Pass track metadata from file queue for video track extraction
            "track_metadata": self.file_queue.track_metadata,
        }

        # Get segment ordering preference (True = by number, False = by timeline)
        order_by_number = self.cut_row.get_selected() == 2
        settings["order_by_segment_number"] = order_by_number

        # For multi-file cutting, store ALL marker information
        if settings["cut_enabled"]:
            # Add current markers if file is active (showing waveform)
            if self.active_audio_id:
                # Get ordered segments based on user preference
                logger.debug(f"Getting segments with order_by_number={order_by_number}")
                current_markers = self.visualizer.get_ordered_marker_pairs(
                    order_by_number
                )
                if current_markers:
                    logger.debug(
                        f"Storing {len(current_markers)} ordered segments for current file"
                    )
                    self.file_markers[self.active_audio_id] = current_markers

            # Process each file's markers with the ordering preference
            ordered_file_markers = {}
            for file_path, markers in self.file_markers.items():
                # Sort markers if needed (for files we didn't just process)
                if ((file_path != self.active_audio_id) and (order_by_number and markers)) and ("segment_index" in markers[0]):
                    logger.debug(
                        f"Reordering {len(markers)} segments for {os.path.basename(file_path)}"
                    )
                    ordered_markers = sorted(
                        markers, key=lambda x: x.get("segment_index", 1)
                    )
                    ordered_file_markers[file_path] = ordered_markers
                    continue

                # Default: keep existing order (either original or already sorted)
                ordered_file_markers[file_path] = markers

            # Store the ordered markers dictionary
            settings["file_markers"] = ordered_file_markers

            # For backward compatibility and logging
            if (
                self.active_audio_id
                and self.active_audio_id in settings["file_markers"]
            ):
                current_file_segments = settings["file_markers"][self.active_audio_id]
                if current_file_segments and len(current_file_segments) > 0:
                    # Use segments from current file for backward compatibility
                    settings["cut_segments"] = current_file_segments
                    logger.debug(
                        f"Final segments order for conversion: {[(s.get('segment_index', '?'), s['start_str']) for s in current_file_segments]}"
                    )

        if settings["format"] == "copy":
            for key in ("noise_reduction", "transient_enabled", "hpf_enabled", "gate_enabled", "compressor_enabled", "eq_enabled", "normalize", "prevent_clipping"):
                settings[key] = False
            settings.update(volume=1.0, speed=1.0, channels=None, sample_rate="original")
        return deepcopy(settings)






    def _show_error_dialog(self, title, message):
        if self._closed:
            return
        dialog = Adw.AlertDialog(heading=title, body=message)
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def _show_info_dialog(self, title, message):
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
        if self.tooltip_helper:
            self.tooltip_helper.cleanup()
        self.app.config.flush()

    def on_edit_segments(self, *_args):
        if not self.active_audio_id:
            self._show_info_dialog(_("No audio selected"), _("Add a file and select it before editing segments."))
            return
        if (self.visualizer.duration or self.player.duration) <= 0:
            self._show_info_dialog(_("Audio duration unavailable"), _("Enable cutting and wait for the waveform to finish before editing this file."))
            return
        editor = SegmentEditor(self)
        self.segment_editor = editor
        editor.present(self)

    def _request_waveform(self, file_path, enabled=None):
        if self._closed or file_path not in self.file_queue.files:
            return
        if enabled is None:
            enabled = self.cut_row.get_selected() > 0 and self.waveform_row.get_active()
        self.waveform_generator.request(file_path, self.converter, self.visualizer,
                                        self.file_markers, track_metadata=self.file_queue.track_metadata,
                                        enabled=enabled)

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
        body = "\n\n".join(name + "\n" + message for name, message in self._probe_errors[-5:])
        body += "\n\n" + _("Other valid files remain in the queue. Check these files and add them again.")
        if self._probe_error_dialog is None:
            self._probe_error_dialog = Adw.AlertDialog(heading=_("Some files could not be added"), body=body)
            self._probe_error_dialog.add_response("ok", _("OK"))
            self._probe_error_dialog.connect("closed", self._probe_errors_closed)
            self._probe_error_dialog.present(self)
        else:
            self._probe_error_dialog.set_body(body)


    def _probe_errors_closed(self, _dialog):
        self._probe_error_dialog = None
        self._probe_errors.clear()

    def _on_player_error(self, message):
        if not self._closed:
            self._show_error_dialog(_("Audio preview"), message)



    def _on_window_size_changed(self, window, param):
        """Handle window size changes."""
        # Only save size if the window is not maximized
        if not self.is_maximized():
            # Debounce to avoid saving while resizing
            if hasattr(self, "_size_save_timeout_id") and self._size_save_timeout_id:
                self._sources.cancel(self._size_save_timeout_id)

            self._size_save_timeout_id = self._sources.later(500, self._save_window_size)

    def _on_window_state_changed(self, window, param):
        """Handle window state changes (maximized)."""
        # Get current maximized state
        is_maximized = self.is_maximized()

        # Save maximized state to config
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("window_maximized", str(is_maximized).lower())

        # When window is unmaximized, make a single adjustment to fix the layout
        if not is_maximized:
            # Single adjustment with a small delay to allow window to settle
            self._sources.later(200, self._fix_layout_after_unmaximize)

    def _save_window_size(self):
        """Save the current window size to config."""
        if hasattr(self.app, "config") and self.app.config:
            width = self.get_width()
            height = self.get_height()

            # Only save if the values are reasonable
            if width > 200 and height > 200:
                self.app.config.set("window_width", str(width))
                self.app.config.set("window_height", str(height))

        if hasattr(self, "_size_save_timeout_id"):
            self._size_save_timeout_id = None

        return False  # Don't repeat the timeout

    def _fix_layout_after_unmaximize(self):
        """Adjust visualizer height and position after unmaximizing."""
        logger.debug("Fixing layout after unmaximize")
        self._restore_geometry()
        return False

    def on_window_mapped(self, widget):
        """Called when the window is mapped. Restore geometry."""
        # Use a short delay to ensure all allocations are done
        self._sources.idle(self._restore_geometry)
        return False

    def _restore_geometry(self):
        """Restore sidebar width and visualizer height after window is shown."""

        # Apply saved cut audio state to visualizer now that it exists
        if hasattr(self, "visualizer") and hasattr(self, "cut_row"):
            self.visualizer.set_markers_enabled(self.cut_row.get_selected() > 0)

        # Use the allocation-based height for accuracy
        window_height = self.get_height()
        if window_height < 100:  # Window not properly sized yet
            return True  # Try again

        # Calculate proper position from saved visualizer height
        visualizer_position = max(200, window_height - self.visualizer_height - 50)
        self.vertical_paned.set_position(visualizer_position)

        # If cut is off, collapse the waveform area
        if hasattr(self, "cut_row") and self.cut_row.get_selected() == 0:
            self._sources.idle(self._update_paned_for_cut_mode, False)

        # Visualizer height is managed by GTK Box layout, no need to set content_height here

        return False  # Don't repeat

    def on_clear_queue(self, button):
        dialog = Adw.AlertDialog(heading=_("Clear Queue"), body=_("Remove the queue entries and their edits? Source files will not be deleted."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("clear", _("Clear Queue"))
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_clear_queue_response)
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
        drop_target.connect("drop", self.on_drop)
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
            parent=self, cancellable=self._dialog_cancellable, callback=self._on_open_files_complete
        )

    def _on_open_files_complete(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
            if self._closed or files is None:
                return
            paths = [file.get_path() for file in files if file.get_path()]
            self.file_queue.add_files(paths)
            if len(paths) != files.get_n_items():
                self._show_info_dialog(_("Local files only"), _("Some selected files are remote. Download them before adding them."))
        except GLib.Error as error:
            if not self._closed and not error.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED) and not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._show_error_dialog(_("Files could not be selected"), error.message)

    def _set_busy_cursor(self, is_busy):
        """Set busy cursor while processing."""
        cursor_name = "wait" if is_busy else "default"
        cursor = Gdk.Cursor.new_from_name(cursor_name, None)
        self.get_surface().set_cursor(cursor)

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
