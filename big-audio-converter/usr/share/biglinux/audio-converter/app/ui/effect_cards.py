"""The settings as rows of a list; each opens a card to adjust it.

A row shows what it applies. Its card opens beside the sidebar, level with
the row and joined to it by a tab, as the pointer reaches the row, and holds
a short description and the controls, like the sliders of the playback bar. A
click, tap or Enter opens it for the keyboard until Escape or a click
elsewhere.
"""

import gettext
import weakref

import gi

gi.require_version("Adw", "1")
from big_gtk_kit.hover_cards import HoverCards
from gi.repository import Adw, GLib, GObject, Graphene, Gtk, Pango

from app.main_loop import weak_callback

_ = gettext.gettext

# Content moving under a still pointer (wheel, kinetic scroll) is not hover.
SCROLL_QUIET_US = 300_000


class ChoiceTiles(Gtk.Grid):
    """Choices as tiles in a grid of ``columns``, with the ``selected`` API of
    a combo row; the choices at ``wide`` take a whole line.

    A label whose name and detail are joined by an em dash shows the detail
    under the name; a tile without one centers its name. Each tile is a radio for screen readers and the
    keyboard, its radio out of sight: the picked tile is outlined instead.
    """

    selected = GObject.Property(type=GObject.TYPE_UINT, maximum=GLib.MAXUINT32)

    def __init__(self, title, labels, columns=2, wide=()):
        super().__init__(
            row_spacing=6,
            column_spacing=6,
            column_homogeneous=True,
            accessible_role=Gtk.AccessibleRole.GROUP,
        )
        self.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.columns, self.wide = columns, wide
        self.buttons = []
        self.connect("notify::selected", weak_callback(self._show_selected))
        self.set_labels(labels)

    def set_labels(self, labels):
        while child := self.get_first_child():
            self.remove(child)
        self.buttons = []
        column = line = 0
        for index, label in enumerate(labels):
            button = _tile(label)
            if self.buttons:
                button.set_group(self.buttons[0])
            button.connect("toggled", weak_callback(self._on_toggled), index)
            if index in self.wide:
                if column:
                    column, line = 0, line + 1
                self.attach(button, 0, line, self.columns, 1)
                line += 1
            else:
                self.attach(button, column, line, 1, 1)
                column += 1
                if column == self.columns:
                    column, line = 0, line + 1
            self.buttons.append(button)
        if self.selected >= len(self.buttons):
            self.selected = 0
        self._show_selected()

    def _on_toggled(self, button, index):
        if button.get_active() and self.selected != index:
            self.selected = index

    def _show_selected(self, *_args):
        if self.buttons:
            self.buttons[self.selected].set_active(True)

    def get_selected(self):
        return self.selected

    def set_selected(self, index):
        # As Adw.ComboRow: setting the shown value again notifies no one.
        if self.selected != index:
            self.selected = index


def _tile(label):
    name, _dash, detail = label.partition(" — ")
    text = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=2,
        hexpand=True,
        valign=Gtk.Align.CENTER,
    )
    text.append(
        Gtk.Label(
            label=name,
            xalign=0 if detail else 0.5,
            justify=Gtk.Justification.CENTER,
            wrap=True,
            css_classes=["option-name"],
        )
    )
    if detail:
        detail = detail[:1].upper() + detail[1:]
        text.append(
            Gtk.Label(label=detail, xalign=0, wrap=True, css_classes=["option-detail"])
        )
    tile = Gtk.CheckButton(child=text, css_classes=["option"])
    # Named by its name alone, its detail read after it: GTK would name it by
    # both labels run together.
    tile.update_property([Gtk.AccessibleProperty.LABEL], [name])
    if detail:
        tile.update_property([Gtk.AccessibleProperty.DESCRIPTION], [detail])
    return tile


