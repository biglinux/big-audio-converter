# app/ui/controls_bar_mixin.py

"""
Controls Bar Mixin for MainWindow.

Extracts all bottom controls bar related methods (zoom popover, volume popover,
speed popover, visualizer viewport sync) from
MainWindow into a reusable mixin.

Usage:
    class MainWindow(ControlsBarMixin, SettingsManagerMixin, PlaybackControllerMixin, Adw.ApplicationWindow):
        ...
"""

import logging
import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

logger = logging.getLogger(__name__)


class ControlsBarMixin:
    """Mixin handling bottom controls bar interactions: zoom, volume, speed popovers and visualizer sync."""

    # --- Zoom popover ---

    def _format_zoom_value(self, scale, value):
        """Format zoom slider value to show actual zoom level."""
        return f"{self._slider_to_zoom(value):.1f}x"

    def _slider_to_zoom(self, slider_value):
        """Convert slider position (0-150) to zoom level (1-1000) logarithmically."""
        # Formula: zoom = 10^(slider_value/50)
        # slider_value=0 → zoom=1, slider_value=50 → zoom=10, slider_value=100 → zoom=100, slider_value=150 → zoom=1000
        return math.pow(10, slider_value / 50.0)

    def _zoom_to_slider(self, zoom_level):
        """Convert zoom level (1-1000) to slider position (0-150) logarithmically."""
        # Formula: slider_value = 50 * log10(zoom)
        # zoom=1 → slider_value=0, zoom=10 → slider_value=50, zoom=100 → slider_value=100, zoom=1000 → slider_value=150
        return 50.0 * math.log10(max(1.0, zoom_level))

    def _on_zoom_scale_changed(self, scale):
        """Handle zoom slider changes."""
        slider_value = scale.get_value()
        # Convert slider value to actual zoom level using logarithmic scale
        zoom_level = self._slider_to_zoom(slider_value)

        self.zoom_value_label.set_text(f"{zoom_level:.1f}x")
        self.visualizer.set_zoom_level(zoom_level, use_mouse_position=True)
        # Keep the label and seekbar on the clamped level actually applied.
        self._on_visualizer_zoom_changed(self.visualizer.zoom_level)

    def _on_zoom_btn_clicked(self, button):
        """Open the zoom popover."""
        self._close_all_bar_popovers(except_name="zoom")
        self.zoom_popover.popup()

    # --- Volume popover ---

    def _close_all_bar_popovers(self, except_name=None):
        for name in ("volume", "speed", "zoom"):
            if name != except_name:
                getattr(self, name + "_popover").popdown()

    def _on_volume_btn_clicked(self, button):
        self._close_all_bar_popovers(except_name="volume")
        self.volume_popover.popup()

    def _on_volume_scale_changed(self, scale):
        self._set_processing_volume(self._slider_to_volume(scale.get_value()))

    # --- Speed popover ---

    def _on_speed_btn_clicked(self, button):
        self._close_all_bar_popovers(except_name="speed")
        self.speed_popover.popup()

    def _on_speed_scale_changed(self, scale):
        self._set_processing_speed(self._slider_to_speed(scale.get_value()))

    # --- Visualizer/seekbar sync ---

    def _on_visualizer_zoom_changed(self, zoom_level):
        """Update zoom slider when zoom changes from visualizer (e.g. mouse wheel)."""
        # Temporarily block signal to avoid feedback loop
        self.zoom_scale.handler_block(self._zoom_scale_handler)
        self.zoom_scale.set_value(self._zoom_to_slider(zoom_level))
        self.zoom_scale.handler_unblock(self._zoom_scale_handler)
        self.zoom_value_label.set_text(f"{zoom_level:.1f}x")

        self._sync_seekbar_viewport()

    def _sync_seekbar_viewport(self):
        """Show the waveform's visible window on the seek bar."""
        self.seekbar.set_zoom_viewport(
            self.visualizer.zoom_level, self.visualizer.viewport_offset
        )
