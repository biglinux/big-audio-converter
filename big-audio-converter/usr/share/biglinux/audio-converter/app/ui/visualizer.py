"""The waveform with its marks, and the seek bar under it."""

import gettext
import logging
import math

import cairo
import gi
import numpy as np

from app.ui.cairo_text import rounded_rect, show_text, text_extents
from app.ui.marker_manager import MarkerManagerMixin
from app.utils.time_formatter import format_time_ruler, format_time_short

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk

_ = gettext.gettext

logger = logging.getLogger(__name__)


class AudioVisualizer(MarkerManagerMixin, Gtk.DrawingArea):
    # Told of every change of the visible window: zoom, panning, scrollbar.
    viewport_changed_callback = None

    @property
    def viewport_offset(self):
        return self._viewport_offset

    @viewport_offset.setter
    def viewport_offset(self, offset):
        changed = offset != getattr(self, "_viewport_offset", None)
        self._viewport_offset = offset
        if changed and self.viewport_changed_callback:
            self.viewport_changed_callback()

    """Widget that displays audio waveform visualization."""

    def __init__(self):
        super().__init__()
        self.set_draw_func(lambda area, *args: area.on_draw(area, *args))

        # Accessibility
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Audio waveform visualizer")],
        )

        # Audio data
        self.waveform_data = None
        self.waveform_peak = 1.0
        self.waveform_error = None  # Set by WaveformGenerator on failure
        self.position = 0
        self.duration = 0
        self.duration_changed_callback = None

        # Mouse hover tracking
        self.hover_time = None
        self.hover_x = None

        # Visualization settings
        self.bg_color = (0.11, 0.11, 0.13)
        self.wave_color = (0.30, 0.65, 1.0)

        # Waveform rendering cache for performance
        self.cached_viewport_key = None  # (start_sample, end_sample, width, height)
        self.cached_waveform_surface = (
            None  # Pre-rendered waveform as Cairo ImageSurface
        )

        self.seek_position_callback = None
        self.hover_time_callback = None  # Callback for waveform hover time changes
        self._init_marker_state()

        # Player reference for checking playback state
        self.player = None

        # Setup click handler for both seeking and marking
        click_controller = Gtk.GestureClick()
        click_controller.connect(
            "pressed",
            lambda controller, *args: controller.get_widget().on_click_handler(
                controller, *args
            ),
        )
        click_controller.connect(
            "released",
            lambda controller, *args: controller.get_widget().on_release_handler(
                controller, *args
            ),
        )
        self.add_controller(click_controller)

        drag_controller = Gtk.GestureDrag()
        drag_controller.connect(
            "drag-begin",
            lambda controller, *args: controller.get_widget().on_drag_begin(
                controller, *args
            ),
        )
        drag_controller.connect(
            "drag-update",
            lambda controller, *args: controller.get_widget().on_drag_update(
                controller, *args
            ),
        )
        drag_controller.connect(
            "drag-end",
            lambda controller, *args: controller.get_widget().on_drag_end(
                controller, *args
            ),
        )
        self.add_controller(drag_controller)

        # Setup motion controller for hover effects
        motion_controller = Gtk.EventControllerMotion()
        motion_controller.connect(
            "motion",
            lambda controller, *args: controller.get_widget().on_motion(
                controller, *args
            ),
        )
        motion_controller.connect(
            "leave",
            lambda controller, *args: controller.get_widget()._on_waveform_leave(
                controller, *args
            ),
        )
        self.add_controller(motion_controller)

        # Zoom and pan functionality
        self.zoom_level = 1.0  # 1.0 = no zoom, higher = zoomed in
        self.viewport_offset = 0.0  # Position in waveform (0.0 to 1.0)
        self.min_zoom = 1.0
        self.max_zoom = 1000.0

        # Simulated scrollbar for horizontal navigation when zoomed
        self.scrollbar_height = 12
        self.scrollbar_margin = 4
        self.is_dragging_scrollbar = False
        self.scrollbar_drag_start_offset = 0
        self.hovering_scrollbar = False

        # Ctrl+drag panning
        self.pan_gesture_active = False
        self.pan_gesture_start_offset = 0

        # Callback for notifying zoom changes (to update UI slider)
        self.zoom_changed_callback = None

        # Loading state for waveform generation
        self.is_loading = False
        self.loading_message = _("Generating waveform...")

        scroll_controller = Gtk.EventControllerScroll()
        scroll_controller.set_flags(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll_controller.connect(
            "scroll",
            lambda controller, *args: controller.get_widget().on_scroll(
                controller, *args
            ),
        )
        self.add_controller(scroll_controller)

        key_controller = Gtk.EventControllerKey()
        key_controller.connect(
            "key-pressed",
            lambda controller, *args: controller.get_widget().on_key_pressed(
                controller, *args
            ),
        )
        self.add_controller(key_controller)

        right_click_controller = Gtk.GestureClick()
        right_click_controller.set_button(3)  # Right mouse button
        right_click_controller.connect(
            "pressed",
            lambda controller, *args: controller.get_widget().on_right_click(
                controller, *args
            ),
        )
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
        self.is_loading = False

        self.position = 0
        self.zoom_level = 1.0
        self.viewport_offset = 0.0
        # Marks stay: the window restores each file's own after this.
        self._drop_cached_surface()
        # A PeakCollector payload, {"peaks", "rate"}; immutable and possibly
        # shared with the generator's cache.
        self.waveform_data = data
        # Scale the drawing to the file's own peak so quiet speech stays
        # readable for cutting.
        self.waveform_peak = 1.0
        if data and len(data["peaks"]):
            self.waveform_peak = float(np.max(data["peaks"])) or 1.0
        self.duration = duration
        if self.duration_changed_callback:
            self.duration_changed_callback(duration)
        self.queue_draw()

    def _drop_cached_surface(self):
        if self.cached_waveform_surface is not None:
            self.cached_waveform_surface.finish()
            self.cached_waveform_surface = None
        self.cached_viewport_key = None

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

                is_playing = self.player and self.player.is_playing()

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
        self.is_loading = False

        self.waveform_data = None
        self.position = 0
        self.duration = 0
        self._drop_cached_surface()

        self.queue_draw()

    def on_right_click(self, gesture, n_press, x, y):
        """Handle right-click for context menu on segments."""
        if not self.duration > 0:
            return

        segment_index = self._find_segment_at_position(x, y)
        if segment_index >= 0:
            self._prompt_delete_segment(segment_index)

    def on_click_handler(self, gesture, n_press, x, y):
        """A press on the waveform: grab a marker or segment, or wait to see
        whether the press becomes a click (seek) or a drag (new segment)."""
        if not self.duration > 0:
            return

        # Ignore clicks on scrollbar
        if self._is_over_scrollbar(x, y):
            return

        width = self.get_width()

        # Account for zoom and pan when calculating position
        visible_duration = self.duration / self.zoom_level
        start_time = self.viewport_offset * self.duration
        position = start_time + (x / width) * visible_duration
        position = round(position, 3)

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
        if segment_body_index >= 0:
            self.potential_drag_segment = {
                "index": segment_body_index,
                "start_x": x,
            }
            self.drag_start_pos = None
            self.pending_press = position
            return

        # Empty waveform: a click seeks, a drag marks a new segment.
        self.pending_press = position

    def on_release_handler(self, gesture, n_press, x, y):
        """Handle mouse release events."""
        # End pan gesture if active
        if self.pan_gesture_active:
            self.pan_gesture_active = False
            self.set_cursor(None)
            return

        # If we were dragging, notify about the marker update
        if self.is_dragging_marker:
            if self.dragging_marker_type == "new":
                pair = self.marker_pairs[self.dragging_pair_index]
                if pair["stop"] - pair["start"] < 0.1:
                    del self.marker_pairs[self.dragging_pair_index]
                self.queue_draw()
            if self.marker_updated_callback:
                self.marker_updated_callback(self.get_marker_pairs())

            if self.marker_drag_callback:
                self.marker_drag_callback(False)

            self.is_dragging_marker = False
            self.dragging_marker_type = None
            self.dragging_pair_index = -1

    def on_drag_begin(self, gesture, start_x, start_y):
        """Handle start of drag operation."""
        if self._is_over_scrollbar_thumb(start_x, start_y):
            self.is_dragging_scrollbar = True
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
                self.pan_gesture_start_offset = self.viewport_offset
                self.set_cursor(Gdk.Cursor.new_from_name("grabbing"))
                return

        # If we have a potential segment drag, start tracking for REAL drag
        # We don't want to immediately enter drag mode - that happens in update

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
        else:
            if pair["start"] is not None:
                new_position = max(new_position, pair["start"] + 0.1)
            pair["stop"] = new_position

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

        # A drag from empty waveform draws a new segment, in either direction.
        if (
            not self.is_dragging_marker
            and self.potential_drag_segment is None
            and self.pending_press is not None
            and abs(offset_x) > self.drag_threshold
        ):
            self.marker_pairs.append(
                {"start": self.pending_press, "stop": self.pending_press}
            )
            self.is_dragging_marker = True
            self.dragging_pair_index = len(self.marker_pairs) - 1
            self.dragging_marker_type = "new"
            self.new_segment_anchor = self.pending_press
            if self.marker_drag_callback:
                self.marker_drag_callback(True)

        if (
            not self.is_dragging_marker and self.potential_drag_segment is not None
        ) and (abs(offset_x) > self.drag_threshold):
            self.is_dragging_marker = True
            self.dragging_pair_index = self.potential_drag_segment["index"]
            self.dragging_marker_type = "segment"
            # Record the starting segment position for more accurate movement
            pair = self.marker_pairs[self.dragging_pair_index]
            self.drag_start_pos = {"start": pair["start"], "stop": pair["stop"]}

            if self.marker_drag_callback:
                self.marker_drag_callback(True)

        # Continue with normal drag handling if active
        if not self.is_dragging_marker or self.dragging_pair_index < 0:
            return

        ok, start_x, _start_y = gesture.get_start_point()
        if not ok:
            return

        # Calculate current x position
        current_x = start_x + offset_x
        width = self.get_width()

        pair = self.marker_pairs[self.dragging_pair_index]

        if self.dragging_marker_type == "start":
            self._drag_update_single_marker(pair, current_x, width, "start")
        elif self.dragging_marker_type == "stop":
            self._drag_update_single_marker(pair, current_x, width, "stop")
        elif self.dragging_marker_type == "segment" and self.drag_start_pos is not None:
            self._drag_update_segment(pair, offset_x, width)
        elif self.dragging_marker_type == "new":
            visible_duration = self.duration / self.zoom_level
            start_time = self.viewport_offset * self.duration
            here = start_time + (current_x / width) * visible_duration
            here = round(max(0, min(here, self.duration)), 3)
            pair["start"] = min(self.new_segment_anchor, here)
            pair["stop"] = max(self.new_segment_anchor, here)
            self.queue_draw()
            if self.seek_position_callback:
                self.seek_position_callback(here, False)

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

        # A press that never moved is a click: listen from there.
        if (
            self.pending_press is not None
            and not self.is_dragging_marker
            and abs(offset_x) <= self.drag_threshold
            and self.seek_position_callback
        ):
            self.seek_position_callback(self.pending_press, False)
        self.pending_press = None

        self.potential_drag_segment = None
        self.drag_start_pos = None

        # Handle normal drag cleanup
        # The cleanup is handled in on_release_handler

    def on_scroll(self, controller, dx, dy):
        """Handle mouse wheel scrolling for zoom and horizontal panning."""
        if not self.duration > 0:
            return False

        event = controller.get_current_event()
        if event:
            result = event.get_position()
            if result and len(result) == 3:
                success, mouse_x, _mouse_y = result
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
        if self.hovered_segment_index >= 0 and (
            keyval == Gdk.KEY_Delete or keyval == Gdk.KEY_BackSpace
        ):
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
        """Zoom by ``factor`` around the playback position, or the middle."""
        previous = self.zoom_level
        self.set_zoom_level(self.zoom_level * factor, use_mouse_position=False)
        if self.zoom_level != previous and self.zoom_changed_callback:
            self.zoom_changed_callback(self.zoom_level)

    def zoom_out(self, factor=1.2):
        """Zoom out by the specified factor."""
        self.zoom_in(1 / factor)

    def reset_zoom(self):
        """Reset zoom to default level."""
        if self.zoom_level != 1.0:
            self.zoom_level = 1.0
            self.viewport_offset = 0.0

            if self.zoom_changed_callback:
                self.zoom_changed_callback(self.zoom_level)

            self.queue_draw()

    def on_motion(self, controller, x, y):
        """Handle mouse motion for hover effects."""
        width = self.get_width()

        was_hovering_scrollbar = self.hovering_scrollbar
        self.hovering_scrollbar = self._is_over_scrollbar(x, y)

        if was_hovering_scrollbar != self.hovering_scrollbar:
            self.queue_draw()  # Redraw to show hover effect

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

            if self.hover_time_callback:
                self.hover_time_callback(self.hover_time)

            # Track which segment is being hovered for keyboard shortcuts and visual feedback
            old_hovered = self.hovered_segment_index
            self.hovered_segment_index = self._find_segment_at_position(x, y)

            was_hovering_delete = self.hovering_delete_button
            self.hovering_delete_button = self._is_over_delete_button(x, y)

            if (
                old_hovered != self.hovered_segment_index
                or was_hovering_delete != self.hovering_delete_button
                or self.hover_time is not None
            ):
                self.queue_draw()

        if self.duration <= 0:
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
            if segment_body_index >= 0:
                self.set_cursor(Gdk.Cursor.new_from_name("move"))
                return

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
                    ext = text_extents(cr, label)
                    lx = max(2, min(width - ext.width - 2, x - ext.width / 2))
                    cr.set_source_rgba(0.55, 0.55, 0.60, 0.65)
                    cr.move_to(lx, tick_base_y - tick_h - 3)
                    show_text(cr, label)
            t += time_per_mark

    def _draw_hint(self, cr):
        """How to use the waveform, until the first segment exists."""
        text = _("Click to listen from there · Drag to mark a part")
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(12)
        extents = text_extents(cr, text)
        # The waveform's peaks reach the top; the text keeps its own backdrop.
        cr.set_source_rgba(*self.bg_color, 0.85)
        rounded_rect(cr, 6, 6, extents.width + 12, 20, 4)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.85)
        cr.move_to(12, 20)
        show_text(cr, text)

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

        if self.hovering_scrollbar or self.is_dragging_scrollbar:
            cr.set_source_rgba(0.6, 0.7, 0.9, 0.9)  # Brighter when hovering/dragging
        else:
            cr.set_source_rgba(0.5, 0.6, 0.8, 0.7)  # Normal state

        cr.rectangle(thumb_x, scrollbar_y + 2, thumb_width, self.scrollbar_height - 4)
        cr.fill()

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
        """A static status avoids redraw polling and respects reduced motion."""
        cr.set_font_size(14)
        cr.set_source_rgb(0.9, 0.9, 0.9)
        extents = text_extents(cr, self.loading_message)
        cr.move_to(max(12, (width - extents.width) / 2), height / 2)
        show_text(cr, self.loading_message)

    def cleanup(self):
        self._close_marker_dialog()
        self.is_loading = False
        self.player = None
        for name in (
            "duration_changed_callback",
            "seek_position_callback",
            "hover_time_callback",
            "marker_updated_callback",
            "marker_drag_callback",
            "zoom_changed_callback",
            "viewport_changed_callback",
        ):
            setattr(self, name, None)
        self.waveform_data = None
        self._drop_cached_surface()
        self.set_draw_func(lambda *args: None)

    def _render_waveform_cache(self, width, height, visible_waveform, viewport_key):
        """Render the waveform to a cached ImageSurface."""
        y_center = height / 2
        y_scale = height * 0.4 / self.waveform_peak
        render_width = int(width)
        bar_width = 1
        samples_per_bar = (
            len(visible_waveform) / render_width
            if render_width > 0
            else len(visible_waveform)
        )

        # The loudest peak under each pixel column.
        starts = np.minimum(
            (np.arange(render_width) * samples_per_bar).astype(int),
            len(visible_waveform) - 1,
        )
        peaks = np.maximum.reduceat(visible_waveform, starts) if render_width else []

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
        for pixel_x, peak in enumerate(peaks):
            # Levels hold absolute peaks; draw them mirrored.
            if 2 * peak * y_scale >= 0.5:
                surface_cr.rectangle(
                    pixel_x, y_center - peak * y_scale, bar_width, 2 * peak * y_scale
                )
        surface_cr.fill()

        self.cached_waveform_surface = surface
        self.cached_viewport_key = viewport_key

    def _draw_position_marker(
        self, cr, width, height, start_time, end_time, visible_duration
    ):
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

    def _draw_no_waveform_message(
        self, cr, width, height, start_time, end_time, visible_duration
    ):
        """Draw placeholder text when no waveform data is available."""
        cr.set_source_rgb(0.7, 0.7, 0.7)
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
            cr.set_source_rgb(0.5, 0.5, 0.5)
            sub_ext = text_extents(cr, subtitle)
            cr.move_to((width - sub_ext.width) / 2, (height + sub_ext.height) / 2 + 10)
            show_text(cr, subtitle)

        # Position marker even without waveform
        if self.duration > 0 and self.position > 0:
            self._draw_position_marker(
                cr, width, height, start_time, end_time, visible_duration
            )

    def on_draw(self, area, cr, width, height):
        """Draw the waveform visualization."""
        cr.set_source_rgb(*self.bg_color)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        if self.is_loading:
            self._draw_loading_indicator(cr, width, height)
            return

        visible_duration = self.duration / self.zoom_level if self.duration > 0 else 0
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
            cr.set_source_surface(self.cached_waveform_surface, 0, 0)
            cr.paint()
            self._draw_position_marker(
                cr, width, height, start_time, end_time, visible_duration
            )

        if self.duration > 0:
            self._draw_timeline_ruler(cr, width, height)

        # Draw markers after waveform
        self._draw_markers(cr, width, height)
        if self.waveform_data is not None and not self.marker_pairs:
            self._draw_hint(cr)

        self._draw_hover_time(cr, width, height)

        if self.zoom_level > 1.0:
            self._draw_scrollbar(cr, width, height)


