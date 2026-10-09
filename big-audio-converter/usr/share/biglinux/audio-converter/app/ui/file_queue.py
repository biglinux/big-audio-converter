"""The queue of files to convert and their probes."""

import gettext
import logging
import os
import time
import weakref
from collections import deque
from contextlib import contextmanager
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from app.audio.media import audio_duration, audio_stream, track_identifier
from app.audio.probe_service import ProbeService
from app.audio.process import MediaError
from app.audio.profiles import copy_container
from app.ui.file_info import FileInfoWindow
from app.utils.main_loop import MainLoopSources, weak_callback
from app.utils.time_formatter import format_time_display

_ = gettext.gettext
logger = logging.getLogger(__name__)


def open_containing_folder(widget, path):
    """Show `path` in the file manager, or say where it is if none opens."""
    root = widget.get_root()

    def opened(launcher, result):
        try:
            launcher.open_containing_folder_finish(result)
        except GLib.Error as exc:
            logger.warning("The containing folder could not be opened: %s", exc.message)
            dialog = Adw.AlertDialog(
                heading=_("Folder could not be opened"),
                body=_("Open your file manager and navigate to: {path}").format(
                    path=os.path.dirname(path)
                ),
            )
            dialog.add_response("close", _("Close"))
            dialog.present(root)

    launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(path))
    launcher.open_containing_folder(root, None, opened)


def _result_details(item):
    """A status button whose popover says what went wrong and why."""
    status = {
        "success": _("Converted with warnings"),
        "failed": _("Failed"),
        "cancelled": _("Cancelled"),
    }[item.status]
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=8,
        margin_start=12,
        margin_end=12,
        margin_top=12,
        margin_bottom=12,
    )
    heading = Gtk.Label(label=status, xalign=0, css_classes=["heading"])
    box.append(heading)
    for text in (item.message, *item.warnings):
        if text:
            # Not selectable: a popover would focus and select it on opening.
            box.append(Gtk.Label(label=text, wrap=True, xalign=0, max_width_chars=50))
    technical = "\n\n".join((*item.notes, item.details[-16384:])).strip()
    if technical:
        details = Gtk.Expander(label=_("Technical details"))
        details.set_child(
            Gtk.ScrolledWindow(
                child=Gtk.Label(
                    label=technical,
                    wrap=True,
                    xalign=0,
                    selectable=True,
                    max_width_chars=50,
                    css_classes=["caption", "monospace"],
                ),
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                propagate_natural_height=True,
                max_content_height=240,
            )
        )
        box.append(details)
    failed = item.status == "failed"
    button = Gtk.MenuButton(
        icon_name="dialog-error-symbolic" if failed else "dialog-warning-symbolic",
        tooltip_text=status,
        valign=Gtk.Align.CENTER,
        css_classes=["flat", "circular", "error" if failed else "warning"],
        popover=Gtk.Popover(child=box),
    )
    button.update_property([Gtk.AccessibleProperty.LABEL], [status])
    return button


