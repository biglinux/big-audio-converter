"""The seek bar under the waveform."""

import gettext
import math

import cairo
import gi

from app.ui.cairo_text import INK, rounded_rect, show_text, text_extents
from app.ui.time_formatter import format_time_display, format_time_short

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk

_ = gettext.gettext


class SeekBar(Gtk.DrawingArea):
    """The file's whole length under the waveform: position, the zoomed window
    and hover times. It also writes the elapsed and total time beside it."""

    HEIGHT = 30

    def __init__(self, elapsed, total):
        super().__init__()
        self._elapsed = elapsed
        self._total = total
        self.add_css_class("seekbar")
        self.set_content_height(self.HEIGHT)
        self.set_draw_func(lambda area, *args: area._draw(area, *args))

        # Accessibility
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Audio position seekbar")],
        )

        self.set_focusable(True)
        self.set_accessible_role(Gtk.AccessibleRole.SLIDER)
        keys = Gtk.EventControllerKey()
        keys.connect(
            "key-pressed",
            lambda controller, *args: controller.get_widget()._on_key_pressed(
                controller, *args
            ),
        )
        self.add_controller(keys)
        # FOCUS_VISIBLE comes and goes with keyboard use, not only with focus.
        self.connect("state-flags-changed", lambda widget, _old: widget.queue_draw())
        self.duration = 0.0
        self._position = 0.0
        self._zoom_level = 1.0
        self._viewport_offset = 0.0
        self._seek_callback = None
        self._is_dragging = False
        self._hover_x = -1  # -1 means no hover
        self._hover_time = 0.0
        self._waveform_hover_time = None  # hover time from waveform

        # Click handler for seeking
        click = Gtk.GestureClick()
        click.connect(
            "pressed",
            lambda controller, *args: controller.get_widget()._on_pressed(
                controller, *args
            ),
        )
        click.connect(
            "released",
            lambda controller, *args: controller.get_widget()._on_released(
                controller, *args
            ),
        )
        self.add_controller(click)

        # Drag handler for scrubbing
        drag = Gtk.GestureDrag()
        drag.connect(
            "drag-update",
            lambda controller, *args: controller.get_widget()._on_drag_update(
                controller, *args
            ),
        )
        self.add_controller(drag)

        # Motion handler for hover preview
        motion = Gtk.EventControllerMotion()
        motion.connect(
            "motion",
            lambda controller, *args: controller.get_widget()._on_motion(
                controller, *args
            ),
        )
        motion.connect(
            "leave",
            lambda controller, *args: controller.get_widget()._on_leave(
                controller, *args
            ),
        )
        self.add_controller(motion)

    def connect_seek_handler(self, callback):
        self._seek_callback = callback

    def set_duration(self, duration):
        self.duration = max(0.0, duration)
        self.update_property(
            [Gtk.AccessibleProperty.VALUE_MIN, Gtk.AccessibleProperty.VALUE_MAX],
            [0.0, self.duration],
        )
        self._show_time()
        self.queue_draw()

    def set_position(self, position):
        self._position = max(0.0, position)
        self.update_property(
            [Gtk.AccessibleProperty.VALUE_NOW, Gtk.AccessibleProperty.VALUE_TEXT],
            [self._position, format_time_short(self._position)],
        )
        self._show_time()
        self.queue_draw()

    def _show_time(self):
        # Whole seconds: the labels change once a second while playing.
        for label, seconds in (
            (self._elapsed, self._position),
            (self._total, self.duration),
        ):
            text = format_time_display(seconds)
            if label.get_label() != text:
                label.set_label(text)

    def set_zoom_viewport(self, zoom_level, viewport_offset):
        self._zoom_level = max(1.0, zoom_level)
        self._viewport_offset = max(0.0, min(1.0, viewport_offset))
        self.queue_draw()

    def set_waveform_hover_time(self, time):
        """Set hover time from waveform (None to clear)."""
        self._waveform_hover_time = time
        self.queue_draw()

    def _x_to_position(self, x):
        """Convert x coordinate to time position (track-aware)."""
        track_x = getattr(self, "_track_x", 6)
        track_w = getattr(self, "_track_w", max(1, self.get_width() - 12))
        if track_w <= 0 or self.duration <= 0:
            return 0.0
        frac = max(0.0, min(1.0, (x - track_x) / track_w))
        return frac * self.duration

    def _on_pressed(self, gesture, n_press, x, y):
        if self.duration <= 0:
            return
        self.grab_focus()
        self._is_dragging = True
        pos = self._x_to_position(x)
        if self._seek_callback:
            self._seek_callback(pos, False)

    def _on_released(self, gesture, n_press, x, y):
        self._is_dragging = False
        self.queue_draw()

    def _on_drag_update(self, gesture, offset_x, offset_y):
        if not self._is_dragging or self.duration <= 0:
            return
        success, start_x, _ = gesture.get_start_point()
        if success:
            pos = self._x_to_position(start_x + offset_x)
            if self._seek_callback:
                self._seek_callback(pos, False)

    def _on_motion(self, controller, x, y):
        self._hover_x = x
        if self.duration > 0:
            self._hover_time = self._x_to_position(x)
        self.queue_draw()

    def _on_leave(self, controller):
        self._hover_x = -1
        self.queue_draw()

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if self.duration <= 0 or not self._seek_callback:
            return False
        step = 0.1 if state & Gdk.ModifierType.SHIFT_MASK else 5.0
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Down):
            target = max(0.0, self._position - step)
        elif keyval in (Gdk.KEY_Right, Gdk.KEY_Up):
            target = min(self.duration, self._position + step)
        elif keyval == Gdk.KEY_Home:
            target = 0.0
        elif keyval == Gdk.KEY_End:
            target = self.duration
        else:
            return False
        self._seek_callback(target, False)
        return True

    def _draw(self, area, cr, width, height):
        color = self.get_color()
        accent = (color.red, color.green, color.blue)
        # The ring marks keyboard focus; a click to seek is not one.
        if self.get_state_flags() & Gtk.StateFlags.FOCUS_VISIBLE:
            cr.set_source_rgba(*INK, 0.8)
            cr.set_line_width(2)
            rounded_rect(cr, 1, 1, width - 2, height - 2, 6)
            cr.stroke()

        # Room for the thumb at either end; the caller's margins line the
        # track up with the waveform.
        pad = 6
        track_h = 4
        track_w = width - 2 * pad
        track_cy = height - 9
        track_y = track_cy - track_h / 2
        self._track_x = pad
        self._track_w = track_w
        if track_w <= 0:
            return
        progress = (
            max(0.0, min(1.0, self._position / self.duration))
            if self.duration > 0
            else 0.0
        )

        if self._zoom_level > 1.0:
            window = 1.0 / self._zoom_level
            window_x = pad + self._viewport_offset * (1.0 - window) * track_w
            cr.set_source_rgba(*INK, 0.10)
            rounded_rect(cr, window_x, track_cy - 6, window * track_w, 12, 6)
            cr.fill()

        cr.set_source_rgba(*INK, 0.16)
        rounded_rect(cr, pad, track_y, track_w, track_h, track_h / 2)
        cr.fill()
        if progress > 0:
            cr.set_source_rgba(*accent, 1.0)
            rounded_rect(cr, pad, track_y, progress * track_w, track_h, track_h / 2)
            cr.fill()

        hover_time = None
        if pad <= self._hover_x <= width - pad and not self._is_dragging:
            hover_time = self._hover_time
        elif self._waveform_hover_time is not None and self.duration > 0:
            hover_time = self._waveform_hover_time
        if hover_time is not None and self.duration > 0:
            hx = pad + max(0.0, min(1.0, hover_time / self.duration)) * track_w
            cr.set_source_rgba(*INK, 0.35)
            cr.set_line_width(1)
            cr.move_to(int(hx) + 0.5, track_cy - 5)
            cr.line_to(int(hx) + 0.5, track_cy + 5)
            cr.stroke()
            cr.select_font_face(
                "Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL
            )
            cr.set_font_size(10)
            text = format_time_short(hover_time)
            ext = text_extents(cr, text)
            chip_x = max(0, min(width - ext.width - 8, hx - ext.width / 2 - 4))
            cr.set_source_rgba(0, 0, 0, 0.75)
            rounded_rect(cr, chip_x, 1, ext.width + 8, ext.height + 2, 4)
            cr.fill()
            cr.set_source_rgba(*INK, 0.9)
            cr.move_to(chip_x + 4, 1 + ext.height)
            show_text(cr, text)

        thumb_x = pad + progress * track_w
        large = self._is_dragging or self._hover_x >= 0
        cr.set_source_rgba(*INK, 0.95)
        cr.arc(thumb_x, track_cy, 6 if large else 4, 0, 2 * math.pi)
        cr.fill()
        if large:
            cr.set_source_rgba(*accent, 1.0)
            cr.arc(thumb_x, track_cy, 2.5, 0, 2 * math.pi)
            cr.fill()
