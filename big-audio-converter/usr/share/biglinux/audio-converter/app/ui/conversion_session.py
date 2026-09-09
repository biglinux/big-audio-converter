"""One conversion session, with explicit worker and main-context ownership."""

import copy
import gettext
from pathlib import Path
import threading
import weakref

from gi.repository import Adw, Gio, Gtk
from app.utils.main_context import SourceGroup

_ = gettext.gettext


class ConversionSession:
    """Keep a stable request snapshot until the worker acknowledges completion."""

    def __init__(self, window):
        self.window = weakref.ref(window)
        self.converter = window.converter
        self.sources = SourceGroup()
        self.worker = None
        self.files = ()
        self.settings = {}
        self.dialog = None
        self.closing = False
        self.disposed = False
        self.finished = False

    @property
    def running(self):
        return self.worker is not None and self.worker.is_alive()

    def start(self, files, settings):
        window = self.window()
        if self.running or self.disposed or window is None:
            return False
        self.files = tuple(files)
        if not self.files:
            return False
        self.settings = copy.deepcopy(settings)
        self.finished = False
        self.converter.reset_cancellation()
        self.bar = Gtk.ProgressBar(show_text=True)
        self.bar.update_property([Gtk.AccessibleProperty.LABEL], [_("Conversion progress")])
        self.dialog = Adw.AlertDialog(heading=_("Converting Files"), body=_("Preparing audio…"))
        self.dialog.set_extra_child(self.bar)
        self.dialog.add_response("cancel", _("Cancel"))
        self.dialog.connect("response", lambda dialog, response: self.cancel() if not self.finished else None)
        self.dialog.present(window)
        window.header_bar.convert_button.set_sensitive(False)
        self.worker = threading.Thread(target=self._run, name="bac-conversion", daemon=False)
        self.worker.start()
        self.sources.timeout(40, self._poll)
        return True

    def _run(self):
        self.converter.convert_all_files(self.files, self.settings, self._progress, None, reset=False)

    def _progress(self, index, identifier, value):
        window = self.window()
        if self.disposed or self.closing or window is None:
            return
        fraction = min(1.0, max(0.0, (index + value) / len(self.files)))
        if self.dialog is not None:
            self.bar.set_fraction(fraction)
            self.bar.set_text(_("{percent}%").format(percent=int(fraction * 100)))
            self.dialog.set_body(_("File {current} of {total}: {name}").format(
                current=index + 1, total=len(self.files), name=Path(identifier).name))
        # The queue can be reordered while encoding. Never reuse an old index.
        current_files = window.file_queue.get_files()
        if identifier in current_files:
            window.file_queue.update_progress(current_files.index(identifier), value)

    def _poll(self):
        if self.running:
            return True
        window = self.window()
        if self.worker is not None:
            self.worker.join()
        self.finished = True
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None
        if self.disposed or window is None:
            return False
        window.header_bar.convert_button.set_sensitive(True)
        if self.closing:
            window.close()
            return False
        self._show_results(window)
        return False

    def _show_results(self, window):
        result = self.converter.last_batch
        completed = len(result.successful_sources)
        failed = len(result.failed_sources)
        cancelled = sum(item.status == "cancelled" for item in result.files)
        dialog = Adw.AlertDialog(heading=_("Conversion Results"),
            body=_("{completed} completed · {failed} failed · {cancelled} cancelled").format(
                completed=completed, failed=failed, cancelled=cancelled))
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for item in result.files:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            title = Gtk.Label(label=Path(item.source).name, xalign=0, selectable=True, wrap=True)
            title.add_css_class("heading")
            box.append(title)
            if item.status == "success":
                for path in item.outputs:
                    label = Gtk.Label(label=path, xalign=0, selectable=True, wrap=True)
                    box.append(label)
                    button = Gtk.Button(label=_("Open Containing Folder"), halign=Gtk.Align.START)
                    button.connect("clicked", lambda button, path=path: self._reveal(path))
                    box.append(button)
            elif item.status == "cancelled":
                box.append(Gtk.Label(label=_("Cancelled. Unfinished output was not published."), xalign=0, wrap=True))
            else:
                box.append(Gtk.Label(label=_("This file could not be converted. Other files were processed independently."), xalign=0, wrap=True))
                box.append(Gtk.Label(label=_("Check the input, selected format, folder permissions and free disk space, then retry."), xalign=0, wrap=True))
                details = Gtk.Expander(label=_("Technical details"))
                details.set_child(Gtk.Label(label=item.message + "\n" + item.diagnostics,
                                            selectable=True, xalign=0, wrap=True))
                box.append(details)
            for warning in item.warnings:
                box.append(Gtk.Label(label=warning, xalign=0, wrap=True))
            content.append(box)
        scrolled = Gtk.ScrolledWindow(min_content_height=160, max_content_height=350,
                                      propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_child(content)
        dialog.set_extra_child(scrolled)
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        if failed:
            dialog.add_response("retry", _("Retry Failed Files"))
            dialog.set_response_appearance("retry", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda dialog, response: self.start(result.failed_sources, self.settings) if response == "retry" else None)
        dialog.present(window)

    def _reveal(self, path):
        window = self.window()
        if window is None or self.disposed:
            return
        file = Gio.File.new_for_path(str(Path(path).parent))
        try:
            Gio.AppInfo.launch_default_for_uri(file.get_uri(), None)
        except Exception as error:
            window._show_error_dialog(_("Could not open the folder"), str(error))

    def cancel(self):
        if self.running:
            self.converter.cancel_conversion()

    def request_close(self):
        self.closing = True
        self.cancel()
        return self.running

    def close(self):
        self.disposed = True
        self.cancel()
        if self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(timeout=2)
        self.sources.close()
        self.finished = True
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None
