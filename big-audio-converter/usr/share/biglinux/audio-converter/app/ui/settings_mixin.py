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
import weakref

import gi

from app.utils.main_loop import weak_callback

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk

from app.audio.profiles import BITRATES, SAMPLE_RATES

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


def _untruncated_list_factory():
    """Popup items shown in full, with a check on the chosen one."""
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, item):
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Label(xalign=0, hexpand=True))
        box.append(
            Gtk.Image(
                icon_name="object-select-symbolic",
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        item.set_child(box)
        item.connect("notify::selected", _show_check)

    def bind(_factory, item):
        item.get_child().get_first_child().set_label(item.get_item().get_string())
        _show_check(item)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


def _show_check(item, *_args):
    item.get_child().get_last_child().set_opacity(1.0 if item.get_selected() else 0.0)


def _short_label_factory(labels):
    """Button text for a dropdown whose list entries carry an explanation."""
    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda _f, item: item.set_child(Gtk.Label(xalign=0)))
    factory.connect(
        "bind",
        lambda _f, item: item.get_child().set_label(labels[item.get_position()]),
    )
    return factory


class ChoiceRow(Adw.ActionRow):
    """A titled row whose value is a dropdown button, as in the compact list.

    It mirrors the ComboRow calls the settings code uses, so a row can be
    read, set and watched through ``selected`` without reaching the button.
    """

    selected = GObject.Property(type=GObject.TYPE_UINT, maximum=GLib.MAXUINT32)

    def __init__(self, title, labels, short_labels=None):
        super().__init__(title=title)
        self.dropdown = Gtk.DropDown(
            model=Gtk.StringList.new(labels), valign=Gtk.Align.CENTER
        )
        self.dropdown.set_list_factory(_untruncated_list_factory())
        if short_labels:
            self.dropdown.set_factory(_short_label_factory(short_labels))
        self.dropdown.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.bind_property(
            "selected",
            self.dropdown,
            "selected",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        self.add_suffix(self.dropdown)
        self.set_activatable_widget(self.dropdown)

    def get_selected(self):
        return self.dropdown.get_selected()

    def set_selected(self, position):
        self.dropdown.set_selected(position)

    def get_selected_item(self):
        return self.dropdown.get_selected_item()

    def set_model(self, model):
        self.dropdown.set_model(model)


class SettingsManagerMixin:
    """Mixin providing all conversion-settings logic for MainWindow."""

    # --- Conversion options UI setup ---

    def setup_conversion_options(self, parent_box):
        """One compact list: common choices first, rare ones in closed rows."""
        owner = weakref.proxy(self)
        self._updating_profile = False
        options = Adw.PreferencesGroup(
            margin_start=12,
            margin_end=12,
            margin_top=12,
            css_classes=["compact-options"],
        )
        parent_box.append(options)

        def numeric_row(parent, title, value, lower, upper, step, callback, digits=2):
            row = Adw.ActionRow(title=title)
            row.set_title_lines(2)
            adjustment = Gtk.Adjustment(
                value=value,
                lower=lower,
                upper=upper,
                step_increment=step,
                page_increment=step * 5,
            )
            control = Gtk.SpinButton(
                adjustment=adjustment,
                digits=digits,
                numeric=True,
                valign=Gtk.Align.CENTER,
                width_chars=5,
            )
            control.update_property([Gtk.AccessibleProperty.LABEL], [title])
            control.connect("value-changed", weak_callback(callback))
            row.add_suffix(control)
            row.set_activatable_widget(control)
            (parent.add_row if isinstance(parent, Adw.ExpanderRow) else parent.add)(row)
            return row, control

        self._format_list = ["copy", "mp3", "ogg", "flac", "wav", "aac", "opus"]
        self.format_row = ChoiceRow(
            _("Format"),
            [
                _("Copy without changing quality"),
                _("MP3 — widely compatible"),
                _("Ogg Vorbis — open format"),
                _("FLAC — lossless, smaller than WAV"),
                _("WAV — uncompressed"),
                _("AAC — good quality in small files"),
                _("Opus — smallest files, great for voice"),
            ],
            [_("Copy"), "MP3", "Ogg", "FLAC", "WAV", "AAC", "Opus"],
        )
        self.format_row.set_selected(1)
        self.format_row.connect(
            "notify::selected", weak_callback(self._on_format_changed)
        )
        options.add(self.format_row)

        self._bitrate_list = list(BITRATES["mp3"])
        self.bitrate_row = ChoiceRow(_("Bitrate"), self._bitrate_list)
        self.bitrate_row.set_selected(self._bitrate_list.index("192k"))
        self.bitrate_row.connect(
            "notify::selected", weak_callback(self._on_bitrate_changed)
        )
        options.add(self.bitrate_row)

        self.volume_spin = Adw.SpinRow.new_with_range(0, 1000, 5)
        self.volume_spin.set_title(_("Volume (%)"))
        self.volume_spin.set_value(100)
        self.volume_spin.connect(
            "notify::value", lambda row, _pspec: owner._on_volume_spin_changed(row)
        )
        options.add(self.volume_spin)
        self.speed_spin = Adw.SpinRow.new_with_range(0.10, 5.0, 0.05)
        self.speed_spin.set_title(_("Speed (×)"))
        self.speed_spin.set_digits(2)
        self.speed_spin.set_value(1.0)
        self.speed_spin.connect(
            "notify::value", lambda row, _pspec: owner._on_speed_spin_changed(row)
        )
        options.add(self.speed_spin)

        # Each switch is followed by the settings it enables, shown only while on.
        self.noise_row = Adw.SwitchRow(title=_("Noise Reduction"))
        self.noise_row.connect(
            "notify::active", weak_callback(self._on_noise_switch_changed)
        )
        options.add(self.noise_row)
        self.noise_engines = ("dfn3", "dpdfnet")
        self.noise_model_row = ChoiceRow(
            _("Noise reduction mode"),
            [
                _("Light — DeepFilterNet3"),
                _("Higher quality — DPDFNet-2 48 kHz"),
            ],
            ["DeepFilterNet3", "DPDFNet-2"],
        )
        self.noise_model_row.connect(
            "notify::selected", weak_callback(self._on_noise_model_changed)
        )
        options.add(self.noise_model_row)
        self.noise_strength_row, self.noise_strength_scale = numeric_row(
            options,
            _("Noise reduction strength (%)"),
            100,
            0,
            100,
            5,
            self._on_noise_strength_changed,
            0,
        )
        self.noise_strength_row.set_subtitle(
            _("Lower values keep more background sound; 0 leaves it unchanged")
        )
        self._update_noise_availability()

        equalizer_row = self.equalizer_row = Adw.ActionRow(title=_("Equalizer"))
        configure = Gtk.Button(label=_("Configure…"), valign=Gtk.Align.CENTER)
        configure.connect("clicked", lambda *_: owner.eq_toggle_btn.set_active(True))
        equalizer_row.add_suffix(configure)
        equalizer_row.set_activatable_widget(configure)
        options.add(equalizer_row)

        self._cut_list = [
            _("Keep the whole audio"),
            _("Cut in timeline order"),
            _("Cut in the order marked"),
        ]
        self.cut_row = ChoiceRow(_("Cut audio"), self._cut_list)
        self.cut_row.connect(
            "notify::selected", weak_callback(self._on_cut_combo_changed)
        )
        options.add(self.cut_row)
        self.cut_output_row = ChoiceRow(
            _("Save segments"), [_("Separate Files"), _("Merge into One")]
        )
        self.cut_output_row.set_visible(False)
        self.cut_output_row.connect(
            "notify::selected", weak_callback(self._on_cut_output_changed)
        )
        options.add(self.cut_output_row)
        self.waveform_row = Adw.SwitchRow(
            title=_("Show waveform"), active=True, visible=False
        )
        self.waveform_row.connect(
            "notify::active", weak_callback(self._on_waveform_switch_changed)
        )
        options.add(self.waveform_row)

        self.destination_row = Adw.ActionRow(
            title=_("Save to"), subtitle=_("Same folder as each source")
        )
        choose = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        choose.set_tooltip_text(_("Choose output folder"))
        choose.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Choose output folder")]
        )
        choose.connect("clicked", weak_callback(self._choose_output_folder))
        reset = self.output_reset_button = Gtk.Button(
            icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER, visible=False
        )
        reset.set_tooltip_text(_("Use source folders"))
        reset.update_property([Gtk.AccessibleProperty.LABEL], [_("Use source folders")])
        reset.connect("clicked", lambda *_: owner._set_output_folder(""))
        self.destination_row.add_suffix(choose)
        self.destination_row.add_suffix(reset)
        self._set_output_folder(
            str(self.app.config.get("output_directory", "") or ""), save=False
        )
        options.add(self.destination_row)

        # Most conversions change nothing below; each closed row names what is on.
        effects = self.effects_expander = Adw.ExpanderRow(title=_("Adjust sound"))
        options.add(effects)
        for prefix, title, subtitle, initial, handler, intensity_handler in (
            (
                "gate",
                _("Noise Gate"),
                _("Silences the background between words"),
                0.5,
                self._on_gate_switch_changed,
                self._on_gate_intensity_changed,
            ),
            (
                "compressor",
                _("Compressor"),
                _("Evens out loud and quiet parts"),
                1,
                self._on_compressor_switch_changed,
                self._on_compressor_intensity_changed,
            ),
        ):
            switch_row = Adw.SwitchRow(title=title, subtitle=subtitle)
            switch_row.connect("notify::active", weak_callback(handler))
            effects.add_row(switch_row)
            row, spin = numeric_row(
                effects, _("Intensity"), initial, 0, 1, 0.05, intensity_handler
            )
            row.set_visible(False)
            setattr(self, prefix + "_row", switch_row)
            setattr(self, prefix + "_intensity_row", row)
            setattr(self, prefix + "_intensity_scale", spin)

        self.hpf_row = Adw.SwitchRow(
            title=_("High-Pass Filter"), subtitle=_("Removes low-frequency rumble")
        )
        self.hpf_row.connect(
            "notify::active", weak_callback(self._on_hpf_switch_changed)
        )
        effects.add_row(self.hpf_row)
        self.hpf_freq_row, self.hpf_freq_scale = numeric_row(
            effects,
            _("Frequency (Hz)"),
            80,
            20,
            500,
            5,
            self._on_hpf_freq_changed,
            0,
        )
        self.hpf_freq_row.set_visible(False)
        self.normalize_row = Adw.SwitchRow(
            title=_("Loudness Normalization"),
            subtitle=_("Target: −16 LUFS; changes the original volume"),
        )
        self.normalize_row.connect(
            "notify::active", weak_callback(self._on_normalize_switch_changed)
        )
        effects.add_row(self.normalize_row)
        self.clipping_row = Adw.SwitchRow(
            title=_("Clipping protection"),
            subtitle=_("Limit peaks explicitly; may change dynamics"),
        )
        self.clipping_row.connect(
            "notify::active", weak_callback(self._on_clipping_changed)
        )
        effects.add_row(self.clipping_row)

        self.advanced_row = Adw.ExpanderRow(
            title=_("Advanced encoding"),
            subtitle=_("Keep original properties when supported"),
        )
        self._advanced_subtitle = self.advanced_row.get_subtitle()
        self.channels_row = ChoiceRow(
            _("Channels"), [_("Original"), _("Mono"), _("Stereo")]
        )
        self.channels_row.connect(
            "notify::selected", weak_callback(self._on_channels_changed)
        )
        self.advanced_row.add_row(self.channels_row)
        self._sample_rate_list = ["original"] + [
            str(rate) for rate in SAMPLE_RATES["mp3"]
        ]
        self.sample_rate_row = ChoiceRow(
            _("Sample rate"),
            [_("Original")] + [f"{rate} Hz" for rate in SAMPLE_RATES["mp3"]],
        )
        self.sample_rate_row.connect(
            "notify::selected", weak_callback(self._on_sample_rate_changed)
        )
        self.advanced_row.add_row(self.sample_rate_row)
        self.precision_row = Adw.SwitchRow(
            title=_("Allow conversion to 24-bit PCM"),
            subtitle=_(
                "For floating-point audio exported to FLAC. Reduces precision; WAV preserves the original representation."
            ),
            active=self.app.config.get("allow_precision_reduction", False) is True,
        )
        self.precision_row.connect(
            "notify::active",
            lambda row, _pspec: owner.app.config.set(
                "allow_precision_reduction", row.get_active()
            ),
        )
        self.advanced_row.add_row(self.precision_row)
        options.add(self.advanced_row)

        # A group places plain widgets after its rows: notices and the cut
        # tools sit under the list.
        self.copy_notice = Gtk.Label(
            label=_(
                "Fast Copy keeps encoded audio. Cuts follow packet boundaries and may not match the exact times. Effects are bypassed in preview and export."
            ),
            wrap=True,
            xalign=0,
            visible=False,
            margin_top=12,
            css_classes=["dim-label", "caption"],
        )
        options.add(self.copy_notice)
        self.gain_notice = Gtk.Label(
            label=_(
                "Boosting volume or equalizer bands can distort audio. Reduce gain or enable clipping protection."
            ),
            wrap=True,
            xalign=0,
            margin_top=12,
            css_classes=["dim-label", "caption"],
        )
        options.add(self.gain_notice)
        self._restore_conversion_settings()
        self.clipping_row.set_active(self._config_bool("prevent_clipping"))
        self._update_effects_summary()
        self._on_format_changed(self.format_row, None)
        self._update_gain_notice()

    def _choose_output_folder(self, _button):
        dialog = Gtk.FileDialog(title=_("Choose output folder"))
        dialog.select_folder(
            self, self._dialog_cancellable, self._output_folder_selected
        )

    def _output_folder_selected(self, dialog, result):
        try:
            selected = dialog.select_folder_finish(result)
            if selected and not self._closed:
                path = selected.get_path()
                if path:
                    self._set_output_folder(path)
                else:
                    self._show_message(
                        _("Local folder required"),
                        _("Choose a folder on the local filesystem."),
                    )
        except GLib.Error as exc:
            if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                logger.debug("Could not select output folder: %s", exc)

    def _set_output_folder(self, path, save=True):
        self.output_directory = path
        if save:
            self.app.config.set("output_directory", path)
        home = GLib.get_home_dir()
        shown = "~" + path[len(home) :] if path.startswith(home + "/") else path
        self.destination_row.set_subtitle(
            GLib.markup_escape_text(shown) or _("Same folder as each source")
        )
        self.output_reset_button.set_visible(bool(path))

    def _on_sample_rate_changed(self, row, _pspec):
        if not self._updating_profile and row.get_selected() < len(
            self._sample_rate_list
        ):
            self.app.config.set(
                "sample_rate", self._sample_rate_list[row.get_selected()]
            )
            self._update_advanced_summary()

    def _on_clipping_changed(self, row, _pspec):
        self.app.config.set("prevent_clipping", str(row.get_active()).lower())
        self.player.set_prevent_clipping(row.get_active())
        self._update_effects_summary()

    # --- Settings change handlers ---

    def _on_format_changed(self, row, _pspec):
        if self._updating_profile:
            return
        format_name = self._format_list[row.get_selected()]
        self.app.config.set("conversion_format", format_name)
        self._updating_profile = True
        saved_bitrate = self.app.config.get("conversion_bitrate", "192k")
        self._bitrate_list = list(BITRATES.get(format_name, ("192k",)))
        self.bitrate_row.set_model(Gtk.StringList.new(self._bitrate_list))
        self.bitrate_row.set_selected(
            self._bitrate_list.index(saved_bitrate)
            if saved_bitrate in self._bitrate_list
            else self._bitrate_list.index("192k")
        )
        self.bitrate_row.set_visible(format_name in BITRATES)
        saved_rate = str(self.app.config.get("sample_rate", "original"))
        rates = SAMPLE_RATES.get(
            format_name,
            (8000, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000, 192000),
        )
        self._sample_rate_list = ["original"] + [str(rate) for rate in rates]
        self.sample_rate_row.set_model(
            Gtk.StringList.new([_("Original")] + [f"{rate} Hz" for rate in rates])
        )
        selected_rate = (
            self._sample_rate_list.index(saved_rate)
            if saved_rate in self._sample_rate_list
            else 0
        )
        self.sample_rate_row.set_selected(selected_rate)
        self._updating_profile = False
        self._update_advanced_summary()
        self._set_copy_mode_ui(format_name == "copy")

    def _on_bitrate_changed(self, row, _pspec):
        if not self._updating_profile and row.get_selected() < len(self._bitrate_list):
            self.app.config.set(
                "conversion_bitrate", self._bitrate_list[row.get_selected()]
            )

    def _on_channels_changed(self, row, pspec):
        """Handle channels selection change and save setting."""
        self.app.config.set("audio_channels", str(row.get_selected()))
        self._update_advanced_summary()

    def _update_advanced_summary(self):
        """Name the properties that differ from the source, like the queue rows."""
        changed = [
            row.get_selected_item().get_string()
            for row in (self.channels_row, self.sample_rate_row)
            if row.get_selected() > 0
        ]
        self.advanced_row.set_subtitle(" · ".join(changed) or self._advanced_subtitle)

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
                icon = (
                    "muted"
                    if volume == 0
                    else "low"
                    if volume < 33
                    else "medium"
                    if volume <= 100
                    else "high"
                )
                self.volume_btn.set_icon_name(f"audio-volume-{icon}-symbolic")
            self.player.set_volume(volume / 100)
            self.app.config.set("conversion_volume", str(volume))
            self._update_gain_notice()
        finally:
            self._syncing_volume = False

    def _update_gain_notice(self):
        # The equalizer panel is created after the settings are restored.
        boosted_eq = hasattr(self, "eq_panel") and any(
            scale.get_value() > 0 for scale in self.eq_panel.band_scales.values()
        )
        self.gain_notice.set_visible(
            self.volume_spin.get_value() > 100.001 or boosted_eq
        )

    def _on_speed_spin_changed(self, spin):
        self._set_processing_speed(spin.get_value())

    def _set_processing_speed(self, speed):
        if getattr(self, "_syncing_speed", False):
            return
        self._syncing_speed = True
        try:
            speed = max(0.1, min(5.0, speed))
            self.speed_spin.set_value(speed)
            if hasattr(self, "speed_scale"):
                self.speed_scale.set_value(self._speed_to_slider(speed))
                self.speed_value_label.set_text(f"{speed:.2f}×")
            self.player.set_playback_speed(speed)
            self.player.set_pitch_correction(True)
            self.app.config.set("conversion_speed", str(speed))
        finally:
            self._syncing_speed = False

    def _update_noise_availability(self):
        engine = self.noise_engines[self.noise_model_row.get_selected()]
        self.noise_available = engine in self.player.noise_plugins
        self.noise_row.set_sensitive(
            self.noise_available
            and self._format_list[self.format_row.get_selected()] != "copy"
        )
        if not self.noise_available:
            self.noise_row.set_active(False)
            package = "deepfilternet3-native" if engine == "dfn3" else "dpdfnet-native"
            self.noise_row.set_subtitle(
                _("Unavailable: install {package}").format(package=package)
            )
        else:
            self.noise_row.set_subtitle("")
        self._show_noise_rows()

    def _show_noise_rows(self):
        active = self.noise_row.get_active()
        # A missing engine keeps the mode reachable so another can be chosen.
        self.noise_model_row.set_visible(active or not self.noise_available)
        self.noise_strength_row.set_visible(active)

    def _on_noise_switch_changed(self, row, _pspec):
        state = row.get_active()
        if state and not (
            self.noise_available and self.player.set_noise_reduction(True)
        ):
            row.set_active(False)
            return
        if not state:
            self.player.set_noise_reduction(False)
        self.app.config.set("noise_reduction_enabled", str(state).lower())
        self._show_noise_rows()

    def _update_effects_summary(self):
        """Name the active effects so a closed list still shows its state."""
        active = [
            row.get_title()
            for row in (
                self.gate_row,
                self.compressor_row,
                self.hpf_row,
                self.normalize_row,
                self.clipping_row,
            )
            if row.get_active()
        ]
        self.effects_expander.set_subtitle(
            ", ".join(active) or _("Filters and loudness")
        )

    def _on_noise_strength_changed(self, scale):
        percent = scale.get_value()
        self.app.config.set("noise_strength", str(percent))
        self.player.set_noise_strength(percent)

    def _on_noise_model_changed(self, row, pspec):
        engine = self.noise_engines[row.get_selected()]
        if engine not in self.player.noise_plugins:
            self.noise_row.set_active(False)
        self.player.set_noise_engine(engine)
        self.app.config.set("noise_engine", engine)
        self._update_noise_availability()

    def _on_gate_switch_changed(self, row, _pspec):
        """Handle noise gate toggle."""
        state = row.get_active()
        self.app.config.set("gate_enabled", str(state).lower())
        self.gate_intensity_row.set_visible(state)
        self.player.set_gate_enabled(state)
        self._update_effects_summary()

    def _on_gate_intensity_changed(self, scale):
        """Handle gate intensity slider change."""
        intensity = scale.get_value()
        self.app.config.set("gate_intensity", str(intensity))

        self.player.set_gate_intensity(intensity)

    def _on_compressor_switch_changed(self, row, _pspec):
        """Handle compressor toggle."""
        state = row.get_active()
        self.app.config.set("compressor_enabled", str(state).lower())
        self.compressor_intensity_row.set_visible(state)
        self.player.set_compressor_enabled(state)
        self._update_effects_summary()

    def _on_compressor_intensity_changed(self, scale):
        """Handle compressor intensity change."""
        intensity = scale.get_value()
        self.app.config.set("compressor_intensity", str(intensity))

        self.player.set_compressor_intensity(intensity)

    def _on_hpf_switch_changed(self, row, pspec):
        """Handle high-pass filter toggle."""
        state = row.get_active()
        self.app.config.set("hpf_enabled", str(state).lower())

        self.hpf_freq_row.set_visible(state)

        self.player.set_hpf_enabled(state)
        self._update_effects_summary()

    def _on_hpf_freq_changed(self, scale):
        """Handle HPF frequency change."""
        freq = int(scale.get_value())
        self.app.config.set("hpf_frequency", str(freq))

        self.player.set_hpf_frequency(freq)

    def _on_normalize_switch_changed(self, row, _pspec):
        state = row.get_active()
        self.app.config.set("normalize_enabled", str(state).lower())
        self.player.set_normalize(state)
        self._update_effects_summary()

    def _on_waveform_switch_changed(self, row, pspec):
        enabled = row.get_active()
        self.app.config.set("generate_waveforms", str(enabled).lower())
        if self.active_audio_id:
            self._request_waveform(
                self.active_audio_id,
                enabled=enabled and self.cut_row.get_selected() > 0,
            )

    def _on_cut_combo_changed(self, row, pspec):
        """Handle cut audio combo box changes."""
        active = row.get_selected()
        # Enable markers and show options when any option except "Off" is selected
        enabled = active > 0

        # Show/hide segment output option
        self.cut_output_row.set_visible(enabled)
        self.waveform_row.set_visible(enabled)

        # Settings are restored before setup_ui builds the waveform and bottom
        # bar (the seekbar last); setup_ui then applies the same visibility.
        if hasattr(self, "seekbar"):
            self.visualizer.set_markers_enabled(enabled)
            self.play_selection_switch.set_visible(enabled)
            self.segment_edit_button.set_visible(enabled)
            self.zoom_box.set_visible(enabled)
            self._set_waveform_visible(enabled and bool(self.file_queue.files))

        # Generate waveform if enabling cut and active file has no waveform data
        if enabled and self.active_audio_id and self.visualizer.waveform_data is None:
            self._request_waveform(self.active_audio_id, enabled=None)

        if not enabled and self.active_audio_id:
            self._request_waveform(self.active_audio_id, enabled=False)

        # Save setting
        self.app.config.set("cut_audio_enabled", str(enabled).lower())
        self.app.config.set("cut_audio_mode", str(active))

    def _on_cut_output_changed(self, row, pspec):
        """Handle cut output mode change (separate files vs merge)."""
        self.app.config.set("cut_output_mode", str(row.get_selected()))

    # --- Equalizer toggle ---

    def _on_eq_toggle_clicked(self, button):
        """Toggle the inline equalizer panel visibility (from bottom bar)."""
        self.eq_revealer.set_reveal_child(button.get_active())

    def _on_eq_revealer_changed(self, revealer, pspec):
        """Sync equalizer toggle button with revealer state."""
        is_revealed = revealer.get_reveal_child()
        if self.eq_toggle_btn.get_active() != is_revealed:
            self.eq_toggle_btn.handler_block(self._eq_toggle_btn_handler)
            self.eq_toggle_btn.set_active(is_revealed)
            self.eq_toggle_btn.handler_unblock(self._eq_toggle_btn_handler)

    # --- Copy mode UI ---

    def _set_copy_mode_ui(self, is_copy_mode):
        """Bypass effects without discarding the user's saved processing choices."""
        self.copy_notice.set_visible(is_copy_mode)
        self.precision_row.set_visible(
            self._format_list[self.format_row.get_selected()] == "flac"
        )
        self.advanced_row.set_visible(not is_copy_mode)
        # Copy keeps the encoded audio, so nothing that processes it applies.
        for row in (
            self.volume_spin,
            self.speed_spin,
            self.noise_model_row,
            self.noise_strength_row,
            self.equalizer_row,
            self.effects_expander,
        ):
            row.set_sensitive(not is_copy_mode)
        self._update_noise_availability()
        # Settings are restored before setup_ui builds the bottom bar (the
        # seekbar last); setup_ui calls this again once the bar exists.
        bar_ready = hasattr(self, "seekbar")
        self.player.set_effects_bypassed(
            is_copy_mode or (bar_ready and self.original_preview.get_active())
        )
        if not bar_ready:
            return
        for widget in (
            self.original_preview,
            self.volume_btn,
            self.speed_btn,
            self.eq_toggle_btn,
        ):
            widget.set_sensitive(not is_copy_mode)
        if is_copy_mode:
            self.volume_popover.popdown()
            self.speed_popover.popdown()
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

    def reset_settings(self):
        """Return every option to its first-run value through its own control.

        Each control's handler saves the value and updates the player, so the
        screen, the preview and the settings file agree afterwards.
        """
        self.format_row.set_selected(self._format_list.index("mp3"))
        self.bitrate_row.set_selected(self._bitrate_list.index("192k"))
        self.channels_row.set_selected(0)
        self.sample_rate_row.set_selected(0)
        self.precision_row.set_active(False)
        self._set_output_folder("")
        self.cut_row.set_selected(0)
        self.cut_output_row.set_selected(0)
        self.waveform_row.set_active(True)
        self._set_processing_volume(100)
        self._set_processing_speed(1.0)
        self.noise_row.set_active(False)
        self.noise_model_row.set_selected(0)
        self.noise_strength_scale.set_value(100)
        self.gate_row.set_active(False)
        self.gate_intensity_scale.set_value(0.5)
        self.compressor_row.set_active(False)
        self.compressor_intensity_scale.set_value(1.0)
        self.hpf_row.set_active(False)
        self.hpf_freq_scale.set_value(80)
        self.normalize_row.set_active(False)
        self.clipping_row.set_active(False)
        self.eq_panel.preset_dropdown.set_selected(
            self.eq_panel.PRESET_KEYS.index("flat")
        )
        self.auto_advance_switch.set_active(True)
        self.original_preview.set_active(False)

    def _restore_conversion_settings(self):
        """Restore saved conversion settings from config.

        Runs inside setup_conversion_options, before the bottom bar exists;
        setup_conversion_options then applies the format-dependent rows.
        """
        self.format_row.set_selected(
            self._config_list_index("conversion_format", self._format_list, 1)
        )
        self.channels_row.set_selected(
            self._config_int("audio_channels", 0, lo=0, hi=2)
        )
        self.volume_spin.set_value(self._config_float("conversion_volume", 100))
        self.speed_spin.set_value(self._config_float("conversion_speed", 1.0))

        # Legacy GTCRN settings stay on disk but never enable a different model.
        engine = self.app.config.get("noise_engine", "dfn3")
        self.noise_model_row.set_selected(self.noise_engines.index(engine))
        self._on_noise_model_changed(self.noise_model_row, None)
        self.noise_strength_scale.set_value(self._config_float("noise_strength", 100))
        self.noise_row.set_active(
            self.noise_available and self._config_bool("noise_reduction_enabled")
        )

        # Restore noise gate settings (intensity slider)
        self.gate_row.set_active(self._config_bool("gate_enabled"))
        self.gate_intensity_scale.set_value(self._config_float("gate_intensity", 0.5))

        # Restore compressor
        self.compressor_row.set_active(self._config_bool("compressor_enabled"))
        self.compressor_intensity_scale.set_value(
            self._config_float("compressor_intensity", 1.0)
        )

        # Restore HPF
        self.hpf_row.set_active(self._config_bool("hpf_enabled"))
        self.hpf_freq_scale.set_value(self._config_float("hpf_frequency", 80))

        # Restore normalization
        self.normalize_row.set_active(self._config_bool("normalize_enabled"))

        # Cut mode; its change handler shows the dependent rows.
        mode = self._config_int("cut_audio_mode", -1, lo=0, hi=2)
        if mode < 0:
            # Legacy key fallback
            mode = 1 if self._config_bool("cut_audio_enabled") else 0
        self.cut_row.set_selected(mode)
        self.cut_output_row.set_selected(
            self._config_int("cut_output_mode", 0, lo=0, hi=1)
        )
