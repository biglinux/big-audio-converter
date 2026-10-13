"""Hover cards of the sidebar's rows and the playback bar, in the real window."""

import os
import shutil
import subprocess
import time

import pytest
from conftest import pump

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="A graphical display is required",
)


def test_keyboard_opens_row_card_with_amount(window):
    row = window.compressor_row
    window.present()
    pump(lambda: row.get_mapped())
    row.activate()
    cards = window.effect_cards.hover_cards
    assert cards.is_open(row) and cards.pinned is row
    pump(lambda: row.steps.buttons[0].has_focus())
    row.steps.set_selected(3)
    assert row.value_label.get_text() == "Strong"
    [count] = [c for rows, c in window.effect_cards.groups if row in rows]
    assert count.get_text() == "1 active"
    # Off is the default of a correction: there is nothing to reset to.
    assert row.reset_button is None
    cards.close(row)
    assert cards.pinned is None


def _wait(seconds):

    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        context.iteration(False)


def _hand_driven(cards):
    """Silence the real pointer's motion controllers, so only the pointer the
    test moves reaches the cards. Xvfb's pointer stays at the screen's center,
    and a card opening under it would otherwise meet a second pointer."""
    from gi.repository import Gtk

    widgets = [cards._watched[0], *cards._anchors.values(), *cards.panels.values()]
    for widget in widgets:
        for controller in widget.observe_controllers():
            if isinstance(controller, Gtk.EventControllerMotion):
                controller.set_propagation_phase(Gtk.PropagationPhase.NONE)


def _open_by_rest(cards, row):
    """Put the pointer in the middle of ``row``; wait until its card is laid
    out beside it."""
    _hand_driven(cards)
    box = cards._anchor_box(row)
    middle = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    cards._intent.pointer(row, middle)
    pump(lambda: cards.is_open(row) and cards.panels[row].get_width() > 0)
    return middle


def _move(cards, *points, step=0.016):
    """The pointer through ``points`` in window coordinates, a frame apart."""
    for point in points:
        cards._on_window_motion(None, *point)
        _wait(step)


def test_card_opens_level_with_its_row(window):
    cards = window.effect_cards.hover_cards
    row = window.compressor_row
    window.present()
    pump(lambda: row.get_mapped())
    _open_by_rest(cards, row)
    anchor, card = cards._anchor_box(row), cards._card_box(row)
    scrolled = window.effect_cards.scrolled
    from gi.repository import Graphene

    _found, edge = scrolled.compute_point(
        window, Graphene.Point().init(scrolled.get_width(), 0)
    )
    # Past the sidebar's edge, over the queue; its top level with the ring
    # the row draws inside its edges.
    from big_gtk_kit import hover_cards

    assert card[0] >= edge.x
    assert abs(card[1] - (anchor[1] + hover_cards.RING_INSET)) <= 1
    assert row.has_css_class("card-open")
    # Its scrollbar stays out of the tab's way while the card is open.
    assert scrolled.has_css_class("card-open")
    cards.close(row)
    pump(lambda: not row.has_css_class("card-open"))
    assert not scrolled.has_css_class("card-open")


def test_top_row_card_stays_inside_window(window):
    cards = window.effect_cards.hover_cards
    row = window.format_row  # the tallest card, on the first row
    window.set_default_size(1100, 560)
    window.present()
    pump(lambda: row.get_mapped() and window.get_height() <= 600)
    _open_by_rest(cards, row)
    _x0, top, _x1, bottom = cards._card_box(row)
    assert top >= 0 and bottom <= window.get_height()
    cards.close(row)


def test_straight_down_hands_over_the_card(window):
    cards = window.effect_cards.hover_cards
    first, second = window.format_row, window.bitrate_row
    window.present()
    pump(lambda: second.get_mapped())
    x, y = _open_by_rest(cards, first)
    below = cards._anchor_box(second)
    _move(cards, *[(x, y + (below[1] + 12 - y) * i / 4) for i in range(1, 5)])
    pump(lambda: cards.is_open(second))
    assert not cards.is_open(first)
    cards.close(second)


