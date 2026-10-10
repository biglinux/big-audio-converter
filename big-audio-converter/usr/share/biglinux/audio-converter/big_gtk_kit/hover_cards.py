# SPDX-FileCopyrightText: 2026 BigLinux contributors
# SPDX-License-Identifier: MIT
"""Cards that open as the pointer rests on their anchor.

The anchors sit in one line (a list, a bar) and every card opens on the same
side of it. A card opens once the pointer rests on its anchor, and the pointer
can travel from the anchor into the card across the neighboring anchors: a
triangle from where it left the anchor to the card's near edge is part of the
card's region, and an anchor inside that triangle takes over only when the
pointer leaves the triangle or rests there. The geometry is plain functions of
boxes; :class:`HoverIntent` decides with an injected clock, so tests run it
without a display; :class:`HoverCards` connects it to GTK.

A click, tap or Enter opens a card for the keyboard (focused, closed by Escape
or a click elsewhere); the pointer never replaces such a card. Escape also
closes a card the pointer opened.
"""

import math
import time
import weakref

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Graphene, Gtk

FIRST_OPEN_MS = 225  # GTK 3's submenu delay: passing over an anchor opens nothing
SWITCH_MS = 100  # with a card open, or just after one closed
TRIANGLE_REST_MS = 400  # resting on an anchor that lies on the way to the card
CLOSE_MS = 400
WARM_S = 0.3
REST_RADIUS = 4  # a hand's tremor still counts as resting
APEX_BACKOFF = 4  # one jittery sample where the pointer left the anchor
BASE_BUFFER = 8
MARGIN = 8  # between a card and the window's edge
# Adwaita's popover contents: padding and border around the card's child.
CARD_PADDING = 8
OPEN_CLASS = "card-open"


# Geometry, in the window's coordinates. A box is (x0, y0, x1, y1).


def contains(box, point):
    x, y = point
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def in_triangle(point, a, b, c):
    """Whether ``point`` lies in the triangle ``a b c``; a degenerate one holds
    nothing."""

    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    if abs(cross(a, b, c)) < 1e-9:
        return False
    signs = (cross(a, b, point), cross(b, c, point), cross(c, a, point))
    return not (any(s < 0 for s in signs) and any(s > 0 for s in signs))


def safe_triangle(apex, card, side):
    """The triangle from ``apex``, backed off toward the anchor, to the near
    edge of ``card``, which lies on ``side`` ("right", "left", "top" or
    "bottom") of the anchors, widened by a buffer at both ends."""
    x, y = apex
    x0, y0, x1, y1 = card
    b = BASE_BUFFER
    if side == "right":
        return (x - APEX_BACKOFF, y), (x0, y0 - b), (x0, y1 + b)
    if side == "left":
        return (x + APEX_BACKOFF, y), (x1, y0 - b), (x1, y1 + b)
    if side == "top":
        return (x, y + APEX_BACKOFF), (x0 - b, y1), (x1 + b, y1)
    return (x, y - APEX_BACKOFF), (x0 - b, y0), (x1 + b, y0)


def in_region(point, anchor, card, apex, side):
    """Whether ``point`` is on the anchor, the card or the way between them."""
    if contains(anchor, point) or (card is not None and contains(card, point)):
        return True
    return (
        card is not None
        and apex is not None
        and in_triangle(point, *safe_triangle(apex, card, side))
    )


def card_offset(side, anchor, card_size, window_size, margin=MARGIN):
    """``(dx, dy, max_height)`` that move a card GTK centers on ``anchor`` so
    it stays inside the window: beside the anchors its top lines up with the
    anchor's; above or below them it stays centered where it fits. A taller
    card than ``max_height`` must shrink to it."""
    ax0, ay0, ax1, ay1 = anchor
    width, height = card_size
    window_width, window_height = window_size
    if side in ("right", "left"):
        max_height = max(window_height - 2 * margin, 0)
        height = min(height, max_height)
        top = max(margin, min(ay0, window_height - margin - height))
        return 0, round(top - ((ay0 + ay1) / 2 - height / 2)), max_height
    centered = (ax0 + ax1) / 2 - width / 2
    left = max(margin, min(centered, window_width - margin - width))
    room = ay0 - margin if side == "top" else window_height - ay1 - margin
    return round(left - centered), 0, max(room, 0)


