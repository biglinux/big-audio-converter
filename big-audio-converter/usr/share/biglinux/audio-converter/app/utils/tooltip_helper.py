"""Help tooltips by key, shown as the shared BigLinux card while enabled."""

import gettext
import weakref

_ = gettext.gettext

TOOLTIPS = {
    "format": _("Choose the output format. Fast Copy does not apply effects."),
    "bitrate": _(
        "Higher values sound better and make larger files.\n"
        "96k: speech, smallest files\n"
        "128k: podcasts and audiobooks\n"
        "192k: music, a good balance\n"
        "320k: highest MP3 quality\n"
        "Available values depend on the format."
    ),
    "volume": _(
        "Changes preview and export volume. Values above 100% can distort the audio."
    ),
    "speed": _("Changes preview and export speed while preserving pitch."),
    "noise_reduction": _(
        "Reduce noise in speech with DeepFilterNet3 or DPDFNet. Not intended for music."
    ),
    "noise_gate": _("Reduce quiet background sound between spoken phrases."),
    "normalize": _("Adjust loudness to −16 LUFS. This changes the original volume."),
    "cut": _("Choose the order in which marked segments are exported."),
    "cut_output": _("Export each segment separately or join the selected segments."),
    "channels": _("Keep the original layout or explicitly downmix to mono or stereo."),
    "waveform_visualizer": _(
        "Click the waveform to mark cuts, or drag markers to adjust them. Use the seek bar to preview. Edit Segments provides numeric and keyboard controls."
    ),
    "clear_queue_button": _(
        "Remove all entries from the queue without deleting source files."
    ),
    "prev_audio_btn": _("Preview the previous file."),
    "pause_play_btn": _("Play or pause. Keyboard shortcut: Ctrl+Space."),
    "next_audio_btn": _("Preview the next file."),
    "play_selection_switch": _("Preview only the marked segments."),
    "auto_advance_switch": _("Preview the next track after the current track ends."),
    "eq_toggle_btn": _("Show the equalizer. Its settings affect exported audio."),
    "play_this_file": _("Preview this audio file."),
    "remove_from_queue": _("Remove this entry without deleting the source file."),
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
