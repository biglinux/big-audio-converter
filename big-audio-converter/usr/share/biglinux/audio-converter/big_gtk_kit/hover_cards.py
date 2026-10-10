# SPDX-FileCopyrightText: 2026 BigLinux contributors
# SPDX-License-Identifier: MIT
"""Cards that open as the pointer rests on their anchor.

The anchors sit in one line (a list, a bar) and every card opens on the same
side of it, in a layer above the window's content, joined to its anchor by a
tab: the open anchor, the tab and the card's near edge share one color. A card opens once the pointer rests on its anchor, and the pointer
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

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, GLib, GObject, Graphene, Gsk, Gtk

FIRST_OPEN_MS = 225  # GTK 3's submenu delay: passing over an anchor opens nothing
SWITCH_MS = 100  # with a card open, or just after one closed
TRIANGLE_REST_MS = 400  # resting on an anchor that lies on the way to the card
CLOSE_MS = 400
WARM_S = 0.3
REST_RADIUS = 4  # a hand's tremor still counts as resting
APEX_BACKOFF = 4  # one jittery sample where the pointer left the anchor
BASE_BUFFER = 8
MARGIN = 8  # between a card and the window's edge
TAB_RADIUS = 12  # a boxed list's corners; the card and the tab's curves match it
OVERLAP = 1  # the tab reaches over the card's outline
INTO_ANCHOR = 4  # and over the container's outline and edge shading
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


def card_box(side, anchor, edge, size, window, margin=MARGIN):
    """The card's box beside ``anchor``, its near edge on ``edge`` (an x for
    "right"/"left", a y for "top"/"bottom"), inside ``window`` by ``margin``.
    Beside the anchors its top lines up with the anchor's; above or below them
    it is centered on the anchor where it fits. A card taller (or, above a
    bar, higher) than the room is cut to it; the caller scrolls the rest."""
    ax0, ay0, ax1, _ay1 = anchor
    width, height = size
    window_width, window_height = window
    if side in ("right", "left"):
        left = edge if side == "right" else edge - width
        # Level with the anchor, scrolling the rest, while the room below
        # holds three anchors' height: its header then stays beside the
        # anchor. Only a card with less room rises to fit.
        room = window_height - margin - ay0
        if ay0 >= margin and min(height, 3 * (anchor[3] - ay0)) <= room < height:
            return (left, ay0, left + width, ay0 + room)
        height = min(height, max(window_height - 2 * margin, 0))
        top = max(margin, min(ay0, window_height - margin - height))
        return (left, top, left + width, top + height)
    left = (ax0 + ax1) / 2 - width / 2
    left = max(margin, min(left, window_width - margin - width))
    # An edge that would stop just short of the anchor's lines up with it, so
    # the tab joins that corner instead of curving into a sliver.
    if 0 < ax0 - left < TAB_RADIUS and ax0 + width <= window_width - margin:
        left = ax0
    elif 0 < left + width - ax1 < TAB_RADIUS and ax1 - width >= margin:
        left = ax1 - width
    if side == "top":
        height = min(height, max(edge - margin, 0))
        return (left, edge - height, left + width, edge)
    height = min(height, max(window_height - margin - edge, 0))
    return (left, edge, left + width, edge + height)


def _fillets(gap, at_anchor, beyond, radius=TAB_RADIUS):
    """Radii of the concave corners on one side of the tab: where it leaves
    the anchors' container (``at_anchor``) and where it meets a card that goes
    on ``beyond`` the anchor by that much. Both share the gap between them
    when both are needed, and the card's never outgrows how far it goes on."""
    at_card = beyond > 0.5
    room = gap / 2 if at_anchor and at_card else gap
    r = max(min(radius, room), 0)
    return (r if at_anchor else 0), (min(r, beyond) if at_card else 0)


