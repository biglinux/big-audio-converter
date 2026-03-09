"""
Inline equalizer panel with vertical sliders for audio frequency adjustment.
Designed to slide in/out using Gtk.Revealer at the bottom of the playlist area.
"""

import gettext

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gtk

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

_CSS = """
.eq-panel {
    border-top: 1px solid alpha(currentColor, 0.15);
}
.eq-band-value {
    font-size: 10px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    min-width: 24px;
}
.eq-band-value.active {
    color: @accent_color;
}
.eq-band-freq {
    font-size: 10px;
    opacity: 0.55;
}
.eq-scale-column scale {
    min-height: 100px;
}
.eq-scale-column scale trough {
    min-width: 4px;
    border-radius: 2px;
}
.eq-scale-column scale slider {
    min-width: 16px;
    min-height: 16px;
}
"""

_css_loaded = False


def _ensure_css():
    global _css_loaded
    if _css_loaded:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS.encode())
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
    )
    _css_loaded = True


class EqualizerPanel(Gtk.Box):
    """Inline equalizer panel with vertical sliders and preset selector."""

    BANDS = [
        ("31", 31),
        ("63", 63),
        ("125", 125),
        ("250", 250),
        ("500", 500),
        ("1k", 1000),
        ("2k", 2000),
        ("4k", 4000),
        ("8k", 8000),
        ("16k", 16000),
    ]

    PRESET_KEYS = [
        "default_voice", "flat", "voice_boost", "podcast", "warm",
        "bright", "de_esser", "bass_cut", "presence", "custom",
    ]

    PRESETS = {
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
        "custom": {
            "name": _("Custom"),
            "bands": [0.0] * 10,
        },
    }

    def __init__(self, player, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)
        _ensure_css()
        self.add_css_class("eq-panel")
        self.player = player
        self.band_scales = {}
        self._value_labels = {}
        self._updating_preset = False
        self._build_ui()

    def _build_ui(self):
        # Header row
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(16)
        header.set_margin_end(8)
        header.set_margin_top(8)
        header.set_margin_bottom(4)

        # Title
        title = Gtk.Label(label=_("Equalizer"))
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        header.append(title)

        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)

        # Preset dropdown
        dropdown_labels = [self.PRESETS[k]["name"] for k in self.PRESET_KEYS]
        self.preset_dropdown = Gtk.DropDown.new_from_strings(dropdown_labels)
        self.preset_dropdown.set_selected(1)  # Start at "flat"
        self.preset_dropdown.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Equalizer preset")],
        )
        self.preset_dropdown.connect("notify::selected", self._on_preset_changed)
        self.preset_dropdown.set_valign(Gtk.Align.CENTER)
        header.append(self.preset_dropdown)

        # Close button
        close_btn = Gtk.Button(icon_name="window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.add_css_class("circular")
        close_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Close equalizer")],
        )
        close_btn.connect("clicked", self._on_close)
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
            scale.connect("value-changed", self._on_scale_changed, freq)
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
            self.preset_dropdown.handler_block_by_func(self._on_preset_changed)
            self.preset_dropdown.set_selected(custom_idx)
            self.preset_dropdown.handler_unblock_by_func(self._on_preset_changed)
            self._apply_equalizer()

    def _on_preset_changed(self, dropdown, _pspec):
        idx = dropdown.get_selected()
        if idx < len(self.PRESET_KEYS):
            key = self.PRESET_KEYS[idx]
            if key != "custom":
                values = self.PRESETS[key]["bands"]
                self._updating_preset = True
                for i, (_, freq) in enumerate(self.BANDS):
                    if i < len(values):
                        self.band_scales[freq].set_value(values[i])
                self._updating_preset = False
                self._apply_equalizer()

    def _on_close(self, _button):
        parent = self.get_parent()
        if isinstance(parent, Gtk.Revealer):
            parent.set_reveal_child(False)

    def _apply_equalizer(self):
        eq_bands = []
        for _, freq in self.BANDS:
            gain = self.band_scales[freq].get_value()
            if gain != 0:
                eq_bands.append((freq, gain))
        if hasattr(self.player, "set_equalizer_bands"):
            self.player.set_equalizer_bands(eq_bands)
