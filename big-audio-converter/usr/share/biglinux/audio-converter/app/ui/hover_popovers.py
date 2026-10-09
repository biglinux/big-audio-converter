"""Popovers that open as the pointer reaches their anchor.

The anchor, its popover and the band between them count as one region: the
popover stays while the pointer is anywhere in it, even over another anchor
on the way unless the pointer rests there, and closes a moment after the
pointer leaves it. A click opens
one for the keyboard instead: focused, and closed by Escape or a click
elsewhere.
"""

from gi.repository import Gdk, Graphene, Gtk

from app.utils.main_loop import weak_callback

CLOSE_DELAY_MS = 300


def _in_band(x, y, first, second):
    """Whether (x, y) lies between two boxes (x0, y0, x1, y1), in the band
    that narrows or widens from one to the other: up or down when one is
    above the other, otherwise sideways."""
    if first[3] <= second[1] or second[3] <= first[1]:
        upper, lower = sorted((first, second), key=lambda b: b[1])
        start, end, along, across = upper[3], lower[1], y, x
        near, far = (upper[0], upper[2]), (lower[0], lower[2])
    else:
        left, right = sorted((first, second), key=lambda b: b[0])
        start, end, along, across = left[2], right[0], x, y
        near, far = (left[1], left[3]), (right[1], right[3])
    if not start <= along <= end:
        return False
    t = (along - start) / (end - start) if end > start else 0
    low = near[0] + t * (far[0] - near[0])
    high = near[1] + t * (far[1] - near[1])
    return low <= across <= high


