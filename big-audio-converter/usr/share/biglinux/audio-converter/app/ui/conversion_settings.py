"""The window's conversion settings: rows, their saved values and effects."""

import gettext
import locale
import logging
import math
import weakref

import gi

from app.main_loop import weak_callback

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from app.audio.pauses import DEFAULT_VOICE_SENSITIVITY, PAUSE_LENGTHS
from app.audio.profiles import BITRATES, SAMPLE_RATES
from app.ui.effect_cards import (
    ChoiceRow,
    SettingRow,
    StepRow,
    SwitchRow,
)
from app.ui.pause_panel import PausePanel

_ = gettext.gettext

logger = logging.getLogger(__name__)

# Intensity of each named compressor step after Off.
COMPRESSOR_STEPS = (0.0, 0.35, 0.65, 1.0)

# Quality in words by bitrate. Opus reaches each level at a lower bitrate.
QUALITY_WORDS = {
    "opus": {32: "voice", 64: "good", 96: "very good", 128: "very good"},
    "other": {
        32: "voice",
        64: "voice",
        96: "fair",
        128: "good",
        160: "very good",
        192: "very good",
    },
}


def quality_labels(format_name, bitrates):
    """List entries and button texts for the quality choices of a format."""
    words = {
        "voice": _("Voice only"),
        "fair": _("Fair"),
        "good": _("Good"),
        "very good": _("Very good"),
        "best": _("Best"),
    }
    table = QUALITY_WORDS["opus" if format_name == "opus" else "other"]
    entries, buttons = [], []
    for bitrate in bitrates:
        kbps = int(bitrate.rstrip("k"))
        word = words[table.get(kbps, "best")]
        megabytes = locale.format_string("%.1f", kbps * 60 / 8 / 1000)
        # The bitrate names each choice: neighboring ones share a word.
        entries.append(
            f"{kbps} kbps — "
            + _("{quality}, about {size} MB per minute").format(
                quality=word, size=megabytes
            )
        )
        buttons.append(f"{word} · {kbps} kbps")
    return entries, buttons


def _attention(widget, on):
    """A bar setting that changes the sound a lot stays in sight, amber as
    the players show a muted volume: easy to forget once changed."""
    (widget.add_css_class if on else widget.remove_css_class)("attention")


