# app/ui/main_window.py

"""
Main Window for the Audio Converter application.
"""

import gettext
import gi

gettext.textdomain("big-audio-converter")
_ = gettext.gettext
import os
import threading
import logging

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, Gio, GLib

from app.ui.file_queue import FileQueue
from app.ui.visualizer import AudioVisualizer
from app.ui.equalizer_dialog import EqualizerDialog
from app.audio import waveform
from app.utils.time_formatter import format_time_short
from app.utils.tooltip_helper import TooltipHelper

logger = logging.getLogger(__name__)


class MainWindow(Adw.ApplicationWindow):
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
                        default_width = int(saved_width)
                    except (ValueError, TypeError):
                        pass

                saved_height = config.get("window_height")
                if saved_height:
                    try:
                        default_height = int(saved_height)
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
        # For debouncing window size save
        self._size_save_timeout_id = None

        self.app = kwargs.get("application")

        # Initialize tooltip helper
        if hasattr(self.app, "config") and self.app.config:
            self.tooltip_helper = TooltipHelper(self)  # Pass the MainWindow instance
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

        # Add property to track the last manually set sidebar width
        self._manual_sidebar_width = self.sidebar_width

        # Set up GUI first, creating the visualizer
        self.setup_ui()
        self.setup_drop_target()
        self.connect("close-request", self.on_close_request)

        # Setup visualizer tooltip after UI is fully created
        if self.tooltip_helper and hasattr(self, 'visualizer'):
            GLib.idle_add(self._setup_visualizer_tooltip)

        # Connect to map event for visualizer height restoration
        self.connect("map", self.on_window_mapped)

        # Connect window state signals
        self.connect("notify::default-width", self._on_window_size_changed)
        self.connect("notify::default-height", self._on_window_size_changed)
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

    def setup_ui(self):
        """Set up the user interface."""
        # Create main vertical container to hold both paned view and visualizer
        main_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_container)

        # Use a vertical paned container to allow resizing the visualizer
        self.vertical_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        main_container.append(self.vertical_paned)
        main_container.set_vexpand(True)

        # Create top section containing the horizontal split
        top_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top_container.set_vexpand(True)

        # Create a split view that allows resizing with the mouse (for sidebar and content)
        self.split_view = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.split_view.set_position(self.sidebar_width)

        top_container.append(self.split_view)

        # Create CSS for sidebar styling
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(
            b"""
        .sidebar {
            background-color: @sidebar_bg_color;
        }
        preferencesgroup box list row {
            padding: 0px;
            margin-top: -1px;
            margin-bottom: -1px;
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
        left_content.set_margin_start(20)
        left_content.set_margin_end(20)
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

        # Create header bar for right side
        right_header = Adw.HeaderBar()
        right_header.set_show_title(True)
        # Detect window button layout and set decoration layout accordingly
        if not self._window_buttons_on_left():
            right_header.set_decoration_layout("menu:minimize,maximize,close")
        else:
            right_header.set_decoration_layout("")

        # Always create queue controls
        self.clear_queue_button = Gtk.Button()
        self.clear_queue_button.set_icon_name("edit-delete-remove")
        self.clear_queue_button.add_css_class(
            "flat"
        )  # Flat style to match headerbar buttons
        self.clear_queue_button.add_css_class("circular")
        self.clear_queue_button.set_valign(Gtk.Align.CENTER)

        self.clear_queue_button.connect("clicked", self.on_clear_queue)
        self.clear_queue_button.add_css_class("destructive-action")
        self.clear_queue_button.set_visible(False)  # Initially hidden
        self.header_queue_size_label = Gtk.Label(label=_("0 files"))
        self.header_queue_size_label.add_css_class("caption")
        self.header_queue_size_label.add_css_class("dim-label")
        self.header_queue_size_label.set_visible(False)
        self.header_queue_size_label.set_margin_start(4)
        self.header_queue_size_label.set_margin_end(8)
        self.header_queue_size_label.set_valign(Gtk.Align.CENTER)
        # Only add queue controls to right headerbar if window buttons are on the left

        # Create menu button and add to right side of header directly
        menu = Gio.Menu()
        menu.append(_("Show Welcome Screen"), "app.show-welcome")
        menu.append(_("About"), "app.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        # If window buttons are on the left, add app icon after menu button (rightmost)
        if window_buttons_left:
            icon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            icon_box.set_halign(Gtk.Align.END)
            icon_box.set_valign(Gtk.Align.CENTER)
            icon_box.append(menu_button)
            app_icon = Gtk.Image.new_from_icon_name("big-audio-converter")
            app_icon.set_pixel_size(20)
            app_icon.set_halign(Gtk.Align.END)
            app_icon.set_valign(Gtk.Align.CENTER)
            icon_box.append(app_icon)
            right_header.pack_end(icon_box)
        else:
            right_header.pack_end(menu_button)

        # Left section for queue controls - pack directly to headerbar
        left_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        #  add left margin
        left_controls.set_margin_start(20)
        left_controls.set_halign(Gtk.Align.START)

        # Add clear queue button to left controls (first)
        left_controls.append(self.clear_queue_button)

        # Add queue size label to left controls (second)
        left_controls.append(self.header_queue_size_label)

        # Pack left controls at the start of the headerbar (left side)
        right_header.pack_start(left_controls)

        # Center section for buttons - this goes in title_widget to stay centered
        center_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        center_box.set_halign(Gtk.Align.CENTER)

        # Add Files button
        add_files_button = Gtk.Button(label=_("Add Files"))
        add_files_button.connect("clicked", self.on_add_files)
        add_files_button.add_css_class("suggested-action")
        center_box.append(add_files_button)

        # Convert button
        self.convert_button = Gtk.Button(label=_("Convert"))
        self.convert_button.connect("clicked", self.on_convert)
        self.convert_button.add_css_class("suggested-action")
        self.convert_button.set_visible(False)  # Initially hidden
        center_box.append(self.convert_button)

        # Set center_box as the title widget (stays centered between packed widgets)
        right_header.set_title_widget(center_box)
        right_box.add_top_bar(right_header)

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

        # Add the two views to the paned container (swapped order)
        self.split_view.set_start_child(left_box)
        self.split_view.set_end_child(right_box)

        # Connect to position changes to save sidebar width
        self.split_view.connect("notify::position", self._on_sidebar_width_changed)

        # Add top container to the vertical paned view
        self.vertical_paned.set_start_child(top_container)

        # Add visualizer at the bottom spanning full width
        # Create the visualizer instance
        self.visualizer = AudioVisualizer()

        # Give visualizer reference to player for checking playback state
        self.visualizer.player = self.player

        # Create container for visualizer and zoom controls
        visualizer_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Create zoom control bar
        zoom_control_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        zoom_control_box.set_margin_start(15)
        zoom_control_box.set_margin_end(15)

        # Add "Only the Selected Area" switch - when active, playback automatically skips non-selected parts
        play_selection_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        play_selection_box.set_valign(Gtk.Align.CENTER)
        play_selection_label = Gtk.Label(label=_("Only the Selected Area"))
        play_selection_box.append(play_selection_label)

        self.play_selection_switch = Gtk.Switch()
        self.play_selection_switch.set_active(False)
        self.play_selection_switch.connect(
            "notify::active", self._on_play_selection_switch_changed
        )
        self.play_selection_switch.set_sensitive(
            False
        )  # Disabled until markers are set
        play_selection_box.append(self.play_selection_switch)
        
        # Store reference to box for tooltip (apply to entire control)
        self.play_selection_box = play_selection_box

        zoom_control_box.append(play_selection_box)

        # Add "Auto-Advance" switch - when active, automatically plays next track in queue
        auto_advance_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        auto_advance_box.set_valign(Gtk.Align.CENTER)
        auto_advance_label = Gtk.Label(label=_("Auto-Advance"))
        auto_advance_box.append(auto_advance_label)

        self.auto_advance_switch = Gtk.Switch()
        # Load saved setting from config
        auto_advance_enabled = self.app.config.get("auto_advance_enabled", True)
        self.auto_advance_switch.set_active(auto_advance_enabled)
        self.auto_advance_switch.connect(
            "notify::active", self._on_auto_advance_switch_changed
        )
        auto_advance_box.append(self.auto_advance_switch)
        
        # Store reference to box for tooltip (apply to entire control)
        self.auto_advance_box = auto_advance_box

        zoom_control_box.append(auto_advance_box)

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
        self.pause_play_btn.set_tooltip_text(_("Play"))
        self.pause_play_btn.connect("clicked", self._on_pause_play_clicked)
        playback_controls_box.append(self.pause_play_btn)

        # Next audio button (right side)
        self.next_audio_btn = Gtk.Button()
        self.next_audio_btn.set_icon_name("media-skip-forward-symbolic")
        self.next_audio_btn.add_css_class("flat")
        self.next_audio_btn.add_css_class("circular")
        self.next_audio_btn.connect(
            "clicked", lambda btn: self._on_next_audio_clicked()
        )
        self.next_audio_btn.set_visible(False)  # Initially hidden
        playback_controls_box.append(self.next_audio_btn)

        # Add the centered playback controls to the main box
        zoom_control_box.append(playback_controls_box)

        # Add zoom label
        zoom_label = Gtk.Label(label=_("Zoom:"))
        zoom_label.set_halign(Gtk.Align.START)
        zoom_control_box.append(zoom_label)

        # Add zoom slider with logarithmic scale (0-150 slider maps to 1x-1000x zoom)
        # Using linear slider 0-150, will convert logarithmically in handler
        self.zoom_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.0, 150.0, 0.1
        )
        self.zoom_scale.set_value(0.0)  # Start at 0 (maps to 1x zoom)
        self.zoom_scale.set_draw_value(True)
        self.zoom_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.zoom_scale.set_hexpand(False)  # Don't expand horizontally
        self.zoom_scale.set_size_request(200, -1)

        # Format the value display to show actual zoom level with "x" suffix
        self.zoom_scale.set_format_value_func(self._format_zoom_value)
        self.zoom_scale.connect("value-changed", self._on_zoom_scale_changed)

        # No marks on the slider for cleaner appearance
        zoom_control_box.append(self.zoom_scale)

        # Store reference to zoom control box for showing/hiding
        self.zoom_control_box = zoom_control_box

        # Hide controls initially until waveform is loaded
        zoom_control_box.set_visible(False)

        visualizer_container.append(zoom_control_box)

        # Create a frame around the visualizer for better appearance
        visualizer_frame = Gtk.Frame()
        visualizer_frame.set_margin_start(10)
        visualizer_frame.set_margin_end(10)
        visualizer_frame.set_margin_top(0)
        visualizer_frame.set_margin_bottom(10)

        # Set minimum height instead of fixed size
        visualizer_frame.set_size_request(-1, 100)  # Min height 100px

        # Connect seek handler before adding the visualizer to the frame
        self.visualizer.connect_seek_handler(self.on_visualizer_seek)

        # Connect marker drag handler to track when markers are being dragged
        self.visualizer.connect_marker_drag_handler(self._on_marker_drag_state_changed)

        # Connect zoom change handler to update slider
        self.visualizer.zoom_changed_callback = self._on_visualizer_zoom_changed

        # Connect marker update handler to refresh selection playback
        self.visualizer.marker_updated_callback = self._on_markers_updated

        # Set content height for the visualizer based on saved/default height
        self.visualizer.set_content_height(self.visualizer_height)

        # Make sure the visualizer can receive mouse events and expand with parent
        self.visualizer.set_vexpand(True)  # Allow expansion
        self.visualizer.set_focusable(True)

        # Add the visualizer directly to the frame (no ScrolledWindow for performance)
        visualizer_frame.set_child(self.visualizer)

        # Add visualizer frame to container
        visualizer_container.append(visualizer_frame)

        # Add visualizer container to the bottom part of the vertical paned
        self.vertical_paned.set_end_child(visualizer_container)

        # Set initial position, but don't rely on it for final height
        # We'll adjust this after the window is mapped
        self.vertical_paned.set_position(300)  # Use a reasonable initial position

        # Connect to position changes to save visualizer height
        self.vertical_paned.connect(
            "notify::position", self._on_visualizer_height_changed
        )

    def update_queue_size_label(self, count=None, text=None):
        """Update the queue size label in the header."""
        # Get count if not provided
        if count is None and hasattr(self.file_queue, "get_queue_size"):
            count = self.file_queue.get_queue_size()

        # Get text if not provided
        if text is None:
            text = self.file_queue.get_queue_size_text()

        # Update the label text
        self.header_queue_size_label.set_text(text)

        # Show/hide elements based on file count
        has_files = count > 0
        has_multiple_files = count >= 2

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

    def setup_conversion_options(self, parent_box):
        """Set up the conversion options UI."""
        # Create container to center the options group
        options_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        options_container.set_valign(Gtk.Align.CENTER)
        options_container.set_hexpand(True)
        options_container.set_margin_bottom(8)  # Add bottom margin

        # Create a single preference group for all options
        options_group = Adw.PreferencesGroup()

        # Format selection
        format_row = Adw.ActionRow(title=_("Output Format"))
        self.format_combo = Gtk.ComboBoxText()
        self.format_combo.set_valign(Gtk.Align.CENTER)
        formats = ["copy", "mp3", "ogg", "flac", "wav", "aac", "opus"]
        for fmt in formats:
            self.format_combo.append_text(fmt)
        # Connect format change to save settings
        self.format_combo.connect("changed", self._on_format_changed)
        format_row.add_suffix(self.format_combo)
        options_group.add(format_row)

        # Bitrate selection
        bitrate_row = Adw.ActionRow(title=_("Bitrate"))
        self.bitrate_combo = Gtk.ComboBoxText()
        self.bitrate_combo.set_valign(Gtk.Align.CENTER)
        bitrates = ["32k", "64k", "128k", "192k", "256k", "320k"]
        for br in bitrates:
            self.bitrate_combo.append_text(br)
        # Connect bitrate change to save settings
        self.bitrate_combo.connect("changed", self._on_bitrate_changed)
        bitrate_row.add_suffix(self.bitrate_combo)
        options_group.add(bitrate_row)
        self.bitrate_row = bitrate_row  # Store reference for disabling

        # Volume adjustment - replace Scale with SpinRow
        self.volume_spin = Adw.SpinRow.new_with_range(0, 500, 5)
        self.volume_spin.set_title(_("Volume"))
        self.volume_spin.set_value(100)  # Default to 100%
        self.volume_spin.connect("changed", self._on_volume_spin_changed)
        options_group.add(self.volume_spin)

        # Speed adjustment - replace Scale with SpinRow
        self.speed_spin = Adw.SpinRow.new_with_range(0.5, 5.0, 0.05)
        self.speed_spin.set_title(_("Speed"))
        self.speed_spin.set_digits(2)  # Show 2 decimal places
        self.speed_spin.set_value(1.0)  # Default to normal speed
        self.speed_spin.connect("changed", self._on_speed_spin_changed)
        options_group.add(self.speed_spin)

        # Noise reduction
        noise_row = Adw.ActionRow(
            title=_("Noise Reduction")
        )
        self.noise_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        # Connect noise reduction switch to player and settings
        self.noise_switch.connect("state-set", self._on_noise_switch_changed)
        noise_row.add_suffix(self.noise_switch)
        options_group.add(noise_row)
        self.noise_row = noise_row  # Store reference for disabling

        # Waveform generation toggle
        waveform_row = Adw.ActionRow(title=_("Generate Waveforms"))
        self.waveform_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.waveform_switch.set_active(True)  # Default to enabled
        # Connect waveform switch to save settings
        self.waveform_switch.connect("state-set", self._on_waveform_switch_changed)
        waveform_row.add_suffix(self.waveform_switch)
        options_group.add(waveform_row)

        # Mouseover tips toggle
        tips_row = Adw.ActionRow(title=_("Show help on hover"))
        self.tips_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        # Load saved state (default to True)
        if hasattr(self.app, "config") and self.app.config:
            tips_enabled = self.app.config.get("show_mouseover_tips", "true").lower() == "true"
            self.tips_switch.set_active(tips_enabled)
        else:
            self.tips_switch.set_active(True)
        # Connect tips switch to save settings and refresh tooltips
        self.tips_switch.connect("state-set", self._on_tips_switch_changed)
        tips_row.add_suffix(self.tips_switch)
        options_group.add(tips_row)

        # Equalizer - moved from effects group
        self.eq_row = Adw.ActionRow(title=_("Equalizer"))
        eq_button = Gtk.Button(label=_("Configure..."))
        eq_button.set_valign(Gtk.Align.CENTER)
        eq_button.connect("clicked", self.on_configure_equalizer)
        self.eq_row.add_suffix(eq_button)
        options_group.add(self.eq_row)

        # Replace the cut audio switch with a combo box
        cut_row = Adw.ActionRow(title=_("Cut"))

        # Add box to contain combo and help button side by side
        cut_suffix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)



        cut_row.add_suffix(cut_suffix_box)

        self.cut_combo = Gtk.ComboBoxText()
        self.cut_combo.set_valign(Gtk.Align.CENTER)
        self.cut_combo.append_text(_("Off"))
        self.cut_combo.append_text(_("Chronological"))
        self.cut_combo.append_text(_("Segment Number"))
        self.cut_combo.set_active(1)  # Default to Chronological
        self.cut_combo.connect("changed", self._on_cut_combo_changed)
        cut_suffix_box.append(self.cut_combo)

        options_group.add(cut_row)

        # Create simplified box for cut instructions (initially hidden)
        self.cut_options_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.cut_options_box.set_margin_start(12)
        self.cut_options_box.set_visible(False)  # Hidden by default

        # Remove help labels - they'll now be shown in a dialog instead

        # Add cutting options below the group
        options_container.append(self.cut_options_box)

        # Add group to centering container
        options_container.append(options_group)

        # Add container to parent
        parent_box.append(options_container)

        # Apply tooltips to UI elements
        self._apply_tooltips()

        # Restore saved settings after UI is created
        self._restore_conversion_settings()

    def _apply_tooltips(self):
        """Apply tooltips to UI elements."""
        if not self.tooltip_helper:
            return
        
        # Add tooltips to format combo parent row
        if hasattr(self, 'format_combo'):
            parent = self.format_combo.get_parent()
            while parent and not isinstance(parent, Adw.ActionRow):
                parent = parent.get_parent()
            if parent:
                self.tooltip_helper.add_tooltip(parent, "format")
        
        # Add tooltip to bitrate row
        if hasattr(self, 'bitrate_row'):
            self.tooltip_helper.add_tooltip(self.bitrate_row, "bitrate")
        
        # Add tooltip to volume spin
        if hasattr(self, 'volume_spin'):
            self.tooltip_helper.add_tooltip(self.volume_spin, "volume")
        
        # Add tooltip to speed spin
        if hasattr(self, 'speed_spin'):
            self.tooltip_helper.add_tooltip(self.speed_spin, "speed")
        
        # Add tooltip to noise row
        if hasattr(self, 'noise_row'):
            self.tooltip_helper.add_tooltip(self.noise_row, "noise_reduction")
        
        # Add tooltip to waveform row
        if hasattr(self, 'waveform_switch'):
            parent = self.waveform_switch.get_parent()
            while parent and not isinstance(parent, Adw.ActionRow):
                parent = parent.get_parent()
            if parent:
                self.tooltip_helper.add_tooltip(parent, "waveform")
        
        # Add tooltip to equalizer row
        if hasattr(self, 'eq_row'):
            self.tooltip_helper.add_tooltip(self.eq_row, "equalizer")
        
        # Add tooltip to cut combo parent row
        if hasattr(self, 'cut_combo'):
            parent = self.cut_combo.get_parent()
            while parent and not isinstance(parent, Adw.ActionRow):
                parent = parent.get_parent()
            if parent:
                self.tooltip_helper.add_tooltip(parent, "cut")
        
        # Add tooltip to tips switch row
        if hasattr(self, 'tips_switch'):
            parent = self.tips_switch.get_parent()
            while parent and not isinstance(parent, Adw.ActionRow):
                parent = parent.get_parent()
            if parent:
                self.tooltip_helper.add_tooltip(parent, "mouseover_tips")
        
        # Add tooltips to headerbar controls
        if hasattr(self, 'clear_queue_button'):
            self.tooltip_helper.add_tooltip(self.clear_queue_button, "clear_queue_button")
        if hasattr(self, 'prev_audio_btn'):
            self.tooltip_helper.add_tooltip(self.prev_audio_btn, "prev_audio_btn")
        if hasattr(self, 'pause_play_btn'):
            self.tooltip_helper.add_tooltip(self.pause_play_btn, "pause_play_btn")
        if hasattr(self, 'next_audio_btn'):
            self.tooltip_helper.add_tooltip(self.next_audio_btn, "next_audio_btn")
        # Apply tooltips to box containers
        if hasattr(self, 'play_selection_box'):
            self.tooltip_helper.add_tooltip(self.play_selection_box, "play_selection_switch")
        if hasattr(self, 'auto_advance_box'):
            self.tooltip_helper.add_tooltip(self.auto_advance_box, "auto_advance_switch")

    def _on_tips_switch_changed(self, switch, state):
        """Handle mouseover tips toggle and save setting."""
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("show_mouseover_tips", "true" if state else "false")
        
        if state:
            # When enabling tooltips, re-apply all of them
            self._apply_tooltips()
            # Also setup visualizer tooltip
            if hasattr(self, 'visualizer'):
                GLib.idle_add(self._setup_visualizer_tooltip)
        else:
            # When disabling tooltips, refresh to hide them
            if self.tooltip_helper:
                self.tooltip_helper.refresh_all()
            # Hide visualizer tooltip
            if hasattr(self, 'visualizer'):
                self._hide_visualizer_tooltip()
        
        return False

    def _setup_visualizer_tooltip(self):
        """Setup tooltip for the waveform visualizer (special handling for DrawingArea)."""
        from app.utils.tooltip_helper import TOOLTIPS
        
        if not self.tooltip_helper or not self.tooltip_helper.is_enabled():
            return False
        
        tooltip_text = TOOLTIPS.get("waveform_visualizer")
        if not tooltip_text or not hasattr(self, 'visualizer'):
            return False
        
        # Create a container box that will hold both visualizer and tooltip
        # Get the visualizer's current parent
        visualizer_parent = self.visualizer.get_parent()
        if not visualizer_parent:
            return False
        
        # Create popover for visualizer with proper positioning
        self.visualizer_tooltip_popover = Gtk.Popover()
        self.visualizer_tooltip_popover.set_autohide(False)
        self.visualizer_tooltip_popover.set_position(Gtk.PositionType.TOP)
        # Set parent to the visualizer's parent container, not the visualizer itself
        self.visualizer_tooltip_popover.set_parent(self.visualizer)
        
        # Create label with tooltip text
        label = Gtk.Label()
        label.set_text(tooltip_text)
        label.set_wrap(True)
        label.set_max_width_chars(60)
        label.set_margin_start(12)
        label.set_margin_end(12)
        label.set_margin_top(8)
        label.set_margin_bottom(8)
        label.set_halign(Gtk.Align.START)
        self.visualizer_tooltip_popover.set_child(label)
        
        # Initialize tooltip state
        self.visualizer_tooltip_timer = None
        self.visualizer_tooltip_active = False
        
        # Use the tooltip helper's approach: add a motion controller
        # that won't interfere with visualizer's existing controllers
        motion_controller = Gtk.EventControllerMotion.new()
        motion_controller.connect(
            "enter", lambda c, x, y: self._schedule_visualizer_tooltip()
        )
        motion_controller.connect(
            "leave", lambda c: self._hide_visualizer_tooltip()
        )
        self.visualizer.add_controller(motion_controller)
        
        return False  # Don't repeat idle_add
    
    def _schedule_visualizer_tooltip(self):
        """Schedule visualizer tooltip to appear after delay."""
        if not self.tooltip_helper or not self.tooltip_helper.is_enabled():
            return
        
        # Cancel any existing timer
        if self.visualizer_tooltip_timer:
            GLib.source_remove(self.visualizer_tooltip_timer)
        
        # Schedule tooltip after 200ms
        self.visualizer_tooltip_timer = GLib.timeout_add(
            200, self._show_visualizer_tooltip_animated
        )
    
    def _show_visualizer_tooltip_animated(self):
        """Show visualizer tooltip with fade-in animation."""
        if not hasattr(self, 'visualizer_tooltip_popover'):
            return False
        
        self.visualizer_tooltip_active = True
        
        # Set initial opacity
        self.visualizer_tooltip_popover.set_opacity(0.0)
        
        # Show popover
        self.visualizer_tooltip_popover.popup()
        
        # Animate opacity
        self._animate_visualizer_opacity(0.0, 1.0, 200)
        
        return False
    
    def _animate_visualizer_opacity(self, start, end, duration_ms):
        """Animate visualizer tooltip opacity."""
        steps = 20
        step_duration = duration_ms // steps
        increment = (end - start) / steps
        current_step = [0]
        
        def update():
            if not hasattr(self, 'visualizer_tooltip_popover') or not self.visualizer_tooltip_active:
                return False
            
            current_step[0] += 1
            new_opacity = start + (increment * current_step[0])
            
            if current_step[0] >= steps:
                self.visualizer_tooltip_popover.set_opacity(end)
                return False
            else:
                self.visualizer_tooltip_popover.set_opacity(new_opacity)
                return True
        
        GLib.timeout_add(step_duration, update)
    
    def _hide_visualizer_tooltip(self):
        """Hide visualizer tooltip."""
        self.visualizer_tooltip_active = False
        
        # Cancel pending timer
        if self.visualizer_tooltip_timer:
            GLib.source_remove(self.visualizer_tooltip_timer)
            self.visualizer_tooltip_timer = None
        
        # Hide popover
        if hasattr(self, 'visualizer_tooltip_popover'):
            self.visualizer_tooltip_popover.popdown()

    def _on_format_changed(self, combo):
        """Handle format selection change and save setting."""
        if hasattr(self.app, "config") and self.app.config:
            selected_format = combo.get_active_text()
            if selected_format:
                self.app.config.set("conversion_format", selected_format)

                # Handle copy mode special case
                if selected_format == "copy":
                    # Disable quality/speed/volume/noise/equalizer controls
                    self._set_copy_mode_ui(True)
                else:
                    # Enable controls for normal conversion
                    self._set_copy_mode_ui(False)

    def _on_bitrate_changed(self, combo):
        """Handle bitrate selection change and save setting."""
        if hasattr(self.app, "config") and self.app.config:
            selected_bitrate = combo.get_active_text()
            if selected_bitrate:
                self.app.config.set("conversion_bitrate", selected_bitrate)

    def _on_volume_spin_changed(self, spin):
        """Handle volume spin change and save setting."""
        volume = spin.get_value()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("conversion_volume", str(volume))

        # Also update player volume (original functionality)
        player_volume = volume / 100.0
        if player_volume > 1.0:
            player_volume = (
                1.0 + (player_volume - 1.0) * 0.5
            )  # Scale values above 100% appropriately
        self.player.set_volume(player_volume)

    def _on_speed_spin_changed(self, spin):
        """Handle playback speed spin change and save setting."""
        speed = spin.get_value()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("conversion_speed", str(speed))

        # Also update player speed (original functionality)
        self.player.set_playback_speed(speed)
        self.player.set_pitch_correction(True)

    def _on_noise_switch_changed(self, switch, state):
        """Handle noise reduction toggle and save setting."""
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("conversion_noise_reduction", str(state).lower())

        # Update player setting (will log warning but won't actually apply in live playback)
        if hasattr(self.player, "set_noise_reduction"):
            self.player.set_noise_reduction(state)

        return False

    def _on_waveform_switch_changed(self, switch, state):
        """Handle waveform generation toggle and save setting."""
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("generate_waveforms", str(state).lower())

        # If enabling waveforms and there's an active file without waveform data, generate it
        if state and self.active_audio_id:
            # Check if visualizer has no waveform data
            if self.visualizer.waveform_data is None:
                logger.info(
                    f"Waveforms enabled, generating for active file: {self.active_audio_id}"
                )

                import threading

                threading.Thread(
                    target=waveform.generate,
                    args=(
                        self.active_audio_id,
                        self.converter,
                        self.visualizer,
                        self.file_markers,
                        self.zoom_control_box
                        if hasattr(self, "zoom_control_box")
                        else None,
                        self.file_queue.track_metadata,
                    ),
                    daemon=True,
                ).start()

        return False  # Allow the state to be changed

    def _on_cut_combo_changed(self, combo):
        """Handle cut audio combo box changes."""
        active = combo.get_active()
        # Enable markers and show options when any option except "Off" is selected
        enabled = active > 0

        # Show/hide cut options based on selection
        self.cut_options_box.set_visible(enabled)

        # Enable/disable waveform markers
        if hasattr(self, "visualizer"):
            self.visualizer.set_markers_enabled(enabled)

        # Save setting
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("cut_audio_enabled", str(enabled).lower())
            self.app.config.set("cut_audio_mode", str(active))

        return False

    # Replace or modify the former cut switch handler
    def on_convert(self, button):
        """Start conversion process."""
        if not self.file_queue.has_files():
            self._show_error_dialog(
                _("No files to convert"), _("Please add at least one file to convert.")
            )
            return

        # Collect settings
        settings = {
            "format": self.format_combo.get_active_text(),
            "bitrate": self.bitrate_combo.get_active_text(),
            "volume": self.volume_spin.get_value() / 100,
            "speed": self.speed_spin.get_value(),
            "noise_reduction": self.noise_switch.get_active(),
            "cut_enabled": self.cut_combo.get_active() > 0,
            # Pass track metadata from file queue for video track extraction
            "track_metadata": self.file_queue.track_metadata,
        }

        # Get segment ordering preference (True = by number, False = by timeline)
        order_by_number = self.cut_combo.get_active() == 2
        settings["order_by_segment_number"] = order_by_number

        # For multi-file cutting, store ALL marker information
        if settings["cut_enabled"]:
            # Add current markers if file is active (showing waveform)
            if self.active_audio_id:
                # Get ordered segments based on user preference
                print(f"Getting segments with order_by_number={order_by_number}")
                current_markers = self.visualizer.get_ordered_marker_pairs(
                    order_by_number
                )
                if current_markers:
                    print(
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
                            print(
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
                    print(
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

    def on_configure_equalizer(self, button):
        """Show equalizer configuration dialog."""
        dialog = EqualizerDialog(self, self.player)
        dialog.present()

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

    def _set_copy_mode_ui(self, is_copy_mode):
        """Enable or disable UI controls based on copy mode state."""
        # Disable/enable controls that require re-encoding
        self.bitrate_row.set_sensitive(not is_copy_mode)
        self.volume_spin.set_sensitive(not is_copy_mode)
        self.speed_spin.set_sensitive(not is_copy_mode)
        self.noise_row.set_sensitive(not is_copy_mode)
        self.eq_row.set_sensitive(not is_copy_mode)  # Disable equalizer in copy mode

    def on_close_request(self, window):
        """Handle window close event."""
        # Save current window state before closing
        if not self.is_maximized() and hasattr(self.app, "config") and self.app.config:
            # Save the size if not maximized
            self._save_window_size()

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
            except:
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
                except:
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
        if hasattr(self, "visualizer") and hasattr(self, "cut_combo"):
            self.visualizer.set_markers_enabled(self.cut_combo.get_active() > 0)

        # Use the allocation-based height for accuracy
        window_height = self.get_height()
        if window_height < 100:  # Window not properly sized yet
            return True  # Try again

        # Calculate proper position from saved visualizer height
        visualizer_position = max(200, window_height - self.visualizer_height - 50)
        self.vertical_paned.set_position(visualizer_position)

        # Update visualizer content height
        actual_visualizer_height = window_height - visualizer_position - 50
        if hasattr(self.visualizer, "set_content_height"):
            self.visualizer.set_content_height(actual_visualizer_height)

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

    def on_playback_finished(self, player):
        """Handle playback completion and auto-play next file."""
        logger.info("Playback finished, checking for next track")

        # Check if auto-advance is enabled
        auto_advance_enabled = (
            self.auto_advance_switch.get_active()
            if hasattr(self, "auto_advance_switch")
            else True
        )
        if not auto_advance_enabled:
            logger.info("Auto-advance disabled, stopping playback")
            # Update UI to reflect end of playback
            GLib.idle_add(self.file_queue.update_playing_state, False)
            return

        # Get information about current file
        current_index = self.file_queue.get_current_playing_index()
        if current_index is None:
            logger.debug("No current track index found")
            return

        # Get all files in queue
        files = self.file_queue.get_files()
        if not files:
            logger.debug("Queue is empty")
            return

        # Check if there's a next file available
        next_index = current_index + 1
        if next_index < len(files):
            logger.info(f"Auto-playing next file (index {next_index})")
            next_file = files[next_index]
            # Small delay to ensure clean transition
            GLib.timeout_add(300, self._play_next_file, next_file, next_index)
        else:
            logger.info("Reached end of queue, stopping playback")
            # Update UI to reflect end of playback
            GLib.idle_add(self.file_queue.update_playing_state, False)

    def _play_next_file(self, file_path, index):
        """Helper to play the next file with proper UI updates."""
        # Make sure we're not already playing this file
        if self.player.current_file == file_path and self.player.is_playing():
            return False

        # Check if this is a different file from the current one
        same_file_as_active = file_path == self.active_audio_id

        # Load and play the file
        if self.player.load(file_path, self.file_queue.track_metadata):
            # Tell the file queue which item is being played
            self.file_queue.set_currently_playing(index)
            self.player.play()

            # Only generate waveform if this is a different file
            if not same_file_as_active:
                logger.debug(f"Generating waveform for new file in queue: {file_path}")
                # Clear old waveform first
                self.visualizer.set_waveform(None, 0)
                # Update active audio ID
                self.active_audio_id = file_path

                # Generate waveform data in a separate thread
                threading.Thread(
                    target=waveform.generate,
                    args=(
                        file_path,
                        self.converter,
                        self.visualizer,
                        self.file_markers,
                        self.zoom_control_box
                        if hasattr(self, "zoom_control_box")
                        else None,
                        self.file_queue.track_metadata,
                    ),
                    daemon=True,
                ).start()
            else:
                logger.debug(f"Same file, skipping waveform generation: {file_path}")

        return False  # Don't repeat the timeout

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

    def on_activate_file(self, file_path, index):
        """Handle file row activation (clicked) - show waveform without playing."""
        logger.info(f"File row activated: {file_path} at index {index}")
        self._load_file_for_visualization(file_path, index, play_audio=False)

    def on_play_file(self, file_path, index):
        """Play a selected file."""
        self._load_file_for_visualization(file_path, index, play_audio=True)
        return False  # Don't repeat the timeout

    def _save_current_file_state(self):
        """Save markers for the currently active file before switching."""
        if self.active_audio_id and hasattr(self.visualizer, "get_marker_pairs"):
            current_markers = self.visualizer.get_marker_pairs()
            if current_markers:
                logger.debug(
                    f"Saving {len(current_markers)} markers for {self.active_audio_id}"
                )
                self.file_markers[self.active_audio_id] = current_markers
            elif self.active_audio_id in self.file_markers:
                logger.debug(f"Removing cached markers for {self.active_audio_id}")
                del self.file_markers[self.active_audio_id]

    def _prepare_visualizer_for_new_file(self, file_path, index):
        """Clears the old visualizer state and starts waveform generation for a new file."""
        # Reset visualizer before showing new content
        logger.debug(f"Resetting visualizer for new file: {file_path}")
        self.visualizer.set_waveform(None, 0)
        if hasattr(self, "zoom_control_box"):
            self.zoom_control_box.set_visible(False)

        # Store the file_path as the active audio ID
        self.active_audio_id = file_path
        logger.debug(f"Setting active_audio_id to: {file_path}")

        # Mark the file as active in the queue UI
        if hasattr(self.file_queue, "set_active_file"):
            self.file_queue.set_active_file(index)

        # Preserve markers_enabled flag but clear all markers first
        markers_enabled = self.visualizer.markers_enabled
        self.visualizer.clear_all_markers()
        self.visualizer.markers_enabled = markers_enabled

        # Generate waveform or activate without waveform based on setting
        if self.waveform_switch.get_active():
            threading.Thread(
                target=waveform.generate,
                args=(
                    file_path,
                    self.converter,
                    self.visualizer,
                    self.file_markers,
                    self.zoom_control_box,
                    self.file_queue.track_metadata,
                ),
                daemon=True,
            ).start()
        else:
            threading.Thread(
                target=waveform.activate_without_waveform,
                args=(
                    file_path,
                    self.converter,
                    self.visualizer,
                    self.file_markers,
                    self.zoom_control_box,
                    self.file_queue.track_metadata,
                ),
                daemon=True,
            ).start()

    def _load_file_for_visualization(self, file_path, index, play_audio=True):
        """Load a file for visualization and optional playback."""
        # Case 1: Toggling play/pause on the currently loaded file.
        if (
            play_audio
            and self.player.is_playing()
            and self.player.current_file == file_path
        ):
            self.player.pause()
            if hasattr(self.file_queue, "update_playing_state"):
                self.file_queue.update_playing_state(False)
            return

        # Stop any different file that might be playing.
        if self.player.is_playing() and self.player.current_file != file_path:
            self.player.stop()

        # Save state of the previously active file.
        self._save_current_file_state()

        # Case 2: New file is selected for visualization/playback.
        if file_path != self.active_audio_id:
            self._prepare_visualizer_for_new_file(file_path, index)

        # Load the file into the player.
        if self.player.load(file_path, self.file_queue.track_metadata):
            if play_audio:
                if hasattr(self.file_queue, "set_currently_playing"):
                    self.file_queue.set_currently_playing(index)
                self.player.play()

    def _restore_conversion_settings(self):
        """Restore saved conversion settings from config."""
        if not hasattr(self.app, "config") or not self.app.config:
            return

        # Restore format selection
        saved_format = self.app.config.get("conversion_format")
        if saved_format:
            # Directly check each format in the combo box
            formats = ["copy", "mp3", "ogg", "flac", "wav", "aac", "opus"]
            if saved_format in formats:
                self.format_combo.set_active(formats.index(saved_format))
            else:
                # Default to mp3 if format not found (index 1, since copy is now at 0)
                self.format_combo.set_active(1)
        else:
            # Default to mp3 (index 1)
            self.format_combo.set_active(1)

        # Don't show copy mode dialog or apply UI on startup
        # The format change handler will apply the UI state

        # Mark initialization as complete
        self._initializing = False

        # Restore bitrate selection
        saved_bitrate = self.app.config.get("conversion_bitrate")
        if saved_bitrate:
            # Directly check each bitrate in the combo box
            bitrates = ["32k", "64k", "128k", "192k", "256k", "320k"]
            if saved_bitrate in bitrates:
                self.bitrate_combo.set_active(bitrates.index(saved_bitrate))
            else:
                # Default to 192k (index 2)
                self.bitrate_combo.set_active(2)
        else:
            # Default to 192k (index 2)
            self.bitrate_combo.set_active(2)

        # Restore volume
        saved_volume = self.app.config.get("conversion_volume")
        if saved_volume:
            try:
                self.volume_spin.set_value(float(saved_volume))
            except (ValueError, TypeError):
                self.volume_spin.set_value(100)
        else:
            self.volume_spin.set_value(100)

        # Restore speed
        saved_speed = self.app.config.get("conversion_speed")
        if saved_speed:
            try:
                self.speed_spin.set_value(float(saved_speed))
            except (ValueError, TypeError):
                self.speed_spin.set_value(1.0)
        else:
            self.speed_spin.set_value(1.0)

        # Restore noise reduction
        saved_noise = self.app.config.get("conversion_noise_reduction")
        if saved_noise:
            self.noise_switch.set_active(saved_noise.lower() == "true")
        else:
            self.noise_switch.set_active(False)

        # Restore waveform generation setting
        saved_waveform = self.app.config.get("generate_waveforms")
        if saved_waveform:
            self.waveform_switch.set_active(saved_waveform.lower() == "true")
        else:
            self.waveform_switch.set_active(True)  # Default to enabled

        # Restore cut audio mode if present
        if hasattr(self, "cut_combo"):
            # First try to get the specific mode
            saved_cut_mode = self.app.config.get("cut_audio_mode")
            if saved_cut_mode:
                try:
                    mode = int(saved_cut_mode)
                    if 0 <= mode <= 2:  # Ensure valid range
                        self.cut_combo.set_active(mode)
                except (ValueError, TypeError):
                    # Fall back to enabled/disabled state
                    saved_cut_enabled = self.app.config.get("cut_audio_enabled")
                    if saved_cut_enabled and saved_cut_enabled.lower() == "false":
                        self.cut_combo.set_active(0)  # Off
                    else:
                        self.cut_combo.set_active(1)  # Default to chronological
            else:
                # Fall back to enabled/disabled state
                saved_cut_enabled = self.app.config.get("cut_audio_enabled")
                if saved_cut_enabled and saved_cut_enabled.lower() == "false":
                    self.cut_combo.set_active(0)  # Off
                else:
                    self.cut_combo.set_active(1)  # Default to chronological

            # Set visibility based on combo selection
            active = self.cut_combo.get_active()
            if hasattr(self, "cut_options_box"):
                self.cut_options_box.set_visible(active > 0)

            # The markers will be enabled in the window realize callback
            # after the visualizer is fully created

            # Restore cut times if those UI elements exist
            if hasattr(self, "start_time_entry"):
                saved_start_time = self.app.config.get("cut_start_time")
                if saved_start_time:
                    self.start_time_entry.set_text(saved_start_time)

            if hasattr(self, "end_time_entry"):
                saved_end_time = self.app.config.get("cut_end_time")
                if saved_end_time:
                    self.end_time_entry.set_text(saved_end_time)

    def _update_time_display(self, position, duration):
        """Update the time display label (called via GLib.idle_add)."""
        if hasattr(self, "time_display_label"):
            # Only show total duration, not current time
            duration_str = format_time_short(duration)
            self.time_display_label.set_label(duration_str)
        return False  # Don't repeat

    def on_player_position_updated(self, player, position, duration):
        """Handle position updates from player."""
        # Update visualizer position
        self.visualizer.set_position(position)

        # Update time display label safely on main GTK thread
        GLib.idle_add(self._update_time_display, position, duration)

        # Update Play Selection switch state based on markers
        GLib.idle_add(self._update_play_selection_button)

        # Handle segment transitions in Play Selection Only mode
        if self._playing_selection and self._selection_segments:
            # Skip checks if user is dragging a marker or a transition is already in progress
            if self._marker_dragging or self._is_transitioning_segment:
                return

            # Ensure current segment index is valid
            if self._current_segment_index >= len(self._selection_segments):
                logger.warning("Invalid segment index, stopping selection playback.")
                self.player.pause()
                self._playing_selection = False
                return

            start, stop = self._selection_segments[self._current_segment_index]

            # A small tolerance helps prevent false triggers from minor timing variations.
            TOLERANCE = 0.02  # 20ms tolerance

            if position > stop - TOLERANCE:
                # Position is at or past the end of the current segment.
                logger.info(
                    f"Segment {self._current_segment_index + 1} end detected at {position:.3f}s (target: {stop:.3f}s). Transitioning."
                )

                # Lock to prevent re-triggering
                self._is_transitioning_segment = True

                # Move to the next segment
                self._current_segment_index += 1

                # Schedule the transition logic
                GLib.idle_add(self._do_segment_transition_with_retry)
                return  # Exit after scheduling

            # If playback position is somehow before the start, correct it.
            if position < start - TOLERANCE:
                logger.info(
                    f"Position {position:.3f}s is before segment start {start:.3f}s. Seeking to start."
                )
                self.player.seek(start)
                return  # Exit after correcting

        # Auto-next detection fallback
        if duration > 0 and position >= duration - 0.2:
            if (
                not hasattr(self, "_end_of_track_handled")
                or not self._end_of_track_handled
            ):
                self._end_of_track_handled = True
                GLib.idle_add(self.on_playback_finished, player)
        elif hasattr(self, "_end_of_track_handled") and self._end_of_track_handled:
            self._end_of_track_handled = False

    def _do_segment_transition_with_retry(self, retry_count=0):
        """Execute segment transition with error handling and retry capability."""
        max_retries = 2

        if not self._playing_selection or not self._selection_segments:
            logger.debug("Segment transition cancelled - not in selection mode")
            self._is_transitioning_segment = False  # UNLOCK
            return False

        if self._current_segment_index < len(self._selection_segments):
            # Jump to next segment
            next_start, next_stop = self._selection_segments[
                self._current_segment_index
            ]
            logger.info(
                f"Transitioning to segment {self._current_segment_index + 1}/{len(self._selection_segments)}: {next_start:.3f}-{next_stop:.3f}"
            )

            # Try to seek to next segment
            try:
                # Ensure we're not at the exact same position (can cause issues)
                current_pos = getattr(self.player, "_position", 0)
                if abs(current_pos - next_start) < 0.01:
                    logger.debug("Already at target position, skipping seek")
                    # Still need to ensure playback continues
                    if not self.player.is_playing():
                        self.player.play()
                    self._is_transitioning_segment = False  # UNLOCK
                    return False

                success = self.player.seek(next_start)

                if not success and retry_count < max_retries:
                    # Seek failed, retry after a short delay
                    logger.warning(
                        f"Segment transition seek failed, retry {retry_count + 1}/{max_retries}"
                    )
                    GLib.timeout_add(
                        100,
                        lambda: self._do_segment_transition_with_retry(retry_count + 1),
                    )
                    return False
                elif not success:
                    logger.error(
                        f"Segment transition failed after {max_retries} retries"
                    )
                    # Try to recover by stopping selection mode
                    self._playing_selection = False
                    self._is_transitioning_segment = False  # UNLOCK
                    return False

                # CRITICAL: Always ensure playback resumes after segment transition
                def ensure_playing():
                    if self._playing_selection and not self.player.is_playing():
                        logger.info("Resuming playback after segment transition")
                        self.player.play()
                    return False

                GLib.timeout_add(50, ensure_playing)
                self._is_transitioning_segment = False  # UNLOCK
                logger.debug("Transition lock released after successful seek.")

            except Exception as e:
                logger.error(f"Exception during segment transition: {e}")
                # Recover by stopping selection mode
                self._playing_selection = False
                self._is_transitioning_segment = False  # UNLOCK
                return False
        else:
            # All segments played - pause playback to respect segment boundaries
            logger.info(
                f"All {len(self._selection_segments)} segments completed - pausing playback"
            )
            self._playing_selection = False
            self._is_transitioning_segment = False  # UNLOCK

            # Pause playback after last segment (keeps pipeline state and markers visible)
            if self.player.is_playing():
                self.player.pause()

        return False  # Don't repeat

    def _do_segment_transition(self):
        """Execute segment transition on GTK main thread (legacy wrapper)."""
        # Call the new resilient version
        return self._do_segment_transition_with_retry(0)

    def on_player_duration_changed(self, player, duration):
        """Handle duration changes from player."""
        pass

    def on_visualizer_seek(self, position, should_play):
        """Handle seek request from visualizer."""
        logger.info(
            f"🔍 MAIN_WINDOW: Received seek position={position:.6f}s, should_play={should_play}"
        )
        if not hasattr(self.player, "seek"):
            return

        # If marker is being dragged/resized, allow free seeking without boundaries
        if self._marker_dragging:
            self.player.seek(position)
            self.visualizer.set_position(position)
            return

        # If in Play Selection Only mode, validate position is in a segment
        if self._play_selection_mode and self._selection_segments:
            # Check if position is within any segment
            in_segment = False
            for i, (start, stop) in enumerate(self._selection_segments):
                if start <= position <= stop:
                    in_segment = True
                    # Update current segment index regardless of playback state
                    self._current_segment_index = i
                    logger.debug(f"Seek action sets current segment to index {i}")
                    break

            # If not in any segment, find appropriate segment based on position
            if not in_segment:
                # Find if we're after any selection or before all
                after_any_selection = False
                for start, stop in self._selection_segments:
                    if position > stop:
                        after_any_selection = True
                        break

                if after_any_selection:
                    # We're after a selection, find the NEXT selection on the right
                    next_segment = None
                    for i, (start, stop) in enumerate(self._selection_segments):
                        if start > position:
                            next_segment = i
                            break

                    if next_segment is not None:
                        position = self._selection_segments[next_segment][0]
                        self._current_segment_index = next_segment
                        logger.info(
                            f"Seek after selection, jumping to next segment {next_segment} at {position:.3f}"
                        )
                    else:
                        # No segment after, go to first segment
                        position = self._selection_segments[0][0]
                        self._current_segment_index = 0
                        logger.info(
                            f"No segment after, jumping to first segment at {position:.3f}"
                        )
                else:
                    # We're before all selections or between, find closest
                    nearest_segment = None
                    min_distance = float("inf")

                    for i, (start, stop) in enumerate(self._selection_segments):
                        # Calculate distance to segment start
                        if position < start:
                            distance = start - position
                        elif position > stop:
                            distance = position - stop
                        else:
                            distance = 0

                        if distance < min_distance:
                            min_distance = distance
                            nearest_segment = i

                    # Jump to the start of the nearest segment
                    if nearest_segment is not None:
                        position = self._selection_segments[nearest_segment][0]
                        self._current_segment_index = nearest_segment
                        logger.info(
                            f"Seek before selections, jumping to nearest segment {nearest_segment} at {position:.3f}"
                        )

            # CRITICAL FIX: Reset the transition lock on any manual seek.
            # This prevents a stale lock from blocking future boundary checks.
            self._is_transitioning_segment = False

        # Perform the seek
        self.player.seek(position)

        # CRITICAL FIX: Immediately update visualizer position after seeking
        self.visualizer.set_position(position)

        # If the click intended to start playback, do it now
        if should_play:
            self._on_pause_play_clicked(None)

    def _on_markers_updated(self, markers):
        """Handle marker updates from visualizer."""
        # If Play Selection mode is enabled, reload segments
        if self._play_selection_mode:
            self._load_selection_segments()

            # If currently playing, update the current segment index
            if self._playing_selection and self._selection_segments:
                # Try to find which new segment corresponds to current position
                current_position = (
                    self.player._position if hasattr(self.player, "_position") else 0
                )

                # Find the segment that contains current position
                found_index = -1
                for i, (start, stop) in enumerate(self._selection_segments):
                    if start <= current_position <= stop:
                        found_index = i
                        break

                if found_index >= 0:
                    # Update to the segment containing current position
                    self._current_segment_index = found_index
                    logger.debug(
                        f"Updated current segment to {found_index} after marker change"
                    )
                else:
                    # Position not in any segment, find the next segment after current position
                    for i, (start, stop) in enumerate(self._selection_segments):
                        if start > current_position:
                            self._current_segment_index = i
                            logger.debug(
                                f"Jumped to next segment {i} after marker change"
                            )
                            break
                    else:
                        # No segment after current position, stop playback
                        logger.info("No valid segment after marker change, stopping")
                        self._playing_selection = False
                        self.player.stop()

    def _on_marker_drag_state_changed(self, is_dragging):
        """Handle marker drag state changes.

        When dragging markers, temporarily disable segment boundary enforcement
        to allow hearing audio beyond current segment limits.
        """
        self._marker_dragging = is_dragging
        if is_dragging:
            logger.info(
                "🎯 Marker dragging started - DISABLING segment boundary checks"
            )
        else:
            logger.info(
                "🎯 Marker dragging ended - RE-ENABLING segment boundary checks"
            )

            # After dragging ends, check if current position is valid
            # and adjust if necessary to prevent stuck playback
            if self._playing_selection and self._selection_segments:
                current_position = (
                    self.player._position if hasattr(self.player, "_position") else 0
                )

                # Clear transition tracking to prevent false duplicate detections
                # when we seek to a new segment after drag
                if hasattr(self, "_last_transition_times"):
                    self._last_transition_times.clear()
                    logger.debug("Cleared transition tracking after drag end")

                # Find which segment we should be in based on current position
                found_segment = -1
                for i, (start, stop) in enumerate(self._selection_segments):
                    if start <= current_position <= stop:
                        found_segment = i
                        break

                if found_segment >= 0:
                    # Current position is in a valid segment
                    self._current_segment_index = found_segment
                    logger.debug(
                        f"Position {current_position:.3f}s is in segment {found_segment}"
                    )
                else:
                    # Position is outside all segments - need to find where to go
                    # Check if position is before first segment or after last segment
                    if current_position < self._selection_segments[0][0]:
                        # Before first segment - jump to start of first segment
                        logger.info(
                            f"Position {current_position:.3f}s is before segments, seeking to first segment"
                        )
                        self._current_segment_index = 0
                        start, _ = self._selection_segments[0]
                        # Temporarily disable boundary checks during this seek to prevent loops
                        self._marker_dragging = True
                        GLib.idle_add(
                            lambda: (
                                self.player.seek(start)
                                if self.player.is_playing()
                                else None,
                                setattr(self, "_marker_dragging", False),
                            )[0]
                        )
                    elif current_position > self._selection_segments[-1][1]:
                        # After last segment - stop playback or loop to first
                        logger.info(
                            f"Position {current_position:.3f}s is after all segments, stopping"
                        )
                        self._playing_selection = False
                        GLib.idle_add(
                            lambda: self.player.stop()
                            if self.player.is_playing()
                            else None
                        )
                    else:
                        # In a gap between segments - find next segment
                        for i, (start, stop) in enumerate(self._selection_segments):
                            if start > current_position:
                                logger.info(
                                    f"Position {current_position:.3f}s is in gap, seeking to next segment {i}"
                                )
                                self._current_segment_index = i
                                # Temporarily disable boundary checks during this seek to prevent loops
                                self._marker_dragging = True

                                def do_seek_and_reenable():
                                    if self.player.is_playing():
                                        self.player.seek(start)
                                    # Re-enable boundary checks after a short delay to let seek complete
                                    GLib.timeout_add(
                                        150,
                                        lambda: (
                                            setattr(self, "_marker_dragging", False),
                                            False,
                                        )[1],
                                    )
                                    return False

                                GLib.idle_add(do_seek_and_reenable)
                                break

    def _on_visualizer_height_changed(self, paned, param):
        """Handle visualizer height changes and save to config."""
        if not hasattr(self.app, "config") or not self.app.config:
            return

        # Get total height and position
        total_height = self.get_height()
        position = paned.get_position()

        # Calculate visualizer height (accounting for margins and zoom control bar)
        visualizer_height = (
            total_height - position - 50
        )  # 50px = 10px margin + 40px zoom controls

        # Define minimum heights for both sections
        min_top_height = 200
        min_visualizer_height = 100

        # Make sure we don't resize the visualizer too small
        if visualizer_height < min_visualizer_height:
            # Calculate the maximum valid position to maintain minimum visualizer height
            max_position = total_height - min_visualizer_height - 50
            # Adjust the position
            paned.set_position(max_position)
            # Recalculate visualizer height
            visualizer_height = min_visualizer_height

        # Make sure we don't resize the top section too small (only check if visualizer constraint is satisfied)
        elif position < min_top_height:
            # Prevent the top section from getting too small
            paned.set_position(min_top_height)
            # Recalculate visualizer height
            visualizer_height = total_height - min_top_height - 50

        # Only save if it's a reasonable value
        if (
            visualizer_height >= min_visualizer_height
            and visualizer_height <= total_height * 0.8
        ):
            # Update stored height
            self.visualizer_height = visualizer_height
            # Save to config
            self.app.config.set("visualizer_height", str(visualizer_height))

            # Update visualizer content height to match
            if hasattr(self.visualizer, "set_content_height"):
                self.visualizer.set_content_height(visualizer_height)

    def on_player_state_changed(self, player, is_playing):
        """Handle player state changes to update UI."""

        # Always use GLib.idle_add to ensure UI updates happen on the main GTK thread
        def update_ui():
            # Update the play/stop button in the file queue
            if hasattr(self.file_queue, "update_playing_state"):
                self.file_queue.update_playing_state(is_playing)

            # Update the pause/play button icon based on actual player state
            if hasattr(self, "pause_play_btn"):
                if is_playing:
                    self.pause_play_btn.set_icon_name("media-playback-pause-symbolic")
                    self.pause_play_btn.set_tooltip_text(_("Pause"))
                else:
                    self.pause_play_btn.set_icon_name("media-playback-start-symbolic")
                    self.pause_play_btn.set_tooltip_text(_("Play"))
            return False  # Don't repeat

        GLib.idle_add(update_ui)

    def _format_zoom_value(self, scale, value):
        """Format zoom slider value to show actual zoom level."""
        # Convert linear slider value (0-100) to logarithmic zoom (1-100)
        # Using formula: zoom = 10^(value/50) where value 0→1x, 50→10x, 100→100x
        import math

        zoom = math.pow(10, value / 50.0)
        return f"{zoom:.1f}x"

    def _slider_to_zoom(self, slider_value):
        """Convert slider position (0-150) to zoom level (1-1000) logarithmically."""
        import math

        # Formula: zoom = 10^(slider_value/50)
        # slider_value=0 → zoom=1, slider_value=50 → zoom=10, slider_value=100 → zoom=100, slider_value=150 → zoom=1000
        return math.pow(10, slider_value / 50.0)

    def _zoom_to_slider(self, zoom_level):
        """Convert zoom level (1-1000) to slider position (0-150) logarithmically."""
        import math

        # Formula: slider_value = 50 * log10(zoom)
        # zoom=1 → slider_value=0, zoom=10 → slider_value=50, zoom=100 → slider_value=100, zoom=1000 → slider_value=150
        return 50.0 * math.log10(max(1.0, zoom_level))

    def _on_zoom_scale_changed(self, scale):
        """Handle zoom slider changes."""
        slider_value = scale.get_value()
        # Convert slider value to actual zoom level using logarithmic scale
        zoom_level = self._slider_to_zoom(slider_value)

        if hasattr(self.visualizer, "set_zoom_level"):
            # Use the new set_zoom_level method which can use mouse position
            self.visualizer.set_zoom_level(zoom_level, use_mouse_position=True)

            # Notify zoom change (will be blocked if called from visualizer)
            if self.visualizer.zoom_changed_callback:
                self.visualizer.zoom_changed_callback(self.visualizer.zoom_level)

    def _on_visualizer_zoom_changed(self, zoom_level):
        """Update zoom slider when zoom changes from visualizer (e.g. mouse wheel)."""
        if hasattr(self, "zoom_scale"):
            # Convert zoom level back to slider value
            slider_value = self._zoom_to_slider(zoom_level)
            # Temporarily block signal to avoid feedback loop
            self.zoom_scale.handler_block_by_func(self._on_zoom_scale_changed)
            self.zoom_scale.set_value(slider_value)
            self.zoom_scale.handler_unblock_by_func(self._on_zoom_scale_changed)

    def _on_pause_play_clicked(self, button):
        """Handle pause/play button click."""
        if self.player.is_playing():
            self.player.pause()
            # If playing selection, cancel it
            if self._playing_selection:
                self._playing_selection = False
            # Don't update button here - let on_player_state_changed handle it
        else:
            # Find the index of the active file in the queue
            active_index = None
            if self.active_audio_id:
                for i, file_path in enumerate(self.file_queue.files):
                    if file_path == self.active_audio_id:
                        active_index = i
                        break

            # Before playing, make sure we have the active file loaded
            if (
                self.active_audio_id
                and self.player.current_file != self.active_audio_id
            ):
                # Load the active file if not already loaded
                logger.info(f"Loading active file for playback: {self.active_audio_id}")
                self.player.load(self.active_audio_id, self.file_queue.track_metadata)

                # Update the currently playing index in file queue
                if active_index is not None and hasattr(
                    self.file_queue, "set_currently_playing"
                ):
                    self.file_queue.set_currently_playing(active_index)
            elif active_index is not None and hasattr(
                self.file_queue, "set_currently_playing"
            ):
                # File is already loaded, just update the index
                self.file_queue.set_currently_playing(active_index)

            # If play selection mode is on, prepare for it before playing
            if self._play_selection_mode:
                self._start_selection_playback()

            # Now, start the player
            self.player.play()

    def _on_previous_audio_clicked(self):
        """Handle previous audio button click - go to previous file in queue."""
        if not self.file_queue.files:
            return

        # Get current file index
        current_index = None
        if (
            hasattr(self.file_queue, "currently_playing_index")
            and self.file_queue.currently_playing_index is not None
        ):
            current_index = self.file_queue.currently_playing_index
        elif (
            hasattr(self.file_queue, "active_file_index")
            and self.file_queue.active_file_index is not None
        ):
            current_index = self.file_queue.active_file_index
        else:
            # If no file is active, start from the end
            current_index = len(self.file_queue.files)

        # Calculate previous index (wrap around to end)
        prev_index = (current_index - 1) % len(self.file_queue.files)

        # Load and play the previous file
        prev_file = self.file_queue.files[prev_index]
        logger.info(f"Going to previous audio: {prev_file}")
        self._load_file_for_visualization(prev_file, prev_index, play_audio=True)

    def _on_next_audio_clicked(self):
        """Handle next audio button click - go to next file in queue."""
        if not self.file_queue.files:
            return

        # Get current file index
        current_index = None
        if (
            hasattr(self.file_queue, "currently_playing_index")
            and self.file_queue.currently_playing_index is not None
        ):
            current_index = self.file_queue.currently_playing_index
        elif (
            hasattr(self.file_queue, "active_file_index")
            and self.file_queue.active_file_index is not None
        ):
            current_index = self.file_queue.active_file_index
        else:
            # If no file is active, start from the beginning
            current_index = -1

        # Calculate next index (wrap around to beginning)
        next_index = (current_index + 1) % len(self.file_queue.files)

        # Load and play the next file
        next_file = self.file_queue.files[next_index]
        logger.info(f"Going to next audio: {next_file}")
        self._load_file_for_visualization(next_file, next_index, play_audio=True)

    def _on_play_selection_switch_changed(self, switch, param):
        """Handle play selection switch state change."""
        active = switch.get_active()
        self._play_selection_mode = active

        # Update visualizer to know about selection-only mode
        # This affects marker dragging behavior (seeking vs not seeking)
        self.visualizer.set_selection_only_mode(active)

        logger.info(f"Play Selection Only mode: {'ENABLED' if active else 'DISABLED'}")

        if active:
            # Start selection playback immediately when switch is turned on
            if self.player.is_playing():
                self._start_selection_playback()
        else:
            # If switching off, stop segment mode
            if self._playing_selection:
                self._playing_selection = False
                logger.info("Selection playback mode stopped")

    def _on_auto_advance_switch_changed(self, switch, param):
        """Handle auto-advance switch state change."""
        is_active = switch.get_active()
        # Save to config
        self.app.config.set("auto_advance_enabled", is_active)
        logger.info(f"Auto-advance mode: {'ENABLED' if is_active else 'DISABLED'}")

    def _load_selection_segments(self):
        """Load segments from markers."""
        order_by_number = self.cut_combo.get_active() == 2
        marker_pairs = self.visualizer.get_ordered_marker_pairs(order_by_number)

        segments = []
        for marker in marker_pairs:
            if marker.get("start") is not None and marker.get("stop") is not None:
                start = marker["start"]
                stop = marker["stop"]
                if start > stop:
                    start, stop = stop, start
                segments.append((start, stop))

        self._selection_segments = segments
        return segments

    def _start_selection_playback(self):
        """Prepare for selection playback by setting state and seeking."""
        segments = self._load_selection_segments()
        if not segments:
            logger.warning("Cannot start selection playback - no segments available.")
            self._playing_selection = False  # Ensure it's off
            return

        self._playing_selection = True
        self._is_transitioning_segment = False

        # If current index is invalid, reset to 0.
        if self._current_segment_index >= len(segments):
            self._current_segment_index = 0

        start, stop = segments[self._current_segment_index]
        logger.info(
            f"Preparing selection playback from segment {self._current_segment_index + 1}/{len(segments)}: {start:.3f}-{stop:.3f}"
        )

        # Seek to the start position. The play() call will happen after this.
        self.player.seek(start)

    def _update_play_selection_button(self):
        """Update Play Selection switch enabled state based on markers."""
        if not hasattr(self, "play_selection_switch"):
            return False

        # Enable switch if there's at least one complete marker pair
        has_complete_pair = False
        for marker in self.visualizer.marker_pairs:
            if marker.get("start") is not None and marker.get("stop") is not None:
                has_complete_pair = True
                break

        self.play_selection_switch.set_sensitive(has_complete_pair)

        # If no complete pairs and switch is on, turn it off
        if not has_complete_pair and self.play_selection_switch.get_active():
            self.play_selection_switch.set_active(False)

        return False  # Don't repeat

    def _on_file_removed(self, file_id):
        """Handle when a file is removed from the queue."""
        # If the removed file was active in the visualizer, clear it
        if file_id == self.active_audio_id:
            logger.info(
                f"Active file {file_id} was removed, clearing visualizer and player"
            )
            self.visualizer.clear_waveform()
            self.active_audio_id = None

        # Stop and unload player if it has the removed file loaded
        # Check both current_file and current_actual_file for track files
        if hasattr(self.player, "current_file"):
            player_file = self.player.current_file
            player_actual_file = getattr(self.player, "current_actual_file", None)

            # Check if removed file matches player's loaded file (virtual or actual path)
            if player_file == file_id or (
                player_actual_file and player_actual_file == file_id
            ):
                logger.info("Player had removed file loaded, stopping and clearing")
                self.player.stop()
                self.player.current_file = None
                if hasattr(self.player, "current_actual_file"):
                    self.player.current_actual_file = None
                logger.debug("Stopped and unloaded player")
