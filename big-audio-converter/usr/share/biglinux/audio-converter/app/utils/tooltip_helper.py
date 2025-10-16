"""
Tooltip helper for showing helpful explanations on UI elements.
Provides a simple way to add tooltips to any GTK widget.
"""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

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
        "Reduce background noise during conversion\n\n"
        "Applies a noise filter to improve clarity. "
        "Note: it only affects the converted file, not the live preview."
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
        "You’re seeing an example of help shown when hovering over an item."
    ),
}

class TooltipHelper:
    """Helper class to manage tooltips across the application."""

    def __init__(self, config_manager):
        """Initialize the tooltip helper with config manager."""
        self.config_manager = config_manager
        self.tooltip_popovers = {}  # Store popovers for cleanup
        self.tooltip_timers = {}  # Store timer IDs for delay

    def is_enabled(self):
        """Check if tooltips are enabled in config."""
        enabled_str = self.config_manager.get("show_mouseover_tips", "true")
        return enabled_str.lower() == "true"

    def add_tooltip(self, widget, tooltip_key):
        """
        Add a tooltip to a widget.

        Args:
            widget: The GTK widget to attach tooltip to
            tooltip_key: The key in TOOLTIPS dictionary
        """
        if not self.is_enabled():
            return

        tooltip_text = TOOLTIPS.get(tooltip_key)
        if not tooltip_text:
            return

        # Create popover for this widget
        popover = Gtk.Popover()
        popover.set_autohide(False)
        popover.set_position(Gtk.PositionType.TOP)
        popover.set_parent(widget)

        # Create label with tooltip text
        label = Gtk.Label()
        label.set_text(tooltip_text)
        label.set_wrap(True)
        label.set_max_width_chars(50)
        label.set_margin_start(12)
        label.set_margin_end(12)
        label.set_margin_top(8)
        label.set_margin_bottom(8)
        label.set_halign(Gtk.Align.START)
        popover.set_child(label)

        # Store popover reference
        self.tooltip_popovers[widget] = popover

        # Add motion controller to show/hide tooltip
        motion_controller = Gtk.EventControllerMotion.new()
        motion_controller.connect(
            "enter", lambda c, x, y: self._schedule_show_tooltip(widget, popover) if self.is_enabled() else None
        )
        motion_controller.connect("leave", lambda c: self._cancel_and_hide_tooltip(widget, popover))
        widget.add_controller(motion_controller)

    def _schedule_show_tooltip(self, widget, popover):
        """Schedule tooltip to show after 200ms delay."""
        # Cancel any existing timer for this widget
        if widget in self.tooltip_timers:
            GLib.source_remove(self.tooltip_timers[widget])
            del self.tooltip_timers[widget]
        
        # Schedule tooltip to show after 200ms
        timer_id = GLib.timeout_add(200, lambda: self._show_tooltip_with_animation(widget, popover))
        self.tooltip_timers[widget] = timer_id

    def _show_tooltip_with_animation(self, widget, popover):
        """Show tooltip popover with 200ms fade-in animation."""
        # Remove timer reference
        if widget in self.tooltip_timers:
            del self.tooltip_timers[widget]
        
        # Set initial opacity to 0
        popover.set_opacity(0.0)
        
        # Show the popover
        popover.popup()
        
        # Animate opacity from 0 to 1 over 200ms
        self._animate_opacity(popover, 0.0, 1.0, 200)
        
        return False  # Don't repeat timer

    def _cancel_and_hide_tooltip(self, widget, popover):
        """Cancel scheduled tooltip and hide if visible."""
        # Cancel any pending timer
        if widget in self.tooltip_timers:
            GLib.source_remove(self.tooltip_timers[widget])
            del self.tooltip_timers[widget]
        
        # Hide tooltip
        self._hide_tooltip(popover)

    def _animate_opacity(self, popover, start_opacity, end_opacity, duration_ms):
        """Animate popover opacity over specified duration."""
        steps = 20  # Number of animation steps
        step_duration = duration_ms // steps
        opacity_increment = (end_opacity - start_opacity) / steps
        current_step = [0]  # Use list to allow modification in nested function
        
        def update_opacity():
            current_step[0] += 1
            new_opacity = start_opacity + (opacity_increment * current_step[0])
            
            if current_step[0] >= steps:
                popover.set_opacity(end_opacity)
                return False  # Stop animation
            else:
                popover.set_opacity(new_opacity)
                return True  # Continue animation
        
        GLib.timeout_add(step_duration, update_opacity)

    def _hide_tooltip(self, popover):
        """Hide tooltip popover."""
        popover.popdown()

    def refresh_all(self):
        """Refresh all tooltips based on current settings."""
        enabled = self.is_enabled()

        for widget, popover in self.tooltip_popovers.items():
            if not enabled:
                # Hide all tooltips if disabled and cancel any pending timers
                if widget in self.tooltip_timers:
                    GLib.source_remove(self.tooltip_timers[widget])
                    del self.tooltip_timers[widget]
                popover.popdown()

    def cleanup(self):
        """Clean up all tooltip popovers and timers."""
        # Cancel all pending timers
        for timer_id in self.tooltip_timers.values():
            GLib.source_remove(timer_id)
        self.tooltip_timers.clear()
        
        # Clean up popovers
        for popover in self.tooltip_popovers.values():
            popover.unparent()
        self.tooltip_popovers.clear()
