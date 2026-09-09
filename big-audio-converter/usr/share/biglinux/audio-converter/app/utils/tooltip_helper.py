"""Native, concise contextual help for the audio converter."""

import gettext

_ = gettext.gettext

TOOLTIPS = {
    'format': _('Choose the output format. Copy mode keeps encoded audio and uses approximate cuts.'),
    'bitrate': _('Choose the target bitrate. Suitable values depend on the codec, channels and content.'),
    'volume': _('100% keeps the original gain. Higher gain can cause clipping.'),
    'speed': _('Change playback and conversion speed'),
    'noise_reduction': _('Optional neural denoising for speech. It can damage music.'),
    'noise_reduction_strength': _('Adjust the intensity of noise reduction'),
    'noise_gate': _('Noise gate silences audio below a volume threshold'),
    'gate_threshold': _('Volume level below which the gate closes'),
    'gate_range': _('How much the audio is attenuated when the gate closes'),
    'gate_attack': _('How quickly the gate opens when signal exceeds threshold'),
    'gate_release': _('How quickly the gate closes after signal drops below threshold'),
    'waveform': _('Display audio as a visual waveform'),
    'equalizer': _('Adjust the sound frequencies'),
    'cut': _('Cut and export selected parts of your audio'),
    'cut_output': _('Choose how cut segments are saved'),
    'channels': _('Limit the number of audio channels'),
    'waveform_visualizer': _('Seek in the upper area or mark cuts below. Edit Segments provides a keyboard-accessible alternative.'),
    'mouseover_tips': _("You're seeing an example of help shown when hovering over an item."),
    'clear_queue_button': _('Remove all files from the queue'),
    'prev_audio_btn': _('Go to the previous audio file in the queue'),
    'pause_play_btn': _('Play or pause the current audio'),
    'next_audio_btn': _('Go to the next audio file in the queue'),
    'play_selection_switch': _('When enabled, playback automatically plays only the marked segments, skipping unselected parts'),
    'auto_advance_switch': _('When enabled, automatically plays the next track when current track finishes'),
    'eq_toggle_btn': _('Show or hide the equalizer panel to adjust audio frequencies'),
    'zoom_btn': _('Open zoom control to adjust the waveform view magnification'),
    'volume_btn': _('Adjust the output volume level (0-1000%)'),
    'speed_btn': _('Adjust the playback and conversion speed'),
    'play_this_file': _('Preview this audio file'),
    'remove_from_queue': _('Remove this file from the queue'),
    'right_click_options': _('Right-click for more options'),
    'normalize': _('Normalize to -16 LUFS with a -1.5 dBTP true-peak limit. This is not the EBU R128 broadcast target.'),
}


class TooltipHelper:
    """Use GTK's tooltip placement, theme and lifecycle instead of custom popovers."""
    def __init__(self, config_manager=None):
        import weakref
        self.config_manager = config_manager
        self._widgets = weakref.WeakKeyDictionary()

    def is_enabled(self):
        return self.config_manager is None or str(self.config_manager.get("show_mouseover_tips", "true")).lower() == "true"

    def add_tooltip(self, widget, tooltip_key, y_offset=0):
        """Retain the legacy signature; native GTK controls tooltip placement."""
        text = TOOLTIPS.get(tooltip_key)
        if text:
            self._widgets[widget] = text
            widget.set_tooltip_text(text if self.is_enabled() else None)

    def refresh(self):
        for widget, text in list(self._widgets.items()):
            widget.set_tooltip_text(text if self.is_enabled() else None)

    def hide(self, immediate=False):
        # Native tooltips are dismissed by GTK when focus/pointer context changes.
        self.refresh()

    def hide_all(self):
        self.refresh()

    def cleanup(self):
        for widget in list(self._widgets):
            widget.set_tooltip_text(None)
        self._widgets.clear()