def test_diagonal_into_card_keeps_it_open(window):
    cards = window.effect_cards.hover_cards
    first, second = window.format_row, window.bitrate_row
    window.present()
    pump(lambda: second.get_mapped())
    x, y = _open_by_rest(cards, first)
    card = cards._card_box(first)
    below = cards._anchor_box(second)
    # Down and right over the next row, short of the card: a hand undershoots
    # and pauses before it corrects.
    target = (below[2] - 6, below[1] + 10)
    _move(
        cards,
        *[
            (x + (target[0] - x) * i / 6, y + (target[1] - y) * i / 6)
            for i in range(1, 7)
        ],
    )
    assert cards._anchor_at(target) is second
    _wait(0.3)
    assert cards.is_open(first) and not cards.is_open(second)
    _move(cards, (card[0] + 30, target[1] + 6))
    cards._intent.entered_card(first)
    _wait(0.6)
    assert cards.is_open(first) and not cards.is_open(second)
    # Resting on that row instead hands it the card.
    cards._intent.left_card(first)
    _move(cards, target)
    pump(lambda: cards.is_open(second))
    cards.close(second)


def test_escape_closes_a_hovered_card(window):
    from gi.repository import Gdk

    cards = window.effect_cards.hover_cards
    row = window.compressor_row
    window.present()
    pump(lambda: row.get_mapped())
    _open_by_rest(cards, row)
    assert cards._on_window_key(None, Gdk.KEY_Escape, 0, 0)
    pump(lambda: not cards.is_open(row))
    # It stays closed while the pointer stays on the row.
    box = cards._anchor_box(row)
    cards._intent.pointer(row, (box[0] + 20, box[1] + 10))
    _wait(0.5)
    assert not cards.is_open(row)