class FileQueueRow(Adw.ActionRow):
    """Row representing a file in the queue using Adwaita ActionRow."""

    def __init__(
        self,
        file_path,
        index,
        on_remove_callback,
        on_play_callback,
        on_delete_callback,
        on_activate_callback=None,
    ):
        super().__init__()

        owner = weakref.proxy(self)
        self.file_path = file_path
        self.source_path = file_path
        self.stream_index = None
        self.duration = None  # seconds, once probed
        self.probe_service = None
        self._info_dialogs = []
        self.index = index
        self.on_remove_callback = on_remove_callback
        self.on_play_callback = on_play_callback
        self.on_delete_callback = on_delete_callback
        self.on_activate_callback = on_activate_callback

        # Set title to filename (escape special characters for Pango markup)
        filename = os.path.basename(file_path)
        self.set_title(GLib.markup_escape_text(filename))

        self._details = ""
        self._cut_count = 0

        # Make row activatable
        self.set_activatable(True)

        if on_activate_callback:
            self.connect(
                "activated",
                lambda row: row.on_activate_callback(row.file_path, row.index),
            )

        # The row's own play button, a round badge leading the row.
        self.play_button = Gtk.Button(
            valign=Gtk.Align.CENTER, css_classes=["circular", "queue-badge"]
        )
        self.show_playing(False)
        self.play_button.connect(
            "clicked", lambda btn: owner.on_play_callback(owner.file_path, owner.index)
        )
        self.add_prefix(self.play_button)

        # Progress bar (right side)
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_visible(False)
        self.progress_bar.add_css_class("file-progress")
        self.add_suffix(self.progress_bar)
        # The last conversion's outcome, filled by set_result.
        self.result_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.add_suffix(self.result_box)

        # Only the entry: the menu's "Move to Trash" is the one that deletes.
        self.remove_button = Gtk.Button(
            icon_name="window-close-symbolic",
            tooltip_text=_("Remove from queue"),
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular"],
        )
        self.remove_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Remove from queue")]
        )
        self.remove_button.connect(
            "clicked", lambda btn: owner.on_remove_callback(owner.index)
        )
        self.add_suffix(self.remove_button)

        self.more_button = Gtk.MenuButton(icon_name="view-more-symbolic")
        self.more_button.add_css_class("flat")
        self.more_button.set_valign(Gtk.Align.CENTER)
        self.more_button.set_tooltip_text(_("File actions"))
        self.more_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("File actions")]
        )
        self.more_button.set_create_popup_func(
            lambda button: owner._setup_context_menu(button)
        )
        self.add_suffix(self.more_button)
        right_click = Gtk.GestureClick.new()
        right_click.set_button(3)
        right_click.connect(
            "pressed", lambda gesture, *_: gesture.get_widget().more_button.popup()
        )
        self.add_controller(right_click)

    def _setup_context_menu(self, button, *_args):
        """Allocate a row's menu only when it is opened, including by keyboard."""
        owner = weakref.proxy(self)
        menu = Gtk.PopoverMenu()
        menu_model = Gio.Menu()

        # Delete file from filesystem action
        menu_model.append(_("Move to Trash"), "row.delete")

        # Open containing folder action
        menu_model.append(_("Open Containing Folder"), "row.open_folder")

        # More information action
        menu_model.append(_("More Information..."), "row.info")

        menu.set_menu_model(menu_model)

        action_group = Gio.SimpleActionGroup()

        # Delete action (with confirmation)
        delete_action = Gio.SimpleAction.new("delete", None)
        delete_action.connect(
            "activate",
            lambda a, p: owner.on_delete_callback(owner.index, owner.file_path),
        )
        action_group.add_action(delete_action)

        # Open folder action
        open_folder_action = Gio.SimpleAction.new("open_folder", None)
        open_folder_action.connect(
            "activate", lambda *_: open_containing_folder(owner, owner.source_path)
        )
        action_group.add_action(open_folder_action)

        # Info action
        info_action = Gio.SimpleAction.new("info", None)
        info_action.connect(
            "activate", lambda action, param: owner._on_show_info(action, param)
        )
        action_group.add_action(info_action)

        self.insert_action_group("row", action_group)
        button.set_popover(menu)

    def _on_show_info(self, action, param):
        if self.probe_service is None:
            return
        dialog = FileInfoWindow(
            self.get_root(),
            self.probe_service,
            self.source_path,
            os.path.basename(self.file_path),
            self.stream_index,
        )
        self._info_dialogs.append(dialog)
        owner = weakref.proxy(self)
        dialog.connect(
            "close-request",
            lambda window: owner._info_dialogs.remove(window) or False,
        )
        dialog.present()

    def cleanup(self):
        for dialog in self._info_dialogs[:]:
            dialog.close()
        self._info_dialogs.clear()
        self.on_remove_callback = self.on_play_callback = self.on_delete_callback = None
        self.on_activate_callback = None

    def set_result(self, item):
        """A conversion's outcome at the row's end; None clears it."""
        while child := self.result_box.get_first_child():
            self.result_box.remove(child)
        if item is None:
            return
        if item.successful and not item.warnings:
            done = Gtk.Image(
                icon_name="object-select-symbolic", css_classes=["success"]
            )
            done.update_property([Gtk.AccessibleProperty.LABEL], [_("Converted")])
            self.result_box.append(done)
        else:
            self.result_box.append(_result_details(item))
        if item.outputs:
            owner = weakref.proxy(self)
            reveal = Gtk.Button(
                icon_name="folder-open-symbolic",
                tooltip_text=_("Show output file"),
                css_classes=["flat", "circular"],
            )
            reveal.update_property(
                [Gtk.AccessibleProperty.LABEL], [_("Show output file")]
            )
            reveal.connect(
                "clicked",
                lambda _button, path=item.outputs[0]: open_containing_folder(
                    owner, path
                ),
            )
            self.result_box.append(reveal)

    def show_playing(self, playing):
        label = _("Stop") if playing else _("Play file")
        self.play_button.set_icon_name(
            "media-playback-stop-symbolic"
            if playing
            else "media-playback-start-symbolic"
        )
        self.play_button.set_tooltip_text(label)
        self.play_button.update_property([Gtk.AccessibleProperty.LABEL], [label])

    def set_details(self, details):
        """What the probe found; the number of parts is added to it."""
        self._details = details
        self._show_subtitle()

    def set_cut_count(self, count):
        if count != self._cut_count:
            self._cut_count = count
            self._show_subtitle()

    def _show_subtitle(self):
        parts = [self._details] if self._details else []
        if self._cut_count:
            parts.append(
                gettext.ngettext(
                    "{count} part", "{count} parts", self._cut_count
                ).format(count=self._cut_count)
            )
        self.set_subtitle(GLib.markup_escape_text(" · ".join(parts)))


