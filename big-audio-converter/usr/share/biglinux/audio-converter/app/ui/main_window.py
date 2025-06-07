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
        default_width = 1000
        default_height = 700
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

        # Initialize components
        self.player = self.app.player
        self.converter = self.app.converter
        # Add marker cache to remember markers for each file
        self.file_markers = {}  # Dictionary mapping file path to marker pairs

        # For debouncing sidebar width save
        self._sidebar_save_timeout_id = None

        # Default sidebar width - will be overridden by saved value if available
        self.sidebar_width = 300

        # Default visualizer height - will be overridden by saved value
        self.visualizer_height = 150

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

        # Connect to map event for visualizer height restoration
        # Map is better than realize as it happens when window is actually drawn on screen
        self.connect("map", self._on_window_mapped)

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
        self.clear_queue_button.set_tooltip_text("Clear Queue")
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
            title_label = Gtk.Label(label="Audio Converter")
            title_label.set_halign(Gtk.Align.CENTER)
            title_label.set_valign(Gtk.Align.START)
            title_label.set_hexpand(True)
            center_box.set_center_widget(title_label)
            # No end widget
            left_header.set_title_widget(center_box)
        else:
            title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title_label = Gtk.Label(label="Audio Converter")
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
        self.clear_queue_button.set_icon_name("trash-symbolic")
        self.clear_queue_button.set_tooltip_text("Clear Queue")
        self.clear_queue_button.add_css_class("circular")
        self.clear_queue_button.connect("clicked", self.on_clear_queue)
        self.clear_queue_button.add_css_class("destructive-action")
        self.clear_queue_button.set_visible(False)  # Initially hidden
        self.header_queue_size_label = Gtk.Label(label="0 files")
        self.header_queue_size_label.add_css_class("caption")
        self.header_queue_size_label.add_css_class("dim-label")
        self.header_queue_size_label.set_visible(False)
        self.header_queue_size_label.set_margin_start(4)
        self.header_queue_size_label.set_margin_end(8)
        self.header_queue_size_label.set_valign(Gtk.Align.CENTER)
        # Only add queue controls to right headerbar if window buttons are on the left

        # Create menu button and add to right side of header directly
        menu = Gio.Menu()
        menu.append("Preferences", "app.preferences")
        menu.append("About Audio Converter", "app.about")
        menu.append("Quit", "app.quit")
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

        # Create a center box layout for right header
        header_container = Gtk.CenterBox()
        header_container.set_hexpand(True)

        # Left section for queue controls (moved from right side)
        left_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        left_controls.set_halign(Gtk.Align.START)
        left_controls.set_margin_start(0)

        # Center section for buttons
        center_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        center_box.set_halign(Gtk.Align.CENTER)
        center_box.set_spacing(8)  # Space between buttons
        center_box.set_margin_end(46)

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

        # Assemble the header layout (without right controls as the menu is now directly in the header)
        header_container.set_start_widget(left_controls)
        header_container.set_center_widget(center_box)
        # No need for end_widget as menu is directly in header now

        # Set the container as the title widget
        right_header.set_title_widget(header_container)
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
        right_content.append(self.file_queue)

        # Connect queue size change handler
        self.file_queue.on_queue_size_changed = self.update_queue_size_label

        # Connect play callback for file queue
        self.file_queue.on_play_file = self.on_play_file

        # Connect stop playback callback to player.stop method
        self.file_queue.on_stop_playback = self.player.stop

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

        # Create a frame around the visualizer for better appearance
        visualizer_frame = Gtk.Frame()
        visualizer_frame.set_margin_start(10)
        visualizer_frame.set_margin_end(10)
        visualizer_frame.set_margin_top(10)
        visualizer_frame.set_margin_bottom(10)

        # Set minimum height instead of fixed size
        visualizer_frame.set_size_request(-1, 100)  # Min height 100px

        # Connect seek handler before adding the visualizer to the frame
        self.visualizer.connect_seek_handler(self.on_visualizer_seek)

        # Set content height for the visualizer based on saved/default height
        self.visualizer.set_content_height(self.visualizer_height)

        # Make sure the visualizer can receive mouse events and expand with parent
        self.visualizer.set_vexpand(True)  # Allow expansion
        self.visualizer.set_focusable(True)

        # Add the visualizer to the frame
        visualizer_frame.set_child(self.visualizer)

        # Add visualizer frame to the bottom part of the vertical paned
        self.vertical_paned.set_end_child(visualizer_frame)

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

        # Show/hide both elements based on file count
        has_files = count > 0
        self.header_queue_size_label.set_visible(has_files)
        if hasattr(self, "clear_queue_button"):
            self.clear_queue_button.set_visible(has_files)
        if hasattr(self, "convert_button"):
            self.convert_button.set_visible(has_files)

    def setup_conversion_options(self, parent_box):
        """Set up the conversion options UI."""
        # Create container to center the options group
        options_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        options_container.set_valign(Gtk.Align.CENTER)
        options_container.set_hexpand(True)
        options_container.set_margin_top(8)  # Add top margin
        options_container.set_margin_bottom(8)  # Add bottom margin
        options_container.set_spacing(6)  # Add spacing between children

        # Create a single preference group for all options
        options_group = Adw.PreferencesGroup()

        # Format selection
        format_row = Adw.ActionRow(title=_("Output Format"))
        self.format_combo = Gtk.ComboBoxText()
        self.format_combo.set_valign(Gtk.Align.CENTER)
        formats = ["mp3", "ogg", "flac", "wav", "aac", "opus"]
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
        bitrates = ["64k", "128k", "192k", "256k", "320k"]
        for br in bitrates:
            self.bitrate_combo.append_text(br)
        # Connect bitrate change to save settings
        self.bitrate_combo.connect("changed", self._on_bitrate_changed)
        bitrate_row.add_suffix(self.bitrate_combo)
        options_group.add(bitrate_row)

        # Volume adjustment - replace Scale with SpinRow
        self.volume_spin = Adw.SpinRow.new_with_range(0, 200, 5)
        self.volume_spin.set_title(_("Volume"))
        self.volume_spin.set_subtitle(_("100 = original volume"))
        self.volume_spin.set_value(100)  # Default to 100%
        self.volume_spin.connect("changed", self._on_volume_spin_changed)
        options_group.add(self.volume_spin)

        # Speed adjustment - replace Scale with SpinRow
        self.speed_spin = Adw.SpinRow.new_with_range(0.5, 2.0, 0.05)
        self.speed_spin.set_title(_("Speed"))
        self.speed_spin.set_subtitle(_("1.0 = original speed"))
        self.speed_spin.set_digits(2)  # Show 2 decimal places
        self.speed_spin.set_value(1.0)  # Default to normal speed
        self.speed_spin.connect("changed", self._on_speed_spin_changed)
        options_group.add(self.speed_spin)

        # Noise reduction
        noise_row = Adw.ActionRow(title=_("Noise Reduction"))
        self.noise_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        # Connect noise reduction switch to player and settings
        self.noise_switch.connect("state-set", self._on_noise_switch_changed)
        noise_row.add_suffix(self.noise_switch)
        options_group.add(noise_row)

        # Equalizer - moved from effects group
        eq_row = Adw.ActionRow(title=_("Equalizer"))
        eq_button = Gtk.Button(label=_("Configure..."))
        eq_button.set_valign(Gtk.Align.CENTER)
        eq_button.connect("clicked", self.on_configure_equalizer)
        eq_row.add_suffix(eq_button)
        options_group.add(eq_row)

        # Replace the cut audio switch with a combo box
        cut_row = Adw.ActionRow(title=_("Cut"))

        # Add box to contain combo and help button side by side
        cut_suffix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        # Add help button with question mark icon
        help_button = Gtk.Button.new_from_icon_name("help-about-symbolic")
        help_button.set_tooltip_text(_("How to use waveform cutting"))
        help_button.add_css_class("circular")
        help_button.add_css_class("flat")
        help_button.connect("clicked", self._show_cut_help_dialog)
        cut_suffix_box.append(help_button)

        cut_row.add_suffix(cut_suffix_box)

        self.cut_combo = Gtk.ComboBoxText()
        self.cut_combo.set_valign(Gtk.Align.CENTER)
        self.cut_combo.append_text(_("Off"))
        self.cut_combo.append_text(_("Chronological"))
        self.cut_combo.append_text(_("Segment Number"))
        self.cut_combo.set_active(0)  # Default to Off
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

        # Restore saved settings after UI is created
        self._restore_conversion_settings()

    def _on_format_changed(self, combo):
        """Handle format selection change and save setting."""
        if hasattr(self.app, "config") and self.app.config:
            selected_format = combo.get_active_text()
            if selected_format:
                self.app.config.set("conversion_format", selected_format)

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

        # Also update player (original functionality)
        if hasattr(self.player, "set_noise_reduction"):
            # Store current playback state and position
            was_playing = self.player.is_playing()
            current_position = 0

            if was_playing:
                current_position = (
                    self.player._position if hasattr(self.player, "_position") else 0
                )

            # Apply noise reduction setting
            self.player.set_noise_reduction(state)

            # If it was playing, seek to the saved position and resume
            if was_playing:
                position_to_seek = current_position

                def resume_playback():
                    self.player._position = position_to_seek
                    self.player.play()
                    return False

                GLib.timeout_add(150, resume_playback)

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
                "No files to convert", "Please add at least one file to convert."
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
        }

        # Get segment ordering preference (True = by number, False = by timeline)
        order_by_number = self.cut_combo.get_active() == 2
        settings["order_by_segment_number"] = order_by_number

        # For multi-file cutting, store ALL marker information
        if settings["cut_enabled"]:
            # Add current markers if file is playing
            if self.player.current_file:
                # Get ordered segments based on user preference
                print(f"Getting segments with order_by_number={order_by_number}")
                current_markers = self.visualizer.get_ordered_marker_pairs(
                    order_by_number
                )
                if current_markers:
                    print(
                        f"Storing {len(current_markers)} ordered segments for current file"
                    )
                    self.file_markers[self.player.current_file] = current_markers

            # Process each file's markers with the ordering preference
            ordered_file_markers = {}
            for file_path, markers in self.file_markers.items():
                # Sort markers if needed (for files we didn't just process)
                if file_path != self.player.current_file:
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
                self.player.current_file
                and self.player.current_file in settings["file_markers"]
            ):
                current_file_segments = settings["file_markers"][
                    self.player.current_file
                ]
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
        content_box.set_margin_top(10)
        content_box.set_margin_bottom(10)
        content_box.set_margin_start(20)
        content_box.set_margin_end(20)

        # Create progress bar
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_fraction(0.0)
        content_box.append(self.progress_bar)

        # Create the dialog with our custom content
        self.progress_dialog = Adw.MessageDialog(
            transient_for=self,
            title="Converting Files",
            body=f"Converting file 1 of {total_files}",
        )

        # Set the extra child (content area)
        self.progress_dialog.set_extra_child(content_box)

        # Add a cancel button
        self.progress_dialog.add_response("cancel", "Cancel")
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
                            f"Converting file {file_index + 1} of {total_files} ({percent}%)\n"
                            f"Current file: {filename}"
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

            GLib.idle_add(
                self._show_info_dialog,
                "Conversion Complete",
                error_message or "Files were converted successfully.",
            )
        else:
            GLib.idle_add(
                self._show_error_dialog,
                "Conversion Error",
                error_message or "An error occurred during conversion.",
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

    def _show_error_dialog(self, title, message):
        """Show an error dialog."""
        dialog = Adw.MessageDialog(transient_for=self, title=title, body=message)
        dialog.add_response("ok", "OK")
        dialog.present()

    def _show_info_dialog(self, title, message):
        """Show an information dialog."""
        dialog = Adw.MessageDialog(transient_for=self, title=title, body=message)
        dialog.add_response("ok", "OK")
        dialog.present()

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
            GLib.source_remove(self._sidebar_save_timeout_id)
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
                GLib.source_remove(self._size_save_timeout_id)

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

        # Get window dimensions
        height = self.get_height()

        # Restore manual sidebar width without changing visualizer size
        if hasattr(self, "_manual_sidebar_width") and hasattr(self, "split_view"):
            self.split_view.set_position(self._manual_sidebar_width)

        # Use the saved visualizer height directly without recalculating
        if hasattr(self, "vertical_paned") and hasattr(self, "visualizer_height"):
            # Calculate position accounting for both top and bottom margins (20px total)
            # The visualizer frame has 10px margin on top and 10px on bottom
            total_margin = 20  # top margin + bottom margin
            position = max(200, height - self.visualizer_height - total_margin)
            self.vertical_paned.set_position(position)

            # Update the content height directly from saved value
            if hasattr(self.visualizer, "set_content_height"):
                self.visualizer.set_content_height(self.visualizer_height)

            # Ensure the paned widget recalculates its layout
            self.vertical_paned.queue_resize()

        # Don't save window dimensions here - that happens in _save_window_size

        if hasattr(self, "_size_save_timeout_id"):
            self._size_save_timeout_id = None

        return False  # Don't repeat the timeout

    def on_clear_queue(self, button):
        """Show confirmation dialog before clearing the queue."""
        dialog = Adw.MessageDialog(
            transient_for=self,
            title="Clear Queue",
            body="Are you sure you want to clear the queue?",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_clear_queue_response)
        dialog.present()

    def _on_clear_queue_response(self, dialog, response):
        """Handle clear queue dialog response."""
        if response == "clear":
            self.file_queue.clear_queue()

    def on_playback_finished(self, player):
        """Handle playback completion and auto-play next file."""
        logger.info("Playback finished, checking for next track")

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

        # Load and play the file
        if self.player.load(file_path):
            # Tell the file queue which item is being played
            self.file_queue.set_currently_playing(index)
            self.player.play()

            # Generate waveform data in a separate thread
            threading.Thread(
                target=self.generate_waveform, args=(file_path,), daemon=True
            ).start()

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
        dialog.set_title("Select Audio Files")

        # Create file filters
        audio_filter = Gtk.FileFilter()
        audio_filter.set_name("Audio files (mp3, wav, ogg, etc.)")

        # Add common audio file extensions - make them lowercase to ensure matching
        for ext in ["mp3", "wav", "ogg", "flac", "m4a", "aac", "opus", "wma", "aiff"]:
            audio_filter.add_suffix(ext)
            audio_filter.add_suffix(ext.upper())  # Also add uppercase versions

        # Add all files filter
        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")

        # Add filters to the dialog
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(audio_filter)
        filters.append(all_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(audio_filter)

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

    def on_play_file(self, file_path, index):
        """Play a selected file."""
        # Check if we're already playing this file - if so, stop playback
        if self.player.is_playing() and self.player.current_file == file_path:
            self.player.stop()
            # Update the file queue UI to show play button
            if hasattr(self.file_queue, "update_playing_state"):
                self.file_queue.update_playing_state(False)
            # Don't reset active_audio_id when just stopping playback
            # This preserves the waveform visualization
            return

        # Save current markers if we have a current file
        if self.player.current_file and hasattr(self.visualizer, "get_marker_pairs"):
            current_markers = self.visualizer.get_marker_pairs()
            if current_markers:
                logger.debug(
                    f"Saving {len(current_markers)} markers for {self.player.current_file}"
                )
                self.file_markers[self.player.current_file] = current_markers
            elif self.player.current_file in self.file_markers:
                # If no markers but file was in cache, remove it from cache
                logger.debug(f"Removing cached markers for {self.player.current_file}")
                del self.file_markers[self.player.current_file]

        # Check if we're restarting the same file that was previously stopped
        same_file_as_active = file_path == self.active_audio_id

        # Only reset visualizer when changing to a different file
        if not same_file_as_active:
            # Reset visualizer before showing new content
            logger.debug(f"Resetting visualizer for new file: {file_path}")
            self.visualizer.set_waveform(None, 0)  # Clear waveform immediately

            # Store the file_path as the active audio ID
            self.active_audio_id = file_path
            logger.debug(f"Setting active_audio_id to: {file_path}")

            # Preserve markers_enabled flag but clear all markers first
            markers_enabled = self.visualizer.markers_enabled
            self.visualizer.clear_all_markers()
            self.visualizer.markers_enabled = markers_enabled

            # NOW generate waveform data in a separate thread - only when playing new file
            threading.Thread(
                target=self.generate_waveform, args=(file_path,), daemon=True
            ).start()

        # Otherwise load and play the file
        if self.player.load(file_path):
            # Tell the file queue which item is being played
            if hasattr(self.file_queue, "set_currently_playing"):
                self.file_queue.set_currently_playing(index)

            self.player.play()

            # For a new file, waveform generation was already initiated above
            # If restarting the same file, we don't need to regenerate the waveform

        return False  # Don't repeat the timeout

    def generate_waveform(self, file_path):
        """Generate waveform data for visualization."""
        try:
            import os
            import tempfile
            import subprocess
            import soundfile as sf
            import numpy as np

            if not os.path.exists(file_path):
                logger.error(f"File not found: {file_path}")
                GLib.idle_add(self.visualizer.set_waveform, None, 0)
                return

            logger.info(f"Generating waveform for: {file_path}")
            data = []
            duration = 0
            valid_waveform = False

            # Skip direct reading for files with special characters
            has_special_chars = any(ord(c) > 127 for c in file_path)

            if not has_special_chars:
                try:
                    # Try direct reading first
                    info = sf.info(file_path)
                    duration = info.duration

                    with sf.SoundFile(file_path) as sound_file:
                        data = sound_file.read()
                        if len(data.shape) > 1:  # Multi-channel - convert to mono
                            data = np.mean(data, axis=1)

                    # Validate the waveform data
                    valid_waveform = (
                        len(data) > 100
                        and not np.isnan(data).any()
                        and not np.isinf(data).any()
                    )
                except Exception:
                    valid_waveform = False

            # If direct reading failed, try FFmpeg conversion
            if not valid_waveform:
                temp_dir = tempfile.mkdtemp()
                temp_wav = os.path.join(temp_dir, "temp_visual.wav")

                try:
                    # Get FFmpeg path
                    ffmpeg_path = getattr(self.converter, "ffmpeg_path", "ffmpeg")

                    # Create FFmpeg command
                    cmd = [
                        ffmpeg_path,
                        "-v",
                        "error",
                        "-i",
                        file_path,
                        "-ac",
                        "1",
                        "-ar",
                        "44100",
                        "-sample_fmt",
                        "s16",
                        "-f",
                        "wav",
                        "-vn",
                        "-map_metadata",
                        "-1",
                        "-y",
                        temp_wav,
                    ]

                    # Run FFmpeg with timeout
                    result = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=30,  # Reduced timeout from 60 to 30 seconds
                    )

                    if (
                        result.returncode == 0
                        and os.path.exists(temp_wav)
                        and os.path.getsize(temp_wav) > 1000
                    ):
                        try:
                            info = sf.info(temp_wav)
                            duration = info.duration

                            with sf.SoundFile(temp_wav) as sound_file:
                                data = sound_file.read()
                                if len(data.shape) > 1:
                                    data = np.mean(data, axis=1)

                            valid_waveform = (
                                len(data) > 100
                                and not np.isnan(data).any()
                                and not np.isinf(data).any()
                            )
                        except Exception:
                            valid_waveform = False
                except subprocess.TimeoutExpired:
                    logger.error("FFmpeg conversion timed out")
                    valid_waveform = False
                finally:
                    # Clean up temp files
                    try:
                        if os.path.exists(temp_wav):
                            os.unlink(temp_wav)
                        os.rmdir(temp_dir)
                    except Exception:
                        pass

            # Update visualizer in main thread
            if valid_waveform and len(data) > 0:

                def update_visualizer():
                    self.visualizer.set_waveform(data, duration)
                    if (
                        file_path in self.file_markers
                        and self.visualizer.markers_enabled
                    ):
                        self.visualizer.restore_markers(self.file_markers[file_path])
                    return False

                GLib.idle_add(update_visualizer)

        except Exception as e:
            logger.error(f"Error generating waveform: {str(e)}")
            GLib.idle_add(self.visualizer.set_waveform, None, 0)

    def _restore_conversion_settings(self):
        """Restore saved conversion settings from config."""
        if not hasattr(self.app, "config") or not self.app.config:
            return

        # Restore format selection
        saved_format = self.app.config.get("conversion_format")
        if saved_format:
            # Directly check each format in the combo box
            formats = ["mp3", "ogg", "flac", "wav", "aac", "opus"]
            if saved_format in formats:
                self.format_combo.set_active(formats.index(saved_format))
            else:
                # Default to mp3 if format not found
                self.format_combo.set_active(0)
        else:
            # Default to mp3
            self.format_combo.set_active(0)

        # Restore bitrate selection
        saved_bitrate = self.app.config.get("conversion_bitrate")
        if saved_bitrate:
            # Directly check each bitrate in the combo box
            bitrates = ["64k", "128k", "192k", "256k", "320k"]
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
                    if saved_cut_enabled and saved_cut_enabled.lower() == "true":
                        self.cut_combo.set_active(1)  # Default to chronological
                    else:
                        self.cut_combo.set_active(0)  # Off
            else:
                # Fall back to enabled/disabled state
                saved_cut_enabled = self.app.config.get("cut_audio_enabled")
                if saved_cut_enabled and saved_cut_enabled.lower() == "true":
                    self.cut_combo.set_active(1)  # Default to chronological
                else:
                    self.cut_combo.set_active(0)  # Off

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

    def on_player_position_updated(self, player, position, duration):
        """Handle position updates from player."""
        self.visualizer.set_position(position)

        # Add auto-next detection as fallback if we reach end of track
        if duration > 0 and position >= duration - 0.2:  # Within 200ms of the end
            # Check if we haven't recently handled this
            if (
                not hasattr(self, "_end_of_track_handled")
                or not self._end_of_track_handled
            ):
                self._end_of_track_handled = True
                logger.debug("Detected end of track through position")
                GLib.idle_add(self.on_playback_finished, player)
        elif hasattr(self, "_end_of_track_handled") and self._end_of_track_handled:
            # Reset the flag when not at the end
            self._end_of_track_handled = False

    def on_player_duration_changed(self, player, duration):
        """Handle duration changes from player."""
        pass

    def on_visualizer_seek(self, position):
        """Handle seek request from visualizer."""
        # Simply pass the position to the player's seek method
        if hasattr(self.player, "seek"):
            self.player.seek(position)

    def _on_visualizer_height_changed(self, paned, param):
        """Handle visualizer height changes and save to config."""
        if not hasattr(self.app, "config") or not self.app.config:
            return

        # Get total height and position
        total_height = self.get_height()
        position = paned.get_position()

        # Calculate visualizer height (accounting for margins)
        visualizer_height = total_height - position - 20  # 20px for margins

        # Define minimum heights for both sections
        min_top_height = 200
        min_visualizer_height = 100

        # Make sure we don't resize the visualizer too small
        if visualizer_height < min_visualizer_height:
            # Calculate the maximum valid position to maintain minimum visualizer height
            max_position = total_height - min_visualizer_height - 20
            # Adjust the position
            paned.set_position(max_position)
            # Recalculate visualizer height
            visualizer_height = min_visualizer_height

        # Make sure we don't resize the top section too small (only check if visualizer constraint is satisfied)
        elif position < min_top_height:
            # Prevent the top section from getting too small
            paned.set_position(min_top_height)
            # Recalculate visualizer height
            visualizer_height = total_height - min_top_height - 20

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

    def _on_window_mapped(self, widget):
        """Called when the window is mapped (displayed on screen)."""
        # Use a short delay to ensure all allocations are done
        GLib.timeout_add(100, self._restore_visualizer_height)
        return False

    def _restore_visualizer_height(self):
        """Restore the visualizer height with proper window dimensions."""
        # Apply saved cut audio state to visualizer now that everything is created
        if hasattr(self, "visualizer") and hasattr(self, "cut_combo"):
            self.visualizer.set_markers_enabled(self.cut_combo.get_active() > 0)

        # Use the allocation-based height for accuracy
        window_height = self.get_height()
        logger.debug(f"Window height on map: {window_height}")

        if window_height < 100:  # Window not properly sized yet
            # Try again in a bit
            return True  # Keep trying

        # Use a short delay to ensure all allocations are done
        GLib.timeout_add(100, self._restore_visualizer_height)
        return False

    def _restore_visualizer_height(self):
        """Restore the visualizer height with proper window dimensions."""
        # Apply saved cut audio state to visualizer now that everything is created
        if hasattr(self, "visualizer") and hasattr(self, "cut_combo"):
            self.visualizer.set_markers_enabled(self.cut_combo.get_active() > 0)

        # Use the allocation-based height for accuracy
        window_height = self.get_height()
        logger.debug(f"Window height on map: {window_height}")

        if window_height < 100:  # Window not properly sized yet
            # Try again in a bit
            return True  # Keep trying

        # Calculate proper position from saved visualizer height
        visualizer_position = max(200, window_height - self.visualizer_height - 20)

        # Safety check - don't make visualizer too big or too small
        min_visualizer_height = 100
        max_visualizer_percent = 0.8

        # Ensure it's not too small
        if window_height - visualizer_position - 20 < min_visualizer_height:
            visualizer_position = window_height - min_visualizer_height - 20

        # And not too large
        if (
            window_height - visualizer_position - 20
            > window_height * max_visualizer_percent
        ):
            visualizer_position = (
                window_height - int(window_height * max_visualizer_percent) - 20
            )

        logger.debug(f"Setting visualizer position to: {visualizer_position}")

        # Set the position with all constraints in mind
        self.vertical_paned.set_position(visualizer_position)

        # Update visualizer content height
        actual_visualizer_height = window_height - visualizer_position - 20
        if hasattr(self.visualizer, "set_content_height"):
            self.visualizer.set_content_height(actual_visualizer_height)

        return False  # Don't repeat this timeout

    def on_player_state_changed(self, player, is_playing):
        """Handle player state changes to update UI."""
        # Update the play/stop button in the file queue
        if hasattr(self.file_queue, "update_playing_state"):
            GLib.idle_add(self.file_queue.update_playing_state, is_playing)

    def _show_cut_help_dialog(self, button):
        """Show help dialog with waveform cutting instructions."""
        dialog = Adw.MessageDialog(
            transient_for=self,
            title="Waveform Cutting Help",
            body="Learn how to use the waveform cutting feature:",
        )

        # Create content box for dialog
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(16)
        content.set_margin_end(16)

        # Add help sections with icons

        # Setting markers section
        markers_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        markers_icon = Gtk.Image.new_from_icon_name("cursor-mode-Click-symbolic")
        markers_icon.set_pixel_size(24)
        markers_box.append(markers_icon)

        markers_text = Gtk.Label(
            label="Click on the waveform to set markers.\n"
            + "First click sets start, second click sets end."
        )
        markers_text.set_halign(Gtk.Align.START)
        markers_text.set_wrap(True)
        markers_box.append(markers_text)
        markers_box.set_margin_bottom(6)
        content.append(markers_box)

        # Multiple segments section
        segments_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        segments_icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
        segments_icon.set_pixel_size(24)
        segments_box.append(segments_icon)

        segments_text = Gtk.Label(
            label="After confirming a segment, you can add more\n"
            + "segments by clicking again on the waveform."
        )
        segments_text.set_halign(Gtk.Align.START)
        segments_text.set_wrap(True)
        segments_box.append(segments_text)
        segments_box.set_margin_bottom(6)
        content.append(segments_box)

        # Remove segments section
        remove_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        remove_icon = Gtk.Image.new_from_icon_name("user-trash-symbolic")
        remove_icon.set_pixel_size(24)
        remove_box.append(remove_icon)

        remove_text = Gtk.Label(
            label="To remove a segment, click on it in the waveform\n"
            + "and confirm deletion when prompted."
        )
        remove_text.set_halign(Gtk.Align.START)
        remove_text.set_wrap(True)
        remove_box.append(remove_text)
        content.append(remove_box)

        # Add the content to the dialog
        dialog.set_extra_child(content)

        # Add close button
        dialog.add_response("close", "Close")

        # Show the dialog
        dialog.present()

    def _on_file_removed(self, file_id):
        """Handle when a file is removed from the queue."""
        # If the removed file was active in the visualizer, clear it
        if file_id == self.active_audio_id:
            self.visualizer.clear_waveform()
            self.active_audio_id = None

            # Additional cleanup if needed (e.g., stop playback, etc.)
            # ...
