"""Conversion in the queue: progress on its rows, the outcome in a banner."""

import gettext
import weakref
from copy import deepcopy

from gi.repository import Gio, Gtk

from app.audio.process import MediaError
from app.main_loop import MainLoopSources

_ = gettext.gettext


class BatchRunner:
    """One batch at a time; the queue and settings stay locked while it runs."""

    def __init__(self, window):
        self.window = weakref.ref(window)
        self.converter = window.converter
        self.sources = MainLoopSources()
        self.closed = False
        self.files = ()
        self.last_batch = None
        self._inhibit_cookie = 0
        self._banner_action = None

    def start(self, files=None):
        window = self.window()
        if self.closed or window is None or self.converter.busy:
            return
        files = tuple(files if files is not None else window.file_queue.get_files())
        try:
            settings = deepcopy(window._collect_conversion_settings())
        except (MediaError, ValueError, TypeError) as exc:
            window._show_message(_("Check conversion settings"), str(exc))
            return
        self.files = files
        window.banner.set_revealed(False)
        for row in window.file_queue.file_rows:
            row.set_result(None)
        try:
            self.converter.start_batch(
                files, settings, self._progress_received, self._finished
            )
        except (MediaError, RuntimeError) as exc:
            window._show_message(_("Conversion could not start"), str(exc))
            return
        self._show_running(window, True)
        self._show_progress(0, files[0], 0.0)
        app = window.get_application()
        self._inhibit_cookie = app.inhibit(
            window,
            Gtk.ApplicationInhibitFlags.SUSPEND | Gtk.ApplicationInhibitFlags.IDLE,
            _("Converting audio"),
        )

    def _show_running(self, window, running):
        """Settings and queue edits wait; listening does not."""
        window.options_scroll.set_sensitive(not running)
        window.destination_bar.set_sensitive(not running)
        if running:
            window.effect_cards.close()
        window.file_queue.set_locked(running)
        # Clearing comes back with the queue summary once the batch ends.
        for name in ("add-files", "clear-queue", "reset-settings"):
            window.lookup_action(name).set_enabled(not running)
        button = window.convert_button
        button.set_sensitive(True)
        if running:
            button.set_action_name("win.cancel-conversion")
            button.set_label(_("Cancel"))
            button.remove_css_class("suggested-action")
        else:
            button.set_action_name("win.convert")
            button.add_css_class("suggested-action")
            window._update_convert_label()
            window._sync_convert_action()

    def cancel(self):
        window = self.window()
        if self.closed or window is None or not self.converter.busy:
            return
        self.converter.cancel_conversion()
        window.convert_button.set_label(_("Cancelling…"))
        window.convert_button.set_sensitive(False)

    def _progress_received(self, index, path, fraction):
        # Called by the conversion worker. No widget reads, even for file count.
        self.sources.later(
            80, self._show_progress, index, path, fraction, key="progress"
        )

    def _show_progress(self, index, path, fraction):
        window = self.window()
        if self.closed or window is None or not self.converter.busy:
            return
        total = len(self.files)
        overall = max(0.0, min(1.0, (index + fraction) / total))
        window.queue_label.set_label(
            _("Converting {current} of {total} · {percent}%").format(
                current=index + 1, total=total, percent=int(overall * 100)
            )
        )
        window.queue_label.set_visible(True)
        if path in window.file_queue.files:
            window.file_queue.update_progress(
                window.file_queue.files.index(path), fraction
            )

    def _finished(self):
        window = self.window()
        if self.closed or window is None:
            return
        self.sources.clear()
        self._uninhibit(window)
        self.last_batch = deepcopy(self.converter.last_batch_result)
        queue = window.file_queue
        for row in queue.file_rows:
            row.progress_bar.set_visible(False)
        for item in self.last_batch.items:
            if item.source in queue.files:
                queue.file_rows[queue.files.index(item.source)].set_result(item)
        self._show_running(window, False)
        window._show_queue_summary(len(queue.files))
        self._show_outcome(window)

    def _show_outcome(self, window):
        """Inputs stay queued, so a batch can be repeated or retried."""
        items = self.last_batch.items
        unfinished = tuple(item.source for item in items if not item.successful)
        outputs = [output for item in items for output in item.outputs]
        if unfinished:
            title = gettext.ngettext(
                "{count} file was not converted",
                "{count} files were not converted",
                len(unfinished),
            ).format(count=len(unfinished))
            button, self._banner_action = _("Try Again"), ("retry", unfinished)
        else:
            title = gettext.ngettext(
                "{count} file converted", "{count} files converted", len(items)
            ).format(count=len(items))
            button = _("Show Files") if outputs else ""
            self._banner_action = ("reveal", outputs[0]) if outputs else None
        window.banner.set_title(title)
        window.banner.set_button_label(button)
        window.banner.set_revealed(True)
        if not window.is_active():
            notification = Gio.Notification.new(title)
            window.get_application().send_notification("conversion", notification)

    def banner_activated(self):
        if self._banner_action is None:
            return
        kind, value = self._banner_action
        if kind == "retry":
            # With the current settings and marks: the message may have asked
            # for a change. Files removed since then stay out.
            queued = self.window().file_queue.files
            remaining = tuple(path for path in value if path in queued)
            if remaining:
                self.start(remaining)
            else:
                self.window().banner.set_revealed(False)
        else:
            self.window().file_queue.reveal(value)

    def _uninhibit(self, window):
        if self._inhibit_cookie:
            window.get_application().uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0

    def cleanup(self):
        self.closed = True
        self.sources.close()
        self.converter.cancel_conversion()
        window = self.window()
        if window is not None:
            self._uninhibit(window)