class HoverIntent:
    """Which card the pointer means, decided from where it rests and goes.

    ``boxes(name)`` gives the anchor's box and its card's box (``None`` while
    closed) in one coordinate space; ``show``/``hide`` open and close a card;
    ``later(ms, callback)``/``cancel(id)`` schedule; ``now()`` is seconds.
    """

    def __init__(self, side, boxes, show, hide, later, cancel, now, **hooks):
        self.side = side
        self._boxes = boxes
        self._show = show
        self._hide = hide
        self._later = later
        self._cancel = cancel
        self._now = now
        self._can_open = hooks.get("can_open") or (lambda _name: True)
        self._is_held = hooks.get("is_held") or (lambda _name: False)
        self.owner = None  # the card the pointer opened
        self.pinned = None  # the card a click or key opened
        self.apex = None  # where the pointer last was on the owner's anchor
        self.suppressed = None  # closed by Escape; reopens once the pointer leaves
        self._rest = None  # (name, point, delay, source)
        self._closing = None
        self._warm_until = -math.inf

    def pointer(self, name, point):
        """The pointer is at ``point``, over anchor ``name`` or none."""
        if self.pinned is not None:
            return
        if self.suppressed is not None and name != self.suppressed:
            self.suppressed = None
        if self.owner is None:
            if name is None or name == self.suppressed:
                self._cancel_rest()
                return
            warm = self._now() < self._warm_until
            self._rest_on(name, point, SWITCH_MS if warm else FIRST_OPEN_MS)
            return
        if name == self.owner:
            self.apex = point
            self._cancel_close()
            self._cancel_rest()
            return
        anchor, card = self._boxes(self.owner)
        on_the_way = (
            card is not None
            and self.apex is not None
            and in_triangle(point, *safe_triangle(self.apex, card, self.side))
        )
        if on_the_way or contains(anchor, point) or (card and contains(card, point)):
            self._cancel_close()
        else:
            self._schedule_close()
        if name is None or name == self.suppressed:
            self._cancel_rest()
            return
        self._rest_on(name, point, TRIANGLE_REST_MS if on_the_way else SWITCH_MS)

    def left_anchor(self, name):
        if self._rest is not None and self._rest[0] == name:
            self._cancel_rest()
        if self.suppressed == name:
            self.suppressed = None

    def left_window(self):
        self._cancel_rest()
        self._schedule_close()

    def entered_card(self, name):
        if name == self.owner:
            self._cancel_close()
            self._cancel_rest()

    def left_card(self, name):
        if name == self.owner:
            self._schedule_close()

    def open(self, name, apex):
        """Open ``name``'s card as the pointer would, the pointer at ``apex``."""
        self._cancel_rest()
        self._cancel_close()
        if self.owner is not None and self.owner != name:
            self._hide(self.owner)
        self.owner, self.apex = name, apex
        self._show(name)

    def pin(self, name):
        """A click or key opens ``name``'s card; the caller shows it."""
        self._cancel_rest()
        self._cancel_close()
        if self.owner is not None:
            owner, self.owner, self.apex = self.owner, None, None
            self._hide(owner)
        self.pinned = name

    def closed(self, name):
        """The card closed, for whatever reason."""
        if name == self.owner:
            self.owner = self.apex = None
            self._cancel_close()
        if name == self.pinned:
            self.pinned = None

    def escape(self):
        """Close the pointer's card; True when there was one."""
        if self.owner is None:
            return False
        name, self.owner, self.apex = self.owner, None, None
        self._cancel_close()
        self._cancel_rest()
        self.suppressed = name
        self._hide(name)
        return True

    def close_now(self):
        """Close the pointer's card at once, as when the anchors scroll."""
        self._cancel_rest()
        if self.owner is not None:
            name, self.owner, self.apex = self.owner, None, None
            self._cancel_close()
            self._hide(name)

    def cancel_all(self):
        self._cancel_rest()
        self._cancel_close()

    def _rest_on(self, name, point, delay):
        if self._rest is not None:
            rest_name, rest_point, rest_delay, _source = self._rest
            if (
                rest_name == name
                and rest_delay == delay
                and math.dist(point, rest_point) < REST_RADIUS
            ):
                return
        self._cancel_rest()
        source = self._later(delay, self._rested)
        self._rest = (name, point, delay, source)

    def _rested(self):
        name, point, _delay, _source = self._rest
        self._rest = None
        if self.pinned is None and self._can_open(name):
            self.open(name, point)

    def _cancel_rest(self):
        if self._rest is not None:
            self._cancel(self._rest[3])
            self._rest = None

    def _schedule_close(self):
        if self.owner is not None and self._closing is None:
            self._closing = self._later(CLOSE_MS, self._close_by_pointer)

    def _cancel_close(self):
        if self._closing is not None:
            self._cancel(self._closing)
            self._closing = None

    def _close_by_pointer(self):
        self._closing = None
        # A slider dragged outside, or a dropdown open in the card, is in use;
        # the next pointer move outside closes it.
        if self.owner is None or self._is_held(self.owner):
            return
        name, self.owner, self.apex = self.owner, None, None
        self._warm_until = self._now() + WARM_S
        self._hide(name)