class FileQueue(Gtk.Box):
    """Widget for managing a queue of files to be converted."""

    def __init__(self, converter, tooltip_helper, file_markers):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.converter = converter
        self._tooltip_helper = tooltip_helper
        # The window's cuts by entry; a file expanded into tracks hands its cuts on.
        self._file_markers = file_markers
        self.files = []  # List of file paths
        self.file_rows = []  # List of FileQueueRow widgets
        self.currently_playing_index = None
        self.active_file_index = None  # Index of file showing its waveform
        self._probes = ProbeService(converter.ffmpeg_path)
        self._sources = MainLoopSources()
        self._pending_probes = {}
        self._imports = deque()
        self._import_source = None
        self._closed = False
        self.on_probe_error = None
        self.on_trash_error = None
        self.on_pending_changed = None
        self.on_queue_size_changed = None

        # Track metadata storage for video files with multiple audio tracks
        # Key: file_path, Value: dict with 'source_video', 'track_index', 'codec', 'language', etc.
        self.track_metadata = {}

        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True
        )

        self.file_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            valign=Gtk.Align.START,
            css_classes=["boxed-list"],
            margin_start=12,
            margin_end=12,
            margin_top=6,
            margin_bottom=12,
        )

        # Enable reordering by drag and drop within the list
        self.file_list.set_can_focus(True)

        # Delete removes the focused entry, never the file itself.
        owner = weakref.proxy(self)
        delete = Gtk.ShortcutController()
        delete.add_shortcut(
            Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string("Delete|KP_Delete"),
                action=Gtk.CallbackAction.new(lambda *_: owner._remove_focused_row()),
            )
        )
        self.file_list.add_controller(delete)

        drop_target_reorder = Gtk.DropTarget.new(FileQueueRow, Gdk.DragAction.MOVE)
        drop_target_reorder.connect("drop", weak_callback(self._on_row_drop))
        self.file_list.add_controller(drop_target_reorder)

        scrolled.set_child(self.file_list)

        empty = Adw.StatusPage(
            icon_name="big-audio-converter",
            title=_("Add audio or video files"),
            # How to begin first; the button is named as the header shows it.
            description=_("Drag files here or use {button}.").format(
                button=_("Add Files")
            )
            + "\n"
            + _(
                "Each audio track of a video becomes its own entry. "
                "The originals are never changed."
            ),
        )
        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True
        )
        self.stack.add_named(empty, "empty")
        self.stack.add_named(scrolled, "files")
        self.append(self.stack)

        self.on_stop_playback = None
        self.on_file_added_to_empty_queue = None
        self.on_activate_file = None  # A row was activated (clicked)
        self.on_play_file = None

        self.file_removed_signal = None  # Will be set by MainWindow

    def _add_track_entry(self, video_file, track_info, track_display_index):
        """Expand an inspected source without additional blocking probes."""
        codec = track_info.get("codec_name", "unknown")
        # An audio-only container rather than a guessed AAC for unknown codecs.
        extension = "." + copy_container({"codec_name": codec}, "")[0]
        title = (
            os.path.basename(video_file)
            + " — "
            + _("Track {number}").format(number=track_display_index)
        )
        identifier = track_identifier(video_file, track_info["index"])
        if identifier in self.files:
            return False
        self.track_metadata[identifier] = {
            "source_video": video_file,
            "track_index": track_info["index"],
            "output_name": f"{Path(video_file).stem}-track{track_display_index}{extension}",
            "display_name": title,
            "codec": codec,
            "channels": track_info.get("channels", 0),
            "sample_rate": track_info.get("sample_rate", ""),
            "bitrate": track_info.get("bit_rate", ""),
            "language": track_info.get("tags", {}).get("language", ""),
            "title": track_info.get("tags", {}).get("title", ""),
        }
        row = self._append_row(identifier)
        row.source_path = video_file
        row.stream_index = track_info["index"]
        row.set_title(GLib.markup_escape_text(title))
        return True

    def _append_row(self, identifier):
        row = FileQueueRow(
            identifier,
            len(self.files),
            self.remove_file,
            self.on_play_file,
            self.on_delete_file,
            self.on_activate_file,
        )
        row.probe_service = self._probes
        self.files.append(identifier)
        self.file_rows.append(row)
        self.file_list.append(row)
        self._apply_row_tooltips(row)
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect("prepare", weak_callback(self._on_row_drag_prepare))
        drag.connect("drag-begin", weak_callback(self._on_row_drag_begin))
        drag.connect("drag-end", self._on_row_drag_end)
        row.add_controller(drag)
        return row

    @property
    def pending(self):
        return bool(self._pending_probes or self._imports)

    def _pending_changed(self):
        if self.on_pending_changed and not self._closed:
            self.on_pending_changed(self.pending)

    def _trash_failed(self, message):
        if self.on_trash_error and not self._closed:
            self.on_trash_error(_("Could not move the file to Trash"), message)

    def _probe_failed(self, path, message):
        if self.on_probe_error and not self._closed:
            self.on_probe_error(path, message)
        else:
            logger.warning("Media inspection failed: %s", message)

    def _probe_ready(self, token, path, info, error):
        if self._closed or self._pending_probes.get(path) != token:
            return
        self._pending_probes.pop(path, None)
        if path not in self.files:
            self._pending_changed()
            return
        index = self.files.index(path)
        tracks = [
            s for s in (info or {}).get("streams", []) if s.get("codec_type") == "audio"
        ]
        if error or not tracks:
            self.remove_file(index)
            self._probe_failed(
                path, error or _("This file has no supported audio track.")
            )
            self._pending_changed()
            return
        row = self.file_rows[index]
        if len(tracks) > 1:
            # Replace the pending row in place, preserving any user's reordering.
            with self._rows_keep_selection():
                self.files.pop(index)
                self.file_rows.pop(index)
                self.file_list.remove(row)
                row.cleanup()
                # Cuts handed over for the file apply to each of its tracks.
                cuts = self._file_markers.pop(path, None)
                added = []
                for number, track in enumerate(tracks, 1):
                    if self._add_track_entry(path, track, number):
                        added.append((self.files.pop(), self.file_rows.pop()))
                for offset, (identifier, track_row) in enumerate(added):
                    self.file_list.remove(track_row)
                    self.files.insert(index + offset, identifier)
                    self.file_rows.insert(index + offset, track_row)
                    self.file_list.insert(track_row, index + offset)
                    self._set_probed_metadata(track_row, info)
                    if cuts is not None:
                        self._file_markers[identifier] = [dict(cut) for cut in cuts]
                for position, current_row in enumerate(self.file_rows):
                    current_row.index = position
        else:
            row.stream_index = tracks[0]["index"]
            row.set_activatable(True)
            row.play_button.set_sensitive(True)
            self._set_probed_metadata(row, info)
        self.update_queue_size_label()
        self._pending_changed()
        if (
            self.active_file_index is None
            and self.files
            and self.on_file_added_to_empty_queue
        ):
            first = next(
                (
                    r
                    for r in self.file_rows
                    if r.source_path not in self._pending_probes
                ),
                None,
            )
            if first is not None:
                self.on_file_added_to_empty_queue(first.file_path, first.index)

    def _set_probed_metadata(self, row, info):
        """Length, type and sample rate: "0:06 · WAV · 48 kHz"."""
        stream = audio_stream(info, row.stream_index)
        row.duration = audio_duration(info, stream)
        parts = [
            _("Unknown duration")
            if row.duration is None
            else format_time_display(row.duration)
        ]
        # A track is told apart by its codec; a file by the type it was saved as.
        if row.file_path != row.source_path:
            parts.append(stream.get("codec_name", "").upper())
        else:
            parts.append(Path(row.source_path).suffix[1:].upper())
        try:
            rate = int(stream.get("sample_rate"))
        except (TypeError, ValueError):
            rate = 0
        if rate:
            parts.append(f"{rate / 1000:g} kHz")
        language = stream.get("tags", {}).get("language")
        if language and language != "und":
            parts.append(language)
        row.set_details(" · ".join(part for part in parts if part))

    def add_file(self, file_path):
        """Add immediately; inspect and expand tracks on a bounded worker."""
        if self._closed:
            return False
        try:
            if not file_path or "\0" in file_path:
                raise MediaError(_("Choose a valid local media file."))
            path = os.path.abspath(os.fspath(file_path))
            if path in self.files or any(
                m["source_video"] == path for m in self.track_metadata.values()
            ):
                return False
            token = self._probes.request(path, self._probe_ready)
            self._pending_probes[path] = token
            row = self._append_row(path)
            row.set_details(_("Inspecting audio…"))
            row.set_activatable(False)
            row.play_button.set_sensitive(False)
            self.update_queue_size_label()
            self._pending_changed()
            return True
        except (MediaError, OSError, TypeError, ValueError) as exc:
            self._probe_failed(str(file_path), str(exc))
            return False

    def add_files(self, paths):
        """Build rows in small GTK work slices; queue inspection stays bounded."""
        if self._closed:
            return
        for path in paths:
            if len(self._imports) + len(self.files) >= 10000:
                self._probe_failed(
                    str(path),
                    _(
                        "The queue limit is 10,000 files. Convert this batch before adding more."
                    ),
                )
                break
            self._imports.append(path)
        if self._imports and self._import_source is None:
            self._import_source = self._sources.idle(self._import_chunk)
        self._pending_changed()

    def _import_chunk(self):
        self._import_source = None
        deadline = time.monotonic() + 0.008
        while (
            self._imports
            and len(self._pending_probes) < 128
            and time.monotonic() < deadline
        ):
            self.add_file(self._imports.popleft())
        if self._imports:
            self._import_source = self._sources.later(16, self._import_chunk)
        self._pending_changed()

    def update_queue_size_label(self):
        if self._closed:
            return
        self.stack.set_visible_child_name("files" if self.files else "empty")
        if self.on_queue_size_changed:
            self.on_queue_size_changed(len(self.files))

    def total_duration(self):
        return sum(row.duration for row in self.file_rows if row.duration)

    def on_delete_file(self, index, file_path):
        if self.converter.busy:
            self._trash_failed(
                _(
                    "Cancel the conversion or wait for it to finish before moving its input to Trash."
                )
            )
            return
        actual = self.track_metadata.get(file_path, {}).get("source_video", file_path)
        dialog = Adw.AlertDialog(
            heading=_("Move File to Trash?"),
            body=_(
                "Move '{name}' to Trash? All of its tracks will be removed from the queue."
            ).format(name=os.path.basename(actual)),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("trash", _("Move to Trash"))
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_response, actual)
        dialog.present(self.get_root())

    def _on_delete_response(self, dialog, response, path):
        if response != "trash" or self._closed:
            return
        if self.converter.busy:
            self._trash_failed(
                _(
                    "The file is in use by a conversion. Cancel it before moving the input to Trash."
                )
            )
            return

        def trashed(file, result):
            try:
                file.trash_finish(result)
            except GLib.Error:
                if not self._closed:
                    self._trash_failed(
                        _(
                            "The file could not be moved to Trash. It has not been deleted."
                        )
                    )
                return
            if not self._closed:
                for index in reversed(range(len(self.file_rows))):
                    if self.file_rows[index].source_path == path:
                        self.remove_file(index)

        Gio.File.new_for_path(path).trash_async(GLib.PRIORITY_DEFAULT, None, trashed)

    def set_locked(self, locked):
        """While converting, entries are neither removed nor reordered."""
        for row in self.file_rows:
            row.remove_button.set_sensitive(not locked)

    def reveal(self, path):
        open_containing_folder(self, path)

    def _remove_focused_row(self):
        row = self.file_list.get_focus_child()
        if self.converter.busy or not isinstance(row, FileQueueRow):
            return False
        self.remove_file(row.index)
        if self.file_rows:
            self.file_rows[min(row.index, len(self.file_rows) - 1)].grab_focus()
        return True

    def remove_file(self, index):
        if not 0 <= index < len(self.files):
            return False
        identifier = self.files[index]
        row = self.file_rows[index]
        token = self._pending_probes.pop(identifier, None)
        if token is not None:
            self._probes.cancel(token)
        was_playing = self.currently_playing_index == index
        if was_playing and self.on_stop_playback:
            self.on_stop_playback()
        self.files.pop(index)
        self.file_rows.pop(index)
        self.track_metadata.pop(identifier, None)
        for attribute in ("currently_playing_index", "active_file_index"):
            current = getattr(self, attribute)
            if current is not None:
                setattr(
                    self,
                    attribute,
                    None if current == index else current - int(current > index),
                )
        for position, remaining in enumerate(self.file_rows):
            remaining.index = position
        row.cleanup()
        self.file_list.remove(row)
        if self.file_removed_signal:
            self.file_removed_signal(identifier)
        self.update_queue_size_label()
        self._pending_changed()
        return True

    def clear_queue(self):
        self._imports.clear()
        self._sources.cancel(self._import_source)
        self._import_source = None
        for token in self._pending_probes.values():
            self._probes.cancel(token)
        self._pending_probes.clear()
        if self.on_stop_playback:
            self.on_stop_playback()
        previous = self.files[:]
        for row in self.file_rows:
            row.cleanup()
            self.file_list.remove(row)
        self.files.clear()
        self.file_rows.clear()
        self.track_metadata.clear()
        self.currently_playing_index = self.active_file_index = None
        if self.file_removed_signal:
            for identifier in previous:
                self.file_removed_signal(identifier)
        self.update_queue_size_label()
        self._pending_changed()

    def cleanup(self):
        if self._closed:
            return
        self._closed = True
        self._imports.clear()
        self._sources.close()
        self._probes.cleanup()
        self._pending_probes.clear()
        for row in self.file_rows:
            row.cleanup()
        for name in (
            "on_stop_playback",
            "on_file_added_to_empty_queue",
            "on_activate_file",
            "file_removed_signal",
            "on_probe_error",
            "on_pending_changed",
            "on_queue_size_changed",
        ):
            setattr(self, name, None)

    def get_files(self):
        """Get all files in the queue."""
        return self.files.copy()

    def update_progress(self, index, progress):
        """Update conversion progress for a file."""
        if 0 <= index < len(self.file_rows):
            row = self.file_rows[index]

            row.progress_bar.set_fraction(progress)
            row.progress_bar.set_text(f"{int(progress * 100)}%")
            row.progress_bar.set_visible(True)

    def set_currently_playing(self, index):
        """Set the currently playing file and update UI."""
        if (
            self.currently_playing_index is not None
            and 0 <= self.currently_playing_index < len(self.file_rows)
        ):
            self.file_rows[self.currently_playing_index].show_playing(False)

        self.currently_playing_index = index
        if 0 <= index < len(self.file_rows):
            self.file_rows[index].show_playing(True)

    def set_active_file(self, index):
        """Set the active file (the one showing its waveform) and update UI."""

        if self.active_file_index is not None and 0 <= self.active_file_index < len(
            self.file_rows
        ):
            old_row = self.file_rows[self.active_file_index]
            old_row.remove_css_class("active-waveform-file")

        self.active_file_index = index
        if index is not None and 0 <= index < len(self.file_rows):
            new_row = self.file_rows[index]
            new_row.add_css_class("active-waveform-file")

    def update_playing_state(self, is_playing):
        """Update UI based on player state."""
        if (
            self.currently_playing_index is not None
            and 0 <= self.currently_playing_index < len(self.file_rows)
        ):
            self.file_rows[self.currently_playing_index].show_playing(is_playing)

    def add_dropped(self, value):
        """Queue the local files of a drop and report the others."""
        if not value:
            return False
        files = value.get_files() if isinstance(value, Gdk.FileList) else value
        paths = []
        for file in files:
            if not isinstance(file, Gio.File):
                continue
            if file.get_path():
                paths.append(file.get_path())
            else:
                self._probe_failed(
                    file.get_uri(),
                    _("Download this file to a local folder before adding it."),
                )
        self.add_files(paths)
        return bool(paths)

    def _on_row_drag_prepare(self, drag_source, x, y):
        """Prepare drag operation for a row."""
        if self.converter.busy:
            return None
        content = Gdk.ContentProvider.new_for_value(drag_source.get_widget())
        return content

    def _on_row_drag_begin(self, drag_source, drag):
        """Handle drag begin for a row."""
        row = drag_source.get_widget()
        row.add_css_class("drag-row")
        paintable = Gtk.WidgetPaintable.new(row)
        drag_source.set_icon(paintable, 0, 0)

    @staticmethod
    def _on_row_drag_end(drag_source, _drag, _delete_data):
        drag_source.set_icon(None, 0, 0)
        drag_source.get_widget().remove_css_class("drag-row")

    def _on_row_drop(self, drop_target, value, x, y):
        """Handle drop of a row for reordering."""
        dragged_row = value
        if self.converter.busy or not isinstance(dragged_row, FileQueueRow):
            return False

        # Find the target position based on y coordinate
        target_row = None
        for row in self.file_rows:
            allocation = row.get_allocation()
            row_y = row.translate_coordinates(self.file_list, 0, 0)[1]

            if y >= row_y and y < row_y + allocation.height:
                target_row = row
                break

        # If no target row found, append to end
        if not target_row:
            target_row = self.file_rows[-1] if self.file_rows else None

        # Don't reorder if dropping on same position
        if target_row == dragged_row:
            return False

        # Perform the reordering
        try:
            old_index = dragged_row.index
            new_index = target_row.index if target_row else len(self.file_rows)

            with self._rows_keep_selection():
                self.files.insert(new_index, self.files.pop(old_index))
                self.file_rows.insert(new_index, self.file_rows.pop(old_index))
                for i, row in enumerate(self.file_rows):
                    row.index = i

            # Reorder in the ListBox
            self.file_list.remove(dragged_row)
            if new_index >= len(self.file_rows):
                # Insert at end
                self.file_list.append(dragged_row)
            else:
                # Insert at specific position
                self.file_list.insert(dragged_row, new_index)

            # Remove drag styling
            dragged_row.remove_css_class("drag-row")

            return True

        except (ValueError, IndexError, TypeError, RuntimeError) as e:
            logger.error("Error reordering files: %s", e)
            return False

    @contextmanager
    def _rows_keep_selection(self):
        """Keep the shown and the playing file selected while rows move."""
        shown = (
            self.files[self.active_file_index]
            if self.active_file_index is not None
            else None
        )
        playing = (
            self.files[self.currently_playing_index]
            if self.currently_playing_index is not None
            else None
        )
        yield
        self.active_file_index = (
            self.files.index(shown) if shown in self.files else None
        )
        self.currently_playing_index = (
            self.files.index(playing) if playing in self.files else None
        )

    def connect_file_removed_signal(self, callback):
        """Connect a callback to be notified when a file is removed."""
        self.file_removed_signal = callback

    def _apply_row_tooltips(self, row):
        """Apply custom tooltips to a file queue row."""
        # The buttons' own tooltips take precedence over the row's.
        self._tooltip_helper.add_tooltip(row, "right_click_options")
