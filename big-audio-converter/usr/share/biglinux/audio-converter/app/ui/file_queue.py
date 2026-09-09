"""
File queue UI component for managing files to be converted.
"""

import gettext
import logging
import os
import time
from collections import deque
from pathlib import Path

import gi

from app.audio.media import track_identifier

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from app.audio.media import audio_duration, audio_stream
from app.audio.probe_service import ProbeService
from app.audio.process import MediaError
from app.audio.profiles import copy_container
from app.utils.main_loop import MainLoopSources

logger = logging.getLogger(__name__)


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

        self.file_path = file_path
        self.source_path = file_path
        self.stream_index = None
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

        # Metadata will be set as subtitle
        self.set_subtitle("")

        # Make row activatable
        self.set_activatable(True)

        # Connect activated signal to callback
        if on_activate_callback:
            self.connect(
                "activated",
                lambda row: self.on_activate_callback(self.file_path, self.index),
            )

        # Play button (left side)
        self.play_button = Gtk.Button.new_from_icon_name(
            "media-playback-start-symbolic"
        )
        self.play_button.add_css_class("flat")
        self.play_button.set_valign(Gtk.Align.CENTER)
        self.play_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Play file")],
        )
        self.play_button.connect(
            "clicked", lambda btn: self.on_play_callback(self.file_path, self.index)
        )
        self.add_prefix(self.play_button)

        # Remove from queue button (left side, after play button)
        remove_button = Gtk.Button.new_from_icon_name("edit-delete-symbolic")
        remove_button.add_css_class("flat")
        remove_button.set_valign(Gtk.Align.CENTER)
        remove_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Remove from queue")],
        )
        remove_button.connect(
            "clicked", lambda btn: self.on_remove_callback(self.index)
        )
        self.add_prefix(remove_button)

        # Progress bar (right side)
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_visible(False)
        self.progress_bar.add_css_class("file-progress")
        self.add_suffix(self.progress_bar)

        # Add right-click context menu
        self._setup_context_menu()

        # Connect to realize signal to add tooltip to title widget after it's created
        self.connect("realize", self._on_row_realized)

        # Store references for tooltip helper (will be accessed later)
        self._play_button = self.play_button
        self._remove_button = remove_button

    def _setup_context_menu(self):
        """Setup right-click context menu for the file row."""
        # Create popup menu
        menu = Gtk.PopoverMenu()
        menu_model = Gio.Menu()

        # Delete file from filesystem action
        menu_model.append(_("Move to Trash"), "row.delete")

        # Open containing folder action
        menu_model.append(_("Open Containing Folder"), "row.open_folder")

        # More information action
        menu_model.append(_("More Information..."), "row.info")

        menu.set_menu_model(menu_model)

        # Create action group
        action_group = Gio.SimpleActionGroup()

        # Delete action (with confirmation)
        delete_action = Gio.SimpleAction.new("delete", None)
        delete_action.connect(
            "activate", lambda a, p: self.on_delete_callback(self.index, self.file_path)
        )
        action_group.add_action(delete_action)

        # Open folder action
        open_folder_action = Gio.SimpleAction.new("open_folder", None)
        open_folder_action.connect("activate", self._on_open_folder)
        action_group.add_action(open_folder_action)

        # Info action
        info_action = Gio.SimpleAction.new("info", None)
        info_action.connect("activate", self._on_show_info)
        action_group.add_action(info_action)

        self.insert_action_group("row", action_group)
        self._context_menu = menu
        more = Gtk.MenuButton(icon_name="view-more-symbolic", popover=menu)
        more.set_tooltip_text(_("File actions"))
        more.update_property([Gtk.AccessibleProperty.LABEL], [_("File actions")])
        self.add_suffix(more)

        # Add right-click gesture
        right_click = Gtk.GestureClick.new()
        right_click.set_button(3)  # Right mouse button
        right_click.connect("pressed", lambda g, n, x, y: menu.popup())
        self.add_controller(right_click)

    def _on_row_realized(self, widget):
        """Add tooltip to the title label after the row is realized."""

        # The ActionRow creates internal widgets, we need to find the title label
        # In Adwaita, the title is typically in a Box containing labels
        def find_title_label(widget):
            """Recursively find the title label widget."""
            if (isinstance(widget, Gtk.Label)) and (widget.get_label() == self.get_title()):
                return widget

            # If widget is a container, check its children
            if hasattr(widget, "get_first_child"):
                child = widget.get_first_child()
                while child:
                    result = find_title_label(child)
                    if result:
                        return result
                    child = child.get_next_sibling()
            return None

        # Find and add tooltip to the title label
        title_label = find_title_label(self)
        # Tooltip will be added via tooltip_helper if available
        self._title_label = title_label
        
        # Now apply tooltip to the title label if tooltip_helper exists
        # Need to get tooltip_helper from file_queue parent
        if title_label and hasattr(self, 'index'):
            # Access file queue through callbacks to get tooltip_helper
            parent = self.get_parent()
            while parent and not isinstance(parent, Gtk.ListBox):
                parent = parent.get_parent()
            if parent:
                file_queue = parent.get_parent()
                while file_queue and not isinstance(file_queue, FileQueue):
                    file_queue = file_queue.get_parent()
                if file_queue and hasattr(file_queue, '_tooltip_helper') and file_queue._tooltip_helper:
                    file_queue._tooltip_helper.add_tooltip(title_label, "right_click_options")

    def _on_open_folder(self, action, param):
        parent = self.get_root()
        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(self.source_path))
        launcher.open_containing_folder(parent, None, self._folder_opened)

    def _folder_opened(self, launcher, result):
        try:
            launcher.open_containing_folder_finish(result)
        except GLib.Error as exc:
            logger.warning("The containing folder could not be opened: %s", exc.message)

    @staticmethod
    def _format_size(size_bytes):
        """Format byte count to human-readable string."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        if size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

    @staticmethod
    def _parse_duration_tag(value):
        """Parse a duration tag string like '00:20:55.072000000' into seconds."""
        time_parts = value.split(":")
        if len(time_parts) == 3:
            return int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + float(time_parts[2])
        return None

    @staticmethod
    def _format_duration(total_secs):
        """Format seconds into H:MM:SS or M:SS string."""
        hours = int(total_secs // 3600)
        minutes = int((total_secs % 3600) // 60)
        seconds = int(total_secs % 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


    def _get_stream_tag(self, stream, prefixes):
        """Get the first matching tag value from a stream's tags."""
        if not stream or "tags" not in stream:
            return None
        for key, value in stream["tags"].items():
            for prefix in prefixes:
                if key.startswith(prefix + "-") or key == prefix:
                    return value
        return None

    def _extract_audio_props(self, audio_stream, data, is_video_track, actual_path):
        """Extract ordered list of (label, value) for audio properties."""
        props = []

        # --- Size ---
        duration_val = self._get_tag_as_duration(audio_stream)
        if duration_val is None and audio_stream and "duration" in audio_stream:
            try:
                duration_val = float(audio_stream["duration"])
            except (ValueError, TypeError):
                pass

        bitrate_val = self._get_tag_as_int(audio_stream, ("BPS",))
        if bitrate_val is None and audio_stream and "bit_rate" in audio_stream:
            try:
                bitrate_val = int(audio_stream["bit_rate"])
            except (ValueError, TypeError):
                pass

        if duration_val is not None and bitrate_val is not None:
            props.append(("Size", self._format_size(int(duration_val * bitrate_val / 8))))
        elif not is_video_track:
            try:
                props.append(("Size", self._format_size(os.path.getsize(actual_path))))
            except OSError:
                props.append(("Size", "Unknown"))
        else:
            props.append(("Size", "Unknown"))

        # --- Duration ---
        if duration_val is not None:
            props.append(("Duration", self._format_duration(duration_val)))
        elif "format" in data and "duration" in data["format"]:
            try:
                props.append(("Duration", self._format_duration(float(data["format"]["duration"]))))
            except (ValueError, TypeError):
                pass

        # --- Format ---
        if audio_stream and "codec_long_name" in audio_stream:
            props.append(("Format", audio_stream["codec_long_name"]))
        elif "format" in data and "format_long_name" in data["format"]:
            props.append(("Format", data["format"]["format_long_name"]))

        # --- Bitrate ---
        if bitrate_val is not None:
            props.append(("Bitrate", f"{bitrate_val // 1000} kbps"))
        elif "format" in data and "bit_rate" in data["format"]:
            try:
                props.append(("Bitrate", f"{int(data['format']['bit_rate']) // 1000} kbps"))
            except (ValueError, TypeError):
                pass

        # --- Sample rate, channels, bit depth ---
        if audio_stream:
            if "sample_rate" in audio_stream:
                try:
                    props.append(("Sample Rate", f"{int(audio_stream['sample_rate']) / 1000:g} kHz"))
                except (ValueError, TypeError):
                    pass
            if "channels" in audio_stream:
                ch = audio_stream["channels"]
                layout = audio_stream.get("channel_layout", "")
                props.append(("Channels", f"{ch} ({layout})" if layout else str(ch)))
            bps = audio_stream.get("bits_per_sample", 0)
            if bps and int(bps) > 0:
                props.append(("Bit Depth", f"{bps} bit"))

        return props

    def _get_tag_as_duration(self, stream):
        """Get duration in seconds from stream tags."""
        raw = self._get_stream_tag(stream, ("DURATION",))
        if raw:
            try:
                return self._parse_duration_tag(raw)
            except (ValueError, TypeError, OverflowError):
                logger.debug("Unrecognized duration tag", exc_info=True)
        return None

    def _get_tag_as_int(self, stream, prefixes):
        """Get an integer tag value from stream."""
        raw = self._get_stream_tag(stream, prefixes)
        if raw:
            try:
                return int(raw)
            except (ValueError, TypeError):
                pass
        return None

    def _on_show_info(self, action, param):
        """Present immediately; refresh metadata without blocking the GTK thread."""
        if self.probe_service is None:
            return
        dialog = Gtk.Window(transient_for=self.get_root(), modal=True,
                            title=_("File Information"), default_width=640, default_height=540)
        header = Gtk.HeaderBar()
        copy_button = Gtk.Button(icon_name="edit-copy-symbolic", sensitive=False,
                                 tooltip_text=_("Copy information to clipboard"))
        header.pack_end(copy_button)
        dialog.set_titlebar(header)
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scrolled.set_child(main_box)
        dialog.set_child(scrolled)
        title = Gtk.Label(label=os.path.basename(self.file_path), wrap=True,
                          margin_top=18, margin_start=18, margin_end=18)
        title.add_css_class("title-2")
        main_box.append(title)
        spinner = Gtk.Spinner(spinning=True, margin_top=18, margin_bottom=18)
        main_box.append(spinner)
        clipboard = []
        copy_button.connect("clicked", lambda button: self._copy_to_clipboard("\n".join(clipboard), dialog))

        def ready(token, path, info, error):
            spinner.stop()
            main_box.remove(spinner)
            if error or not info:
                self._append_error_label(main_box, error or _("No audio information is available."))
                return
            try:
                stream = audio_stream(info, self.stream_index)
                properties = self._extract_audio_props(stream, info, self.stream_index is not None, path)
                main_box.append(self._create_info_group(_("File Details"), [(_("Path"), path)]))
                main_box.append(self._create_info_group(_("Audio Properties"), properties))
                clipboard.extend([path, ""] + [f"{label}: {value}" for label, value in properties])
                self._append_metadata_group(info, main_box, clipboard)
                copy_button.set_sensitive(True)
            except (MediaError, ValueError, TypeError) as exc:
                self._append_error_label(main_box, str(exc))

        try:
            token = self.probe_service.request(self.source_path, ready, priority=True)
        except (MediaError, RuntimeError) as exc:
            spinner.stop()
            self._append_error_label(main_box, str(exc))
            token = None
        self._info_dialogs.append(dialog)

        def closed(window):
            if token is not None:
                self.probe_service.cancel(token)
            if window in self._info_dialogs:
                self._info_dialogs.remove(window)
            return False

        dialog.connect("close-request", closed)
        dialog.present()

    def cleanup(self):
        for dialog in self._info_dialogs[:]:
            dialog.close()
        self._info_dialogs.clear()
        self.on_remove_callback = self.on_play_callback = self.on_delete_callback = None
        self.on_activate_callback = None

    def _append_metadata_group(self, data, main_box, info_text):
        """Extract and append metadata tags group to the dialog."""
        if "format" not in data or "tags" not in data["format"]:
            return
        tags = data["format"]["tags"]
        metadata_items = []
        tag_order = [
            "title", "artist", "album", "album_artist", "date", "genre",
            "track", "disc", "comment", "composer", "performer",
            "copyright", "encoded_by", "encoder",
        ]
        for tag_key in tag_order:
            if tag_key in tags:
                label = tag_key.replace("_", " ").title()
                metadata_items.append((label, str(tags[tag_key])[:4096]))
        for key, value in list(tags.items())[:128]:
            if key.lower() not in tag_order:
                label = key.replace("_", " ").title()
                metadata_items.append((label, str(value)[:4096]))
        if metadata_items:
            main_box.append(self._create_info_group(_("Metadata Tags"), metadata_items))
            info_text.append("")
            for label, value in metadata_items:
                info_text.append(f"{label}: {value}")

    @staticmethod
    def _append_error_label(main_box, message):
        """Append an error label to the dialog."""
        error_label = Gtk.Label(label=message)
        error_label.add_css_class("dim-label")
        error_label.set_margin_top(12)
        error_label.set_margin_bottom(12)
        main_box.append(error_label)

    def _create_info_group(self, title, items):
        """Create a group of information items with Adwaita styling.

        Args:
            title: Group title
            items: List of (label, value) tuples

        Returns:
            Gtk.Box: Container with the group
        """
        group_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        group_box.set_margin_start(12)
        group_box.set_margin_end(12)
        group_box.set_margin_bottom(24)

        # Group title
        title_label = Gtk.Label()
        title_label.set_markup(
            f"<span weight='bold'>{GLib.markup_escape_text(title)}</span>"
        )
        title_label.set_xalign(0)
        title_label.set_margin_start(12)
        title_label.set_margin_bottom(6)
        group_box.append(title_label)

        # Create list box for items
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")

        for label, value in items:
            row = Adw.ActionRow()
            row.set_title(GLib.markup_escape_text(str(label)))

            # Value label
            value_label = Gtk.Label(label=str(value))
            value_label.set_wrap(True)
            value_label.set_xalign(1)
            value_label.add_css_class("dim-label")
            value_label.set_valign(Gtk.Align.CENTER)
            row.add_suffix(value_label)

            # Copy button for this row
            copy_btn = Gtk.Button()
            copy_btn.set_icon_name("edit-copy-symbolic")
            copy_btn.set_tooltip_text(_("Copy value"))
            copy_btn.add_css_class("flat")
            copy_btn.add_css_class("circular")
            copy_btn.set_valign(Gtk.Align.CENTER)
            copy_btn.update_property(
                [Gtk.AccessibleProperty.LABEL], [_("Copy value")],
            )
            copy_btn.connect(
                "clicked", lambda b, v=str(value): self._copy_value_to_clipboard(v)
            )
            row.add_suffix(copy_btn)

            listbox.append(row)

        group_box.append(listbox)
        return group_box

    def _copy_value_to_clipboard(self, value):
        """Copy a single value to clipboard."""
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(str(value))
        logger.debug("Metadata value copied to clipboard")

    def _copy_to_clipboard(self, text, dialog):
        """Copy text to clipboard."""
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(text)
        logger.info("Information copied to clipboard")

    def set_metadata(self, metadata_text):
        """Set the metadata subtitle."""
        self.set_subtitle(metadata_text)

    def update_progress(self, progress):
        """Update the progress bar."""
        self.progress_bar.set_fraction(progress)
        self.progress_bar.set_text(f"{int(progress * 100)}%")
        self.progress_bar.set_visible(True)  # Make visible during conversion


