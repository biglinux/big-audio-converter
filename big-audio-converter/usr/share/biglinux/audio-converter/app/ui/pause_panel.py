"""Find the pauses of the shown file and mark the speech between them.

What is marked is what is exported, so the pauses go by marking the speech to
keep and joining it.

The panel is the card of the sidebar's "Remove pauses" tile; the tile shows how
an analysis of a long file goes while the rest of the window stays usable.
"""

import gettext
import shutil
import weakref
from typing import NamedTuple

from gi.repository import Adw, Gtk

from app.audio.media import MediaSource
from app.audio.pauses import PAUSE_LENGTHS, PauseFinder, find_pauses, find_speech
from app.audio.process import MediaError
from app.main_loop import weak_callback
from app.ui.time_formatter import format_time_display

_ = gettext.gettext


class Applied(NamedTuple):
    """What marking replaced, kept until the file or its marks change."""

    source_id: str
    previous_marks: list
    previous_remove: bool
    previous_order: int
    previous_output: int
    marks: list


def mark_spans(markers):
    return [(m["start"], m["stop"]) for m in markers]


class PausePanel(Adw.PreferencesGroup):
    def __init__(self, window):
        super().__init__()
        self.window = weakref.ref(window)
        self.tile = None  # weak reference to the tile whose card this is
        self._failed = False
        self.finder = PauseFinder()
        self.source_id = None  # queue entry the analysis or levels belong to
        self.levels = None
        self.pauses = []
        self.speech = []  # what marking keeps: the parts between the pauses
        self.undo = None  # Applied
        owner = weakref.proxy(self)

        lengths = Gtk.Box(
            css_classes=["linked"],
            valign=Gtk.Align.CENTER,
            accessible_role=Gtk.AccessibleRole.GROUP,
        )
        lengths.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Pauses longer than")]
        )
        self.length_buttons = []
        for seconds, label in zip(
            PAUSE_LENGTHS, (_("0.5 s"), _("1 s"), _("2 s")), strict=True
        ):
            button = Gtk.ToggleButton(label=label)
            if self.length_buttons:
                button.set_group(self.length_buttons[0])
            button.seconds = seconds
            lengths.append(button)
            self.length_buttons.append(button)
        self.length_row = Adw.ActionRow(title=_("Pauses longer than"))
        self.length_row.add_suffix(lengths)
        self.add(self.length_row)

        # One row says where the analysis stands and offers its next step.
        self.status_row = Adw.ActionRow(title_lines=3, subtitle_lines=3)
        self.actions = Gtk.Stack(valign=Gtk.Align.CENTER, hhomogeneous=False)
        for name, label, handler, suggested in (
            ("find", _("Find Pauses"), lambda _b: owner._on_find(), True),
            ("cancel", _("Cancel"), lambda _b: owner._on_cancel(), False),
            ("retry", _("Try Again"), lambda _b: owner._on_find(), False),
        ):
            button = Gtk.Button(label=label)
            if suggested:
                button.add_css_class("suggested-action")
            button.connect("clicked", handler)
            self.actions.add_named(button, name)
        self.status_row.add_suffix(self.actions)
        self.add(self.status_row)

        self.progress = Gtk.ProgressBar(
            show_text=True,
            margin_start=12,
            margin_end=12,
            margin_top=10,
            margin_bottom=10,
        )
        self.progress.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Finding pauses")]
        )
        self.progress_row = Gtk.ListBoxRow(
            child=self.progress, activatable=False, selectable=False, visible=False
        )
        self.add(self.progress_row)

        self.undo_row = Adw.ActionRow(visible=False)
        undo = Gtk.Button(label=_("Undo"), valign=Gtk.Align.CENTER)
        undo.connect("clicked", lambda _b: owner.undo_apply())
        self.undo_row.add_suffix(undo)
        self.add(self.undo_row)

        try:
            chosen = PAUSE_LENGTHS.index(
                float(window.app.config.get("pause_length", 1.0))
            )
        except (TypeError, ValueError):
            chosen = 1
        # Connected after showing the saved choice, which is no new choice.
        self.length_buttons[chosen].set_active(True)
        for button in self.length_buttons:
            button.connect("toggled", weak_callback(self._on_length_toggled))
        self.sync()

    def sync(self):
        """Follow the shown file: drop work and results that belong to another."""
        window = self.window()
        current = window.active_audio_id if window else None
        if current == self.source_id:
            # dpdfnet-native may have been installed since the last look.
            if self.levels is None and not self.finder.busy:
                self._show_state()
            return
        self.finder.cancel()
        self.source_id = current
        self.levels = None
        self.undo = None
        self._show_state()

    def _length(self):
        return next(b.seconds for b in self.length_buttons if b.get_active())

    def _show_state(self, name=None):
        if name is None:
            if self.source_id is None:
                name = "idle"
            elif self.finder.busy:
                name = "busy"
            elif self.levels is not None:
                name = "ready"
            elif not shutil.which("dpdfnet-enhance"):
                name = "missing"
            else:
                name = "start"
        self._failed = name == "failed"
        self.progress_row.set_visible(name == "busy")
        # Once edited, the marks are the person's; finding again marks anew.
        self.actions.set_visible(
            name in ("start", "busy", "failed")
            or (name == "ready" and bool(self.pauses) and not self._applied())
        )
        # Without a file or the denoiser, one line says why and nothing waits.
        self.length_row.set_visible(name not in ("idle", "missing"))
        self.status_row.set_subtitle("")
        if name in ("start", "ready"):
            self.actions.set_visible_child_name("find")
        if name == "start":
            self.status_row.set_title(_("Takes about a minute for each hour of audio."))
            self.status_row.set_subtitle(
                _("Made for speech. Pauses are found on the voice cleaned by DPDFNet.")
            )
        elif name == "busy":
            self.status_row.set_title(_("Finding pauses"))
            self.actions.set_visible_child_name("cancel")
        elif name == "missing":
            self.status_row.set_title(
                _("Unavailable: install {package}").format(package="dpdfnet-native")
            )
        elif name == "idle":
            self.status_row.set_title(_("Open a file to find its pauses"))
        elif name == "ready":
            self._summarize()
        self._show_undo()

    def _applied(self):
        window = self.window()
        return bool(
            window
            and self.undo
            and mark_spans(window.visualizer.get_marker_pairs())
            == mark_spans(self.undo.marks)
        )

    def _find(self):
        """The pauses at the chosen length and the speech between them."""
        self.pauses = find_pauses(self.levels, self._length())
        self.speech = find_speech(self.levels, self._length())

    def _pause_summary(self):
        count = len(self.pauses)
        removed = sum(p["stop"] - p["start"] for p in self.pauses)
        return (
            gettext.ngettext(
                "{count} pause · {duration} shorter",
                "{count} pauses · {duration} shorter",
                count,
            ).format(count=count, duration=format_time_display(removed))
            if count
            else _("No pauses found. Nothing would change.")
        )

    def _summarize(self):
        self.status_row.set_title(self._pause_summary())

    def show_on(self, tile):
        """Tell ``tile``, whose card this panel is, where the work stands."""
        self.tile = weakref.ref(tile)
        self._show_tile()

    def _show_tile(self):
        tile = self.tile() if self.tile else None
        if tile is None:
            return
        applied = self._applied()
        if self.finder.busy:
            text = self.progress.get_text() or _("{percent}%").format(percent="0")
        elif self._failed:
            text = _("Failed")
        elif applied:
            count = len(self.undo.marks)
            text = gettext.ngettext(
                "{count} segment", "{count} segments", count
            ).format(count=count)
        else:
            text = _("Off")
        tile.show_value(text, applied)
        # Like the other cuts: grayed with the reason until there is a file.
        window = self.window()
        tile.set_unavailable(
            None
            if window and window.active_audio_id is not None
            else _("Add a file first.")
        )

    def _show_undo(self):
        window = self.window()
        visible = bool(
            window and self.undo and self.undo.source_id == window.active_audio_id
        )
        self.undo_row.set_visible(visible)
        # Once marked, one row says it all: the result, what it replaced and
        # that it can be undone.
        self.status_row.set_visible(not visible)
        if visible:
            count = len(self.undo.marks)
            self.undo_row.set_title(
                gettext.ngettext(
                    "Speech marked in {count} segment",
                    "Speech marked in {count} segments",
                    count,
                ).format(count=count)
            )
            notes = [self._pause_summary()]
            replaced = len(self.undo.previous_marks)
            if replaced:
                notes.append(
                    gettext.ngettext(
                        "This replaces the {count} segment already marked on the file.",
                        "This replaces the {count} segments already marked on the file.",
                        replaced,
                    ).format(count=replaced)
                )
            notes.append(_("Nothing is deleted from the original file."))
            self.undo_row.set_subtitle("\n".join(notes))
        self._show_tile()

    def markers_changed(self, markers):
        """An edit after marking makes the earlier marks no longer undoable."""
        if self.undo and mark_spans(markers) != mark_spans(self.undo.marks):
            self.undo = None
            if self.levels is not None and not self.finder.busy:
                self._show_state()
            else:
                self._show_undo()

    def _on_find(self):
        window = self.window()
        if window is None or window.active_audio_id is None:
            return
        try:
            source = MediaSource.resolve(
                window.active_audio_id, window.file_queue.track_metadata
            )
            key = PauseFinder.identity(source)
        except (MediaError, OSError) as exc:
            self._fail(str(exc))
            return
        self.levels = self.finder.cached(key)
        if self.levels is not None:
            self._find()
            self._mark()
            return
        denoiser = shutil.which("dpdfnet-enhance")
        if not denoiser:
            self._show_state("missing")
            return
        self.progress.set_fraction(0)
        self.progress.set_text(_("{percent}%").format(percent="0"))
        self.finder.start(
            key,
            source,
            window.converter.ffmpeg_path,
            denoiser,
            weak_callback(self._on_progress),
            weak_callback(self._on_found),
            weak_callback(self._fail),
        )
        self._show_state("busy")

    def _on_progress(self, fraction):
        self.progress.set_fraction(fraction)
        percent = f"{fraction * 100:.0f}"
        self.progress.set_text(_("{percent}%").format(percent=percent))
        self.status_row.set_title(
            _("Finding pauses: {percent}%").format(percent=percent)
        )
        self._show_tile()

    def _on_found(self, _key, levels):
        self.levels = levels
        self._find()
        self._mark()

    def _fail(self, message):
        self._show_state("failed")
        self.status_row.set_title(message or _("The pauses could not be measured."))
        self.actions.set_visible_child_name("retry")
        self._show_tile()

    def _on_cancel(self):
        self.finder.cancel()
        self._show_state()

    def _on_length_toggled(self, button):
        if not button.get_active():
            return
        window = self.window()
        if window:
            window.app.config.set("pause_length", button.seconds)
        if self.levels is not None:
            self._find()
            self._mark()

    def _mark(self):
        """Mark the speech found, kept in order and joined: the pauses go.

        A new length replaces the marks the last one made, not the person's.
        """
        window = self.window()
        if window is None:
            return
        if self._applied():
            self.undo_apply()
        if not self.pauses:
            self._show_state()
            return
        visualizer = window.visualizer
        previous = [dict(m) for m in visualizer.get_marker_pairs()]
        remove = window.removes_marked()
        order = window.cut_order_row.get_selected()
        output = window.cut_output_row.get_selected()
        self.undo = None
        if not visualizer.restore_markers(self.speech):
            window._show_message(
                _("The pauses could not be marked"),
                _("The file changed after it was measured. Find the pauses again."),
            )
            self._show_state()
            return
        # The marks as validated: the last part ends at the file's end.
        self.undo = Applied(
            window.active_audio_id,
            previous,
            remove,
            order,
            output,
            visualizer.get_marker_pairs(),
        )
        # Kept in timeline order and joined: the speech without its pauses.
        window.set_removes_marked(False)
        window.cut_order_row.set_selected(0)
        window.cut_output_row.set_selected(1)
        self._show_state()

    def undo_apply(self):
        window = self.window()
        if (
            window is None
            or not self.undo
            or self.undo.source_id != window.active_audio_id
        ):
            return
        applied, self.undo = self.undo, None
        window.visualizer.restore_markers(applied.previous_marks)
        window.set_removes_marked(applied.previous_remove)
        window.cut_order_row.set_selected(applied.previous_order)
        window.cut_output_row.set_selected(applied.previous_output)
        self._show_state()

    def cleanup(self):
        self.finder.cleanup()
