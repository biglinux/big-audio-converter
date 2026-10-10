"""The sound settings as tiles; each opens a card to adjust it.

A tile shows what it applies. Its card, above it or below when there is no
room, opens as the pointer reaches the tile and holds a fuller description
and the controls, like the sliders of the playback bar. A click, tap or Enter
opens it for the keyboard until Escape or a click elsewhere.
"""

import gettext
import weakref

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Graphene, Gtk, Pango

from app.main_loop import MainLoopSources, weak_callback
from app.ui.hover_popovers import HoverPopovers

_ = gettext.gettext

# Content moving under a still pointer (wheel, kinetic scroll) is not hover.
SCROLL_QUIET_US = 300_000


class ChoiceToggles(Gtk.Box):
    """Linked toggle buttons with the ``selected`` API of a combo row."""

    selected = GObject.Property(type=GObject.TYPE_UINT, maximum=GLib.MAXUINT32)

    def __init__(self, title, labels, orientation=Gtk.Orientation.HORIZONTAL):
        # Not homogeneous: two buttons at the widest label's width overflow
        # a narrow card.
        super().__init__(css_classes=["linked"], orientation=orientation)
        self.set_accessible_role(Gtk.AccessibleRole.GROUP)
        self.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.buttons = []
        for index, label in enumerate(labels):
            button = Gtk.ToggleButton(label=label, active=index == 0, hexpand=True)
            if self.buttons:
                button.set_group(self.buttons[0])
            button.connect("toggled", weak_callback(self._on_toggled), index)
            self.append(button)
            self.buttons.append(button)
        self.connect("notify::selected", weak_callback(self._show_selected))

    def _on_toggled(self, button, index):
        if button.get_active() and self.selected != index:
            self.selected = index

    def _show_selected(self, *_args):
        self.buttons[self.selected].set_active(True)

    def get_selected(self):
        return self.selected

    def set_selected(self, index):
        self.selected = index


class EffectTile(Gtk.FlowBoxChild):
    """An icon badge, the setting's name and its value, with a card.

    ``body`` holds the controls shown in the tile's card; a tile without one
    has no card. Activating a switch tile flips ``switch``, and one with
    ``on_activate`` runs it, instead of opening the card: the click is then
    the change itself. A tile with a ``reset`` method offers it in the card;
    corrections that are off by default need none.
    """

    reset = None  # returns the setting to its default
    counts = True  # an applied tile is an adjustment of the sound
    unavailable = None  # why the tile is grayed

    def __init__(self, icon, title, description, body, switch=None, on_activate=None):
        super().__init__(
            css_classes=["card", "effect-tile"],
            accessible_role=Gtk.AccessibleRole.BUTTON,
        )
        self.title = title
        self.description = description
        self.body = body
        self.switch = switch
        self.on_activate = on_activate
        self.applied = False
        self.cards = None  # weak reference, set by EffectCards.add_tile
        self.set_cursor_from_name("pointer")
        self.update_property([Gtk.AccessibleProperty.LABEL], [title])

        box = Gtk.Box(spacing=10)
        # Filled when the setting applies: the state shows in a shape too,
        # not only in the color of the value.
        self.icon = Gtk.Image(
            valign=Gtk.Align.CENTER,
            css_classes=["effect-badge"],
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        if isinstance(icon, str):
            self.icon.set_from_icon_name(icon)
        else:
            self.icon.set_from_gicon(icon)
        box.append(self.icon)
        text = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            valign=Gtk.Align.CENTER,
            hexpand=True,
        )
        # Titles wrap so two tiles share a line even in the narrowest sidebar.
        text.append(
            Gtk.Label(
                label=title,
                xalign=0,
                wrap=True,
                lines=2,
                ellipsize=Pango.EllipsizeMode.END,
                max_width_chars=12,
                css_classes=["heading"],
            )
        )
        # Up to three lines: a grayed tile says here why, not in a tooltip
        # alone, and a reason cut short says nothing.
        self.value_label = Gtk.Label(
            xalign=0,
            wrap=True,
            lines=3,
            ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=12,
            css_classes=["effect-value", "caption"],
        )
        self._value = ""
        text.append(self.value_label)
        box.append(text)
        self.set_child(box)

        self.popover = self.reset_button = None
        if body is None:
            return
        card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            width_request=300,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        header = Gtk.Box(spacing=8)
        header.append(
            Gtk.Label(
                label=title, xalign=0, hexpand=True, wrap=True, css_classes=["heading"]
            )
        )
        self.card_value = Gtk.Label(css_classes=["effect-value", "numeric"])
        header.append(self.card_value)
        card.append(header)
        if description:
            card.append(
                Gtk.Label(
                    label=description,
                    xalign=0,
                    wrap=True,
                    max_width_chars=36,
                    css_classes=["dim-label"],
                )
            )
        card.append(body)
        if self.reset is not None:
            owner = weakref.proxy(self)
            self.reset_button = Gtk.Button(label=_("Reset"), halign=Gtk.Align.END)
            self.reset_button.connect("clicked", lambda _b: owner.reset())
            card.append(self.reset_button)
        # A bounded width: a long line in the body wraps instead of widening
        # the card past its neighbors.
        # Beside the sidebar, over the queue: above or below the tile it
        # covered the neighboring tiles. EffectCards points it at the edge.
        self.popover = Gtk.Popover(
            child=Adw.Clamp(child=card, maximum_size=340, tightening_threshold=340),
            position=Gtk.PositionType.RIGHT,
            css_classes=["effect-card"],
        )
        self.popover.update_property([Gtk.AccessibleProperty.LABEL], [title])

    def set_unavailable(self, reason):
        """Greyed with ``reason`` in place of its value, or usable again."""
        self.unavailable = reason
        if reason and self.popover is not None:
            self.popover.popdown()
        self.set_sensitive(reason is None)
        self.set_tooltip_text(reason)
        self.value_label.set_text(reason or self._value)
        self._describe()

    def _describe(self):
        text = self._value
        detail = self.unavailable or self.description
        self.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION],
            [f"{text}. {detail}" if detail else text],
        )

    def show_value(self, text, applied):
        """Show the value on the tile, in its card and to screen readers."""
        self.applied = applied
        self._value = text
        self.value_label.set_text(self.unavailable or text)
        (self.add_css_class if applied else self.remove_css_class)("applied")
        if self.popover is not None:
            self.card_value.set_text(text)
        if self.reset_button is not None:
            self.reset_button.set_sensitive(applied)
        self._describe()
        cards = self.cards() if self.cards else None
        if cards:
            cards.tile_changed(self)


