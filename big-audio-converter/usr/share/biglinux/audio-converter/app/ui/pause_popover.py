"""Find the pauses of the shown file, preview them and mark them for removal."""

import gettext
import shutil
import weakref
from typing import NamedTuple

from gi.repository import Adw, Gtk

from app.audio.media import MediaSource
from app.audio.pauses import PAUSE_LENGTHS, PauseFinder, find_pauses
from app.audio.process import MediaError
from app.utils.main_loop import weak_callback
from app.utils.time_formatter import format_time_display

_ = gettext.gettext


class Applied(NamedTuple):
    """What applying replaced, kept until the file or its marks change."""

    source_id: str
    previous_marks: list
    previous_remove: bool
    marks: list


class PausePopover(Gtk.Popover):
    def __init__(self, window):
        super().__init__()
        self.window = weakref.ref(window)
        self.finder = PauseFinder()
        self.source_id = None  # queue entry the analysis or levels belong to
        self.levels = None
        self.pauses = []
        self.undo = None  # Applied

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            width_request=320,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        box.append(
            Gtk.Label(label=_("Remove pauses"), xalign=0, css_classes=["heading"])
        )
        box.append(
            Gtk.Label(
                label=_(
                    "Made for speech. Pauses are found on the voice cleaned by DPDFNet."
                ),
                xalign=0,
                wrap=True,
                max_width_chars=40,
                css_classes=["dim-label"],
            )
        )
        self.stack = Gtk.Stack(vhomogeneous=False)
        box.append(self.stack)

        self.stack.add_named(
            Gtk.Label(
                label=_("Unavailable: install {package}").format(
                    package="dpdfnet-native"
                ),
                xalign=0,
                wrap=True,
            ),
            "missing",
        )

        start = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        start.append(
            Gtk.Label(
                label=_("Takes about a minute for each hour of audio."),
                xalign=0,
                wrap=True,
            )
        )
        find = Gtk.Button(label=_("Find Pauses"), css_classes=["suggested-action"])
        find.connect("clicked", weak_callback(self._on_find))
        start.append(find)
        self.stack.add_named(start, "start")

        busy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Finding pauses")]
        )
        busy.append(self.progress)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", weak_callback(self._on_cancel))
        busy.append(cancel)
        self.stack.add_named(busy, "busy")

        failed = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.error = Gtk.Label(xalign=0, wrap=True, max_width_chars=40)
        failed.append(self.error)
        retry = Gtk.Button(label=_("Try Again"))
        retry.connect("clicked", weak_callback(self._on_find))
        failed.append(retry)
        self.stack.add_named(failed, "failed")

        ready = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        length_label = Gtk.Label(label=_("Pauses longer than"), xalign=0)
        ready.append(length_label)
        lengths = Gtk.Box(css_classes=["linked"], homogeneous=True)
        lengths.set_accessible_role(Gtk.AccessibleRole.GROUP)
        lengths.update_property(
            [Gtk.AccessibleProperty.LABEL], [length_label.get_label()]
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
        ready.append(lengths)
        self.summary = Gtk.Label(xalign=0, wrap=True)
        ready.append(self.summary)
        self.apply_button = Gtk.Button(
            label=_("Mark for Removal"), css_classes=["suggested-action"]
        )
        self.apply_button.connect("clicked", weak_callback(self._on_apply))
        ready.append(self.apply_button)
        self.stack.add_named(ready, "ready")

        self.undo_box = Gtk.Box(spacing=12, visible=False)
        self.undo_label = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.undo_box.append(self.undo_label)
        undo = Gtk.Button(label=_("Undo"))
        undo.connect("clicked", weak_callback(self.undo_apply))
        self.undo_box.append(undo)
        box.append(self.undo_box)
        self.set_child(box)

        # The menu button shows a spinner in place of its icon while working.
        self.icon = Gtk.Stack()
        self.icon.add_named(Gtk.Image(icon_name="edit-cut-symbolic"), "idle")
        self.icon.add_named(Gtk.Spinner(spinning=True), "busy")
        content = Gtk.Box(spacing=6)
        content.append(self.icon)
        content.append(Gtk.Label(label=_("Remove pauses")))
        self.button = Gtk.MenuButton(
            child=content,
            popover=self,
            direction=Gtk.ArrowType.UP,
            css_classes=["waveform-tool"],
            halign=Gtk.Align.END,
            valign=Gtk.Align.START,
            margin_top=6,
            margin_end=6,
        )
        self.button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Remove pauses")]
        )

        config = window.app.config
        try:
            chosen = PAUSE_LENGTHS.index(float(config.get("pause_length", 1.0)))
        except (TypeError, ValueError):
            chosen = 1
        # Connected after showing the saved choice, which is no new choice.
        self.length_buttons[chosen].set_active(True)
        for button in self.length_buttons:
            button.connect("toggled", weak_callback(self._on_length_toggled))
        self.connect("show", weak_callback(self._on_show))
        self.connect("closed", weak_callback(self._on_closed))
        self.sync()

    def sync(self):
        """Follow the shown file: drop work and results that belong to another."""
        window = self.window()
        current = window.active_audio_id if window else None
        self.button.set_sensitive(current is not None)
        self.button.set_tooltip_text(
            None if current else _("Open a file to find its pauses")
        )
        if current == self.source_id:
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
            if self.finder.busy:
                name = "busy"
            elif self.levels is not None:
                name = "ready"
            elif not shutil.which("dpdfnet-enhance"):
                name = "missing"
            else:
                name = "start"
        self.stack.set_visible_child_name(name)
        self.icon.set_visible_child_name("busy" if name == "busy" else "idle")
        if name != "busy":
            self.button.update_property([Gtk.AccessibleProperty.DESCRIPTION], [""])
        if name == "ready":
            self._find()
        self._show_undo()

    def _find(self):
        """Pauses at the chosen length, previewed on the waveform while open."""
        self.pauses = find_pauses(self.levels, self._length())
        count = len(self.pauses)
        removed = sum(p["stop"] - p["start"] for p in self.pauses)
        summary = (
            gettext.ngettext(
                "{count} pause · {duration} shorter",
                "{count} pauses · {duration} shorter",
                count,
            ).format(count=count, duration=format_time_display(removed))
            if count
            else _("No pauses found. Nothing would change.")
        )
        window = self.window()
        existing = len(window.visualizer.get_marker_pairs()) if window else 0
        if count and existing:
            summary += "\n" + gettext.ngettext(
                "This replaces the {count} mark already on the file.",
                "This replaces the {count} marks already on the file.",
                existing,
            ).format(count=existing)
        self.summary.set_label(summary)
        self.apply_button.set_sensitive(bool(count))
        self._preview(self.pauses if self.get_visible() else [])

    def _preview(self, pauses):
        window = self.window()
        if window:
            window.visualizer.proposed_pairs = pauses
            window.visualizer.queue_draw()

    def _show_undo(self):
        window = self.window()
        visible = bool(
            window and self.undo and self.undo.source_id == window.active_audio_id
        )
        self.undo_box.set_visible(visible)
        if visible:
            count = len(self.undo.marks)
            self.undo_label.set_label(
                gettext.ngettext(
                    "{count} pause marked", "{count} pauses marked", count
                ).format(count=count)
            )

    def markers_changed(self, markers):
        """An edit after applying makes the earlier marks no longer undoable."""
        if self.undo and [(m["start"], m["stop"]) for m in markers] != [
            (m["start"], m["stop"]) for m in self.undo.marks
        ]:
            self.undo = None
            self._show_undo()

    def _on_find(self, _button):
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
            self._show_state()
            return
        denoiser = shutil.which("dpdfnet-enhance")
        if not denoiser:
            self._show_state("missing")
            return
        self.progress.set_fraction(0)
        self.progress.set_text(None)
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
        self.button.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION],
            [_("Finding pauses: {percent}%").format(percent=percent)],
        )

    def _on_found(self, _key, levels):
        self.levels = levels
        self._show_state()
        window = self.window()
        if window and not self.get_visible():
            # The analysis ran in the background: say so where it can be reopened.
            toast = Adw.Toast(title=_("Pauses found"), button_label=_("Review"))
            toast.connect("button-clicked", weak_callback(self._review))
            window.toast_overlay.add_toast(toast)

    def _review(self, _toast):
        self.button.popup()

    def _fail(self, message):
        self.error.set_label(message or _("The pauses could not be measured."))
        self._show_state("failed")

    def _on_cancel(self, _button):
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

    def _on_show(self, _popover):
        # dpdfnet-native may have been installed while the window was open.
        if self.stack.get_visible_child_name() == "missing":
            self._show_state()
        if self.stack.get_visible_child_name() == "ready":
            self._preview(self.pauses)

    def _on_closed(self, _popover):
        self._preview([])

    def _on_apply(self, _button):
        window = self.window()
        if window is None or not self.pauses:
            return
        visualizer = window.visualizer
        previous = [dict(m) for m in visualizer.get_marker_pairs()]
        remove = window.removes_marked()
        self.undo = None
        self._preview([])
        if not visualizer.restore_markers(self.pauses):
            self.popdown()
            window._show_message(
                _("The pauses could not be marked"),
                _("The file changed after it was measured. Find the pauses again."),
            )
            return
        # The marks as validated: the last pause ends at the file's end.
        self.undo = Applied(
            window.active_audio_id, previous, remove, visualizer.get_marker_pairs()
        )
        window.set_removes_marked(True)
        count = len(self.pauses)
        toast = Adw.Toast(
            title=gettext.ngettext(
                "{count} pause marked for removal",
                "{count} pauses marked for removal",
                count,
            ).format(count=count),
            button_label=_("Undo"),
        )
        toast.connect("button-clicked", weak_callback(self.undo_apply))
        window.toast_overlay.add_toast(toast)
        self._show_undo()
        self.popdown()

    def undo_apply(self, *_args):
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
        self._show_undo()
        self.popdown()

    def cleanup(self):
        self.finder.cleanup()
