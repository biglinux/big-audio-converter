"""Keyboard-accessible numeric editing of the same segments as the waveform."""
import gettext
import weakref

from gi.repository import Adw, Gtk

from app.audio.media import Segment
from app.audio.process import MediaError

_ = gettext.gettext


class SegmentEditor(Adw.Dialog):
    def __init__(self, window):
        super().__init__(title=_("Edit Segments"), content_width=580, content_height=580)
        self.window = weakref.ref(window)
        self.source = window.active_audio_id
        self.duration = window.visualizer.duration or window.player.duration
        self.rows = []
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_start=18, margin_end=18, margin_top=12, margin_bottom=18)
        box.append(Gtk.Label(label=_("Set start and end times in seconds. Changes are applied only when you press Apply."), wrap=True, xalign=0))
        position_row = Gtk.Box(spacing=8)
        label = Gtk.Label(label=_("Position (seconds)"))
        self.position = self._spin(window.player._position)
        self.position.update_property([Gtk.AccessibleProperty.LABEL], [_("Playback position in seconds")])
        seek = Gtk.Button.new_with_mnemonic(_("_Seek"))
        seek.connect("clicked", self._seek)
        position_row.append(label)
        position_row.append(self.position)
        position_row.append(seek)
        box.append(position_row)
        self.error = Gtk.Label(wrap=True, xalign=0, visible=False)
        self.error.add_css_class("error")
        box.append(self.error)
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scrolled.set_child(self.list_box)
        box.append(scrolled)
        add = Gtk.Button.new_with_mnemonic(_("_Add Segment"))
        add.set_halign(Gtk.Align.START)
        add.connect("clicked", lambda button: self.add_segment(0, min(self.duration, 1)))
        box.append(add)
        self.add_button = add
        for segment in window.visualizer.get_marker_pairs():
            self.add_segment(segment["start"], segment["stop"])
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button.new_with_mnemonic(_("_Cancel"))
        cancel.connect("clicked", lambda button: self.close())
        self.apply_button = Gtk.Button.new_with_mnemonic(_("_Apply"))
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", self.apply)
        buttons.append(cancel)
        buttons.append(self.apply_button)
        box.append(buttons)
        toolbar.set_content(box)
        self.set_child(toolbar)

    def _spin(self, value):
        spin = Gtk.SpinButton.new_with_range(0, max(self.duration, 1), .01)
        spin.set_digits(6)
        spin.set_numeric(True)
        spin.set_value(value)
        spin.set_hexpand(True)
        return spin

    def add_segment(self, start, stop):
        group = Adw.PreferencesGroup()
        fields = Gtk.Grid(column_spacing=8, row_spacing=8)
        start_spin, stop_spin = self._spin(start), self._spin(stop)
        fields.attach(Gtk.Label(label=_("Start (seconds)"), xalign=0), 0, 0, 1, 1)
        fields.attach(start_spin, 1, 0, 1, 1)
        fields.attach(Gtk.Label(label=_("End (seconds)"), xalign=0), 0, 1, 1, 1)
        fields.attach(stop_spin, 1, 1, 1, 1)
        actions = Gtk.Box(spacing=6)
        row = {"group": group, "start": start_spin, "stop": stop_spin}
        for name, label, icon, callback in (
            ("up", _("Move segment up"), "go-up-symbolic", lambda button: self.move(row, -1)),
            ("down", _("Move segment down"), "go-down-symbolic", lambda button: self.move(row, 1)),
            ("remove", _("Remove segment"), "list-remove-symbolic", lambda button: self.remove(row)),
        ):
            button = Gtk.Button(icon_name=icon, tooltip_text=label)
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            button.connect("clicked", callback)
            actions.append(button)
            row[name] = button
        fields.attach(actions, 0, 2, 2, 1)
        group.add(fields)
        self.rows.append(row)
        self.list_box.append(group)
        self._renumber()

    def _renumber(self):
        for number, row in enumerate(self.rows, 1):
            row["group"].set_title(_("Segment {number}").format(number=number))
            row["start"].update_property([Gtk.AccessibleProperty.LABEL], [_("Segment {number} start in seconds").format(number=number)])
            row["stop"].update_property([Gtk.AccessibleProperty.LABEL], [_("Segment {number} end in seconds").format(number=number)])
            row["up"].set_sensitive(number > 1)
            row["down"].set_sensitive(number < len(self.rows))

    def remove(self, row):
        if row not in self.rows:
            return
        self.rows.remove(row)
        self.list_box.remove(row["group"])
        self._renumber()
        self.add_button.grab_focus()

    def move(self, row, offset):
        if row not in self.rows:
            return
        current = self.rows.index(row)
        target = current + offset
        if not 0 <= target < len(self.rows):
            return
        self.rows.pop(current)
        self.rows.insert(target, row)
        self.list_box.reorder_child_after(row["group"], self.rows[target - 1]["group"] if target else None)
        self._renumber()
        row["start"].grab_focus()

    def _seek(self, button):
        window = self.window()
        if window and not window._closed and window.active_audio_id == self.source:
            if window.player.current_file != self.source:
                window.player.load(self.source, window.file_queue.track_metadata)
            self.position.update()
            window.player.seek(self.position.get_value())

    def apply(self, button=None):
        window = self.window()
        if window is None or window._closed:
            self.close()
            return
        if window.active_audio_id != self.source:
            self.error.set_text(_("The active file changed. Reopen the segment editor for the current file."))
            self.error.set_visible(True)
            return
        try:
            segments = []
            for number, row in enumerate(self.rows, 1):
                row["start"].update()
                row["stop"].update()
                segment = Segment.from_mapping({"start": row["start"].get_value(),
                                                "stop": row["stop"].get_value(),
                                                "segment_index": number}, self.duration)
                segments.append(segment.as_mapping())
        except (MediaError, ValueError) as exc:
            self.error.set_text(str(exc))
            self.error.set_visible(True)
            return
        window.file_markers[self.source] = segments
        if window.cut_row.get_selected() == 0:
            window.cut_row.set_selected(1)
        window.visualizer.restore_markers(segments)
        window._update_play_selection_button()
        self.close()
