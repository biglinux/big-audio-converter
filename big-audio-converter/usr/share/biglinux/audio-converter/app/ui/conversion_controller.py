"""Conversion presentation: immutable requests, cancellable jobs, real outputs."""
import gettext
import os
import weakref
from copy import deepcopy

from gi.repository import Adw, Gio, GLib, Gtk

from app.audio.media import source_label
from app.audio.process import MediaError
from app.utils.main_loop import MainLoopSources

_ = gettext.gettext


class ConversionController:
    """Keep batch presentation separate from the main window's layout."""

    def __init__(self, window):
        self.window = weakref.ref(window)
        self.converter = window.converter
        self.sources = MainLoopSources()
        self.closed = False
        self.dialog = None
        self.result_dialog = None
        self.files = ()
        self.settings = None
        self.last_batch = None

    def start(self, files=None, settings=None):
        window = self.window()
        if self.closed or window is None or self.converter.busy:
            return
        if window.file_queue.pending:
            window._show_info_dialog(_("Inspecting audio"), _("Files are still being inspected. Conversion will be available when inspection finishes."))
            return
        files = tuple(files if files is not None else window.file_queue.get_files())
        if not files:
            window._show_info_dialog(_("No files to convert"), _("Please add at least one file to convert."))
            return
        self.files = files
        try:
            self.settings = deepcopy(settings if settings is not None else window._collect_conversion_settings())
        except (MediaError, ValueError, TypeError) as exc:
            window._show_error_dialog(_("Check conversion settings"), str(exc))
            return
        if self.result_dialog is not None:
            self.result_dialog.close()
            self.result_dialog = None
        self.dialog = Adw.Dialog(title=_("Converting Files"), content_width=420, can_close=False)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False))
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                          margin_start=24, margin_end=24, margin_top=18, margin_bottom=24)
        self.status = Gtk.Label(label=_("Preparing conversion…"), wrap=True, xalign=0)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.update_property([Gtk.AccessibleProperty.LABEL], [_("Overall conversion progress")])
        self.cancel_button = Gtk.Button(label=_("Cancel"), halign=Gtk.Align.END)
        self.cancel_button.connect("clicked", self.cancel)
        content.append(self.status)
        content.append(self.progress)
        content.append(self.cancel_button)
        toolbar.set_content(content)
        self.dialog.set_child(toolbar)
        window.convert_button.set_sensitive(False)
        self.dialog.present(window)
        try:
            self.converter.start_batch(files, self.settings, self._progress_received, self._finished)
        except (MediaError, RuntimeError) as exc:
            self.dialog.set_can_close(True)
            self.dialog.close()
            self.dialog = None
            window.convert_button.set_sensitive(True)
            window._show_error_dialog(_("Conversion could not start"), str(exc))

    def cancel(self, *_args):
        if self.closed or not self.converter.busy:
            return
        self.converter.cancel_conversion()
        self.cancel_button.set_sensitive(False)
        self.cancel_button.set_label(_("Cancelling…"))
        self.status.set_text(_("Stopping the current operation and removing incomplete outputs…"))

    def _progress_received(self, index, path, fraction):
        # Called by the conversion worker. No widget reads, even for file count.
        self.sources.later(80, self._show_progress, index, path, fraction, key="progress")

    def _show_progress(self, index, path, fraction):
        window = self.window()
        if self.closed or window is None or self.dialog is None:
            return
        total = len(self.files)
        value = max(0.0, min(1.0, (index + fraction) / total))
        self.progress.set_fraction(value)
        if self.cancel_button.get_sensitive():
            self.status.set_text(_("File {current} of {total}\n{name}").format(
                current=index + 1, total=total, name=source_label(path, self.settings.get("track_metadata"))))
        if path in window.file_queue.files:
            window.file_queue.update_progress(window.file_queue.files.index(path), fraction)

    def _finished(self, _success, _message, _successful_sources):
        if self.closed:
            return
        window = self.window()
        if window is None:
            return
        self.sources.clear()
        if self.dialog:
            self.dialog.set_can_close(True)
            self.dialog.close()
            self.dialog = None
        window.convert_button.set_sensitive(not window.file_queue.pending)
        self.last_batch = deepcopy(self.converter.last_batch_result)
        for item in self.last_batch.items:
            if item.source in window.file_queue.files:
                row = window.file_queue.file_rows[window.file_queue.files.index(item.source)]
                row.progress_bar.set_visible(False)
                status = {"success": _("Converted"), "failed": _("Failed"), "cancelled": _("Cancelled")}[item.status]
                row.set_metadata(GLib.markup_escape_text(status + (" — " + item.message if item.message else "")))
        # Keep inputs and markers in the queue so the operation can be repeated.
        self.show_results()

    def show_results(self):
        window = self.window()
        if self.closed or window is None or self.last_batch is None:
            return
        batch = self.last_batch
        successes = sum(item.successful for item in batch.items)
        failures = sum(item.status == "failed" for item in batch.items)
        cancelled = sum(item.status == "cancelled" for item in batch.items)
        dialog = self.result_dialog = Adw.Dialog(title=_("Conversion Results"), content_width=600, content_height=520)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_start=18, margin_end=18, margin_top=12, margin_bottom=18)
        summary = _("Converted: {success} · Failed: {failed} · Cancelled: {cancelled}").format(
            success=successes, failed=failures, cancelled=cancelled)
        box.append(Gtk.Label(label=summary, wrap=True, xalign=0))
        box.append(Gtk.Label(label=_("Existing files were not replaced. Inputs remain in the queue."), wrap=True, xalign=0))
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        entries = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scrolled.set_child(entries)
        box.append(scrolled)
        for item in batch.items:
            group = Adw.PreferencesGroup(title=GLib.markup_escape_text(source_label(item.source, self.settings.get("track_metadata"))))
            status = {"success": _("Converted"), "failed": _("Failed"), "cancelled": _("Cancelled")}[item.status]
            message = Gtk.Label(label=status + (": " + item.message if item.message else ""), wrap=True, xalign=0, selectable=True)
            group.add(message)
            for warning in item.warnings:
                group.add(Gtk.Label(label=warning, wrap=True, xalign=0))
            for output in item.outputs:
                row = Adw.ActionRow(title=GLib.markup_escape_text(os.path.basename(output)),
                                    subtitle=GLib.markup_escape_text(os.path.dirname(output)))
                reveal = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                    tooltip_text=_("Show output file"))
                reveal.update_property([Gtk.AccessibleProperty.LABEL], [_("Show output file")])
                reveal.connect("clicked", lambda button, path=output: self._reveal(path))
                row.add_suffix(reveal)
                group.add(row)
            if item.details:
                details = Gtk.Expander(label=_("Technical details"))
                text = Gtk.Label(label=item.details[-16384:], selectable=True, wrap=True, xalign=0)
                details.set_child(text)
                group.add(details)
            entries.append(group)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        retry_files = tuple(item.source for item in batch.items if item.status in ("failed", "cancelled"))
        if retry_files:
            retry = Gtk.Button(label=_("Retry unfinished files"))
            retry.connect("clicked", lambda button: self.start(retry_files, self.settings))
            buttons.append(retry)
        close = Gtk.Button(label=_("Close"))
        close.connect("clicked", lambda button: dialog.close())
        buttons.append(close)
        box.append(buttons)
        toolbar.set_content(box)
        dialog.set_child(toolbar)
        dialog.present(window)

    def _reveal(self, path):
        window = self.window()
        if self.closed or window is None:
            return
        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(path))

        def finished(source, result):
            try:
                source.open_containing_folder_finish(result)
            except GLib.Error:
                if not self.closed:
                    window._show_error_dialog(_("Folder could not be opened"), _("Open your file manager and navigate to: {path}").format(path=os.path.dirname(path)))

        launcher.open_containing_folder(window, None, finished)

    def cleanup(self):
        self.closed = True
        self.sources.close()
        self.converter.cancel_conversion()
        for dialog in (self.dialog, self.result_dialog):
            if dialog:
                dialog.set_can_close(True)
                dialog.close()
        self.dialog = self.result_dialog = None
