"""
Inline equalizer panel with vertical sliders for audio frequency adjustment.
Designed to slide in/out using Gtk.Revealer at the bottom of the playlist area.
"""

import gettext
from typing import ClassVar

import gi

from app.audio.profiles import EQ_FREQUENCIES
from app.main_loop import weak_callback

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

_ = gettext.gettext


class EqualizerPanel(Gtk.Box):
    """Inline equalizer panel with vertical sliders and preset selector."""

    # Labels as the sliders show them: 1000 Hz is "1k".
    BANDS: ClassVar = [
        (f"{freq // 1000}k" if freq >= 1000 else str(freq), freq)
        for freq in EQ_FREQUENCIES
    ]

    PRESET_KEYS: ClassVar = [
        "default_voice",
        "flat",
        "voice_boost",
        "podcast",
        "warm",
        "bright",
        "de_esser",
        "bass_cut",
        "presence",
        "custom",
    ]

    PRESETS: ClassVar = {
        "default_voice": {
            "name": _("Default Voice"),
            "bands": [0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 2.0, 3.0, 1.0, 0.0],
        },
        "flat": {
            "name": _("Natural (No Effects)"),
            "bands": [0.0] * 10,
        },
        "voice_boost": {
            "name": _("Crystal Voice"),
            "bands": [-10.0, -5.0, 0.0, 5.0, 15.0, 20.0, 15.0, 10.0, 5.0, 0.0],
        },
        "podcast": {
            "name": _("Radio Host"),
            "bands": [5.0, 5.0, 10.0, 5.0, 0.0, 5.0, 10.0, 5.0, 0.0, -5.0],
        },
        "warm": {
            "name": _("Velvet Voice"),
            "bands": [10.0, 15.0, 10.0, 5.0, 0.0, -5.0, -10.0, -15.0, -15.0, -20.0],
        },
        "bright": {
            "name": _("Extra Brightness"),
            "bands": [-10.0, -5.0, 0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 20.0, 15.0],
        },
        "de_esser": {
            "name": _("Soften 'S' (De-esser)"),
            "bands": [0.0, 0.0, 0.0, 0.0, 0.0, -5.0, -15.0, -25.0, -20.0, -10.0],
        },
        "bass_cut": {
            "name": _("Remove Rumble"),
            "bands": [-40.0, -35.0, -25.0, -15.0, -5.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        },
        "presence": {
            "name": _("Present Voice"),
            "bands": [-5.0, 0.0, 5.0, 10.0, 15.0, 20.0, 15.0, 10.0, 5.0, 0.0],
        },
        "custom": {"name": _("Custom")},
    }

    def __init__(self, player, config, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)
        self.add_css_class("eq-panel")
        self.player = player
        self.config = config
        self.on_changed = None
        self.band_scales = {}
        self._value_labels = {}
        self._updating_preset = False
        self._build_ui()
        self._restore()

    def _restore(self):
        """The preset and bands of the last session; anything unreadable is flat."""
        key = self.config.get("equalizer_preset", "flat")
        try:
            gains = [
                float(v) for v in self.config.get("equalizer_bands", "").split(",")
            ]
        except (AttributeError, ValueError):
            gains = []
        if key not in self.PRESET_KEYS or len(gains) != len(self.BANDS):
            key, gains = "flat", self.PRESETS["flat"]["bands"]
        self._updating_preset = True
        for (_, freq), gain in zip(self.BANDS, gains, strict=True):
            self.band_scales[freq].set_value(gain)
        self._updating_preset = False
        self.preset_dropdown.handler_block(self._preset_dropdown_handler)
        self.preset_dropdown.set_selected(self.PRESET_KEYS.index(key))
        self.preset_dropdown.handler_unblock(self._preset_dropdown_handler)
        self._apply_equalizer()

    def _build_ui(self):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(16)
        header.set_margin_end(8)
        header.set_margin_top(8)
        header.set_margin_bottom(4)

        title = Gtk.Label(label=_("Equalizer"))
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        header.append(title)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)

        # Preset dropdown
        dropdown_labels = [self.PRESETS[k]["name"] for k in self.PRESET_KEYS]
        self.preset_dropdown = Gtk.DropDown.new_from_strings(dropdown_labels)
        self.preset_dropdown.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Equalizer preset")],
        )
        self._preset_dropdown_handler = self.preset_dropdown.connect(
            "notify::selected", weak_callback(self._on_preset_changed)
        )
        self.preset_dropdown.set_valign(Gtk.Align.CENTER)
        header.append(self.preset_dropdown)

        close_btn = Gtk.Button(icon_name="window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.add_css_class("circular")
        close_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Close equalizer")],
        )
        close_btn.connect("clicked", weak_callback(self._on_close))
        header.append(close_btn)

        self.append(header)

        # Sliders area
        sliders_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        sliders_box.set_margin_start(20)
        sliders_box.set_margin_end(20)
        sliders_box.set_margin_top(2)
        sliders_box.set_margin_bottom(8)
        sliders_box.set_homogeneous(True)

        for label_text, freq in self.BANDS:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            col.add_css_class("eq-scale-column")
            col.set_halign(Gtk.Align.CENTER)

            # dB value
            val_label = Gtk.Label(label="0")
            val_label.add_css_class("eq-band-value")
            val_label.add_css_class("dim-label")
            col.append(val_label)
            self._value_labels[freq] = val_label

            # Vertical slider
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -40, 40, 0.5)
            scale.set_inverted(True)
            scale.set_value(0)
            scale.set_draw_value(False)
            scale.set_vexpand(True)
            scale.set_hexpand(False)
            scale.set_size_request(-1, 100)
            scale.add_mark(0, Gtk.PositionType.LEFT, None)
            scale.connect("value-changed", weak_callback(self._on_scale_changed), freq)
            scale.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [_("Equalizer band {} Hz").format(label_text)],
            )
            self.band_scales[freq] = scale
            col.append(scale)

            # Frequency label
            freq_label = Gtk.Label(label=label_text)
            freq_label.add_css_class("eq-band-freq")
            col.append(freq_label)

            sliders_box.append(col)

        self.append(sliders_box)

    def _on_scale_changed(self, scale, freq):
        val = scale.get_value()
        lbl = self._value_labels[freq]
        lbl.set_text(f"+{val:.0f}" if val > 0 else f"{val:.0f}")
        if val != 0:
            lbl.remove_css_class("dim-label")
            lbl.add_css_class("active")
        else:
            lbl.remove_css_class("active")
            lbl.add_css_class("dim-label")

        if not self._updating_preset:
            # Switch dropdown to "custom" (last item)
            custom_idx = len(self.PRESET_KEYS) - 1
            self.preset_dropdown.handler_block(self._preset_dropdown_handler)
            self.preset_dropdown.set_selected(custom_idx)
            self.preset_dropdown.handler_unblock(self._preset_dropdown_handler)
            self._apply_equalizer()

    def _on_preset_changed(self, dropdown, _pspec):
        key = self.PRESET_KEYS[dropdown.get_selected()]
        if key != "custom":
            self._updating_preset = True
            for (_, freq), gain in zip(
                self.BANDS, self.PRESETS[key]["bands"], strict=True
            ):
                self.band_scales[freq].set_value(gain)
            self._updating_preset = False
        self._apply_equalizer()

    def _on_close(self, _button):
        parent = self.get_parent()
        if isinstance(parent, Gtk.Revealer):
            parent.set_reveal_child(False)

    def _apply_equalizer(self):
        gains = [self.band_scales[freq].get_value() for _, freq in self.BANDS]
        self.player.set_equalizer_bands(
            [
                (freq, gain)
                for (_, freq), gain in zip(self.BANDS, gains, strict=True)
                if gain
            ]
        )
        self.config.set(
            "equalizer_preset", self.PRESET_KEYS[self.preset_dropdown.get_selected()]
        )
        self.config.set("equalizer_bands", ",".join(f"{gain:g}" for gain in gains))
        if self.on_changed:
            self.on_changed()
