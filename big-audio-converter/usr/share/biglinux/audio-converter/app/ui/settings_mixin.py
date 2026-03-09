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
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from app.audio import waveform

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class SettingsManagerMixin:
    """Mixin providing all conversion-settings logic for MainWindow."""

    # --- Conversion options UI setup ---

    def setup_conversion_options(self, parent_box):
        """Set up the conversion options UI following Adwaita HIG."""
        # --- Output group (no title, first group) ---
        output_group = Adw.PreferencesGroup()
        output_group.set_margin_start(12)
        output_group.set_margin_end(12)
        output_group.set_margin_top(0)

        self._format_list = ["copy", "mp3", "ogg", "flac", "wav", "aac", "opus"]
        format_model = Gtk.StringList.new(self._format_list)
        self.format_row = Adw.ComboRow(title=_("Output Format"), model=format_model)
        self.format_row.set_selected(1)
        self.format_row.connect("notify::selected", self._on_format_changed)
        output_group.add(self.format_row)

        self._bitrate_list = ["32k", "64k", "128k", "192k", "256k", "320k"]
        bitrate_model = Gtk.StringList.new(self._bitrate_list)
        self.bitrate_row = Adw.ComboRow(title=_("Bitrate"), model=bitrate_model)
        self.bitrate_row.set_selected(2)
        self.bitrate_row.connect("notify::selected", self._on_bitrate_changed)
        output_group.add(self.bitrate_row)

        # Audio channels
        self._channels_list = [_("Original"), _("Mono"), _("Stereo")]
        channels_model = Gtk.StringList.new(self._channels_list)
        self.channels_row = Adw.ComboRow(title=_("Channels"), model=channels_model)
        self.channels_row.set_selected(0)
        self.channels_row.connect("notify::selected", self._on_channels_changed)
        output_group.add(self.channels_row)

        parent_box.append(output_group)

        # Volume (hidden SpinRow, synced with bottom bar scale)
        self.volume_spin = Adw.SpinRow.new_with_range(0, 1000, 5)
        self.volume_spin.set_title(_("Volume"))
        self.volume_spin.set_value(100)
        self.volume_spin.connect("changed", self._on_volume_spin_changed)

        # Speed (hidden SpinRow, synced with bottom bar scale)
        self.speed_spin = Adw.SpinRow.new_with_range(0.10, 5.0, 0.05)
        self.speed_spin.set_title(_("Speed"))
        self.speed_spin.set_digits(2)
        self.speed_spin.set_value(1.0)
        self.speed_spin.connect("changed", self._on_speed_spin_changed)

        # Waveform toggle (internal use only, not shown in UI)
        self.waveform_row = Adw.SwitchRow(title=_("Generate Waveforms"))
        self.waveform_row.set_active(True)
        self.waveform_row.connect("notify::active", self._on_waveform_switch_changed)

        # --- Cut group (before noise reduction) ---
        cut_group = Adw.PreferencesGroup(title=_("Cut"))
        cut_group.set_margin_start(12)
        cut_group.set_margin_end(12)
        cut_group.set_margin_top(6)

        self._cut_list = [_("Off"), _("Chronological"), _("Segment Number")]
        cut_model = Gtk.StringList.new(self._cut_list)
        self.cut_row = Adw.ComboRow(title=_("Mode"), model=cut_model)
        self.cut_row.set_selected(0)
        self.cut_row.connect("notify::selected", self._on_cut_combo_changed)
        cut_group.add(self.cut_row)

        # Segment output mode: separate files or merge into one
        self._cut_output_list = [_("Separate Files"), _("Merge into One")]
        cut_output_model = Gtk.StringList.new(self._cut_output_list)
        self.cut_output_row = Adw.ComboRow(title=_("Output"), model=cut_output_model)
        self.cut_output_row.set_selected(0)
        self.cut_output_row.set_visible(False)
        self.cut_output_row.connect("notify::selected", self._on_cut_output_changed)
        cut_group.add(self.cut_output_row)

        parent_box.append(cut_group)

        # Cut instructions (initially hidden)
        self.cut_options_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.cut_options_box.set_margin_start(12)
        self.cut_options_box.set_margin_end(12)
        self.cut_options_box.set_visible(False)
        parent_box.append(self.cut_options_box)

        # --- Noise reduction group ---
        noise_group = Adw.PreferencesGroup(title=_("Noise Reduction"))
        noise_group.set_margin_start(12)
        noise_group.set_margin_end(12)
        noise_group.set_margin_top(6)

        self.noise_expander = Adw.ExpanderRow(title=_("Enable"))
        self.noise_expander.set_subtitle(_("Filter background noise from audio"))
        self.noise_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.noise_switch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Enable noise reduction")],
        )
        self.noise_switch.connect("state-set", self._on_noise_switch_changed)
        self.noise_expander.add_suffix(self.noise_switch)
        self.noise_expander.set_enable_expansion(False)
        self.noise_expander.set_expanded(False)
        noise_group.add(self.noise_expander)

        self.noise_strength_row = Adw.ActionRow(title=_("Strength"))
        self.noise_strength_adj = Gtk.Adjustment(value=1.0, lower=0.0, upper=1.0, step_increment=0.05, page_increment=0.1)
        self.noise_strength_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.noise_strength_adj)
        self.noise_strength_scale.set_digits(2)
        self.noise_strength_scale.set_hexpand(True)
        self.noise_strength_scale.set_size_request(200, -1)
        self.noise_strength_scale.set_valign(Gtk.Align.CENTER)
        self.noise_strength_scale.add_mark(0.0, Gtk.PositionType.BOTTOM, "0")
        self.noise_strength_scale.add_mark(0.5, Gtk.PositionType.BOTTOM, "0.5")
        self.noise_strength_scale.add_mark(1.0, Gtk.PositionType.BOTTOM, "1.0")
        self.noise_strength_scale.connect("value-changed", self._on_noise_strength_changed)
        self.noise_strength_row.add_suffix(self.noise_strength_scale)
        self.noise_expander.add_row(self.noise_strength_row)

        # GTCRN Advanced Controls
        self._noise_model_list = [
            _("Maximum Cleaning"),
            _("Natural Voice"),
            _("Smart (both combined)"),
        ]
        noise_model_model = Gtk.StringList.new(self._noise_model_list)
        self.noise_model_row = Adw.ComboRow(title=_("AI Model"), model=noise_model_model)
        self.noise_model_row.set_selected(0)
        self.noise_model_row.connect("notify::selected", self._on_noise_model_changed)
        self.noise_expander.add_row(self.noise_model_row)

        self.noise_speech_strength_row = Adw.ActionRow(title=_("Speech Strength"))
        self.noise_speech_strength_adj = Gtk.Adjustment(value=1.0, lower=0.0, upper=1.0, step_increment=0.05, page_increment=0.1)
        self.noise_speech_strength_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.noise_speech_strength_adj)
        self.noise_speech_strength_scale.set_digits(2)
        self.noise_speech_strength_scale.set_hexpand(True)
        self.noise_speech_strength_scale.set_size_request(200, -1)
        self.noise_speech_strength_scale.set_valign(Gtk.Align.CENTER)
        self.noise_speech_strength_scale.add_mark(0.0, Gtk.PositionType.BOTTOM, "0")
        self.noise_speech_strength_scale.add_mark(0.5, Gtk.PositionType.BOTTOM, "0.5")
        self.noise_speech_strength_scale.add_mark(1.0, Gtk.PositionType.BOTTOM, "1.0")
        self.noise_speech_strength_scale.connect("value-changed", self._on_noise_advanced_changed)
        self.noise_speech_strength_row.add_suffix(self.noise_speech_strength_scale)
        self.noise_expander.add_row(self.noise_speech_strength_row)

        self.noise_lookahead_row = Adw.ActionRow(title=_("Lookahead (ms)"))
        self.noise_lookahead_adj = Gtk.Adjustment(value=0, lower=0, upper=200, step_increment=5, page_increment=20)
        self.noise_lookahead_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.noise_lookahead_adj)
        self.noise_lookahead_scale.set_digits(0)
        self.noise_lookahead_scale.set_hexpand(True)
        self.noise_lookahead_scale.set_size_request(200, -1)
        self.noise_lookahead_scale.set_valign(Gtk.Align.CENTER)
        self.noise_lookahead_scale.add_mark(0, Gtk.PositionType.BOTTOM, "0")
        self.noise_lookahead_scale.add_mark(50, Gtk.PositionType.BOTTOM, "50")
        self.noise_lookahead_scale.add_mark(100, Gtk.PositionType.BOTTOM, "100")
        self.noise_lookahead_scale.add_mark(200, Gtk.PositionType.BOTTOM, "200")
        self.noise_lookahead_scale.connect("value-changed", self._on_noise_advanced_changed)
        self.noise_lookahead_row.add_suffix(self.noise_lookahead_scale)
        self.noise_expander.add_row(self.noise_lookahead_row)

        self.noise_voice_enhance_row = Adw.ActionRow(title=_("Voice Enhance"))
        self.noise_voice_enhance_adj = Gtk.Adjustment(value=0.0, lower=0.0, upper=1.0, step_increment=0.05, page_increment=0.1)
        self.noise_voice_enhance_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.noise_voice_enhance_adj)
        self.noise_voice_enhance_scale.set_digits(2)
        self.noise_voice_enhance_scale.set_hexpand(True)
        self.noise_voice_enhance_scale.set_size_request(200, -1)
        self.noise_voice_enhance_scale.set_valign(Gtk.Align.CENTER)
        self.noise_voice_enhance_scale.add_mark(0.0, Gtk.PositionType.BOTTOM, "0")
        self.noise_voice_enhance_scale.add_mark(0.5, Gtk.PositionType.BOTTOM, "0.5")
        self.noise_voice_enhance_scale.add_mark(1.0, Gtk.PositionType.BOTTOM, "1.0")
        self.noise_voice_enhance_scale.connect("value-changed", self._on_noise_advanced_changed)
        self.noise_voice_enhance_row.add_suffix(self.noise_voice_enhance_scale)
        self.noise_expander.add_row(self.noise_voice_enhance_row)

        # Noise Gate - simplified with intensity slider
        self.gate_expander = Adw.ExpanderRow(title=_("Noise Gate"))
        self.gate_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.gate_switch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Enable noise gate")],
        )
        self.gate_switch.connect("state-set", self._on_gate_switch_changed)
        self.gate_expander.add_suffix(self.gate_switch)
        self.gate_expander.set_enable_expansion(False)
        self.gate_expander.set_expanded(False)

        self.gate_intensity_row = Adw.ActionRow(title=_("Intensity"))
        self.gate_intensity_adj = Gtk.Adjustment(value=0.5, lower=0.0, upper=1.0, step_increment=0.05, page_increment=0.1)
        self.gate_intensity_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.gate_intensity_adj)
        self.gate_intensity_scale.set_digits(2)
        self.gate_intensity_scale.set_hexpand(True)
        self.gate_intensity_scale.set_size_request(200, -1)
        self.gate_intensity_scale.set_valign(Gtk.Align.CENTER)
        self.gate_intensity_scale.add_mark(0.0, Gtk.PositionType.BOTTOM, "0")
        self.gate_intensity_scale.add_mark(0.5, Gtk.PositionType.BOTTOM, "0.5")
        self.gate_intensity_scale.add_mark(1.0, Gtk.PositionType.BOTTOM, "1.0")
        self.gate_intensity_scale.connect("value-changed", self._on_gate_intensity_changed)
        self.gate_intensity_scale.set_sensitive(False)
        self.gate_intensity_row.add_suffix(self.gate_intensity_scale)
        self.gate_expander.add_row(self.gate_intensity_row)

        self.noise_expander.add_row(self.gate_expander)

        # Compressor
        self.compressor_expander = Adw.ExpanderRow(title=_("Compressor"))
        self.compressor_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.compressor_switch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Enable compressor")],
        )
        self.compressor_switch.connect("state-set", self._on_compressor_switch_changed)
        self.compressor_expander.add_suffix(self.compressor_switch)
        self.compressor_expander.set_enable_expansion(False)
        self.compressor_expander.set_expanded(False)

        self.compressor_intensity_row = Adw.ActionRow(title=_("Intensity"))
        self.compressor_intensity_adj = Gtk.Adjustment(value=1.0, lower=0.0, upper=1.0, step_increment=0.05, page_increment=0.1)
        self.compressor_intensity_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.compressor_intensity_adj)
        self.compressor_intensity_scale.set_digits(2)
        self.compressor_intensity_scale.set_hexpand(True)
        self.compressor_intensity_scale.set_size_request(200, -1)
        self.compressor_intensity_scale.set_valign(Gtk.Align.CENTER)
        self.compressor_intensity_scale.add_mark(0.0, Gtk.PositionType.BOTTOM, "0")
        self.compressor_intensity_scale.add_mark(0.5, Gtk.PositionType.BOTTOM, "0.5")
        self.compressor_intensity_scale.add_mark(1.0, Gtk.PositionType.BOTTOM, "1.0")
        self.compressor_intensity_scale.connect("value-changed", self._on_compressor_intensity_changed)
        self.compressor_intensity_scale.set_sensitive(False)
        self.compressor_intensity_row.add_suffix(self.compressor_intensity_scale)
        self.compressor_expander.add_row(self.compressor_intensity_row)

        self.noise_expander.add_row(self.compressor_expander)

        # High-pass filter
        self.hpf_row = Adw.SwitchRow(title=_("High-Pass Filter"))
        self.hpf_row.set_subtitle(_("Removes low-frequency rumble"))
        self.hpf_row.set_active(False)
        self.hpf_row.connect("notify::active", self._on_hpf_switch_changed)
        self.noise_expander.add_row(self.hpf_row)

        self.hpf_freq_row = Adw.ActionRow(title=_("Frequency (Hz)"))
        self.hpf_freq_adj = Gtk.Adjustment(value=80, lower=20, upper=500, step_increment=5, page_increment=20)
        self.hpf_freq_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.hpf_freq_adj)
        self.hpf_freq_scale.set_digits(0)
        self.hpf_freq_scale.set_hexpand(True)
        self.hpf_freq_scale.set_size_request(200, -1)
        self.hpf_freq_scale.set_valign(Gtk.Align.CENTER)
        self.hpf_freq_scale.add_mark(20, Gtk.PositionType.BOTTOM, "20")
        self.hpf_freq_scale.add_mark(80, Gtk.PositionType.BOTTOM, "80")
        self.hpf_freq_scale.add_mark(200, Gtk.PositionType.BOTTOM, "200")
        self.hpf_freq_scale.add_mark(500, Gtk.PositionType.BOTTOM, "500")
        self.hpf_freq_scale.connect("value-changed", self._on_hpf_freq_changed)
        self.hpf_freq_row.add_suffix(self.hpf_freq_scale)
        self.hpf_freq_row.set_visible(False)
        self.noise_expander.add_row(self.hpf_freq_row)

        # Transient suppressor
        self.transient_row = Adw.SwitchRow(title=_("Transient Suppressor"))
        self.transient_row.set_subtitle(_("Suppresses clicks and plosives"))
        self.transient_row.set_active(False)
        self.transient_row.connect("notify::active", self._on_transient_switch_changed)
        self.noise_expander.add_row(self.transient_row)

        self.transient_attack_row = Adw.ActionRow(title=_("Attack"))
        self.transient_attack_adj = Gtk.Adjustment(value=-0.5, lower=-1.0, upper=0.0, step_increment=0.1, page_increment=0.2)
        self.transient_attack_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.transient_attack_adj)
        self.transient_attack_scale.set_digits(1)
        self.transient_attack_scale.set_hexpand(True)
        self.transient_attack_scale.set_size_request(200, -1)
        self.transient_attack_scale.set_valign(Gtk.Align.CENTER)
        self.transient_attack_scale.add_mark(-1.0, Gtk.PositionType.BOTTOM, "-1.0")
        self.transient_attack_scale.add_mark(-0.5, Gtk.PositionType.BOTTOM, "-0.5")
        self.transient_attack_scale.add_mark(0.0, Gtk.PositionType.BOTTOM, "0")
        self.transient_attack_scale.connect("value-changed", self._on_transient_attack_changed)
        self.transient_attack_row.add_suffix(self.transient_attack_scale)
        self.transient_attack_row.set_visible(False)
        self.noise_expander.add_row(self.transient_attack_row)

        parent_box.append(noise_group)

        # --- Normalization group ---
        normalize_group = Adw.PreferencesGroup(title=_("Output Processing"))
        normalize_group.set_margin_start(12)
        normalize_group.set_margin_end(12)
        normalize_group.set_margin_top(6)

        self.normalize_row = Adw.SwitchRow(title=_("Loudness Normalization"))
        self.normalize_row.set_subtitle(_("EBU R128 standard (-16 LUFS)"))
        self.normalize_row.set_active(False)
        self.normalize_row.connect("notify::active", self._on_normalize_switch_changed)
        normalize_group.add(self.normalize_row)

        parent_box.append(normalize_group)

        # Backward-compatible alias for player references
        self.noise_row = self.noise_expander

        # Restore saved settings after UI is created
        self._restore_conversion_settings()

    # --- Settings change handlers ---

    def _on_format_changed(self, row, pspec):
        """Handle format selection change and save setting."""
        if hasattr(self.app, "config") and self.app.config:
            selected_format = self._format_list[row.get_selected()]
            if selected_format:
                self.app.config.set("conversion_format", selected_format)

                # Handle copy mode special case
                if selected_format == "copy":
                    self._set_copy_mode_ui(True)
                else:
                    self._set_copy_mode_ui(False)

                # Bitrate only applies to lossy formats
                lossy_formats = ("mp3", "ogg", "aac", "opus")
                self.bitrate_row.set_visible(selected_format in lossy_formats)

                # Channels not available in copy mode
                if hasattr(self, "channels_row"):
                    self.channels_row.set_visible(selected_format != "copy")

    def _on_bitrate_changed(self, row, pspec):
        """Handle bitrate selection change and save setting."""
        if hasattr(self.app, "config") and self.app.config:
            selected_bitrate = self._bitrate_list[row.get_selected()]
            if selected_bitrate:
                self.app.config.set("conversion_bitrate", selected_bitrate)

    def _on_channels_changed(self, row, pspec):
        """Handle channels selection change and save setting."""
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("audio_channels", str(row.get_selected()))

    def _on_volume_spin_changed(self, spin):
        """Handle volume spin change and save setting."""
        volume = spin.get_value()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("conversion_volume", str(volume))

        # Also update player volume (original functionality)
        player_volume = volume / 100.0
        if player_volume > 1.0:
            player_volume = (
                1.0 + (player_volume - 1.0) * 0.5
            )  # Scale values above 100% appropriately
        self.player.set_volume(player_volume)

    def _on_speed_spin_changed(self, spin):
        """Handle playback speed spin change and save setting."""
        speed = spin.get_value()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("conversion_speed", str(speed))

        # Also update player speed (original functionality)
        self.player.set_playback_speed(speed)
        self.player.set_pitch_correction(True)

    def _on_noise_switch_changed(self, switch, state):
        """Handle noise reduction toggle and save setting."""
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("conversion_noise_reduction", str(state).lower())

        self.noise_expander.set_enable_expansion(state)
        # Prevent auto-expansion from click propagation on the ExpanderRow
        GLib.idle_add(self.noise_expander.set_expanded, False)
        if not state:
            self.gate_switch.set_active(False)
            self.compressor_switch.set_active(False)

        if hasattr(self.player, "set_noise_reduction"):
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

    def _on_normalize_switch_changed(self, row, pspec):
        """Handle loudness normalization toggle."""
        state = row.get_active()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("normalize_enabled", str(state).lower())

    def _on_waveform_switch_changed(self, row, pspec):
        """Handle waveform generation toggle and save setting."""
        state = row.get_active()
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("generate_waveforms", str(state).lower())

        # If enabling waveforms and there's an active file without waveform data, generate it
        if state and self.active_audio_id:
            # Check if visualizer has no waveform data
            if self.visualizer.waveform_data is None:
                logger.info(
                    f"Waveforms enabled, generating for active file: {self.active_audio_id}"
                )

                threading.Thread(
                    target=waveform.generate,
                    args=(
                        self.active_audio_id,
                        self.converter,
                        self.visualizer,
                        self.file_markers,
                        self.zoom_control_box
                        if hasattr(self, "zoom_control_box")
                        else None,
                        self.file_queue.track_metadata,
                    ),
                    daemon=True,
                ).start()

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
        if enabled and hasattr(self, "active_audio_id") and self.active_audio_id:
            if hasattr(self, "visualizer") and self.visualizer.waveform_data is None:
                threading.Thread(
                    target=waveform.generate,
                    args=(
                        self.active_audio_id,
                        self.converter,
                        self.visualizer,
                        self.file_markers,
                        self.zoom_control_box
                        if hasattr(self, "zoom_control_box")
                        else None,
                        self.file_queue.track_metadata
                        if hasattr(self, "file_queue")
                        else None,
                    ),
                    daemon=True,
                ).start()

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
        """Enable or disable UI controls based on copy mode state."""
        # Disable/enable controls that require re-encoding
        self.bitrate_row.set_sensitive(not is_copy_mode)
        self.volume_spin.set_sensitive(not is_copy_mode)
        self.speed_spin.set_sensitive(not is_copy_mode)
        if hasattr(self, "volume_btn"):
            self.volume_btn.set_sensitive(not is_copy_mode)
        if hasattr(self, "speed_btn"):
            self.speed_btn.set_sensitive(not is_copy_mode)
        self.noise_expander.set_sensitive(not is_copy_mode)
        self.normalize_row.set_sensitive(not is_copy_mode)
        if hasattr(self, "eq_toggle_btn"):
            self.eq_toggle_btn.set_sensitive(not is_copy_mode)

        if is_copy_mode:
            # Reset volume to 100 via the scale (triggers full sync)
            if hasattr(self, "volume_scale"):
                self.volume_scale.set_value(self._volume_to_slider(100.0))
            # Reset speed to 1.0 via the scale (triggers full sync)
            if hasattr(self, "speed_scale"):
                self.speed_scale.set_value(self._speed_to_slider(1.0))
            # Close volume/speed popovers if open
            if hasattr(self, "volume_popover") and self.volume_popover.is_visible():
                self.volume_popover.popdown()
            if hasattr(self, "speed_popover") and self.speed_popover.is_visible():
                self.speed_popover.popdown()
            # Hide equalizer if shown
            if hasattr(self, "eq_revealer") and self.eq_revealer.get_reveal_child():
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
        self.noise_switch.set_active(self._config_bool("conversion_noise_reduction"))

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
        self.transient_row.set_active(self._config_bool("transient_enabled"))
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