def test_pointer_never_replaces_a_clicked_card(window):
    cards = window.effect_cards.hover_cards
    pinned, other = window.bitrate_row, window.channels_row
    window.present()
    pump(lambda: other.get_mapped())
    pinned.activate()
    pump(lambda: cards.is_open(pinned))
    box = cards._anchor_box(other)
    cards._intent.pointer(other, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
    _wait(0.6)
    assert cards.is_open(pinned) and not cards.is_open(other)
    cards.close(pinned)


def test_click_focuses_card_without_a_control(window, audio):
    cards = window.effect_cards.hover_cards
    row = window.pause_row  # its card's body is a container
    window.file_queue.add_file(str(audio))
    window.present()
    pump(lambda: row.get_mapped() and row.is_sensitive())
    row.activate()
    panel = cards.panels[row]
    pump(lambda: cards.is_open(row))
    assert window.get_focus() is not None and window.get_focus().is_ancestor(panel)
    cards.close(row)
    assert window.get_focus() is row


def _focus_x11_window(window):
    import gi

    gi.require_version("GdkX11", "4.0")
    from gi.repository import GdkX11

    surface = window.get_native().get_surface()
    assert isinstance(surface, GdkX11.X11Surface)
    xid = str(surface.get_xid())
    subprocess.run(["xdotool", "windowfocus", "--sync", xid], check=True, timeout=5)
    return xid


@pytest.mark.skipif(
    not shutil.which("xdotool") or os.environ.get("GDK_BACKEND") != "x11",
    reason="xdotool supplies native keys to X11 windows",
)
@pytest.mark.filterwarnings("ignore:GdkX11.X11Surface.get_xid:DeprecationWarning")
def test_pinned_card_traps_tab_until_escape(window):
    cards = window.effect_cards.hover_cards
    row = window.compressor_row
    window.present()
    pump(lambda: row.get_mapped())
    _focus_x11_window(window)
    row.grab_focus()
    row.activate()
    panel = cards.panels[row]
    pump(
        lambda: window.get_focus() is not None and window.get_focus().is_ancestor(panel)
    )
    for _ in range(8):
        subprocess.run(["xdotool", "key", "Tab"], check=True, timeout=5)
        pump(lambda: True)
        focus = window.get_focus()
        assert focus is not None and focus.is_ancestor(panel)
    # The steps are one radio group: Tab stops on it once, arrows pick.
    subprocess.run(["xdotool", "key", "Right"], check=True, timeout=5)
    pump(lambda: row.steps.get_selected() == 1)
    assert window.get_focus() is row.steps.buttons[1]
    subprocess.run(["xdotool", "key", "Escape"], check=True, timeout=5)
    pump(lambda: not cards.is_open(row))
    assert window.get_focus() is row


@pytest.mark.skipif(
    not shutil.which("xdotool") or os.environ.get("GDK_BACKEND") != "x11",
    reason="xdotool supplies native pointer clicks to X11 windows",
)
@pytest.mark.filterwarnings("ignore:GdkX11.X11Surface.get_xid:DeprecationWarning")
def test_outside_click_closes_card_and_lands(window):
    from gi.repository import Graphene

    cards = window.effect_cards.hover_cards
    row, target = window.compressor_row, window.normalize_row
    window.present()
    pump(lambda: row.get_mapped() and target.get_mapped())
    xid = _focus_x11_window(window)
    row.activate()
    pump(lambda: cards.is_open(row) and cards._card_box(row) is not None)
    _found, point = target.compute_point(
        window, Graphene.Point().init(target.get_width() / 2, target.get_height() / 2)
    )
    dx, dy = window.get_surface_transform()
    x, y = str(round(point.x + dx)), str(round(point.y + dy))
    subprocess.run(
        ["xdotool", "mousemove", "--window", xid, x, y, "click", "1"],
        check=True,
        timeout=5,
    )
    pump(lambda: target.get_active())
    assert not cards.is_open(row)
    target.set_active(False)


def test_open_card_follows_row_on_resize(window):
    cards = window.effect_cards.hover_cards
    row = window.compressor_row
    # Both sizes keep the sidebar beside the queue; it narrows with the window.
    window.set_default_size(1340, 760)
    window.present()
    pump(lambda: row.get_mapped() and window.get_width() > 1300)
    row.activate()
    pump(lambda: cards._card_box(row) is not None)
    _wait(0.3)
    before_card, before_row = cards._card_box(row), cards._anchor_box(row)
    window.set_default_size(1120, 560)
    pump(lambda: window.get_width() < 1200)
    pump(lambda: cards._anchor_box(row) != before_row)
    _wait(0.3)
    anchor, card = cards._anchor_box(row), cards._card_box(row)
    # The row narrowed with the sidebar, and the card kept its place beside it.
    assert anchor[2] < before_row[2]
    assert abs((card[0] - anchor[2]) - (before_card[0] - before_row[2])) <= 1
    assert card[1] >= 0 and card[3] <= window.get_height()
    cards.close(row)


def test_leaving_the_window_closes_the_card(window):
    cards = window.effect_cards.hover_cards
    row = window.compressor_row
    window.present()
    pump(lambda: row.get_mapped())
    _open_by_rest(cards, row)
    # The window's controller was there before the pointer came in, so GTK
    # tells it when the pointer leaves.
    root, (motion, _keys, _press) = cards._watched
    assert root is window and motion.get_widget() is window
    cards._on_window_leave(motion)
    pump(lambda: not cards.is_open(row))


def test_closed_card_leaves_nothing_running(window):
    cards = window.effect_cards.hover_cards
    row = window.compressor_row
    window.present()
    pump(lambda: row.get_mapped())
    _open_by_rest(cards, row)
    assert cards._after_paint is not None and cards._root is window
    _move(cards, (window.get_width() - 5, window.get_height() - 5))
    pump(lambda: not cards.is_open(row))
    _wait(0.4)  # past the warm window
    # No timer or frame-clock handler is left behind; the window's pointer
    # controller returns at once while no card is open.
    assert cards._sources == set()
    assert cards._after_paint is None and cards._root is None


def test_scrolling_the_sidebar_closes_the_card(window):
    cards = window.effect_cards.hover_cards
    row = window.compressor_row
    window.set_default_size(1000, 480)
    window.present()
    pump(lambda: row.get_mapped())
    _open_by_rest(cards, row)
    adjustment = window.effect_cards.scrolled.get_vadjustment()
    pump(lambda: adjustment.get_upper() > adjustment.get_page_size())
    adjustment.set_value(adjustment.get_value() + 40)
    pump(lambda: not cards.is_open(row))


def test_playback_bar_holds_volume_speed_voice(window):
    preview = window.original_preview
    window.volume_spin.set_value(150)
    assert window.volume_value.get_label() == "150"
    assert window.volume_button.get_icon_name() == "audio-volume-high-symbolic"
    # A bar adjustment counts like a row: the original can be heard.
    assert preview.get_visible()
    assert window.adjustments_label.get_label() == "1 adjustment"
    window.speed_spin.set_value(1.25)
    assert window.speed_value.get_label() == "1.25x"
    assert window.adjustments_label.get_label() == "2 adjustments"
    window.volume_spin.set_value(100)
    window.speed_spin.set_value(1.0)
    assert not preview.get_visible()
    # The voice and its model are in the bar's card, not in the sidebar.
    assert window.voice_value.get_label() == "0"
    assert window.noise_model.is_ancestor(window.bar_cards.panels["voice"])


def test_bar_controls_keep_their_width(window, audio):

    from gi.repository import GLib

    window.file_queue.add_file(str(audio))
    pump(lambda: not window.file_queue.pending)
    window.present()
    voice = window.voice_button.get_parent()
    pump(lambda: voice.get_mapped() and voice.get_width())
    closed = voice.get_width()
    # An open card's expanding choices must not widen the control it points
    # at, which would aim the card's tab past the button.
    window.bar_cards.open("voice")
    pump(lambda: window.bar_cards.is_open("voice"))
    # The next layouts would widen it; give them a few frames.
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        context.iteration(False)
    assert voice.get_width() == closed
    window.bar_cards.close("voice")


def test_moving_along_bar_hands_over_card(window, audio):

    from gi.repository import Gdk, Gtk

    # Themes such as the BigLinux one give cards a wide shadow.
    shadow = Gtk.CssProvider()
    shadow.load_from_string(
        ".hover-card { box-shadow: 0 6px 40px 12px alpha(black, 0.3); }"
    )
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), shadow, Gtk.STYLE_PROVIDER_PRIORITY_USER
    )
    window.file_queue.add_file(str(audio))
    pump(lambda: not window.file_queue.pending)
    window.present()
    cards = window.bar_cards
    speed, voice = (b.get_parent() for b in (window.speed_button, window.voice_button))
    pump(lambda: speed.get_mapped())
    cards.open("speed")
    pump(lambda: cards.is_open("speed") and cards._card_box("speed") is not None)
    # The microphone lies under the speed card's shadow, not under the card:
    # resting there takes over.
    from gi.repository import Graphene

    _found, point = voice.compute_point(
        window, Graphene.Point().init(4, voice.get_height() / 2)
    )
    cards._on_window_motion(None, point.x, point.y)
    pump(lambda: cards.is_open("voice"))
    assert not cards.is_open("speed")
    cards.close_all()
    Gtk.StyleContext.remove_provider_for_display(Gdk.Display.get_default(), shadow)


