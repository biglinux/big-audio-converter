"""Marked parts on the waveform: their state, drawing and delete dialogs."""

import gettext
import logging
import math

import cairo
from gi.repository import Adw

from app.audio.media import Segment
from app.audio.process import MediaError
from app.ui.cairo_text import CUT, KEEP, rounded_rect, show_text, text_extents
from app.ui.time_formatter import format_time_short

_ = gettext.gettext

logger = logging.getLogger(__name__)


class WaveformSegmentsMixin:
    """Marks of AudioVisualizer, which supplies duration, zoom and viewport."""

    @property
    def visible_duration(self):
        """Seconds of audio the view spans at the current zoom."""
        return self.duration / self.zoom_level

    @property
    def max_scroll_offset(self):
        """The largest viewport offset, as a fraction of the duration."""
        return 1.0 - 1.0 / self.zoom_level

    def _init_marker_state(self):
        self._marker_dialog = None
        self.marker_pairs = []  # List of {start, stop} pairs
        self.highlighted_pair = -1  # Index of pair being highlighted

        # Marker drag state
        self.is_dragging_marker = False
        self.dragging_marker_type = (
            None  # "start" or "stop" or "segment" for whole segment
        )
        self.dragging_pair_index = -1
        self.drag_start_pos = None  # Store starting position for more accurate movement

        # Potential drag tracking - helps distinguish between click and drag
        self.potential_drag_segment = None
        self.drag_threshold = 3  # Pixels of movement required to initiate drag
        # Time under a press that may still become a click or a new segment.
        self.pending_press = None
        self.new_segment_anchor = None
        # Segments the export cuts out are drawn red instead of blue.
        self.remove_marked = False

        # Callbacks
        self.marker_updated_callback = None
        self.marker_drag_callback = None  # Callback for marker drag state changes

        self.hovered_segment_index = -1

        self.hovering_delete_button = False
        self.delete_button_bounds = None  # Store (x, y, radius) for hit detection

    def _is_over_delete_button(self, x, y):
        """Check if the mouse position is over the delete button."""
        if self.delete_button_bounds is None:
            return False

        button_x, button_y, button_radius = self.delete_button_bounds

        distance = ((x - button_x) ** 2 + (y - button_y) ** 2) ** 0.5

        # Three pixels of slack make the small button easier to hit.
        return distance <= button_radius + 3

    def _find_segment_at_position(self, x, y):
        """Find if the given x,y position is on an existing segment."""
        width = self.get_width()

        visible_duration = self.visible_duration
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

        for i, pair in enumerate(self.marker_pairs):
            if pair["start"] is not None and pair["stop"] is not None:
                if pair["stop"] < start_time or pair["start"] > end_time:
                    continue

                x_start = ((pair["start"] - start_time) / visible_duration) * width
                x_stop = ((pair["stop"] - start_time) / visible_duration) * width

                if x_start - 5 <= x <= x_stop + 5:
                    return i

        return -1

    def _find_marker_at_position(self, x, y):
        """Find if the given x,y position is on a marker edge."""
        if self.duration <= 0:
            return None

        width = self.get_width()
        height = self.get_height()
        marker_hit_tolerance = 5  # Pixels of tolerance for marker hit detection

        if y < 0 or y > height:
            return None

        visible_duration = self.visible_duration
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

        for i, pair in enumerate(self.marker_pairs):
            if pair["start"] is not None and start_time <= pair["start"] <= end_time:
                x_start = ((pair["start"] - start_time) / visible_duration) * width
                if abs(x - x_start) <= marker_hit_tolerance:
                    return {"index": i, "type": "start"}

            if pair["stop"] is not None and start_time <= pair["stop"] <= end_time:
                x_stop = ((pair["stop"] - start_time) / visible_duration) * width
                if abs(x - x_stop) <= marker_hit_tolerance:
                    return {"index": i, "type": "stop"}

        return None

    def _find_segment_body_at_position(self, x, y):
        """Find if x,y is inside a segment body (not on edge)."""
        width = self.get_width()

        visible_duration = self.visible_duration
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

        edge_tolerance = 5  # Same as marker hit tolerance

        for i, pair in enumerate(self.marker_pairs):
            if pair["start"] is not None and pair["stop"] is not None:
                if pair["stop"] < start_time or pair["start"] > end_time:
                    continue

                x_start = ((pair["start"] - start_time) / visible_duration) * width
                x_stop = ((pair["stop"] - start_time) / visible_duration) * width

                if (x_start + edge_tolerance) <= x <= (x_stop - edge_tolerance):
                    return i  # Return segment index

        return -1

    def _prompt_delete_all_confirmation(self):
        self._close_marker_dialog()
        pairs = self.marker_pairs
        dialog = Adw.AlertDialog(
            heading=_("Delete All Segments"),
            body=_("Delete all marked segments? Source files will not be changed."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete All"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(dialog, response):
            self._marker_dialog = None
            if self.marker_pairs is pairs and response == "delete":
                self.clear_all_markers()
            self.grab_focus()

        dialog.connect("response", responded)
        self._marker_dialog = dialog
        dialog.present(self.get_root())

    def _close_marker_dialog(self):
        if self._marker_dialog is not None:
            dialog, self._marker_dialog = self._marker_dialog, None
            dialog.force_close()

    def _prompt_delete_segment(self, pair_index):
        if not 0 <= pair_index < len(self.marker_pairs):
            return
        self._close_marker_dialog()
        pairs = self.marker_pairs
        selected_pair = pairs[pair_index]
        dialog = Adw.AlertDialog(
            heading=_("Delete Segment"),
            body=_("Delete segment {number}?").format(number=pair_index + 1),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        if len(pairs) > 1:
            dialog.add_response("all", _("Delete All"))
            dialog.set_response_appearance("all", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def responded(dialog, response):
            self._marker_dialog = None
            if self.marker_pairs is not pairs:
                return
            if response == "all":
                self._prompt_delete_all_confirmation()
            elif response == "delete":
                index = next(
                    (i for i, pair in enumerate(pairs) if pair is selected_pair), None
                )
                if index is not None:
                    self.remove_marker_pair(index)
            self.grab_focus()

        dialog.connect("response", responded)
        self._marker_dialog = dialog
        dialog.present(self.get_root())

    def remove_marker_pair(self, index):
        if 0 <= index < len(self.marker_pairs):
            self.marker_pairs.pop(index)
            self.highlighted_pair = -1
            if self.marker_updated_callback:
                self.marker_updated_callback(self.get_marker_pairs())
            self.queue_draw()

    def clear_all_markers(self):
        self._close_marker_dialog()
        self.marker_pairs = []
        self.highlighted_pair = -1
        if self.marker_updated_callback:
            self.marker_updated_callback([])
        self.queue_draw()

    def connect_marker_drag_handler(self, callback):
        """Connect a handler for marker drag state changes.

        Callback will be called with True when dragging starts, False when it ends.
        """
        self.marker_drag_callback = callback

    def get_marker_pairs(self):
        """Export every complete valid segment, without hidden minimum durations."""
        result = []
        for index, pair in enumerate(self.marker_pairs, 1):
            if pair.get("start") is None or pair.get("stop") is None:
                continue  # An unfinished mouse gesture is not a committed segment.
            result.append(
                Segment.from_mapping(
                    dict(pair, segment_index=index), self.duration or None
                ).as_mapping()
            )
        return result

    def get_ordered_marker_pairs(self, order_by_number=False):
        """Get marker pairs ordered by either timeline position or segment number.

        Args:
            order_by_number: If True, order by segment number. If False, order by start time.

        Returns:
            List of ordered marker pairs.
        """
        pairs = self.get_marker_pairs()

        if not pairs:
            return []

        if order_by_number:
            # Order segments by their display number (as shown in UI)
            # Each marker pair should have a "segment_index" key that was added in get_marker_pairs
            ordered_pairs = sorted(pairs, key=lambda x: x["segment_index"])
        else:
            # Order segments by their start time (chronological order)
            ordered_pairs = sorted(pairs, key=lambda x: x["start"])

        return ordered_pairs

    def restore_markers(self, markers):
        """Restore an entire valid edit atomically, including an empty edit."""
        try:
            validated = [
                Segment.from_mapping(pair, self.duration or None).as_mapping()
                for pair in markers
            ]
        except (MediaError, TypeError, ValueError) as exc:
            logger.warning("Saved segments could not be restored: %s", exc)
            return False
        self._close_marker_dialog()
        self.marker_pairs = validated
        self.highlighted_pair = -1
        self.queue_draw()
        if self.marker_updated_callback:
            self.marker_updated_callback(self.get_marker_pairs())
        return True

    def _draw_markers(self, cr, width, height):
        """Draw all marker pairs on the waveform."""
        if not self.duration > 0:
            return

        # A part reads as a lighter block: red to cut out, blue to keep, with
        # a red start and a green end.
        start_color = (0.8, 0.2, 0.2, 0.8)
        stop_color = (0.2, 0.7, 0.3, 0.8)
        border_rgb = CUT if self.remove_marked else KEEP
        region_color = (*border_rgb, 0.30 if self.remove_marked else 0.25)
        highlight_color = (1.0, 0.8, 0.0, 0.35)

        visible_duration = self.visible_duration
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

        # Collect all marker labels for collision-aware placement
        label_items = []  # (x_center, text)

        for i, pair in enumerate(self.marker_pairs):
            if (pair["start"] is not None and pair["stop"] is not None) and (
                pair["stop"] < start_time or pair["start"] > end_time
            ):
                continue  # Skip this pair, it's not visible

            is_highlighted = i == self.highlighted_pair
            # Hundreds of found pauses would bury the waveform under edge
            # lines, times and numbers (112 ms a frame for 600): those show
            # once zoom makes the part wide enough to read them.
            detailed = (pair["stop"] - pair["start"]) / visible_duration * width >= 24
            if (
                detailed
                and pair["start"] is not None
                and start_time <= pair["start"] <= end_time
            ):
                x_start = ((pair["start"] - start_time) / visible_duration) * width
                x_start = max(0, min(width, x_start))

                # Marker line
                cr.set_source_rgba(*start_color)
                cr.set_line_width(2)
                cr.move_to(x_start, 0)
                cr.line_to(x_start, height)
                cr.stroke()

                # Collect label for deferred drawing (condensed format)
                time_str = format_time_short(pair["start"])
                label_items.append((x_start, time_str))

            if (
                detailed
                and pair["stop"] is not None
                and start_time <= pair["stop"] <= end_time
            ):
                x_stop = ((pair["stop"] - start_time) / visible_duration) * width
                x_stop = max(0, min(width, x_stop))

                # Marker line
                cr.set_source_rgba(*stop_color)
                cr.set_line_width(2)
                cr.move_to(x_stop, 0)
                cr.line_to(x_stop, height)
                cr.stroke()

                # Collect label for deferred drawing (condensed format)
                time_str = format_time_short(pair["stop"])
                label_items.append((x_stop, time_str))

            if pair["start"] is not None and pair["stop"] is not None:
                if pair["start"] <= end_time and pair["stop"] >= start_time:
                    # Clamp to visible range
                    visible_start = max(pair["start"], start_time)
                    visible_stop = min(pair["stop"], end_time)

                    x_start = ((visible_start - start_time) / visible_duration) * width
                    x_stop = ((visible_stop - start_time) / visible_duration) * width

                x_start = max(0, min(width, x_start))
                x_stop = max(0, min(width, x_stop))
                if x_start > x_stop:
                    x_start, x_stop = x_stop, x_start

                # Region rectangle - use highlight color if highlighted
                if is_highlighted:
                    cr.set_source_rgba(*highlight_color)
                else:
                    cr.set_source_rgba(*region_color)
                cr.rectangle(x_start, 0, max(1.5, x_stop - x_start), height)
                cr.fill()

                # Subtle top and bottom borders on segment region
                seg_w = x_stop - x_start
                if seg_w > 2:
                    border_alpha = 0.45 if is_highlighted else 0.30
                    cr.set_source_rgba(*border_rgb, border_alpha)
                    cr.set_line_width(1)
                    cr.move_to(x_start, 0.5)
                    cr.line_to(x_stop, 0.5)
                    cr.stroke()
                    cr.move_to(x_start, height - 0.5)
                    cr.line_to(x_stop, height - 0.5)
                    cr.stroke()

                # Pill badge for segment number; removed parts have no order.
                if detailed and not self.remove_marked:
                    badge_text = f"#{i + 1}"
                    cr.select_font_face(
                        "Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD
                    )
                    cr.set_font_size(11)
                    ext = text_extents(cr, badge_text)
                    badge_w = ext.width + 12
                    badge_h = 18
                    badge_x = x_start + 4
                    badge_y = height - badge_h - 28  # above ruler
                    # Badge background
                    cr.set_source_rgba(0, 0, 0, 0.55)
                    rounded_rect(cr, badge_x, badge_y, badge_w, badge_h, 4)
                    cr.fill()
                    # Badge text
                    cr.set_source_rgba(1, 1, 1, 0.90)
                    cr.move_to(badge_x + 6, badge_y + badge_h - 5)
                    show_text(cr, badge_text)

                if i == self.hovered_segment_index and self._marker_dialog is None:
                    self._draw_segment_delete_button(cr, x_start, x_stop, height, i)

        if label_items:
            self._draw_marker_labels(cr, width, label_items)

    def _draw_marker_labels(self, cr, width, label_items):
        """Draw time labels for markers with collision-aware vertical stacking."""
        cr.set_font_size(10)
        label_h = 14
        label_pad = 4
        base_y = 2

        label_items.sort(key=lambda item: item[0])

        placed = []  # (x_left, x_right, y_top)
        for x_center, text in label_items:
            text_width = text_extents(cr, text).width + 4
            x_left = x_center - text_width / 2
            x_right = x_left + text_width

            label_y = base_y
            for px_left, px_right, py_top in placed:
                if x_left < px_right + label_pad and x_right > px_left - label_pad:
                    candidate = py_top + label_h + 2
                    label_y = max(label_y, candidate)

            placed.append((x_left, x_right, label_y))
            x_left = max(1, min(width - text_width - 1, x_left))

            cr.set_source_rgba(0, 0, 0, 0.7)
            cr.rectangle(x_left, label_y, text_width, label_h)
            cr.fill()

            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.move_to(x_left + 2, label_y + label_h - 3)
            show_text(cr, text)

    def _draw_segment_delete_button(self, cr, x_start, x_stop, height, segment_index):
        """Draw a modern floating delete button with trash icon on the hovered segment."""
        button_size = 22
        button_x = x_stop - button_size - 6
        button_y = height - button_size - 4  # at bottom, 4px from edge

        segment_width = x_stop - x_start
        if segment_width < button_size + 12:
            button_x = x_start + (segment_width - button_size) / 2

        cx = button_x + button_size / 2
        cy = button_y + button_size / 2
        r = button_size / 2

        self.delete_button_bounds = (cx, cy, r)
        is_hovered = self.hovering_delete_button

        cr.save()

        # Shadow
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.arc(cx + 1, cy + 1, r + 1, 0, 2 * math.pi)
        cr.fill()

        # Background circle
        cr.set_source_rgba(*CUT, 0.95 if is_hovered else 0.80)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()

        # Trash icon (simplified: lid + body)
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.set_line_width(1.5)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        s = r * 0.45  # scale factor

        # Lid (horizontal line with small handle)
        cr.move_to(cx - s, cy - s * 0.5)
        cr.line_to(cx + s, cy - s * 0.5)
        cr.stroke()
        cr.move_to(cx - s * 0.3, cy - s * 0.5)
        cr.line_to(cx - s * 0.3, cy - s * 0.8)
        cr.line_to(cx + s * 0.3, cy - s * 0.8)
        cr.line_to(cx + s * 0.3, cy - s * 0.5)
        cr.stroke()

        # Body (trapezoid)
        cr.move_to(cx - s * 0.8, cy - s * 0.3)
        cr.line_to(cx - s * 0.6, cy + s)
        cr.line_to(cx + s * 0.6, cy + s)
        cr.line_to(cx + s * 0.8, cy - s * 0.3)
        cr.stroke()

        # Vertical lines inside body
        cr.set_line_width(1)
        cr.move_to(cx, cy - s * 0.1)
        cr.line_to(cx, cy + s * 0.75)
        cr.stroke()

        cr.restore()