class FileQueue(Gtk.Box):
    """Widget for managing a queue of files to be converted."""

    def __init__(self, converter):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.converter = converter
        self.files = []  # List of file paths
        self.file_rows = []  # List of FileQueueRow widgets
        self.currently_playing_index = None
        self.active_file_index = None  # Index of file showing its waveform
        self._updates_suspended = False
        self._probes = ProbeService(converter.ffmpeg_path)
        self._sources = MainLoopSources()
        self._pending_probes = {}
        self._imports = deque()
        self._import_source = None
        self._closed = False
        self.on_probe_error = None
        self.on_pending_changed = None
        self._parent_window = None  # Will be set by MainWindow for dialogs
        self._tooltip_helper = None  # Will be set by MainWindow for tooltips

        # Track metadata storage for video files with multiple audio tracks
        # Key: file_path, Value: dict with 'source_video', 'track_index', 'codec', 'language', etc.
        self.track_metadata = {}

        # Initialize dictionaries for metadata storage
        # Add custom CSS for better visual styling
        self._setup_styles()

        # Create header with queue size label and actions
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        header_box.set_margin_top(6)
        header_box.set_margin_bottom(6)
        header_box.set_margin_start(10)
        header_box.set_margin_end(10)

        # Add queue size label to header (now hidden as it's shown in the headerbar)
        self.queue_size_label = Gtk.Label(label=_("0 files"))
        self.queue_size_label.set_halign(Gtk.Align.START)
        self.queue_size_label.set_hexpand(True)
        self.queue_size_label.set_visible(
            False
        )  # Hide the label as it's now in headerbar

        # Create scrolled window for file list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        # Create file list using Adwaita's ListBox for better styling
        self.file_list = Gtk.ListBox()
        self.file_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.file_list.add_css_class("boxed-list")  # Adwaita style

        # Enable reordering by drag and drop within the list
        self.file_list.set_can_focus(True)

        # Add drop target for reordering rows within the list
        drop_target_reorder = Gtk.DropTarget.new(FileQueueRow, Gdk.DragAction.MOVE)
        drop_target_reorder.connect("drop", self._on_row_drop)
        drop_target_reorder.connect("enter", self._on_row_drag_enter)
        drop_target_reorder.connect("leave", self._on_row_drag_leave)
        self.file_list.add_controller(drop_target_reorder)

        scrolled.set_child(self.file_list)

        # Create a simple placeholder
        self.placeholder = Adw.StatusPage()
        self.placeholder.set_icon_name("folder-music-symbolic")
        self.placeholder.set_title(_("No Audio Files"))
        self.placeholder.set_description(
            _("Drag files here or use the Add Files Button")
        )
        self.file_list.set_placeholder(self.placeholder)

        # Assemble the layout
        self.append(scrolled)

        # Enable drag and drop for multiple files with improved visual feedback
        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.set_gtypes([Gdk.FileList])
        drop_target.connect("drop", self._on_drop)
        drop_target.connect("enter", self._on_drop_enter)
        drop_target.connect("leave", self._on_drop_leave)
        self.add_controller(drop_target)

        # Initialize with no callbacks
        self.on_stop_playback = None
        self.on_playing_file_removed = (
            None  # New callback for when a playing file is removed
        )
        self.on_file_added_to_empty_queue = (
            None  # New callback for when file is added to empty queue
        )
        self.on_activate_file = (
            None  # New callback for when file row is activated (clicked)
        )

        # Add a signal for file removal
        self.file_removed_signal = None  # Will be set by MainWindow

    def _setup_styles(self):
        """Set up custom CSS styles for the file list."""
        css_provider = Gtk.CssProvider()
        css_provider.load_from_string(
            """
            .drag-highlight {
                background-color: alpha(@accent_color, 0.2);
                border: 2px dashed @accent_color;
                border-radius: 8px;
            }
            .drag-row {
                opacity: 0.5;
            }
            /* Active file (showing waveform) background highlight */
            .active-waveform-file {
                background-color: alpha(@accent_bg_color, 0.15);
            }
            """,
        )

        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )



    def _get_audio_codec_extension(self, codec_name):
        """Choose an audio-only container rather than guess AAC for unknown codecs."""
        extension, _ = copy_container({"codec_name": codec_name}, "")
        return "." + extension



    def _add_track_entry(self, video_file, track_info, track_display_index):
        """Expand an inspected source without additional blocking probes."""
        codec = track_info.get("codec_name", "unknown")
        extension = self._get_audio_codec_extension(codec)
        identifier = track_identifier(video_file, track_info["index"])
        if identifier in self.files:
            return False
        self.track_metadata[identifier] = {
            "source_video": video_file, "track_index": track_info["index"],
            "output_name": f"{Path(video_file).stem}-track{track_display_index}{extension}",
            "display_name": os.path.basename(video_file) + " — " + _("Track {number}").format(number=track_display_index),
            "codec": codec, "channels": track_info.get("channels", 0),
            "sample_rate": track_info.get("sample_rate", ""),
            "bitrate": track_info.get("bit_rate", ""),
            "language": track_info.get("tags", {}).get("language", ""),
            "title": track_info.get("tags", {}).get("title", ""),
        }
        row = self._append_row(identifier)
        row.source_path = video_file
        row.stream_index = track_info["index"]
        title = os.path.basename(video_file) + " — " + _("Track {number}").format(number=track_display_index)
        row.set_title(GLib.markup_escape_text(title))
        return True

    def _append_row(self, identifier):
        row = FileQueueRow(identifier, len(self.files), self.on_remove_file,
                           self.on_play_file, self.on_delete_file, self.on_activate_file)
        row.probe_service = self._probes
        self.files.append(identifier)
        self.file_rows.append(row)
        self.file_list.append(row)
        self._apply_row_tooltips(row)
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect("prepare", self._on_row_drag_prepare, row)
        drag.connect("drag-begin", self._on_row_drag_begin, row)
        row.add_controller(drag)
        return row

    @property
    def pending(self):
        return bool(self._pending_probes or self._imports)

    def _pending_changed(self):
        if self.on_pending_changed and not self._closed:
            self.on_pending_changed(self.pending)

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
        tracks = [s for s in (info or {}).get("streams", []) if s.get("codec_type") == "audio"]
        if error or not tracks:
            self.remove_file(index)
            self._probe_failed(path, error or _("This file has no supported audio track."))
            self._pending_changed()
            return
        row = self.file_rows[index]
        if len(tracks) > 1:
            # Replace the pending row in place, preserving any user's reordering.
            self.files.pop(index)
            self.file_rows.pop(index)
            self.file_list.remove(row)
            row.cleanup()
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
            for position, current_row in enumerate(self.file_rows):
                current_row.index = position
        else:
            row.stream_index = tracks[0]["index"]
            row.set_activatable(True)
            row.play_button.set_sensitive(True)
            self._set_probed_metadata(row, info)
        self.update_queue_size_label()
        self._pending_changed()
        if self.active_file_index is None and self.files and self.on_file_added_to_empty_queue:
            first = next((r for r in self.file_rows if r.source_path not in self._pending_probes), None)
            if first is not None:
                self.on_file_added_to_empty_queue(first.file_path, first.index)

    def _set_probed_metadata(self, row, info):
        stream = audio_stream(info, row.stream_index)
        duration = audio_duration(info, stream)
        parts = [stream.get("codec_name", "").upper()]
        if duration is not None:
            parts.append(FileQueueRow._format_duration(duration))
        else:
            parts.append(_("Unknown duration"))
        rate = stream.get("sample_rate")
        if rate:
            parts.append(_("{rate} Hz").format(rate=rate))
        language = stream.get("tags", {}).get("language")
        if language and language != "und":
            parts.append(language)
        row.set_metadata(GLib.markup_escape_text(" · ".join(parts)))

    def add_file(self, file_path):
        """Add immediately; inspect and expand tracks on a bounded worker."""
        if self._closed:
            return False
        try:
            if not file_path or "\0" in file_path:
                raise MediaError(_("Choose a valid local media file."))
            path = os.path.abspath(os.fspath(file_path))
            if path in self.files or any(m["source_video"] == path for m in self.track_metadata.values()):
                return False
            token = self._probes.request(path, self._probe_ready)
            self._pending_probes[path] = token
            row = self._append_row(path)
            row.set_metadata(_("Inspecting audio…"))
            row.set_activatable(False)
            row.play_button.set_sensitive(False)
            if not self._updates_suspended:
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
                self._probe_failed(str(path), _("The queue limit is 10,000 files. Convert this batch before adding more."))
                break
            self._imports.append(path)
        if self._imports and self._import_source is None:
            self._import_source = self._sources.idle(self._import_chunk)
        self._pending_changed()

    def _import_chunk(self):
        self._import_source = None
        deadline = time.monotonic() + .008
        while self._imports and len(self._pending_probes) < 128 and time.monotonic() < deadline:
            self.add_file(self._imports.popleft())
        if self._imports:
            self._import_source = self._sources.later(16, self._import_chunk)
        self._pending_changed()

    def update_queue_size_label(self):
        self.queue_size_label.set_text(self.get_queue_size_text())
        callback = getattr(self, "on_queue_size_changed", None)
        if callback and not self._closed:
            callback(len(self.files), self.get_queue_size_text())

    def get_queue_size_text(self):
        count = len(self.files)
        return gettext.ngettext("{count} file", "{count} files", count).format(count=count)

    def get_queue_size(self):
        """Get the current queue size as a number."""
        return len(self.files)

    def suspend_updates(self):
        """Temporarily suspend UI updates for batch operations."""
        self._updates_suspended = True

    def resume_updates(self):
        """Resume UI updates after batch operations."""
        self._updates_suspended = False
        self.update_queue_size_label()

    def on_remove_file(self, index):
        """Remove a file from the queue."""
        self.remove_file(index)

    def on_delete_file(self, index, file_path):
        if self.converter.busy:
            self._probe_failed(file_path, _("Cancel the conversion or wait for it to finish before moving its input to Trash."))
            return
        actual = self.track_metadata.get(file_path, {}).get("source_video", file_path)
        dialog = Adw.AlertDialog(heading=_("Move File to Trash?"),
                                body=_("Move '{name}' to Trash? All of its tracks will be removed from the queue.").format(name=os.path.basename(actual)))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("trash", _("Move to Trash"))
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_response, actual)
        dialog.present(self._parent_window)

    def _on_delete_response(self, dialog, response, path):
        if response != "trash" or self._closed:
            return
        if self.converter.busy:
            self._probe_failed(path, _("The file is in use by a conversion. Cancel it before moving the input to Trash."))
            return

        def trashed(file, result):
            try:
                file.trash_finish(result)
            except GLib.Error as exc:
                if not self._closed:
                    self._probe_failed(path, _("The file could not be moved to Trash. It has not been deleted."))
                logger.debug("Trash operation failed: %s", exc.message)
                return
            if not self._closed:
                for index in reversed(range(len(self.file_rows))):
                    if self.file_rows[index].source_path == path:
                        self.remove_file(index)

        Gio.File.new_for_path(path).trash_async(GLib.PRIORITY_DEFAULT, None, trashed)

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
                setattr(self, attribute, None if current == index else current - int(current > index))
        for position, remaining in enumerate(self.file_rows):
            remaining.index = position
        row.cleanup()
        self.file_list.remove(row)
        if was_playing and self.on_playing_file_removed:
            self.on_playing_file_removed()
        if self.file_removed_signal:
            self.file_removed_signal(identifier)
        self.update_queue_size_label()
        self._pending_changed()
        return True

    def on_clear_queue(self, button):
        """Clear the entire queue."""
        self.clear_queue()

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
        for name in ("on_stop_playback", "on_playing_file_removed", "on_file_added_to_empty_queue",
                     "on_activate_file", "file_removed_signal", "on_probe_error", "on_pending_changed"):
            setattr(self, name, None)

    def get_files(self):
        """Get all files in the queue."""
        return self.files.copy()

    def has_files(self):
        """Check if there are any files in the queue."""
        return len(self.files) > 0

    def update_progress(self, index, progress):
        """Update conversion progress for a file."""
        if 0 <= index < len(self.file_rows):
            row = self.file_rows[index]

            # Update progress bar
            row.progress_bar.set_fraction(progress)
            row.progress_bar.set_text(f"{int(progress * 100)}%")

            # Make progress visible
            if not row.progress_bar.get_visible():
                row.progress_bar.set_visible(True)

            # Hide progress when complete
            if progress >= 1:

                def hide_progress():
                    row.progress_bar.set_visible(False)
                    return False

                self._sources.later(1500, hide_progress)

    def on_play_file(self, file_path, index):
        """Handle play button click on a file."""
        # This will be implemented by the main window and connected

    def on_stop_playback(self):
        """Stop playback of the current file.

        This method should be overridden by the main window or other component
        that controls audio playback.
        """

    def set_currently_playing(self, index):
        """Set the currently playing file and update UI."""
        # Reset old playing item if any
        if (
            self.currently_playing_index is not None
            and 0 <= self.currently_playing_index < len(self.file_rows)
        ):
            old_row = self.file_rows[self.currently_playing_index]
            old_row.play_button.set_icon_name("media-playback-start-symbolic")
            # Removed accent color - we use background highlighting for active file instead

        # Set new playing item
        self.currently_playing_index = index
        if 0 <= index < len(self.file_rows):
            new_row = self.file_rows[index]
            new_row.play_button.set_icon_name("media-playback-stop-symbolic")
            # Removed accent color - we use background highlighting for active file instead

    def set_active_file(self, index):
        """Set the active file (the one showing its waveform) and update UI."""
        logger.info(f"Setting active file to index {index}")

        # Reset old active item if any
        if self.active_file_index is not None and 0 <= self.active_file_index < len(
            self.file_rows
        ):
            old_row = self.file_rows[self.active_file_index]
            old_row.remove_css_class("active-waveform-file")
            logger.debug(
                f"Removed active highlighting from index {self.active_file_index}"
            )

        # Set new active item
        self.active_file_index = index
        if index is not None and 0 <= index < len(self.file_rows):
            new_row = self.file_rows[index]
            new_row.add_css_class("active-waveform-file")
            logger.info(f"Added active highlighting to index {index}")

    def update_playing_state(self, is_playing):
        """Update UI based on player state."""
        if (
            self.currently_playing_index is not None
            and 0 <= self.currently_playing_index < len(self.file_rows)
        ):
            icon_name = (
                "media-playback-stop-symbolic"
                if is_playing
                else "media-playback-start-symbolic"
            )
            self.file_rows[self.currently_playing_index].play_button.set_icon_name(
                icon_name
            )

    def get_current_playing_index(self):
        """Return the index of the currently playing file or None if nothing is playing."""
        return (
            self.currently_playing_index
            if hasattr(self, "currently_playing_index")
            else None
        )

    def _on_drop(self, drop_target, value, x, y):
        self.placeholder.remove_css_class("drag-highlight")
        self.file_list.remove_css_class("drag-highlight")
        if not value:
            return False
        files = value.get_files() if isinstance(value, Gdk.FileList) else value
        paths = [file.get_path() for file in files if isinstance(file, Gio.File) and file.get_path()]
        self.add_files(paths)
        return bool(paths)

    def _on_row_drag_prepare(self, drag_source, x, y, row):
        """Prepare drag operation for a row."""
        # Set the row as the drag content
        content = Gdk.ContentProvider.new_for_value(row)
        return content

    def _on_row_drag_begin(self, drag_source, drag, row):
        """Handle drag begin for a row."""
        # Add visual feedback
        row.add_css_class("drag-row")
        # Create drag icon from the row
        paintable = Gtk.WidgetPaintable.new(row)
        drag_source.set_icon(paintable, 0, 0)

    def _on_row_drop(self, drop_target, value, x, y):
        """Handle drop of a row for reordering."""
        # Get the dragged row
        dragged_row = value
        if not isinstance(dragged_row, FileQueueRow):
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
            # Get indices
            old_index = dragged_row.index
            new_index = target_row.index if target_row else len(self.file_rows)

            active_path = self.files[self.active_file_index] if self.active_file_index is not None else None
            playing_path = self.files[self.currently_playing_index] if self.currently_playing_index is not None else None

            # Move the file in the internal list
            file_path = self.files.pop(old_index)
            self.files.insert(new_index, file_path)

            # Move the row in the visual list
            row_widget = self.file_rows.pop(old_index)
            self.file_rows.insert(new_index, row_widget)

            # Update row indices
            for i, row in enumerate(self.file_rows):
                row.index = i

            self.active_file_index = self.files.index(active_path) if active_path in self.files else None
            self.currently_playing_index = self.files.index(playing_path) if playing_path in self.files else None

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

            logger.debug(f"Reordered file from index {old_index} to {new_index}")
            return True

        except (ValueError, IndexError, TypeError, RuntimeError) as e:
            logger.error(f"Error reordering files: {e}")
            return False

    def _on_row_drag_enter(self, drop_target, x, y):
        """Handle drag enter for row reordering."""
        # Don't add visual feedback to avoid background color change
        return Gdk.DragAction.MOVE

    def _on_row_drag_leave(self, drop_target):
        """Handle drag leave for row reordering."""
        # No cleanup needed since we don't add visual feedback

    # Drop zone visual feedback
    def _on_drop_enter(self, drop_target, x, y):
        """Handle drag enter events with visual feedback."""
        if self.placeholder.get_parent():
            self.placeholder.add_css_class("drag-highlight")
        else:
            self.file_list.add_css_class("drag-highlight")
        return Gdk.DragAction.COPY

    def _on_drop_leave(self, drop_target):
        """Handle drag leave events."""
        if self.placeholder.get_parent():
            self.placeholder.remove_css_class("drag-highlight")
        else:
            self.file_list.remove_css_class("drag-highlight")

    def connect_file_removed_signal(self, callback):
        """Connect a callback to be notified when a file is removed."""
        self.file_removed_signal = callback

    def _apply_row_tooltips(self, row):
        """Apply custom tooltips to a file queue row."""
        if not hasattr(self, '_tooltip_helper') or not self._tooltip_helper:
            return
        
        # Apply tooltip to play button
        if hasattr(row, '_play_button'):
            self._tooltip_helper.add_tooltip(row._play_button, "play_this_file")
        
        # Apply tooltip to remove button
        if hasattr(row, '_remove_button'):
            self._tooltip_helper.add_tooltip(row._remove_button, "remove_from_queue")
        
        # Apply tooltip to filename label (will be applied when row is realized)
        if hasattr(row, '_title_label') and row._title_label:
            self._tooltip_helper.add_tooltip(row._title_label, "right_click_options")
