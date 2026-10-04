# app/ui/controls_bar_mixin.py

"""The playback bar's sliders and the waveform viewport they follow.

Volume, speed, voice filter and zoom open their slider as the pointer reaches
them, as in the video player, and close a moment after it leaves.
"""

import logging
import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk

from app.utils.main_loop import weak_callback

logger = logging.getLogger(__name__)

# Long enough to cross the gap between a control and its slider.
CLOSE_DELAY_MS = 800


class ControlsBarMixin:
    """The bar's hover sliders and the visualizer viewport sync."""

    def _bar_control(
        self, name, icon_name, title, description, scale, value_label, step, extra=()
    ):
        """A button and readout whose slider opens on hover.

        The heading says what the slider changes, in place of a tooltip.
        Clicking opens it for the keyboard: focused, and closed by Escape or a
        click elsewhere. Returns the hover area and the button.
        """
        if not hasattr(self, "_bar_popovers"):
            self._bar_popovers, self._bar_pointers, self._bar_scales = {}, {}, {}
            self._bar_closing, self._bar_keyboard = {}, {}
        button = Gtk.Button(
            icon_name=icon_name,
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular"],
        )
        button.update_property([Gtk.AccessibleProperty.LABEL], [title])
        box = Gtk.Box(spacing=2)
        box.append(button)
        box.append(value_label)

        scale.set_inverted(True)
        scale.set_draw_value(False)
        scale.set_size_request(-1, 200)
        scale.set_halign(Gtk.Align.CENTER)
        scale.update_property([Gtk.AccessibleProperty.LABEL], [title])
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        content.append(Gtk.Label(label=title, css_classes=["heading"]))
        if description:
            content.append(
                Gtk.Label(
                    label=description,
                    wrap=True,
                    max_width_chars=16,
                    justify=Gtk.Justification.CENTER,
                    css_classes=["caption", "dim-label"],
                )
            )
        content.append(scale)
        for widget in extra:
            content.append(widget)
        popover = Gtk.Popover(
            child=content,
            position=Gtk.PositionType.TOP,
            autohide=False,
            css_classes=["bar-slider"],
        )
        popover.update_property([Gtk.AccessibleProperty.LABEL], [title])
        popover.set_parent(box)
        self._bar_popovers[name] = popover
        self._bar_scales[name] = scale
        self._bar_keyboard[name] = False

        hover = Gtk.EventControllerMotion()
        hover.connect("enter", weak_callback(self._on_bar_enter), name)
        hover.connect("leave", weak_callback(self._on_bar_leave), name)
        box.add_controller(hover)
        pointer = Gtk.EventControllerMotion()
        pointer.connect("enter", weak_callback(self._on_bar_slider_enter), name)
        pointer.connect("leave", weak_callback(self._on_bar_leave), name)
        popover.add_controller(pointer)
        self._bar_pointers[name] = pointer
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", weak_callback(self._on_bar_focus_enter), name)
        focus.connect("leave", weak_callback(self._on_bar_focus_leave), name)
        popover.add_controller(focus)
        popover.connect("closed", weak_callback(self._on_bar_closed), name)
        button.connect("clicked", weak_callback(self._on_bar_clicked), name)

        # The wheel over the control moves its slider without opening it.
        wheel = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL
            | Gtk.EventControllerScrollFlags.DISCRETE
        )
        wheel.connect("scroll", weak_callback(self._on_bar_scroll), scale, step)
        box.add_controller(wheel)
        return box, button

    def _open_bar_slider(self, name):
        self._cancel_bar_close(name)
        for other, popover in self._bar_popovers.items():
            if other != name:
                popover.popdown()
        self._bar_popovers[name].popup()

    def _cancel_bar_close(self, name):
        source = self._bar_closing.pop(name, None)
        if source is not None:
            self._sources.cancel(source)

    def _on_bar_enter(self, _controller, _x, _y, name):
        self._open_bar_slider(name)

    def _on_bar_slider_enter(self, _controller, _x, _y, name):
        self._cancel_bar_close(name)

    def _on_bar_leave(self, _controller, name):
        popover = self._bar_popovers[name]
        if self._bar_keyboard[name] or popover.get_autohide():
            return
        self._cancel_bar_close(name)
        self._bar_closing[name] = self._sources.later(
            CLOSE_DELAY_MS, self._close_bar_slider, name
        )

    def _close_bar_slider(self, name):
        self._bar_closing.pop(name, None)
        if not self._bar_keyboard[name]:
            self._bar_popovers[name].popdown()

    def _on_bar_focus_enter(self, _controller, name):
        # Focus brought by a click or drag on the slider is the pointer's.
        self._bar_keyboard[name] = not self._bar_pointers[name].contains_pointer()
        self._cancel_bar_close(name)

    def _on_bar_focus_leave(self, controller, name):
        self._bar_keyboard[name] = False
        self._on_bar_leave(controller, name)

    def _on_bar_clicked(self, _button, name):
        popover = self._bar_popovers[name]
        popover.popdown()
        popover.set_autohide(True)
        self._open_bar_slider(name)
        self._bar_scales[name].grab_focus()

    def _on_bar_closed(self, popover, name):
        self._cancel_bar_close(name)
        self._bar_keyboard[name] = False
        popover.set_autohide(False)

    def _on_bar_scroll(self, _controller, _dx, dy, scale, step):
        if dy == 0 or not scale.get_sensitive():
            return False
        scale.set_value(scale.get_value() - math.copysign(step, dy))
        return True

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

    def _on_volume_scale_changed(self, scale):
        self._set_processing_volume(self._slider_to_volume(scale.get_value()))

    # --- Speed popover ---

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
