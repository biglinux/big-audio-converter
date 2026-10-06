"""The window's conversion settings: rows, their saved values and effects."""

import gettext
import locale
import logging
import math
import weakref

import gi

from app.utils.main_loop import weak_callback

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango

from app.audio.profiles import BITRATES, SAMPLE_RATES

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


def _button_label_factory(labels):
    """Button text: the short label when entries carry an explanation.

    ``labels`` is read at bind time, so replacing its items with the model's
    relabels the button. The text ellipsizes so a long choice narrows the
    button instead of wrapping the row title.
    """
    factory = Gtk.SignalListItemFactory()
    factory.connect(
        "setup",
        lambda _f, item: item.set_child(
            Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        ),
    )
    factory.connect(
        "bind",
        lambda _f, item: item.get_child().set_label(
            labels[item.get_position()] if labels else item.get_item().get_string()
        ),
    )
    return factory


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
        entries.append(
            _("{quality} — {kbps} kbps, about {size} MB per minute").format(
                quality=word, kbps=kbps, size=megabytes
            )
        )
        buttons.append(f"{word} · {kbps} kbps")
    return entries, buttons


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
        self.short_labels = list(short_labels or ())
        self.dropdown.set_factory(_button_label_factory(self.short_labels))
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

    def set_model(self, model, short_labels=None):
        if short_labels is not None:
            self.short_labels[:] = short_labels
        self.dropdown.set_model(model)


class LevelRow(Adw.PreferencesRow):
    """A correction set by how much of it applies; the lowest value is off.

    ``describe`` turns the value into the text beside the title. The wheel
    scrolls the sidebar instead of moving a slider passing under the pointer.
    """

    def __init__(self, title, adjustment, describe, subtitle=None):
        super().__init__(title=title, activatable=False)
        self.describe = describe
        self.box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_start=12,
            margin_end=12,
            margin_top=8,
            margin_bottom=4,
        )
        header = Gtk.Box(spacing=12)
        header.append(Gtk.Label(label=title, xalign=0, hexpand=True, wrap=True))
        self.value_label = Gtk.Label(xalign=1, css_classes=["dim-label", "numeric"])
        header.append(self.value_label)
        self.box.append(header)
        if subtitle:
            self.box.append(
                Gtk.Label(
                    label=subtitle,
                    xalign=0,
                    wrap=True,
                    css_classes=["caption", "dim-label"],
                )
            )
        self.scale = Gtk.Scale(adjustment=adjustment, draw_value=False, digits=0)
        self.scale.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.scale.connect("value-changed", weak_callback(self._show_value))
        wheel = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL,
            propagation_phase=Gtk.PropagationPhase.CAPTURE,
        )
        wheel.connect("scroll", weak_callback(self._scroll_sidebar))
        self.scale.add_controller(wheel)
        self.box.append(self.scale)
        self.set_child(self.box)
        self._show_value(self.scale)

    def _show_value(self, scale):
        text = self.describe(scale.get_value())
        self.value_label.set_text(text)
        scale.update_property([Gtk.AccessibleProperty.VALUE_TEXT], [text])

    def _scroll_sidebar(self, controller, _dx, dy):
        scrolled = self.get_ancestor(Gtk.ScrolledWindow)
        if scrolled is None:
            return False
        adjustment = scrolled.get_vadjustment()
        # GTK's own wheel step for a scrolled window; touchpads report pixels.
        step = (
            1
            if controller.get_unit() == Gdk.ScrollUnit.SURFACE
            else adjustment.get_page_size() ** (2 / 3)
        )
        adjustment.set_value(adjustment.get_value() + dy * step)
        return True