class HoverPopovers:
    def __init__(self, sources, open_delay_ms=0):
        """``open_delay_ms``: how long the pointer must rest on an anchor
        before its popover opens, so passing over one opens nothing."""
        self._sources = sources
        self._open_delay_ms = open_delay_ms
        self._opening = None  # pending open, while the pointer rests
        self.popovers = {}  # name -> popover
        self._anchors = {}  # name -> anchor widget
        self._focus = {}  # name -> widget given focus when opened by a click
        self._pointers = {}  # name -> motion controller of the popover
        self._keyboard = set()  # names opened or entered by the keyboard
        self._held = set()  # names with a button down inside (a slider drag)
        self._closing = {}  # name -> pending close
        self._window_motion = None  # the window's pointer, while a card is open
        self.can_open = None  # optional check before opening on hover
        self.before_open = None  # optional, to place a popover as it opens

    def attach(self, name, anchor, popover, focus=None):
        popover.set_autohide(False)
        popover.set_parent(anchor)
        self.popovers[name] = popover
        self._anchors[name] = anchor
        self._focus[name] = focus

        # Motion as well as enter: the pointer may already rest on the anchor
        # when opening becomes possible.
        hover = Gtk.EventControllerMotion()
        hover.connect("enter", weak_callback(self._on_anchor_pointer), name)
        hover.connect("motion", weak_callback(self._on_anchor_pointer), name)
        hover.connect("leave", weak_callback(self._on_anchor_leave), name)
        anchor.add_controller(hover)
        pointer = Gtk.EventControllerMotion()
        pointer.connect("enter", weak_callback(self._on_popover_enter), name)
        pointer.connect("leave", weak_callback(self._schedule_close), name)
        popover.add_controller(pointer)
        self._pointers[name] = pointer
        focus_controller = Gtk.EventControllerFocus()
        focus_controller.connect("enter", weak_callback(self._on_focus_enter), name)
        focus_controller.connect("leave", weak_callback(self._on_focus_leave), name)
        popover.add_controller(focus_controller)
        # Sees presses and releases without claiming them from the controls.
        buttons = Gtk.EventControllerLegacy(
            propagation_phase=Gtk.PropagationPhase.CAPTURE
        )
        buttons.connect("event", weak_callback(self._on_popover_event), name)
        popover.add_controller(buttons)
        popover.connect("closed", weak_callback(self._on_closed), name)

    def open(self, name):
        self._cancel_opening()
        self._cancel_close(name)
        for other, popover in self.popovers.items():
            if other != name:
                popover.popdown()
        self._follow(self._anchors[name].get_root())
        if self.before_open is not None:
            self.before_open(name)
        self.popovers[name].popup()

    def open_for_keyboard(self, name):
        popover = self.popovers[name]
        popover.popdown()
        popover.set_autohide(True)
        self.open(name)
        focus = self._focus[name]
        if callable(focus):
            focus = focus()  # the control to focus can change between openings
        if focus is not None:
            focus.grab_focus()

    def close(self, name):
        self.popovers[name].popdown()

    def close_all(self):
        for popover in self.popovers.values():
            popover.popdown()

    def unparent_all(self):
        self._cancel_opening()
        self._unfollow()
        for popover in self.popovers.values():
            popover.unparent()

    def _follow(self, root):
        """While a card is open, watch the pointer over the whole window,
        where the band between the anchor and the card lies."""
        if root is None or self._window_motion is not None:
            return
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", weak_callback(self._on_window_motion), root)
        motion.connect("leave", weak_callback(self._on_window_leave))
        root.add_controller(motion)
        self._window_motion = motion

    def _unfollow(self):
        motion, self._window_motion = self._window_motion, None
        if motion is not None and motion.get_widget() is not None:
            motion.get_widget().remove_controller(motion)

    def _shown_by_pointer(self):
        for name, popover in self.popovers.items():
            if popover.get_visible() and not popover.get_autohide():
                return name
        return None

    def _in_region(self, name, widget, x, y):
        """Whether (x, y) of ``widget`` is on the anchor, the popover or the
        band between them."""
        anchor = self._anchors[name]
        root = anchor.get_root()
        surface = self.popovers[name].get_surface()
        if root is None or not isinstance(surface, Gdk.Popup):
            return False
        found, point = widget.compute_point(root, Graphene.Point().init(x, y))
        found_anchor, box = anchor.compute_bounds(root)
        if not (found and found_anchor):
            return False
        x, y = point.x, point.y
        anchor_box = (
            box.get_x(),
            box.get_y(),
            box.get_x() + box.get_width(),
            box.get_y() + box.get_height(),
        )
        # The popup is placed in the window's surface, whose origin lies
        # outside the root widget by the window's own shadow. The popup's own
        # shadow is no part of the card: under a card it covers the
        # neighboring anchors, which would then never take over.
        popover = self.popovers[name]
        found_card, card = popover.get_child().compute_bounds(popover)
        if not found_card:
            return False
        offset_x, offset_y = root.get_surface_transform()
        shadow_x, shadow_y = popover.get_surface_transform()
        left = surface.get_position_x() - offset_x + shadow_x + card.get_x()
        top = surface.get_position_y() - offset_y + shadow_y + card.get_y()
        popover_box = (left, top, left + card.get_width(), top + card.get_height())
        for x0, y0, x1, y1 in (anchor_box, popover_box):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return True
        return _in_band(x, y, anchor_box, popover_box)

    def _on_anchor_pointer(self, controller, x, y, name):
        shown = self._shown_by_pointer()
        if shown == name:
            self._cancel_close(name)
        elif shown is not None and self._in_region(
            shown, controller.get_widget(), x, y
        ):
            # Crossing this anchor on the way to the open popover keeps it;
            # resting on this one opens its own, where there is a wait.
            self._cancel_close(shown)
            if self._open_delay_ms:
                self._cancel_opening()
                self._opening = self._sources.later(
                    self._open_delay_ms, self._open_by_pointer, name
                )
        elif not self._open_delay_ms:
            if self.can_open is None or self.can_open(name):
                self.open(name)
        else:
            # Each motion starts the wait again: it opens once the pointer rests.
            self._cancel_opening()
            self._opening = self._sources.later(
                self._open_delay_ms, self._open_by_pointer, name
            )

    def _open_by_pointer(self, name):
        self._opening = None
        if self.can_open is None or self.can_open(name):
            self.open(name)

    def _cancel_opening(self):
        if self._opening is not None:
            self._sources.cancel(self._opening)
            self._opening = None

    def _on_anchor_leave(self, controller, name):
        self._cancel_opening()
        self._schedule_close(controller, name)

    def _on_window_motion(self, _controller, x, y, root):
        shown = self._shown_by_pointer()
        if shown is None:
            return
        if self._in_region(shown, root, x, y):
            self._cancel_close(shown)
        else:
            self._schedule_close(None, shown)

    def _on_window_leave(self, _controller):
        # Into the popover, whose enter cancels this, or off the window.
        shown = self._shown_by_pointer()
        if shown is not None:
            self._schedule_close(None, shown)

    def _on_popover_enter(self, _controller, _x, _y, name):
        self._cancel_close(name)

    def _schedule_close(self, _controller, name):
        popover = self.popovers[name]
        if name in self._keyboard or popover.get_autohide() or name in self._closing:
            return
        self._closing[name] = self._sources.later(
            CLOSE_DELAY_MS, self._close_by_pointer, name
        )

    def _close_by_pointer(self, name):
        self._closing.pop(name, None)
        # Checked when the delay ends: a slider dragged outside is still in use.
        if name not in self._keyboard and name not in self._held:
            self.popovers[name].popdown()

    def _on_popover_event(self, controller, event, name):
        kind = event.get_event_type()
        if kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.TOUCH_BEGIN):
            self._held.add(name)
        elif kind in (
            Gdk.EventType.BUTTON_RELEASE,
            Gdk.EventType.TOUCH_END,
            Gdk.EventType.TOUCH_CANCEL,
        ):
            self._held.discard(name)
            if not self._pointers[name].contains_pointer():
                self._schedule_close(controller, name)
        return False

    def _on_focus_enter(self, _controller, name):
        # Focus brought by a click or drag in the popover is the pointer's.
        if not self._pointers[name].contains_pointer():
            self._keyboard.add(name)
        self._cancel_close(name)

    def _on_focus_leave(self, controller, name):
        self._keyboard.discard(name)
        self._schedule_close(controller, name)

    def _on_closed(self, popover, name):
        if not any(other.get_visible() for other in self.popovers.values()):
            self._unfollow()
        self._cancel_close(name)
        self._keyboard.discard(name)
        self._held.discard(name)
        popover.set_autohide(False)

    def _cancel_close(self, name):
        source = self._closing.pop(name, None)
        if source is not None:
            self._sources.cancel(source)
