"""Help tooltips by key, and the choice to show tooltips at all."""

import gettext
import weakref

from big_gtk_kit import tooltip

_ = gettext.gettext

TOOLTIPS = {
    "waveform_visualizer": _(
        "Click to listen from a point. Drag to mark a segment, then drag its edges or body to adjust it. Edit Segments provides numeric and keyboard controls."
    ),
    "prev_audio_btn": _("Preview the previous file."),
    "pause_play_btn": _("Play or pause. Keyboard shortcut: Ctrl+Space."),
    "next_audio_btn": _("Preview the next file."),
    "play_selection_switch": _("Preview only the marked segments."),
    "segment_edit_button": _(
        "Set the exact times of each segment. Keyboard shortcut: Ctrl+E."
    ),
}


class HelpTooltips:
    def __init__(self, config=None):
        self.config = config
        self.widgets = weakref.WeakKeyDictionary()
        self.closed = False

    def is_enabled(self):
        return not self.closed and (
            self.config is None
            or str(self.config.get("show_mouseover_tips", "true")).lower() == "true"
        )

    def add_tooltip(self, widget, tooltip_key):
        """big_gtk_kit.tooltip draws the card; the text stays while tooltips
        are off, as the widget's accessible description."""
        if not self.closed:
            self.widgets[widget] = TOOLTIPS[tooltip_key]
            widget.set_tooltip_text(TOOLTIPS[tooltip_key])

    def refresh(self):
        """Show every tooltip of the program, or none; returns which."""
        enabled = self.is_enabled()
        tooltip.set_enabled(enabled)
        return enabled

    def cleanup(self):
        self.closed = True
        self.widgets.clear()
        self.config = None
