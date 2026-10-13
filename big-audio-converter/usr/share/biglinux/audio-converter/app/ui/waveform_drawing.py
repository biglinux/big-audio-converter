"""The waveform view's drawing: peaks, playhead, time ruler, hint and scrollbar."""

import gettext
import math

import cairo
import numpy as np

from app.ui.cairo_text import INK, rounded_rect, show_text, text_extents
from app.ui.time_formatter import format_time_ruler

_ = gettext.gettext


class WaveformDrawingMixin:
    """Drawing of AudioVisualizer, which supplies the waveform, zoom and marks."""

    def _draw_timeline_ruler(self, cr, width, height):
        """Draw subtle integrated timeline ticks at the bottom of the waveform."""
        visible_duration = self.visible_duration
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

        # Position ticks at the bottom edge (above scrollbar if zoomed)
        bottom_margin = 4
        if self.zoom_level > 1.0:
            bottom_margin = self.scrollbar_height + self.scrollbar_margin * 2 + 4
        tick_base_y = height - bottom_margin

        target_spacing = 80
        marks_count = max(1, int(width / target_spacing))
        time_per_mark = visible_duration / marks_count

        nice_intervals = [
            0.1,
            0.2,
            0.5,
            1,
            2,
            5,
            10,
            15,
            30,
            60,
            120,
            300,
            600,
            1800,
            3600,
        ]
        time_per_mark = min(nice_intervals, key=lambda x: abs(x - time_per_mark))

        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(9)
        cr.set_line_width(1)

        # An integer index, not an accumulated float: 10 × 0.2 s falls short
        # of 2 s and would lose that label.
        for i in range(
            math.ceil(start_time / time_per_mark),
            math.floor(end_time / time_per_mark) + 1,
        ):
            t = i * time_per_mark
            x = ((t - start_time) / visible_duration) * width
            is_major = i % 5 == 0
            tick_h = 8 if is_major else 4

            cr.set_source_rgba(*INK, 0.30)
            cr.move_to(x, tick_base_y)
            cr.line_to(x, tick_base_y - tick_h)
            cr.stroke()

            if is_major:
                label = format_time_ruler(t, time_per_mark * 5)
                ext = text_extents(cr, label)
                lx = max(2, min(width - ext.width - 2, x - ext.width / 2))
                cr.set_source_rgba(*INK, 0.50)
                cr.move_to(lx, tick_base_y - tick_h - 3)
                show_text(cr, label)

    def _draw_hint(self, cr):
        """How to use the waveform, until the first segment exists."""
        text = _("Click to listen from there · Drag to mark a segment")
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(12)
        extents = text_extents(cr, text)
        # The waveform's peaks reach the top; the text keeps its own backdrop.
        cr.set_source_rgba(0, 0, 0, 0.6)
        rounded_rect(cr, 6, 6, extents.width + 12, 20, 4)
        cr.fill()
        cr.set_source_rgba(*INK, 0.85)
        cr.move_to(12, 20)
        show_text(cr, text)

    def _draw_hover_time(self, cr, width, height):
        """Draw vertical guide line at hover position."""
        if self.hover_time is None or self.hover_x is None or self.duration <= 0:
            return

        # Vertical guide line only (time shown on seekbar)
        cr.set_source_rgba(*INK, 0.3)
        cr.set_line_width(1)
        cr.move_to(self.hover_x, 0)
        cr.line_to(self.hover_x, height)
        cr.stroke()

    def _draw_scrollbar(self, cr, width, height):
        """Draw a simulated horizontal scrollbar for zoomed view."""
        scrollbar_y = height - self.scrollbar_height  # No bottom margin
        scrollbar_width = width - (2 * self.scrollbar_margin)
        scrollbar_x = self.scrollbar_margin

        cr.set_source_rgba(*INK, 0.06)
        cr.rectangle(scrollbar_x, scrollbar_y, scrollbar_width, self.scrollbar_height)
        cr.fill()

        # Thumb width represents the visible portion (1/zoom_level)
        visible_fraction = 1.0 / self.zoom_level
        thumb_width = max(30, scrollbar_width * visible_fraction)  # Minimum 30px

        # Thumb position based on viewport_offset
        max_offset = 1.0 - visible_fraction
        if max_offset > 0:
            thumb_fraction = self.viewport_offset / max_offset
        else:
            thumb_fraction = 0

        max_thumb_x = scrollbar_width - thumb_width
        thumb_x = scrollbar_x + (thumb_fraction * max_thumb_x)

        active = self.hovering_scrollbar or self.is_dragging_scrollbar
        cr.set_source_rgba(*INK, 0.55 if active else 0.35)
        rounded_rect(
            cr,
            thumb_x,
            scrollbar_y + 2,
            thumb_width,
            self.scrollbar_height - 4,
            (self.scrollbar_height - 4) / 2,
        )
        cr.fill()

    def _draw_loading_indicator(self, cr, width, height):
        """A static status avoids redraw polling and respects reduced motion."""
        cr.set_font_size(14)
        cr.set_source_rgba(*INK, 0.8)
        extents = text_extents(cr, self.loading_message)
        cr.move_to(max(12, (width - extents.width) / 2), height / 2)
        show_text(cr, self.loading_message)

    def _render_waveform_cache(self, width, height, visible_waveform, viewport_key):
        """Render the waveform once, as an alpha mask the draw paints."""
        y_center = height / 2
        y_scale = height * 0.4 / self.waveform_peak
        bars = int(width)
        samples_per_bar = len(visible_waveform) / bars if bars else 0
        # The loudest peak under each pixel column.
        starts = np.minimum(
            (np.arange(bars) * samples_per_bar).astype(int),
            len(visible_waveform) - 1,
        )
        peaks = np.maximum.reduceat(visible_waveform, starts) if bars else []

        surface = cairo.ImageSurface(cairo.FORMAT_A8, int(width), int(height))
        surface_cr = cairo.Context(surface)
        for bar, peak in enumerate(peaks):
            # Levels hold absolute peaks; draw them mirrored, never thinner than
            # a hairline so silence still reads as a line.
            half = max(0.5, peak * y_scale)
            surface_cr.rectangle(bar, y_center - half, 1, 2 * half)
        surface_cr.fill()

        self.cached_waveform_surface = surface
        self.cached_viewport_key = viewport_key

    def _draw_waveform(self, cr):
        """One color, so a marked part's lighter block stands out on it."""
        color = self.get_color()
        cr.set_source_rgb(color.red, color.green, color.blue)
        cr.mask_surface(self.cached_waveform_surface, 0, 0)

    def _draw_position_marker(
        self, cr, width, height, start_time, end_time, visible_duration
    ):
        """The playhead: a white line with a soft halo."""
        if self.duration > 0 and start_time <= self.position <= end_time:
            position_x = ((self.position - start_time) / visible_duration) * width
            cr.set_source_rgba(*INK, 0.10)
            cr.rectangle(position_x - 3, 0, 6, height)
            cr.fill()
            cr.set_source_rgba(*INK, 0.92)
            cr.set_line_width(2)
            cr.move_to(position_x, 0)
            cr.line_to(position_x, height)
            cr.stroke()

    def _draw_no_waveform_message(
        self, cr, width, height, start_time, end_time, visible_duration
    ):
        """Draw placeholder text when no waveform data is available."""
        cr.set_source_rgba(*INK, 0.7)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(16)

        if self.waveform_error:
            text = _("Waveform could not be generated")
            subtitle = _("Numeric segment editing is still available.")
        elif self.duration > 0:
            text = _("Generating waveform...")
            subtitle = None
        else:
            text = _("No audio loaded")
            subtitle = None

        extents = text_extents(cr, text)
        text_y = (
            (height + extents.height) / 2
            if subtitle is None
            else (height - extents.height) / 2 - 5
        )
        cr.move_to((width - extents.width) / 2, text_y)
        show_text(cr, text)

        if subtitle:
            cr.set_font_size(12)
            cr.set_source_rgba(*INK, 0.5)
            sub_ext = text_extents(cr, subtitle)
            cr.move_to((width - sub_ext.width) / 2, (height + sub_ext.height) / 2 + 10)
            show_text(cr, subtitle)

        # Position marker even without waveform
        if self.duration > 0 and self.position > 0:
            self._draw_position_marker(
                cr, width, height, start_time, end_time, visible_duration
            )

    def on_draw(self, area, cr, width, height):
        """Draw the waveform over the panel's own background."""
        if self.is_loading:
            self._draw_loading_indicator(cr, width, height)
            return

        visible_duration = self.visible_duration if self.duration > 0 else 0
        start_time = self.viewport_offset * self.duration if self.duration > 0 else 0
        end_time = start_time + visible_duration

        if self.waveform_data is None:
            self._draw_no_waveform_message(
                cr, width, height, start_time, end_time, visible_duration
            )
        elif len(self.waveform_data["peaks"]):
            peaks = self.waveform_data["peaks"]
            rate = self.waveform_data["rate"]
            start_sample = max(0, min(len(peaks) - 1, int(start_time * rate)))
            end_sample = max(start_sample + 1, min(len(peaks), int(end_time * rate)))
            viewport_key = (start_sample, end_sample, int(width), int(height))
            if (
                self.cached_viewport_key != viewport_key
                or self.cached_waveform_surface is None
            ):
                self._render_waveform_cache(
                    width, height, peaks[start_sample:end_sample], viewport_key
                )
            cr.set_source_rgba(*INK, 0.06)
            cr.set_line_width(1)
            cr.move_to(0, int(height / 2) + 0.5)
            cr.line_to(width, int(height / 2) + 0.5)
            cr.stroke()
            self._draw_waveform(cr)
            self._draw_position_marker(
                cr, width, height, start_time, end_time, visible_duration
            )

        if self.duration > 0:
            self._draw_timeline_ruler(cr, width, height)

        # Draw markers after waveform
        self._draw_markers(cr, width, height)
        if self.show_hint and self.waveform_data is not None and not self.marker_pairs:
            self._draw_hint(cr)

        self._draw_hover_time(cr, width, height)

        if self.zoom_level > 1.0:
            self._draw_scrollbar(cr, width, height)
