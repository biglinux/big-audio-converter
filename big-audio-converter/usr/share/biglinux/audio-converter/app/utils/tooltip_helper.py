"""
Tooltip helper for showing helpful explanations on UI elements.
Provides a simple way to add tooltips to any GTK widget.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gdk, GLib

import gettext
gettext.textdomain("big-audio-converter")
_ = gettext.gettext

# Tooltip content dictionary
TOOLTIPS = {
    "format": _(
        "Select the output audio format\n\n"
        "• copy: Keeps the original audio data — only cutting is available\n"
        "• mp3: Widely supported, great balance between size and quality\n"
        "• ogg: Open format, good quality at smaller file sizes\n"
        "• flac: Lossless compression, perfect audio fidelity\n"
        "• wav: Uncompressed format, very large files\n"
        "• aac: Modern format with good overall quality\n"
        "• opus: Excellent quality at low bitrates"
    ),
    "bitrate": _(
        "Choose the audio bitrate (quality level)\n\n"
        "• 32k: Very low quality\n"
        "• 64k: Low quality\n"
        "• 128k: Standard quality\n"
        "• 192k: Good quality\n"
        "• 256k: High quality\n"
        "• 320k: Highest quality"
    ),
    "volume": _(
        "Adjust the audio volume\n\n"
        "• 100 = original volume\n"
        "• < 100 = lower volume\n"
        "• > 100 = higher volume\n\n"
        "Tip: Values above 100 may cause distortion"
    ),
    "speed": _(
        "Change playback and conversion speed\n\n"
        "• 1.0 = normal speed\n"
        "• < 1.0 = slower\n"
        "• > 1.0 = faster\n\n"
        "Pitch correction is applied automatically"
    ),
    "noise_reduction": _(
        "Reduce background noise during conversion.\n"
        "Should not be used in music. "
    ),
    "waveform": _(
        "Display audio as a visual waveform\n\n"
        "Easily edit by viewing your audio as a waveform.\n"
        "Disable this option to load large files faster."
    ),
    "equalizer": _(
        "Adjust the sound frequencies\n\n"
        "Fine-tune bass, midrange, and treble to enhance your audio."
    ),
    "cut": _(
        "Cut and export selected parts of your audio\n\n"
        "• Off: Converts the entire file\n"
        "• Chronological: Exports segments in timeline order\n"
        "• Segment Number: Exports segments by marking order"
    ),
    "waveform_visualizer": _(
        " UPPER AREA (Playback):\n"
        " Click the top half of the waveform to jump to that point "
        "and start playback.\n\n"
        " LOWER AREA (Segment Editing Zone):\n"
        " Click the bottom half to add segment markers:\n"
        " • First click: Set START marker (green)\n"
        " • Second click: Set END marker (red)\n\n"
        " ZOOM CONTROLS:\n"
        " • Mouse wheel: Zoom in or out for precise selection"
    ),
    "mouseover_tips": _(
        "You're seeing an example of help shown when hovering over an item."
    ),
    # Headerbar controls
    "clear_queue_button": _(
        "Remove all files from the queue"
    ),
    "prev_audio_btn": _(
        "Go to the previous audio file in the queue"
    ),
    "pause_play_btn": _(
        "Play or pause the current audio"
    ),
    "next_audio_btn": _(
        "Go to the next audio file in the queue"
    ),
    "play_selection_switch": _(
        "When enabled, playback automatically plays only the marked segments, skipping unselected parts"
    ),
    "auto_advance_switch": _(
        "When enabled, automatically plays the next track when current track finishes"
    ),
    # File queue controls
    "play_this_file": _(
        "Preview this audio file"
    ),
    "remove_from_queue": _(
        "Remove this file from the queue"
    ),
    "right_click_options": _(
        "Right-click for more options"
    ),
}

class TooltipHelper:
    """
    Manages a single, reusable Gtk.Popover to display custom tooltips.

    Rationale: This is the canonical implementation. It uses a singleton popover
    to prevent state conflicts. The animation is handled by CSS classes, and the
    fade-in is reliably triggered by hooking into the popover's "map" signal.
    This avoids all race conditions with the GTK renderer.
    """

    def __init__(self, config_manager):
        """Initialize the tooltip helper with config manager."""
        self.config_manager = config_manager

        # --- State Machine Variables ---
        self.active_widget = None
        self.show_timer_id = None

        # --- The Single, Reusable Popover ---
        self.popover = Gtk.Popover()
        self.popover.set_autohide(False)
        self.popover.set_has_arrow(True)
        self.popover.set_position(Gtk.PositionType.TOP)
        
        self.label = Gtk.Label(
            wrap=True,
            max_width_chars=50,
            margin_start=12,
            margin_end=12,
            margin_top=8,
            margin_bottom=8,
            halign=Gtk.Align.START,
        )
        self.popover.set_child(self.label)

        # --- CSS for Class-Based Animation ---
        self.css_provider = Gtk.CssProvider()
        css = b"""
        .tooltip-popover {
            opacity: 0;
            transition: opacity 200ms ease-in-out;
        }
        .tooltip-popover.visible {
            opacity: 1;
        }
        """
        self.css_provider.load_from_data(css)
        self.popover.add_css_class("tooltip-popover")
        
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Connect to the "map" signal to trigger the fade-in animation.
        self.popover.connect("map", self._on_popover_map)

    def _on_popover_map(self, popover):
        """Called when the popover is drawn. Adds the .visible class to fade in."""
        self.popover.add_css_class("visible")

    def is_enabled(self):
        """Check if tooltips are enabled in config."""
        enabled_str = self.config_manager.get("show_mouseover_tips", "true")
        return enabled_str.lower() == "true"

    def add_tooltip(self, widget, tooltip_key):
        """
        Connects a widget (or container) to the tooltip management system.
        """
        widget.tooltip_key = tooltip_key
        
        motion_controller = Gtk.EventControllerMotion.new()
        motion_controller.connect("enter", self._on_enter, widget)
        motion_controller.connect("leave", self._on_leave)
        widget.add_controller(motion_controller)

    def _clear_timer(self):
        if self.show_timer_id:
            GLib.source_remove(self.show_timer_id)
            self.show_timer_id = None

    def _on_enter(self, controller, x, y, widget):
        if not self.is_enabled() or self.active_widget == widget:
            return

        self._clear_timer()
        self._hide_tooltip()

        self.active_widget = widget
        self.show_timer_id = GLib.timeout_add(350, self._show_tooltip)

    def _on_leave(self, controller):
        self._clear_timer()
        if self.active_widget:
            self._hide_tooltip(animate=True)
            self.active_widget = None

    def _show_tooltip(self):
        if not self.active_widget:
            return GLib.SOURCE_REMOVE

        tooltip_key = self.active_widget.tooltip_key
        tooltip_text = TOOLTIPS.get(tooltip_key)

        if not tooltip_text:
            return GLib.SOURCE_REMOVE

        # Configure and place on screen. The popover is initially transparent
        # due to the .tooltip-popover class. The "map" signal will then
        # trigger the animation by adding the .visible class.
        self.label.set_text(tooltip_text)
        self.popover.set_parent(self.active_widget)
        self.popover.popup()
        
        self.show_timer_id = None
        return GLib.SOURCE_REMOVE

    def _hide_tooltip(self, animate=False):
        if not self.popover.is_visible():
            return

        def do_cleanup():
            self.popover.popdown()
            self.popover.unparent()
            return GLib.SOURCE_REMOVE

        # This triggers the fade-out animation.
        self.popover.remove_css_class("visible")

        if animate:
            # Wait for animation to finish before cleaning up.
            GLib.timeout_add(200, do_cleanup)
        else:
            do_cleanup()

    def cleanup(self):
        """Call this when the application is shutting down."""
        self._clear_timer()
        if self.popover.get_parent():
            self.popover.unparent()
