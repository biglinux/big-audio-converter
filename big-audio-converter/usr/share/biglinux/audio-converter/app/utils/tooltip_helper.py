"""Help tooltips by key, shown as the shared BigLinux card while enabled."""

import gettext
import weakref

_ = gettext.gettext

TOOLTIPS = {
    "waveform_visualizer": _(
        "Click to listen from a point. Drag to mark a part, then drag its edges or body to adjust it. Edit Segments provides numeric and keyboard controls."
    ),
    "prev_audio_btn": _("Preview the previous file."),
    "pause_play_btn": _("Play or pause. Keyboard shortcut: Ctrl+Space."),
    "next_audio_btn": _("Preview the next file."),
    "play_selection_switch": _("Preview only the marked segments."),
    "right_click_options": _(
        "More actions are available from the adjacent menu button."
    ),
}


class TooltipHelper:
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
        """big_gtk_kit.tooltip draws the card; GTK keeps the accessible text."""
        if not self.closed:
            self.widgets[widget] = TOOLTIPS[tooltip_key]
            widget.set_tooltip_text(
                TOOLTIPS[tooltip_key] if self.is_enabled() else None
            )

    def refresh(self):
        enabled = self.is_enabled()
        for widget, text in list(self.widgets.items()):
            widget.set_tooltip_text(text if enabled else None)

    def cleanup(self):
        self.closed = True
        self.refresh()
        self.widgets.clear()
        self.config = None
