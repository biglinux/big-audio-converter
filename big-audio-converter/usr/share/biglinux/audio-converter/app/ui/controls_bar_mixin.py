# app/ui/controls_bar_mixin.py

"""
Controls Bar Mixin for MainWindow.

Extracts all bottom controls bar related methods (zoom popover, volume popover,
speed popover, hover controllers, visualizer height/viewport sync) from
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
from gi.repository import GLib

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

        # Schedule auto-close of popover after user finishes adjusting
        if hasattr(self, "_zoom_close_timer") and self._zoom_close_timer:
            GLib.source_remove(self._zoom_close_timer)
        self._zoom_close_timer = GLib.timeout_add(1500, self._auto_close_zoom_popover)

    def _on_zoom_btn_clicked(self, button):
        """Open the zoom popover."""
        self._close_all_bar_popovers(except_name="zoom")
        self.zoom_popover.popup()

    def _auto_close_zoom_popover(self):
        """Auto-close the zoom popover after inactivity."""
        self._zoom_close_timer = None
        if hasattr(self, "zoom_popover"):
            self.zoom_popover.popdown()
        return False  # Don't repeat

    def _cancel_zoom_hover_close(self):
        """Cancel pending hover close timer."""
        if hasattr(self, "_zoom_hover_close_timer") and self._zoom_hover_close_timer:
            GLib.source_remove(self._zoom_hover_close_timer)
            self._zoom_hover_close_timer = None

    def _on_zoom_btn_hover_enter(self, controller, x, y):
        """Show zoom popover on mouse hover."""
        self._cancel_zoom_hover_close()
        if not self.zoom_popover.is_visible():
            self._close_all_bar_popovers(except_name="zoom")
            self.zoom_popover.popup()

    def _on_zoom_btn_hover_leave(self, controller):
        """Schedule close when mouse leaves zoom button."""
        self._cancel_zoom_hover_close()
        self._zoom_hover_close_timer = GLib.timeout_add(
            1000, self._hover_close_zoom_popover
        )

    def _on_zoom_popover_hover_enter(self, controller, x, y):
        """Cancel close when mouse enters the popover."""
        self._cancel_zoom_hover_close()
        # Also cancel the scale-change auto-close timer
        if hasattr(self, "_zoom_close_timer") and self._zoom_close_timer:
            GLib.source_remove(self._zoom_close_timer)
            self._zoom_close_timer = None

    def _on_zoom_popover_hover_leave(self, controller):
        """Schedule close when mouse leaves the popover."""
        self._cancel_zoom_hover_close()
        self._zoom_hover_close_timer = GLib.timeout_add(
            1000, self._hover_close_zoom_popover
        )

    def _hover_close_zoom_popover(self):
        """Close the zoom popover after hover inactivity."""
        self._zoom_hover_close_timer = None
        if hasattr(self, "zoom_popover"):
            self.zoom_popover.popdown()
        return False

    # --- Volume popover ---

    def _close_all_bar_popovers(self, except_name=None):
        """Close all bottom bar popovers except the specified one."""
        popovers = {
            "volume": ("volume_popover", "_volume_hover_close_timer"),
            "speed": ("speed_popover", "_speed_hover_close_timer"),
            "zoom": ("zoom_popover", "_zoom_hover_close_timer"),
        }
        for name, (pop_attr, timer_attr) in popovers.items():
            if name == except_name:
                continue
            timer = getattr(self, timer_attr, None)
            if timer:
                GLib.source_remove(timer)
                setattr(self, timer_attr, None)
            popover = getattr(self, pop_attr, None)
            if popover and popover.is_visible():
                popover.popdown()

    def _cancel_volume_hover_close(self):
        if (
            hasattr(self, "_volume_hover_close_timer")
            and self._volume_hover_close_timer
        ):
            GLib.source_remove(self._volume_hover_close_timer)
            self._volume_hover_close_timer = None

    def _on_volume_btn_hover_enter(self, controller, x, y):
        if not self.volume_btn.get_sensitive():
            return
        self._cancel_volume_hover_close()
        if not self.volume_popover.is_visible():
            self._close_all_bar_popovers(except_name="volume")
            self.volume_popover.popup()

    def _on_volume_btn_hover_leave(self, controller):
        self._cancel_volume_hover_close()
        self._volume_hover_close_timer = GLib.timeout_add(
            1000, self._hover_close_volume_popover
        )

    def _on_volume_popover_hover_enter(self, controller, x, y):
        self._cancel_volume_hover_close()

    def _on_volume_popover_hover_leave(self, controller):
        self._cancel_volume_hover_close()
        self._volume_hover_close_timer = GLib.timeout_add(
            1000, self._hover_close_volume_popover
        )

    def _hover_close_volume_popover(self):
        self._volume_hover_close_timer = None
        if hasattr(self, "volume_popover"):
            self.volume_popover.popdown()
        return False

    def _on_volume_btn_clicked(self, button):
        self._close_all_bar_popovers(except_name="volume")
        self.volume_popover.popup()

    def _on_volume_scale_changed(self, scale):
        """Handle volume scale changes from bottom bar."""
        slider_value = scale.get_value()
        volume = self._slider_to_volume(slider_value)
        # Update label
        self.volume_value_label.set_text(f"{int(volume)}")
        # Update icon based on level
        if volume == 0:
            self.volume_btn.set_icon_name("audio-volume-muted-symbolic")
        elif volume <= 33:
            self.volume_btn.set_icon_name("audio-volume-low-symbolic")
        elif volume <= 100:
            self.volume_btn.set_icon_name("audio-volume-medium-symbolic")
        else:
            self.volume_btn.set_icon_name("audio-volume-high-symbolic")
        # Sync with hidden spin row
        self.volume_spin.handler_block_by_func(self._on_volume_spin_changed)
        self.volume_spin.set_value(volume)
        self.volume_spin.handler_unblock_by_func(self._on_volume_spin_changed)
        # Apply volume
        player_volume = volume / 100.0
        if player_volume > 1.0:
            player_volume = 1.0 + (player_volume - 1.0) * 0.5
        self.player.set_volume(player_volume)
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("conversion_volume", str(volume))

    # --- Speed popover ---

    def _cancel_speed_hover_close(self):
        if hasattr(self, "_speed_hover_close_timer") and self._speed_hover_close_timer:
            GLib.source_remove(self._speed_hover_close_timer)
            self._speed_hover_close_timer = None

    def _on_speed_btn_hover_enter(self, controller, x, y):
        if not self.speed_btn.get_sensitive():
            return
        self._cancel_speed_hover_close()
        if not self.speed_popover.is_visible():
            self._close_all_bar_popovers(except_name="speed")
            self.speed_popover.popup()

    def _on_speed_btn_hover_leave(self, controller):
        self._cancel_speed_hover_close()
        self._speed_hover_close_timer = GLib.timeout_add(
            1000, self._hover_close_speed_popover
        )

    def _on_speed_popover_hover_enter(self, controller, x, y):
        self._cancel_speed_hover_close()

    def _on_speed_popover_hover_leave(self, controller):
        self._cancel_speed_hover_close()
        self._speed_hover_close_timer = GLib.timeout_add(
            1000, self._hover_close_speed_popover
        )

    def _hover_close_speed_popover(self):
        self._speed_hover_close_timer = None
        if hasattr(self, "speed_popover"):
            self.speed_popover.popdown()
        return False

    def _on_speed_btn_clicked(self, button):
        self._close_all_bar_popovers(except_name="speed")
        self.speed_popover.popup()

    def _on_speed_scale_changed(self, scale):
        """Handle speed scale changes from bottom bar."""
        slider_value = scale.get_value()
        speed = self._slider_to_speed(slider_value)
        # Clamp to valid range
        speed = max(0.10, min(5.0, speed))
        # Update label
        self.speed_value_label.set_text(f"{speed:.2f}x")
        # Sync with hidden spin row
        self.speed_spin.handler_block_by_func(self._on_speed_spin_changed)
        self.speed_spin.set_value(speed)
        self.speed_spin.handler_unblock_by_func(self._on_speed_spin_changed)
        # Apply speed
        self.player.set_playback_speed(speed)
        self.player.set_pitch_correction(True)
        if hasattr(self.app, "config") and self.app.config:
            self.app.config.set("conversion_speed", str(speed))

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
