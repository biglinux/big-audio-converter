# app/ui/visualizer.py

"""
Audio visualization component for displaying waveforms.
"""

import gettext
import logging
import math
import time
from threading import Lock

import cairo
import gi
import numpy as np

from app.ui.marker_manager import MarkerManagerMixin, MarkerMode
from app.utils.time_formatter import format_time_ruler, format_time_short

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class AudioVisualizer(MarkerManagerMixin, Gtk.DrawingArea):
    """Widget that displays audio waveform visualization."""

    def __init__(self):
        super().__init__()
        self.set_draw_func(self.on_draw)

        # Accessibility
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Audio waveform visualizer")],
        )

        # Audio data
        self.waveform_data = None
        self.waveform_data_lock = Lock()
        self.position = 0
        self.duration = 0

        # Mouse hover tracking
        self.hover_time = None
        self.hover_x = None

        # Visualization settings
        self.bg_color = (0.11, 0.11, 0.13)
        self.wave_color = (0.30, 0.65, 1.0)
        self.position_color = (1.0, 0.5, 0.0)

        # Waveform rendering cache for performance
        self.cached_peaks = None
        self.cached_viewport_key = None  # (start_sample, end_sample, width, height)
        self.cached_waveform_surface = (
            None  # Pre-rendered waveform as Cairo ImageSurface
        )

        # Add listener for position updates
        self.seek_position_callback = None
        self.hover_time_callback = None  # Callback for waveform hover time changes
        # Initialize marker management state (from MarkerManagerMixin)
        self._init_marker_state()

        # Player reference for checking playback state
        self.player = None

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
        motion_controller.connect("leave", self._on_waveform_leave)
        self.add_controller(motion_controller)

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

        # Queue redraw to display "No audio loaded" message
        self.queue_draw()

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
        """Unified handler for clicks on the waveform.

        The waveform is now exclusively for marker/segment manipulation.
        All time-seeking is handled by the SeekBar widget.
        """
        if not self.duration > 0:
            return

        # Ignore clicks on scrollbar
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

        width = self.get_width()

        # Account for zoom and pan when calculating position
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        position = start_time + (x / width) * visible_duration
        position = round(position, 3)
        logger.info(
            f"🔍 CLICK: x={x:.1f}, width={width}, duration={self.duration:.6f}, visible_dur={visible_duration:.6f}, start={start_time:.6f}, position={position:.6f}"
        )

        # HIGHEST PRIORITY: Always check confirm/cancel buttons first when in dialog modes
        if self.marker_mode == MarkerMode.CONFIRM:
            if self._check_confirm_buttons(x, y):
                return

        elif (
            self.marker_mode == MarkerMode.DELETE_PROMPT
            or self.marker_mode == MarkerMode.DELETE_ALL_CONFIRM
        ):
            if self._check_delete_buttons(x, y):
                return

            if self.marker_mode == MarkerMode.DELETE_PROMPT:
                self._cancel_delete_prompt()
                return
            elif self.marker_mode == MarkerMode.DELETE_ALL_CONFIRM:
                self.marker_mode = MarkerMode.DELETE_PROMPT
                self.queue_draw()
                return

        # Marker/segment interactions (only when markers are enabled)
        if self.markers_enabled:
            # Check delete button
            if self.hovering_delete_button and self.hovered_segment_index >= 0:
                self._prompt_delete_segment(self.hovered_segment_index)
                return

            # Check marker edge for drag
            marker_info = self._find_marker_at_position(x, y)
            if marker_info:
                self.is_dragging_marker = True
                self.dragging_pair_index = marker_info["index"]
                self.dragging_marker_type = marker_info["type"]
                if self.marker_drag_callback:
                    self.marker_drag_callback(True)
                return

            # Check segment body for drag
            segment_body_index = self._find_segment_body_at_position(x, y)
            if segment_body_index is not None:
                self.potential_drag_segment = {
                    "index": segment_body_index,
                    "start_x": x,
                }
                self.drag_start_pos = None
                return

            # Handle marker placement based on mode
            if self.marker_mode == MarkerMode.START:
                self.add_start_marker(position)
            elif self.marker_mode == MarkerMode.STOP:
                self.add_stop_marker(position)

    def on_release_handler(self, gesture, n_press, x, y):
        """Handle mouse release events."""
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

    def _drag_update_single_marker(self, pair, current_x, width, marker_type):
        """Update a single start/stop marker position during drag."""
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        new_position = start_time + (current_x / width) * visible_duration
        new_position = max(0, min(new_position, self.duration))
        new_position = round(new_position, 3)

        if marker_type == "start":
            if pair["stop"] is not None:
                new_position = min(new_position, pair["stop"] - 0.1)
            pair["start"] = new_position
            pair["start_str"] = self._format_time(new_position)
        else:
            if pair["start"] is not None:
                new_position = max(new_position, pair["start"] + 0.1)
            pair["stop"] = new_position
            pair["stop_str"] = self._format_time(new_position)

        self.queue_draw()
        if self.seek_position_callback:
            self.seek_position_callback(new_position, False)

    def _drag_update_segment(self, pair, offset_x, width):
        """Move a whole segment during drag with collision detection."""
        visible_duration = self.duration / self.zoom_level
        time_delta = (offset_x / width) * visible_duration

        new_start = self.drag_start_pos["start"] + time_delta
        new_stop = self.drag_start_pos["stop"] + time_delta
        segment_len = new_stop - new_start

        # Bounds clamping
        if new_start < 0:
            new_start = 0
            new_stop = segment_len
        elif new_stop > self.duration:
            new_stop = self.duration
            new_start = self.duration - segment_len

        # Collision detection with other segments
        gap = 0.01
        for j, other in enumerate(self.marker_pairs):
            if j == self.dragging_pair_index:
                continue
            if other["start"] is None or other["stop"] is None:
                continue
            os_, oe_ = other["start"], other["stop"]
            if new_start < oe_ and new_stop > os_:
                desired_center = (new_start + new_stop) / 2
                other_center = (os_ + oe_) / 2
                if desired_center >= other_center:
                    new_start = oe_ + gap
                    new_stop = new_start + segment_len
                else:
                    new_stop = os_ - gap
                    new_start = new_stop - segment_len

        # Final bounds clamping
        if new_start < 0:
            new_start = 0
            new_stop = segment_len
        if new_stop > self.duration:
            new_stop = self.duration
            new_start = self.duration - segment_len

        pair["start"] = new_start
        pair["stop"] = new_stop
        pair["start_str"] = self._format_time(new_start)
        pair["stop_str"] = self._format_time(new_stop)
        self.queue_draw()

        if self.seek_position_callback:
            self.seek_position_callback(new_start, False)

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

        # Handle pan gesture (highest priority when active)
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
            self._drag_update_single_marker(pair, current_x, width, "start")
        elif self.dragging_marker_type == "stop":
            self._drag_update_single_marker(pair, current_x, width, "stop")
        elif self.dragging_marker_type == "segment" and self.drag_start_pos is not None:
            self._drag_update_segment(pair, offset_x, width)

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

        # Reset tracking variables
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

            # Notify seekbar of hover time
            if self.hover_time_callback:
                self.hover_time_callback(self.hover_time)

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
            self.set_cursor(None)
            return

        # If in delete prompt or confirm mode, only check for button hover
        if self.marker_mode in [MarkerMode.DELETE_PROMPT, MarkerMode.CONFIRM]:
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
                self.set_cursor(None)
                return

        # Marker interactions work across full waveform height (no more manipulation zones)
        if not self.is_dragging_marker:
            # Check delete button hover
            if self.hovering_delete_button:
                self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
                return

            # Check marker edge for resize cursor
            marker_info = self._find_marker_at_position(x, y)
            if marker_info:
                if marker_info["type"] == "start":
                    self.set_cursor(Gdk.Cursor.new_from_name("w-resize"))
                else:
                    self.set_cursor(Gdk.Cursor.new_from_name("e-resize"))
                return

            # Check segment body for move cursor
            segment_body_index = self._find_segment_body_at_position(x, y)
            if segment_body_index is not None:
                self.set_cursor(Gdk.Cursor.new_from_name("move"))
                return

            # Update segment hover highlight
            pair_index = self._find_segment_at_position(x, y)
            if pair_index != self.highlighted_pair:
                self.highlighted_pair = pair_index
                self.queue_draw()

            if pair_index >= 0:
                self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            else:
                self.set_cursor(Gdk.Cursor.new_from_name("crosshair"))

    def connect_seek_handler(self, callback):
        """Connect a handler for seek events."""
        self.seek_position_callback = callback

    def _on_waveform_leave(self, controller):
        """Clear hover state when mouse leaves the waveform."""
        self.hover_time = None
        self.hover_x = None
        self.hovered_segment_index = -1
        self.hovering_delete_button = False
        if self.hover_time_callback:
            self.hover_time_callback(None)
        self.queue_draw()

    def _draw_timeline_ruler(self, cr, width, height):
        """Draw subtle integrated timeline ticks at the bottom of the waveform."""
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        end_time = start_time + visible_duration

        # Position ticks at the bottom edge (above scrollbar if zoomed)
        bottom_margin = 4
        if self.zoom_level > 1.0:
            bottom_margin = self.scrollbar_height + self.scrollbar_margin * 2 + 4
        tick_base_y = height - bottom_margin

        # Calculate nice interval for marks
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

        first_mark = int(start_time / time_per_mark) * time_per_mark
        t = first_mark
        while t <= end_time:
            if t >= start_time:
                x = ((t - start_time) / visible_duration) * width
                is_major = abs(t % (time_per_mark * 5)) < 0.01
                tick_h = 8 if is_major else 4

                # Tick mark (upward from base)
                cr.set_source_rgba(0.55, 0.55, 0.60, 0.45)
                cr.move_to(x, tick_base_y)
                cr.line_to(x, tick_base_y - tick_h)
                cr.stroke()

                # Time label for major marks
                if is_major:
                    label = format_time_ruler(t, time_per_mark)
                    ext = cr.text_extents(label)
                    lx = max(2, min(width - ext.width - 2, x - ext.width / 2))
                    cr.set_source_rgba(0.55, 0.55, 0.60, 0.65)
                    cr.move_to(lx, tick_base_y - tick_h - 3)
                    cr.show_text(label)
            t += time_per_mark

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

    def _draw_hover_time(self, cr, width, height):
        """Draw vertical guide line at hover position."""
        if self.hover_time is None or self.hover_x is None or self.duration <= 0:
            return

        # Vertical guide line only (time shown on seekbar)
        cr.set_source_rgba(1, 1, 1, 0.3)
        cr.set_line_width(1)
        cr.move_to(self.hover_x, 0)
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

    def _select_waveform_level(self):
        """Select the appropriate waveform LOD level for current zoom.

        Returns (waveform_array, sample_rate, selected_level).
        """
        is_multi_level = isinstance(self.waveform_data, dict) and "levels" in self.waveform_data
        if is_multi_level:
            zoom_thresholds = self.waveform_data["zoom_thresholds"]
            levels = self.waveform_data["levels"]
            rates = self.waveform_data["rates"]
            selected_level = 0
            for i, threshold in enumerate(zoom_thresholds):
                if self.zoom_level >= threshold:
                    selected_level = i
            return levels[selected_level], rates[selected_level], selected_level
        # Old single-level format
        sample_rate = len(self.waveform_data) / self.duration if self.duration > 0 else 1
        return self.waveform_data, sample_rate, 0

    def _render_waveform_cache(self, width, height, visible_waveform, viewport_key):
        """Render the waveform to a cached ImageSurface."""
        y_center = height / 2
        y_scale = height * 0.4
        render_width = int(width)
        bar_width = 1
        samples_per_bar = len(visible_waveform) / render_width if render_width > 0 else len(visible_waveform)

        peaks = []
        for bar_idx in range(render_width):
            s_start = int(bar_idx * samples_per_bar)
            s_end = int(min((bar_idx + 1) * samples_per_bar, len(visible_waveform)))
            if s_end <= s_start:
                s_end = s_start + 1
            if s_start >= len(visible_waveform):
                break
            pixel_samples = visible_waveform[s_start:s_end]
            if len(pixel_samples) > 0:
                peaks.append((bar_idx, bar_width, float(np.min(pixel_samples)), float(np.max(pixel_samples))))
            else:
                peaks.append((bar_idx, bar_width, 0.0, 0.0))

        self.cached_peaks = peaks

        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(width), int(height))
        surface_cr = cairo.Context(surface)
        surface_cr.set_operator(cairo.OPERATOR_CLEAR)
        surface_cr.paint()
        surface_cr.set_operator(cairo.OPERATOR_OVER)

        # Center reference line
        surface_cr.set_source_rgba(0.4, 0.4, 0.45, 0.15)
        surface_cr.set_line_width(1)
        surface_cr.move_to(0, y_center)
        surface_cr.line_to(render_width, y_center)
        surface_cr.stroke()

        # Waveform bars
        surface_cr.set_source_rgb(*self.wave_color)
        for pixel_x, bw, sample_min, sample_max in peaks:
            y_top = y_center - (sample_max * y_scale)
            y_bottom = y_center - (sample_min * y_scale)
            rect_height = y_bottom - y_top
            if rect_height >= 0.5:
                surface_cr.rectangle(pixel_x, y_top, bw, rect_height)
        surface_cr.fill()

        self.cached_waveform_surface = surface
        self.cached_viewport_key = viewport_key

    def _draw_position_marker(self, cr, width, height, start_time, end_time, visible_duration):
        """Draw the orange playback position marker line."""
        if self.duration > 0 and start_time <= self.position <= end_time:
            position_x = ((self.position - start_time) / visible_duration) * width
            cr.set_source_rgba(1.0, 0.5, 0.0, 0.08)
            cr.rectangle(position_x - 3, 0, 6, height)
            cr.fill()
            cr.set_source_rgba(1.0, 0.5, 0.0, 0.9)
            cr.set_line_width(2)
            cr.move_to(position_x, 0)
            cr.line_to(position_x, height)
            cr.stroke()

    def _draw_no_waveform_message(self, cr, width, height, start_time, end_time, visible_duration):
        """Draw placeholder text when no waveform data is available."""
        cr.set_source_rgb(0.7, 0.7, 0.7)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(16)

        if self.duration > 0:
            text = _("Waveform visualization disabled")
            subtitle = _("Enable in settings to see waveform")
        else:
            text = _("No audio loaded")
            subtitle = None

        extents = cr.text_extents(text)
        text_y = (height + extents.height) / 2 if subtitle is None else (height - extents.height) / 2 - 5
        cr.move_to((width - extents.width) / 2, text_y)
        cr.show_text(text)

        if subtitle:
            cr.set_font_size(12)
            cr.set_source_rgb(0.5, 0.5, 0.5)
            sub_ext = cr.text_extents(subtitle)
            cr.move_to((width - sub_ext.width) / 2, (height + sub_ext.height) / 2 + 10)
            cr.show_text(subtitle)

        # Position marker even without waveform
        if self.duration > 0 and self.position > 0:
            self._draw_position_marker(cr, width, height, start_time, end_time, visible_duration)

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
                waveform_array, sample_rate, selected_level = self._select_waveform_level()

                if len(waveform_array) > 0:
                    # Calculate sample indices based on visible time range
                    start_sample = int(start_time * sample_rate)
                    end_sample = int(end_time * sample_rate)
                    total_samples = len(waveform_array)
                    start_sample = max(0, min(total_samples - 1, start_sample))
                    end_sample = max(start_sample + 1, min(total_samples, end_sample))

                    visible_waveform = waveform_array[start_sample:end_sample]

                    if len(visible_waveform) > 0:
                        is_multi_level = isinstance(self.waveform_data, dict) and "levels" in self.waveform_data
                        viewport_key = (start_sample, end_sample, int(width), int(height),
                                        selected_level if is_multi_level else 0)

                        if self.cached_viewport_key != viewport_key or self.cached_waveform_surface is None:
                            self._render_waveform_cache(width, height, visible_waveform, viewport_key)

                        cr.set_source_surface(self.cached_waveform_surface, 0, 0)
                        cr.paint()

                    # Draw position marker
                    self._draw_position_marker(cr, width, height, start_time, end_time, visible_duration)
            else:
                self._draw_no_waveform_message(cr, width, height, start_time, end_time, visible_duration)

        # Draw timeline ruler at bottom of waveform
        if self.duration > 0:
            self._draw_timeline_ruler(cr, width, height)

        # Draw markers after waveform
        self._draw_markers(cr, width, height)

        # Position time removed - now only showing in top-left display

        # Draw hover time indicator
        self._draw_hover_time(cr, width, height)

        # Draw simulated scrollbar when zoomed
        if self.zoom_level > 1.0:
            self._draw_scrollbar(cr, width, height)


