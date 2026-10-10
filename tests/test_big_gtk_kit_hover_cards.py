"""Hover cards decide by geometry and a clock, so these run without a display."""

import pytest
from big_gtk_kit import hover_cards as hc

ROW = 48
WIDTH = 300
CARD_X = 308
CARD_HEIGHT = 450


class Clock:
    """Timers that fire only when the test advances time."""

    def __init__(self):
        self.t = 0.0
        self.timers = {}
        self._next = 0

    def now(self):
        return self.t

    def later(self, milliseconds, callback):
        self._next += 1
        self.timers[self._next] = (self.t + milliseconds / 1000, callback)
        return self._next

    def cancel(self, source):
        self.timers.pop(source, None)

    def advance(self, milliseconds):
        end = self.t + milliseconds / 1000
        while True:
            due = sorted(
                (when, source)
                for source, (when, _cb) in self.timers.items()
                if when <= end + 1e-9
            )
            if not due:
                break
            self.t, source = due[0]
            self.timers.pop(source)[1]()
        self.t = end


class List:
    """A list of rows with cards beside it, top-aligned to their row."""

    def __init__(self, rows=6, held=False):
        self.clock = Clock()
        self.open = set()
        self.held = held
        self.intent = hc.HoverIntent(
            "right",
            self.boxes,
            self.open.add,
            self.open.discard,
            self.clock.later,
            self.clock.cancel,
            self.clock.now,
            is_held=lambda _name: self.held,
        )
        self.rows = rows

    def boxes(self, row):
        anchor = (0, row * ROW, WIDTH, (row + 1) * ROW)
        card = None
        if row in self.open:
            top = row * ROW
            card = (CARD_X, top, CARD_X + 300, top + CARD_HEIGHT)
        return anchor, card

    def row_at(self, point):
        x, y = point
        if 0 <= x <= WIDTH and 0 <= y < self.rows * ROW:
            return int(y // ROW)
        return None

    def move(self, *points, step_ms=16):
        """The pointer through ``points``, one event per frame."""
        for point in points:
            self.intent.pointer(self.row_at(point), point)
            self.clock.advance(step_ms)

    def rest(self, point, milliseconds):
        self.move(point)
        self.clock.advance(milliseconds)

    def open_row(self, row):
        self.rest((150, row * ROW + ROW / 2), hc.FIRST_OPEN_MS + 10)
        assert self.open == {row}


def path(start, end, steps):
    (x0, y0), (x1, y1) = start, end
    return [
        (x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
        for i in range(1, steps + 1)
    ]


def test_a_card_opens_only_once_the_pointer_rests():
    cards = List()
    cards.move((150, 24))
    cards.clock.advance(hc.FIRST_OPEN_MS - 30)
    cards.move((150, 400))  # off the list before the wait ends
    cards.clock.advance(500)
    assert cards.open == set()
    cards.rest((150, 24), hc.FIRST_OPEN_MS + 10)
    assert cards.open == {0}


def test_tremor_still_counts_as_resting():
    cards = List()
    # +-3 px of tremor, one event per frame for 16 frames, past 225 ms.
    for offset in (0, 3, -3, 2, -2, 3, 0, -3, 3, 0, 2, -2, 3, -3, 0, 1):
        cards.move((150 + offset, 24 + (offset > 0) - (offset < 0)))
    assert cards.open == {0}
    other = List()
    other.move((150, 24))
    for _ in range(30):
        other.move((150, 24), step_ms=0)
        other.clock.advance(100)
        other.move((156, 24), step_ms=0)  # a real move restarts the wait
        other.clock.advance(100)
        other.move((150, 24), step_ms=0)
    assert other.open == set()


def test_straight_down_to_the_next_row_switches_fast():
    cards = List()
    cards.open_row(0)
    cards.move(*path((150, 24), (150, 72), 4))
    cards.clock.advance(hc.SWITCH_MS + 10)
    assert cards.open == {1}


def test_the_diagonal_into_a_card_keeps_it_across_the_next_row():
    cards = List()
    cards.open_row(0)
    # 45 degrees down and right, over row 1, then a pause short of the card,
    # as a hand undershoots before correcting.
    cards.move(*path((150, 24), (290, 70), 8))
    assert cards.row_at((290, 70)) == 1
    cards.clock.advance(hc.TRIANGLE_REST_MS - 50)
    assert cards.open == {0}
    cards.move(*path((290, 70), (330, 90), 3))
    cards.intent.entered_card(0)
    cards.clock.advance(1000)
    assert cards.open == {0}


def test_resting_on_a_row_along_the_way_hands_it_the_card():
    cards = List()
    cards.open_row(0)
    cards.move(*path((150, 24), (290, 70), 8))
    cards.clock.advance(hc.TRIANGLE_REST_MS + 10)
    assert cards.open == {1}


def test_leaving_the_triangle_over_a_row_switches_at_once():
    cards = List()
    cards.open_row(0)
    cards.move(*path((150, 24), (290, 70), 8))
    cards.move(*path((290, 70), (100, 80), 4))  # back left, out of the triangle
    cards.clock.advance(hc.SWITCH_MS + 10)
    assert cards.open == {1}


def test_a_fast_flick_across_the_list_opens_nothing():
    cards = List()
    cards.move(*path((150, 5), (150, 280), 6), step_ms=8)
    cards.move((600, 280))
    cards.clock.advance(1000)
    assert cards.open == set()


def test_leaving_closes_after_a_while_and_the_next_row_opens_warm():
    cards = List()
    cards.open_row(0)
    cards.move((900, 500))  # far from the row, its card and the way between
    cards.clock.advance(hc.CLOSE_MS - 50)
    assert cards.open == {0}
    cards.clock.advance(60)
    assert cards.open == set()
    cards.rest((150, 3 * ROW + 24), hc.SWITCH_MS + 10)
    assert cards.open == {3}
    cards.move((900, 500))
    cards.clock.advance(hc.CLOSE_MS + 10 + hc.WARM_S * 1000 + 10)
    cards.rest((150, 2 * ROW + 24), hc.SWITCH_MS + 10)
    assert cards.open == set()  # cold again: the first-open wait applies
    cards.clock.advance(hc.FIRST_OPEN_MS)
    assert cards.open == {2}


def test_the_pointer_leaving_the_window_closes_unless_it_enters_the_card():
    cards = List()
    cards.open_row(0)
    cards.intent.left_window()
    cards.intent.entered_card(0)  # the card is a surface of its own
    cards.clock.advance(1000)
    assert cards.open == {0}
    cards.intent.left_card(0)
    cards.clock.advance(hc.CLOSE_MS + 10)
    assert cards.open == set()


def test_the_pointer_never_replaces_a_pinned_card():
    cards = List()
    cards.open_row(0)
    cards.intent.pin(2)
    assert cards.open == set()  # the pointer's card closed for the pinned one
    cards.open.add(2)
    cards.rest((150, 4 * ROW + 24), 2000)
    assert cards.open == {2}
    cards.open.discard(2)
    cards.intent.closed(2)
    cards.rest((150, 4 * ROW + 24), hc.FIRST_OPEN_MS + 10)
    assert cards.open == {4}


def test_escape_closes_the_pointers_card_until_the_pointer_leaves_its_row():
    cards = List()
    cards.open_row(1)
    assert cards.intent.escape()
    assert cards.open == set()
    cards.rest((160, 72), 2000)
    assert cards.open == set()
    cards.move((150, 4 * ROW + 24))  # another row
    cards.rest((150, 72), hc.SWITCH_MS + hc.FIRST_OPEN_MS)
    assert cards.open == {1}
    assert not List().intent.escape()  # nothing to close: Escape is not taken


def test_a_card_in_use_stays_open_when_its_close_comes():
    cards = List(held=True)
    cards.open_row(0)
    cards.move((900, 500))
    cards.clock.advance(2000)
    assert cards.open == {0}
    cards.held = False
    cards.move((901, 500))
    cards.clock.advance(hc.CLOSE_MS + 10)
    assert cards.open == set()


def test_scrolling_closes_the_pointers_card_at_once():
    cards = List()
    cards.open_row(0)
    cards.intent.close_now()
    assert cards.open == set()


@pytest.mark.parametrize(
    ("point", "inside"),
    [((5, 5), True), ((0, 0), True), ((10, 0), True), ((6, 6), False)],
)
def test_triangle_contains_its_edges(point, inside):
    assert hc.in_triangle(point, (0, 0), (10, 0), (0, 10)) is inside


def test_a_degenerate_triangle_holds_nothing():
    assert not hc.in_triangle((1, 1), (0, 0), (2, 2), (4, 4))
    assert not hc.in_triangle((0, 0), (0, 0), (0, 0), (0, 0))


def test_the_region_joins_anchor_card_and_the_way_between():
    anchor, card = (0, 0, 300, 48), (308, 0, 608, 450)
    apex = (150, 24)
    assert hc.in_region((150, 24), anchor, card, apex, "right")
    assert hc.in_region((400, 300), anchor, card, apex, "right")
    assert hc.in_region((290, 70), anchor, card, apex, "right")
    assert not hc.in_region((150, 72), anchor, card, apex, "right")
    assert not hc.in_region((150, 72), anchor, None, apex, "right")
    assert not hc.in_region((290, 70), anchor, card, None, "right")


def test_a_card_beside_the_list_lines_up_with_its_row_inside_the_window():
    window = (1280, 800)
    # At the edge it is given, level with its row.
    assert hc.card_box("right", (0, 100, 300, 148), 312, (300, 200), window) == (
        312,
        100,
        612,
        300,
    )
    # Too tall for the room below, but the room holds three rows: it stays
    # level and is cut to the room, which then scrolls.
    assert hc.card_box("right", (0, 500, 300, 548), 312, (300, 400), window) == (
        312,
        500,
        612,
        792,
    )
    # Less room than that: it rises to stay 8 px inside.
    _x0, top, _x1, bottom = hc.card_box(
        "right", (0, 700, 300, 748), 312, (300, 300), window
    )
    assert (top, bottom) == (492, 792)
    # Taller than the window: cut to the window less the margins.
    _x0, top, _x1, bottom = hc.card_box(
        "right", (0, 0, 300, 48), 312, (300, 2000), window
    )
    assert (top, bottom) == (8, 792)


def test_a_card_above_a_bar_is_centered_and_meets_a_close_edge_flush():
    window = (1280, 800)
    assert hc.card_box("top", (600, 740, 640, 780), 728, (120, 220), window) == (
        560,
        508,
        680,
        728,
    )
    # Pushed inside the window, it would stop 4 px short of the control's
    # edge: it lines up with it instead.
    x0, _y0, _x1, _y1 = hc.card_box("top", (12, 740, 52, 780), 728, (120, 220), window)
    assert x0 == 12
    # Room above is the edge less the margin.
    _x0, y0, _x1, y1 = hc.card_box("top", (600, 740, 640, 780), 100, (120, 220), window)
    assert (y0, y1) == (8, 100)


def _filled(polygons):
    """Points on a 1 px grid inside the tab, as GSK would fill it."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Graphene, Gsk

    path = hc._path(polygons)
    x0, y0, x1, y1 = hc.shape_bounds(polygons)
    return {
        (x, y)
        for x in range(int(x0), int(x1) + 1)
        for y in range(int(y0), int(y1) + 1)
        if path.in_fill(Graphene.Point().init(x + 0.5, y + 0.5), Gsk.FillRule.WINDING)
    }


def test_the_tab_spans_the_row_from_the_list_into_the_card():
    row, card = (12, 100, 300, 160), (316, 100, 640, 400)
    polygons, joined_start, joined_end = hc.tab_shape("right", row, card, (True, True))
    assert joined_start and not joined_end
    filled = _filled(polygons)
    # Every pixel row of the band, from inside the list into the card.
    assert all((x, y) in filled for x in range(297, 317) for y in range(100, 160))
    # The top runs straight into the card's square corner; the list side
    # leaves through a concave 12 px corner.
    assert (300, 95) in filled and (300, 92) in filled
    assert (300, 87) not in filled and (310, 99) not in filled
    # Below, list and card share the 16 px gap: two 8 px corners, the gap
    # between them ending in an arch.
    assert (300, 163) in filled and (300, 169) not in filled
    assert (315, 163) in filled and (315, 169) not in filled
    assert (308, 163) not in filled
    # Inside the list it stays within the row: the next rows are their own.
    assert (297, 98) not in filled and (297, 162) not in filled


def test_a_first_row_fills_the_lists_corner_and_a_cut_list_ends_plain():
    row, card = (12, 0, 300, 60), (316, 0, 640, 300)
    polygons, *_ = hc.tab_shape("right", row, card, (False, True))
    filled = _filled(polygons)
    # The list's rounded corner is filled square; nothing rises above it.
    assert (299, 0) in filled and (290, 1) in filled
    assert all((x, -1) not in filled for x in range(280, 320))
    polygons, *_ = hc.tab_shape("right", row, card, (True, None))
    filled = _filled(polygons)
    # A scrolled view cuts the list below: no corner reaches past it.
    assert all((x, 61) not in filled for x in range(296, 304))


def test_a_risen_card_meets_the_tab_through_a_corner_and_stays_rounded():
    row, card = (12, 500, 300, 560), (316, 300, 640, 592)
    polygons, joined_start, joined_end = hc.tab_shape("right", row, card, (True, True))
    assert not joined_start and not joined_end
    filled = _filled(polygons)
    # Both ends of the gap meet curves: 8 px each above.
    assert (315, 496) in filled and (315, 491) not in filled
    assert (300, 496) in filled and (300, 491) not in filled


def test_a_bar_cards_corner_curve_never_outgrows_the_card():
    control, card = (300, 700, 340, 740), (297, 400, 520, 688)
    polygons, joined_start, _joined_end = hc.tab_shape("top", control, card, None)
    assert not joined_start
    filled = _filled(polygons)
    # The card goes on 3 px past the control's left edge: a 3 px corner, not
    # one that reaches beyond the card.
    assert (296, 688) not in filled and (299, 688) in filled
    # On the right the card goes on far: a 12 px corner.
    assert (341, 689) in filled and (345, 688) in filled
    assert (351, 688) not in filled and (351, 699) not in filled