class ConversionSettingsMixin:
    """Mixin providing all conversion-settings logic for MainWindow."""

    def setup_conversion_options(self, parent_box):
        """One compact list: common choices first, rare ones in closed rows."""
        owner = weakref.proxy(self)
        self._updating_profile = False
        cards = self.effect_cards
        # What the files become, as rows like the effects below.
        output_box, output_rows = cards.add_group(None)
        output_box.set_margin_start(12)
        output_box.set_margin_end(12)
        output_box.set_margin_top(12)
        parent_box.append(output_box)
        self.output_box = output_box

        # Only FLAC asks it, so it waits in the format's card.
        self.precision_row = Adw.SwitchRow(
            title=_("Allow conversion to 24-bit PCM"),
            subtitle=_(
                "Only for floating-point audio saved as FLAC. It lowers the precision. WAV keeps the original as it is."
            ),
            active=self.app.config.get("allow_precision_reduction", False) is True,
        )
        self.precision_row.connect(
            "notify::active",
            lambda row, _pspec: owner.app.config.set(
                "allow_precision_reduction", row.get_active()
            ),
        )
        self.precision_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"]
        )
        self.precision_list.append(self.precision_row)

        # Restoring settings runs handlers before the waveform exists.

        # Most requests name the format, so the common ones come first and the
        # packet copy, which ignores every other choice, comes last.
        self._format_list = ["mp3", "m4a", "opus", "flac", "wav", "ogg", "copy"]
        self.format_row = ChoiceRow(
            "audio-x-generic-symbolic",
            _("Format"),
            _("If you are not sure, pick MP3. It plays on any device."),
            [
                _("MP3 — plays everywhere"),
                _("M4A — good quality in small files"),
                _("Opus — smallest files, great for voice"),
                _("FLAC — lossless, smaller than WAV"),
                _("WAV — uncompressed"),
                _("Ogg Vorbis — open format"),
                _("Keep the original format — no conversion, no effects"),
            ],
            ["MP3", "M4A", "Opus", "FLAC", "WAV", "Ogg", _("Original")],
            (self.precision_list,),
            wide=(6,),
        )
        self.format_row.set_selected(0)
        self.format_row.connect(
            "notify::selected", weak_callback(self._on_format_changed)
        )
        cards.add_row(output_rows, self.format_row)

        # The quality chosen is the one for stereo; on by default, other
        # channel counts get their share of it.
        self.bitrate_channels_row = Adw.SwitchRow(
            title=_("Adjust to the number of channels"),
            subtitle=_(
                "The quality above is for stereo. Mono uses half of it, and 5.1 three times as much."
            ),
            active=str(self.app.config.get("bitrate_per_channel", True)).lower()
            != "false",
        )
        self.bitrate_channels_row.connect(
            "notify::active",
            lambda row, _pspec: owner.app.config.set(
                "bitrate_per_channel", row.get_active()
            ),
        )
        bitrate_channels_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"]
        )
        bitrate_channels_list.append(self.bitrate_channels_row)
        self._bitrate_list = list(BITRATES["mp3"])
        self.bitrate_row = ChoiceRow(
            "starred-symbolic",
            _("Quality"),
            _("The higher the bitrate, the better the sound and the bigger the file."),
            *quality_labels("mp3", self._bitrate_list),
            extra=(bitrate_channels_list,),
        )
        self.bitrate_row.set_selected(self._bitrate_list.index("192k"))
        self.bitrate_row.connect(
            "notify::selected", weak_callback(self._on_bitrate_changed)
        )
        cards.add_row(output_rows, self.bitrate_row)

        # The playback bar's sliders move in their own units; the spin buttons
        # take exact amounts.
        self.volume_adjustment = Gtk.Adjustment(
            upper=100, step_increment=0.5, page_increment=10
        )
        self.speed_adjustment = Gtk.Adjustment(
            upper=100, step_increment=0.5, page_increment=10
        )
        self.volume_adjustment.connect(
            "value-changed",
            lambda adjustment: owner._set_processing_volume(
                owner._slider_to_volume(adjustment.get_value())
            ),
        )
        self.speed_adjustment.connect(
            "value-changed",
            lambda adjustment: owner._set_processing_speed(
                owner._slider_to_speed(adjustment.get_value())
            ),
        )
        self.volume_spin = Gtk.SpinButton.new_with_range(0, 1000, 1)
        self.volume_spin.set_value(100)
        self.volume_spin.connect(
            "notify::value", lambda spin, _pspec: owner._on_volume_spin_changed(spin)
        )
        self.speed_spin = Gtk.SpinButton.new_with_range(0.10, 5.0, 0.01)
        self.speed_spin.set_digits(2)
        self.speed_spin.set_value(1.0)
        self.speed_spin.connect(
            "notify::value", lambda spin, _pspec: owner._on_speed_spin_changed(spin)
        )

        # Strength 0 is off.
        self.noise_engines = ("dfn3", "dpdfnet")
        self.noise_status = Gtk.Label(
            wrap=True, xalign=0, css_classes=["caption", "warning"]
        )
        self.noise_adjustment = Gtk.Adjustment(
            upper=100, step_increment=1, page_increment=10
        )
        self.noise_adjustment.connect(
            "value-changed", weak_callback(self._on_noise_strength_changed)
        )
        # DeepFilterNet3 and DPDFNet-2, named by what the choice changes.
        self.noise_model = Adw.ToggleGroup(homogeneous=True, hexpand=True)
        for label in (_("Faster"), _("Better, slower")):
            self.noise_model.add(Adw.Toggle(label=label))
        self.noise_model.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Cleaning quality")]
        )
        self.noise_model.connect(
            "notify::active", weak_callback(self._on_noise_model_changed)
        )
        # The denoiser mutes what its voice detector does not take for a voice:
        # music, noise and the silence between words.
        voice_only_label = Gtk.Label(
            label=_("Audio only when a voice is detected"),
            xalign=0,
            wrap=True,
            max_width_chars=26,
            hexpand=True,
        )
        self.voice_only_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.voice_only_switch.update_property(
            [Gtk.AccessibleProperty.LABEL], [voice_only_label.get_label()]
        )
        voice_only_row = Gtk.Box(spacing=12)
        voice_only_row.append(voice_only_label)
        voice_only_row.append(self.voice_only_switch)
        # The pause finder's five steps, named the same way.
        self.voice_only_names = (
            _("Very sensitive"),
            _("Sensitive"),
            _("Normal"),
            _("Strict"),
            _("Very strict"),
        )
        self.voice_only_scale = Gtk.Scale(
            adjustment=Gtk.Adjustment(
                lower=1, upper=5, step_increment=1, page_increment=1
            ),
            round_digits=0,
            hexpand=True,
        )
        for level in range(1, 6):
            self.voice_only_scale.add_mark(level, Gtk.PositionType.BOTTOM, None)
        self.voice_only_scale.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Voice detection")]
        )
        self.voice_only_level = Gtk.Label(css_classes=["dim-label"])
        heading = Gtk.Box(spacing=12)
        heading.append(Gtk.Label(label=_("Voice detection"), xalign=0, hexpand=True))
        heading.append(self.voice_only_level)
        self.voice_only_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.voice_only_box.append(voice_only_row)
        self.voice_only_detection = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.voice_only_detection.append(heading)
        self.voice_only_detection.append(self.voice_only_scale)
        self.voice_only_box.append(self.voice_only_detection)

        # Files are cut wherever segments are marked on the waveform; these
        # rows stay in place and wait, grayed with the reason, until a part
        # is marked.
        self.cut_mode_row = ChoiceRow(
            "edit-cut-symbolic",
            _("Segments"),
            _(
                "Keep saves only the segments you marked. Remove takes them out and saves the rest."
            ),
            [_("Keep them"), _("Remove them")],
        )
        self.cut_mode_row.connect(
            "notify::selected", weak_callback(self._on_cut_mode_changed)
        )
        self.cut_order_row = ChoiceRow(
            "view-sort-ascending-symbolic",
            _("Order"),
            _(
                "The segments can follow the audio from start to end, or the order in which you marked them."
            ),
            [_("In timeline order"), _("In the order marked")],
        )
        self.cut_order_row.connect(
            "notify::selected", weak_callback(self._on_cut_order_changed)
        )
        self.cut_output_row = ChoiceRow(
            "document-save-symbolic",
            _("Save segments"),
            _("Each segment can become its own file, or they can all go into one."),
            [_("Separate files"), _("Join this file's segments")],
        )
        self.cut_output_row.connect(
            "notify::selected", weak_callback(self._on_cut_output_changed)
        )
        # Joining decides what the files become; it closes the cuts.
        self.join_row = SwitchRow(
            "media-playlist-consecutive-symbolic",
            _("Join into one file"),
            _(
                "Puts all the files in the queue into one, in the order they appear. It is named after the first file."
            ),
        )
        self.join_row.counts = False
        self.join_row.switch.connect(
            "notify::active", weak_callback(self._on_join_changed)
        )

        # Where the files go: below the queue, as in Big Video Converter.
        self.destination_choice = Gtk.DropDown.new_from_strings(
            # Big Video Converter's words, so both say it the same way.
            [_("Save in the same folder as the original file"), _("Folder to save")]
        )
        self.destination_choice.add_css_class("flat")
        self.destination_choice.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Save to")]
        )
        self._destination_handler = self.destination_choice.connect(
            "notify::selected", weak_callback(self._on_destination_choice)
        )
        self.destination_folder = Adw.ButtonContent(
            icon_name="folder-open-symbolic", can_shrink=True
        )
        self.destination_button = Gtk.Button(
            child=self.destination_folder,
            tooltip_text=_("Choose output folder"),
            css_classes=["flat"],
            visible=False,
        )
        self.destination_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Choose output folder")]
        )
        self.destination_button.connect(
            "clicked", weak_callback(self._choose_output_folder)
        )
        # On the right, as in Big Video Converter, in its words.
        delete_label = Gtk.Label(label=_("Delete original files"))
        self.delete_originals_switch = Gtk.Switch(
            active=self._config_bool("delete_originals", False),
            valign=Gtk.Align.CENTER,
        )
        self.delete_originals_switch.update_property(
            [Gtk.AccessibleProperty.LABEL], [delete_label.get_label()]
        )
        self.delete_originals_switch.connect(
            "notify::active", weak_callback(self._on_delete_originals_changed)
        )
        self.delete_box = Gtk.Box(spacing=8, hexpand=True, halign=Gtk.Align.END)
        self.delete_box.append(delete_label)
        self.delete_box.append(self.delete_originals_switch)
        self.destination_bar = Gtk.Box(spacing=6, css_classes=["destination-bar"])
        self.destination_bar.append(self.destination_choice)
        self.destination_bar.append(self.destination_button)
        self.destination_bar.append(self.delete_box)
        self._set_output_folder(
            str(self.app.config.get("output_directory", "") or ""), save=False
        )

        # Each setting is a row saying what it applies; its card holds the
        # controls and says what it does. Under format and quality: the
        # encoding, then the loudness.
        self.channels_row = ChoiceRow(
            "audio-speakers-symbolic",
            _("Channels"),
            _("Keep it on Original. Mono makes the file about half the size."),
            [_("Original"), _("Mono"), _("Stereo")],
            wide=(0,),
        )
        self.channels_row.connect(
            "notify::selected", weak_callback(self._on_channels_changed)
        )
        self._sample_rate_list = ["original"] + [
            str(rate) for rate in SAMPLE_RATES["mp3"]
        ]
        self.sample_rate_row = ChoiceRow(
            "audio-card-symbolic",
            _("Sample rate"),
            _(
                "Leave it on Original unless a device or website asks for a specific rate."
            ),
            [_("Original")] + [f"{rate} Hz" for rate in SAMPLE_RATES["mp3"]],
            columns=3,
            wide=(0,),
        )
        self.sample_rate_row.connect(
            "notify::selected", weak_callback(self._on_sample_rate_changed)
        )
        self.normalize_row = SwitchRow(
            "power-profile-balanced-symbolic",
            _("Standard volume"),
            _(
                "Evens out the volume between files, so when you play several in a row none sounds louder or quieter than the others."
            ),
        )
        self.normalize_row.switch.connect(
            "notify::active", weak_callback(self._on_normalize_switch_changed)
        )
        self.compressor_row = StepRow(
            "multimedia-volume-control-symbolic",
            _("Lift quiet parts"),
            _(
                "Works inside each file, making its quiet parts louder and its loud parts softer. Standard volume evens out whole files instead."
            ),
            [_("Off"), _("Light"), _("Medium"), _("Strong")],
        )
        self.compressor_row.steps.connect(
            "notify::selected", weak_callback(self._on_compressor_changed)
        )
        for row in (
            self.channels_row,
            self.sample_rate_row,
            self.normalize_row,
            self.compressor_row,
        ):
            cards.add_row(output_rows, row)

        # Cuts: finding pauses marks the speech; the rows after it say how the
        # marked parts are saved and wait until there are some. Cuts also
        # apply to the original format, unlike the effects.
        self.pause_panel = PausePanel(self)
        # The app's own glyph: the player's pause icon read as a Pause button.
        self.pause_row = SettingRow(
            "big-audio-converter-remove-pauses-symbolic",
            _("Remove pauses"),
            _(
                "Finds the silences between sentences and leaves them out of the saved file."
            ),
            self.pause_panel,
        )
        self.pause_row.counts = False  # what is kept, not how it sounds
        self.parts_group, parts_rows = cards.add_group(_("Cuts"))
        self.parts_group.set_margin_start(12)
        self.parts_group.set_margin_end(12)
        self.parts_group.set_margin_top(18)
        parent_box.append(self.parts_group)
        for row in (
            self.pause_row,
            self.cut_mode_row,
            self.cut_order_row,
            self.cut_output_row,
            self.join_row,
        ):
            cards.add_row(parts_rows, row)
        self.pause_panel.show_on(self.pause_row)

        self.copy_notice = Gtk.Label(
            label=_(
                "The original format keeps the encoded audio. Cuts follow packet boundaries and may not match the exact times. Effects are bypassed in preview and export."
            ),
            wrap=True,
            xalign=0,
            visible=False,
            css_classes=["dim-label", "caption"],
        )
        self.output_box.append(self.copy_notice)
        self.gain_notice = Gtk.Label(
            label=_(
                "Boosting volume or equalizer bands can distort audio. Reduce the gain or turn on Standard volume."
            ),
            wrap=True,
            xalign=0,
            css_classes=["dim-label", "caption"],
        )
        self.output_box.append(self.gain_notice)

    def _on_destination_choice(self, choice, _pspec):
        if choice.get_selected() == 0:
            self._set_output_folder("")
        else:
            self._choose_output_folder()

    def _choose_output_folder(self, *_args):
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
        if not self._closed:
            # Without a folder chosen, the files still go beside the originals.
            self._set_output_folder(self.output_directory, save=False)

    def _set_output_folder(self, path, save=True):
        self.output_directory = path
        if save:
            self.app.config.set("output_directory", path)
        home = GLib.get_home_dir()
        shown = "~" + path[len(home) :] if path.startswith(home + "/") else path
        self.destination_folder.set_label(shown)
        self.destination_button.set_visible(bool(path))
        self.destination_choice.handler_block(self._destination_handler)
        self.destination_choice.set_selected(int(bool(path)))
        self.destination_choice.handler_unblock(self._destination_handler)

    def _on_sample_rate_changed(self, row, _pspec):
        if not self._updating_profile and row.get_selected() < len(
            self._sample_rate_list
        ):
            self.app.config.set(
                "sample_rate", self._sample_rate_list[row.get_selected()]
            )

    def _on_format_changed(self, row, _pspec):
        if self._updating_profile:
            return
        format_name = self._format_list[row.get_selected()]
        self.app.config.set("conversion_format", format_name)
        self._updating_profile = True
        saved_bitrate = self.app.config.get("conversion_bitrate", "192k")
        self._bitrate_list = list(BITRATES.get(format_name, ("192k",)))
        if format_name in BITRATES:
            entries, buttons = quality_labels(format_name, self._bitrate_list)
        else:
            # Nothing to choose: the format keeps all of the audio.
            entries = buttons = [
                _("Original") if format_name == "copy" else _("Lossless")
            ]
        self.bitrate_row.set_model(Gtk.StringList.new(entries), buttons)
        self.bitrate_row.set_selected(
            self._bitrate_list.index(saved_bitrate)
            if saved_bitrate in self._bitrate_list
            else self._bitrate_list.index("192k")
        )
        self.bitrate_row.set_sensitive(format_name in BITRATES)
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
        self._set_copy_mode_ui(format_name == "copy")

    def _on_bitrate_changed(self, row, _pspec):
        if not self._updating_profile and row.get_selected() < len(self._bitrate_list):
            self.app.config.set(
                "conversion_bitrate", self._bitrate_list[row.get_selected()]
            )

    def _on_channels_changed(self, row, pspec):
        self.app.config.set("audio_channels", str(row.get_selected()))

    def _on_volume_spin_changed(self, spin):
        self._set_processing_volume(spin.get_value())

    def _set_processing_volume(self, volume):
        if self._syncing_volume:
            return
        self._syncing_volume = True
        try:
            volume = max(0.0, min(1000.0, volume))
            self.volume_spin.set_value(volume)
            self.volume_adjustment.set_value(self._volume_to_slider(volume))
            icon = (
                "muted"
                if volume == 0
                else "low"
                if volume < 33
                else "medium"
                if volume <= 100
                else "high"
            )
            self.volume_button.set_icon_name(f"audio-volume-{icon}-symbolic")
            self.volume_value.set_label(f"{volume:.0f}")
            _attention(self.volume_button.get_parent(), not 50 <= volume <= 100)
            self.player.set_volume(volume / 100)
            self.app.config.set("conversion_volume", str(volume))
            self._update_gain_notice()
            self._update_preview_toggle()
        finally:
            self._syncing_volume = False

    def _update_gain_notice(self):
        boosted_eq = any(
            scale.get_value() > 0 for scale in self.eq_panel.band_scales.values()
        )
        self.gain_notice.set_visible(
            self.volume_spin.get_value() > 100.001 or boosted_eq
        )

    def _equalizer_applied(self):
        return any(s.get_value() != 0 for s in self.eq_panel.band_scales.values())

    def _show_equalizer_state(self):
        """A changed curve lights the bar's button, which names the preset to
        screen readers; boosted bands may distort."""
        button = self.equalizer_button
        _attention(button, self._equalizer_applied())
        button.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION],
            [self.eq_panel.preset_dropdown.get_selected_item().get_string()],
        )
        self._update_gain_notice()
        self._update_preview_toggle()

    def _on_speed_spin_changed(self, spin):
        self._set_processing_speed(spin.get_value())

    def _set_processing_speed(self, speed):
        if self._syncing_speed:
            return
        self._syncing_speed = True
        try:
            speed = max(0.1, min(5.0, speed))
            self.speed_spin.set_value(speed)
            self.speed_adjustment.set_value(self._speed_to_slider(speed))
            self.speed_value.set_label(f"{speed:.2f}x")
            _attention(self.speed_button.get_parent(), round(speed, 2) != 1)
            self.player.set_playback_speed(speed)
            self.player.set_pitch_correction(True)
            self.app.config.set("conversion_speed", str(speed))
            self._update_preview_toggle()
        finally:
            self._syncing_speed = False

    def _noise_engine(self):
        return self.noise_engines[self.noise_model.get_active()]

    def _noise_active(self):
        return self.noise_available and self.noise_adjustment.get_value() > 0

    def _update_noise_availability(self):
        engine = self._noise_engine()
        self.noise_available = engine in self.player.noise_plugins
        is_copy = self._format_list[self.format_row.get_selected()] == "copy"
        self.voice_scale.set_sensitive(self.noise_available and not is_copy)
        package = "deepfilternet3-native" if engine == "dfn3" else "dpdfnet-native"
        self.noise_status.set_label(
            _("Unavailable: install {package}").format(package=package)
        )
        # A missing engine keeps the modes reachable so another can be chosen.
        self.noise_status.set_visible(not self.noise_available)
        if not self.noise_available:
            self.player.set_noise_reduction(False)
        self._show_noise_strength()

    def _show_noise_strength(self):
        _attention(self.voice_button.get_parent(), self._noise_active())
        # The model matters once the voice is cleaned, or to leave a missing
        # one; it stays in place so the open card keeps its height.
        self.noise_model.set_sensitive(
            self.noise_adjustment.get_value() > 0 or not self.noise_available
        )
        self.voice_only_box.set_sensitive(self._noise_active())
        self.voice_only_detection.set_sensitive(self.voice_only_switch.get_active())
        level = round(self.voice_only_scale.get_value())
        self.voice_only_level.set_label(self.voice_only_names[level - 1])

    def _on_voice_only_changed(self, *_args):
        active = self.voice_only_switch.get_active()
        level = round(self.voice_only_scale.get_value())
        self.app.config.set("voice_only_enabled", str(active).lower())
        self.app.config.set("voice_only_sensitivity", str(level))
        self.player.set_voice_only(active, level)
        self._show_noise_strength()

    def _on_noise_strength_changed(self, scale):
        percent = scale.get_value()
        self.app.config.set("noise_strength", str(percent))
        self.app.config.set("noise_reduction_enabled", str(percent > 0).lower())
        self.voice_value.set_label(f"{percent:.0f}")
        self._show_noise_strength()
        self._update_preview_toggle()
        if not self.noise_available:
            return
        if percent == 0:
            self.player.set_noise_reduction(False)
            return
        self.player.set_noise_strength(percent)
        if not self.player.noise_reduction and not self.player.set_noise_reduction(
            True
        ):
            scale.set_value(0)

    def _on_noise_model_changed(self, _row, _pspec):
        engine = self._noise_engine()
        if engine not in self.player.noise_plugins:
            self.player.set_noise_reduction(False)
        self.player.set_noise_engine(engine)
        self.app.config.set("noise_engine", engine)
        self._update_noise_availability()
        if self._noise_active():
            self.player.set_noise_reduction(True)

    def _on_compressor_changed(self, steps, _pspec):
        """Off at the first step, otherwise that step's intensity."""
        step = steps.get_selected()
        if step:
            intensity = COMPRESSOR_STEPS[step]
            self.app.config.set("compressor_intensity", str(intensity))
            self.player.set_compressor_intensity(intensity)
        self.app.config.set("compressor_enabled", str(step > 0).lower())
        self.player.set_compressor_enabled(step > 0)

    def _on_normalize_switch_changed(self, row, _pspec):
        state = row.get_active()
        self.app.config.set("normalize_enabled", str(state).lower())
        self.player.set_normalize(state)

    def _on_cut_order_changed(self, row, _pspec):
        # Saved as the former cut mode: 2 was "in the order marked".
        self.app.config.set("cut_audio_mode", "2" if row.get_selected() else "0")

    def _on_cut_mode_changed(self, row, _pspec):
        # Not saved: marks do not outlive the session, and a saved "remove"
        # would turn the next session's marks into cuts.
        remove = self.removes_marked()
        self.visualizer.remove_marked = remove
        self.visualizer.queue_draw()
        self._on_markers_updated(self.visualizer.get_marker_pairs())

    def removes_marked(self):
        """True when the marked parts are cut out rather than kept."""
        return self.cut_mode_row.get_selected() == 1

    def set_removes_marked(self, remove):
        self.cut_mode_row.set_selected(int(remove))

    def _update_cut_rows(self):
        """Offer keeping or removing, ordering, joining and preview while any
        file has segments. What remains after removing is one file in order."""
        has_segments = bool(self.visualizer.get_marker_pairs()) or any(
            segments
            for path, segments in self.file_markers.items()
            if path != self.active_audio_id
        )
        for row in self.file_queue.file_rows:
            row.set_cut_count(
                len(self.visualizer.get_marker_pairs())
                if row.file_path == self.active_audio_id
                else len(self.file_markers.get(row.file_path, ()))
            )
        keeping = not self.removes_marked()
        self.play_selection_switch.set_visible(has_segments)
        # Short: the reason shows on the row. The first row says where to
        # mark; the ones that wait for it only that it comes first.
        unmarked = None if has_segments else _("Mark a segment on the waveform first.")
        first = None if has_segments else _("Mark a segment first.")
        joining = (
            _("Joining files makes one file of everything.")
            if self._joining()
            else None
        )
        self.cut_mode_row.set_unavailable(unmarked)
        self.cut_order_row.set_unavailable(
            first or (None if keeping else _("Removing keeps the timeline order."))
        )
        self.cut_output_row.set_unavailable(
            first
            or (None if keeping else _("Removing leaves a single file."))
            or joining
        )

    def _sync_join(self):
        """Joining needs two files and an encoded format."""
        copy = self._format_list[self.format_row.get_selected()] == "copy"
        self.join_row.set_unavailable(
            _("The original format cannot join files")
            if copy
            else _("Add another file to join them.")
            if len(self.file_queue.files) < 2
            else None,
        )
        self._on_join_changed()

    def _joining(self):
        return self.join_row.get_active() and self.join_row.get_sensitive()

    def _on_join_changed(self, *_args):
        self._update_cut_rows()
        self._update_convert_label()

    def _on_cut_output_changed(self, row, pspec):
        self.app.config.set("cut_output_mode", str(row.get_selected()))

    def _update_preview_toggle(self):
        """Listening without the adjustments only means something while one is on."""
        copy = self._format_list[self.format_row.get_selected()] == "copy"
        # The sidebar's rows and the playback bar's volume, speed and voice.
        count = (
            self.effect_cards.active_count()
            + (self.volume_spin.get_value() != 100)
            + (self.speed_spin.get_value() != 1.0)
            + self._noise_active()
            + self._equalizer_applied()
        )
        summary = gettext.ngettext(
            "{count} adjustment", "{count} adjustments", count
        ).format(count=count)
        self.adjustments_label.set_label(summary)
        self.adjustments_label.set_visible(count > 0)
        self.sidebar_button.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION], [summary]
        )
        shown = not copy and count > 0
        if not shown:
            self.original_preview.set_active_name("adjusted")
        self.original_preview.set_visible(shown)
        self._apply_effects_bypass()

    def _apply_effects_bypass(self):
        copy = self._format_list[self.format_row.get_selected()] == "copy"
        self.player.set_effects_bypassed(
            copy or self.original_preview.get_active_name() == "original"
        )

    def _set_copy_mode_ui(self, is_copy_mode):
        """Bypass effects without discarding the user's saved processing choices."""
        self.copy_notice.set_visible(is_copy_mode)
        self.precision_list.set_visible(
            self._format_list[self.format_row.get_selected()] == "flac"
        )
        # Copy keeps the encoded audio as it is: no encoding to choose, and
        # nothing that processes it applies.
        if is_copy_mode:
            self.effect_cards.close()
            self.bar_cards.close_all()
        kept = (
            _("The original format keeps the audio as it is.") if is_copy_mode else None
        )
        for row in (
            self.channels_row,
            self.sample_rate_row,
            self.normalize_row,
            self.compressor_row,
        ):
            row.set_unavailable(kept)
        bar_controls = (self.volume_button, self.speed_button, self.voice_button)
        for widget in (
            *(b.get_parent() for b in bar_controls),
            self.equalizer_button,
        ):
            widget.set_sensitive(not is_copy_mode)
        self._update_noise_availability()
        self._update_preview_toggle()
        self._sync_join()
        if is_copy_mode:
            self.eq_revealer.set_reveal_child(False)

    def _slider_to_volume(self, slider_value):
        """Convert slider position (0-100) to volume (0-1000) with quadratic curve."""
        return round((slider_value / 100.0) ** 2 * 1000.0)

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

    def show_delete_mode(self):
        """Say where originals go: the Trash, or deleted outright."""
        permanent = self._config_bool("skip_trash", False)
        self.delete_box.set_tooltip_text(
            _(
                "Originals are deleted permanently, without the Trash, only "
                "after every file converted from them is checked"
            )
            if permanent
            else _(
                "Originals go to the Trash only after every file converted "
                "from them is checked"
            )
        )
        self.file_queue.set_permanent_delete(permanent)

    def _on_delete_originals_changed(self, switch, _pspec):
        self.app.config.set("delete_originals", switch.get_active())

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
        self.bitrate_channels_row.set_active(True)
        self._set_output_folder("")
        self.delete_originals_switch.set_active(False)
        self.set_removes_marked(False)
        self.join_row.set_active(False)
        self.cut_order_row.set_selected(0)
        self.cut_output_row.set_selected(0)
        self._set_processing_volume(100)
        self._set_processing_speed(1.0)
        self.noise_model.set_active(0)
        self.noise_adjustment.set_value(0)
        self.compressor_row.steps.set_selected(0)
        self.normalize_row.set_active(False)
        self.eq_panel.preset_dropdown.set_selected(
            self.eq_panel.PRESET_KEYS.index("flat")
        )
        self.auto_advance_action.change_state(GLib.Variant.new_boolean(True))
        self.original_preview.set_active_name("adjusted")
        self.pause_panel.length_buttons[PAUSE_LENGTHS.index(1.0)].set_active(True)
        self.pause_panel.sensitivity.set_value(DEFAULT_VOICE_SENSITIVITY)
        self.voice_only_switch.set_active(False)
        self.voice_only_scale.set_value(DEFAULT_VOICE_SENSITIVITY)

    def _restore_conversion_settings(self):
        """Restore saved conversion settings once every control exists."""
        self.format_row.set_selected(
            self._config_list_index("conversion_format", self._format_list, 0)
        )
        self.channels_row.set_selected(
            self._config_int("audio_channels", 0, lo=0, hi=2)
        )
        # Through the setters, which also show a value equal to the default.
        self._set_processing_volume(self._config_float("conversion_volume", 100))
        self._set_processing_speed(self._config_float("conversion_speed", 1.0))

        engine = self.app.config.get("noise_engine", "dfn3")
        self.noise_model.set_active(self.noise_engines.index(engine))
        self._on_noise_model_changed(self.noise_model, None)
        self.noise_adjustment.set_value(
            self._config_float("noise_strength", 100)
            if self._config_bool("noise_reduction_enabled")
            else 0
        )
        self.voice_only_switch.set_active(self._config_bool("voice_only_enabled"))
        self.voice_only_scale.set_value(
            self._config_float("voice_only_sensitivity", DEFAULT_VOICE_SENSITIVITY)
        )
        self._on_voice_only_changed()
        # Connected once the saved choice shows, which is no new choice.
        self.voice_only_switch.connect(
            "notify::active", weak_callback(self._on_voice_only_changed)
        )
        self.voice_only_scale.connect(
            "value-changed", weak_callback(self._on_voice_only_changed)
        )

        # Off is the first step; the strength is kept for later. A saved
        # intensity between steps takes the nearest one.
        intensity = self._config_float("compressor_intensity", 1.0)
        self.compressor_row.steps.set_selected(
            min(
                range(1, len(COMPRESSOR_STEPS)),
                key=lambda step: abs(COMPRESSOR_STEPS[step] - intensity),
            )
            if self._config_bool("compressor_enabled")
            else 0
        )

        self.normalize_row.set_active(self._config_bool("normalize_enabled"))

        # The former cut mode 2 was "in the order marked".
        self.cut_order_row.set_selected(
            int(self._config_int("cut_audio_mode", 1, lo=0, hi=2) == 2)
        )
        self.cut_output_row.set_selected(
            self._config_int("cut_output_mode", 0, lo=0, hi=1)
        )