class SeekBar(Gtk.DrawingArea):
    """Modern seekbar with rounded track, smooth thumb, hover preview, and time labels."""

    HEIGHT = 36

    def __init__(self):
        super().__init__()
        self.set_content_height(self.HEIGHT)
        self.set_draw_func(self._draw)

        # Accessibility
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Audio position seekbar")],
        )

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
        click.connect("pressed", self._on_pressed)
        click.connect("released", self._on_released)
        self.add_controller(click)

        # Drag handler for scrubbing
        drag = Gtk.GestureDrag()
        drag.connect("drag-update", self._on_drag_update)
        self.add_controller(drag)

        # Motion handler for hover preview
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

    # --- Public API ---

    def connect_seek_handler(self, callback):
        self._seek_callback = callback

    def set_duration(self, duration):
        self.duration = max(0.0, duration)
        self.queue_draw()

    def set_position(self, position):
        self._position = max(0.0, position)
        self.queue_draw()

    def set_zoom_viewport(self, zoom_level, viewport_offset):
        self._zoom_level = max(1.0, zoom_level)
        self._viewport_offset = max(0.0, min(1.0, viewport_offset))
        self.queue_draw()

    def set_waveform_hover_time(self, time):
        """Set hover time from waveform (None to clear)."""
        self._waveform_hover_time = time
        self.queue_draw()

    # --- Input handlers ---

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

    # --- Drawing helpers ---

    def _draw_rounded_rect(self, cr, x, y, w, h, r):
        """Draw a rounded rectangle path."""
        deg = 3.14159 / 180.0
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -90 * deg, 0)
        cr.arc(x + w - r, y + h - r, r, 0, 90 * deg)
        cr.arc(x + r, y + h - r, r, 90 * deg, 180 * deg)
        cr.arc(x + r, y + r, r, 180 * deg, 270 * deg)
        cr.close_path()

    # --- Drawing ---

    def _draw(self, area, cr, width, height):

        # Full transparent background (blends with parent)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()

        # Layout constants — track spans full width to align with waveform
        pad = 4  # horizontal padding (matches waveform frame margins)
        track_h = 6  # track height
        track_r = 3  # track corner radius
        thumb_r = 7  # thumb radius
        track_w = width - 2 * pad

        # Center labels+track ensemble vertically for equal top/bottom spacing
        text_h_approx = 10  # font size 10 approximate ascent
        gap = 6  # space between label baseline and track top
        content_h = text_h_approx + gap + track_h
        top_margin = (height - content_h) / 2
        track_y = top_margin + text_h_approx + gap

        # Store bounds for input handlers
        self._track_x = pad
        self._track_w = track_w

        if track_w <= 0:
            return

        progress = (
            max(0.0, min(1.0, self._position / self.duration))
            if self.duration > 0
            else 0.0
        )

        # --- Time labels with hundredths precision (above track) ---
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)

        elapsed_str = format_time_short(self._position)
        remaining = max(0, self.duration - self._position)
        remaining_str = f"-{format_time_short(remaining)}"

        label_y = track_y - 6

        # Elapsed (left)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.80)
        cr.move_to(pad, label_y)
        cr.show_text(elapsed_str)

        # Remaining (right)
        ext_r = cr.text_extents(remaining_str)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.55)
        cr.move_to(width - pad - ext_r.width, label_y)
        cr.show_text(remaining_str)

        # Waveform hover time (at corresponding position, with opaque bg over labels)
        if self._waveform_hover_time is not None and self.duration > 0:
            hover_str = format_time_short(self._waveform_hover_time)
            hover_progress = max(
                0.0, min(1.0, self._waveform_hover_time / self.duration)
            )
            hover_x = pad + hover_progress * track_w
            ext_h = cr.text_extents(hover_str)
            # Clamp label position within bounds
            lx = max(pad, min(width - pad - ext_h.width, hover_x - ext_h.width / 2))
            # Opaque background so it covers elapsed/remaining labels beneath
            bg_px, bg_py = 3, 2
            cr.set_source_rgba(0.12, 0.12, 0.14, 0.95)
            cr.rectangle(
                lx - bg_px,
                label_y - ext_h.height - bg_py,
                ext_h.width + bg_px * 2,
                ext_h.height + bg_py * 2,
            )
            cr.fill()
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.85)
            cr.move_to(lx, label_y)
            cr.show_text(hover_str)
            # Small indicator line on track at hover position
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.35)
            cr.set_line_width(1)
            cr.move_to(hover_x, track_y - 2)
            cr.line_to(hover_x, track_y + track_h + 2)
            cr.stroke()

        # --- Zoom viewport indicator (subtle, behind track) ---
        if self._zoom_level > 1.0:
            vp_frac = 1.0 / self._zoom_level
            vp_x = pad + self._viewport_offset * (1.0 - vp_frac) * track_w
            vp_w = vp_frac * track_w
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.06)
            self._draw_rounded_rect(
                cr, vp_x, track_y - 4, vp_w, track_h + 8, track_r + 2
            )
            cr.fill()
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.12)
            cr.set_line_width(1)
            self._draw_rounded_rect(
                cr, vp_x + 0.5, track_y - 3.5, vp_w - 1, track_h + 7, track_r + 2
            )
            cr.stroke()

        # --- Track background ---
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.15)
        self._draw_rounded_rect(cr, pad, track_y, track_w, track_h, track_r)
        cr.fill()

        # --- Elapsed portion (accent gradient) ---
        elapsed_w = progress * track_w
        if elapsed_w > 0:
            pat = cairo.LinearGradient(pad, 0, pad + elapsed_w, 0)
            pat.add_color_stop_rgba(0, 0.30, 0.56, 0.92, 0.90)
            pat.add_color_stop_rgba(1, 0.45, 0.72, 1.0, 0.95)
            cr.set_source(pat)
            self._draw_rounded_rect(cr, pad, track_y, elapsed_w, track_h, track_r)
            cr.fill()

        # --- Hover preview line ---
        if (
            self._hover_x >= pad
            and self._hover_x <= width - pad
            and not self._is_dragging
        ):
            hover_prog = max(0.0, min(1.0, (self._hover_x - pad) / track_w))
            hx = pad + hover_prog * track_w
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.25)
            cr.set_line_width(1)
            cr.move_to(hx, track_y - 3)
            cr.line_to(hx, track_y + track_h + 3)
            cr.stroke()

            # Hover time tooltip
            hover_str = format_time_short(self._hover_time)
            cr.set_font_size(9)
            ext = cr.text_extents(hover_str)
            tip_x = hx - ext.width / 2
            tip_x = max(pad, min(width - pad - ext.width, tip_x))
            tip_y = track_y - 8

            cr.set_source_rgba(0, 0, 0, 0.7)
            self._draw_rounded_rect(
                cr, tip_x - 4, tip_y - ext.height - 2, ext.width + 8, ext.height + 4, 3
            )
            cr.fill()
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.9)
            cr.move_to(tip_x, tip_y)
            cr.show_text(hover_str)

        # --- Thumb ---
        thumb_x = pad + progress * track_w
        thumb_cy = track_y + track_h / 2
        show_thumb = self._is_dragging or (self._hover_x >= 0)

        if show_thumb:
            cr.set_source_rgba(0.40, 0.65, 1.0, 0.15)
            cr.arc(thumb_x, thumb_cy, thumb_r + 4, 0, 2 * 3.14159)
            cr.fill()

        cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
        cr.arc(thumb_x, thumb_cy, thumb_r if show_thumb else 4, 0, 2 * 3.14159)
        cr.fill()

        if show_thumb:
            cr.set_source_rgba(0.35, 0.60, 0.95, 1.0)
            cr.arc(thumb_x, thumb_cy, 3, 0, 2 * 3.14159)
            cr.fill()
