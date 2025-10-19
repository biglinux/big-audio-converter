"""
Tooltip helper for showing helpful explanations on UI elements.
Provides a simple way to add tooltips to any GTK widget using the native GTK tooltip API.
"""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

import gettext
gettext.textdomain("big-audio-converter")
_ = gettext.gettext

# Tooltip content dictionary (remains the same)
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
    """Helper class to manage tooltips across the application."""

    def __init__(self, main_window):
        """Initialize the tooltip helper with the main window instance."""
        self.main_window = main_window
        self.config_manager = main_window.app.config
        # Store a map of widgets to their tooltip keys to re-apply them if needed
        self.widget_map = {}

    def is_enabled(self):
        """Check if tooltips are enabled in config."""
        enabled_str = self.config_manager.get("show_mouseover_tips", "true")
        return enabled_str.lower() == "true"

    def add_tooltip(self, widget, tooltip_key):
        """
        Add a tooltip to a widget using the native GTK tooltip API.

        Args:
            widget: The GTK widget to attach tooltip to.
            tooltip_key: The key in the tooltips dictionary.
        """
        # Store the relationship for later, e.g., if tooltips are toggled off and on
        self.widget_map[widget] = tooltip_key

        if not self.is_enabled():
            widget.set_tooltip_text(None)
            return

        tooltip_text = TOOLTIPS.get(tooltip_key)
        if tooltip_text:
            widget.set_tooltip_text(tooltip_text)

    # This method is no longer needed with the native API, but we keep it for compatibility
    # and just redirect it to the main add_tooltip method.
    def add_tooltip_to_container(self, container, tooltip_key):
        """Adds a tooltip to a container by calling the standard add_tooltip."""
        self.add_tooltip(container, tooltip_key)

    def refresh_all(self):
        """Refresh all tooltips based on current settings."""
        enabled = self.is_enabled()

        for widget, tooltip_key in self.widget_map.items():
            if enabled:
                tooltip_text = TOOLTIPS.get(tooltip_key)
                widget.set_tooltip_text(tooltip_text)
            else:
                widget.set_tooltip_text(None)

    def cleanup(self):
        """Clean up tooltip references."""
        # With the native API, there's less to clean, but clearing the map is good practice.
        self.widget_map.clear()