def tab_shape(side, anchor, card, container=None, radius=TAB_RADIUS):
    """The tab that joins an open card to its anchor, in the same coordinates.

    It spans the anchor across (its top to bottom beside a list, its left to
    right above a bar) and runs from the anchor's outer edge to the card's near
    edge, reaching over both outlines so neither crosses it. ``container`` is
    ``None`` for a lone anchor, or ``(before, after)`` for an anchor in a
    rounded container (a boxed list), one value per end: True where the
    container goes on beyond the anchor (the tab leaves it through a concave
    corner), False where the anchor ends it (the tab fills its rounded corner
    square), None where a scrolled view cuts it (a plain end). Where the card goes on
    beyond the anchor the tab meets it through a concave corner too; where the
    card's edge is level with the anchor's, the card's corner there is joined
    (drawn square). Returns ``(polygons, joined_start, joined_end)``; each
    polygon is a list of ``("M", x, y)``, ``("L", x, y)``,
    ``("A", corner_x, corner_y, x, y)`` (a quarter circle tangent to the
    corner) and ``("Z",)``."""
    # Work as if the card were to the right: u runs from the anchor to the
    # card, v across. ``to_xy`` turns the result back.
    if side == "right":
        e, p = anchor[2], card[0]
        t, b, ct, cb = anchor[1], anchor[3], card[1], card[3]

        def to_xy(u, v):
            return (u, v)
    elif side == "left":
        e, p = -anchor[0], -card[2]
        t, b, ct, cb = anchor[1], anchor[3], card[1], card[3]

        def to_xy(u, v):
            return (-u, v)
    elif side == "top":
        e, p = -anchor[1], -card[3]
        t, b, ct, cb = anchor[0], anchor[2], card[0], card[2]

        def to_xy(u, v):
            return (v, -u)
    else:
        e, p = anchor[3], card[1]
        t, b, ct, cb = anchor[0], anchor[2], card[0], card[2]

        def to_xy(u, v):
            return (v, u)

    gap = p - e
    ends = container or (None, None)
    inside_before, inside_after = ends[0] is True, ends[1] is True
    joined_start = abs(ct - t) < 0.5
    joined_end = abs(cb - b) < 0.5
    top_anchor, top_card = _fillets(gap, inside_before, t - ct, radius)
    bottom_anchor, bottom_card = _fillets(gap, inside_after, cb - b, radius)
    left, right = e - INTO_ANCHOR, p + OVERLAP
    ops = []

    def move(u, v):
        ops.append(("M", *to_xy(u, v)))

    def line(u, v):
        ops.append(("L", *to_xy(u, v)))

    def arc(cu, cv, u, v):
        ops.append(("A", *to_xy(cu, cv), *to_xy(u, v)))

    # Only within the anchor's span does the tab reach into the container:
    # beyond it lie the neighbouring anchors.
    move(left, t)
    if top_anchor:
        line(e, t)
        line(e, t - top_anchor)
        arc(e, t, e + top_anchor, t)
    if top_card:
        line(p - top_card, t)
        arc(p, t, p, t - top_card)
        line(right, t - top_card)
    else:
        line(right, t)
    if bottom_card:
        line(right, b + bottom_card)
        line(p, b + bottom_card)
        arc(p, b, p - bottom_card, b)
    else:
        line(right, b)
    if bottom_anchor:
        line(e + bottom_anchor, b)
        arc(e, b, e, b + bottom_anchor)
        line(e, b)
    line(left, b)
    ops.append(("Z",))
    polygons = [ops]
    # A first or last anchor sits in its container's rounded corner; the tab
    # fills that corner square so no sliver of the background shows in it.
    for end, end_v in ((ends[0], t), (ends[1], b - radius)):
        if end is False and b - t >= radius:
            u0, u1, v0, v1 = e - radius, e, end_v, end_v + radius
            polygons.append(
                [
                    ("M", *to_xy(u0, v0)),
                    ("L", *to_xy(u1, v0)),
                    ("L", *to_xy(u1, v1)),
                    ("L", *to_xy(u0, v1)),
                    ("Z",),
                ]
            )
    return polygons, joined_start, joined_end


def shape_bounds(polygons):
    xs = [op[i] for poly in polygons for op in poly for i in range(len(op))[1::2]]
    ys = [op[i] for poly in polygons for op in poly for i in range(len(op))[2::2]]
    return (min(xs), min(ys), max(xs), max(ys))


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
FADE_MS = 120


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


def _path(polygons):
    builder = Gsk.PathBuilder.new()
    for polygon in polygons:
        for op in polygon:
            if op[0] == "M":
                builder.move_to(op[1], op[2])
            elif op[0] == "L":
                builder.line_to(op[1], op[2])
            elif op[0] == "A":
                builder.arc_to(*op[1:])
            else:
                builder.close()
    return builder.to_path()