class SettingRow(Gtk.ListBoxRow):
    """An icon badge, the setting's name and its value, with a card.

    ``body`` holds the controls shown in the row's card; a row without one
    has no card. Activating a switch row flips ``switch``, and one with
    ``on_activate`` runs it, instead of opening the card: the click is then
    the change itself. A row with a ``reset`` method offers it in the card;
    corrections that are off by default need none.
    """

    reset = None  # returns the setting to its default
    counts = True  # an applied row is an adjustment of the sound
    unavailable = None  # why the row is grayed

    def __init__(self, icon, title, description, body, switch=None, on_activate=None):
        super().__init__(css_classes=["setting-row"], activatable=True)
        self.title = title
        self.description = description
        self.body = body
        self.switch = switch
        self.on_activate = on_activate
        self.applied = False
        self.cards = None  # weak reference, set by EffectCards.add_row
        self.update_property([Gtk.AccessibleProperty.LABEL], [title])

        box = Gtk.Box(spacing=12)
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
        text.append(
            Gtk.Label(
                label=title,
                xalign=0,
                wrap=True,
                lines=2,
                ellipsize=Pango.EllipsizeMode.END,
                css_classes=["effect-title"],
            )
        )
        # A grayed row says here why, not in a tooltip alone.
        self.reason_label = Gtk.Label(
            xalign=0, wrap=True, visible=False, css_classes=["caption", "effect-reason"]
        )
        text.append(self.reason_label)
        box.append(text)
        self.value_label = Gtk.Label(
            xalign=1,
            justify=Gtk.Justification.RIGHT,
            wrap=True,
            lines=2,
            ellipsize=Pango.EllipsizeMode.END,
            # Asks for its unwrapped width, so "Very good · 192 kbps" keeps to
            # one line and wraps only in a narrow sidebar.
            natural_wrap_mode=Gtk.NaturalWrapMode.NONE,
            valign=Gtk.Align.CENTER,
            css_classes=["effect-value"],
        )
        self._value = ""
        box.append(self.value_label)
        self.set_child(box)

        self.card = self.reset_button = None
        if body is None:
            return
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=300)
        # The row beside the card already shows its name and value; the card
        # opens with what the row lacks.
        about = Gtk.Label(
            label=description or "",
            xalign=0,
            wrap=True,
            max_width_chars=36,
            hexpand=True,
            visible=bool(description),
            css_classes=["card-description"],
        )
        card.append(about)
        controls = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=16,
            margin_start=12,
            margin_end=12,
        )
        controls.append(body)
        if self.reset is not None:
            owner = weakref.proxy(self)
            self.reset_button = Gtk.Button(label=_("Reset"), halign=Gtk.Align.END)
            self.reset_button.connect("clicked", lambda _b: owner.reset())
            controls.append(self.reset_button)
        card.append(controls)
        # A bounded width: a long line in the body wraps instead of widening
        # the card.
        self.card = Adw.Clamp(child=card, maximum_size=340, tightening_threshold=340)

    def set_unavailable(self, reason):
        """Greyed with ``reason`` under its name in place of its value, or
        usable again."""
        self.unavailable = reason
        cards = self.cards() if self.cards else None
        if reason and cards and self.card is not None:
            cards.hover_cards.close(self)
        self.set_sensitive(reason is None)
        self.set_tooltip_text(reason)
        self.reason_label.set_text(reason or "")
        self.reason_label.set_visible(reason is not None)
        self.value_label.set_visible(reason is None)
        self._describe()

    def _describe(self):
        text = self._value
        detail = self.unavailable or self.description
        self.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION],
            [f"{text}. {detail}" if detail else text],
        )

    def show_value(self, text, applied):
        """Show the value on the row and to screen readers."""
        self.applied = applied
        self._value = text
        self.value_label.set_text(text)
        (self.add_css_class if applied else self.remove_css_class)("applied")
        if self.reset_button is not None:
            self.reset_button.set_sensitive(applied)
        self._describe()
        cards = self.cards() if self.cards else None
        if cards:
            cards.row_changed(self)