def test_bar_and_sidebar_share_one_card(window, audio):
    window.file_queue.add_file(str(audio))
    pump(lambda: not window.file_queue.pending)
    window.present()
    bar, cards = window.bar_cards, window.effect_cards.hover_cards
    row = window.compressor_row
    pump(lambda: row.get_mapped() and window.volume_button.get_mapped())
    bar.open("volume")
    pump(lambda: bar.is_open("volume"))
    cards.open(row)
    assert cards.is_open(row) and not bar.is_open("volume")
    cards.close(row)
    # A card a click opened stays; the pointer opens nothing beside it.
    bar.open_for_keyboard("volume")
    _hand_driven(cards)
    box = cards._anchor_box(row)
    cards._intent.pointer(row, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
    _wait(0.6)
    assert bar.is_open("volume") and not cards.is_open(row)
    bar.close("volume")


def test_bar_marks_volume_outside_normal_range(window):
    anchor = window.volume_button.get_parent()
    for value, marked in (
        (49, True),
        (50, False),
        (100, False),
        (101, True),
        (0, True),
    ):
        window.volume_spin.set_value(value)
        assert anchor.has_css_class("attention") == marked, value


def test_closing_bar_card_refocuses_its_button(window, audio):
    window.file_queue.add_file(str(audio))
    pump(lambda: not window.file_queue.pending)
    window.present()
    bar = window.bar_cards
    pump(lambda: window.volume_button.get_mapped())
    bar.open_for_keyboard("volume")
    pump(lambda: window.get_focus() is not None)
    assert window.get_focus().is_ancestor(bar.panels["volume"])
    bar.close("volume")
    assert window.get_focus() is window.volume_button


def test_reaching_a_row_opens_its_card(window):
    from types import SimpleNamespace

    cards = window.effect_cards.hover_cards
    row = window.compressor_row
    window.present()
    pump(lambda: row.get_mapped())
    controller = SimpleNamespace(get_widget=lambda: row)
    cards._on_anchor_pointer(controller, 10, 10, row)
    assert cards.is_open(row)
    cards.close(row)


def test_long_card_option_wraps(window):
    # FLAC adds the 24-bit row, whose explanation is one long line.
    row = window.format_row
    row.set_selected(window._format_list.index("flac"))
    window.present()
    pump(lambda: row.get_mapped())
    cards = window.effect_cards.hover_cards
    cards.open(row)
    pump(lambda: cards.is_open(row) and row.card.get_width())
    assert row.card.get_width() <= 340
    cards.close(row)


def test_same_choice_notifies_no_one(window):
    from app.ui.effect_cards import ChoiceRow

    row = ChoiceRow("audio-speakers-symbolic", "Channels", None, ["A", "B"])
    seen = []
    row.connect("notify::selected", lambda *_a: seen.append(row.get_selected()))
    row.set_selected(1)
    row.set_selected(1)
    row.tiles.set_selected(1)
    assert seen == [1]
