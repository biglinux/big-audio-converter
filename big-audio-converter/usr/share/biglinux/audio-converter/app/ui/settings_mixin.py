# app/ui/settings_mixin.py

"""
Settings Manager Mixin for MainWindow.

Extracts all conversion-settings-related methods (format, bitrate, volume,
speed, noise reduction, gate, waveform, cut mode, equalizer toggle, and
settings persistence) from MainWindow into a reusable mixin.

Usage:
    class MainWindow(SettingsManagerMixin, PlaybackControllerMixin, Adw.ApplicationWindow):
        ...
"""

import gettext
import logging
import math
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from app.audio.profiles import BITRATES, SAMPLE_RATES

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class SettingsManagerMixin:
    """Mixin providing all conversion-settings logic for MainWindow."""

    # --- Conversion options UI setup ---

    def setup_conversion_options(self, parent_box):
        """Expose the common path first; keep format-specific controls contextual."""
        self._updating_profile = False
        def group(title):
            item = Adw.PreferencesGroup(title=title, margin_start=12, margin_end=12, margin_top=12)
            parent_box.append(item)
            return item

        def numeric_row(parent, title, value, lower, upper, step, callback, digits=2):
            row = Adw.ActionRow(title=title)
            row.set_title_lines(2)
            adjustment = Gtk.Adjustment(value=value, lower=lower, upper=upper,
                                        step_increment=step, page_increment=step * 5)
            control = Gtk.SpinButton(adjustment=adjustment, digits=digits,
                                     numeric=True, valign=Gtk.Align.CENTER, width_chars=5)
            control.update_property([Gtk.AccessibleProperty.LABEL], [title])
            control.connect("value-changed", callback)
            row.add_suffix(control)
            row.set_activatable_widget(control)
            parent.add_row(row)
            return row, control

        output = group(_("Output"))
        self._format_list = ["copy", "mp3", "ogg", "flac", "wav", "aac", "opus"]
        labels = [_("Fast Copy (no encoding)"), "MP3", "Ogg Vorbis", "FLAC", "WAV", "AAC", "Opus"]
        self.format_row = Adw.ComboRow(title=_("Format"), model=Gtk.StringList.new(labels))
        self.format_row.set_selected(1)
        self.format_row.connect("notify::selected", self._on_format_changed)
        output.add(self.format_row)
        self.copy_notice = Gtk.Label(label=_("Fast Copy keeps encoded audio. Cuts follow packet boundaries and may not match the exact times. Effects are bypassed in preview and export."),
                                    wrap=True, xalign=0, visible=False, margin_top=6)
        output.add(self.copy_notice)

        self.destination_row = Adw.ActionRow(title=_("Save to"), subtitle=_("Same folder as each source"))
        choose = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        choose.update_property([Gtk.AccessibleProperty.LABEL], [_("Choose output folder")])
        choose.connect("clicked", self._choose_output_folder)
        reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER)
        reset.update_property([Gtk.AccessibleProperty.LABEL], [_("Use source folders")])
        reset.connect("clicked", lambda *_: self._set_output_folder(""))
        self.destination_row.add_suffix(choose)
        self.destination_row.add_suffix(reset)
        output.add(self.destination_row)
        self.output_directory = str(self.app.config.get("output_directory", "") or "")
        if self.output_directory:
            self.destination_row.set_subtitle(self.output_directory)

        self._bitrate_list = list(BITRATES["mp3"])
        self.bitrate_row = Adw.ComboRow(title=_("Bitrate"), model=Gtk.StringList.new(self._bitrate_list))
        self.bitrate_row.set_selected(self._bitrate_list.index("192k"))
        self.bitrate_row.connect("notify::selected", self._on_bitrate_changed)
        output.add(self.bitrate_row)
        self.advanced_row = Adw.ExpanderRow(title=_("Advanced encoding"), subtitle=_("Keep original properties when supported"))
        self.channels_row = Adw.ComboRow(title=_("Channels"), model=Gtk.StringList.new([_("Original"), _("Mono"), _("Stereo")]))
        self.channels_row.connect("notify::selected", self._on_channels_changed)
        self.advanced_row.add_row(self.channels_row)
        self._sample_rate_list = ["original"] + [str(rate) for rate in SAMPLE_RATES["mp3"]]
        self.sample_rate_row = Adw.ComboRow(title=_("Sample rate"), model=Gtk.StringList.new([_("Original")] + [f"{rate} Hz" for rate in SAMPLE_RATES["mp3"]]))
        self.sample_rate_row.connect("notify::selected", self._on_sample_rate_changed)
        self.advanced_row.add_row(self.sample_rate_row)
        self.precision_row = Adw.SwitchRow(title=_("Allow conversion to 24-bit PCM"), subtitle=_("For floating-point audio exported to FLAC. Reduces precision; WAV preserves the original representation."), active=self.app.config.get("allow_precision_reduction", False) is True)
        self.precision_row.connect("notify::active", lambda row, _pspec: self.app.config.set("allow_precision_reduction", row.get_active()))
        self.advanced_row.add_row(self.precision_row)
        output.add(self.advanced_row)

        editing = group(_("Editing"))
        self._cut_list = [_("Off"), _("Chronological"), _("Segment Number")]
        self.cut_row = Adw.ComboRow(title=_("Cut order"), model=Gtk.StringList.new(self._cut_list))
        self.cut_row.connect("notify::selected", self._on_cut_combo_changed)
        editing.add(self.cut_row)
        self.cut_output_row = Adw.ComboRow(title=_("Save segments"), model=Gtk.StringList.new([_("Separate Files"), _("Merge into One")]), visible=False)
        self.cut_output_row.connect("notify::selected", self._on_cut_output_changed)
        editing.add(self.cut_output_row)
        self.segment_edit_button = Gtk.Button(label=_("Edit Segments…"), margin_top=6)
        self.segment_edit_button.connect("clicked", self.on_edit_segments)
        editing.add(self.segment_edit_button)
        self.cut_options_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        self.cut_options_box.append(Gtk.Label(label=_("Enter start and end times in Edit Segments, or mark sections on the waveform. Files without segments are converted in full."), wrap=True, xalign=0))
        self.waveform_row = Adw.SwitchRow(title=_("Show waveform"), active=True)
        self.waveform_row.connect("notify::active", self._on_waveform_switch_changed)
        self.cut_options_box.append(self.waveform_row)
        editing.add(self.cut_options_box)

        self.effects_group = group(_("Audio effects"))
        self.effects_group.set_description(_("These effects also change the exported audio."))
        self.effects_expander = Adw.ExpanderRow(title=_("Adjust sound"), subtitle=_("Volume, speed, filters and loudness"))
        self.effects_group.add(self.effects_expander)
        self.volume_spin = Adw.SpinRow.new_with_range(0, 1000, 5)
        self.volume_spin.set_title(_("Volume (%)"))
        self.volume_spin.set_value(100)
        self.volume_spin.connect("notify::value", lambda row, _pspec: self._on_volume_spin_changed(row))
        self.effects_expander.add_row(self.volume_spin)
        self.speed_spin = Adw.SpinRow.new_with_range(0.10, 5.0, 0.05)
        self.speed_spin.set_title(_("Speed (×)"))
        self.speed_spin.set_digits(2)
        self.speed_spin.set_value(1.0)
        self.speed_spin.connect("notify::value", lambda row, _pspec: self._on_speed_spin_changed(row))
        self.effects_expander.add_row(self.speed_spin)

        self.noise_expander = Adw.ExpanderRow(title=_("Noise Reduction"), subtitle=_("For speech, not music"), enable_expansion=False)
        self.noise_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.noise_switch.update_property([Gtk.AccessibleProperty.LABEL], [_("Enable noise reduction")])
        self.noise_switch.connect("state-set", self._on_noise_switch_changed)
        self.noise_expander.add_suffix(self.noise_switch)
        self.effects_expander.add_row(self.noise_expander)
        self.noise_strength_row, self.noise_strength_scale = numeric_row(self.noise_expander, _("Strength"), 1, 0, 1, .05, self._on_noise_strength_changed)
        self.noise_model_row = Adw.ComboRow(title=_("AI Model"), model=Gtk.StringList.new([_("Maximum Cleaning"), _("Natural Voice"), _("Smart (both combined)")]))
        self.noise_model_row.connect("notify::selected", self._on_noise_model_changed)
        self.noise_expander.add_row(self.noise_model_row)
        self.noise_speech_strength_row, self.noise_speech_strength_scale = numeric_row(self.noise_expander, _("Speech Strength"), 1, 0, 1, .05, self._on_noise_advanced_changed)
        self.noise_lookahead_row, self.noise_lookahead_scale = numeric_row(self.noise_expander, _("Lookahead (ms)"), 0, 0, 200, 5, self._on_noise_advanced_changed, 0)
        self.noise_voice_enhance_row, self.noise_voice_enhance_scale = numeric_row(self.noise_expander, _("Voice Enhance"), 0, 0, 1, .05, self._on_noise_advanced_changed)
        self.noise_available = bool(self.player.gtcrn_ladspa_path)
        self.noise_switch.set_sensitive(self.noise_available)
        if not self.noise_available:
            self.noise_expander.set_subtitle(_("Unavailable: install the GTCRN audio plugin"))

        for prefix, title, initial, handler, intensity_handler in (
            ("gate", _("Noise Gate"), .5, self._on_gate_switch_changed, self._on_gate_intensity_changed),
            ("compressor", _("Compressor"), 1, self._on_compressor_switch_changed, self._on_compressor_intensity_changed),
        ):
            expander = Adw.ExpanderRow(title=title, enable_expansion=False)
            switch = Gtk.Switch(valign=Gtk.Align.CENTER)
            switch.update_property([Gtk.AccessibleProperty.LABEL], [title])
            switch.connect("state-set", handler)
            expander.add_suffix(switch)
            row, spin = numeric_row(expander, _("Intensity"), initial, 0, 1, .05, intensity_handler)
            spin.set_sensitive(False)
            setattr(self, prefix + "_expander", expander)
            setattr(self, prefix + "_switch", switch)
            setattr(self, prefix + "_intensity_row", row)
            setattr(self, prefix + "_intensity_scale", spin)
            self.effects_expander.add_row(expander)

        self.hpf_row = Adw.SwitchRow(title=_("High-Pass Filter"), subtitle=_("Removes low-frequency rumble"))
        self.hpf_row.connect("notify::active", self._on_hpf_switch_changed)
        self.effects_expander.add_row(self.hpf_row)
        self.hpf_freq_row, self.hpf_freq_scale = numeric_row(self.effects_expander, _("Frequency (Hz)"), 80, 20, 500, 5, self._on_hpf_freq_changed, 0)
        self.hpf_freq_row.set_visible(False)
        self.transient_row = Adw.SwitchRow(title=_("Transient Suppressor"), subtitle=_("Suppresses clicks and plosives"))
        self.transient_row.connect("notify::active", self._on_transient_switch_changed)
        self.effects_expander.add_row(self.transient_row)
        self.transient_attack_row, self.transient_attack_scale = numeric_row(self.effects_expander, _("Attack"), -.5, -1, 0, .1, self._on_transient_attack_changed, 1)
        self.transient_attack_row.set_visible(False)
        plugin = self.player.gtcrn_ladspa_path
        self.transient_available = bool(plugin and (Path(plugin).parent / "transient_split.so").is_file())
        self.transient_row.set_sensitive(self.transient_available)
        if not self.transient_available:
            self.transient_row.set_subtitle(_("Unavailable: install the transient audio plugin"))
        self.normalize_row = Adw.SwitchRow(title=_("Loudness Normalization"), subtitle=_("Target: −16 LUFS; changes the original volume"))
        self.normalize_row.connect("notify::active", self._on_normalize_switch_changed)
        self.effects_expander.add_row(self.normalize_row)
        self.clipping_row = Adw.SwitchRow(title=_("Clipping protection"), subtitle=_("Limit peaks explicitly; may change dynamics"))
        self.clipping_row.connect("notify::active", self._on_clipping_changed)
        self.effects_expander.add_row(self.clipping_row)
        self.gain_notice = Gtk.Label(label=_("Boosting volume or equalizer bands can distort audio. Reduce gain or enable clipping protection."), wrap=True, xalign=0, margin_top=6)
        self.effects_group.add(self.gain_notice)
        self.noise_row = self.noise_expander
        self._restore_conversion_settings()
        self.clipping_row.set_active(self._config_bool("prevent_clipping"))
        self._on_format_changed(self.format_row, None)
        self._update_gain_notice()


    def _choose_output_folder(self, _button):
        dialog = Gtk.FileDialog(title=_("Choose output folder"))
        dialog.select_folder(self, self._dialog_cancellable, self._output_folder_selected)


    def _output_folder_selected(self, dialog, result):
        try:
            selected = dialog.select_folder_finish(result)
            if selected and not self._closed:
                path = selected.get_path()
                if path:
                    self._set_output_folder(path)
                else:
                    self._show_error_dialog(_("Local folder required"), _("Choose a folder on the local filesystem."))
        except GLib.Error as exc:
            if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                logger.debug("Could not select output folder: %s", exc)


    def _set_output_folder(self, path):
        self.output_directory = path
        self.app.config.set("output_directory", path)
        self.destination_row.set_subtitle(path or _("Same folder as each source"))


    def _on_sample_rate_changed(self, row, _pspec):
        if not self._updating_profile and row.get_selected() < len(self._sample_rate_list):
            self.app.config.set("sample_rate", self._sample_rate_list[row.get_selected()])


    def _on_clipping_changed(self, row, _pspec):
        self.app.config.set("prevent_clipping", str(row.get_active()).lower())
        self.player.set_prevent_clipping(row.get_active())

    # --- Settings change handlers ---

    def _on_format_changed(self, row, _pspec):
        if not hasattr(self, "clipping_row") or self._updating_profile:
            return
        format_name = self._format_list[row.get_selected()]
        self.app.config.set("conversion_format", format_name)
        self._updating_profile = True
        saved_bitrate = self.app.config.get("conversion_bitrate", "192k")
        self._bitrate_list = list(BITRATES.get(format_name, ("192k",)))
        self.bitrate_row.set_model(Gtk.StringList.new(self._bitrate_list))
        self.bitrate_row.set_selected(self._bitrate_list.index(saved_bitrate) if saved_bitrate in self._bitrate_list else self._bitrate_list.index("192k"))
        self.bitrate_row.set_visible(format_name in BITRATES)
        saved_rate = str(self.app.config.get("sample_rate", "original"))
        rates = SAMPLE_RATES.get(format_name, (8000, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000, 192000))
        self._sample_rate_list = ["original"] + [str(rate) for rate in rates]
        self.sample_rate_row.set_model(Gtk.StringList.new([_("Original")] + [f"{rate} Hz" for rate in rates]))
        selected_rate = self._sample_rate_list.index(saved_rate) if saved_rate in self._sample_rate_list else 0
        self.sample_rate_row.set_selected(selected_rate)
        self._updating_profile = False
        self._set_copy_mode_ui(format_name == "copy")

    def _on_bitrate_changed(self, row, _pspec):
        if not self._updating_profile and row.get_selected() < len(self._bitrate_list):
            self.app.config.set("conversion_bitrate", self._bitrate_list[row.get_selected()])

    def _on_channels_changed(self, row, pspec):
        """Handle channels selection change and save setting."""
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("audio_channels", str(row.get_selected()))

    def _on_volume_spin_changed(self, spin):
        self._set_processing_volume(spin.get_value())


    def _set_processing_volume(self, volume):
        if getattr(self, "_syncing_volume", False):
            return
        self._syncing_volume = True
        try:
            volume = max(0.0, min(1000.0, volume))
            self.volume_spin.set_value(volume)
            if hasattr(self, "volume_scale"):
                self.volume_scale.set_value(self._volume_to_slider(volume))
                self.volume_value_label.set_text(f"{volume:.0f}%")
                icon = "muted" if volume == 0 else "low" if volume < 33 else "medium" if volume <= 100 else "high"
                self.volume_btn.set_icon_name(f"audio-volume-{icon}-symbolic")
            self.player.set_volume(volume / 100)
            self.app.config.set("conversion_volume", str(volume))
            self._update_gain_notice()
        finally:
            self._syncing_volume = False


    def _update_gain_notice(self):
        if not hasattr(self, "gain_notice"):
            return
        boosted_eq = hasattr(self, "eq_panel") and any(scale.get_value() > 0 for scale in self.eq_panel.band_scales.values())
        self.gain_notice.set_visible(self.volume_spin.get_value() > 100.001 or boosted_eq)

    def _on_speed_spin_changed(self, spin):
        self._set_processing_speed(spin.get_value())


    def _set_processing_speed(self, speed):
        if getattr(self, "_syncing_speed", False):
            return
        self._syncing_speed = True
        try:
            speed = max(.1, min(5.0, speed))
            self.speed_spin.set_value(speed)
            if hasattr(self, "speed_scale"):
                self.speed_scale.set_value(self._speed_to_slider(speed))
                self.speed_value_label.set_text(f"{speed:.2f}×")
            self.player.set_playback_speed(speed)
            self.player.set_pitch_correction(True)
            self.app.config.set("conversion_speed", str(speed))
        finally:
            self._syncing_speed = False

    def _on_noise_switch_changed(self, switch, state):
        if state and not self.noise_available:
            return True
        self.app.config.set("conversion_noise_reduction", str(state).lower())
        self.noise_expander.set_enable_expansion(state)
        self.noise_expander.set_expanded(state)
        self.player.set_noise_reduction(state)
        return False

    def _on_noise_strength_changed(self, scale):
        """Handle noise reduction strength change and save setting."""
        strength = scale.get_value()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("noise_reduction_strength", str(strength))

        if hasattr(self.player, "set_noise_strength"):
            self.player.set_noise_strength(strength)

    def _on_noise_model_changed(self, row, pspec):
        """Handle noise model selection change.

        Index 0 = DNS3, 1 = VCTK, 2 = Intelligent Blending.
        """
        index = row.get_selected()
        if index == 0:
            model = 0
            blending = False
        elif index == 1:
            model = 1
            blending = False
        else:
            model = 0
            blending = True

        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("noise_model", str(model))
            self.app.config.set("noise_model_blend", str(blending).lower())
        if hasattr(self.player, "set_noise_model"):
            self.player.set_noise_model(model)
        if hasattr(self.player, "set_noise_advanced"):
            self.player.set_noise_advanced(
                speech_strength=self.noise_speech_strength_scale.get_value(),
                lookahead=int(self.noise_lookahead_scale.get_value()),
                voice_enhance=self.noise_voice_enhance_scale.get_value(),
                model_blend=blending,
            )

    def _on_noise_advanced_changed(self, *args):
        """Handle any GTCRN advanced control change."""
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("noise_speech_strength", str(self.noise_speech_strength_scale.get_value()))
            self.app.config.set("noise_lookahead", str(int(self.noise_lookahead_scale.get_value())))
            self.app.config.set("noise_voice_enhance", str(self.noise_voice_enhance_scale.get_value()))
        # Derive blending from model combo index
        model_index = self.noise_model_row.get_selected()
        blending = model_index == 2
        if hasattr(self.player, "set_noise_advanced"):
            self.player.set_noise_advanced(
                speech_strength=self.noise_speech_strength_scale.get_value(),
                lookahead=int(self.noise_lookahead_scale.get_value()),
                voice_enhance=self.noise_voice_enhance_scale.get_value(),
                model_blend=blending,
            )

    def _on_gate_switch_changed(self, switch, state):
        """Handle noise gate toggle."""
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("gate_enabled", str(state).lower())

        self.gate_expander.set_enable_expansion(state)
        self.gate_intensity_scale.set_sensitive(state)

        if not state:
            self.gate_expander.set_expanded(False)

        if hasattr(self.player, "set_gate_enabled"):
            self.player.set_gate_enabled(state)

        return False

    def _on_gate_intensity_changed(self, scale):
        """Handle gate intensity slider change."""
        intensity = scale.get_value()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("gate_intensity", str(intensity))

        if hasattr(self.player, "set_gate_intensity"):
            self.player.set_gate_intensity(intensity)

    def _on_compressor_switch_changed(self, switch, state):
        """Handle compressor toggle."""
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("compressor_enabled", str(state).lower())

        self.compressor_expander.set_enable_expansion(state)
        self.compressor_intensity_scale.set_sensitive(state)

        if not state:
            self.compressor_expander.set_expanded(False)

        if hasattr(self.player, "set_compressor_enabled"):
            self.player.set_compressor_enabled(state)

        return False

    def _on_compressor_intensity_changed(self, scale):
        """Handle compressor intensity change."""
        intensity = scale.get_value()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("compressor_intensity", str(intensity))

        if hasattr(self.player, "set_compressor_intensity"):
            self.player.set_compressor_intensity(intensity)

    def _on_hpf_switch_changed(self, row, pspec):
        """Handle high-pass filter toggle."""
        state = row.get_active()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("hpf_enabled", str(state).lower())

        self.hpf_freq_row.set_visible(state)

        if hasattr(self.player, "set_hpf_enabled"):
            self.player.set_hpf_enabled(state)

    def _on_hpf_freq_changed(self, scale):
        """Handle HPF frequency change."""
        freq = int(scale.get_value())
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("hpf_frequency", str(freq))

        if hasattr(self.player, "set_hpf_frequency"):
            self.player.set_hpf_frequency(freq)

    def _on_transient_switch_changed(self, row, pspec):
        """Handle transient suppressor toggle."""
        state = row.get_active()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("transient_enabled", str(state).lower())

        self.transient_attack_row.set_visible(state)

        if hasattr(self.player, "set_transient_enabled"):
            self.player.set_transient_enabled(state)

    def _on_transient_attack_changed(self, scale):
        """Handle transient attack change."""
        attack = scale.get_value()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("transient_attack", str(attack))

        if hasattr(self.player, "set_transient_attack"):
            self.player.set_transient_attack(attack)

    def _on_normalize_switch_changed(self, row, _pspec):
        state = row.get_active()
        self.app.config.set("normalize_enabled", str(state).lower())
        self.player.set_normalize(state)

    def _on_waveform_switch_changed(self, row, pspec):
        enabled = row.get_active()
        self.app.config.set("generate_waveforms", str(enabled).lower())
        if self.active_audio_id:
            self._request_waveform(self.active_audio_id, enabled=enabled and self.cut_row.get_selected() > 0)

    def _on_cut_combo_changed(self, row, pspec):
        """Handle cut audio combo box changes."""
        active = row.get_selected()
        # Enable markers and show options when any option except "Off" is selected
        enabled = active > 0

        # Show/hide cut options based on selection
        self.cut_options_box.set_visible(enabled)

        # Show/hide segment output option
        if hasattr(self, "cut_output_row"):
            self.cut_output_row.set_visible(enabled)

        # Enable/disable waveform markers
        if hasattr(self, "visualizer"):
            self.visualizer.set_markers_enabled(enabled)

        # Show/hide waveform-related UI elements based on cut mode
        if hasattr(self, "play_selection_switch"):
            self.play_selection_switch.set_visible(enabled)
        if hasattr(self, "zoom_box"):
            self.zoom_box.set_visible(enabled)
        if hasattr(self, "seekbar"):
            self.seekbar.set_visible(True)
        if hasattr(self, "visualizer_frame"):
            self.visualizer_frame.set_visible(enabled)

        # Collapse or expand the waveform area in the paned
        self._update_paned_for_cut_mode(enabled)

        # Generate waveform if enabling cut and active file has no waveform data
        if (enabled and hasattr(self, "active_audio_id") and self.active_audio_id) and (hasattr(self, "visualizer") and self.visualizer.waveform_data is None):
            self._request_waveform(self.active_audio_id, enabled=None)

        if not enabled and self.active_audio_id:
            self._request_waveform(self.active_audio_id, enabled=False)

        # Save setting
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("cut_audio_enabled", str(enabled).lower())
            self.app.config.set("cut_audio_mode", str(active))

    def _on_cut_output_changed(self, row, pspec):
        """Handle cut output mode change (separate files vs merge)."""
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("cut_output_mode", str(row.get_selected()))

    def _update_paned_for_cut_mode(self, cut_enabled):
        """Collapse or restore the paned position based on cut mode."""
        if not hasattr(self, "vertical_paned") or not hasattr(self, "visualizer_container"):
            return
        if not self.visualizer_container.get_visible():
            return

        total_height = self.get_height()
        if total_height <= 0:
            return

        if not cut_enabled:
            # Save the current paned position before collapsing
            current_pos = self.vertical_paned.get_position()
            controls_bar_height = 48
            seekbar_height = 36
            collapse_pos = total_height - controls_bar_height - seekbar_height
            # Only save if not already collapsed
            if current_pos < collapse_pos - 10:
                self._saved_paned_position = current_pos
            self.vertical_paned.set_position(collapse_pos)
        else:
            # Restore saved paned position
            if hasattr(self, "_saved_paned_position") and self._saved_paned_position:
                self.vertical_paned.set_position(self._saved_paned_position)
            else:
                # Fallback: use saved visualizer height
                visualizer_position = max(200, total_height - self.visualizer_height - 50)
                self.vertical_paned.set_position(visualizer_position)

    # --- Equalizer toggle ---

    def _on_eq_toggle_clicked(self, button):
        """Toggle the inline equalizer panel visibility (from bottom bar)."""
        self.eq_revealer.set_reveal_child(button.get_active())

    def _on_eq_revealer_changed(self, revealer, pspec):
        """Sync equalizer toggle button with revealer state."""
        is_revealed = revealer.get_reveal_child()
        if (
            hasattr(self, "eq_toggle_btn")
            and self.eq_toggle_btn.get_active() != is_revealed
        ):
            self.eq_toggle_btn.handler_block_by_func(self._on_eq_toggle_clicked)
            self.eq_toggle_btn.set_active(is_revealed)
            self.eq_toggle_btn.handler_unblock_by_func(self._on_eq_toggle_clicked)

    # --- Copy mode UI ---

    def _set_copy_mode_ui(self, is_copy_mode):
        """Bypass effects without discarding the user's saved processing choices."""
        self.copy_notice.set_visible(is_copy_mode)
        self.precision_row.set_visible(self._format_list[self.format_row.get_selected()] == "flac")
        self.advanced_row.set_visible(not is_copy_mode)
        self.effects_group.set_sensitive(not is_copy_mode)
        self.player.set_effects_bypassed(is_copy_mode)
        for name in ("volume_btn", "speed_btn", "eq_toggle_btn"):
            if hasattr(self, name):
                getattr(self, name).set_sensitive(not is_copy_mode)
        if is_copy_mode:
            for name in ("volume_popover", "speed_popover"):
                if hasattr(self, name):
                    getattr(self, name).popdown()
            if hasattr(self, "eq_revealer"):
                self.eq_revealer.set_reveal_child(False)

    # --- Slider / value conversion utilities ---

    def _slider_to_volume(self, slider_value):
        """Convert slider position (0-100) to volume (0-1000) with quadratic curve."""
        return (slider_value / 100.0) ** 2 * 1000.0

    def _volume_to_slider(self, volume):
        """Convert volume (0-1000) to slider position (0-100)."""
        if volume <= 0:
            return 0.0
        return math.sqrt(volume / 1000.0) * 100.0

    def _slider_to_speed(self, slider_value):
        """Convert slider position (0-100) to speed (0.10-5.0) with logarithmic curve."""
        return 0.10 * math.pow(50.0, slider_value / 100.0)

    def _speed_to_slider(self, speed):
        """Convert speed (0.10-5.0) to slider position (0-100)."""
        if speed <= 0.10:
            return 0.0
        return math.log10(speed / 0.10) / math.log10(50.0) * 100.0

    # --- Settings persistence ---

    def _config_float(self, key, default):
        """Get a float config value, returning default on missing/invalid."""
        saved = self.app.config.get(key)
        if saved is not None:
            try:
                return float(saved)
            except (ValueError, TypeError):
                pass
        return default

    def _config_int(self, key, default, lo=None, hi=None):
        """Get an int config value, clamped to [lo, hi] if given."""
        saved = self.app.config.get(key)
        if saved is not None:
            try:
                val = int(saved)
                if lo is not None and val < lo:
                    return default
                if hi is not None and val > hi:
                    return default
                return val
            except (ValueError, TypeError):
                pass
        return default

    def _config_bool(self, key, default=False):
        """Get a boolean config value."""
        saved = self.app.config.get(key)
        if saved is not None:
            return str(saved).lower() == "true"
        return default

    def _config_list_index(self, key, valid_list, default_idx):
        """Get a config value's index in valid_list, or default_idx."""
        saved = self.app.config.get(key)
        if saved and saved in valid_list:
            return valid_list.index(saved)
        return default_idx

    def _restore_conversion_settings(self):
        """Restore saved conversion settings from config."""
        if not hasattr(self.app, "config") or not self.app.config:
            return

        # Restore format selection
        self.format_row.set_selected(
            self._config_list_index("conversion_format", self._format_list, 1)
        )

        # Don't show copy mode dialog or apply UI on startup
        # The format change handler will apply the UI state

        # Mark initialization as complete
        self._initializing = False

        # Restore bitrate selection
        self.bitrate_row.set_selected(
            self._config_list_index("conversion_bitrate", self._bitrate_list, 3)
        )

        # Set bitrate visibility based on format (only lossy formats use bitrate)
        current_format = self._format_list[self.format_row.get_selected()]
        lossy_formats = ("mp3", "ogg", "aac", "opus")
        self.bitrate_row.set_visible(current_format in lossy_formats)

        # Restore channels
        if hasattr(self, "channels_row"):
            self.channels_row.set_selected(
                self._config_int("audio_channels", 0, lo=0, hi=2)
            )
            # Hide channels in copy mode
            self.channels_row.set_visible(current_format != "copy")

        # Restore volume
        vol_val = self._config_float("conversion_volume", 100)
        self.volume_spin.set_value(vol_val)
        if hasattr(self, "volume_scale"):
            self.volume_scale.set_value(self._volume_to_slider(vol_val))
            self.volume_value_label.set_text(f"{int(vol_val)}")

        # Restore speed
        spd_val = self._config_float("conversion_speed", 1.0)
        self.speed_spin.set_value(spd_val)
        if hasattr(self, "speed_scale"):
            self.speed_scale.set_value(self._speed_to_slider(spd_val))
            self.speed_value_label.set_text(f"{spd_val:.2f}x")

        # Restore noise reduction
        self.noise_switch.set_active(self.noise_available and self._config_bool("conversion_noise_reduction"))

        # Restore noise reduction strength
        self.noise_strength_scale.set_value(
            self._config_float("noise_reduction_strength", 1.0)
        )

        # Restore GTCRN advanced controls — derive combo index from model + blending
        saved_model = self._config_int("noise_model", 0, lo=0, hi=1)
        saved_blending = self._config_bool("noise_model_blend")
        if saved_blending:
            model_combo_index = 2  # Smart (both combined)
        elif saved_model == 1:
            model_combo_index = 1  # Natural Voice (VCTK)
        else:
            model_combo_index = 0  # Maximum Cleaning (DNS3)
        self.noise_model_row.set_selected(model_combo_index)

        self.noise_speech_strength_scale.set_value(
            self._config_float("noise_speech_strength", 1.0)
        )
        self.noise_lookahead_scale.set_value(
            self._config_float("noise_lookahead", 0)
        )
        self.noise_voice_enhance_scale.set_value(
            self._config_float("noise_voice_enhance", 0.0)
        )

        # Restore noise gate settings (intensity slider)
        self.gate_switch.set_active(self._config_bool("gate_enabled"))
        self.gate_expander.set_expanded(False)
        self.gate_intensity_scale.set_value(
            self._config_float("gate_intensity", 0.5)
        )

        # Restore compressor
        self.compressor_switch.set_active(self._config_bool("compressor_enabled"))
        self.compressor_expander.set_expanded(False)
        self.compressor_intensity_scale.set_value(
            self._config_float("compressor_intensity", 1.0)
        )

        # Restore HPF
        self.hpf_row.set_active(self._config_bool("hpf_enabled"))
        self.hpf_freq_scale.set_value(self._config_float("hpf_frequency", 80))

        # Restore transient
        self.transient_row.set_active(self.transient_available and self._config_bool("transient_enabled"))
        self.transient_attack_scale.set_value(
            self._config_float("transient_attack", -0.5)
        )

        # Restore normalization
        self.normalize_row.set_active(self._config_bool("normalize_enabled"))

        # Restore cut audio mode if present
        if hasattr(self, "cut_row"):
            mode = self._config_int("cut_audio_mode", -1, lo=0, hi=2)
            if mode < 0:
                # Legacy key fallback
                mode = 1 if self._config_bool("cut_audio_enabled") else 0
            self.cut_row.set_selected(mode)

            # Set visibility based on combo selection
            active = self.cut_row.get_selected()
            if hasattr(self, "cut_options_box"):
                self.cut_options_box.set_visible(active > 0)

            # Restore cut output mode (separate files vs merge)
            if hasattr(self, "cut_output_row"):
                self.cut_output_row.set_visible(active > 0)
                self.cut_output_row.set_selected(
                    self._config_int("cut_output_mode", 0, lo=0, hi=1)
                )

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
