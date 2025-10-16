"""
Welcome dialog for Big Audio Converter
"""

import gettext
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from app.utils.config import AppConfig

gettext.textdomain("big-audio-converter")
_ = gettext.gettext


class WelcomeDialog:
    """Welcome dialog explaining audio converter features and benefits"""

    def __init__(self, parent_window):
        """Initialize the welcome dialog"""
        self.parent_window = parent_window
        self.config = AppConfig()
        self.dialog = None
        self.show_switch = None

        self.setup_ui()

    def setup_ui(self):
        """Set up the UI components"""
        # Create scrolled window for content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        # Removed fixed min sizes to allow adaptation to screen size

        # Content container
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_start(20)
        content_box.set_margin_end(20)
        content_box.set_margin_top(20)

        # Header with icon
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        header_box.set_halign(Gtk.Align.CENTER)

        app_icon = Gtk.Image.new_from_icon_name("big-audio-converter")
        app_icon.set_pixel_size(64)
        header_box.append(app_icon)

        title = Gtk.Label()
        title.set_markup(
            "<span size='xx-large' weight='bold'>"
            + _("Welcome to Big Audio Converter")
            + "</span>"
        )
        header_box.append(title)

        content_box.append(header_box)

        # Main content area with two columns
        columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        columns_box.set_margin_top(18)
        columns_box.set_halign(Gtk.Align.CENTER)
        columns_box.set_hexpand(True)

        # Left column - Features
        left_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left_column.set_hexpand(True)

        features = [
            (
                "🎵 " + _("Play and Convert Audio"),
                _("MP3, FLAC, OGG, WAV, AAC, Opus, WMA"),
            ),
            (
                "🎬 " + _("Extract Audio from Video"),
                _(
                    "Get the audio track from video files\n"
                    "and save it in any audio format"
                ),
            ),
            (
                "✂️ " + _("Cut and Merge Audio"),
                _(
                    "Mark sections on the visual waveform\n"
                    "and create new files with only those parts"
                ),
            ),
            (
                "⚡ " + _("Fast Copy Mode"),
                _(
                    "Cut audio in copy mode to fully preserve quality\n"
                    "with ultra-fast processing—no re-encoding"
                ),
            ),
        ]

        for title, description in features:
            left_column.append(self._create_feature_box(title, description))

        columns_box.append(left_column)

        # Right column - More features
        right_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        right_column.set_hexpand(True)

        more_features = [
            (
                "🔇 " + _("Noise Reduction"),
                _("Apply noise filter during conversion for cleaner audio"),
            ),
            (
                "🎚️ " + _("Volume and Equalizer"),
                _(
                    "Control audio volume levels and\n"
                    "apply equalization for better sound"
                ),
            ),
            (
                "⚙️ " + _("Speed Control"),
                _("Change playback and conversion speed\nto fit your needs"),
            ),
        ]

        for title, description in more_features:
            right_column.append(self._create_feature_box(title, description))

        columns_box.append(right_column)

        content_box.append(columns_box)

        # Separator before switch
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.set_margin_top(12)
        content_box.append(separator)

        # Don't show again switch
        switch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        switch_box.set_margin_top(12)

        self.show_switch = Gtk.Switch()
        self.show_switch.set_valign(Gtk.Align.CENTER)

        switch_label = Gtk.Label(label=_("Show dialog on startup"))
        switch_label.set_xalign(0)
        switch_label.set_hexpand(True)

        switch_box.append(switch_label)
        switch_box.append(self.show_switch)

        # Set initial state based on saved preference
        current_pref = self.config.get("show_welcome_dialog", True)
        self.show_switch.set_active(current_pref)

        content_box.append(switch_box)

        # Button box with close button
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        button_box.set_margin_top(18)
        button_box.set_halign(Gtk.Align.CENTER)

        close_button = Gtk.Button(label=_("Let's Start"))
        close_button.add_css_class("suggested-action")
        close_button.add_css_class("pill")
        close_button.set_size_request(150, -1)
        close_button.connect("clicked", self.on_close_clicked)

        button_box.append(close_button)
        content_box.append(button_box)

        # Add content box to scrolled window
        scrolled.set_child(content_box)

        # Create Adwaita Dialog with responsive sizing
        self.dialog = Adw.Dialog()
        # Use larger defaults but allow dialog to adapt to screen size
        self.dialog.set_content_width(900)
        self.dialog.set_content_height(650)

        # Set the scrolled window as the child
        self.dialog.set_child(scrolled)

    def present(self):
        """Present the dialog"""
        if self.dialog and self.parent_window:
            self.dialog.present(self.parent_window)

    def destroy(self):
        """Destroy/close the dialog"""
        if self.dialog:
            self.dialog.close()

    def _create_feature_box(self, title, description):
        """Create a feature box with title and description"""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        title_label = Gtk.Label()
        title_label.set_markup(title)
        title_label.set_halign(Gtk.Align.START)
        title_label.set_wrap(True)

        desc_label = Gtk.Label(label=description)
        desc_label.set_halign(Gtk.Align.START)
        desc_label.set_wrap(True)
        desc_label.set_xalign(0)
        desc_label.add_css_class("dim-label")
        desc_label.set_max_width_chars(40)

        box.append(title_label)
        box.append(desc_label)

        return box

    def on_close_clicked(self, button):
        """Handle close button click"""
        # Get the switch state and save preference
        if self.show_switch:
            show_on_startup = self.show_switch.get_active()
            self.config.set("show_welcome_dialog", show_on_startup)
        # Close the dialog
        if self.dialog:
            self.dialog.close()

    def on_response(self, dialog, response):
        """Handle dialog response"""
        # Get the switch state and save preference
        if self.show_switch:
            show_on_startup = self.show_switch.get_active()
            self.config.set("show_welcome_dialog", show_on_startup)

    @staticmethod
    def should_show_welcome():
        """Check if the welcome dialog should be shown"""
        config = AppConfig()
        return config.get("show_welcome_dialog", True)