class ChoiceTile(EffectTile):
    """One of a few choices, named on the tile and picked in its card.

    It has the ``selected`` API of a combo row. A choice is no effect: the
    tile never counts as an applied adjustment. "Name — detail" entries show
    the detail under the name.
    """

    selected = GObject.Property(type=GObject.TYPE_UINT, maximum=GLib.MAXUINT32)

    def __init__(self, icon, title, description, labels, short_labels=None, extra=()):
        self._choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.append(
            Gtk.ScrolledWindow(
                child=self._choices,
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                propagate_natural_height=True,
                max_content_height=360,
            )
        )
        for widget in extra:
            body.append(widget)
        super().__init__(icon, title, description, body)
        self._buttons = []
        self._model = None
        self.short_labels = []
        self.connect("notify::selected", weak_callback(self._show_selected))
        self.set_model(Gtk.StringList.new(labels), short_labels)

    def set_model(self, model, short_labels=None):
        self._model = model
        self.short_labels = list(short_labels or ())
        while child := self._choices.get_first_child():
            self._choices.remove(child)
        self._buttons = []
        for index in range(model.get_n_items()):
            name, _dash, detail = model.get_string(index).partition(" — ")
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            content.append(Gtk.Label(label=name, xalign=0, wrap=True))
            if detail:
                content.append(
                    Gtk.Label(
                        label=detail[:1].upper() + detail[1:],
                        xalign=0,
                        wrap=True,
                        css_classes=["caption", "dim-label"],
                    )
                )
            button = Gtk.CheckButton(child=content)
            if self._buttons:
                button.set_group(self._buttons[0])
            button.connect("toggled", weak_callback(self._on_toggled), index)
            self._choices.append(button)
            self._buttons.append(button)
        if self.selected >= len(self._buttons):
            self.selected = 0
        self._show_selected()

    def _on_toggled(self, button, index):
        if button.get_active() and self.selected != index:
            self.selected = index

    def _show_selected(self, *_args):
        if not self._buttons:
            return
        self._buttons[self.selected].set_active(True)
        text = (
            self.short_labels[self.selected]
            if self.selected < len(self.short_labels)
            else self._model.get_string(self.selected).partition(" — ")[0]
        )
        self.show_value(text, False)

    def get_selected(self):
        return self.selected

    def set_selected(self, index):
        self.selected = index

    def get_selected_item(self):
        return self._model.get_item(self.selected)


class StepTile(EffectTile):
    """A correction in a few named steps, picked in its card; the first is off."""

    def __init__(self, icon, title, description, labels):
        self.steps = ChoiceToggles(title, labels)
        super().__init__(icon, title, description, self.steps)
        self.labels = labels
        self.steps.connect("notify::selected", weak_callback(self._show))
        self._show()

    def _show(self, *_args):
        step = self.steps.get_selected()
        self.show_value(self.labels[step], step > 0)

    def focus_target(self):
        return self.steps.buttons[self.steps.get_selected()]