class CardLayer(Gtk.Fixed):
    """The window's layer for cards, an overlay above all its content. Only
    the cards and their tabs take the pointer; the rest of the layer lets it
    through to the window below."""

    def __init__(self):
        super().__init__()
        self.owners = weakref.WeakSet()  # the HoverCards placing cards here

    def do_contains(self, _x, _y):
        return False


class _Tab(Gtk.Widget):
    """The shape joining an open card to its anchor, in its CSS ``color``."""

    def __init__(self, css_classes):
        super().__init__(
            css_classes=["card-tab", *css_classes],
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
            focusable=False,
        )
        self._path = None

    def set_shape(self, polygons, origin):
        ox, oy = origin
        local = [
            [
                (op[0], *[v - (ox if i % 2 == 0 else oy) for i, v in enumerate(op[1:])])
                for op in polygon
            ]
            for polygon in polygons
        ]
        self._path = _path(local)
        self.queue_draw()

    def do_snapshot(self, snapshot):
        if self._path is not None:
            snapshot.append_fill(self._path, Gsk.FillRule.WINDING, self.get_color())

    def do_contains(self, x, y):
        return self._path is not None and self._path.in_fill(
            Graphene.Point().init(x, y), Gsk.FillRule.WINDING
        )


class HoverCards:
    """Cards in the window's :class:`CardLayer` that open as the pointer rests
    on their anchors, each joined to its anchor by a tab.

    ``side`` is where every card opens relative to the line of anchors. The
    consumer styles ``.hover-card`` (the card) and ``.card-tab`` (its ``color``
    is the tab's, the same as the anchor's ``.card-open`` background and the
    card's near edge). Hooks: ``can_open(name)`` may refuse to open;
    ``edge(name)`` gives the card's near edge in window coordinates (by
    default 12 px past the anchor); ``before_place(name, min_header)`` may
    make the card's header at least ``min_header`` tall, so the tab reads as
    one band with it. ``later``, ``cancel`` and ``now`` default to GLib
    timeouts and the monotonic clock.
    """

    def __init__(
        self,
        layer,
        side=Gtk.PositionType.RIGHT,
        later=None,
        cancel=None,
        now=time.monotonic,
    ):
        self.layer = layer
        layer.owners.add(self)
        self.side = side
        self.panels = {}  # name -> the card's frame in the layer
        self._tabs = {}  # name -> its tab in the layer
        self._anchors = {}  # name -> anchor
        self._scrollers = {}  # name -> the scroller that caps the card's height
        self._focus = {}  # name -> widget focused when a click opens the card
        self._inside = {}  # name -> the card's motion controller
        self._held = set()  # names with a button down inside
        self._sources = set()
        self._root = None  # the window whose frames move the open card
        self._watched = None  # (window, its controllers)
        self._after_paint = None  # (frame clock, handler) while a card is open
        self._placed = {}  # name -> (anchor box, window size) it was placed at
        self._fade = None
        self.can_open = None
        self.edge = None
        self.before_place = None
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

    def attach(self, name, anchor, content, title, css_classes=(), focus=None):
        """``content`` becomes ``name``'s card beside ``anchor``; ``title``
        names it to screen readers."""
        side = _SIDES[self.side]
        scroller = Gtk.ScrolledWindow(
            child=content,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
            propagate_natural_width=True,
        )
        panel = Gtk.Box(
            css_classes=["hover-card", f"side-{side}", *css_classes],
            overflow=Gtk.Overflow.HIDDEN,
            visible=False,
            accessible_role=Gtk.AccessibleRole.GROUP,
        )
        panel.update_property([Gtk.AccessibleProperty.LABEL], [title])
        panel.append(scroller)
        # The card lives outside the anchor's tree, so it takes its state here.
        anchor.bind_property(
            "sensitive", panel, "sensitive", GObject.BindingFlags.SYNC_CREATE
        )
        tab = _Tab(css_classes)
        tab.set_visible(False)
        # The tab above the card, so the card's outline and shadow stop at it.
        self.layer.put(panel, 0, 0)
        self.layer.put(tab, 0, 0)
        self.panels[name] = panel
        self._tabs[name] = tab
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
        panel.add_controller(inside)
        self._inside[name] = inside
        # Sees presses and releases without claiming them from the controls.
        buttons = Gtk.EventControllerLegacy(
            propagation_phase=Gtk.PropagationPhase.CAPTURE
        )
        buttons.connect("event", _weak(self._on_card_event), name)
        panel.add_controller(buttons)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", _weak(self._on_card_key), name)
        panel.add_controller(keys)
        # The window's controllers must be in place before the pointer enters:
        # GTK tells a controller added later nothing when the pointer leaves.
        self._watch(anchor.get_root())
        anchor.connect("notify::root", _weak(self._on_anchor_rooted))

    @property
    def pinned(self):
        """The card a click or key opened, or None."""
        return self._intent.pinned

    def is_open(self, name):
        return self.panels[name].get_visible()

    def open(self, name):
        """Open ``name``'s card as the pointer resting on its anchor would."""
        box = self._anchor_box(name)
        apex = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2) if box else None
        self._intent.open(name, apex)

    def open_for_keyboard(self, name):
        self._intent.pin(name)
        self._present(name)
        focus = self._focus[name]
        if callable(focus):
            focus = focus()  # the control to focus can change between openings
        # A container takes no focus itself: its first control does.
        if focus is None or not focus.grab_focus():
            self.panels[name].child_focus(Gtk.DirectionType.TAB_FORWARD)

    def close(self, name):
        panel = self.panels[name]
        if not panel.get_visible():
            return
        root = panel.get_root()
        focused = root.get_focus() if root is not None else None
        had_focus = focused is not None and focused.is_ancestor(panel)
        if self._fade is not None and self._fade[0] == name:
            self._fade[1].skip()
        panel.set_visible(False)
        self._tabs[name].set_visible(False)
        self._anchors[name].remove_css_class(OPEN_CLASS)
        self._held.discard(name)
        self._intent.closed(name)
        if had_focus:
            self._anchors[name].grab_focus()
        if not any(p.get_visible() for p in self.panels.values()):
            self._unfollow()

    def close_all(self):
        for name in self.panels:
            self.close(name)

    def close_hover(self):
        """Close the card the pointer opened, as when its anchors scroll."""
        self._intent.close_now()

    def reposition(self, name):
        """Place an open card again if its anchor or the window moved."""
        if self.is_open(name) and self._placed.get(name) != self._placement(name):
            self._place(name)

    def unparent_all(self):
        self._intent.cancel_all()
        self._cancel_after_paint()
        if self._fade is not None:
            self._fade[1].skip()
        for source in list(self._sources):
            GLib.source_remove(source)
        self._sources.clear()
        self._unfollow()
        if self._watched is not None:
            root, controllers = self._watched
            self._watched = None
            for controller in controllers:
                root.remove_controller(controller)
        for name in list(self.panels):
            self.layer.remove(self.panels[name])
            self.layer.remove(self._tabs[name])

    # Placement and geometry.

    def _show(self, name):
        self._present(name)

    def _present(self, name):
        # One card at a time in the window: the previous one goes at once,
        # never fading out beside the next.
        for owner in list(self.layer.owners):
            for other in owner.panels:
                if owner is not self or other != name:
                    owner.close(other)
        panel, tab = self.panels[name], self._tabs[name]
        was_open = panel.get_visible()
        self._anchors[name].add_css_class(OPEN_CLASS)
        if not was_open:
            panel.set_opacity(0)
            tab.set_opacity(0)
        # Shown before it is placed: a hidden widget measures nothing.
        panel.set_visible(True)
        tab.set_visible(True)
        self._place(name)
        self._follow(self._anchors[name].get_root())
        if not was_open:
            self._fade_in(name)

    def _fade_in(self, name):
        panel, tab = self.panels[name], self._tabs[name]
        if self._fade is not None:
            self._fade[1].skip()

        def step(value):
            panel.set_opacity(value)
            tab.set_opacity(value)

        # Adw skips to the end when the desktop asks for no animations.
        animation = Adw.TimedAnimation(
            widget=panel,
            value_from=0.0,
            value_to=1.0,
            duration=FADE_MS,
            target=Adw.CallbackAnimationTarget.new(step),
        )
        self._fade = (name, animation)
        animation.connect("done", _weak(self._on_faded))
        animation.play()

    def _on_faded(self, _animation):
        self._fade = None

    def _container(self, name):
        """For each end of the anchor, as :func:`tab_shape` takes it: whether
        its boxed list goes on (True), ends with it (False) or is cut there by
        the scrolled view (None); None for an anchor in no list."""
        anchor = self._anchors[name]
        if not isinstance(anchor.get_parent(), Gtk.ListBox):
            return None

        def shown(row, step):
            row = step(row)
            while row is not None and not row.get_visible():
                row = step(row)
            return row is not None

        box, view = self._anchor_box(name), self._view_box(name)
        cut_before = view is not None and box[1] - view[1] < TAB_RADIUS
        cut_after = view is not None and view[3] - box[3] < TAB_RADIUS
        return (
            None if cut_before else shown(anchor, Gtk.Widget.get_prev_sibling),
            None if cut_after else shown(anchor, Gtk.Widget.get_next_sibling),
        )

    def _view_box(self, name):
        """The scrolled view the anchor sits in, or None."""
        view = self._anchors[name].get_ancestor(Gtk.ScrolledWindow)
        return self._box_of(view) if view is not None else None

    def _edge(self, name, box):
        if self.edge is not None:
            return self.edge(name)
        side = _SIDES[self.side]
        return {
            "right": box[2] + 12,
            "left": box[0] - 12,
            "top": box[1] - 12,
            "bottom": box[3] + 12,
        }[side]

    def _place(self, name):
        anchor = self._anchors[name]
        root = anchor.get_root()
        box = self._anchor_box(name)
        if root is None or box is None:
            return
        side = _SIDES[self.side]
        edge = self._edge(name, box)
        container = self._container(name)
        across = box[3] - box[1] if side in ("right", "left") else box[2] - box[0]
        outer = {"right": box[2], "left": box[0], "top": box[1], "bottom": box[3]}
        gap = abs(edge - outer[side])
        if self.before_place is not None:
            # Taller than the anchor by the concave corner below it, so that
            # corner meets the header and not the card's body.
            after = bool(container and container[1])
            self.before_place(name, across + _fillets(gap, after, TAB_RADIUS)[1])
        panel, scroller = self.panels[name], self._scrollers[name]
        scroller.set_max_content_height(-1)
        panel.set_size_request(-1, -1)
        _min, width, _mb, _nb = panel.measure(Gtk.Orientation.HORIZONTAL, -1)
        _min, height, _mb, _nb = panel.measure(Gtk.Orientation.VERTICAL, width)
        window = (root.get_width(), root.get_height())
        card = card_box(side, box, edge, (width, height), window)
        if card[3] - card[1] < height:
            scroller.set_max_content_height(max(round(card[3] - card[1]), 1))
        # The layer gives a child its minimum size; a card wants its natural one.
        panel.set_size_request(round(card[2] - card[0]), round(card[3] - card[1]))
        polygons, joined_start, joined_end = tab_shape(side, box, card, container)
        for css, on in (("joined-start", joined_start), ("joined-end", joined_end)):
            (panel.add_css_class if on else panel.remove_css_class)(css)
        found, origin = root.compute_point(self.layer, Graphene.Point().init(0, 0))
        ox, oy = (origin.x, origin.y) if found else (0, 0)
        self.layer.move(panel, card[0] + ox, card[1] + oy)
        bounds = shape_bounds(polygons)
        tab = self._tabs[name]
        tab.set_size_request(
            math.ceil(bounds[2] - bounds[0]), math.ceil(bounds[3] - bounds[1])
        )
        tab.set_shape(polygons, (bounds[0], bounds[1]))
        self.layer.move(tab, bounds[0] + ox, bounds[1] + oy)
        self._placed[name] = self._placement(name)

    def _placement(self, name):
        root = self._anchors[name].get_root()
        size = (root.get_width(), root.get_height()) if root is not None else None
        return self._anchor_box(name), size

    def _box_of(self, widget):
        root = widget.get_root()
        if root is None:
            return None
        found, bounds = widget.compute_bounds(root)
        if not found:
            return None
        x, y = bounds.get_x(), bounds.get_y()
        return (x, y, x + bounds.get_width(), y + bounds.get_height())

    def _anchor_box(self, name):
        """The anchor as shown: the part its scrolled view does not hide."""
        box = self._box_of(self._anchors[name])
        view = self._view_box(name)
        if box is None or view is None:
            return box
        x0, y0 = max(box[0], view[0]), max(box[1], view[1])
        x1, y1 = min(box[2], view[2]), min(box[3], view[3])
        return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None

    def _card_box(self, name):
        """The card as drawn, in the window's coordinates, or None."""
        panel = self.panels[name]
        if not panel.get_visible() or panel.get_root() is None:
            return None
        return self._box_of(panel)

    def _boxes(self, name):
        """The anchor reaching over its tab, and the card."""
        anchor, card = self._anchor_box(name), self._card_box(name)
        if anchor is None or card is None:
            return anchor, card
        side = _SIDES[self.side]
        x0, y0, x1, y1 = anchor
        if side == "right":
            x1 = card[0]
        elif side == "left":
            x0 = card[2]
        elif side == "top":
            y0 = card[3]
        else:
            y1 = card[1]
        return (x0, y0, x1, y1), card

    def _on_tab(self, name, x, y):
        tab = self._tabs[name]
        root = tab.get_root()
        if root is None or not tab.get_visible():
            return False
        found, point = root.compute_point(tab, Graphene.Point().init(x, y))
        return found and tab.contains(point.x, point.y)

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
        # The pointer never replaces a card a click opened, whoever owns it.
        return (
            anchor.get_mapped()
            and anchor.is_sensitive()
            and all(owner.pinned is None for owner in self.layer.owners)
            and (self.can_open is None or self.can_open(name))
        )

    def _is_held(self, name):
        return name in self._held or _child_popup_visible(self.panels[name])

    # Events.

    def _on_anchor_pointer(self, controller, x, y, name):
        if self._intent.owner is not None:
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
        if self._intent.owner is not None:
            self._intent.left_window()

    def _on_window_motion(self, _controller, x, y):
        # Only the way to an open card needs the whole window.
        if self._intent.owner is not None:
            self._intent.pointer(self._anchor_at((x, y)), (x, y))

    def _on_window_press(self, _gesture, _n_press, x, y):
        # A press outside a pinned card closes it and still does its own work.
        name = self._intent.pinned
        if name is None:
            return
        card, anchor = self._card_box(name), self._anchor_box(name)
        if card is not None and contains(card, (x, y)):
            return
        if anchor is not None and contains(anchor, (x, y)):
            return
        if not self._on_tab(name, x, y):
            self.close(name)

    def _on_anchor_rooted(self, anchor, _pspec):
        self._watch(anchor.get_root())

    def _on_window_key(self, _controller, keyval, _keycode, _state):
        if keyval != Gdk.KEY_Escape:
            return False
        if self._intent.pinned is not None:
            self.close(self._intent.pinned)
            return True
        return bool(self._intent.escape())

    def _on_card_key(self, _controller, keyval, _keycode, state, name):
        # Tab cycles within a pinned card, as within a dialog.
        if keyval not in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_KP_Tab):
            return False
        backward = keyval == Gdk.KEY_ISO_Left_Tab or (
            state & Gdk.ModifierType.SHIFT_MASK
        )
        direction = (
            Gtk.DirectionType.TAB_BACKWARD
            if backward
            else Gtk.DirectionType.TAB_FORWARD
        )
        panel = self.panels[name]
        if not panel.child_focus(direction):
            root = panel.get_root()
            if root is not None:
                root.set_focus(None)
            panel.child_focus(direction)
        return True

    def _on_card_event(self, _controller, event, name):
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

    def _watch(self, root):
        """Follow the pointer over the whole window, where the way to a card
        lies; take Escape; see presses outside a pinned card."""
        if root is None or (self._watched is not None and self._watched[0] is root):
            return
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", _weak(self._on_window_motion))
        motion.connect("leave", _weak(self._on_window_leave))
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", _weak(self._on_window_key))
        press = Gtk.GestureClick(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        press.connect("pressed", _weak(self._on_window_press))
        for controller in (motion, keys, press):
            root.add_controller(controller)
        self._watched = (root, (motion, keys, press))

    def _follow(self, root):
        """While a card is open it follows its anchor whenever a frame moved
        it: a resize, a reflow, a scroll. Frames are drawn only when something
        changes."""
        if root is None or self._after_paint is not None:
            return
        self._watch(root)
        self._root = root
        clock = root.get_frame_clock()
        if clock is not None:
            handler = clock.connect("after-paint", _weak(self._on_after_paint))
            self._after_paint = (clock, handler)

    def _unfollow(self):
        self._cancel_after_paint()
        self._root = None

    def _on_after_paint(self, _clock):
        for name in self.panels:
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
