"""
Equalizer dialog for audio preview configuration.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk


class EqualizerDialog(Gtk.Dialog):
    """Dialog for configuring the equalizer."""

    def __init__(self, parent, player, **kwargs):
        super().__init__(
            title="Equalizer",
            transient_for=parent,
            use_header_bar=1,
            modal=True,
            **kwargs,
        )

        self.player = player

        # Set up dialog - only Close button, no Apply
        self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.set_default_response(Gtk.ResponseType.CLOSE)

        # Create content area
        content_area = self.get_content_area()
        content_area.set_margin_start(12)
        content_area.set_margin_end(12)
        content_area.set_margin_top(12)
        content_area.set_margin_bottom(12)
        content_area.set_spacing(6)

        # Create preset selector
        preset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        preset_box.append(Gtk.Label(label="Preset:"))

        self.preset_combo = Gtk.ComboBoxText()
        for preset in [
            "Flat",
            "Bass Boost",
            "Treble Boost",
            "Vocal Boost",
            "Rock",
            "Dance",
        ]:
            self.preset_combo.append_text(preset)
        self.preset_combo.set_active(0)
        self.preset_combo.set_hexpand(True)
        self.preset_combo.connect("changed", self._on_preset_changed)
        preset_box.append(self.preset_combo)

        content_area.append(preset_box)

        # Add separator
        content_area.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Define frequency bands
        self.bands = [
            ("60 Hz", 60),
            ("150 Hz", 150),
            ("400 Hz", 400),
            ("1 kHz", 1000),
            ("2.4 kHz", 2400),
            ("6 kHz", 6000),
            ("16 kHz", 16000),
        ]

        # Create sliders for different frequency bands
        self.band_scales = {}

        for name, freq in self.bands:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.append(Gtk.Label(label=name))

            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -12, 12, 1)
            scale.set_value(0)
            scale.set_size_request(200, -1)
            scale.add_mark(0, Gtk.PositionType.BOTTOM, "0")
            scale.set_hexpand(True)

            # Connect value-changed signal to apply changes immediately
            scale.connect("value-changed", self._on_scale_value_changed)

            # Store reference to the scale
            self.band_scales[freq] = scale

            row.append(scale)
            content_area.append(row)

        # Add reset button
        reset_button = Gtk.Button(label="Reset")
        reset_button.connect("clicked", self._on_reset)
        reset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        reset_box.set_halign(Gtk.Align.END)
        reset_box.append(reset_button)
        content_area.append(reset_box)

        # Connect response signal
        self.connect("response", self._on_response)

    def _on_preset_changed(self, combo):
        """Apply selected equalizer preset."""
        preset_name = combo.get_active_text()

        # Define presets - these are internal equalizer presets, not conversion presets
        presets = {
            "Flat": [0, 0, 0, 0, 0, 0, 0],
            "Bass Boost": [6, 6, 6, 4, 0, 0, 0],
            "Treble Boost": [0, 0, 0, 0, 0, 2, 4],
            "Vocal Boost": [0, 2, 4, 6, 6, 4, 2],
            "Rock": [4, 2, -4, -6, -2, 2, 4],
            "Dance": [6, 4, 2, 0, 0, -4, -6],
        }

        # Apply preset values
        if preset_name in presets:
            values = presets[preset_name]

            # Block the signal handlers temporarily to avoid multiple applications
            for scale in self.band_scales.values():
                scale.handler_block_by_func(self._on_scale_value_changed)

            for i, (_, freq) in enumerate(self.bands):
                if i < len(values):
                    self.band_scales[freq].set_value(values[i])

            # Unblock the signal handlers
            for scale in self.band_scales.values():
                scale.handler_unblock_by_func(self._on_scale_value_changed)

            # Apply the equalizer settings once after all values are set
            self.apply_equalizer()

    def _on_reset(self, button):
        """Reset all equalizer bands to zero."""
        # Block the signal handlers temporarily
        for scale in self.band_scales.values():
            scale.handler_block_by_func(self._on_scale_value_changed)

        # Reset all sliders to zero
        for scale in self.band_scales.values():
            scale.set_value(0)

        # Unblock the signal handlers
        for scale in self.band_scales.values():
            scale.handler_unblock_by_func(self._on_scale_value_changed)

        # Apply the reset equalizer settings
        self.apply_equalizer()

    def _on_response(self, dialog, response_id):
        """Handle dialog response."""
        # Close the dialog
        self.destroy()

    def _on_scale_value_changed(self, scale):
        """Handle a slider value change."""
        # Apply the equalizer settings immediately when a slider changes
        self.apply_equalizer()

    def apply_equalizer(self):
        """Apply the current equalizer settings to the player."""
        # Collect band settings
        eq_bands = []
        for _, freq in self.bands:
            gain = self.band_scales[freq].get_value()
            # Only include bands with non-zero gain
            if gain != 0:
                eq_bands.append((freq, gain))

        # Apply to player
        if hasattr(self.player, "set_equalizer_bands"):
            self.player.set_equalizer_bands(eq_bands)
