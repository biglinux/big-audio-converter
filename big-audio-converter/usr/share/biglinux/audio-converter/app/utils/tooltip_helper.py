"""Short native tooltips with weak widget ownership and no custom timers."""

import gettext
import weakref
from typing import Protocol


class TooltipWidget(Protocol):
    def set_tooltip_text(self, text: str | None) -> None: ...

_ = gettext.gettext

TOOLTIPS = {
    'format': _('Choose the output format. Fast Copy does not apply effects.'),
    'bitrate': _('Higher bitrates generally use more space. Available values depend on the format.'),
    'volume': _('Changes preview and export volume. Values above 100% can distort the audio.'),
    'speed': _('Changes preview and export speed while preserving pitch.'),
    'noise_reduction': _('Reduce noise in speech using the installed GTCRN plugin. Not intended for music.'),
    'noise_strength': _('Lower strength retains more of the original sound.'),
    'noise_gate': _('Reduce quiet background sound between spoken phrases.'),
    'gate_threshold': _('Level below which the gate attenuates the sound.'),
    'gate_range': _('Maximum attenuation when the gate is closed.'),
    'gate_attack': _('How quickly the gate opens.'),
    'gate_release': _('How quickly the gate closes.'),
    'normalize': _('Adjust loudness to −16 LUFS. This changes the original volume.'),
    'waveform': _('Show an overview of peaks from every audio channel.'),
    'equalizer': _('Adjust low, middle and high frequencies.'),
    'cut': _('Choose the order in which marked segments are exported.'),
    'cut_output': _('Export each segment separately or join the selected segments.'),
    'channels': _('Keep the original layout or explicitly downmix to mono or stereo.'),
    'waveform_visualizer': _('Click the waveform to mark cuts, or drag markers to adjust them. Use the seek bar to preview. Edit Segments provides numeric and keyboard controls.'),
    'show_mouseover_tips': _('Show supplementary help for controls.'),
    'clear_queue_button': _('Remove all entries from the queue without deleting source files.'),
    'prev_audio_btn': _('Preview the previous file.'),
    'pause_play_btn': _('Play or pause. Keyboard shortcut: Ctrl+Space.'),
    'next_audio_btn': _('Preview the next file.'),
    'play_selection_switch': _('Preview only the marked segments.'),
    'auto_next_switch': _('Preview the next track after the current track ends.'),
    'eq_toggle_btn': _('Show the equalizer. Its settings affect exported audio.'),
    'zoom_btn': _('Adjust waveform magnification.'),
    'volume_btn': _('Adjust preview and export volume.'),
    'speed_btn': _('Adjust preview and export speed.'),
    'play_this_file': _('Preview this audio file.'),
    'remove_from_queue': _('Remove this entry without deleting the source file.'),
    'right_click_options': _('More actions are available from the adjacent menu button.'),
}


class TooltipHelper:
    def __init__(self, config=None):
        self.config = config
        self.widgets: weakref.WeakKeyDictionary[TooltipWidget, str] = weakref.WeakKeyDictionary()
        self.closed = False

    def is_enabled(self):
        return not self.closed and (self.config is None or str(self.config.get("show_mouseover_tips", "true")).lower() == "true")

    def add_tooltip(self, widget, tooltip_key, y_offset=0):
        """GTK owns placement, timing, keyboard help and high-contrast styling."""
        if not self.closed and tooltip_key in TOOLTIPS:
            self.widgets[widget] = TOOLTIPS[tooltip_key]
            widget.set_tooltip_text(TOOLTIPS[tooltip_key] if self.is_enabled() else None)

    def refresh(self):
        enabled = self.is_enabled()
        for widget, text in list(self.widgets.items()):
            widget.set_tooltip_text(text if enabled else None)

    def hide(self, immediate=False):
        self.refresh()

    def hide_all(self):
        self.refresh()

    def cleanup(self):
        self.closed = True
        self.refresh()
        self.widgets.clear()
        self.config = None
