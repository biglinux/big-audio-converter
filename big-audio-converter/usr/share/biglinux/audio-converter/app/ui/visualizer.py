"""The waveform with its marks, zoom and scrollbar."""

import gettext
import logging

import gi
import numpy as np

from app.ui.waveform_drawing import WaveformDrawingMixin
from app.ui.waveform_segments import WaveformSegmentsMixin

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk

_ = gettext.gettext

logger = logging.getLogger(__name__)


class AudioVisualizer(WaveformSegmentsMixin, WaveformDrawingMixin, Gtk.DrawingArea):
    """The waveform with its marks, zoom, scrollbar and time ruler."""

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

        self.add_css_class("waveform")

        # Waveform rendering cache for performance
        self.cached_viewport_key = None  # (start_sample, end_sample, width, height)
        self.cached_waveform_surface = (
            None  # Pre-rendered waveform as Cairo ImageSurface
        )

        self.seek_position_callback = None
        self.hover_time_callback = None  # Callback for waveform hover time changes
        self._init_marker_state()

        self.player = None

        # Handlers reach the widget through the controller: a closure over self
        # would keep the widget alive through its own controllers.
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

        self.zoom_level = 1.0  # 1.0 = no zoom, higher = zoomed in
        self.show_hint = True  # how to mark, until the first segment
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
        self.is_loading = is_loading
        self.loading_message = (
            message if message is not None else _("Generating waveform...")
        )
        self.queue_draw()

    def set_waveform(self, data, duration):
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
        self.position = position

        # Auto-follow: adjust viewport during playback OR when seeking outside visible range
        if self.duration > 0 and self.zoom_level > 1.0:
            width = self.get_width()
            if width > 0:
                visible_duration = self.visible_duration
                start_time = self.viewport_offset * self.duration
                end_time = start_time + visible_duration

                marker_pixel_x = ((position - start_time) / visible_duration) * width

                is_playing = self.player and self.player.is_playing()

                # When playing, follow with margin before edge
                if is_playing:
                    edge_threshold = 50  # pixels
                    if marker_pixel_x > width - edge_threshold:
                        # Shift viewport forward to keep marker visible
                        new_start_time = position - visible_duration * 0.3
                        max_offset = self.max_scroll_offset
                        self.viewport_offset = max(
                            0, min(max_offset, new_start_time / self.duration)
                        )
                    elif marker_pixel_x < 0:
                        new_start_time = position - visible_duration * 0.7
                        max_offset = self.max_scroll_offset
                        self.viewport_offset = max(
                            0, min(max_offset, new_start_time / self.duration)
                        )
                else:
                    # Seeking while paused centers a position outside the view.
                    if position < start_time or position > end_time:
                        new_start_time = position - visible_duration * 0.5
                        max_offset = self.max_scroll_offset
                        self.viewport_offset = max(
                            0, min(max_offset, new_start_time / self.duration)
                        )
                        self._drop_cached_surface()

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
        visible_duration = self.visible_duration
        start_time = self.viewport_offset * self.duration
        position = start_time + (x / width) * visible_duration
        position = round(position, 3)

        if self.hovering_delete_button and self.hovered_segment_index >= 0:
            self._prompt_delete_segment(self.hovered_segment_index)
            return

        marker_info = self._find_marker_at_position(x, y)
        if marker_info:
            self.is_dragging_marker = True
            self.dragging_pair_index = marker_info["index"]
            self.dragging_marker_type = marker_info["type"]
            if self.marker_drag_callback:
                self.marker_drag_callback(True)
            return

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
        # End pan gesture if active
        if self.pan_gesture_active:
            self.pan_gesture_active = False
            self.set_cursor(None)
            return

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
        if self._is_over_scrollbar_thumb(start_x, start_y):
            self.is_dragging_scrollbar = True
            self.scrollbar_drag_start_offset = self.viewport_offset
            self.queue_draw()
            return

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

        # We don't want to immediately enter drag mode - that happens in update

    def _drag_update_single_marker(self, pair, current_x, width, marker_type):
        visible_duration = self.visible_duration
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
        visible_duration = self.visible_duration
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
        if self.is_dragging_scrollbar:
            width = self.get_width()
            scrollbar_bounds = self._get_scrollbar_bounds(width, self.get_height())
            if scrollbar_bounds:
                visible_fraction = 1.0 / self.zoom_level
                thumb_width = max(30, scrollbar_bounds["width"] * visible_fraction)
                max_thumb_travel = scrollbar_bounds["width"] - thumb_width

                if max_thumb_travel > 0:
                    max_offset = 1.0 - visible_fraction
                    delta_offset = (offset_x / max_thumb_travel) * max_offset
                    new_offset = self.scrollbar_drag_start_offset + delta_offset
                    self.viewport_offset = max(0, min(max_offset, new_offset))
                    self.queue_draw()
            return

        if self.pan_gesture_active:
            width = self.get_width()
            delta_fraction = -offset_x / width  # Negative for natural scrolling

            new_offset = (
                self.pan_gesture_start_offset + delta_fraction / self.zoom_level
            )

            # Clamp to valid range
            max_offset = self.max_scroll_offset
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
            visible_duration = self.visible_duration
            start_time = self.viewport_offset * self.duration
            here = start_time + (current_x / width) * visible_duration
            here = round(max(0, min(here, self.duration)), 3)
            pair["start"] = min(self.new_segment_anchor, here)
            pair["stop"] = max(self.new_segment_anchor, here)
            self.queue_draw()
            if self.seek_position_callback:
                self.seek_position_callback(here, False)

    def on_drag_end(self, gesture, offset_x, offset_y):
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

        modifiers = event.get_modifier_state() if event else 0
        shift_pressed = (modifiers & Gdk.ModifierType.SHIFT_MASK) != 0

        # Horizontal panning with Shift+Scroll or trackpad horizontal scroll
        if shift_pressed or (abs(dx) > abs(dy) and abs(dx) > 0):
            if self.zoom_level > 1.0:
                # Pan horizontally
                pan_speed = 0.05  # Adjust sensitivity
                scroll_delta = dx if abs(dx) > abs(dy) else dy
                delta_offset = scroll_delta * pan_speed / self.zoom_level

                max_offset = self.max_scroll_offset
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

        self.set_zoom_level(new_zoom, use_mouse_position=True)

        if self.zoom_changed_callback:
            self.zoom_changed_callback(self.zoom_level)

        return True  # Event handled

    def on_key_pressed(self, controller, keyval, keycode, state):
        """Handle keyboard shortcuts for zoom and segment deletion."""
        if not self.duration > 0:
            return False

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
            max_offset = self.max_scroll_offset

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

        # Anchor: the hovered point, else the playback position, else the center.
        mouse_fraction = None

        if use_mouse_position and self.hover_x is not None:
            width = self.get_width()
            if width > 0:
                mouse_fraction = self.hover_x / width

        if mouse_fraction is None and self.position > 0:
            visible_duration = self.visible_duration
            start_time = self.viewport_offset * self.duration
            end_time = start_time + visible_duration
            if start_time <= self.position <= end_time:
                mouse_fraction = (self.position - start_time) / visible_duration

        if mouse_fraction is None:
            mouse_fraction = 0.5

        visible_duration = self.visible_duration
        start_time = self.viewport_offset * self.duration
        anchor_time = start_time + mouse_fraction * visible_duration

        self.zoom_level = new_zoom

        # Adjust viewport to keep anchor position
        new_visible_duration = self.visible_duration
        new_start_time = anchor_time - mouse_fraction * new_visible_duration

        # Clamp viewport offset
        self.viewport_offset = max(
            0, min(self.max_scroll_offset, new_start_time / self.duration)
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

        if self.duration > 0:
            self.hover_x = x
            # Account for zoom and pan
            visible_duration = self.visible_duration
            start_time = self.viewport_offset * self.duration
            self.hover_time = start_time + (x / width) * visible_duration

            if self.hover_time_callback:
                self.hover_time_callback(self.hover_time)

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

        # Markers and segments answer across the waveform's full height.
        if not self.is_dragging_marker:
            if self.hovering_delete_button:
                self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
                return

            marker_info = self._find_marker_at_position(x, y)
            if marker_info:
                if marker_info["type"] == "start":
                    self.set_cursor(Gdk.Cursor.new_from_name("w-resize"))
                else:
                    self.set_cursor(Gdk.Cursor.new_from_name("e-resize"))
                return

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

    def _get_scrollbar_bounds(self, width, height):
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
        if self.zoom_level <= 1.0:
            return None

        scrollbar_bounds = self._get_scrollbar_bounds(width, height)
        if not scrollbar_bounds:
            return None

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
