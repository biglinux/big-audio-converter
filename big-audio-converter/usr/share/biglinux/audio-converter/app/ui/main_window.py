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
import threading

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from app.ui.controls_bar_mixin import ControlsBarMixin
from app.ui.equalizer_panel import EqualizerPanel
from app.ui.file_queue import FileQueue
from app.ui.playback_controller import PlaybackControllerMixin
from app.ui.settings_mixin import SettingsManagerMixin
from app.ui.visualizer import AudioVisualizer, SeekBar
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
                app.config.get("show_mouseover_tips", "true").lower() == "true"
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
        left_controls.set_margin_start(20)
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
        add_files_button.add_css_class("suggested-action")
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
        """Detect if window buttons (close/min/max) are on the left side."""
        try:
            settings = Gio.Settings.new("org.gnome.desktop.wm.preferences")
            layout = settings.get_string("button-layout")
            logger.info(f"Detected button-layout: {layout}")
            if layout and ":" in layout:
                left, right = layout.split(":", 1)
                # Check for 'close' on the left side
                if "close" in left:
                    return True
                # Check for 'close' on the right side
                if "close" in right:
                    return False
            elif layout:
                # If no colon, treat as right side (default GNOME)
                if "close" in layout:
                    return False
            logger.warning(
                f"Unusual button-layout format: {layout}, defaulting to right"
            )
        except Exception as e:
            logger.warning(f"Could not detect window button layout: {e}")
        # Default: right side
        return False

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
                    is_maximized = saved_maximized.lower() == "true"

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
        self.set_size_request(920, 600)
        # For debouncing window size save
        self._size_save_timeout_id = None

        self.app = kwargs.get("application")

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
                    self.sidebar_width = int(saved_width)
                    if self.sidebar_width < 150:  # Ensure minimum width
                        self.sidebar_width = 150
                except (ValueError, TypeError):
                    pass  # Use default if conversion fails

            # Load saved visualizer height
            saved_height = self.app.config.get("visualizer_height")
            if saved_height:
                try:
                    self.visualizer_height = int(saved_height)
                    if self.visualizer_height < 100:  # Ensure minimum height
                        self.visualizer_height = 100
                except (ValueError, TypeError):
                    pass  # Use default if conversion fails

        # Ensure sidebar width doesn't violate right pane minimum (620px)
        # We use default_width (which is at least 920) to calculate safe sidebar width
        max_sidebar_width = default_width - 620
        if self.sidebar_width > max_sidebar_width:
            self.sidebar_width = max_sidebar_width

        # Ensure it's not smaller than sidebar minimum (300)
        self.sidebar_width = max(self.sidebar_width, 300)

        # Add property to track the last manually set sidebar width
        self._manual_sidebar_width = self.sidebar_width

        # Set up GUI first, creating the visualizer
        self.setup_ui()
        self.setup_drop_target()
        self.connect("close-request", self.on_close_request)

        # Setup visualizer tooltip after UI is fully created
        if self.tooltip_helper and hasattr(self, "visualizer"):
            GLib.idle_add(self._setup_visualizer_tooltip)

        # Connect to map event for visualizer height restoration
        self.connect("map", self.on_window_mapped)

        # Connect window state signals (only maximized, save size on close only)
        self.connect("notify::maximized", self._on_window_state_changed)

        # Connect player signals to UI
        self.player.connect("position-updated", self.on_player_position_updated)
        self.player.connect("duration-changed", self.on_player_duration_changed)
        self.player.connect("state-changed", self.on_player_state_changed)
        self.player.connect("eos", self.on_playback_finished)

        # Restore maximized state after window is fully initialized
        if self._should_maximize:
            GLib.timeout_add(100, self.maximize)

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

        # Set accelerators at application level
        app = self.get_application()
        app.set_accels_for_action("win.add-files", ["<Control>o"])
        app.set_accels_for_action("win.convert", ["<Control>Return"])
        app.set_accels_for_action("win.play-pause", ["space"])

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

        # Create a split view that allows resizing with the mouse (for sidebar and content)
        self.split_view = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.split_view.set_position(self.sidebar_width)
        self.split_view.set_vexpand(True)

        # Prevent panes from shrinking below their minimum size
        self.split_view.set_shrink_start_child(False)
        self.split_view.set_shrink_end_child(False)

        # Add split_view directly to vertical_paned (top part)
        self.vertical_paned.set_start_child(self.split_view)

        # Create CSS for sidebar styling
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(
            b"""
        .sidebar {
            background-color: @sidebar_bg_color;
        }
        .dark-bottom-panel {
            background-color: #1a1a1e;
        }
        .dark-controls-bar {
            background-color: #2a2a30;
            padding: 6px 15px;
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        .dark-controls-bar label {
            color: rgba(255, 255, 255, 0.75);
        }
        .dark-controls-bar button {
            color: rgba(255, 255, 255, 0.85);
            background: none;
            box-shadow: none;
            border: none;
        }
        .dark-controls-bar button:hover {
            color: #ffffff;
            background-color: rgba(255, 255, 255, 0.1);
        }
        .dark-controls-bar button:active,
        .dark-controls-bar button:checked {
            color: rgba(255, 255, 255, 0.95);
            background-color: alpha(@accent_bg_color, 0.5);
        }
        .dark-controls-bar scale trough {
            background-color: rgba(255, 255, 255, 0.12);
        }
        .dark-controls-bar scale highlight {
            background-color: @accent_bg_color;
        }
        .dark-controls-bar scale slider {
            background-color: rgba(255, 255, 255, 0.85);
        }
        .dark-controls-bar scale value {
            color: rgba(255, 255, 255, 0.75);
        }
        popover.dark-popover {
            background: none;
            border: none;
            box-shadow: none;
            padding: 0;
        }
        popover.dark-popover > contents {
            background-color: #2a2a30;
            color: rgba(255, 255, 255, 0.85);
            border: 1px solid rgba(255, 255, 255, 0.15);
            border-radius: 12px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        }
        popover.dark-popover > arrow {
            background-color: #2a2a30;
            border-color: rgba(255, 255, 255, 0.15);
        }
        popover.dark-popover label {
            color: rgba(255, 255, 255, 0.85);
        }
        popover.dark-popover scale trough {
            background-color: rgba(255, 255, 255, 0.25);
            min-width: 10px;
            min-height: 10px;
            border-radius: 5px;
        }
        popover.dark-popover scale highlight {
            background-color: @accent_bg_color;
            min-width: 10px;
            min-height: 10px;
            border-radius: 5px;
        }
        popover.dark-popover scale slider {
            background-color: rgba(255, 255, 255, 0.9);
            min-width: 20px;
            min-height: 20px;
            border-radius: 10px;
        }
        popover.dark-popover scale indicator {
            background-color: rgba(255, 255, 255, 0.3);
            min-width: 6px;
            min-height: 1px;
        }
        """,
            -1,
        )
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
        left_box.set_size_request(300, -1)

        # Create header bar for left side
        left_header = Adw.HeaderBar()
        left_header.add_css_class("sidebar")
        left_header.set_show_title(True)
        # Configure left header bar based on window button layout
        left_header.set_decoration_layout(
            "close,maximize,minimize:menu" if window_buttons_left else ""
        )

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
        right_box.set_size_request(620, -1)

        # Create header bar for right side using the dedicated class
        # This handles proper layout behavior and resizing
        self.right_header = HeaderBar(self, window_buttons_left)
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
        self.split_view.set_start_child(left_box)
        self.split_view.set_end_child(right_box)

        # Connect to position changes to save sidebar width
        self.split_view.connect("notify::position", self._on_sidebar_width_changed)

        # Add visualizer at the bottom spanning full width
        # Create the visualizer instance
        self.visualizer = AudioVisualizer()

        # Give visualizer reference to player for checking playback state
        self.visualizer.player = self.player

        # Create container for visualizer and zoom controls
        visualizer_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        visualizer_container.add_css_class("dark-bottom-panel")

        # Create zoom control bar
        zoom_control_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        zoom_control_box.set_margin_start(0)
        zoom_control_box.set_margin_end(0)
        zoom_control_box.add_css_class("dark-controls-bar")

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
        self.volume_popover.add_css_class("dark-popover")

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

        self._volume_hover_close_timer = None
        vol_hover_ctrl = Gtk.EventControllerMotion()
        vol_hover_ctrl.connect("enter", self._on_volume_btn_hover_enter)
        vol_hover_ctrl.connect("leave", self._on_volume_btn_hover_leave)
        vol_box.add_controller(vol_hover_ctrl)

        vol_pop_hover_ctrl = Gtk.EventControllerMotion()
        vol_pop_hover_ctrl.connect("enter", self._on_volume_popover_hover_enter)
        vol_pop_hover_ctrl.connect("leave", self._on_volume_popover_hover_leave)
        self.volume_popover.add_controller(vol_pop_hover_ctrl)

        self.volume_btn.connect("clicked", self._on_volume_btn_clicked)

        vol_box.append(self.volume_btn)
        vol_box.append(self.volume_value_label)
        right_controls_box.append(vol_box)

        # --- Speed button with popover ---
        speed_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.speed_value_label = Gtk.Label(label="1.00x")
        self.speed_value_label.add_css_class("caption")

        self.speed_btn = Gtk.Button()
        self.speed_btn.set_icon_name("speedometer-symbolic")
        self.speed_btn.add_css_class("flat")
        self.speed_btn.add_css_class("circular")
        self.speed_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Playback speed")],
        )

        self.speed_popover = Gtk.Popover()
        self.speed_popover.set_parent(self.speed_btn)
        self.speed_popover.set_position(Gtk.PositionType.TOP)
        self.speed_popover.add_css_class("dark-popover")

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

        self._speed_hover_close_timer = None
        spd_hover_ctrl = Gtk.EventControllerMotion()
        spd_hover_ctrl.connect("enter", self._on_speed_btn_hover_enter)
        spd_hover_ctrl.connect("leave", self._on_speed_btn_hover_leave)
        speed_box.add_controller(spd_hover_ctrl)

        spd_pop_hover_ctrl = Gtk.EventControllerMotion()
        spd_pop_hover_ctrl.connect("enter", self._on_speed_popover_hover_enter)
        spd_pop_hover_ctrl.connect("leave", self._on_speed_popover_hover_leave)
        self.speed_popover.add_controller(spd_pop_hover_ctrl)

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
        self.zoom_popover.add_css_class("dark-popover")

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

        # Auto-close popover after a delay when value changes
        self._zoom_close_timer = None
        self._zoom_hover_close_timer = None

        # Hover behavior: show on mouse enter, auto-close on mouse leave
        zoom_hover_ctrl = Gtk.EventControllerMotion()
        zoom_hover_ctrl.connect("enter", self._on_zoom_btn_hover_enter)
        zoom_hover_ctrl.connect("leave", self._on_zoom_btn_hover_leave)
        self.zoom_btn.add_controller(zoom_hover_ctrl)

        # Also track hover on the popover itself to keep it open
        popover_hover_ctrl = Gtk.EventControllerMotion()
        popover_hover_ctrl.connect("enter", self._on_zoom_popover_hover_enter)
        popover_hover_ctrl.connect("leave", self._on_zoom_popover_hover_leave)
        self.zoom_popover.add_controller(popover_hover_ctrl)

        # Click still works for touchscreen
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
                GLib.idle_add(self._update_paned_for_cut_mode, False)

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
                GLib.idle_add(self._setup_visualizer_tooltip)
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
        """Start conversion process."""
        if not self.file_queue.has_files():
            self._show_error_dialog(
                _("No files to convert"), _("Please add at least one file to convert.")
            )
            return

        # Collect settings
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
                if file_path != self.active_audio_id:
                    if order_by_number and markers:
                        # Sort by segment number if that option is selected
                        if "segment_index" in markers[0]:
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

        # Create a progress dialog
        files = self.file_queue.get_files()
        total_files = len(files)

        # Create a box for the progress bar
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.set_margin_start(20)
        content_box.set_margin_end(20)

        # Create progress bar
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_fraction(0.0)
        content_box.append(self.progress_bar)

        # Create the dialog with our custom content
        self.progress_dialog = Adw.MessageDialog(
            transient_for=self,
            title=_("Converting Files"),
            body=_("Converting file 1 of {0}").format(total_files),
        )

        # Set the extra child (content area)
        self.progress_dialog.set_extra_child(content_box)

        # Add a cancel button
        self.progress_dialog.add_response("cancel", _("Cancel"))
        self.progress_dialog.connect("response", self._on_conversion_cancel)

        # Show the dialog
        self.progress_dialog.present()

        # Start conversion in a separate thread
        threading.Thread(
            target=self.converter.convert_all_files,
            args=(
                files,
                settings,
                self.on_conversion_progress,
                self.on_conversion_finished,
            ),
            daemon=True,
        ).start()

    def _on_conversion_cancel(self, dialog, response):
        """Handle cancel button in the conversion dialog."""
        if response == "cancel":
            # Cancel the conversion
            self.converter.cancel_conversion()
            # Close the dialog
            self.progress_dialog.close()

    def on_conversion_progress(self, file_index, file_path, progress):
        """Update conversion progress."""
        # Update queue item progress
        GLib.idle_add(self.file_queue.update_progress, file_index, progress)

        # Update progress dialog
        total_files = len(self.file_queue.get_files())
        filename = os.path.basename(file_path)

        # Calculate overall progress (current file index + progress within current file)
        overall_progress = (file_index + progress) / total_files

        # Format percentage for display
        percent = int(overall_progress * 100)

        def update_progress_ui():
            try:
                if hasattr(self, "progress_dialog") and self.progress_dialog:
                    # Check if dialog still exists and is valid
                    if hasattr(self.progress_bar, "set_fraction"):
                        # Ensure progress is between 0 and 1
                        safe_progress = max(0, min(1, overall_progress))
                        self.progress_bar.set_fraction(safe_progress)

                        # Update dialog message with percentage
                        self.progress_dialog.set_body(
                            _(
                                "Converting file {0} of {1} ({2}%)\nCurrent file: {3}"
                            ).format(file_index + 1, total_files, percent, filename)
                        )
            except Exception as e:
                logger.error(f"Error updating progress UI: {e}")
            return False  # Run once, don't repeat

        GLib.idle_add(update_progress_ui)

    def on_conversion_finished(self, success, error_message=None, converted_files=None):
        """Handle conversion completion."""
        # Close the progress dialog
        if hasattr(self, "progress_dialog"):
            self.progress_dialog.close()

        if success:
            # Remove successfully converted files from the queue
            if converted_files:
                self._remove_converted_files(converted_files)

            # Show improved success dialog
            GLib.idle_add(
                self._show_conversion_success_dialog,
                len(converted_files) if converted_files else 0,
                converted_files,
            )
        else:
            GLib.idle_add(
                self._show_error_dialog,
                _("Conversion Error"),
                error_message or _("An error occurred during conversion."),
            )

    def _remove_converted_files(self, converted_files):
        """Remove successfully converted files from the queue."""
        if not converted_files or not hasattr(self.file_queue, "get_files"):
            return

        # Get all files in the queue
        queue_files = self.file_queue.get_files()

        # Find file indexes to remove (in reverse order to avoid index shifting)
        to_remove = []
        for file_path in converted_files:
            try:
                idx = queue_files.index(file_path)
                to_remove.append(idx)
            except ValueError:
                # File not in queue
                pass

        # Sort in reverse order to remove from end first
        to_remove.sort(reverse=True)

        # Remove each file
        for idx in to_remove:
            GLib.idle_add(self.file_queue.remove_file, idx)

    def _show_conversion_success_dialog(self, file_count, converted_files=None):
        """Show an improved success dialog after conversion."""
        # Create content box
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_start(20)
        content_box.set_margin_end(20)
        content_box.set_margin_top(20)
        content_box.set_margin_bottom(20)

        # Success icon
        icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        icon.set_pixel_size(48)
        icon.add_css_class("success")
        icon.set_halign(Gtk.Align.CENTER)
        content_box.append(icon)

        # Success message
        if file_count > 1:
            message = _("Successfully converted {} files").format(file_count)
        else:
            message = _("Successfully converted 1 file")

        message_label = Gtk.Label()
        message_label.set_markup(f"<span size='large' weight='bold'>{message}</span>")
        message_label.set_halign(Gtk.Align.CENTER)
        message_label.set_margin_top(12)
        content_box.append(message_label)

        # Get output folder path from first converted file
        output_folder = None
        if converted_files and len(converted_files) > 0:
            # Assuming converted files are in the same folder or we use the first one's folder
            first_file = converted_files[0]
            output_folder = os.path.dirname(first_file)

        # Create dialog
        dialog = Adw.AlertDialog()
        dialog.set_heading(_("Conversion Complete"))
        dialog.set_extra_child(content_box)

        # Add buttons
        if output_folder:
            dialog.add_response("open_folder", _("Open Folder"))
            dialog.set_response_appearance(
                "open_folder", Adw.ResponseAppearance.SUGGESTED
            )
            dialog.add_response("close", _("Close"))
            dialog.set_default_response("open_folder")
        else:
            dialog.add_response("close", _("Close"))
            dialog.set_response_appearance("close", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("close")

        dialog.set_close_response("close")

        # Connect response handler
        def on_response(dlg, response):
            if response == "open_folder" and output_folder:
                # Open the output folder in file manager
                try:
                    Gio.AppInfo.launch_default_for_uri(f"file://{output_folder}", None)
                except Exception as e:
                    logger.error(f"Failed to open folder: {e}")

        dialog.connect("response", on_response)
        dialog.present(self)

    def _show_error_dialog(self, title, message):
        """Show an error dialog."""
        dialog = Adw.MessageDialog(transient_for=self, title=title, body=message)
        dialog.add_response("ok", _("OK"))
        dialog.present()

    def _show_info_dialog(self, title, message):
        """Show an information dialog."""
        dialog = Adw.MessageDialog(transient_for=self, title=title, body=message)
        dialog.add_response("ok", _("OK"))
        dialog.present()

    def on_close_request(self, window):
        """Handle window close event."""
        # Save current window state before closing
        if not self.is_maximized() and hasattr(self.app, "config") and self.app.config:
            # Save the size if not maximized
            self._save_window_size()

        # Flush pending config changes to disk
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.flush()

        # Stop any playing audio
        self.player.stop()
        # Clean up resources
        self.converter.cleanup()
        return False

    def _on_sidebar_width_changed(self, paned, param):
        """Handle sidebar width changes and save to config."""
        width = paned.get_position()

        # Store this as a manually set width
        self._manual_sidebar_width = width

        # Get the total width of the window
        total_width = self.get_width()

        # Calculate the minimum width for right side (400px)
        right_min_width = 400

        # Calculate maximum allowed position to ensure right side has enough space
        max_position = total_width - right_min_width

        # If the position exceeds the maximum, adjust it
        if width > max_position and max_position > 350:
            # Only adjust if max_position is reasonable (above minimum sidebar width)
            paned.set_position(max_position)
            width = max_position
        # If position is below minimum sidebar width, adjust it
        elif width < 350:
            paned.set_position(350)
            width = 350

        # Proceed with debouncing for saving settings
        # Cancel any existing save timeout
        if self._sidebar_save_timeout_id:
            try:
                GLib.source_remove(self._sidebar_save_timeout_id)
            except Exception:
                pass  # Source was already removed
            self._sidebar_save_timeout_id = None

        # Set new timeout to save after resize is completed (500ms of inactivity)
        self._sidebar_save_timeout_id = GLib.timeout_add(
            500, self._save_sidebar_width, width
        )

    def _save_sidebar_width(self, width):
        """Save the sidebar width to config after resize is completed."""
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("sidebar_width", str(width))
        self._sidebar_save_timeout_id = None
        return False  # Don't repeat the timeout

    def _on_window_size_changed(self, window, param):
        """Handle window size changes."""
        # Only save size if the window is not maximized
        if not self.is_maximized():
            # Preserve the sidebar width by updating the split_view position
            if hasattr(self, "split_view") and hasattr(self, "_manual_sidebar_width"):
                # Set the position to maintain the sidebar width
                self.split_view.set_position(self._manual_sidebar_width)

            # Debounce to avoid saving while resizing
            if hasattr(self, "_size_save_timeout_id") and self._size_save_timeout_id:
                try:
                    GLib.source_remove(self._size_save_timeout_id)
                except Exception:
                    pass  # Source was already removed

            self._size_save_timeout_id = GLib.timeout_add(500, self._save_window_size)

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
            GLib.timeout_add(200, self._fix_layout_after_unmaximize)

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
        GLib.idle_add(self._restore_geometry)
        return False

    def _restore_geometry(self):
        """Restore sidebar width and visualizer height after window is shown."""
        # Restore manual sidebar width
        if hasattr(self, "_manual_sidebar_width") and hasattr(self, "split_view"):
            self.split_view.set_position(self._manual_sidebar_width)

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
            GLib.idle_add(self._update_paned_for_cut_mode, False)

        # Visualizer height is managed by GTK Box layout, no need to set content_height here

        return False  # Don't repeat

    def on_clear_queue(self, button):
        """Show confirmation dialog before clearing the queue."""
        dialog = Adw.MessageDialog(
            transient_for=self,
            title=_("Clear Queue"),
            body=_("Are you sure you want to clear the queue?"),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("clear", _("Clear"))
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_clear_queue_response)
        dialog.present()

    def _on_clear_queue_response(self, dialog, response):
        """Handle clear queue dialog response."""
        if response == "clear":
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
            parent=self, cancellable=None, callback=self._on_open_files_complete
        )

    def _on_open_files_complete(self, dialog, result):
        """Handle completion of file open dialog."""
        try:
            files = dialog.open_multiple_finish(result)
            if files and files.get_n_items() > 0:
                # Show busy cursor
                self._set_busy_cursor(True)

                # Collect file paths first
                file_paths = []
                for i in range(files.get_n_items()):
                    file = files.get_item(i)
                    if isinstance(file, Gio.File):
                        file_paths.append(file.get_path())

                # Add diagnostic timing information
                total_files = len(file_paths)
                logger.info(f"Starting import of {total_files} files")

                # First, try to suspend UI updates in file queue if possible
                has_suspend_method = hasattr(self.file_queue, "suspend_updates")
                has_resume_method = hasattr(self.file_queue, "resume_updates")

                if has_suspend_method:
                    self.file_queue.suspend_updates()

                # Process files with timing
                import time

                start_time = time.time()

                try:
                    # Add files to queue
                    for i, path in enumerate(file_paths):
                        file_start = time.time()
                        self.file_queue.add_file(path)
                        file_time = time.time() - file_start

                        # Log slow file additions (taking more than 100ms)
                        if file_time > 0.1:
                            logger.warning(
                                f"Slow file addition: {path} took {file_time:.2f}s"
                            )

                        # Log progress periodically
                        if i > 0 and (i % 10 == 0 or i == total_files - 1):
                            elapsed = time.time() - start_time
                            rate = (i + 1) / elapsed if elapsed > 0 else 0
                            logger.info(
                                f"Added {i + 1}/{total_files} files, {rate:.1f} files/sec"
                            )
                finally:
                    # Resume UI updates
                    if has_resume_method:
                        self.file_queue.resume_updates()

                    # Total time
                    total_time = time.time() - start_time
                    logger.info(
                        f"Total import time: {total_time:.2f}s for {total_files} files ({total_files / total_time:.1f} files/sec)"
                    )

                    # Restore cursor
                    self._set_busy_cursor(False)

        except GLib.Error as error:
            if not error.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                logger.error(f"Error opening files: {error.message}")

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