class SeekBar(Gtk.DrawingArea):
    """Modern seekbar with rounded track, smooth thumb, hover preview, and time labels."""

    HEIGHT = 36

    def __init__(self):
        super().__init__()
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
        self.queue_draw()

    def set_position(self, position):
        self._position = max(0.0, position)
        self.update_property(
            [Gtk.AccessibleProperty.VALUE_NOW, Gtk.AccessibleProperty.VALUE_TEXT],
            [self._position, format_time_short(self._position)],
        )
        self.queue_draw()

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
        fg = (color.red, color.green, color.blue)
        bg = (0.15, 0.15, 0.17) if sum(fg) > 1.5 else (0.97, 0.97, 0.98)
        # The ring marks keyboard focus; a click to seek is not one.
        if self.get_state_flags() & Gtk.StateFlags.FOCUS_VISIBLE:
            cr.set_source_rgb(*fg)
            cr.set_line_width(2)
            cr.rectangle(1, 1, width - 2, height - 2)
            cr.stroke()

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

        self._track_x = pad
        self._track_w = track_w

        if track_w <= 0:
            return

        progress = (
            max(0.0, min(1.0, self._position / self.duration))
            if self.duration > 0
            else 0.0
        )

        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)

        elapsed_str = format_time_short(self._position)
        remaining = max(0, self.duration - self._position)
        remaining_str = f"-{format_time_short(remaining)}"

        label_y = track_y - 6

        # Elapsed (left)
        cr.set_source_rgba(*fg, 1)
        cr.move_to(pad, label_y)
        show_text(cr, elapsed_str)

        # Remaining (right)
        ext_r = text_extents(cr, remaining_str)
        cr.set_source_rgba(*fg, 1)
        cr.move_to(width - pad - ext_r.width, label_y)
        show_text(cr, remaining_str)

        # Waveform hover time (at corresponding position, with opaque bg over labels)
        if self._waveform_hover_time is not None and self.duration > 0:
            hover_str = format_time_short(self._waveform_hover_time)
            hover_progress = max(
                0.0, min(1.0, self._waveform_hover_time / self.duration)
            )
            hover_x = pad + hover_progress * track_w
            ext_h = text_extents(cr, hover_str)
            # Clamp label position within bounds
            lx = max(pad, min(width - pad - ext_h.width, hover_x - ext_h.width / 2))
            # Opaque background so it covers elapsed/remaining labels beneath
            bg_px, bg_py = 3, 2
            cr.set_source_rgba(*bg, 0.98)
            cr.rectangle(
                lx - bg_px,
                label_y - ext_h.height - bg_py,
                ext_h.width + bg_px * 2,
                ext_h.height + bg_py * 2,
            )
            cr.fill()
            cr.set_source_rgba(*fg, 0.85)
            cr.move_to(lx, label_y)
            show_text(cr, hover_str)
            # Small indicator line on track at hover position
            cr.set_source_rgba(*fg, 0.35)
            cr.set_line_width(1)
            cr.move_to(hover_x, track_y - 2)
            cr.line_to(hover_x, track_y + track_h + 2)
            cr.stroke()

        if self._zoom_level > 1.0:
            vp_frac = 1.0 / self._zoom_level
            vp_x = pad + self._viewport_offset * (1.0 - vp_frac) * track_w
            vp_w = vp_frac * track_w
            cr.set_source_rgba(*fg, 0.06)
            rounded_rect(cr, vp_x, track_y - 4, vp_w, track_h + 8, track_r + 2)
            cr.fill()
            cr.set_source_rgba(*fg, 0.12)
            cr.set_line_width(1)
            rounded_rect(
                cr, vp_x + 0.5, track_y - 3.5, vp_w - 1, track_h + 7, track_r + 2
            )
            cr.stroke()

        cr.set_source_rgba(*fg, 0.15)
        rounded_rect(cr, pad, track_y, track_w, track_h, track_r)
        cr.fill()

        elapsed_w = progress * track_w
        if elapsed_w > 0:
            pat = cairo.LinearGradient(pad, 0, pad + elapsed_w, 0)
            pat.add_color_stop_rgba(0, 0.30, 0.56, 0.92, 0.90)
            pat.add_color_stop_rgba(1, 0.45, 0.72, 1.0, 0.95)
            cr.set_source(pat)
            rounded_rect(cr, pad, track_y, elapsed_w, track_h, track_r)
            cr.fill()

        if (
            self._hover_x >= pad
            and self._hover_x <= width - pad
            and not self._is_dragging
        ):
            hover_prog = max(0.0, min(1.0, (self._hover_x - pad) / track_w))
            hx = pad + hover_prog * track_w
            cr.set_source_rgba(*fg, 0.25)
            cr.set_line_width(1)
            cr.move_to(hx, track_y - 3)
            cr.line_to(hx, track_y + track_h + 3)
            cr.stroke()

            # Hover time tooltip
            hover_str = format_time_short(self._hover_time)
            cr.set_font_size(9)
            ext = text_extents(cr, hover_str)
            tip_x = hx - ext.width / 2
            tip_x = max(pad, min(width - pad - ext.width, tip_x))
            tip_y = track_y - 8

            cr.set_source_rgba(*bg, 0.98)
            rounded_rect(
                cr, tip_x - 4, tip_y - ext.height - 2, ext.width + 8, ext.height + 4, 3
            )
            cr.fill()
            cr.set_source_rgba(*fg, 0.9)
            cr.move_to(tip_x, tip_y)
            show_text(cr, hover_str)

        thumb_x = pad + progress * track_w
        thumb_cy = track_y + track_h / 2
        show_thumb = self._is_dragging or (self._hover_x >= 0)

        if show_thumb:
            cr.set_source_rgba(0.40, 0.65, 1.0, 0.15)
            cr.arc(thumb_x, thumb_cy, thumb_r + 4, 0, 2 * math.pi)
            cr.fill()

        cr.set_source_rgba(*fg, 0.95)
        cr.arc(thumb_x, thumb_cy, thumb_r if show_thumb else 4, 0, 2 * math.pi)
        cr.fill()

        if show_thumb:
            cr.set_source_rgba(0.35, 0.60, 0.95, 1.0)
            cr.arc(thumb_x, thumb_cy, 3, 0, 2 * math.pi)
            cr.fill()
