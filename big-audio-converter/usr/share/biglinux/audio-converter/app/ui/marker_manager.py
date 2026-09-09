# app/ui/marker_manager.py

"""
Marker management mixin for AudioVisualizer.

Provides all marker-related logic: creating, editing, deleting,
drawing markers and related UI dialogs.
"""

import gettext
import logging
from enum import Enum, auto

import cairo
from gi.repository import Adw

from app.audio.media import Segment
from app.audio.process import MediaError
from app.ui.cairo_text import show_text, text_extents
from app.utils.time_formatter import format_time_short

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class MarkerMode(Enum):
    """Defines the state of the marker interaction system."""

    START = auto()
    STOP = auto()


class MarkerManagerMixin:
    """Mixin providing marker management to AudioVisualizer.

    This mixin is designed to be used with AudioVisualizer via multiple
    inheritance. It accesses AudioVisualizer attributes (duration, zoom_level,
    viewport_offset, etc.) through self.
    """

    def _init_marker_state(self):
        """Initialize all marker-related state variables."""
        self._marker_dialog = None
        self.markers_enabled = False
        self.marker_mode = MarkerMode.START  # Use Enum for state
        self.marker_pairs = []  # List of {start, stop} pairs
        self.current_pair_index = -1  # Index of currently active pair
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

        # Callbacks
        self.marker_updated_callback = None
        self.marker_drag_callback = None  # Callback for marker drag state changes

        # Track which segment is being hovered for visual feedback and keyboard shortcuts
        self.hovered_segment_index = -1

        # Track if hovering over delete button for cursor change
        self.hovering_delete_button = False
        self.delete_button_bounds = None  # Store (x, y, radius) for hit detection

    def set_markers_enabled(self, enabled):
        """Enable or disable marker system.

        Note: Disabling markers only prevents creating new markers,
        it does NOT delete existing markers to avoid accidental data loss.
        """
        self.markers_enabled = enabled

        # Don't clear markers when disabling - just prevent new ones from being created
        # This prevents accidental deletion when user switches cut mode

        # Redraw to update UI
        self.queue_draw()

    def _is_over_delete_button(self, x, y):
        """Check if the mouse position is over the delete button."""
        if self.delete_button_bounds is None:
            return False

        button_x, button_y, button_radius = self.delete_button_bounds

        # Calculate distance from center of button
        distance = ((x - button_x) ** 2 + (y - button_y) ** 2) ** 0.5

        # Return True if within button radius (with small padding for easier clicking)
        return distance <= button_radius + 3

    def _find_segment_at_position(self, x, y):
        """Find if the given x,y position is on an existing segment."""
        width = self.get_width()

        # Calculate visible time range
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

        # Check each pair
        for i, pair in enumerate(self.marker_pairs):
            if pair["start"] is not None and pair["stop"] is not None:
                # Skip segments not in visible range
                if pair["stop"] < start_time or pair["start"] > end_time:
                    continue

                x_start = ((pair["start"] - start_time) / visible_duration) * width
                x_stop = ((pair["stop"] - start_time) / visible_duration) * width

                # Check if x is within segment bounds (with a small margin)
                if x_start - 5 <= x <= x_stop + 5:
                    return i

        return -1

    def _find_marker_at_position(self, x, y):
        """Find if the given x,y position is on a marker edge."""
        if not self.markers_enabled or self.duration <= 0:
            return None

        width = self.get_width()
        height = self.get_height()
        marker_hit_tolerance = 5  # Pixels of tolerance for marker hit detection

        # Only check within reasonable Y bounds
        if y < 0 or y > height:
            return None

        # Calculate visible time range
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

        # Check each marker pair
        for i, pair in enumerate(self.marker_pairs):
            # Check start marker (only if visible)
            if pair["start"] is not None and start_time <= pair["start"] <= end_time:
                x_start = ((pair["start"] - start_time) / visible_duration) * width
                if abs(x - x_start) <= marker_hit_tolerance:
                    return {"index": i, "type": "start"}

            # Check stop marker (only if visible)
            if pair["stop"] is not None and start_time <= pair["stop"] <= end_time:
                x_stop = ((pair["stop"] - start_time) / visible_duration) * width
                if abs(x - x_stop) <= marker_hit_tolerance:
                    return {"index": i, "type": "stop"}

        return None

    def _find_segment_body_at_position(self, x, y):
        """Find if x,y is inside a segment body (not on edge)."""
        width = self.get_width()

        # Calculate visible time range
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

        # Check each segment
        edge_tolerance = 5  # Same as marker hit tolerance

        for i, pair in enumerate(self.marker_pairs):
            if pair["start"] is not None and pair["stop"] is not None:
                # Skip segments not in visible range
                if pair["stop"] < start_time or pair["start"] > end_time:
                    continue

                x_start = ((pair["start"] - start_time) / visible_duration) * width
                x_stop = ((pair["stop"] - start_time) / visible_duration) * width

                # Check if point is inside segment, but not on edges
                if (x_start + edge_tolerance) <= x <= (x_stop - edge_tolerance):
                    return i  # Return segment index

        return None



    def _prompt_delete_all_confirmation(self):
        self._close_marker_dialog()
        pairs = self.marker_pairs
        dialog = Adw.AlertDialog(heading=_("Delete All Segments"), body=_("Delete all marked segments? Source files will not be changed."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete All"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        def responded(dialog, response):
            self._marker_dialog = None
            if self.marker_pairs is pairs and response == "delete":
                self._delete_all_segments()
            self.grab_focus()
        dialog.connect("response", responded)
        self._marker_dialog = dialog
        dialog.present(self.get_root())

    def _close_marker_dialog(self):
        if self._marker_dialog is not None:
            dialog, self._marker_dialog = self._marker_dialog, None
            dialog.force_close()

    def _delete_all_segments(self):
        """Delete all segments."""
        if not self.marker_pairs:
            return

        # Clear all markers
        self._close_marker_dialog()
        self.marker_pairs = []
        self.current_pair_index = -1
        self.marker_mode = MarkerMode.START
        self.highlighted_pair = -1

        # Notify listeners
        if self.marker_updated_callback:
            self.marker_updated_callback([])

        # Redraw
        self.queue_draw()

    def _confirm_current_segment(self):
        """Confirm the current segment and prepare for next one."""
        if self.current_pair_index >= 0:
            # Reset to start mode for next segment
            self.marker_mode = MarkerMode.START
            self.current_pair_index = -1

            # Notify listener
            if self.marker_updated_callback:
                self.marker_updated_callback(self.get_marker_pairs())

            self.queue_draw()

    def _cancel_current_segment(self):
        """Cancel the current segment being edited."""
        if self.current_pair_index >= 0:
            # Remove the unconfirmed pair
            self.marker_pairs.pop(self.current_pair_index)
            self.current_pair_index = -1
            self.marker_mode = MarkerMode.START

            # Notify listener
            if self.marker_updated_callback:
                self.marker_updated_callback(self.get_marker_pairs())

            self.queue_draw()

    def _prompt_delete_segment(self, pair_index):
        if not 0 <= pair_index < len(self.marker_pairs):
            return
        self._close_marker_dialog()
        pairs = self.marker_pairs
        selected_pair = pairs[pair_index]
        dialog = Adw.AlertDialog(heading=_("Delete Segment"), body=_("Delete segment {number}?").format(number=pair_index + 1))
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
                index = next((i for i, pair in enumerate(pairs) if pair is selected_pair), None)
                if index is not None:
                    self.remove_marker_pair(index)
            self.grab_focus()
        dialog.connect("response", responded)
        self._marker_dialog = dialog
        dialog.present(self.get_root())



    def add_start_marker(self, position):
        """Add a start marker at the given position."""
        # Ensure valid position
        position = max(0, min(position, self.duration))
        position = round(position, 3)  # Consistent precision

        # Create new pair if needed
        if self.current_pair_index < 0 or self.current_pair_index >= len(
            self.marker_pairs
        ):
            self.marker_pairs.append({"start": position, "stop": None})
            self.current_pair_index = len(self.marker_pairs) - 1
        else:
            # Update existing pair
            self.marker_pairs[self.current_pair_index]["start"] = position

        # Auto-switch to stop mode after setting start
        self.marker_mode = MarkerMode.STOP
        self.queue_draw()

    def add_stop_marker(self, position):
        """Add a stop marker at the given position."""
        if self.current_pair_index >= 0 and self.current_pair_index < len(
            self.marker_pairs
        ):
            # Ensure valid position
            position = max(0, min(position, self.duration))
            position = round(position, 3)  # Consistent precision

            # Make sure stop is after start
            start = self.marker_pairs[self.current_pair_index]["start"]
            if position < start:
                # If clicked before start, swap (use click as start, old start as stop)
                self.marker_pairs[self.current_pair_index]["start"] = position
                self.marker_pairs[self.current_pair_index]["stop"] = start
            else:
                # Normal case - stop marker is after start
                self.marker_pairs[self.current_pair_index]["stop"] = position

            if position == start:
                self.marker_pairs[self.current_pair_index]["stop"] = None
                return

            # Commit only a nonempty segment.
            self._confirm_current_segment()
            self.queue_draw()

    def remove_marker_pair(self, index):
        """Remove a marker pair by index."""
        if 0 <= index < len(self.marker_pairs):
            self.marker_pairs.pop(index)

            # Reset current pair index if it was removed
            if self.current_pair_index == index:
                self.current_pair_index = -1
                self.marker_mode = MarkerMode.START
            # Adjust index if needed
            elif self.current_pair_index > index:
                self.current_pair_index -= 1

            # Reset highlight
            self.highlighted_pair = -1

            # Notify listener
            if self.marker_updated_callback:
                self.marker_updated_callback(self.get_marker_pairs())

            # Redraw
            self.queue_draw()

    def clear_all_markers(self):
        """Clear all marker pairs."""
        logger.debug("Clearing all markers")
        self._close_marker_dialog()
        self.marker_pairs = []
        self.current_pair_index = -1
        self.marker_mode = MarkerMode.START
        self.highlighted_pair = -1
        # Notify listener if any
        if self.marker_updated_callback:
            self.marker_updated_callback([])

        # Redraw
        self.queue_draw()

    def connect_marker_drag_handler(self, callback):
        """Connect a handler for marker drag state changes.

        Callback will be called with True when dragging starts, False when it ends.
        """
        self.marker_drag_callback = callback

    def _format_time(self, time_in_seconds):
        """Format time in seconds to HH:MM:SS.ms format for FFmpeg compatibility."""
        if time_in_seconds is None:
            return ""

        # Ensure we have consistent precision (3 decimal places for milliseconds)
        time_in_seconds = round(time_in_seconds, 3)

        hours = int(time_in_seconds // 3600)
        minutes = int((time_in_seconds % 3600) // 60)
        seconds = int(time_in_seconds % 60)
        milliseconds = int((time_in_seconds % 1) * 1000)

        # Use FFmpeg-compatible format (HH:MM:SS.mmm)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

    def get_marker_pairs(self):
        """Export every complete valid segment, without hidden minimum durations."""
        result = []
        for index, pair in enumerate(self.marker_pairs, 1):
            if pair.get("start") is None or pair.get("stop") is None:
                continue  # An unfinished mouse gesture is not a committed segment.
            result.append(Segment.from_mapping(dict(pair, segment_index=index), self.duration or None).as_mapping())
        return result

    def get_ordered_marker_pairs(self, order_by_number=False):
        """Get marker pairs ordered by either timeline position or segment number.

        Args:
            order_by_number: If True, order by segment number. If False, order by start time.

        Returns:
            List of ordered marker pairs.
        """
        # First, get all valid marker pairs
        pairs = self.get_marker_pairs()

        if not pairs:
            return []

        if order_by_number:
            # Order segments by their display number (as shown in UI)
            # Each marker pair should have a "segment_index" key that was added in get_marker_pairs
            ordered_pairs = sorted(pairs, key=lambda x: x["segment_index"])
            logger.debug(
                "Ordering by NUMBER: %s",
                [(p['segment_index'], p['start_str']) for p in ordered_pairs],
            )
        else:
            # Order segments by their start time (chronological order)
            ordered_pairs = sorted(pairs, key=lambda x: x["start"])
            logger.debug(
                "Ordering by TIME: %s",
                [(p['segment_index'], p['start_str']) for p in ordered_pairs],
            )

        return ordered_pairs

    # Add a new method for setting existing markers from strings
    def restore_markers(self, markers):
        """Restore an entire valid edit atomically, including an empty edit."""
        try:
            validated = [Segment.from_mapping(pair, self.duration or None).as_mapping() for pair in markers]
        except (MediaError, TypeError, ValueError) as exc:
            logger.warning("Saved segments could not be restored: %s", exc)
            return False
        self._close_marker_dialog()
        self.marker_pairs = validated
        self.current_pair_index = -1
        self.marker_mode = MarkerMode.START
        self.highlighted_pair = -1
        self.queue_draw()
        return True

    def _draw_markers(self, cr, width, height):
        """Draw all marker pairs on the waveform.

        Note: Markers are drawn even when markers_enabled is False,
        to show existing markers. The enabled flag only controls creating new markers.
        """
        if not self.duration > 0:
            return

        # Define colors
        start_color = (0.8, 0.2, 0.2, 0.8)  # Red
        stop_color = (0.2, 0.7, 0.3, 0.8)  # Green
        region_color = (0.3, 0.6, 1.0, 0.25)  # Light blue, semi-transparent
        highlight_color = (1.0, 0.8, 0.0, 0.35)  # Gold, highlighted region

        # Calculate visible time range
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

        # Collect all marker labels for collision-aware placement
        label_items = []  # list of (x_center, text, color_rgba)

        # First draw the segments and markers
        for i, pair in enumerate(self.marker_pairs):
            # Skip markers outside visible range
            if (pair["start"] is not None and pair["stop"] is not None) and (pair["stop"] < start_time or pair["start"] > end_time):
                continue  # Skip this pair, it's not visible

            # Use highlight color if this segment is highlighted
            is_highlighted = i == self.highlighted_pair
            # Draw start marker with precise positioning (account for zoom)
            if pair["start"] is not None and start_time <= pair["start"] <= end_time:
                x_start = ((pair["start"] - start_time) / visible_duration) * width
                # Ensure x_start is within bounds
                x_start = max(0, min(width, x_start))

                # Marker line
                cr.set_source_rgba(*start_color)
                cr.set_line_width(2)
                cr.move_to(x_start, 0)
                cr.line_to(x_start, height)
                cr.stroke()

                # Collect label for deferred drawing (condensed format)
                time_str = format_time_short(pair["start"])
                label_items.append((x_start, time_str, start_color))

            # Draw stop marker with precise positioning (account for zoom)
            if pair["stop"] is not None and start_time <= pair["stop"] <= end_time:
                x_stop = ((pair["stop"] - start_time) / visible_duration) * width
                # Ensure x_stop is within bounds
                x_stop = max(0, min(width, x_stop))

                # Marker line
                cr.set_source_rgba(*stop_color)
                cr.set_line_width(2)
                cr.move_to(x_stop, 0)
                cr.line_to(x_stop, height)
                cr.stroke()

                # Collect label for deferred drawing (condensed format)
                time_str = format_time_short(pair["stop"])
                label_items.append((x_stop, time_str, stop_color))

            # Draw region between start and stop with bounds checking (account for zoom)
            if pair["start"] is not None and pair["stop"] is not None:
                # Calculate positions accounting for zoom and viewport
                if pair["start"] <= end_time and pair["stop"] >= start_time:
                    # Clamp to visible range
                    visible_start = max(pair["start"], start_time)
                    visible_stop = min(pair["stop"], end_time)

                    x_start = ((visible_start - start_time) / visible_duration) * width
                    x_stop = ((visible_stop - start_time) / visible_duration) * width

                # Ensure x_start and x_stop are within bounds and in correct order
                x_start = max(0, min(width, x_start))
                x_stop = max(0, min(width, x_stop))
                if x_start > x_stop:
                    x_start, x_stop = x_stop, x_start

                # Region rectangle - use highlight color if highlighted
                if is_highlighted:
                    cr.set_source_rgba(*highlight_color)
                else:
                    cr.set_source_rgba(*region_color)
                cr.rectangle(x_start, 0, x_stop - x_start, height)
                cr.fill()

                # Subtle top and bottom borders on segment region
                seg_w = x_stop - x_start
                if seg_w > 2:
                    border_alpha = 0.45 if is_highlighted else 0.30
                    cr.set_source_rgba(0.3, 0.6, 1.0, border_alpha)
                    cr.set_line_width(1)
                    cr.move_to(x_start, 0.5)
                    cr.line_to(x_stop, 0.5)
                    cr.stroke()
                    cr.move_to(x_start, height - 0.5)
                    cr.line_to(x_stop, height - 0.5)
                    cr.stroke()

                # Pill badge for segment number
                badge_text = f"#{i + 1}"
                cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(11)
                ext = text_extents(cr, badge_text)
                badge_w = ext.width + 12
                badge_h = 18
                badge_x = x_start + 4
                badge_y = height - badge_h - 28  # above ruler
                # Badge background
                cr.set_source_rgba(0, 0, 0, 0.55)
                self._draw_rounded_rect(cr, badge_x, badge_y, badge_w, badge_h, 4)
                cr.fill()
                # Badge text
                cr.set_source_rgba(1, 1, 1, 0.90)
                cr.move_to(badge_x + 6, badge_y + badge_h - 5)
                show_text(cr, badge_text)

                # Draw delete button when hovering over this segment
                if i == self.hovered_segment_index and self._marker_dialog is None:
                    self._draw_segment_delete_button(cr, x_start, x_stop, height, i)

        # Draw marker time labels with collision avoidance
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
        for x_center, text, color in label_items:
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
        cr.arc(cx + 1, cy + 1, r + 1, 0, 2 * 3.14159)
        cr.fill()

        # Background circle
        if is_hovered:
            cr.set_source_rgba(0.90, 0.22, 0.22, 0.95)
        else:
            cr.set_source_rgba(0.70, 0.15, 0.15, 0.80)
        cr.arc(cx, cy, r, 0, 2 * 3.14159)
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
        # Handle on lid
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