class SwitchTile(EffectTile):
    """A correction that is on or off; activating the tile flips it."""

    def __init__(self, icon, title, description):
        switch = Gtk.Switch(halign=Gtk.Align.START)
        switch.update_property([Gtk.AccessibleProperty.LABEL], [title])
        super().__init__(icon, title, description, switch, switch=switch)
        switch.connect("notify::active", weak_callback(self._show))
        self._show()

    def _show(self, *_args):
        active = self.switch.get_active()
        self.show_value(_("On") if active else _("Off"), active)

    def get_active(self):
        return self.switch.get_active()

    def set_active(self, active):
        self.switch.set_active(active)


class EffectCards:
    """The groups of tiles in the sidebar and the cards they open."""

    def __init__(self, scrolled):
        self.scrolled = scrolled
        self._sources = MainLoopSources()
        # A card opens once the pointer rests on its tile, not on the way past.
        self.popovers = HoverPopovers(self._sources, open_delay_ms=150)
        self.popovers.can_open = weak_callback(self._can_open)
        self.popovers.before_open = weak_callback(self._point_at_edge)
        self.groups = []  # (tiles, count label)
        self.on_changed = None  # told after any tile's state changes
        self._scrolled_at = 0
        owner = weakref.proxy(self)
        self._scroll_handler = scrolled.get_vadjustment().connect(
            "value-changed", lambda _a: owner._note_scroll()
        )

    def add_group(self, title, columns=2):
        """A group of tiles, titled if ``title``; returns its box and flow box."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        count = Gtk.Label(css_classes=["dim-label", "caption"])
        if title:
            header = Gtk.Box(spacing=8)
            header.append(
                Gtk.Label(label=title, xalign=0, hexpand=True, css_classes=["heading"])
            )
            header.append(count)
            box.append(header)
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            # Side by side always: by natural width, a long title in a wider
            # font would drop a group to one tile per line.
            min_children_per_line=columns,
            max_children_per_line=columns,
            column_spacing=6,
            row_spacing=6,
            activate_on_single_click=True,
        )
        flow.connect("child-activated", weak_callback(self._on_activated))
        box.append(flow)
        self.groups.append(([], count))
        flow.group_index = len(self.groups) - 1
        return box, flow

    def add_tile(self, flow, tile):
        tile.cards = weakref.ref(self)
        tile.group_index = flow.group_index
        self.groups[flow.group_index][0].append(tile)
        flow.append(tile)
        if tile.popover is not None:
            # A click gives the keyboard to the chosen step, or the card's one
            # control.
            focus = getattr(tile, "focus_target", lambda: tile.body)
            self.popovers.attach(tile, tile, tile.popover, focus=focus)
        self.tile_changed(tile)

    def tile_changed(self, tile):
        tiles, count = self.groups[tile.group_index]
        active = sum(t.applied and t.counts for t in tiles)
        count.set_text(
            gettext.ngettext("{count} active", "{count} active", active).format(
                count=active
            )
            if active
            else ""
        )
        if self.on_changed is not None:
            self.on_changed()

    def active_count(self):
        return sum(
            tile.applied and tile.counts
            for tiles, _count in self.groups
            for tile in tiles
        )

    def _on_activated(self, _flow, tile):
        if tile.switch is not None:
            tile.switch.set_active(not tile.switch.get_active())
        elif tile.on_activate is not None:
            self.close()
            tile.on_activate()
        else:
            self.popovers.open_for_keyboard(tile)

    def _can_open(self, tile):
        return (
            tile.is_sensitive()
            and GLib.get_monotonic_time() - self._scrolled_at >= SCROLL_QUIET_US
        )

    def _point_at_edge(self, tile):
        """Point the card at the sidebar's edge level with the tile, so it
        opens over the queue, clear of the tiles in the other column."""
        found, edge = self.scrolled.compute_point(
            tile, Graphene.Point().init(self.scrolled.get_width(), 0)
        )
        if found:
            rect = Gdk.Rectangle()
            rect.x, rect.y = int(edge.x) - 1, 0
            rect.width, rect.height = 1, tile.get_height()
            tile.popover.set_pointing_to(rect)

    def _note_scroll(self):
        self._scrolled_at = GLib.get_monotonic_time()

    def close(self):
        self.popovers.close_all()

    def cleanup(self):
        self.on_changed = None
        self._sources.close()
        self.popovers.unparent_all()
        self.scrolled.get_vadjustment().disconnect(self._scroll_handler)