class SettingsManagerMixin:
    """Mixin providing all conversion-settings logic for MainWindow."""

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

        # Restoring settings runs handlers before the waveform exists.

        # Most requests name the format, so the common ones come first and the
        # packet copy, which ignores every other choice, comes last.
        self._format_list = ["mp3", "m4a", "opus", "flac", "wav", "ogg", "copy"]
        self.format_row = ChoiceRow(
            _("Format"),
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
        )
        self.format_row.set_selected(0)
        self.format_row.connect(
            "notify::selected", weak_callback(self._on_format_changed)
        )
        options.add(self.format_row)

        self._bitrate_list = list(BITRATES["mp3"])
        self.bitrate_row = ChoiceRow(
            _("Quality"), *quality_labels("mp3", self._bitrate_list)
        )
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
        self.speed_spin = Adw.SpinRow.new_with_range(0.10, 5.0, 0.05)
        self.speed_spin.set_title(_("Speed (×)"))
        self.speed_spin.set_digits(2)
        self.speed_spin.set_value(1.0)
        self.speed_spin.connect(
            "notify::value", lambda row, _pspec: owner._on_speed_spin_changed(row)
        )

        # The voice filter sits in the playback bar, which is built later; its
        # controls exist now so the saved settings can be restored into them.
        # Strength 0 is off.
        self.noise_engines = ("dfn3", "dpdfnet")
        self.noise_value_label = Gtk.Label(css_classes=["caption"])
        self.noise_status = Gtk.Label(
            wrap=True,
            max_width_chars=16,
            justify=Gtk.Justification.CENTER,
            css_classes=["caption", "warning"],
        )
        self.noise_scale = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, 0, 100, 1)
        self.noise_scale.add_mark(50, Gtk.PositionType.RIGHT, None)
        self.noise_scale.connect(
            "value-changed", weak_callback(self._on_noise_strength_changed)
        )
        self.noise_model_row = ChoiceRow(
            _("Cleaning model"),
            [_("Light · DeepFilterNet3"), _("Higher quality · DPDFNet-2")],
            [_("Light"), _("Higher quality")],
        )
        self.noise_model_row.connect(
            "notify::selected", weak_callback(self._on_noise_model_changed)
        )

        equalizer_row = self.equalizer_row = Adw.ActionRow(title=_("Equalizer"))
        configure = Gtk.Button(label=_("Configure…"), valign=Gtk.Align.CENTER)
        configure.connect("clicked", lambda *_: owner.eq_toggle_btn.set_active(True))
        equalizer_row.add_suffix(configure)
        equalizer_row.set_activatable_widget(configure)

        # Files are cut wherever segments are marked on the waveform; these
        # rows appear once there is something to keep, remove, order or join.
        self.cut_mode_row = ChoiceRow(
            _("Marked parts"), [_("Keep them"), _("Remove them")]
        )
        self.cut_mode_row.set_visible(False)
        self.cut_mode_row.connect(
            "notify::selected", weak_callback(self._on_cut_mode_changed)
        )
        options.add(self.cut_mode_row)
        self.cut_order_row = ChoiceRow(
            _("Segment order"), [_("In timeline order"), _("In the order marked")]
        )
        self.cut_order_row.set_visible(False)
        self.cut_order_row.connect(
            "notify::selected", weak_callback(self._on_cut_order_changed)
        )
        options.add(self.cut_order_row)
        self.cut_output_row = ChoiceRow(
            _("Save segments"), [_("Separate files"), _("Join this file's segments")]
        )
        self.cut_output_row.set_visible(False)
        self.cut_output_row.connect(
            "notify::selected", weak_callback(self._on_cut_output_changed)
        )
        options.add(self.cut_output_row)
        self.join_row = Adw.SwitchRow(title=_("Join into one file"), visible=False)
        self.join_row.connect("notify::active", weak_callback(self._on_join_changed))
        options.add(self.join_row)

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

        # Each recording needs its own amount of each correction, so all of
        # them stay in sight, set by how much; the lowest value is off.
        corrections = self.corrections_group = Adw.PreferencesGroup(
            title=_("Fix the sound"),
            margin_start=12,
            margin_end=12,
            margin_top=18,
            css_classes=["compact-options"],
        )
        parent_box.append(corrections)

        def percent(value):
            return _("Off") if value == 0 else f"{value:.0f}%"

        self.voice_row = LevelRow(
            _("Clean voice"), self.noise_scale.get_adjustment(), percent
        )
        self.voice_row.box.append(self.noise_status)
        corrections.add(self.voice_row)
        corrections.add(self.noise_model_row)
        self._update_noise_availability()
        for prefix, title, subtitle in (
            ("gate", _("Silence pauses"), _("Mutes the background between words")),
            (
                "compressor",
                _("Balance loud and quiet parts"),
                _("Brings quiet passages closer to the loud ones"),
            ),
        ):
            row = LevelRow(
                title,
                Gtk.Adjustment(upper=100, step_increment=1, page_increment=10),
                percent,
                subtitle,
            )
            row.scale.connect(
                "value-changed", weak_callback(self._on_level_changed), prefix
            )
            corrections.add(row)
            setattr(self, prefix + "_row", row)
        # The filter starts at 20 Hz; the first steps above off all mean that.
        self.hpf_row = LevelRow(
            _("Remove low rumble"),
            Gtk.Adjustment(upper=300, step_increment=5, page_increment=20),
            lambda value: _("Off") if value == 0 else f"{max(value, 20):.0f} Hz",
            _("Cuts hum and handling noise below the voice"),
        )
        self.hpf_row.scale.connect(
            "value-changed", weak_callback(self._on_hpf_level_changed)
        )
        corrections.add(self.hpf_row)
        self.normalize_row = Adw.SwitchRow(
            title=_("Even volume"),
            subtitle=_("Brings every file to the same loudness (−16 LUFS)"),
        )
        self.normalize_row.connect(
            "notify::active", weak_callback(self._on_normalize_switch_changed)
        )
        corrections.add(self.normalize_row)
        self.clipping_row = Adw.SwitchRow(
            title=_("Prevent distortion"),
            subtitle=_("Keeps loud peaks from clipping"),
        )
        self.clipping_row.connect(
            "notify::active", weak_callback(self._on_clipping_changed)
        )
        corrections.add(self.clipping_row)

        effects = self.effects_group = Adw.PreferencesGroup(
            title=_("Adjust sound"),
            margin_start=12,
            margin_end=12,
            margin_top=18,
            css_classes=["compact-options"],
        )
        parent_box.append(effects)
        for row in (self.volume_spin, self.speed_spin, equalizer_row):
            effects.add(row)

        more = Adw.PreferencesGroup(
            margin_start=12,
            margin_end=12,
            margin_top=18,
            css_classes=["compact-options"],
        )
        parent_box.append(more)

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
        more.add(self.advanced_row)

        # A group places plain widgets after its rows.
        self.copy_notice = Gtk.Label(
            label=_(
                "The original format keeps the encoded audio. Cuts follow packet boundaries and may not match the exact times. Effects are bypassed in preview and export."
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
                "Boosting volume or equalizer bands can distort audio. Reduce the gain or turn on Prevent distortion."
            ),
            wrap=True,
            xalign=0,
            margin_top=12,
            css_classes=["dim-label", "caption"],
        )
        effects.add(self.gain_notice)

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

    def _on_format_changed(self, row, _pspec):
        if self._updating_profile:
            return
        format_name = self._format_list[row.get_selected()]
        self.app.config.set("conversion_format", format_name)
        self._updating_profile = True
        saved_bitrate = self.app.config.get("conversion_bitrate", "192k")
        self._bitrate_list = list(BITRATES.get(format_name, ("192k",)))
        entries, buttons = quality_labels(format_name, self._bitrate_list)
        self.bitrate_row.set_model(Gtk.StringList.new(entries), buttons)
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
        if self._syncing_volume:
            return
        self._syncing_volume = True
        try:
            volume = max(0.0, min(1000.0, volume))
            self.volume_spin.set_value(volume)
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
        boosted_eq = any(
            scale.get_value() > 0 for scale in self.eq_panel.band_scales.values()
        )
        self.gain_notice.set_visible(
            self.volume_spin.get_value() > 100.001 or boosted_eq
        )

    def _show_equalizer_state(self):
        """Name the preset on the equalizer row; boosted bands may distort."""
        self.equalizer_row.set_subtitle(
            self.eq_panel.preset_dropdown.get_selected_item().get_string()
        )
        self._update_gain_notice()

    def _on_speed_spin_changed(self, spin):
        self._set_processing_speed(spin.get_value())

    def _set_processing_speed(self, speed):
        if self._syncing_speed:
            return
        self._syncing_speed = True
        try:
            speed = max(0.1, min(5.0, speed))
            self.speed_spin.set_value(speed)
            self.speed_scale.set_value(self._speed_to_slider(speed))
            self.speed_value_label.set_text(f"{speed:.2f}×")
            self.player.set_playback_speed(speed)
            self.player.set_pitch_correction(True)
            self.app.config.set("conversion_speed", str(speed))
        finally:
            self._syncing_speed = False

    def _noise_engine(self):
        return self.noise_engines[self.noise_model_row.get_selected()]

    def _noise_active(self):
        return self.noise_available and self.noise_scale.get_value() > 0

    def _update_noise_availability(self):
        engine = self._noise_engine()
        self.noise_available = engine in self.player.noise_plugins
        is_copy = self._format_list[self.format_row.get_selected()] == "copy"
        for scale in (self.noise_scale, self.voice_row.scale):
            scale.set_sensitive(self.noise_available and not is_copy)
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
        strength = self.noise_scale.get_value() if self._noise_active() else 0
        self.noise_value_label.set_text(
            _("Clean voice · {percent}%").format(percent=f"{strength:.0f}")
            if strength
            else _("Clean voice · off")
        )
        # The model matters once the voice is cleaned, or to leave a missing one.
        self.noise_model_row.set_visible(
            self.noise_scale.get_value() > 0 or not self.noise_available
        )

    def _on_noise_strength_changed(self, scale):
        percent = scale.get_value()
        self.app.config.set("noise_strength", str(percent))
        self.app.config.set("noise_reduction_enabled", str(percent > 0).lower())
        self._show_noise_strength()
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

    def _on_level_changed(self, scale, prefix):
        """Gate or compressor: off at zero, otherwise that percent of it."""
        level = scale.get_value()
        if level:
            self.app.config.set(prefix + "_intensity", str(level / 100))
            getattr(self.player, f"set_{prefix}_intensity")(level / 100)
        self.app.config.set(prefix + "_enabled", str(level > 0).lower())
        getattr(self.player, f"set_{prefix}_enabled")(level > 0)

    def _on_hpf_level_changed(self, scale):
        frequency = scale.get_value()
        if frequency:
            frequency = max(int(frequency), 20)
            self.app.config.set("hpf_frequency", str(frequency))
            self.player.set_hpf_frequency(frequency)
        self.app.config.set("hpf_enabled", str(frequency > 0).lower())
        self.player.set_hpf_enabled(frequency > 0)

    def _on_normalize_switch_changed(self, row, _pspec):
        state = row.get_active()
        self.app.config.set("normalize_enabled", str(state).lower())
        self.player.set_normalize(state)

    def _on_cut_order_changed(self, row, _pspec):
        # Saved as the former cut mode: 2 was "in the order marked".
        self.app.config.set("cut_audio_mode", "2" if row.get_selected() else "0")

    def _on_cut_mode_changed(self, row, _pspec):
        remove = self.removes_marked()
        self.app.config.set("cut_remove", remove)
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
        keeping = not self.removes_marked()
        self.cut_mode_row.set_visible(has_segments)
        self.play_selection_switch.set_visible(has_segments)
        self.cut_order_row.set_visible(has_segments and keeping)
        # Joining makes one file of everything, segments included.
        self.cut_output_row.set_visible(
            has_segments and keeping and not self._joining()
        )

    def _joining(self):
        return self.join_row.get_active() and self.join_row.get_sensitive()

    def _on_join_changed(self, *_args):
        self._update_cut_rows()
        self._update_convert_label()

    def _on_cut_output_changed(self, row, pspec):
        """Handle cut output mode change (separate files vs merge)."""
        self.app.config.set("cut_output_mode", str(row.get_selected()))

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

    def _set_copy_mode_ui(self, is_copy_mode):
        """Bypass effects without discarding the user's saved processing choices."""
        self.copy_notice.set_visible(is_copy_mode)
        self.precision_row.set_visible(
            self._format_list[self.format_row.get_selected()] == "flac"
        )
        self.advanced_row.set_visible(not is_copy_mode)
        self.join_row.set_sensitive(not is_copy_mode)
        self.join_row.set_subtitle(
            _("The original format cannot join files")
            if is_copy_mode
            else _("In queue order, named after the first file")
        )
        # Copy keeps the encoded audio, so nothing that processes it applies.
        for widget in (self.effects_group, self.corrections_group):
            widget.set_sensitive(not is_copy_mode)
        self._update_noise_availability()
        self.player.set_effects_bypassed(
            is_copy_mode or self.original_preview.get_active()
        )
        self._on_join_changed()
        for widget in (
            self.original_preview,
            self._bar_popovers["volume"].get_parent(),
            self._bar_popovers["speed"].get_parent(),
            self._bar_popovers["voice"].get_parent(),
            self.eq_toggle_btn,
        ):
            widget.set_sensitive(not is_copy_mode)
        if is_copy_mode:
            for name in ("volume", "speed", "voice"):
                self._bar_popovers[name].popdown()
            self.eq_revealer.set_reveal_child(False)

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
        self.set_removes_marked(False)
        self.join_row.set_active(False)
        self.cut_order_row.set_selected(0)
        self.cut_output_row.set_selected(0)
        self._set_processing_volume(100)
        self._set_processing_speed(1.0)
        self.noise_model_row.set_selected(0)
        for scale in (
            self.noise_scale,
            self.gate_row.scale,
            self.compressor_row.scale,
            self.hpf_row.scale,
        ):
            scale.set_value(0)
        self.normalize_row.set_active(False)
        self.clipping_row.set_active(False)
        self.eq_panel.preset_dropdown.set_selected(
            self.eq_panel.PRESET_KEYS.index("flat")
        )
        self.auto_advance_switch.set_active(True)
        self.original_preview.set_active(False)

    def _restore_conversion_settings(self):
        """Restore saved conversion settings once every control exists."""
        self.format_row.set_selected(
            self._config_list_index("conversion_format", self._format_list, 0)
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
        self.noise_scale.set_value(
            self._config_float("noise_strength", 100)
            if self._config_bool("noise_reduction_enabled")
            else 0
        )

        # Off is the lowest value; the strength in between is kept for later.
        for prefix, default in (("gate", 0.5), ("compressor", 1.0)):
            getattr(self, prefix + "_row").scale.set_value(
                100 * self._config_float(prefix + "_intensity", default)
                if self._config_bool(prefix + "_enabled")
                else 0
            )
        self.hpf_row.scale.set_value(
            self._config_float("hpf_frequency", 80)
            if self._config_bool("hpf_enabled")
            else 0
        )

        # Restore normalization
        self.normalize_row.set_active(self._config_bool("normalize_enabled"))

        self.set_removes_marked(self._config_bool("cut_remove"))
        # The former cut mode 2 was "in the order marked".
        self.cut_order_row.set_selected(
            int(self._config_int("cut_audio_mode", 1, lo=0, hi=2) == 2)
        )
        self.cut_output_row.set_selected(
            self._config_int("cut_output_mode", 0, lo=0, hi=1)
        )
