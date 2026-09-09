"""A keyboard- and screen-reader-accessible alternative to waveform editing."""

import gettext
from gi.repository import Adw, Gtk
from app.audio.models import Segment

_ = gettext.gettext


class SegmentEditor(Adw.Dialog):
    """Edit a local draft; only Apply changes the application's segment model."""

    def __init__(self, duration, segments, on_apply, on_seek):
        super().__init__(title=_("Edit Segments"), content_width=640, content_height=520)
        self.duration = duration
        self._on_apply = on_apply
        self._on_seek = on_seek
        self._draft = [{"start": item["start"], "stop": item["stop"]} for item in segments]
        self._rows = []
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda button: self.close())
        header.pack_start(cancel)
        self.apply_button = Gtk.Button(label=_("Apply"))
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", self._apply)
        header.pack_end(self.apply_button)
        toolbar.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_start=16, margin_end=16, margin_top=12, margin_bottom=16)
        help_text = Gtk.Label(label=_("Enter start and end times in seconds. Reorder segments to change their numbered order. Changes are saved only when you apply them."),
                             xalign=0, wrap=True)
        box.append(help_text)
        self.error = Gtk.Label(xalign=0, wrap=True, focusable=True)
        self.error.add_css_class("error")
        box.append(self.error)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.listbox)
        box.append(scroll)
        self.add_button = Gtk.Button(label=_("Add Segment"), halign=Gtk.Align.START)
        self.add_button.connect("clicked", self._add)
        box.append(self.add_button)
        toolbar.set_content(box)
        self.set_child(toolbar)
        self._render()

    def _spin(self, value, label):
        field = Gtk.SpinButton.new_with_range(0, self.duration, 0.01)
        field.set_digits(3)
        field.set_numeric(True)
        field.set_value(value)
        field.set_hexpand(True)
        field.update_property([Gtk.AccessibleProperty.LABEL], [label])
        return field

    def _read(self):
        result = []
        for start, stop in self._rows:
            start.update()
            stop.update()
            result.append({"start": start.get_value(), "stop": stop.get_value()})
        return result

    def _render(self, focus_index=None):
        child = self.listbox.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.listbox.remove(child)
            child = following
        self._rows = []
        for index, item in enumerate(self._draft):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                          margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
            name = _("Segment {number}").format(number=index + 1)
            heading = Gtk.Label(label=name, xalign=0)
            heading.add_css_class("heading")
            row.append(heading)
            grid = Gtk.Grid(column_spacing=12, row_spacing=6)
            start = self._spin(item["start"], _("{segment}: start, seconds").format(segment=name))
            stop = self._spin(item["stop"], _("{segment}: end, seconds").format(segment=name))
            for column, (label, field) in enumerate(((_("Start (seconds)"), start), (_("End (seconds)"), stop))):
                text = Gtk.Label(label=label, xalign=0)
                text.set_mnemonic_widget(field)
                grid.attach(text, column, 0, 1, 1)
                grid.attach(field, column, 1, 1, 1)
            row.append(grid)
            actions = Gtk.Box(spacing=6)
            for label, callback, sensitive in (
                (_("Go to Start"), lambda button, field=start: self._on_seek(field.get_value()), True),
                (_("Move Up"), lambda button, index=index: self._move(index, -1), index > 0),
                (_("Move Down"), lambda button, index=index: self._move(index, 1), index + 1 < len(self._draft)),
                (_("Remove"), lambda button, index=index: self._remove(index), True),
            ):
                button = Gtk.Button(label=label, sensitive=sensitive)
                button.update_property([Gtk.AccessibleProperty.LABEL], [_("{action}: {segment}").format(action=label, segment=name)])
                button.connect("clicked", callback)
                actions.append(button)
            row.append(actions)
            self.listbox.append(row)
            self._rows.append((start, stop))
        self.add_button.set_sensitive(len(self._draft) < 256 and self.duration >= 0.1)
        if focus_index is not None and self._rows:
            self._rows[min(focus_index, len(self._rows) - 1)][0].grab_focus()
        elif not self._rows:
            self.add_button.grab_focus()

    def _add(self, button):
        self._draft = self._read()
        start = min(self._draft[-1]["stop"], self.duration - 0.1) if self._draft else 0
        self._draft.append({"start": max(0, start), "stop": min(self.duration, start + 1)})
        self._render(len(self._draft) - 1)

    def _move(self, index, direction):
        self._draft = self._read()
        target = index + direction
        if 0 <= target < len(self._draft):
            self._draft[index], self._draft[target] = self._draft[target], self._draft[index]
            self._render(target)

    def _remove(self, index):
        self._draft = self._read()
        self._draft.pop(index)
        self._render(index)

    def _apply(self, button):
        try:
            draft = self._read()
            # Unlike mouse gestures, typed start/end values must not silently swap.
            if any(item["stop"] <= item["start"] for item in draft):
                raise ValueError("Segment end must follow its start")
            values = [Segment.from_mapping(item, self.duration).as_dict() for item in draft]
            for index, item in enumerate(values):
                item["segment_index"] = index + 1
            if self._on_apply(values) is False:
                return
        except ValueError:
            self.error.set_text(_("Each segment must end after its start, last at least 0.1 seconds, and stay within the file."))
            self.error.grab_focus()
            return
        self.close()
