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

from app.utils.time_formatter import format_time_short

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class MarkerMode(Enum):
    """Defines the state of the marker interaction system."""

    START = auto()
    STOP = auto()
    CONFIRM = auto()
    NORMAL = auto()
    DELETE_PROMPT = auto()
    DELETE_ALL_CONFIRM = auto()


class MarkerManagerMixin:
    """Mixin providing marker management to AudioVisualizer.

    This mixin is designed to be used with AudioVisualizer via multiple
    inheritance. It accesses AudioVisualizer attributes (duration, zoom_level,
    viewport_offset, etc.) through self.
    """

    def _init_marker_state(self):
        """Initialize all marker-related state variables."""
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

    def _check_confirm_buttons(self, x, y, just_check=False):
        """Check if x,y is on confirm/cancel buttons and handle if needed."""
        width = self.get_width()
        height = self.get_height()

        # Define button regions (higher up to avoid segment overlap)
        button_y = height * 0.2  # Position higher than before (was 0.25)
        button_height = 30  # Taller for easier targeting

        # Confirm button (green, on right)
        confirm_x = width * 0.6
        confirm_width = 80

        # Cancel button (red, on left)
        cancel_x = width * 0.4 - 80
        cancel_width = 80

        # Expanded hit area for better tap detection (add 10px padding)
        if (
            confirm_x - 10 <= x <= confirm_x + confirm_width + 10
            and button_y - 10 <= y <= button_y + button_height + 10
        ):
            if not just_check:
                self._confirm_current_segment()
            return True

        # Check if clicked on cancel button (with expanded hit area)
        if (
            cancel_x - 10 <= x <= cancel_x + cancel_width + 10
            and button_y - 10 <= y <= button_y + button_height + 10
        ):
            if not just_check:
                self._cancel_current_segment()
            return True

        return False

    def _check_delete_buttons(self, x, y, just_check=False):
        """Check if x,y is on delete/cancel buttons and handle if needed."""
        width = self.get_width()
        height = self.get_height()

        # Card and button layout must match _draw_markers() exactly
        btn_h = 30

        # Check if we're in delete all confirmation mode
        if self.marker_mode == MarkerMode.DELETE_ALL_CONFIRM:
            card_w = min(380, width - 32)
            card_h = 110
            card_x = (width - card_w) / 2
            card_y = (height - card_h) / 2

            btn_gap = 12
            btn_w = (card_w - 32 - btn_gap) / 2
            btn_y = card_y + card_h - btn_h - 14
            cancel_x = card_x + 16
            confirm_x = cancel_x + btn_w + btn_gap

            # Check Cancel button
            if (cancel_x <= x <= cancel_x + btn_w and btn_y <= y <= btn_y + btn_h):
                if not just_check:
                    self.marker_mode = MarkerMode.DELETE_PROMPT
                    self.queue_draw()
                return True

            # Check Confirm button
            if (confirm_x <= x <= confirm_x + btn_w and btn_y <= y <= btn_y + btn_h):
                if not just_check:
                    self._delete_all_segments()
                return True

            return False

        # Normal delete prompt mode
        card_w = min(340, width - 32)
        card_h = 110
        card_x = (width - card_w) / 2
        card_y = (height - card_h) / 2

        show_delete_all = len(self.marker_pairs) > 1

        if show_delete_all:
            btn_gap = 8
            total_btn_w = card_w - 32
            btn_w = (total_btn_w - 2 * btn_gap) / 3
            cancel_x = card_x + 16
            delete_x = cancel_x + btn_w + btn_gap
            delete_all_x = delete_x + btn_w + btn_gap
        else:
            btn_gap = 12
            btn_w = (card_w - 32 - btn_gap) / 2
            cancel_x = card_x + 16
            delete_x = cancel_x + btn_w + btn_gap

        btn_y = card_y + card_h - btn_h - 14

        # Cancel button
        if (cancel_x <= x <= cancel_x + btn_w and btn_y <= y <= btn_y + btn_h):
            if not just_check:
                self._cancel_delete_prompt()
            return True

        # Delete button
        if (delete_x <= x <= delete_x + btn_w and btn_y <= y <= btn_y + btn_h):
            if not just_check:
                self._delete_highlighted_segment()
            return True

        # Delete All button (only if showing)
        if show_delete_all and (delete_all_x <= x <= delete_all_x + btn_w and btn_y <= y <= btn_y + btn_h):
            if not just_check:
                self._prompt_delete_all_confirmation()
            return True

        return False

    def _prompt_delete_all_confirmation(self):
        """Show confirmation dialog for delete all action."""
        self.marker_mode = MarkerMode.DELETE_ALL_CONFIRM
        self.queue_draw()

    def _delete_all_segments(self):
        """Delete all segments."""
        if not self.marker_pairs:
            return

        # Clear all markers
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
        """Display deletion prompt for the segment."""
        # Store the index of pair to potentially delete
        self.highlighted_pair = pair_index
        # Set flag to keep delete prompt visible
        # Switch to delete prompt mode
        self.marker_mode = MarkerMode.DELETE_PROMPT

        self.queue_draw()

    def _delete_highlighted_segment(self):
        """Delete the currently highlighted segment."""
        if self.highlighted_pair >= 0:
            self.remove_marker_pair(self.highlighted_pair)
            # Exit delete mode
            self.marker_mode = MarkerMode.START
            self.highlighted_pair = -1
            self.queue_draw()

    def _cancel_delete_prompt(self):
        """Cancel the delete prompt."""
        # Exit delete mode
        self.marker_mode = MarkerMode.START
        self.highlighted_pair = -1
        self.queue_draw()

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

            # Automatically confirm the segment instead of showing confirmation dialog
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
        """Get a copy of the current marker pairs with formatted strings."""
        result = []
        for i, pair in enumerate(self.marker_pairs):
            # Skip incomplete pairs or pairs where both values are the same
            if (
                pair["start"] is None
                or pair["stop"] is None
                or abs(pair["start"] - pair["stop"]) < 0.1
            ):  # Skip segments shorter than 0.1s
                continue

            # Ensure both values are valid and properly rounded
            start = round(pair["start"], 3) if pair["start"] is not None else None
            stop = round(pair["stop"], 3) if pair["stop"] is not None else None

            # Force start to be before stop (swap if needed)
            if start is not None and stop is not None and start > stop:
                start, stop = stop, start

            # Only include valid pairs
            if start is not None and stop is not None:
                # Create formatted time strings in FFmpeg-compatible format
                start_str = self._format_time(start)
                stop_str = self._format_time(stop)

                result.append({
                    "start": start,
                    "stop": stop,
                    "start_str": start_str,
                    "stop_str": stop_str,
                    "segment_index": i
                    + 1,  # Store the segment's display number (1-based)
                })

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
        """Restore markers from a list of marker pairs."""
        if not markers or not self.markers_enabled or self.duration <= 0:
            logger.debug(
                "Cannot restore markers - conditions not met: markers=%s, enabled=%s, duration=%s",
                bool(markers), self.markers_enabled, self.duration,
            )
            return False

        # Clear existing markers but maintain enabled state
        logger.debug(
            "Restoring %d markers for file with duration %s",
            len(markers), self.duration,
        )
        self.marker_pairs = []
        self.current_pair_index = -1
        self.marker_mode = MarkerMode.START
        self.highlighted_pair = -1
        # Add each marker pair from the provided list
        valid_markers = 0
        for pair in markers:
            if "start" in pair and "stop" in pair:
                try:
                    start_time = float(pair["start"])
                    stop_time = float(pair["stop"])

                    # Ensure times are within duration
                    start_time = min(start_time, self.duration)
                    stop_time = min(stop_time, self.duration)

                    # Skip invalid markers
                    if start_time >= stop_time or start_time < 0:
                        logger.debug(
                            "Skipping invalid marker: %s-%s",
                            start_time, stop_time,
                        )
                        continue

                    # Create a new marker pair and add it to our list
                    validated_pair = {
                        "start": start_time,
                        "stop": stop_time,
                        "start_str": self._format_time(start_time),
                        "stop_str": self._format_time(stop_time),
                    }
                    self.marker_pairs.append(validated_pair)
                    valid_markers += 1
                except (ValueError, TypeError) as e:
                    logger.warning("Error processing marker: %s", e)
                    continue

        logger.debug("Successfully restored %d valid markers", valid_markers)

        # Queue redraw
        self.queue_draw()
        return valid_markers > 0

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
            if pair["start"] is not None and pair["stop"] is not None:
                if pair["stop"] < start_time or pair["start"] > end_time:
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
                ext = cr.text_extents(badge_text)
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
                cr.show_text(badge_text)

                # Draw delete button when hovering over this segment
                if i == self.hovered_segment_index and self.marker_mode not in [
                    MarkerMode.DELETE_PROMPT,
                    MarkerMode.DELETE_ALL_CONFIRM,
                    MarkerMode.CONFIRM,
                ]:
                    self._draw_segment_delete_button(cr, x_start, x_stop, height, i)

        # Draw marker time labels with collision avoidance
        if label_items:
            self._draw_marker_labels(cr, width, label_items)

        # IMPORTANT: Now draw the dialogs and buttons AFTER all segments
        # to ensure they're always on top and clickable

        # Draw confirmation UI if in confirm mode
        if self.marker_mode == MarkerMode.CONFIRM and self.current_pair_index >= 0:
            self._draw_confirm_dialog(cr, width, height)
        elif self.marker_mode == MarkerMode.DELETE_PROMPT and self.highlighted_pair >= 0:
            self._draw_delete_prompt_dialog(cr, width, height)
        elif self.marker_mode == MarkerMode.DELETE_ALL_CONFIRM:
            self._draw_delete_all_dialog(cr, width, height)

    def _draw_marker_labels(self, cr, width, label_items):
        """Draw time labels for markers with collision-aware vertical stacking."""
        cr.set_font_size(10)
        label_h = 14
        label_pad = 4
        base_y = 2

        label_items.sort(key=lambda item: item[0])

        placed = []  # (x_left, x_right, y_top)
        for x_center, text, color in label_items:
            text_width = len(text) * 6
            x_left = x_center - text_width / 2
            x_right = x_left + text_width

            label_y = base_y
            for px_left, px_right, py_top in placed:
                if x_left < px_right + label_pad and x_right > px_left - label_pad:
                    candidate = py_top + label_h + 2
                    if candidate > label_y:
                        label_y = candidate

            placed.append((x_left, x_right, label_y))
            x_left = max(1, min(width - text_width - 1, x_left))

            cr.set_source_rgba(0, 0, 0, 0.7)
            cr.rectangle(x_left, label_y, text_width, label_h)
            cr.fill()

            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.move_to(x_left + 2, label_y + label_h - 3)
            cr.show_text(text)

    def _draw_confirm_dialog(self, cr, width, height):
        """Draw the segment confirmation overlay dialog."""
        cr.set_source_rgba(0, 0, 0, 0.5)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        msg = _("Confirm selection?")
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(14)
        text_extents = cr.text_extents(msg)

        # Text background
        cr.set_source_rgba(0, 0, 0, 0.7)
        cr.rectangle(
            (width - text_extents.width) / 2 - 10,
            height / 2 - 40 - text_extents.height,
            text_extents.width + 20,
            text_extents.height + 10,
        )
        cr.fill()

        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.move_to((width - text_extents.width) / 2, height / 2 - 40)
        cr.show_text(msg)

        button_bg = (0.2, 0.2, 0.2, 0.8)
        button_y = height * 0.2
        button_height = 30

        # Confirm button (green, right)
        confirm_x = width * 0.6
        cr.set_source_rgba(*button_bg)
        cr.rectangle(confirm_x, button_y, 80, button_height)
        cr.fill()
        cr.set_source_rgba(0.2, 0.8, 0.2, 1.0)
        cr.set_font_size(12)
        cr.move_to(confirm_x + 10, button_y + 18)
        cr.show_text(_("Confirm"))

        # Cancel button (red, left)
        cancel_x = width * 0.4 - 80
        cr.set_source_rgba(*button_bg)
        cr.rectangle(cancel_x, button_y, 80, button_height)
        cr.fill()
        cr.set_source_rgba(0.8, 0.2, 0.2, 1.0)
        cr.move_to(cancel_x + 18, button_y + 18)
        cr.show_text(_("Cancel"))

    def _draw_dialog_card(self, cr, width, height, card_w, accent_color):
        """Draw a modal dialog card with overlay, shadow, background and accent line.

        Returns (card_x, card_y, card_h, card_r) for button placement.
        """
        card_h = 110
        card_x = (width - card_w) / 2
        card_y = (height - card_h) / 2
        card_r = 12

        # Drop shadow
        cr.set_source_rgba(0, 0, 0, 0.40)
        self._draw_rounded_rect(cr, card_x + 2, card_y + 3, card_w, card_h, card_r)
        cr.fill()

        # Background
        cr.set_source_rgb(0.16, 0.16, 0.18)
        self._draw_rounded_rect(cr, card_x, card_y, card_w, card_h, card_r)
        cr.fill()

        # Top accent line
        cr.set_source_rgba(*accent_color)
        cr.set_line_width(2)
        cr.move_to(card_x + card_r, card_y + 1)
        cr.line_to(card_x + card_w - card_r, card_y + 1)
        cr.stroke()

        return card_x, card_y, card_h, card_r

    def _draw_dialog_button(self, cr, x, y, w, h, r, label, style="outline"):
        """Draw a dialog button. style: 'outline', 'solid_red', 'solid_dark_red'."""
        if style == "outline":
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.10)
            self._draw_rounded_rect(cr, x, y, w, h, r)
            cr.fill()
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.30)
            cr.set_line_width(1)
            self._draw_rounded_rect(cr, x + 0.5, y + 0.5, w - 1, h - 1, r)
            cr.stroke()
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(12)
            ext = cr.text_extents(label)
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.70)
        elif style == "solid_red":
            cr.set_source_rgb(0.78, 0.18, 0.18)
            self._draw_rounded_rect(cr, x, y, w, h, r)
            cr.fill()
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(12)
            ext = cr.text_extents(label)
            cr.set_source_rgb(1.0, 1.0, 1.0)
        elif style == "solid_dark_red":
            cr.set_source_rgb(0.55, 0.10, 0.10)
            self._draw_rounded_rect(cr, x, y, w, h, r)
            cr.fill()
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(12)
            ext = cr.text_extents(label)
            cr.set_source_rgb(1.0, 1.0, 1.0)
        elif style == "solid_confirm":
            cr.set_source_rgb(0.65, 0.12, 0.12)
            self._draw_rounded_rect(cr, x, y, w, h, r)
            cr.fill()
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(12)
            ext = cr.text_extents(label)
            cr.set_source_rgb(1.0, 1.0, 1.0)
        cr.move_to(x + (w - ext.width) / 2, y + h / 2 + ext.height / 2)
        cr.show_text(label)

    def _draw_delete_prompt_dialog(self, cr, width, height):
        """Draw the delete-segment confirmation dialog."""
        # Dim overlay
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        card_w = min(340, width - 32)
        card_x, card_y, card_h, card_r = self._draw_dialog_card(
            cr, width, height, card_w, (0.85, 0.22, 0.22, 0.80)
        )

        # Title text
        msg = _("Delete segment #{}?").format(self.highlighted_pair + 1)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(14)
        text_extents = cr.text_extents(msg)
        cr.set_source_rgb(0.93, 0.93, 0.93)
        cr.move_to(card_x + (card_w - text_extents.width) / 2, card_y + 34)
        cr.show_text(msg)

        # Buttons row
        btn_h, btn_r = 30, 6
        btn_y = card_y + card_h - btn_h - 14
        show_delete_all = len(self.marker_pairs) > 1

        if show_delete_all:
            btn_gap = 8
            btn_w = (card_w - 32 - 2 * btn_gap) / 3
            cancel_x = card_x + 16
            delete_x = cancel_x + btn_w + btn_gap
            delete_all_x = delete_x + btn_w + btn_gap
        else:
            btn_gap = 12
            btn_w = (card_w - 32 - btn_gap) / 2
            cancel_x = card_x + 16
            delete_x = cancel_x + btn_w + btn_gap

        self._draw_dialog_button(cr, cancel_x, btn_y, btn_w, btn_h, btn_r, _("Cancel"), "outline")
        self._draw_dialog_button(cr, delete_x, btn_y, btn_w, btn_h, btn_r, _("Delete"), "solid_red")
        if show_delete_all:
            self._draw_dialog_button(cr, delete_all_x, btn_y, btn_w, btn_h, btn_r, _("Delete All"), "solid_dark_red")

    def _draw_delete_all_dialog(self, cr, width, height):
        """Draw the delete-all-segments confirmation dialog."""
        # Dim overlay
        cr.set_source_rgba(0, 0, 0, 0.60)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        card_w = min(380, width - 32)
        card_x, card_y, card_h, card_r = self._draw_dialog_card(
            cr, width, height, card_w, (0.90, 0.30, 0.15, 0.85)
        )

        # Warning text
        msg = _("Delete ALL segments? This cannot be undone.")
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(13)
        text_extents = cr.text_extents(msg)
        cr.set_source_rgb(1.0, 0.82, 0.80)
        cr.move_to(card_x + (card_w - text_extents.width) / 2, card_y + 34)
        cr.show_text(msg)

        # Buttons
        btn_h, btn_r, btn_gap = 30, 6, 12
        btn_w = (card_w - 32 - btn_gap) / 2
        btn_y = card_y + card_h - btn_h - 14
        cancel_x = card_x + 16
        confirm_x = cancel_x + btn_w + btn_gap

        self._draw_dialog_button(cr, cancel_x, btn_y, btn_w, btn_h, btn_r, _("Cancel"), "outline")
        self._draw_dialog_button(cr, confirm_x, btn_y, btn_w, btn_h, btn_r, _("Confirm"), "solid_confirm")

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