_SIDES = {
    Gtk.PositionType.RIGHT: "right",
    Gtk.PositionType.LEFT: "left",
    Gtk.PositionType.TOP: "top",
    Gtk.PositionType.BOTTOM: "bottom",
}


def _weak(method):
    """A signal handler that does not keep the method's owner alive: the
    widgets hold their controllers, and the owner holds the widgets."""
    reference = weakref.WeakMethod(method)

    def invoke(*args):
        callback = reference()
        return callback(*args) if callback is not None else None

    return invoke


def _child_popup_visible(widget):
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Popover) and child.get_visible():
            return True
        if _child_popup_visible(child):
            return True
        child = child.get_next_sibling()
    return False


class HoverCards:
    """Popovers that open as the pointer rests on their anchors.

    ``side`` is where every card opens relative to the line of anchors.
    ``can_open(name)`` may refuse to open; ``point_at(name)`` may set the
    popover's pointing rectangle before it is placed. ``later``, ``cancel``
    and ``now`` default to GLib timeouts and the monotonic clock.
    """

    def __init__(
        self, side=Gtk.PositionType.RIGHT, later=None, cancel=None, now=time.monotonic
    ):
        self.side = side
        self.popovers = {}  # name -> popover
        self._anchors = {}  # name -> anchor
        self._scrollers = {}  # name -> the scroller that caps the card's height
        self._focus = {}  # name -> widget focused when a click opens the card
        self._inside = {}  # name -> the popover's motion controller
        self._held = set()  # names with a button down inside
        self._sources = set()
        self._root = None
        self._root_controllers = ()
        self._after_paint = None  # (frame clock, handler) while a card is open
        self._placed = {}  # name -> (anchor box, window size) it was placed at
        self.can_open = None
        self.point_at = None
        self._intent = HoverIntent(
            _SIDES[side],
            self._boxes,
            self._show,
            self.close,
            later or self._later,
            cancel or self._cancel,
            now,
            can_open=self._allowed,
            is_held=self._is_held,
        )

    # Public API.

    def attach(self, name, anchor, popover, focus=None):
        popover.set_autohide(False)
        popover.set_parent(anchor)
        # Scrolls only when the card is taller than the window.
        card = popover.get_child()
        popover.set_child(None)
        scroller = Gtk.ScrolledWindow(
            child=card,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
            propagate_natural_width=True,
        )
        popover.set_child(scroller)
        self.popovers[name] = popover
        self._anchors[name] = anchor
        self._scrollers[name] = scroller
        self._focus[name] = focus

        hover = Gtk.EventControllerMotion()
        hover.connect("enter", _weak(self._on_anchor_pointer), name)
        hover.connect("motion", _weak(self._on_anchor_pointer), name)
        hover.connect("leave", _weak(self._on_anchor_leave), name)
        anchor.add_controller(hover)
        inside = Gtk.EventControllerMotion()
        inside.connect("enter", _weak(self._on_card_enter), name)
        inside.connect("leave", _weak(self._on_card_leave), name)
        popover.add_controller(inside)
        self._inside[name] = inside
        # Sees presses and releases without claiming them from the controls.
        buttons = Gtk.EventControllerLegacy(
            propagation_phase=Gtk.PropagationPhase.CAPTURE
        )
        buttons.connect("event", _weak(self._on_popover_event), name)
        popover.add_controller(buttons)
        popover.connect("closed", _weak(self._on_closed), name)

    def open(self, name):
        """Open ``name``'s card as the pointer resting on its anchor would."""
        box = self._anchor_box(name)
        apex = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2) if box else None
        self._intent.open(name, apex)

    def open_for_keyboard(self, name):
        self._intent.pin(name)
        self._present(name, autohide=True)
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

    def close_hover(self):
        """Close the card the pointer opened, as when its anchors scroll."""
        self._intent.close_now()

    def reposition(self, name):
        """Place an open card again if its anchor or the window moved."""
        popover = self.popovers[name]
        if popover.get_visible() and self._placed.get(name) != self._placement(name):
            self._place(name)
            popover.present()

    def unparent_all(self):
        self._intent.cancel_all()
        self._cancel_after_paint()
        for source in list(self._sources):
            GLib.source_remove(source)
        self._sources.clear()
        self._unfollow()
        for popover in self.popovers.values():
            popover.unparent()

    # Placement and geometry.

    def _show(self, name):
        self._present(name, autohide=False)

    def _present(self, name, autohide):
        for other, popover in self.popovers.items():
            if other != name:
                popover.popdown()
        popover = self.popovers[name]
        popover.set_autohide(autohide)
        self._place(name)
        self._follow(self._anchors[name].get_root())
        self._anchors[name].add_css_class(OPEN_CLASS)
        popover.popup()

    def _place(self, name):
        if self.point_at is not None:
            self.point_at(name)
        anchor = self._anchors[name]
        root = anchor.get_root()
        box = self._anchor_box(name)
        if root is None or box is None:
            return
        child = self._scrollers[name].get_child()
        _min, width, _mb, _nb = child.measure(Gtk.Orientation.HORIZONTAL, -1)
        _min, height, _mb, _nb = child.measure(Gtk.Orientation.VERTICAL, width)
        pad = 2 * CARD_PADDING
        dx, dy, max_height = card_offset(
            _SIDES[self.side],
            box,
            (width + pad, height + pad),
            (root.get_width(), root.get_height()),
        )
        self._scrollers[name].set_max_content_height(max(max_height - pad, 1))
        self.popovers[name].set_offset(dx, dy)
        self._placed[name] = self._placement(name)

    def _placement(self, name):
        root = self._anchors[name].get_root()
        size = (root.get_width(), root.get_height()) if root is not None else None
        return self._anchor_box(name), size

    def _anchor_box(self, name):
        anchor = self._anchors[name]
        root = anchor.get_root()
        if root is None:
            return None
        found, bounds = anchor.compute_bounds(root)
        if not found:
            return None
        x, y = bounds.get_x(), bounds.get_y()
        return (x, y, x + bounds.get_width(), y + bounds.get_height())

    def _card_box(self, name):
        """The card as drawn, in the window's coordinates, or None."""
        popover = self.popovers[name]
        root = self._anchors[name].get_root()
        surface = popover.get_surface()
        if root is None or not popover.get_visible():
            return None
        if not isinstance(surface, Gdk.Popup):
            return None
        found, card = popover.get_child().compute_bounds(popover)
        if not found:
            return None
        # The popup sits in the window's surface, whose origin lies outside
        # the root widget by the window's shadow; the popup's own shadow is no
        # part of the card.
        offset_x, offset_y = root.get_surface_transform()
        shadow_x, shadow_y = popover.get_surface_transform()
        left = surface.get_position_x() - offset_x + shadow_x + card.get_x()
        top = surface.get_position_y() - offset_y + shadow_y + card.get_y()
        return (
            left - CARD_PADDING,
            top - CARD_PADDING,
            left + card.get_width() + CARD_PADDING,
            top + card.get_height() + CARD_PADDING,
        )

    def _boxes(self, name):
        return self._anchor_box(name), self._card_box(name)

    def _anchor_at(self, point):
        for name, anchor in self._anchors.items():
            if not anchor.get_mapped():
                continue
            box = self._anchor_box(name)
            if box is not None and contains(box, point):
                return name
        return None

    def _allowed(self, name):
        anchor = self._anchors[name]
        return (
            anchor.get_mapped()
            and anchor.is_sensitive()
            and (self.can_open is None or self.can_open(name))
        )

    def _is_held(self, name):
        return name in self._held or _child_popup_visible(self.popovers[name])

    # Events.

    def _on_anchor_pointer(self, controller, x, y, name):
        if self._root_controllers:
            return  # the window's controller follows the pointer meanwhile
        anchor = self._anchors[name]
        root = anchor.get_root()
        if root is None:
            return
        found, point = anchor.compute_point(root, Graphene.Point().init(x, y))
        if found:
            self._intent.pointer(name, (point.x, point.y))

    def _on_anchor_leave(self, _controller, name):
        self._intent.left_anchor(name)

    def _on_card_enter(self, _controller, _x, _y, name):
        self._intent.entered_card(name)

    def _on_card_leave(self, _controller, name):
        self._intent.left_card(name)

    def _on_window_leave(self, _controller):
        self._intent.left_window()

    def _on_window_motion(self, _controller, x, y):
        self._intent.pointer(self._anchor_at((x, y)), (x, y))

    def _on_window_key(self, _controller, keyval, _keycode, _state):
        return bool(keyval == Gdk.KEY_Escape and self._intent.escape())

    def _on_popover_event(self, _controller, event, name):
        kind = event.get_event_type()
        if kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.TOUCH_BEGIN):
            self._held.add(name)
        elif kind in (
            Gdk.EventType.BUTTON_RELEASE,
            Gdk.EventType.TOUCH_END,
            Gdk.EventType.TOUCH_CANCEL,
        ):
            self._held.discard(name)
            if not self._inside[name].contains_pointer():
                self._intent.left_card(name)
        return False

    def _on_closed(self, popover, name):
        self._anchors[name].remove_css_class(OPEN_CLASS)
        self._held.discard(name)
        popover.set_autohide(False)
        self._intent.closed(name)
        if not any(p.get_visible() for p in self.popovers.values()):
            self._unfollow()

    def _follow(self, root):
        """While a card is open, follow the pointer over the whole window,
        where the way to the card lies, and take Escape."""
        if root is None or self._root_controllers:
            return
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", _weak(self._on_window_motion))
        motion.connect("leave", _weak(self._on_window_leave))
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", _weak(self._on_window_key))
        root.add_controller(motion)
        root.add_controller(keys)
        self._root = root
        self._root_controllers = (motion, keys)
        # A card follows its anchor whenever a frame moved it: a resize, a
        # reflow, a scroll. Frames are drawn only when something changes.
        clock = root.get_frame_clock()
        if clock is not None:
            handler = clock.connect("after-paint", _weak(self._on_after_paint))
            self._after_paint = (clock, handler)

    def _unfollow(self):
        controllers, self._root_controllers = self._root_controllers, ()
        for controller in controllers:
            if controller.get_widget() is not None:
                controller.get_widget().remove_controller(controller)
        self._cancel_after_paint()
        self._root = None

    def _on_after_paint(self, _clock):
        for name in self.popovers:
            self.reposition(name)

    def _cancel_after_paint(self):
        if self._after_paint is not None:
            clock, handler = self._after_paint
            self._after_paint = None
            clock.disconnect(handler)

    # Default scheduling.

    def _later(self, milliseconds, callback):
        holder = []

        def fire():
            self._sources.discard(holder[0])
            callback()
            return GLib.SOURCE_REMOVE

        holder.append(GLib.timeout_add(milliseconds, fire))
        self._sources.add(holder[0])
        return holder[0]

    def _cancel(self, source):
        if source in self._sources:
            self._sources.discard(source)
            GLib.source_remove(source)
