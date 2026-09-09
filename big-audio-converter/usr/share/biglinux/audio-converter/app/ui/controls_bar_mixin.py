# app/ui/controls_bar_mixin.py

"""
Controls Bar Mixin for MainWindow.

Extracts all bottom controls bar related methods (zoom popover, volume popover,
speed popover, visualizer height/viewport sync) from
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

    # --- Visualizer height ---

    def _on_visualizer_height_changed(self, paned, param):
        """Handle visualizer height changes and save to config."""
        if not hasattr(self.app, "config") or not self.app.config:
            return

        # Don't save position when cut is off (paned is in collapsed state)
        if hasattr(self, "cut_row") and self.cut_row.get_selected() == 0:
            return

        # Get total height and position
        total_height = self.get_height()
        position = paned.get_position()

        # Calculate visualizer height (accounting for margins, zoom control bar, and seekbar)
        visualizer_height = (
            total_height - position - 50 - 34
        )  # 50px = 10px margin + 40px zoom controls; 34px = seekbar (28px + 6px margin)

        # Define minimum heights for both sections
        min_top_height = 200
        min_visualizer_height = 100

        # Make sure we don't resize the visualizer too small
        if visualizer_height < min_visualizer_height:
            # Calculate the maximum valid position to maintain minimum visualizer height
            max_position = total_height - min_visualizer_height - 50 - 34
            # Adjust the position
            paned.set_position(max_position)
            # Recalculate visualizer height
            visualizer_height = min_visualizer_height

        # Make sure we don't resize the top section too small (only check if visualizer constraint is satisfied)
        elif position < min_top_height:
            # Prevent the top section from getting too small
            paned.set_position(min_top_height)
            # Recalculate visualizer height
            visualizer_height = total_height - min_top_height - 50 - 34

        # Only save if it's a reasonable value
        if (
            visualizer_height >= min_visualizer_height
            and visualizer_height <= total_height * 0.8
        ):
            # Update stored height
            self.visualizer_height = visualizer_height
            # Save to config
            self.app.config.set("visualizer_height", str(visualizer_height))

            # Visualizer height is managed by GTK Box layout via vexpand

    # --- Zoom popover ---

    def _format_zoom_value(self, scale, value):
        """Format zoom slider value to show actual zoom level."""
        # Convert linear slider value (0-100) to logarithmic zoom (1-100)
        # Using formula: zoom = 10^(value/50) where value 0→1x, 50→10x, 100→100x
        zoom = math.pow(10, value / 50.0)
        return f"{zoom:.1f}x"

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

        # Update the zoom value label
        if hasattr(self, "zoom_value_label"):
            self.zoom_value_label.set_text(f"{zoom_level:.1f}x")

        if hasattr(self.visualizer, "set_zoom_level"):
            # Use the new set_zoom_level method which can use mouse position
            self.visualizer.set_zoom_level(zoom_level, use_mouse_position=True)

            # Notify zoom change (will be blocked if called from visualizer)
            if self.visualizer.zoom_changed_callback:
                self.visualizer.zoom_changed_callback(self.visualizer.zoom_level)


    def _on_zoom_btn_clicked(self, button):
        """Open the zoom popover."""
        self._close_all_bar_popovers(except_name="zoom")
        self.zoom_popover.popup()








    # --- Volume popover ---

    def _close_all_bar_popovers(self, except_name=None):
        for name in ("volume", "speed", "zoom"):
            if name != except_name and hasattr(self, name + "_popover"):
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
        if hasattr(self, "zoom_scale"):
            # Convert zoom level back to slider value
            slider_value = self._zoom_to_slider(zoom_level)
            # Temporarily block signal to avoid feedback loop
            self.zoom_scale.handler_block_by_func(self._on_zoom_scale_changed)
            self.zoom_scale.set_value(slider_value)
            self.zoom_scale.handler_unblock_by_func(self._on_zoom_scale_changed)

        # Update the zoom value label
        if hasattr(self, "zoom_value_label"):
            self.zoom_value_label.set_text(f"{zoom_level:.1f}x")

        # Sync seekbar viewport
        self.seekbar.set_zoom_viewport(
            self.visualizer.zoom_level, self.visualizer.viewport_offset
        )

    def _on_visualizer_viewport_changed(self):
        """Sync seekbar when the visualizer viewport pans without zoom change."""
        self.seekbar.set_zoom_viewport(
            self.visualizer.zoom_level, self.visualizer.viewport_offset
        )
