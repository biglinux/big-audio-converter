"""
Audio visualization component for displaying waveforms.
"""

import gi
import cairo
import numpy as np
from threading import Lock

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GObject, GLib


class AudioVisualizer(Gtk.DrawingArea):
    """Widget that displays audio waveform visualization."""

    def __init__(self):
        super().__init__()
        self.set_draw_func(self.on_draw)
        self.set_content_width(300)
        self.set_content_height(150)

        # Audio data
        self.waveform_data = None
        self.waveform_data_lock = Lock()
        self.position = 0
        self.duration = 0

        # Mouse hover tracking
        self.hover_time = None
        self.hover_x = None

        # Visualization settings
        self.bg_color = (0.1, 0.1, 0.1)
        self.wave_color = (0.2, 0.7, 1.0)
        self.position_color = (1.0, 0.5, 0.0)

        # Add listener for position updates
        self.seek_position_callback = None

        # Marker system
        self.markers_enabled = False
        self.marker_mode = "start"  # Can be "start", "stop", "confirm", "normal", "delete_prompt", "delete_all_confirm"
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

        # Setup click handler for both seeking and marking
        click_controller = Gtk.GestureClick()
        click_controller.connect("pressed", self.on_click_handler)
        click_controller.connect("released", self.on_release_handler)
        self.add_controller(click_controller)

        # Add drag controller for marker movement
        drag_controller = Gtk.GestureDrag()
        drag_controller.connect("drag-begin", self.on_drag_begin)
        drag_controller.connect("drag-update", self.on_drag_update)
        drag_controller.connect("drag-end", self.on_drag_end)
        self.add_controller(drag_controller)

        # Setup motion controller for hover effects
        motion_controller = Gtk.EventControllerMotion()
        motion_controller.connect("motion", self.on_motion)
        self.add_controller(motion_controller)

        # Callbacks
        self.marker_updated_callback = None
        self.confirm_dialog_callback = None

    def set_waveform(self, data, duration):
        """Set the waveform data to visualize."""
        with self.waveform_data_lock:
            # Reset position when setting a new waveform
            self.position = 0

            # Important: Do NOT clear markers here, as it interferes with the
            # marker restoration process. Markers will be handled by MainWindow.

            # Downsample data if needed for efficiency
            max_points = 1000
            if data is not None and len(data) > max_points:
                step = len(data) // max_points
                downsampled = []
                for i in range(0, len(data), step):
                    chunk = data[i : i + step]
                    if len(chunk) > 0:
                        downsampled.append(np.max(np.abs(chunk)))
                self.waveform_data = np.array(downsampled)
            else:
                self.waveform_data = data

            self.duration = duration

        # Queue redraw
        self.queue_draw()

    def set_position(self, position):
        """Update the current playback position."""
        self.position = position
        self.queue_draw()

    def clear_waveform(self):
        """Clear the current waveform visualization when audio is removed."""
        with self.waveform_data_lock:
            self.waveform_data = None
            self.position = 0
            self.duration = 0

            # Don't clear markers here, that's handled by MainWindow

        # Queue redraw to display "No audio loaded" message
        self.queue_draw()

    def set_markers_enabled(self, enabled):
        """Enable or disable marker system."""
        self.markers_enabled = enabled

        # Reset if disabled
        if not enabled:
            self.clear_all_markers()

        # Redraw
        self.queue_draw()

    def on_click_handler(self, gesture, n_press, x, y):
        """Unified handler for clicks on the waveform."""
        if not self.duration > 0:
            return

        # Calculate position in seconds with improved precision
        width = self.get_width()
        height = self.get_height()
        position = (x / width) * self.duration
        position = round(position, 3)  # Round to millisecond precision

        # Define interactive zones - BOTTOM 40% for segment manipulation, TOP 60% for seeking
        manipulation_zone_start = height * 0.6  # Bottom 40% is for manipulation
        in_manipulation_zone = y >= manipulation_zone_start

        # HIGHEST PRIORITY: Always check confirm/cancel buttons first when in dialog modes
        if self.marker_mode == "confirm":
            if self._check_confirm_buttons(x, y):
                return  # Button was clicked, exit early

        elif (
            self.marker_mode == "delete_prompt"
            or self.marker_mode == "delete_all_confirm"
        ):
            if self._check_delete_buttons(x, y):
                return  # Button was clicked, exit early

            # If clicked elsewhere in delete_prompt mode, exit delete mode
            if self.marker_mode == "delete_prompt":
                self._cancel_delete_prompt()
                return  # Exit after canceling - don't process as a normal click
            # If clicked elsewhere in delete_all_confirm mode, just exit confirmation
            elif self.marker_mode == "delete_all_confirm":
                self.marker_mode = "delete_prompt"  # Go back to delete prompt
                self.queue_draw()
                return

        # Store the click position for potential seeking later
        clicked_position = position

        # Handle marker/segment interaction ONLY in manipulation zone
        if self.markers_enabled and in_manipulation_zone:
            # First check for marker edge - highest priority for drag
            marker_info = self._find_marker_at_position(x, y)
            if marker_info:
                # Edges always start dragging immediately
                self.is_dragging_marker = True
                self.dragging_pair_index = marker_info["index"]
                self.dragging_marker_type = marker_info["type"]
                return  # Exit early to prevent seeking or other actions

            # Check if we're clicking on an existing segment body (not edges)
            segment_body_index = self._find_segment_body_at_position(x, y)
            if segment_body_index is not None:
                # Store as potential drag - will only activate on actual movement
                # We'll determine in drag_update if this becomes a drag or stays a click
                self.potential_drag_segment = {
                    "index": segment_body_index,
                    "start_x": x,
                }
                # Cache segment length for later use
                pair = self.marker_pairs[segment_body_index]
                self.drag_start_pos = None  # Will be set when drag actually starts
                # Don't show delete prompt immediately, let drag_end decide if it was a click
                return  # Exit early

            # Check if we're clicking on a segment (for potential deletion)
            # but only if not on edges or body
            clicked_pair = self._find_segment_at_position(x, y)
            if clicked_pair >= 0:
                # For segments that are clicked but not on edges or main body
                # (like thin edges/borders) - show deletion prompt
                self._prompt_delete_segment(clicked_pair)
                return

            # Handle marker placement based on mode (ONLY in manipulation zone)
            if self.marker_mode == "start":
                self.add_start_marker(position)
            elif self.marker_mode == "stop":
                self.add_stop_marker(position)

        # For top zone (60%), we ONLY seek
        # For bottom zone, we seek only if no other interaction happened
        if self.seek_position_callback:
            self.seek_position_callback(clicked_position)

    def on_release_handler(self, gesture, n_press, x, y):
        """Handle mouse release events."""
        # If we were dragging, notify about the marker update
        if self.is_dragging_marker:
            if self.marker_updated_callback:
                self.marker_updated_callback(self.get_marker_pairs())

            # Reset drag state
            self.is_dragging_marker = False
            self.dragging_marker_type = None
            self.dragging_pair_index = -1
        else:
            # If potential drag didn't become a real drag, it's just a click
            # This is handled in on_drag_end now
            pass

    def on_drag_begin(self, gesture, start_x, start_y):
        """Handle start of drag operation."""
        # If we have a potential segment drag, start tracking for REAL drag
        # We don't want to immediately enter drag mode - that happens in update
        pass

    def on_drag_update(self, gesture, offset_x, offset_y):
        """Handle drag movement updates."""
        # Check if we need to activate a potential segment drag
        if not self.is_dragging_marker and self.potential_drag_segment is not None:
            # Only start dragging if movement exceeds threshold
            if abs(offset_x) > self.drag_threshold:
                # Activate the drag
                self.is_dragging_marker = True
                self.dragging_pair_index = self.potential_drag_segment["index"]
                self.dragging_marker_type = "segment"
                ok, start_x, _ = gesture.get_start_point()
                if ok:
                    self.drag_start_x = start_x
                    # Record the starting segment position for more accurate movement
                    pair = self.marker_pairs[self.dragging_pair_index]
                    self.drag_start_pos = {"start": pair["start"], "stop": pair["stop"]}

        # Continue with normal drag handling if active
        if not self.is_dragging_marker or self.dragging_pair_index < 0:
            return

        # Get the start point properly - this returns (success, x, y)
        ok, start_x, start_y = gesture.get_start_point()
        if not ok:
            return

        # Calculate current x position
        current_x = start_x + offset_x
        width = self.get_width()

        # Update the appropriate marker(s)
        pair = self.marker_pairs[self.dragging_pair_index]

        if self.dragging_marker_type == "start":
            # Direct position calculation works fine for edge markers
            new_position = (current_x / width) * self.duration
            new_position = max(0, min(new_position, self.duration))
            new_position = round(new_position, 3)

            # When dragging start marker, ensure it doesn't go past the stop marker
            if pair["stop"] is not None:
                new_position = min(new_position, pair["stop"] - 0.1)
            pair["start"] = new_position
            pair["start_str"] = self._format_time(new_position)

            # Play audio from the start marker position being adjusted
            if self.seek_position_callback:
                self.seek_position_callback(new_position)

        elif self.dragging_marker_type == "stop":
            # Direct position calculation works fine for edge markers
            new_position = (current_x / width) * self.duration
            new_position = max(0, min(new_position, self.duration))
            new_position = round(new_position, 3)

            # When dragging stop marker, ensure it doesn't go before the start marker
            if pair["start"] is not None:
                new_position = max(new_position, pair["start"] + 0.1)
            pair["stop"] = new_position
            pair["stop_str"] = self._format_time(new_position)

            # Play audio from the stop marker position being adjusted
            if self.seek_position_callback:
                self.seek_position_callback(new_position)

        elif self.dragging_marker_type == "segment" and self.drag_start_pos is not None:
            # For segment movement, use a proportion that better matches mouse movement
            # Calculate the delta in screen space, then convert to time delta
            pixel_delta = offset_x
            time_delta = (pixel_delta / width) * self.duration

            # Apply the delta to the original positions (not cumulative)
            new_start = self.drag_start_pos["start"] + time_delta
            new_stop = self.drag_start_pos["stop"] + time_delta

            # Ensure segment stays within duration bounds
            if new_start < 0:
                # Move both points to maintain segment length
                shift = -new_start
                new_start = 0
                new_stop = self.drag_start_pos["stop"] + time_delta + shift
            elif new_stop > self.duration:
                # Move both points to maintain segment length
                shift = self.duration - new_stop
                new_stop = self.duration
                new_start = self.drag_start_pos["start"] + time_delta + shift

            # Check for collisions with other segments and adjust if needed
            collision = False
            for i, other_pair in enumerate(self.marker_pairs):
                if i == self.dragging_pair_index:
                    continue  # Skip the pair being dragged

                if other_pair["start"] < new_stop and other_pair["stop"] > new_start:
                    # Collision detected
                    collision = True
                    break

            # Only update if no collision
            if not collision:
                # Update the segment
                pair["start"] = new_start
                pair["stop"] = new_stop
                pair["start_str"] = self._format_time(new_start)
                pair["stop_str"] = self._format_time(new_stop)

                # Force redraw to show updated position
                self.queue_draw()

                # If we have a seek callback, update the playback position too
                if self.seek_position_callback:
                    # Seek to start of segment instead of middle for better context
                    self.seek_position_callback(new_start)

    def on_drag_end(self, gesture, offset_x, offset_y):
        """Handle end of drag operation."""
        # If drag never started (just a click), check if we need to prompt for deletion
        if not self.is_dragging_marker and self.potential_drag_segment is not None:
            # This was just a click on a segment (not a drag), show delete prompt
            segment_index = self.potential_drag_segment["index"]
            self._prompt_delete_segment(segment_index)

        # Reset tracking variables
        self.potential_drag_segment = None
        self.drag_start_pos = None

        # Handle normal drag cleanup
        # The cleanup is handled in on_release_handler

    def on_motion(self, controller, x, y):
        """Handle mouse motion for hover effects."""
        # Calculate hover time position
        if self.duration > 0:
            width = self.get_width()
            height = self.get_height()
            self.hover_x = x
            self.hover_time = (x / width) * self.duration
            # Force redraw to update the time display
            self.queue_draw()

        if not self.markers_enabled or self.duration <= 0:
            return

        # Define the manipulation zone boundary
        manipulation_zone_start = height * 0.6  # Bottom 40% is manipulation zone
        in_manipulation_zone = y >= manipulation_zone_start

        # If in delete prompt or confirm mode, only check for button hover
        if self.marker_mode in ["delete_prompt", "confirm"]:
            # Check for button hover first - takes precedence
            if (
                self.marker_mode == "delete_prompt"
                and self._check_delete_buttons(x, y, just_check=True)
            ) or (
                self.marker_mode == "confirm"
                and self._check_confirm_buttons(x, y, just_check=True)
            ):
                self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
                return
            else:
                # Reset cursor when not over buttons in these modes
                self.set_cursor(None)
                return

        # Normal mode - check if hovering over a marker edge for dragging
        if not self.is_dragging_marker:
            # Only show special cursors in the manipulation zone
            marker_info = self._find_marker_at_position(x, y)
            if marker_info and in_manipulation_zone:
                # Show resize cursor when hovering over a marker edge in manipulation zone
                if marker_info["type"] == "start":
                    self.set_cursor(Gdk.Cursor.new_from_name("w-resize"))
                else:
                    self.set_cursor(Gdk.Cursor.new_from_name("e-resize"))
                return

            # Check if hovering over segment body
            segment_body_index = self._find_segment_body_at_position(x, y)
            if segment_body_index is not None and in_manipulation_zone:
                # Show move cursor for segment body (only in manipulation zone)
                self.set_cursor(Gdk.Cursor.new_from_name("move"))
                return

            # If not over a marker, check for segment hover
            pair_index = self._find_segment_at_position(x, y)
            if pair_index != self.highlighted_pair:
                self.highlighted_pair = pair_index
                self.queue_draw()

            # Reset cursor to default if not over any special element
            # Or if outside manipulation zone
            if pair_index >= 0 and in_manipulation_zone:
                # Over a segment in manipulation zone - show pointer to indicate it can be clicked
                self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            else:
                # Not over any special element or outside manipulation zone - reset cursor
                self.set_cursor(None)

    def _find_segment_at_position(self, x, y):
        """Find if the given x,y position is on an existing segment."""
        width = self.get_width()
        height = self.get_height()

        # Only check the bottom 40% - our interaction zone
        if y < height * 0.6:
            return -1

        # Check each pair
        for i, pair in enumerate(self.marker_pairs):
            if pair["start"] is not None and pair["stop"] is not None:
                x_start = (pair["start"] / self.duration) * width
                x_stop = (pair["stop"] / self.duration) * width

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

        # Check each marker pair
        for i, pair in enumerate(self.marker_pairs):
            # Check start marker
            if pair["start"] is not None:
                x_start = (pair["start"] / self.duration) * width
                if abs(x - x_start) <= marker_hit_tolerance:
                    return {"index": i, "type": "start"}

            # Check stop marker
            if pair["stop"] is not None:
                x_stop = (pair["stop"] / self.duration) * width
                if abs(x - x_stop) <= marker_hit_tolerance:
                    return {"index": i, "type": "stop"}

        return None

    def _find_segment_body_at_position(self, x, y):
        """Find if x,y is inside a segment body (not on edge)."""
        width = self.get_width()
        height = self.get_height()

        # Only check bottom 40% - our segment interaction zone
        if y < height * 0.6:
            return None

        # Check each segment
        edge_tolerance = 5  # Same as marker hit tolerance

        for i, pair in enumerate(self.marker_pairs):
            if pair["start"] is not None and pair["stop"] is not None:
                x_start = (pair["start"] / self.duration) * width
                x_stop = (pair["stop"] / self.duration) * width

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

        # Define button regions (higher position to match confirm buttons)
        button_y = height * 0.2  # Match the confirm buttons position
        button_height = 30  # Match the confirm buttons height

        # Check if we're in delete all confirmation mode
        if self.marker_mode == "delete_all_confirm":
            # For delete all confirm, we have 2 buttons: Cancel and Confirm

            # Cancel button (blue, on left)
            cancel_x = width * 0.3 - 40
            cancel_width = 80

            # Confirm button (dark red, on right)
            confirm_x = width * 0.7 - 40
            confirm_width = 80

            # Check Cancel button (with padding)
            if (
                cancel_x - 10 <= x <= cancel_x + cancel_width + 10
                and button_y - 10 <= y <= button_y + button_height + 10
            ):
                if not just_check:
                    self.marker_mode = "delete_prompt"  # Go back to delete prompt
                    self.queue_draw()
                return True

            # Check Confirm button (with padding)
            if (
                confirm_x - 10 <= x <= confirm_x + confirm_width + 10
                and button_y - 10 <= y <= button_y + button_height + 10
            ):
                if not just_check:
                    self._delete_all_segments()
                return True

            return False

        # Normal delete prompt mode
        show_delete_all = len(self.marker_pairs) > 1

        if show_delete_all:
            # Three buttons layout - Cancel, Delete, Delete All
            cancel_x = width * 0.25 - 40
            cancel_width = 80

            delete_x = width * 0.5 - 40
            delete_width = 80

            delete_all_x = width * 0.75 - 40
            delete_all_width = 80
        else:
            # Two buttons layout - Cancel and Delete with wider spacing
            cancel_x = width * 0.3 - 40
            cancel_width = 80

            delete_x = width * 0.7 - 40
            delete_width = 80

        # Cancel button
        if (
            cancel_x - 10 <= x <= cancel_x + cancel_width + 10
            and button_y - 10 <= y <= button_y + button_height + 10
        ):
            if not just_check:
                self._cancel_delete_prompt()
            return True

        # Delete button
        if (
            delete_x - 10 <= x <= delete_x + delete_width + 10
            and button_y - 10 <= y <= button_y + button_height + 10
        ):
            if not just_check:
                self._delete_highlighted_segment()
            return True

        # Delete All button (only if showing)
        if show_delete_all and (
            delete_all_x - 10 <= x <= delete_all_x + delete_all_width + 10
            and button_y - 10 <= y <= button_y + button_height + 10
        ):
            if not just_check:
                self._prompt_delete_all_confirmation()
            return True

        return False

    def _prompt_delete_all_confirmation(self):
        """Show confirmation dialog for delete all action."""
        self.marker_mode = "delete_all_confirm"
        self.queue_draw()

    def _delete_all_segments(self):
        """Delete all segments."""
        if not self.marker_pairs:
            return

        # Clear all markers
        self.marker_pairs = []
        self.current_pair_index = -1
        self.marker_mode = "start"
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
            self.marker_mode = "start"
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
            self.marker_mode = "start"

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
        self.marker_mode = "delete_prompt"

        # If we have a callback for dialogs, use it
        if self.confirm_dialog_callback:
            self.confirm_dialog_callback("delete", pair_index)
            return

        self.queue_draw()

    def _delete_highlighted_segment(self):
        """Delete the currently highlighted segment."""
        if self.highlighted_pair >= 0:
            self.remove_marker_pair(self.highlighted_pair)
            # Exit delete mode
            self.marker_mode = "start"
            self.highlighted_pair = -1
            self.queue_draw()

    def _cancel_delete_prompt(self):
        """Cancel the delete prompt."""
        # Exit delete mode
        self.marker_mode = "start"
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
        self.marker_mode = "stop"
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
                self.marker_mode = "start"
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
        print("Visualizer: Clearing all markers")
        self.marker_pairs = []
        self.current_pair_index = -1
        self.marker_mode = "start"
        self.highlighted_pair = -1
        # Notify listener if any
        if self.marker_updated_callback:
            self.marker_updated_callback([])

        # Redraw
        self.queue_draw()

    def connect_seek_handler(self, callback):
        """Connect a handler for seek events."""
        self.seek_position_callback = callback

    def _format_time(self, time_in_seconds):
        """Format time in seconds to MM:SS.ms format with consistent precision."""
        if time_in_seconds is None:
            return ""

        # Ensure we have consistent precision (3 decimal places for milliseconds)
        time_in_seconds = round(time_in_seconds, 3)

        minutes = int(time_in_seconds // 60)
        seconds = int(time_in_seconds % 60)
        milliseconds = int((time_in_seconds % 1) * 1000)

        # Use FFmpeg-compatible format (MM:SS.mmm)
        return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

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

        # Debug output: Show processed segments
        if result:
            debug_segments = ", ".join([
                f"{s['start_str']}-{s['stop_str']}" for s in result
            ])
            print(f"Visualizer prepared {len(result)} segments: {debug_segments}")

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
            print(
                f"Ordering by NUMBER: {[(p['segment_index'], p['start_str']) for p in ordered_pairs]}"
            )
        else:
            # Order segments by their start time (chronological order)
            ordered_pairs = sorted(pairs, key=lambda x: x["start"])
            print(
                f"Ordering by TIME: {[(p['segment_index'], p['start_str']) for p in ordered_pairs]}"
            )

        return ordered_pairs

    # Add a new method for setting existing markers from strings
    def restore_markers(self, markers):
        """Restore markers from a list of marker pairs."""
        if not markers or not self.markers_enabled or self.duration <= 0:
            print(
                f"Visualizer: Cannot restore markers - conditions not met: markers={bool(markers)}, enabled={self.markers_enabled}, duration={self.duration}"
            )
            return False

        # Clear existing markers but maintain enabled state
        print(
            f"Visualizer: Restoring {len(markers)} markers for file with duration {self.duration}"
        )
        self.marker_pairs = []
        self.current_pair_index = -1
        self.marker_mode = "start"
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
                        print(
                            f"Visualizer: Skipping invalid marker: {start_time}-{stop_time}"
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
                    print(f"Visualizer: Error processing marker: {e}")
                    continue

        print(f"Visualizer: Successfully restored {valid_markers} valid markers")

        # Queue redraw
        self.queue_draw()
        return valid_markers > 0

    def _draw_markers(self, cr, width, height):
        """Draw all marker pairs on the waveform."""
        if not self.markers_enabled or not self.duration > 0:
            return

        # Define colors
        start_color = (0.8, 0.2, 0.2, 0.8)  # Red
        stop_color = (0.2, 0.7, 0.3, 0.8)  # Green
        region_color = (0.3, 0.6, 1.0, 0.25)  # Light blue, semi-transparent
        highlight_color = (1.0, 0.8, 0.0, 0.35)  # Gold, highlighted region
        button_bg = (0.2, 0.2, 0.2, 0.8)  # Dark gray for buttons
        dialog_overlay = (0, 0, 0, 0.5)  # Semi-transparent black for dialog overlay

        # First draw the segments and markers
        for i, pair in enumerate(self.marker_pairs):
            # Use highlight color if this segment is highlighted
            is_highlighted = i == self.highlighted_pair
            # Draw start marker with precise positioning
            if pair["start"] is not None:
                x_start = (pair["start"] / self.duration) * width
                # Ensure x_start is within bounds
                x_start = max(0, min(width, x_start))

                # Marker line
                cr.set_source_rgba(*start_color)
                cr.set_line_width(2)
                cr.move_to(x_start, 0)
                cr.line_to(x_start, height)
                cr.stroke()

                # Marker label
                time_str = self._format_time(pair["start"])
                cr.set_font_size(10)

                # Background for text
                cr.set_source_rgba(0, 0, 0, 0.7)
                text_width = len(time_str) * 6  # Approximate width based on text length
                cr.rectangle(x_start - text_width / 2, 2, text_width, 12)
                cr.fill()

                # Text
                cr.set_source_rgba(1, 1, 1, 0.9)
                cr.move_to(x_start - text_width / 2 + 2, 11)
                cr.show_text(time_str)

            # Draw stop marker with precise positioning
            if pair["stop"] is not None:
                x_stop = (pair["stop"] / self.duration) * width
                # Ensure x_stop is within bounds
                x_stop = max(0, min(width, x_stop))

                # Marker line
                cr.set_source_rgba(*stop_color)
                cr.set_line_width(2)
                cr.move_to(x_stop, 0)
                cr.line_to(x_stop, height)
                cr.stroke()

                # Marker label
                time_str = self._format_time(pair["stop"])
                cr.set_font_size(10)

                # Background for text
                cr.set_source_rgba(0, 0, 0, 0.7)
                text_width = len(time_str) * 6  # Approximate width based on text length
                cr.rectangle(x_stop - text_width / 2, 2, text_width, 12)
                cr.fill()

                # Text
                cr.set_source_rgba(1, 1, 1, 0.9)
                cr.move_to(x_stop - text_width / 2 + 2, 11)
                cr.show_text(time_str)

            # Draw region between start and stop with bounds checking
            if pair["start"] is not None and pair["stop"] is not None:
                x_start = (pair["start"] / self.duration) * width
                x_stop = (pair["stop"] / self.duration) * width

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

                # Add pair number
                cr.set_source_rgba(1, 1, 1, 0.8)
                cr.set_font_size(14)
                cr.move_to(x_start + 5, height - 8)
                cr.show_text(f"#{i + 1}")

        # IMPORTANT: Now draw the dialogs and buttons AFTER all segments
        # to ensure they're always on top and clickable

        # Draw confirmation UI if in confirm mode
        if self.marker_mode == "confirm" and self.current_pair_index >= 0:
            # Draw a stronger darkened overlay that covers everything
            cr.set_source_rgba(*dialog_overlay)
            cr.rectangle(0, 0, width, height)
            cr.fill()

            # Draw confirmation message with more visible background
            cr.set_source_rgba(0, 0, 0, 0.7)  # Black background for text
            msg = "Confirm selection?"
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(14)
            text_extents = cr.text_extents(msg)
            text_width = text_extents.width
            text_height = text_extents.height

            # Text background
            cr.rectangle(
                (width - text_width) / 2 - 10,
                height / 2 - 40 - text_height,
                text_width + 20,
                text_height + 10,
            )
            cr.fill()

            # Text
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.move_to((width - text_extents.width) / 2, height / 2 - 40)
            cr.show_text(msg)

            # Button positioning - higher in the display to avoid segments
            button_y = height * 0.2  # Position them even higher
            button_height = 30  # Slightly taller for better visibility

            # Confirm button (green, on right)
            confirm_x = width * 0.6
            confirm_width = 80

            cr.set_source_rgba(*button_bg)
            cr.rectangle(confirm_x, button_y, confirm_width, button_height)
            cr.fill()

            cr.set_source_rgba(0.2, 0.8, 0.2, 1.0)  # Green text
            cr.set_font_size(12)
            cr.move_to(confirm_x + 10, button_y + 18)  # Adjusted for taller button
            cr.show_text("Confirm")

            # Cancel button (red, on left)
            cancel_x = width * 0.4 - 80
            cancel_width = 80

            cr.set_source_rgba(*button_bg)
            cr.rectangle(cancel_x, button_y, cancel_width, button_height)
            cr.fill()

            cr.set_source_rgba(0.8, 0.2, 0.2, 1.0)  # Red text
            cr.move_to(cancel_x + 18, button_y + 18)  # Adjusted for taller button
            cr.show_text("Cancel")

        # Draw delete prompt if in delete mode with similar improvements
        elif self.marker_mode == "delete_prompt" and self.highlighted_pair >= 0:
            # Draw strong darkened overlay
            cr.set_source_rgba(*dialog_overlay)
            cr.rectangle(0, 0, width, height)
            cr.fill()

            # Draw delete message with background - MOVED LOWER
            cr.set_source_rgba(0, 0, 0, 0.7)  # Black background
            msg = f"Delete segment #{self.highlighted_pair + 1}?"
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(14)
            text_extents = cr.text_extents(msg)

            # Text background - moved to middle of screen (was at height/2 - 30)
            cr.rectangle(
                (width - text_extents.width) / 2 - 10,
                height * 0.5
                - text_extents.height,  # Positioned at 50% of screen height
                text_extents.width + 20,
                text_extents.height + 10,
            )
            cr.fill()

            # Text - moved to middle of screen (was at height/2 - 20)
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.move_to(
                (width - text_extents.width) / 2, height * 0.5 + 5
            )  # Positioned at 50% of screen height
            cr.show_text(msg)

            # Button positioning - improved visibility
            button_y = height * 0.2  # Higher position (unchanged)
            button_height = 30  # Taller buttons
            corner_radius = 6  # Rounded corners for modern look

            # Check if we show Delete All button (only when multiple segments exist)
            show_delete_all = len(self.marker_pairs) > 1

            # Set button positions based on number of buttons
            if show_delete_all:
                # Button positions - Cancel, Delete, Delete All
                cancel_x = width * 0.25 - 40
                delete_x = width * 0.5 - 40
                delete_all_x = width * 0.75 - 40
            else:
                # Button positions - Cancel and Delete with wider spacing
                cancel_x = width * 0.3 - 40
                delete_x = width * 0.7 - 40

            # Standard button width
            button_width = 80

            # Draw cancel button (blue, on left)
            self._draw_modern_button(
                cr,
                cancel_x,
                button_y,
                button_width,
                button_height,
                (0.2, 0.4, 0.8),  # Blue
                "Cancel",
                corner_radius,
            )

            # Draw delete button (red, in middle or right depending on layout)
            self._draw_modern_button(
                cr,
                delete_x,
                button_y,
                button_width,
                button_height,
                (0.8, 0.2, 0.2),  # Red
                "Delete",
                corner_radius,
            )

            # Draw Delete All button if needed (dark red, on right)
            if show_delete_all:
                self._draw_modern_button(
                    cr,
                    delete_all_x,
                    button_y,
                    button_width,
                    button_height,
                    (0.6, 0.1, 0.1),  # Dark red
                    "Delete All",
                    corner_radius,
                )

        # Draw delete all confirmation
        elif self.marker_mode == "delete_all_confirm":
            # Draw strong darkened overlay
            cr.set_source_rgba(0, 0, 0, 0.5)  # Semi-transparent black dialog overlay
            cr.rectangle(0, 0, width, height)
            cr.fill()

            # Draw confirmation message with background - MOVED LOWER
            cr.set_source_rgba(0, 0, 0, 0.7)  # Black background
            msg = f"Delete ALL segments? This cannot be undone."
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(14)
            text_extents = cr.text_extents(msg)

            # Text background - moved to middle of screen (was at height/2 - 30)
            cr.rectangle(
                (width - text_extents.width) / 2 - 10,
                height * 0.5
                - text_extents.height,  # Positioned at 50% of screen height
                text_extents.width + 20,
                text_extents.height + 10,
            )
            cr.fill()

            # Text - moved to middle of screen (was at height/2 - 20)
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.move_to(
                (width - text_extents.width) / 2, height * 0.5 + 5
            )  # Positioned at 50% of screen height
            cr.show_text(msg)

            # Button positioning - improved visibility and spacing for 2 buttons
            button_y = height * 0.2  # Higher position
            button_height = 30  # Taller buttons
            corner_radius = 6  # Rounded corners for modern look

            # Standard button width
            button_width = 80

            # Button positions - Cancel and Confirm with wider spacing
            cancel_x = width * 0.3 - 40
            confirm_x = width * 0.7 - 40

            # Draw cancel button (blue, on left)
            self._draw_modern_button(
                cr,
                cancel_x,
                button_y,
                button_width,
                button_height,
                (0.2, 0.4, 0.8),  # Blue
                "Cancel",
                corner_radius,
            )

            # Draw confirm button (dark red, on right)
            self._draw_modern_button(
                cr,
                confirm_x,
                button_y,
                button_width,
                button_height,
                (0.6, 0.1, 0.1),  # Dark red
                "Confirm",
                corner_radius,
            )

        # Draw mode indicator last to ensure visibility
        mode_text = ""
        if self.marker_mode == "start":
            mode_text = "Click to set START point"
        elif self.marker_mode == "stop":
            mode_text = "Click to set END point"

        if mode_text:
            # Add background to mode text for better visibility
            cr.set_source_rgba(0, 0, 0, 0.7)
            cr.set_font_size(12)
            text_extents = cr.text_extents(mode_text)
            cr.rectangle(8, 10, text_extents.width + 5, 16)
            cr.fill()

            # Draw text
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.move_to(10, 22)
            cr.show_text(mode_text)

    def _draw_modern_button(self, cr, x, y, width, height, color, text, radius=4):
        """Draw a modern-looking button with gradient and rounded corners."""
        # Create gradient from slightly lighter to base color
        lighter = tuple(min(c * 1.2, 1.0) for c in color)

        # Draw rounded button background with gradient
        pattern = cairo.LinearGradient(0, y, 0, y + height)
        pattern.add_color_stop_rgba(0, lighter[0], lighter[1], lighter[2], 1.0)
        pattern.add_color_stop_rgba(1, color[0], color[1], color[2], 1.0)

        cr.save()
        self._draw_rounded_rectangle(cr, x, y, width, height, radius)
        cr.set_source(pattern)
        cr.fill()

        # Draw subtle border
        cr.set_source_rgba(0.1, 0.1, 0.1, 0.3)
        self._draw_rounded_rectangle(cr, x, y, width, height, radius)
        cr.set_line_width(1)
        cr.stroke()

        # Draw text with shadow for depth
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(12)

        # Get text dimensions for centering
        text_extents = cr.text_extents(text)
        text_x = x + (width - text_extents.width) / 2
        text_y = y + height / 2 + text_extents.height / 2

        # Draw subtle text shadow
        cr.set_source_rgba(0, 0, 0, 0.3)
        cr.move_to(text_x + 1, text_y + 1)
        cr.show_text(text)

        # Draw text
        cr.set_source_rgba(1, 1, 1, 1.0)
        cr.move_to(text_x, text_y)
        cr.show_text(text)
        cr.restore()

    def _draw_rounded_rectangle(self, cr, x, y, width, height, radius):
        """Draw a rectangle with rounded corners."""
        degrees = 3.14159 / 180.0

        # Top left corner
        cr.new_sub_path()
        cr.arc(x + radius, y + radius, radius, 180 * degrees, 270 * degrees)

        # Top right corner
        cr.arc(x + width - radius, y + radius, radius, 270 * degrees, 0)

        # Bottom right corner
        cr.arc(x + width - radius, y + height - radius, radius, 0, 90 * degrees)

        # Bottom left corner
        cr.arc(x + radius, y + height - radius, radius, 90 * degrees, 180 * degrees)

        cr.close_path()

    def _draw_hover_time(self, cr, width, height):
        """Draw the time indicator at hover position."""
        if self.hover_time is None or self.hover_x is None or self.duration <= 0:
            return

        # Format the hover time
        time_str = self._format_time(self.hover_time)

        # Draw background for the time display at top of waveform
        cr.set_source_rgba(0, 0, 0, 0.7)  # Semi-transparent black background
        text_width = len(time_str) * 6  # Approximate width based on text length

        # Position the time label at the top center of the view
        x = self.hover_x - text_width / 2
        y = 2  # Near the top

        # Ensure label stays within view boundaries
        x = max(5, min(width - text_width - 5, x))

        # Draw background rectangle
        cr.rectangle(x, y, text_width + 6, 16)
        cr.fill()

        # Draw the time text
        cr.set_source_rgba(1, 1, 1, 1)  # White text
        cr.set_font_size(12)
        cr.move_to(x + 3, y + 12)
        cr.show_text(time_str)

        # Draw a small vertical line at hover position
        cr.set_source_rgba(1, 1, 1, 0.5)  # Semi-transparent white
        cr.set_line_width(1)
        cr.move_to(self.hover_x, y + 16)
        cr.line_to(self.hover_x, height)
        cr.stroke()

    def _draw_position_time(self, cr, width, height):
        """Draw the current playback position time."""
        if self.position <= 0 or self.duration <= 0:
            return

        # Format the position time
        time_str = self._format_time(self.position)

        # Draw background for the time display at the position marker
        cr.set_source_rgba(0, 0, 0, 0.8)  # More opaque black background
        text_width = len(time_str) * 6  # Approximate width based on text length

        # Calculate position for the label (centered on position)
        position_x = (self.position / self.duration) * width
        x = position_x - text_width / 2
        y = height - 22  # Position near bottom, above editing zone label

        # Ensure label stays within view boundaries
        x = max(5, min(width - text_width - 5, x))

        # Draw background rectangle with slightly larger size for better visibility
        cr.rectangle(x, y, text_width + 6, 16)
        cr.fill()

        # Draw the time text
        cr.set_source_rgba(1.0, 0.7, 0.0, 1.0)  # Orange text to match position line
        cr.set_font_size(12)
        cr.move_to(x + 3, y + 12)
        cr.show_text(time_str)

    def on_draw(self, area, cr, width, height):
        """Draw the waveform visualization."""
        # Draw background
        cr.set_source_rgb(*self.bg_color)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        # Draw waveform if we have data
        with self.waveform_data_lock:
            if self.waveform_data is not None and len(self.waveform_data) > 0:
                # Calculate scaling factors
                x_scale = width / len(self.waveform_data)
                y_scale = height * 0.4  # Use 80% of height (40% above and below center)

                # Draw waveform centered vertically
                cr.set_source_rgb(*self.wave_color)
                cr.set_line_width(2)

                for i, amplitude in enumerate(self.waveform_data):
                    x = i * x_scale
                    y_center = height / 2
                    y_offset = amplitude * y_scale

                    if i == 0:
                        cr.move_to(x, y_center - y_offset)
                    else:
                        cr.line_to(x, y_center - y_offset)

                cr.stroke()

                # Draw mirror image below center
                cr.set_source_rgba(
                    self.wave_color[0], self.wave_color[1], self.wave_color[2], 0.5
                )
                for i, amplitude in enumerate(self.waveform_data):
                    x = i * x_scale
                    y_center = height / 2
                    y_offset = amplitude * y_scale

                    if i == 0:
                        cr.move_to(x, y_center + y_offset)
                    else:
                        cr.line_to(x, y_center + y_offset)

                cr.stroke()

                # Draw position marker if playing
                if self.duration > 0:
                    position_x = (self.position / self.duration) * width

                    cr.set_source_rgb(*self.position_color)
                    cr.set_line_width(3)
                    cr.move_to(position_x, 0)
                    cr.line_to(position_x, height)
                    cr.stroke()
            else:
                # No waveform data, just show a placeholder message
                cr.set_source_rgb(0.7, 0.7, 0.7)
                cr.select_font_face(
                    "Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL
                )
                cr.set_font_size(16)

                text = "No audio loaded"
                extents = cr.text_extents(text)
                cr.move_to((width - extents.width) / 2, (height + extents.height) / 2)
                cr.show_text(text)

        # Draw a visual separator for the manipulation zone (bottom 40%)
        manipulation_zone_y = height * 0.6

        # Draw a subtle background for the manipulation zone
        cr.set_source_rgba(0.3, 0.3, 0.5, 0.1)  # Slight purplish tint
        cr.rectangle(0, manipulation_zone_y, width, height - manipulation_zone_y)
        cr.fill()

        # Draw a separator line
        cr.set_source_rgba(0.5, 0.5, 0.7, 0.6)  # More visible purple-ish line
        cr.set_line_width(1)
        cr.move_to(0, manipulation_zone_y)
        cr.line_to(width, manipulation_zone_y)
        cr.stroke()

        # Add a subtle label for the manipulation zone
        if self.markers_enabled:
            cr.set_source_rgba(0.7, 0.7, 0.9, 0.7)
            cr.select_font_face(
                "Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL
            )
            cr.set_font_size(9)
            label = "SEGMENT EDITING ZONE"
            text_extents = cr.text_extents(label)
            cr.move_to((width - text_extents.width) / 2, manipulation_zone_y + 10)
            cr.show_text(label)

        # Draw markers after waveform
        self._draw_markers(cr, width, height)

        # Draw current playback position time
        self._draw_position_time(cr, width, height)

        # Draw hover time indicator
        self._draw_hover_time(cr, width, height)