class ChoiceRow(SettingRow):
    """One of a few choices, named on the row and picked in its card.

    It has the ``selected`` API of a combo row. A choice is no effect: the
    row never counts as an applied adjustment. Its card shows the choices as
    :class:`ChoiceTiles` of ``columns``, ``wide`` and labels.
    """

    selected = GObject.Property(type=GObject.TYPE_UINT, maximum=GLib.MAXUINT32)

    def __init__(
        self,
        icon,
        title,
        description,
        labels,
        short_labels=None,
        extra=(),
        columns=2,
        wide=(),
    ):
        self.tiles = ChoiceTiles(title, labels, columns, wide)
        # The card scrolls as a whole once the window is too short for it.
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.append(self.tiles)
        for widget in extra:
            body.append(widget)
        super().__init__(icon, title, description, body)
        self._model = Gtk.StringList.new(labels)
        self.short_labels = list(short_labels or ())
        self.bind_property(
            "selected",
            self.tiles,
            "selected",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        self.connect("notify::selected", weak_callback(self._show_selected))
        self._show_selected()

    def set_model(self, model, short_labels=None):
        self._model = model
        self.short_labels = list(short_labels or ())
        self.tiles.set_labels(
            [model.get_string(index) for index in range(model.get_n_items())]
        )
        self._show_selected()

    def _show_selected(self, *_args):
        text = (
            self.short_labels[self.selected]
            if self.selected < len(self.short_labels)
            else self._model.get_string(self.selected).partition(" — ")[0]
        )
        self.show_value(text, False)

    def focus_target(self):
        return self.tiles.buttons[self.selected]

    def get_selected(self):
        return self.selected

    def set_selected(self, index):
        # As Adw.ComboRow: setting the shown value again notifies no one.
        if self.selected != index:
            self.selected = index

    def get_selected_item(self):
        return self._model.get_item(self.selected)


class StepRow(SettingRow):
    """A correction in a few named steps, picked in its card; the first is off."""

    def __init__(self, icon, title, description, labels):
        self.steps = ChoiceTiles(title, labels)
        super().__init__(icon, title, description, self.steps)
        self.labels = labels
        self.steps.connect("notify::selected", weak_callback(self._show))
        self._show()

    def _show(self, *_args):
        step = self.steps.get_selected()
        self.show_value(self.labels[step], step > 0)

    def focus_target(self):
        return self.steps.buttons[self.steps.get_selected()]


class SwitchRow(SettingRow):
    """A correction that is on or off, picked in its card's two tiles;
    activating the row flips it. ``switch`` holds the state."""

    def __init__(self, icon, title, description):
        self.tiles = ChoiceTiles(title, [_("Off"), _("On")])
        super().__init__(icon, title, description, self.tiles, switch=Gtk.Switch())
        self.switch.connect("notify::active", weak_callback(self._show))
        self.tiles.connect("notify::selected", weak_callback(self._on_picked))
        self._show()

    def _on_picked(self, *_args):
        self.switch.set_active(self.tiles.get_selected() == 1)

    def _show(self, *_args):
        active = self.switch.get_active()
        self.tiles.set_selected(int(active))
        self.show_value(_("On") if active else _("Off"), active)

    def focus_target(self):
        return self.tiles.buttons[self.tiles.get_selected()]

    def get_active(self):
        return self.switch.get_active()

    def set_active(self, active):
        self.switch.set_active(active)


class EffectCards:
    """The groups of rows in the sidebar and the cards they open."""

    def __init__(self, scrolled, layer):
        self.scrolled = scrolled
        # A card rises from its row: a bridge as tall as the row's ring joins
        # them.
        self.hover_cards = HoverCards(layer, Gtk.PositionType.RIGHT, bridge=True)
        self.hover_cards.can_open = weak_callback(self._can_open)
        self.hover_cards.edge = weak_callback(self._sidebar_edge)
        self.groups = []  # (rows, count label)
        self.on_changed = None  # told after any row's state changes
        self._scrolled_at = 0
        owner = weakref.proxy(self)
        self._scroll_handler = scrolled.get_vadjustment().connect(
            "value-changed", lambda _a: owner._note_scroll()
        )

    def add_group(self, title):
        """A group of rows, titled if ``title``; returns its box and list."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        count = Gtk.Label(css_classes=["dim-label", "caption"])
        if title:
            header = Gtk.Box(spacing=8)
            header.append(
                Gtk.Label(label=title, xalign=0, hexpand=True, css_classes=["heading"])
            )
            header.append(count)
            box.append(header)
        listbox = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            activate_on_single_click=True,
            css_classes=["boxed-list"],
        )
        listbox.connect("row-activated", weak_callback(self._on_activated))
        box.append(listbox)
        self.groups.append(([], count))
        listbox.group_index = len(self.groups) - 1
        return box, listbox

    def add_row(self, listbox, row):
        row.cards = weakref.ref(self)
        row.group_index = listbox.group_index
        self.groups[listbox.group_index][0].append(row)
        listbox.append(row)
        if row.card is not None:
            # A click gives the keyboard to the chosen step, or the card's one
            # control.
            focus = getattr(row, "focus_target", lambda: row.body)
            self.hover_cards.attach(
                row, row, row.card, row.title, ["effect-card"], focus=focus
            )
        self.row_changed(row)

    def row_changed(self, row):
        rows, count = self.groups[row.group_index]
        active = sum(t.applied and t.counts for t in rows)
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
            row.applied and row.counts for rows, _count in self.groups for row in rows
        )

    def _on_activated(self, _listbox, row):
        if row.switch is not None:
            row.switch.set_active(not row.switch.get_active())
        elif row.on_activate is not None:
            self.close()
            row.on_activate()
        else:
            self.hover_cards.open_for_keyboard(row)

    def _can_open(self, row):
        # Rows moving under a still pointer (wheel, kinetic scroll) are not
        # hover.
        return GLib.get_monotonic_time() - self._scrolled_at >= SCROLL_QUIET_US

    def _sidebar_edge(self, _row):
        """Cards open just past the sidebar's edge, over the queue."""
        root = self.scrolled.get_root()
        found, edge = self.scrolled.compute_point(
            root, Graphene.Point().init(self.scrolled.get_width() + 6, 0)
        )
        return edge.x if found else 0

    def _note_scroll(self):
        self._scrolled_at = GLib.get_monotonic_time()
        # The pointer's card points where its row was.
        self.hover_cards.close_hover()

    def close(self):
        self.hover_cards.close_all()

    def cleanup(self):
        self.on_changed = None
        self.hover_cards.unparent_all()
        self.scrolled.get_vadjustment().disconnect(self._scroll_handler)
