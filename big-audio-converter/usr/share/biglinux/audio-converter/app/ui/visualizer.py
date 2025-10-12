# app/ui/visualizer.py

"""
Audio visualization component for displaying waveforms.
"""

import gettext
import gi
import cairo
import numpy as np
import logging
from threading import Lock
from enum import Enum, auto

from app.utils.time_formatter import format_time_short

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib

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


class AudioVisualizer(Gtk.DrawingArea):
    """Widget that displays audio waveform visualization."""

    def __init__(self):
        super().__init__()
        self.set_draw_func(self.on_draw)

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

        # Waveform rendering cache for performance
        self.cached_peaks = None
        self.cached_viewport_key = None  # (start_sample, end_sample, width, height)
        self.cached_waveform_surface = (
            None  # Pre-rendered waveform as Cairo ImageSurface
        )

        # Add listener for position updates
        self.seek_position_callback = None
        self.marker_drag_callback = None  # Callback for marker drag state changes

        # Track if selection-only playback mode is active
        self.selection_only_mode = False

        # Player reference for checking playback state
        self.player = None

        # Marker system
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

        # Zoom and pan functionality
        self.zoom_level = 1.0  # 1.0 = no zoom, higher = zoomed in
        self.viewport_offset = 0.0  # Position in waveform (0.0 to 1.0)
        self.min_zoom = 1.0
        self.max_zoom = 1000.0
        self.is_panning = False
        self.pan_start_x = 0
        self.pan_start_offset = 0

        # Simulated scrollbar for horizontal navigation when zoomed
        self.scrollbar_height = 12
        self.scrollbar_margin = 4
        self.is_dragging_scrollbar = False
        self.scrollbar_drag_start_x = 0
        self.scrollbar_drag_start_offset = 0
        self.hovering_scrollbar = False

        # Middle-click or Ctrl+drag panning
        self.pan_gesture_active = False
        self.pan_gesture_start_x = 0
        self.pan_gesture_start_offset = 0

        # Ruler scrubbing (dragging on timeline)
        self.ruler_scrubbing = False

        # Callback for notifying zoom changes (to update UI slider)
        self.zoom_changed_callback = None

        # Loading state for waveform generation
        self.is_loading = False
        self.loading_message = _("Generating waveform...")

        # Add scroll controller for zoom and horizontal panning with Shift
        scroll_controller = Gtk.EventControllerScroll()
        scroll_controller.set_flags(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll_controller.connect("scroll", self.on_scroll)
        self.add_controller(scroll_controller)

        # Add key controller for keyboard shortcuts
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self.on_key_pressed)
        self.add_controller(key_controller)

        # Add right-click context menu support
        right_click_controller = Gtk.GestureClick()
        right_click_controller.set_button(3)  # Right mouse button
        right_click_controller.connect("pressed", self.on_right_click)
        self.add_controller(right_click_controller)

        # Track which segment is being hovered for visual feedback and keyboard shortcuts
        self.hovered_segment_index = -1

        # Track if hovering over delete button for cursor change
        self.hovering_delete_button = False
        self.delete_button_bounds = None  # Store (x, y, radius) for hit detection

    def set_loading(self, is_loading, message=None):
        """Set the loading state for waveform generation."""
        self.is_loading = is_loading
        self.loading_message = (
            message if message is not None else _("Generating waveform...")
        )
        self.queue_draw()

    def set_waveform(self, data, duration):
        """Set the waveform data to visualize."""
        # Clear loading state when waveform is set
        self.is_loading = False

        with self.waveform_data_lock:
            # Reset position when setting a new waveform
            self.position = 0

            # Reset zoom and viewport when loading new audio
            self.zoom_level = 1.0
            self.viewport_offset = 0.0

            # Important: Do NOT clear markers here, as it interferes with the
            # marker restoration process. Markers will be handled by MainWindow.

            # Clear old waveform data explicitly to release memory
            if self.waveform_data is not None:
                if isinstance(self.waveform_data, dict):
                    # Clear multi-level format
                    if "levels" in self.waveform_data:
                        for level in self.waveform_data["levels"]:
                            del level
                    self.waveform_data.clear()
                del self.waveform_data
                self.waveform_data = None

            # Clear cached surfaces explicitly to release memory
            if self.cached_waveform_surface is not None:
                # Cairo surface cleanup
                self.cached_waveform_surface.finish()
                del self.cached_waveform_surface
                self.cached_waveform_surface = None

            if self.cached_peaks is not None:
                del self.cached_peaks
                self.cached_peaks = None

            # Store waveform data directly - no downsampling
            # This preserves quality for all zoom levels
            if data is not None:
                if isinstance(data, dict) and "levels" in data:
                    # New multi-level format
                    self.waveform_data = data
                    total_samples = sum(len(level) for level in data["levels"])
                    logger.info(
                        f"Multi-level waveform set: {len(data['levels'])} levels, "
                        f"total {total_samples} samples, duration={duration:.2f}s"
                    )
                    for i, (level, rate) in enumerate(
                        zip(data["levels"], data["rates"])
                    ):
                        logger.info(
                            f"  Level {i}: {len(level)} samples @ {rate} Hz ({len(level) * 4 / 1024:.1f} KB)"
                        )
                else:
                    # Old single-level format - wrap it for compatibility
                    self.waveform_data = np.asarray(data, dtype=np.float32)
                    samples_per_second = (
                        len(self.waveform_data) / duration if duration > 0 else 0
                    )
                    logger.info(
                        f"Single-level waveform data set: {len(self.waveform_data)} samples, "
                        f"duration={duration:.2f}s, "
                        f"rate={samples_per_second:.0f} Hz, "
                        f"range=[{np.min(self.waveform_data):.3f}, {np.max(self.waveform_data):.3f}]"
                    )
            else:
                self.waveform_data = None
                logger.info("Waveform data cleared")

            self.duration = duration
            logger.info(f"🔍 VISUALIZER: Set duration={duration:.6f}s")

            # Invalidate viewport cache
            self.cached_viewport_key = None

        # Force garbage collection to release memory
        import gc

        gc.collect()

        # Queue redraw
        self.queue_draw()

    def set_position(self, position):
        """Update the current playback position."""
        self.position = position

        # Auto-follow: adjust viewport during playback OR when seeking outside visible range
        if self.duration > 0 and self.zoom_level > 1.0:
            width = self.get_width()
            if width > 0:
                visible_duration = self.duration / self.zoom_level
                start_time = self.viewport_offset * self.duration
                end_time = start_time + visible_duration

                # Calculate position marker's pixel position
                marker_pixel_x = ((position - start_time) / visible_duration) * width

                # Check if playing for auto-follow behavior
                is_playing = (
                    self.player
                    and hasattr(self.player, "is_playing")
                    and self.player.is_playing()
                )

                # When playing, follow with margin before edge
                if is_playing:
                    edge_threshold = 50  # pixels
                    if marker_pixel_x > width - edge_threshold:
                        # Shift viewport forward to keep marker visible
                        new_start_time = position - visible_duration * 0.3
                        max_offset = 1.0 - 1.0 / self.zoom_level
                        self.viewport_offset = max(
                            0, min(max_offset, new_start_time / self.duration)
                        )
                    elif marker_pixel_x < 0:
                        new_start_time = position - visible_duration * 0.7
                        max_offset = 1.0 - 1.0 / self.zoom_level
                        self.viewport_offset = max(
                            0, min(max_offset, new_start_time / self.duration)
                        )
                else:
                    # When NOT playing (e.g., seeking with buttons), center position if outside visible range
                    if position < start_time or position > end_time:
                        # Center the position in the viewport
                        new_start_time = position - visible_duration * 0.5
                        max_offset = 1.0 - 1.0 / self.zoom_level
                        self.viewport_offset = max(
                            0, min(max_offset, new_start_time / self.duration)
                        )
                        # Invalidate cache to ensure immediate redraw with correct viewport
                        self.cached_viewport_key = None
                        self.cached_waveform_surface = None

        self.queue_draw()

    def clear_waveform(self):
        """Clear the current waveform visualization when audio is removed."""
        # Clear loading state
        self.is_loading = False

        with self.waveform_data_lock:
            # Explicitly delete waveform data to release memory
            if self.waveform_data is not None:
                if isinstance(self.waveform_data, dict):
                    # Clear multi-level format
                    if "levels" in self.waveform_data:
                        for level in self.waveform_data["levels"]:
                            del level
                    self.waveform_data.clear()
                del self.waveform_data

            self.waveform_data = None
            self.position = 0
            self.duration = 0

            # Clear cached surfaces explicitly
            if self.cached_waveform_surface is not None:
                self.cached_waveform_surface.finish()
                del self.cached_waveform_surface
                self.cached_waveform_surface = None

            if self.cached_peaks is not None:
                del self.cached_peaks
                self.cached_peaks = None

            self.cached_viewport_key = None

            # Don't clear markers here, that's handled by MainWindow

        # Force garbage collection to release memory
        import gc

        gc.collect()

        # Queue redraw to display "No audio loaded" message
        self.queue_draw()

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

    def on_right_click(self, gesture, n_press, x, y):
        """Handle right-click for context menu on segments."""
        if not self.markers_enabled or not self.duration > 0:
            return

        # Check if right-clicking on a segment
        segment_index = self._find_segment_at_position(x, y)
        if segment_index >= 0:
            # Show delete prompt for the right-clicked segment
            self._prompt_delete_segment(segment_index)

    def on_click_handler(self, gesture, n_press, x, y):
        """Unified handler for clicks on the waveform."""
        if not self.duration > 0:
            return

        # Ignore clicks on scrollbar - don't create markers or seek
        if self._is_over_scrollbar(x, y):
            return

        # Check if middle button or right button for panning
        button = gesture.get_current_button()

        # Start pan gesture if middle-click (button 2) or right-click (button 3) and zoomed
        if self.zoom_level > 1.0 and (button == 2 or button == 3):
            self.pan_gesture_active = True
            self.pan_gesture_start_x = x
            self.pan_gesture_start_offset = self.viewport_offset
            self.set_cursor(Gdk.Cursor.new_from_name("grabbing"))
            return

        # Calculate position in seconds with improved precision
        width = self.get_width()
        height = self.get_height()

        # Check if click is on timeline ruler (bottom 25px)
        ruler_height = 25
        ruler_y = height - ruler_height
        if y >= ruler_y:
            # If markers are enabled, treat ruler as part of editing zone
            # Otherwise, start scrubbing for seeking
            if not self.markers_enabled:
                # Clicked on ruler - start scrubbing
                self.ruler_scrubbing = True
                visible_duration = self.duration / self.zoom_level
                start_time = self.viewport_offset * self.duration
                position = start_time + (x / width) * visible_duration
                position = round(position, 3)

                if self.seek_position_callback:
                    self.seek_position_callback(
                        position, False
                    )  # MODIFIED: Pass play=False
                return
            # If markers enabled, continue to segment editing logic below

        # Account for zoom and pan when calculating position
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        position = start_time + (x / width) * visible_duration
        position = round(position, 3)  # Round to millisecond precision
        logger.info(
            f"🔍 CLICK: x={x:.1f}, width={width}, duration={self.duration:.6f}, visible_dur={visible_duration:.6f}, start={start_time:.6f}, position={position:.6f}"
        )

        # Define interactive zones - BOTTOM 40% for segment manipulation, TOP 60% for seeking
        manipulation_zone_start = height * 0.6  # Bottom 40% is for manipulation
        in_manipulation_zone = y >= manipulation_zone_start

        # HIGHEST PRIORITY: Always check confirm/cancel buttons first when in dialog modes
        if self.marker_mode == MarkerMode.CONFIRM:
            if self._check_confirm_buttons(x, y):
                return  # Button was clicked, exit early

        elif (
            self.marker_mode == MarkerMode.DELETE_PROMPT
            or self.marker_mode == MarkerMode.DELETE_ALL_CONFIRM
        ):
            if self._check_delete_buttons(x, y):
                return  # Button was clicked, exit early

            # If clicked elsewhere in delete_prompt mode, exit delete mode
            if self.marker_mode == MarkerMode.DELETE_PROMPT:
                self._cancel_delete_prompt()
                return  # Exit after canceling - don't process as a normal click
            # If clicked elsewhere in delete_all_confirm mode, just exit confirmation
            elif self.marker_mode == MarkerMode.DELETE_ALL_CONFIRM:
                self.marker_mode = MarkerMode.DELETE_PROMPT  # Go back to delete prompt
                self.queue_draw()
                return

        # Store the click position for potential seeking later
        clicked_position = position

        # Handle marker/segment interaction ONLY in manipulation zone
        if self.markers_enabled and in_manipulation_zone:
            # HIGHEST PRIORITY: Check if clicking on delete button
            if self.hovering_delete_button and self.hovered_segment_index >= 0:
                self._prompt_delete_segment(self.hovered_segment_index)
                return

            # First check for marker edge - highest priority for drag
            marker_info = self._find_marker_at_position(x, y)
            if marker_info:
                # Edges always start dragging immediately
                self.is_dragging_marker = True
                self.dragging_pair_index = marker_info["index"]
                self.dragging_marker_type = marker_info["type"]
                # Notify that marker dragging started
                if self.marker_drag_callback:
                    self.marker_drag_callback(True)
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
                # Cache segment length for later use (removed unused assignment)
                self.drag_start_pos = None  # Will be set when drag actually starts
                # Don't show delete prompt immediately, let drag_end decide if it was a click
                return  # Exit early

            # Handle marker placement based on mode (ONLY in manipulation zone)
            if self.marker_mode == MarkerMode.START:
                self.add_start_marker(position)
            elif self.marker_mode == MarkerMode.STOP:
                self.add_stop_marker(position)

        # Handle seeking - update position first to ensure viewport is properly adjusted
        if self.seek_position_callback:
            # Determine if playback should start after seeking
            should_play = not in_manipulation_zone and (
                self.player and not self.player.is_playing()
            )

            # Ensure the viewport is properly adjusted before seeking
            if self.duration > 0 and self.zoom_level > 1.0:
                visible_duration = self.duration / self.zoom_level
                start_time = self.viewport_offset * self.duration
                end_time = start_time + visible_duration

                # If clicked position is outside the current visible range, adjust viewport first
                if clicked_position < start_time or clicked_position > end_time:
                    # Center the clicked position in the viewport
                    new_start_time = clicked_position - visible_duration * 0.5
                    max_offset = 1.0 - 1.0 / self.zoom_level
                    self.viewport_offset = max(
                        0, min(max_offset, new_start_time / self.duration)
                    )
                    # Invalidate cache to force immediate redraw
                    self.cached_viewport_key = None
                    self.cached_waveform_surface = None
                    # Force an immediate redraw to update the viewport before seeking
                    self.queue_draw()

            self.seek_position_callback(clicked_position, should_play)

        # REMOVED: The visualizer no longer controls the player directly.
        # This logic is now centralized in MainWindow.

    def on_release_handler(self, gesture, n_press, x, y):
        """Handle mouse release events."""
        # End ruler scrubbing if active
        if self.ruler_scrubbing:
            self.ruler_scrubbing = False
            return

        # End pan gesture if active
        if self.pan_gesture_active:
            self.pan_gesture_active = False
            self.set_cursor(None)
            return

        # If we were dragging, notify about the marker update
        if self.is_dragging_marker:
            if self.marker_updated_callback:
                self.marker_updated_callback(self.get_marker_pairs())

            # Notify that marker dragging ended
            if self.marker_drag_callback:
                self.marker_drag_callback(False)

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
        # Check if dragging scrollbar
        if self._is_over_scrollbar_thumb(start_x, start_y):
            self.is_dragging_scrollbar = True
            self.scrollbar_drag_start_x = start_x
            self.scrollbar_drag_start_offset = self.viewport_offset
            self.queue_draw()
            return

        # Check for Ctrl+drag panning (left-click with Ctrl)
        event = gesture.get_last_event(gesture.get_last_updated_sequence())
        if event and self.zoom_level > 1.0:
            ctrl_pressed = (
                event.get_modifier_state() & Gdk.ModifierType.CONTROL_MASK
            ) != 0
            button = gesture.get_current_button()

            if ctrl_pressed and button == 1:  # Ctrl + left-click
                self.pan_gesture_active = True
                self.pan_gesture_start_x = start_x
                self.pan_gesture_start_offset = self.viewport_offset
                self.set_cursor(Gdk.Cursor.new_from_name("grabbing"))
                return

        # If we have a potential segment drag, start tracking for REAL drag
        # We don't want to immediately enter drag mode - that happens in update
        pass

    def on_drag_update(self, gesture, offset_x, offset_y):
        """Handle drag movement updates."""
        # Handle scrollbar dragging (highest priority)
        if self.is_dragging_scrollbar:
            width = self.get_width()
            scrollbar_bounds = self._get_scrollbar_bounds(width, self.get_height())
            if scrollbar_bounds:
                # Calculate new viewport offset from drag distance
                visible_fraction = 1.0 / self.zoom_level
                thumb_width = max(30, scrollbar_bounds["width"] * visible_fraction)
                max_thumb_travel = scrollbar_bounds["width"] - thumb_width

                if max_thumb_travel > 0:
                    # Convert pixel movement to viewport offset
                    max_offset = 1.0 - visible_fraction
                    delta_offset = (offset_x / max_thumb_travel) * max_offset
                    new_offset = self.scrollbar_drag_start_offset + delta_offset
                    self.viewport_offset = max(0, min(max_offset, new_offset))
                    self.queue_draw()
            return

        # Handle ruler scrubbing (highest priority when active)
        if self.ruler_scrubbing:
            ok, start_x, start_y = gesture.get_start_point()
            if ok:
                width = self.get_width()
                current_x = start_x + offset_x

                # Calculate seek position
                visible_duration = self.duration / self.zoom_level
                start_time = self.viewport_offset * self.duration
                position = start_time + (current_x / width) * visible_duration
                position = max(0, min(self.duration, position))
                position = round(position, 3)

                if self.seek_position_callback:
                    self.seek_position_callback(
                        position, False
                    )  # MODIFIED: Pass play=False
            return

        # Handle pan gesture first (highest priority when active)
        if self.pan_gesture_active:
            width = self.get_width()
            # Calculate pan delta as fraction of visible duration
            delta_fraction = -offset_x / width  # Negative for natural scrolling

            # Apply delta to viewport offset
            new_offset = (
                self.pan_gesture_start_offset + delta_fraction / self.zoom_level
            )

            # Clamp to valid range
            max_offset = 1.0 - 1.0 / self.zoom_level
            self.viewport_offset = max(0, min(max_offset, new_offset))

            self.queue_draw()
            return

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

                # Notify that marker dragging started (whole segment drag)
                if self.marker_drag_callback:
                    self.marker_drag_callback(True)

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
            # Account for zoom when calculating position
            visible_duration = self.duration / self.zoom_level
            start_time = self.viewport_offset * self.duration
            new_position = start_time + (current_x / width) * visible_duration
            new_position = max(0, min(new_position, self.duration))
            new_position = round(new_position, 3)

            # When dragging start marker, ensure it doesn't go past the stop marker
            if pair["stop"] is not None:
                new_position = min(new_position, pair["stop"] - 0.1)
            pair["start"] = new_position
            pair["start_str"] = self._format_time(new_position)

            # Force redraw to show updated position
            self.queue_draw()

            # Always seek to marker position when dragging to help identify cut point
            # This allows audio to follow marker movements in all modes
            if self.seek_position_callback:
                self.seek_position_callback(
                    new_position, False
                )  # MODIFIED: Pass play=False

        elif self.dragging_marker_type == "stop":
            # Account for zoom when calculating position
            visible_duration = self.duration / self.zoom_level
            start_time = self.viewport_offset * self.duration
            new_position = start_time + (current_x / width) * visible_duration
            new_position = max(0, min(new_position, self.duration))
            new_position = round(new_position, 3)

            # When dragging stop marker, ensure it doesn't go before the start marker
            if pair["start"] is not None:
                new_position = max(new_position, pair["start"] + 0.1)
            pair["stop"] = new_position
            pair["stop_str"] = self._format_time(new_position)

            # Force redraw to show updated position
            self.queue_draw()

            # Always seek to marker position when dragging to help identify cut point
            # This allows audio to follow marker movements in all modes
            if self.seek_position_callback:
                self.seek_position_callback(
                    new_position, False
                )  # MODIFIED: Pass play=False

        elif self.dragging_marker_type == "segment" and self.drag_start_pos is not None:
            # For segment movement, use a proportion that better matches mouse movement
            # Calculate the delta in screen space, then convert to time delta
            pixel_delta = offset_x
            visible_duration = self.duration / self.zoom_level
            time_delta = (pixel_delta / width) * visible_duration

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

            # Allow segments to overlap - no collision detection
            # Update the segment position freely
            pair["start"] = new_start
            pair["stop"] = new_stop
            pair["start_str"] = self._format_time(new_start)
            pair["stop_str"] = self._format_time(new_stop)

            # Force redraw to show updated position
            self.queue_draw()

            # If we have a seek callback, update the playback position too
            if self.seek_position_callback:
                # Seek to start of segment instead of middle for better context
                self.seek_position_callback(
                    new_start, False
                )  # MODIFIED: Pass play=False

    def on_drag_end(self, gesture, offset_x, offset_y):
        """Handle end of drag operation."""
        # End scrollbar dragging
        if self.is_dragging_scrollbar:
            self.is_dragging_scrollbar = False
            self.queue_draw()
            return

        # End pan gesture if active
        if self.pan_gesture_active:
            self.pan_gesture_active = False
            self.set_cursor(None)

        # End ruler scrubbing if active
        if self.ruler_scrubbing:
            self.ruler_scrubbing = False

        # Reset tracking variables (removed delete prompt on segment click)
        self.potential_drag_segment = None
        self.drag_start_pos = None

        # Handle normal drag cleanup
        # The cleanup is handled in on_release_handler

    def on_scroll(self, controller, dx, dy):
        """Handle mouse wheel scrolling for zoom and horizontal panning."""
        if not self.duration > 0:
            return False

        # Update hover position from current event if possible
        event = controller.get_current_event()
        if event:
            result = event.get_position()
            if result and len(result) == 3:
                success, mouse_x, mouse_y = result
                if success:
                    self.hover_x = mouse_x

        # Check for Shift key for horizontal panning
        modifiers = event.get_modifier_state() if event else 0
        shift_pressed = (modifiers & Gdk.ModifierType.SHIFT_MASK) != 0

        # Horizontal panning with Shift+Scroll or trackpad horizontal scroll
        if shift_pressed or (abs(dx) > abs(dy) and abs(dx) > 0):
            if self.zoom_level > 1.0:
                # Pan horizontally
                pan_speed = 0.05  # Adjust sensitivity
                scroll_delta = dx if abs(dx) > abs(dy) else dy
                delta_offset = scroll_delta * pan_speed / self.zoom_level

                max_offset = 1.0 - 1.0 / self.zoom_level
                new_offset = self.viewport_offset + delta_offset
                self.viewport_offset = max(0, min(max_offset, new_offset))

                self.queue_draw()
                return True
            return False

        # Vertical scrolling = zoom
        zoom_factor = 1.2  # 20% zoom per scroll step

        if dy < 0:  # Scroll up = zoom in
            new_zoom = self.zoom_level * zoom_factor
        else:  # Scroll down = zoom out
            new_zoom = self.zoom_level / zoom_factor

        # Use the unified zoom method (same as slider)
        self.set_zoom_level(new_zoom, use_mouse_position=True)

        # Notify zoom change callback
        if self.zoom_changed_callback:
            self.zoom_changed_callback(self.zoom_level)

        return True  # Event handled

    def on_key_pressed(self, controller, keyval, keycode, state):
        """Handle keyboard shortcuts for zoom and segment deletion."""
        if not self.duration > 0:
            return False

        # Check for modifier keys
        ctrl_pressed = (state & Gdk.ModifierType.CONTROL_MASK) != 0

        if ctrl_pressed:
            # Ctrl + Plus/Equal = Zoom in
            if (
                keyval == Gdk.KEY_plus
                or keyval == Gdk.KEY_equal
                or keyval == Gdk.KEY_KP_Add
            ):
                self.zoom_in()
                return True
            # Ctrl + Minus = Zoom out
            elif keyval == Gdk.KEY_minus or keyval == Gdk.KEY_KP_Subtract:
                self.zoom_out()
                return True
            # Ctrl + 0 = Reset zoom
            elif keyval == Gdk.KEY_0 or keyval == Gdk.KEY_KP_0:
                self.reset_zoom()
                return True

        # Arrow keys for horizontal panning when zoomed
        if self.zoom_level > 1.0:
            pan_amount = 0.05 / self.zoom_level  # Smaller steps when more zoomed
            max_offset = 1.0 - 1.0 / self.zoom_level

            if keyval == Gdk.KEY_Left:
                # Pan left
                self.viewport_offset = max(0, self.viewport_offset - pan_amount)
                self.queue_draw()
                return True
            elif keyval == Gdk.KEY_Right:
                # Pan right
                self.viewport_offset = min(
                    max_offset, self.viewport_offset + pan_amount
                )
                self.queue_draw()
                return True

        # Delete/Backspace key - delete hovered segment
        if self.markers_enabled and self.hovered_segment_index >= 0:
            if keyval == Gdk.KEY_Delete or keyval == Gdk.KEY_BackSpace:
                self._prompt_delete_segment(self.hovered_segment_index)
                return True

        return False

    def set_zoom_level(self, new_zoom, use_mouse_position=True):
        """
        Set the zoom level with intelligent anchor positioning.

        Args:
            new_zoom: The new zoom level to set
            use_mouse_position: If True, try to zoom around last known mouse position;
                               if False or mouse unavailable, zoom around playback position or center
        """
        if not self.duration > 0:
            return

        new_zoom = max(self.min_zoom, min(self.max_zoom, new_zoom))
        if new_zoom == self.zoom_level:
            return

        # Determine anchor fraction (0.0 to 1.0 across visible area)
        mouse_fraction = None

        # First priority: Use hover position if available and requested
        if use_mouse_position and self.hover_x is not None:
            width = self.get_width()
            if width > 0:
                mouse_fraction = self.hover_x / width

        # Second priority: Use playback position if available
        if mouse_fraction is None and self.position > 0:
            # Calculate where playback position appears in current view
            visible_duration = self.duration / self.zoom_level
            start_time = self.viewport_offset * self.duration
            end_time = start_time + visible_duration
            # Check if playback position is in visible range
            if start_time <= self.position <= end_time:
                # Calculate fraction of playback position in visible area
                mouse_fraction = (self.position - start_time) / visible_duration

        # Third priority: Default to center
        if mouse_fraction is None:
            mouse_fraction = 0.5

        # Calculate anchor time
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        anchor_time = start_time + mouse_fraction * visible_duration

        # Update zoom
        self.zoom_level = new_zoom

        # Adjust viewport to keep anchor position
        new_visible_duration = self.duration / self.zoom_level
        new_start_time = anchor_time - mouse_fraction * new_visible_duration

        # Clamp viewport offset
        self.viewport_offset = max(
            0, min(1.0 - 1.0 / self.zoom_level, new_start_time / self.duration)
        )

        self.queue_draw()

    def zoom_in(self, factor=1.2):
        """Zoom in by the specified factor."""
        new_zoom = self.zoom_level * factor
        new_zoom = max(self.min_zoom, min(self.max_zoom, new_zoom))

        if new_zoom != self.zoom_level:
            # Zoom towards center
            visible_duration = self.duration / self.zoom_level
            start_time = self.viewport_offset * self.duration
            center_time = start_time + visible_duration / 2

            self.zoom_level = new_zoom

            # Adjust viewport to keep center position
            new_visible_duration = self.duration / self.zoom_level
            new_start_time = center_time - new_visible_duration / 2

            self.viewport_offset = max(
                0, min(1.0 - 1.0 / self.zoom_level, new_start_time / self.duration)
            )

            # Notify zoom change callback
            if self.zoom_changed_callback:
                self.zoom_changed_callback(self.zoom_level)

            self.queue_draw()

    def zoom_out(self, factor=1.2):
        """Zoom out by the specified factor."""
        new_zoom = self.zoom_level / factor
        new_zoom = max(self.min_zoom, min(self.max_zoom, new_zoom))

        if new_zoom != self.zoom_level:
            # Zoom from center
            visible_duration = self.duration / self.zoom_level
            start_time = self.viewport_offset * self.duration
            center_time = start_time + visible_duration / 2

            self.zoom_level = new_zoom

            # Adjust viewport to keep center position
            new_visible_duration = self.duration / self.zoom_level
            new_start_time = center_time - new_visible_duration / 2

            self.viewport_offset = max(
                0, min(1.0 - 1.0 / self.zoom_level, new_start_time / self.duration)
            )

            # Notify zoom change callback
            if self.zoom_changed_callback:
                self.zoom_changed_callback(self.zoom_level)

            self.queue_draw()

    def reset_zoom(self):
        """Reset zoom to default level."""
        if self.zoom_level != 1.0:
            self.zoom_level = 1.0
            self.viewport_offset = 0.0

            # Notify zoom change callback
            if self.zoom_changed_callback:
                self.zoom_changed_callback(self.zoom_level)

            self.queue_draw()

    def on_motion(self, controller, x, y):
        """Handle mouse motion for hover effects."""
        # Get dimensions for all cursor checks
        width = self.get_width()
        height = self.get_height()

        # Check if hovering over scrollbar (when zoomed)
        was_hovering_scrollbar = self.hovering_scrollbar
        self.hovering_scrollbar = self._is_over_scrollbar(x, y)

        if was_hovering_scrollbar != self.hovering_scrollbar:
            self.queue_draw()  # Redraw to show hover effect

        # Set pointer cursor when over scrollbar
        if self.hovering_scrollbar:
            if self._is_over_scrollbar_thumb(x, y):
                self.set_cursor(
                    Gdk.Cursor.new_from_name("hand2")
                )  # Hand cursor for draggable thumb
            else:
                self.set_cursor(
                    Gdk.Cursor.new_from_name("pointer")
                )  # Pointer cursor for scrollbar track
            return  # Exit early when over scrollbar

        # Calculate hover time position and trigger redraw for hover line display
        if self.duration > 0:
            self.hover_x = x
            # Account for zoom and pan
            visible_duration = self.duration / self.zoom_level
            start_time = self.viewport_offset * self.duration
            self.hover_time = start_time + (x / width) * visible_duration

            # Track which segment is being hovered for keyboard shortcuts and visual feedback
            old_hovered = self.hovered_segment_index
            self.hovered_segment_index = self._find_segment_at_position(x, y)

            # Check if hovering over delete button (highest priority for cursor)
            was_hovering_delete = self.hovering_delete_button
            self.hovering_delete_button = self._is_over_delete_button(x, y)

            # Redraw if hover state changed or every movement for hover line
            if (
                old_hovered != self.hovered_segment_index
                or was_hovering_delete != self.hovering_delete_button
                or self.hover_time is not None
            ):
                self.queue_draw()

        if not self.markers_enabled or self.duration <= 0:
            # Check if hovering over ruler for seek cursor
            ruler_height = 25
            ruler_y = height - ruler_height
            if y >= ruler_y:
                self.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
                return

            # Check if Ctrl is pressed for pan cursor
            if self.zoom_level > 1.0:
                event = controller.get_current_event()
                if event:
                    ctrl_pressed = (
                        event.get_modifier_state() & Gdk.ModifierType.CONTROL_MASK
                    ) != 0
                    if ctrl_pressed:
                        self.set_cursor(Gdk.Cursor.new_from_name("grab"))
                        return
            return

        # Define the manipulation zone boundary FIRST
        manipulation_zone_start = height * 0.6  # Bottom 40% is manipulation zone
        in_manipulation_zone = y >= manipulation_zone_start

        # If in delete prompt or confirm mode, only check for button hover
        if self.marker_mode in [MarkerMode.DELETE_PROMPT, MarkerMode.CONFIRM]:
            # Check for button hover first - takes precedence
            if (
                self.marker_mode == MarkerMode.DELETE_PROMPT
                and self._check_delete_buttons(x, y, just_check=True)
            ) or (
                self.marker_mode == MarkerMode.CONFIRM
                and self._check_confirm_buttons(x, y, just_check=True)
            ):
                self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
                return
            else:
                # Reset cursor when not over buttons in these modes
                self.set_cursor(None)
                return

        # Normal mode - check if hovering over a marker edge for dragging (HIGHEST PRIORITY)
        if not self.is_dragging_marker:
            # HIGHEST PRIORITY: Check if hovering over delete button
            if self.hovering_delete_button and in_manipulation_zone:
                self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
                return

            # Only show special cursors in the manipulation zone
            marker_info = self._find_marker_at_position(x, y)
            if marker_info and in_manipulation_zone:
                # Show resize cursor when hovering over a marker edge in manipulation zone
                if marker_info["type"] == "start":
                    self.set_cursor(Gdk.Cursor.new_from_name("w-resize"))
                else:
                    self.set_cursor(Gdk.Cursor.new_from_name("e-resize"))
                return

            # Check if hovering over segment body (SECOND PRIORITY)
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

            # Show pointer cursor when hovering over a segment (indicating clickable)
            if pair_index >= 0 and in_manipulation_zone:
                self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            # Default cursor for manipulation zone (including ruler area)
            elif in_manipulation_zone:
                # In manipulation zone - show crosshair for editing (including ruler)
                self.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
            else:
                # Outside manipulation zone - reset cursor
                self.set_cursor(None)

    def _find_segment_at_position(self, x, y):
        """Find if the given x,y position is on an existing segment."""
        width = self.get_width()
        height = self.get_height()

        # Only check the bottom 40% - our interaction zone
        if y < height * 0.6:
            return -1

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
        height = self.get_height()

        # Only check bottom 40% - our segment interaction zone
        if y < height * 0.6:
            return None

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

        # Define button regions (higher position to match confirm buttons)
        button_y = height * 0.2  # Match the confirm buttons position
        button_height = 30  # Match the confirm buttons height

        # Check if we're in delete all confirmation mode
        if self.marker_mode == MarkerMode.DELETE_ALL_CONFIRM:
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
                    self.marker_mode = (
                        MarkerMode.DELETE_PROMPT
                    )  # Go back to delete prompt
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
        print("Visualizer: Clearing all markers")
        self.marker_pairs = []
        self.current_pair_index = -1
        self.marker_mode = MarkerMode.START
        self.highlighted_pair = -1
        # Notify listener if any
        if self.marker_updated_callback:
            self.marker_updated_callback([])

        # Redraw
        self.queue_draw()

    def connect_seek_handler(self, callback):
        """Connect a handler for seek events."""
        self.seek_position_callback = callback

    def connect_marker_drag_handler(self, callback):
        """Connect a handler for marker drag state changes.

        Callback will be called with True when dragging starts, False when it ends.
        """
        self.marker_drag_callback = callback

    def set_selection_only_mode(self, enabled):
        """Set whether selection-only playback mode is active.

        When enabled, marker dragging won't seek to avoid audio jumps.
        When disabled, marker dragging will seek to help identify cut points.
        """
        self.selection_only_mode = enabled

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

    def _draw_timeline_ruler(self, cr, width, height):
        """Draw a timeline ruler at the bottom showing time markings."""
        # Calculate visible time range
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

        # Define ruler area at the bottom
        ruler_height = 25
        ruler_y = height - ruler_height

        # Draw ruler background
        cr.set_source_rgba(0.15, 0.15, 0.15, 0.9)
        cr.rectangle(0, ruler_y, width, ruler_height)
        cr.fill()

        # Calculate appropriate time interval for marks
        # Aim for marks every 50-80 pixels
        target_mark_spacing = 60  # pixels
        marks_count = max(1, int(width / target_mark_spacing))
        time_per_mark = visible_duration / marks_count

        # Round to nice intervals (1s, 5s, 10s, 30s, 1m, 5m, etc.)
        nice_intervals = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600]
        time_per_mark = min(nice_intervals, key=lambda x: abs(x - time_per_mark))

        # Draw marks
        cr.set_source_rgba(0.6, 0.6, 0.6, 1.0)
        cr.set_line_width(1)
        cr.set_font_size(9)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)

        # Start from the first mark that's visible
        first_mark_time = int(start_time / time_per_mark) * time_per_mark

        current_mark_time = first_mark_time
        while current_mark_time <= end_time:
            if current_mark_time >= start_time:
                # Calculate x position for this mark
                x_pos = ((current_mark_time - start_time) / visible_duration) * width

                # Draw tick mark
                is_major = (
                    current_mark_time % (time_per_mark * 5)
                ) < 0.1  # Major marks every 5 intervals
                tick_height = 8 if is_major else 4
                cr.move_to(x_pos, ruler_y)
                cr.line_to(x_pos, ruler_y + tick_height)
                cr.stroke()

                # Draw time label for major marks
                if is_major:
                    time_str = format_time_short(current_mark_time)
                    text_extents = cr.text_extents(time_str)
                    cr.move_to(x_pos - text_extents.width / 2, ruler_y + 18)
                    cr.show_text(time_str)

            current_mark_time += time_per_mark

        # Draw top border line
        cr.set_source_rgba(0.4, 0.4, 0.4, 1.0)
        cr.set_line_width(1)
        cr.move_to(0, ruler_y)
        cr.line_to(width, ruler_y)
        cr.stroke()

    def _draw_playback_time_display(self, cr, width, height):
        """Draw current playback time as a clear overlay."""
        # Format current time
        time_str = format_time_short(self.position)
        duration_str = format_time_short(self.duration)
        display_str = f"{time_str} / {duration_str}"

        # Set font
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(11)
        text_extents = cr.text_extents(display_str)

        # Position in top-left corner (moved 20px down)
        padding = 8
        box_x = padding
        box_y = padding + 20
        box_width = text_extents.width + 12
        box_height = 20

        # Draw semi-transparent background
        cr.set_source_rgba(0, 0, 0, 0.75)
        self._draw_rounded_rect(cr, box_x, box_y, box_width, box_height, 4)
        cr.fill()

        # Draw text
        cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)
        cr.move_to(box_x + 6, box_y + 14)
        cr.show_text(display_str)

    def _draw_rounded_rect(self, cr, x, y, width, height, radius):
        """Draw a rounded rectangle path."""
        degrees = 3.14159 / 180.0

        cr.new_sub_path()
        cr.arc(x + width - radius, y + radius, radius, -90 * degrees, 0 * degrees)
        cr.arc(
            x + width - radius, y + height - radius, radius, 0 * degrees, 90 * degrees
        )
        cr.arc(x + radius, y + height - radius, radius, 90 * degrees, 180 * degrees)
        cr.arc(x + radius, y + radius, radius, 180 * degrees, 270 * degrees)
        cr.close_path()

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
        button_bg = (0.2, 0.2, 0.2, 0.8)  # Dark gray for buttons
        dialog_overlay = (0, 0, 0, 0.5)  # Semi-transparent black for dialog overlay

        # Calculate visible time range
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

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

                # Add pair number
                cr.set_source_rgba(1, 1, 1, 0.8)
                cr.set_font_size(14)
                cr.move_to(x_start + 5, height - 8)
                cr.show_text(f"#{i + 1}")

                # Draw delete button when hovering over this segment
                if i == self.hovered_segment_index and self.marker_mode not in [
                    MarkerMode.DELETE_PROMPT,
                    MarkerMode.DELETE_ALL_CONFIRM,
                    MarkerMode.CONFIRM,
                ]:
                    self._draw_segment_delete_button(cr, x_start, x_stop, height, i)

        # IMPORTANT: Now draw the dialogs and buttons AFTER all segments
        # to ensure they're always on top and clickable

        # Draw confirmation UI if in confirm mode
        if self.marker_mode == MarkerMode.CONFIRM and self.current_pair_index >= 0:
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
        elif (
            self.marker_mode == MarkerMode.DELETE_PROMPT and self.highlighted_pair >= 0
        ):
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
            button_y = height * 0.2 - 4  # Higher position (moved up 4 pixels)
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
        elif self.marker_mode == MarkerMode.DELETE_ALL_CONFIRM:
            # Draw strong darkened overlay
            cr.set_source_rgba(0, 0, 0, 0.5)  # Semi-transparent black dialog overlay
            cr.rectangle(0, 0, width, height)
            cr.fill()

            # Draw confirmation message with background - MOVED LOWER
            cr.set_source_rgba(0, 0, 0, 0.7)  # Black background
            msg = "Delete ALL segments? This cannot be undone."
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
            button_y = height * 0.2 - 4  # Higher position (moved up 4 pixels)
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

    def _draw_segment_delete_button(self, cr, x_start, x_stop, height, segment_index):
        """Draw a visual delete button (×) on the hovered segment."""
        # Calculate button position - top-right corner of segment
        button_size = 16
        button_x = x_stop - button_size - 5
        button_y = height - button_size - 10  # Moved up 5 pixels (was -5, now -10)

        # Ensure button is visible within segment
        segment_width = x_stop - x_start
        if segment_width < button_size + 10:
            # For narrow segments, center the button
            button_x = x_start + (segment_width - button_size) / 2

        # Draw button background with gradient
        cr.save()

        # Create circular button background
        button_center_x = button_x + button_size / 2
        button_center_y = button_y + button_size / 2
        button_radius = button_size / 2

        # Store button bounds for hit detection
        self.delete_button_bounds = (button_center_x, button_center_y, button_radius)

        # Check if hovering over this button for enhanced visual feedback
        is_button_hovered = self.hovering_delete_button

        # Draw shadow for depth (bigger if hovered)
        shadow_offset = 2 if is_button_hovered else 1
        cr.set_source_rgba(0, 0, 0, 0.5 if is_button_hovered else 0.4)
        cr.arc(
            button_center_x + shadow_offset,
            button_center_y + shadow_offset,
            button_radius,
            0,
            2 * 3.14159,
        )
        cr.fill()

        # Draw button circle with solid red color (brighter when hovered)
        if is_button_hovered:
            # Brighter solid red when hovered
            cr.set_source_rgba(0.95, 0.25, 0.25, 1.0)  # Bright red
        else:
            # Normal solid red
            cr.set_source_rgba(0.85, 0.15, 0.15, 0.95)  # Dark red

        cr.arc(button_center_x, button_center_y, button_radius, 0, 2 * 3.14159)
        cr.fill()

        # Draw border (thicker when hovered)
        cr.set_source_rgba(0.5, 0.05, 0.05, 0.9)
        cr.set_line_width(2.0 if is_button_hovered else 1.5)
        cr.arc(button_center_x, button_center_y, button_radius - 0.75, 0, 2 * 3.14159)
        cr.stroke()

        # Draw × symbol (thicker when hovered)
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.set_line_width(2.5 if is_button_hovered else 2.0)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)  # Draw X with proper proportions
        offset = button_radius * 0.4
        cr.move_to(button_center_x - offset, button_center_y - offset)
        cr.line_to(button_center_x + offset, button_center_y + offset)
        cr.stroke()

        cr.move_to(button_center_x + offset, button_center_y - offset)
        cr.line_to(button_center_x - offset, button_center_y + offset)
        cr.stroke()

        cr.restore()

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

    def _draw_scrollbar(self, cr, width, height):
        """Draw a simulated horizontal scrollbar for zoomed view."""
        # Calculate scrollbar dimensions
        scrollbar_y = height - self.scrollbar_height  # No bottom margin
        scrollbar_width = width - (2 * self.scrollbar_margin)
        scrollbar_x = self.scrollbar_margin

        # Draw scrollbar track (background)
        cr.set_source_rgba(0.2, 0.2, 0.2, 0.3)  # Dark semi-transparent
        cr.rectangle(scrollbar_x, scrollbar_y, scrollbar_width, self.scrollbar_height)
        cr.fill()

        # Calculate thumb (handle) dimensions
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

        # Draw thumb (handle) with hover effect
        if self.hovering_scrollbar or self.is_dragging_scrollbar:
            cr.set_source_rgba(0.6, 0.7, 0.9, 0.9)  # Brighter when hovering/dragging
        else:
            cr.set_source_rgba(0.5, 0.6, 0.8, 0.7)  # Normal state

        cr.rectangle(thumb_x, scrollbar_y + 2, thumb_width, self.scrollbar_height - 4)
        cr.fill()

        # Draw border around thumb for definition
        cr.set_source_rgba(0.7, 0.8, 1.0, 0.5)
        cr.set_line_width(1)
        cr.rectangle(thumb_x, scrollbar_y + 2, thumb_width, self.scrollbar_height - 4)
        cr.stroke()

    def _get_scrollbar_bounds(self, width, height):
        """Get the bounds of the scrollbar area for hit detection."""
        if self.zoom_level <= 1.0:
            return None

        scrollbar_y = height - self.scrollbar_height  # No bottom margin
        scrollbar_width = width - (2 * self.scrollbar_margin)
        scrollbar_x = self.scrollbar_margin

        return {
            "x": scrollbar_x,
            "y": scrollbar_y,
            "width": scrollbar_width,
            "height": self.scrollbar_height,
        }

    def _get_scrollbar_thumb_bounds(self, width, height):
        """Get the bounds of the scrollbar thumb for hit detection."""
        if self.zoom_level <= 1.0:
            return None

        scrollbar_bounds = self._get_scrollbar_bounds(width, height)
        if not scrollbar_bounds:
            return None

        # Calculate thumb dimensions
        visible_fraction = 1.0 / self.zoom_level
        thumb_width = max(30, scrollbar_bounds["width"] * visible_fraction)

        # Thumb position
        max_offset = 1.0 - visible_fraction
        if max_offset > 0:
            thumb_fraction = self.viewport_offset / max_offset
        else:
            thumb_fraction = 0

        max_thumb_x = scrollbar_bounds["width"] - thumb_width
        thumb_x = scrollbar_bounds["x"] + (thumb_fraction * max_thumb_x)

        return {
            "x": thumb_x,
            "y": scrollbar_bounds["y"] + 2,
            "width": thumb_width,
            "height": scrollbar_bounds["height"] - 4,
        }

    def _is_over_scrollbar(self, x, y):
        """Check if mouse is over scrollbar area."""
        width = self.get_width()
        height = self.get_height()
        bounds = self._get_scrollbar_bounds(width, height)

        if not bounds:
            return False

        return (
            bounds["x"] <= x <= bounds["x"] + bounds["width"]
            and bounds["y"] <= y <= bounds["y"] + bounds["height"]
        )

    def _is_over_scrollbar_thumb(self, x, y):
        """Check if mouse is over scrollbar thumb."""
        width = self.get_width()
        height = self.get_height()
        thumb_bounds = self._get_scrollbar_thumb_bounds(width, height)

        if not thumb_bounds:
            return False

        return (
            thumb_bounds["x"] <= x <= thumb_bounds["x"] + thumb_bounds["width"]
            and thumb_bounds["y"] <= y <= thumb_bounds["y"] + thumb_bounds["height"]
        )

    def _draw_loading_indicator(self, cr, width, height):
        """Draw a loading indicator with spinner and message."""
        import math
        import time

        # Draw semi-transparent overlay
        cr.set_source_rgba(0.05, 0.05, 0.05, 0.95)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        # Calculate center position
        center_x = width / 2
        center_y = height / 2

        # Draw animated spinner
        spinner_radius = 30
        num_dots = 8
        current_time = time.time()
        rotation = (current_time * 2) % (2 * math.pi)  # Rotate at 2 rad/s

        for i in range(num_dots):
            angle = rotation + (i * 2 * math.pi / num_dots)
            x = center_x + spinner_radius * math.cos(angle)
            y = center_y + spinner_radius * math.sin(angle)

            # Fade dots based on position
            alpha = 0.3 + 0.7 * (i / num_dots)
            dot_radius = 4

            cr.set_source_rgba(0.2, 0.7, 1.0, alpha)  # Blue dots with varying opacity
            cr.arc(x, y, dot_radius, 0, 2 * math.pi)
            cr.fill()

        # Draw loading message
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(16)
        text_extents = cr.text_extents(self.loading_message)

        # Draw text shadow for depth
        cr.set_source_rgba(0, 0, 0, 0.5)
        cr.move_to(
            center_x - text_extents.width / 2 + 1, center_y + spinner_radius + 35 + 1
        )
        cr.show_text(self.loading_message)

        # Draw actual text
        cr.set_source_rgba(0.8, 0.8, 0.8, 1.0)
        cr.move_to(center_x - text_extents.width / 2, center_y + spinner_radius + 35)
        cr.show_text(self.loading_message)

        # Queue another redraw to animate the spinner
        GLib.timeout_add(50, self.queue_draw)  # Update at ~20 FPS

    def on_draw(self, area, cr, width, height):
        """Draw the waveform visualization."""
        # Draw background
        cr.set_source_rgb(*self.bg_color)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        # Draw loading indicator if currently loading
        if self.is_loading:
            self._draw_loading_indicator(cr, width, height)
            return

        # Draw waveform if we have data
        with self.waveform_data_lock:
            # Calculate visible portion based on zoom
            visible_duration = (
                self.duration / self.zoom_level if self.duration > 0 else 0
            )
            start_time = (
                self.viewport_offset * self.duration if self.duration > 0 else 0
            )
            end_time = start_time + visible_duration

            if self.waveform_data is not None:
                # Check if multi-level or single-level data
                is_multi_level = (
                    isinstance(self.waveform_data, dict)
                    and "levels" in self.waveform_data
                )

                if is_multi_level:
                    # Select appropriate level based on zoom
                    zoom_thresholds = self.waveform_data["zoom_thresholds"]
                    levels = self.waveform_data["levels"]
                    rates = self.waveform_data["rates"]

                    # Find the best level for current zoom
                    selected_level = 0
                    for i, threshold in enumerate(zoom_thresholds):
                        if self.zoom_level >= threshold:
                            selected_level = i

                    waveform_array = levels[selected_level]
                    sample_rate = rates[selected_level]

                    logger.debug(
                        f"Using level {selected_level} ({sample_rate} Hz) for zoom {self.zoom_level:.1f}x"
                    )
                else:
                    # Old single-level format
                    waveform_array = self.waveform_data
                    sample_rate = (
                        len(waveform_array) / self.duration if self.duration > 0 else 1
                    )
                    selected_level = 0

                if len(waveform_array) > 0:
                    # Calculate sample indices based on visible time range
                    total_samples = len(waveform_array)
                    samples_per_second = sample_rate

                    start_sample = int(start_time * samples_per_second)
                    end_sample = int(end_time * samples_per_second)

                    # Clamp to valid range
                    start_sample = max(0, min(total_samples - 1, start_sample))
                    end_sample = max(start_sample + 1, min(total_samples, end_sample))

                    # Get visible portion of waveform
                    visible_waveform = waveform_array[start_sample:end_sample]

                    if len(visible_waveform) > 0:
                        # Check if we need to recalculate peaks or can use cached data
                        viewport_key = (
                            start_sample,
                            end_sample,
                            int(width),
                            int(height),
                            selected_level if is_multi_level else 0,
                        )

                        # Check if we can use the cached waveform surface
                        if (
                            self.cached_viewport_key != viewport_key
                            or self.cached_waveform_surface is None
                        ):
                            # Need to render waveform to a new ImageSurface
                            y_center = height / 2
                            y_scale = height * 0.4
                            render_width = int(width)
                            bar_width = 1

                            samples_per_bar = (
                                len(visible_waveform) / render_width
                                if render_width > 0
                                else len(visible_waveform)
                            )

                            # Pre-calculate peaks - with proper LOD, we always have good sample density
                            peaks = []

                            for bar_idx in range(render_width):
                                sample_start_idx = int(bar_idx * samples_per_bar)
                                sample_end_idx = int(
                                    min(
                                        (bar_idx + 1) * samples_per_bar,
                                        len(visible_waveform),
                                    )
                                )

                                # Ensure we have at least one sample
                                if sample_end_idx <= sample_start_idx:
                                    sample_end_idx = sample_start_idx + 1

                                if sample_start_idx >= len(visible_waveform):
                                    break

                                pixel_samples = visible_waveform[
                                    sample_start_idx:sample_end_idx
                                ]

                                if len(pixel_samples) > 0:
                                    sample_min = float(np.min(pixel_samples))
                                    sample_max = float(np.max(pixel_samples))
                                    peaks.append((
                                        bar_idx,
                                        bar_width,
                                        sample_min,
                                        sample_max,
                                    ))
                                else:
                                    peaks.append((bar_idx, bar_width, 0.0, 0.0))

                            # Cache the peaks
                            self.cached_peaks = peaks

                            # Create an ImageSurface to render the waveform once
                            surface = cairo.ImageSurface(
                                cairo.FORMAT_ARGB32, int(width), int(height)
                            )
                            surface_cr = cairo.Context(surface)

                            # Clear surface with transparent background
                            surface_cr.set_operator(cairo.OPERATOR_CLEAR)
                            surface_cr.paint()
                            surface_cr.set_operator(cairo.OPERATOR_OVER)

                            # Render waveform to the surface
                            # Top waveform - solid color
                            surface_cr.set_source_rgb(*self.wave_color)
                            for pixel_x, bar_width, sample_min, sample_max in peaks:
                                y_top = y_center - (sample_max * y_scale)
                                y_bottom = y_center - (sample_min * y_scale)
                                rect_height = y_bottom - y_top
                                # Skip drawing if too small to be visible
                                if rect_height >= 0.5:
                                    surface_cr.rectangle(
                                        pixel_x, y_top, bar_width, rect_height
                                    )
                            surface_cr.fill()

                            # Bottom mirrored waveform - with transparency
                            surface_cr.set_source_rgba(
                                self.wave_color[0],
                                self.wave_color[1],
                                self.wave_color[2],
                                0.15,
                            )
                            for pixel_x, bar_width, sample_min, sample_max in peaks:
                                y_top = y_center + (sample_min * y_scale)
                                y_bottom = y_center + (sample_max * y_scale)
                                rect_height = y_bottom - y_top
                                # Skip drawing if too small to be visible
                                if rect_height >= 0.5:
                                    surface_cr.rectangle(
                                        pixel_x, y_top, bar_width, rect_height
                                    )
                            surface_cr.fill()

                            # Cache the rendered surface
                            self.cached_waveform_surface = surface
                            self.cached_viewport_key = viewport_key

                        # Blit the cached waveform surface - MUCH FASTER than redrawing!
                        cr.set_source_surface(self.cached_waveform_surface, 0, 0)
                        cr.paint()

                    # Draw position marker (account for zoom)
                    if self.duration > 0 and start_time <= self.position <= end_time:
                        position_x = (
                            (self.position - start_time) / visible_duration
                        ) * width

                        cr.set_source_rgb(*self.position_color)
                        cr.set_line_width(3)
                        cr.move_to(position_x, 0)
                        cr.line_to(position_x, height)
                        cr.stroke()
            else:
                # No waveform data, show appropriate message
                cr.set_source_rgb(0.7, 0.7, 0.7)
                cr.select_font_face(
                    "Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL
                )
                cr.set_font_size(16)

                # Different message based on whether a file is loaded
                if self.duration > 0:
                    text = _("Waveform visualization disabled")
                    subtitle = _("Enable in settings to see waveform")
                else:
                    text = _("No audio loaded")
                    subtitle = None

                extents = cr.text_extents(text)
                text_y = (
                    (height + extents.height) / 2
                    if subtitle is None
                    else (height - extents.height) / 2 - 5
                )
                cr.move_to((width - extents.width) / 2, text_y)
                cr.show_text(text)

                # Draw subtitle if present
                if subtitle:
                    cr.set_font_size(12)
                    cr.set_source_rgb(0.5, 0.5, 0.5)
                    subtitle_extents = cr.text_extents(subtitle)
                    cr.move_to(
                        (width - subtitle_extents.width) / 2,
                        (height + subtitle_extents.height) / 2 + 10,
                    )
                    cr.show_text(subtitle)

                # Draw position marker even without waveform if duration is known
                if self.duration > 0 and self.position > 0:
                    # Calculate position based on zoom
                    if start_time <= self.position <= end_time:
                        position_x = (
                            (self.position - start_time) / visible_duration
                        ) * width

                        cr.set_source_rgb(*self.position_color)
                        cr.set_line_width(3)
                        cr.move_to(position_x, 0)
                        cr.line_to(position_x, height)
                        cr.stroke()

        # Draw timeline ruler at the bottom
        if self.duration > 0:
            self._draw_timeline_ruler(cr, width, height)

        # Draw current playback time display
        if self.duration > 0 and self.position > 0:
            self._draw_playback_time_display(cr, width, height)

        # Draw a visual separator for the manipulation zone (bottom 40%
        manipulation_zone_y = height * 0.6

        # Draw a more visible background for the manipulation zone
        cr.set_source_rgba(
            0.15, 0.2, 0.35, 0.45
        )  # Darker blue-purple tint with more opacity
        cr.rectangle(0, manipulation_zone_y, width, height - manipulation_zone_y)
        cr.fill()

        # Draw a more prominent separator line with gradient effect
        # Top edge - brighter
        cr.set_source_rgba(0.4, 0.5, 0.8, 0.9)  # Bright blue line
        cr.set_line_width(2)
        cr.move_to(0, manipulation_zone_y)
        cr.line_to(width, manipulation_zone_y)
        cr.stroke()

        # Shadow below the line for depth
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.3)
        cr.set_line_width(1)
        cr.move_to(0, manipulation_zone_y + 2)
        cr.line_to(width, manipulation_zone_y + 2)
        cr.stroke()

        # Add a more visible label for the manipulation zone
        if self.markers_enabled:
            cr.set_source_rgba(0.7, 0.8, 1.0, 0.9)  # Brighter blue-white
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(10)
            label = "SEGMENT EDITING ZONE"
            text_extents = cr.text_extents(label)

            # Draw text shadow for better readability
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.6)
            cr.move_to((width - text_extents.width) / 2 + 1, manipulation_zone_y + 16)
            cr.show_text(label)

            # Draw actual text
            cr.set_source_rgba(0.7, 0.8, 1.0, 0.9)
            cr.move_to((width - text_extents.width) / 2, manipulation_zone_y + 15)
            cr.show_text(label)

        # Draw markers after waveform
        self._draw_markers(cr, width, height)

        # Position time removed - now only showing in top-left display

        # Draw hover time indicator
        self._draw_hover_time(cr, width, height)

        # Draw simulated scrollbar when zoomed
        if self.zoom_level > 1.0:
            self._draw_scrollbar(cr, width, height)
