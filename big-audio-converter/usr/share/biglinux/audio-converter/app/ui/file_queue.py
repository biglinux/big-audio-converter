"""
File queue UI component for managing files to be converted.
"""

import gettext
import json
import logging
import os
import queue
import subprocess
import threading
import time
import uuid
import weakref
from app.audio.models import MediaSource
from app.audio.media_probe import audio_stream, media_duration
from app.audio.media_tasks import MediaTasks
from app.utils.main_context import SourceGroup

import gi

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

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
        menu_model = Gio.Menu()
        menu_model.append(_("Delete File"), "row.delete")
        menu_model.append(_("Open Containing Folder"), "row.open_folder")
        menu_model.append(_("More Information..."), "row.info")
        self._menu = Gtk.PopoverMenu.new_from_model(menu_model)
        button = Gtk.MenuButton(icon_name="view-more-symbolic", popover=self._menu, valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.update_property([Gtk.AccessibleProperty.LABEL], [_("File options")])
        self.add_suffix(button)
        actions = Gio.SimpleActionGroup()
        self._delete_action = Gio.SimpleAction.new("delete", None)
        self._delete_action.connect("activate", lambda action, parameter: self.on_delete_callback(self.index, self.file_path))
        actions.add_action(self._delete_action)
        for name, callback in (("open_folder", self._on_open_folder), ("info", self._on_show_info)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            actions.add_action(action)
        self.insert_action_group("row", actions)
        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", lambda gesture, count, x, y: self._menu.popup())
        self.add_controller(gesture)
        self._info_requests = {}

    def _on_row_realized(self, widget):
        """Add tooltip to the title label after the row is realized."""

        # The ActionRow creates internal widgets, we need to find the title label
        # In Adwaita, the title is typically in a Box containing labels
        def find_title_label(widget):
            """Recursively find the title label widget."""
            if isinstance(widget, Gtk.Label):
                # Check if this label's text matches our title
                if widget.get_label() == self.get_title():
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
        source = getattr(self, "media_source", MediaSource.resolve(self.file_path))
        try:
            parent = Gio.File.new_for_path(source.path).get_parent()
            Gio.AppInfo.launch_default_for_uri(parent.get_uri(), None)
        except Exception as error:
            owner = self.queue_owner() if hasattr(self, "queue_owner") else None
            if owner and owner._parent_window:
                owner._parent_window._show_error_dialog(_("Could not open the folder"), str(error))

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

    @staticmethod
    def _find_audio_stream(data, file_path, is_video_track):
        """Find the correct audio stream from ffprobe data."""
        if "streams" not in data:
            return None
        if is_video_track:
            try:
                track_part = file_path.split("::")[-1]
                track_num = int(track_part.split("track")[-1].split(".")[0])
                audio_streams = [s for s in data["streams"] if s.get("codec_type") == "audio"]
                if track_num <= len(audio_streams):
                    return audio_streams[track_num - 1]
            except Exception:
                pass
        # Fallback: first audio stream, or first stream
        for s in data["streams"]:
            if s.get("codec_type") == "audio":
                return s
        return data["streams"][0] if data["streams"] else None

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
                    props.append(("Sample Rate", _("{rate} Hz").format(rate=int(audio_stream["sample_rate"]))))
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
            except Exception:
                pass
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
        owner = self.queue_owner() if hasattr(self, "queue_owner") else None
        if owner is None or owner._disposed:
            return
        source = self.media_source
        dialog = Adw.Dialog(title=_("File Information"), content_width=640, content_height=540)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        copy_button = Gtk.Button(icon_name="edit-copy-symbolic", sensitive=False)
        copy_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Copy information to clipboard")])
        header.pack_end(copy_button)
        toolbar.add_top_bar(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_start=16, margin_end=16, margin_top=12, margin_bottom=16)
        content.append(Gtk.Label(label=source.path, xalign=0, wrap=True, selectable=True))
        loading = Gtk.Label(label=_("Analyzing audio…"), xalign=0)
        content.append(loading)
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_child(content)
        toolbar.set_content(scrolled)
        dialog.set_child(toolbar)
        request_id = uuid.uuid4().hex
        self._info_requests[request_id] = dialog
        def closed(dialog):
            self._info_requests.pop(request_id, None)
            owner._media_tasks.cancel(request_id)
        dialog.connect("closed", closed)
        def completed(info, error):
            if request_id not in self._info_requests or owner._disposed:
                return
            content.remove(loading)
            if error or info is None:
                content.append(Gtk.Label(label=_("Media information is unavailable. Check that the file still exists and is readable."), wrap=True))
                return
            try:
                stream = audio_stream(info, source.stream_index)
                properties = self._extract_audio_props(stream, info, source.stream_index is not None, source.path)
                content.append(self._create_info_group(_("Audio Properties"), properties))
                tags = dict(info.get("format", {}).get("tags", {}))
                tags.update(stream.get("tags", {}))
                items = [(str(key)[:128], str(value)[:2048]) for key, value in list(tags.items())[:200]]
                if items:
                    content.append(self._create_info_group(_("Metadata Tags"), items))
                if len(tags) > 200 or any(len(str(value)) > 2048 for value in tags.values()):
                    content.append(Gtk.Label(label=_("Long metadata is shortened for display. Copy retains the complete values."), wrap=True))
                clipboard_text = source.path + "\n" + "\n".join(f"{key}: {value}" for key, value in properties)
                clipboard_text += "\n" + "\n".join(f"{key}: {value}" for key, value in tags.items())
                copy_button.connect("clicked", lambda button: Gdk.Display.get_default().get_clipboard().set(clipboard_text))
                copy_button.set_sensitive(True)
            except (ValueError, TypeError, KeyError) as error:
                content.append(Gtk.Label(label=_("No readable audio stream was found."), wrap=True))
        dialog.present(owner._parent_window)
        owner._media_tasks.submit(request_id, source, completed)

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
                metadata_items.append((label, tags[tag_key]))
        for key, value in tags.items():
            if key.lower() not in tag_order:
                label = key.replace("_", " ").title()
                metadata_items.append((label, value))
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
            row.set_title(label)

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
        logger.info(f"Value copied to clipboard: {value}")

    def _copy_to_clipboard(self, text, dialog):
        """Copy text to clipboard."""
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(text)
        logger.info("Information copied to clipboard")

    def set_metadata(self, metadata_text):
        """Media tags are untrusted text, never application markup."""
        self.set_subtitle(GLib.markup_escape_text(str(metadata_text)))

    def update_progress(self, progress):
        """Update the progress bar."""
        self.progress_bar.set_fraction(progress)
        self.progress_bar.set_text(f"{int(progress * 100)}%")
        self.progress_bar.set_visible(True)  # Make visible during conversion

    def cleanup(self):
        owner = self.queue_owner() if hasattr(self, "queue_owner") else None
        for identity, dialog in list(self._info_requests.items()):
            if owner is not None:
                owner._media_tasks.cancel(identity)
            dialog.close()
        self._info_requests.clear()
        self._menu.popdown()


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
        self._disposed = False
        self._sources = SourceGroup()
        self._media_tasks = MediaTasks(self.converter.ffmpeg_path)
        self._rows_by_id = {}

    def _setup_styles(self):
        """Set up custom CSS styles for the file list."""
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(
            b"""
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
            -1,
        )

        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _is_valid_media_file_quick(self, file_path):
        """Quick check if file is likely a media file by extension."""
        ext = os.path.splitext(file_path)[1].lower()
        valid_extensions = [
            # Audio formats
            ".mp3",
            ".wav",
            ".ogg",
            ".flac",
            ".m4a",
            ".aac",
            ".opus",
            ".wma",
            ".aiff",
            ".ape",
            ".alac",
            ".dsd",
            ".dsf",
            ".mka",
            ".oga",
            ".spx",
            ".tta",
            ".wv",
            ".eac3",  # Dolby Digital Plus / E-AC-3
            ".ac3",  # Dolby Digital / AC-3
            ".dts",  # DTS audio
            # Video formats (can be converted to audio)
            ".mp4",
            ".mkv",
            ".avi",
            ".mov",
            ".wmv",
            ".flv",
            ".webm",
            ".m4v",
            ".mpg",
            ".mpeg",
            ".3gp",
            ".ogv",
            ".ts",
            ".mts",
            ".m2ts",
        ]
        return ext in valid_extensions

    def _is_video_file(self, file_path):
        """Check if file is a video file by extension.

        Args:
            file_path: Path to the file to check

        Returns:
            bool: True if file has video extension, False otherwise
        """
        ext = os.path.splitext(file_path)[1].lower()
        video_extensions = [
            ".mp4",
            ".mkv",
            ".avi",
            ".mov",
            ".wmv",
            ".flv",
            ".webm",
            ".m4v",
            ".mpg",
            ".mpeg",
            ".3gp",
            ".ogv",
            ".ts",
            ".mts",
            ".m2ts",
        ]
        return ext in video_extensions

    def _get_audio_codec_extension(self, codec_name):
        """Map audio codec name to file extension.

        Args:
            codec_name: FFprobe codec name (e.g., 'aac', 'mp3', 'opus')

        Returns:
            str: File extension including dot (e.g., '.aac', '.mp3')
        """
        # Map common codecs to extensions
        codec_map = {
            "aac": ".aac",
            "mp3": ".mp3",
            "opus": ".opus",
            "vorbis": ".ogg",
            "flac": ".flac",
            "pcm_s16le": ".wav",
            "pcm_s24le": ".wav",
            "pcm_s32le": ".wav",
            "ac3": ".ac3",
            "eac3": ".eac3",
            "dts": ".dts",
            "truehd": ".thd",
            "alac": ".m4a",
            "wmav2": ".wma",
        }

        # Return mapped extension or default to .aac
        return codec_map.get(codec_name.lower(), ".mka")




    def add_file(self, file_path):
        """Display immediately; never run a decoder from a GTK event handler."""
        if self._disposed or len(self.files) >= 4096:
            return False
        try:
            source = MediaSource.resolve(file_path)
            if not os.path.isfile(source.path) or not self._is_valid_media_file_quick(source.path):
                return False
            if any(row.media_source.path == source.path for row in self.file_rows):
                return False
            row = self._create_media_row(source.path, source)
            self._media_tasks.submit(row.request_id, source,
                lambda info, error, identity=row.request_id: self._metadata_ready(identity, info, error))
            if not self._updates_suspended:
                self.update_queue_size_label()
            return True
        except (OSError, ValueError, TypeError) as error:
            logger.warning("The media could not be queued: %s", error)
            return False



    def update_queue_size_label(self):
        text = self.get_queue_size_text()
        self.queue_size_label.set_text(text)
        if callable(getattr(self, "on_queue_size_changed", None)):
            self.on_queue_size_changed(len(self.files), text)
        window = self._parent_window
        if window is not None:
            pending = any(not row.metadata_ready for row in self.file_rows)
            session = getattr(window, "conversion_session", None)
            window.convert_button.set_sensitive(not pending and not (session and session.running))

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
        """Delete a file permanently with confirmation dialog."""
        # Get parent window for dialog
        parent = self._parent_window
        if not parent:
            # Try to find the parent window
            widget = self.get_parent()
            while widget:
                if isinstance(widget, Gtk.Window):
                    parent = widget
                    break
                widget = widget.get_parent()

        # Create confirmation dialog
        filename = os.path.basename(file_path)
        dialog = Adw.MessageDialog(
            transient_for=parent,
            heading=_("Delete File Permanently?"),
            body=_(
                "Are you sure you want to delete '{0}'? This action cannot be undone and the file will be permanently deleted from your disk."
            ).format(filename),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        # Connect response handler
        dialog.connect("response", self._on_delete_response, index, file_path)
        dialog.present()

    def _on_delete_response(self, dialog, response, index, file_path):
        if response != "delete" or file_path not in self.files:
            return
        current = self.files.index(file_path)
        row = self.file_rows[current]
        if row.media_source.stream_index is not None:
            return
        try:
            os.remove(row.media_source.path)
            self.remove_file(current)
        except OSError as error:
            if self._parent_window is not None:
                self._parent_window._show_error_dialog(_("Error Deleting File"), str(error))

    def remove_file(self, index):
        if not 0 <= index < len(self.files):
            return False
        active, playing = self._selected_identifiers()
        row = self.file_rows[index]
        identifier = row.file_path
        self._media_tasks.cancel(row.request_id)
        self._rows_by_id.pop(row.request_id, None)
        self.track_metadata.pop(identifier, None)
        if identifier == playing and callable(self.on_stop_playback):
            self.on_stop_playback()
        if identifier in (active, playing) and callable(self.on_playing_file_removed):
            self.on_playing_file_removed()
        self.files.pop(index)
        self.file_rows.pop(index)
        self.file_list.remove(row)
        row.cleanup()
        self._restore_identifiers(active, playing)
        self.update_queue_size_label()
        if self.file_removed_signal:
            self.file_removed_signal(identifier)
        return True

    def on_clear_queue(self, button):
        """Clear the entire queue."""
        self.clear_queue()

    def clear_queue(self):
        if callable(self.on_stop_playback):
            self.on_stop_playback()
        identifiers = list(self.files)
        for row in self.file_rows:
            self._media_tasks.cancel(row.request_id)
            row.cleanup()
            self.file_list.remove(row)
        self.files.clear()
        self.file_rows.clear()
        self._rows_by_id.clear()
        self.track_metadata.clear()
        self.currently_playing_index = None
        self.active_file_index = None
        if not self._disposed:
            self.update_queue_size_label()
            for identifier in identifiers:
                if self.file_removed_signal:
                    self.file_removed_signal(identifier)

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
                    if row.request_id in self._rows_by_id:
                        row.progress_bar.set_visible(False)
                    return False

                self._sources.timeout(1500, hide_progress)

    def on_play_file(self, file_path, index):
        """Handle play button click on a file."""
        # This will be implemented by the main window and connected
        pass

    def on_stop_playback(self):
        """Stop playback of the current file.

        This method should be overridden by the main window or other component
        that controls audio playback.
        """
        pass

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
        """Handle dropped files (multiple file support).
        Args:
            drop_target: The drop target controller.
            value: The dropped value (Gdk.FileList).
            x: X coordinate.
            y: Y coordinate.
        Returns:
            bool: True if files were added, False otherwise.
        """
        # Clear drag highlight immediately when drop occurs
        if self.placeholder.get_parent():
            self.placeholder.remove_css_class("drag-highlight")
        else:
            self.file_list.remove_css_class("drag-highlight")

        file_list = value
        if file_list:
            for file in file_list:
                if hasattr(file, "get_path"):
                    file_path = file.get_path()
                    if file_path:
                        self.add_file(file_path)
            return True
        return False

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

            return self.move_file(old_index, min(new_index, len(self.files) - 1))

        except Exception as e:
            logger.error(f"Error reordering files: {e}")
            return False

    def _on_row_drag_enter(self, drop_target, x, y):
        """Handle drag enter for row reordering."""
        # Don't add visual feedback to avoid background color change
        return Gdk.DragAction.MOVE

    def _on_row_drag_leave(self, drop_target):
        """Handle drag leave for row reordering."""
        # No cleanup needed since we don't add visual feedback
        pass

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

    def _create_media_row(self, identifier, source, index=None):
        if index is None:
            index = len(self.files)
        row = FileQueueRow(identifier, index, self.on_remove_file, self.on_play_file,
                           self.on_delete_file, self.on_activate_file)
        row.request_id = uuid.uuid4().hex
        row.media_source = source
        row._delete_action.set_enabled(source.stream_index is None)
        row.queue_owner = weakref.ref(self)
        row.metadata_ready = False
        row.set_activatable(False)
        row.play_button.set_sensitive(False)
        row.set_metadata(_("Analyzing audio…"))
        self.files.insert(index, identifier)
        self.file_rows.insert(index, row)
        self._rows_by_id[row.request_id] = row
        self.file_list.insert(row, index)
        self._apply_row_tooltips(row)
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect("prepare", self._on_row_drag_prepare, row)
        drag.connect("drag-begin", self._on_row_drag_begin, row)
        row.add_controller(drag)
        for position, item in enumerate(self.file_rows):
            item.index = position
        return row

    def _fill_media_row(self, row, info, stream):
        row.metadata_ready = True
        row.set_activatable(True)
        row.play_button.set_sensitive(True)
        duration = media_duration(info, stream)
        parts = [stream.get("codec_name", "").upper()]
        if duration:
            parts.append(FileQueueRow._format_duration(duration))
        if stream.get("sample_rate"):
            parts.append(_("{rate} Hz").format(rate=int(stream["sample_rate"])))
        channels = int(stream.get("channels", 0))
        parts.append(gettext.ngettext("{count} channel", "{count} channels", channels).format(count=channels))
        tags = stream.get("tags", {})
        if tags.get("language"):
            parts.append(str(tags["language"])[:32])
        if tags.get("title"):
            parts.append(str(tags["title"])[:160])
        row.set_metadata(" · ".join(parts))

    def _metadata_ready(self, identity, info, error):
        row = self._rows_by_id.get(identity)
        if self._disposed or row is None:
            return
        streams = [stream for stream in (info or {}).get("streams", []) if stream.get("codec_type") == "audio"]
        if error or not streams or len(streams) > 256:
            row.metadata_ready = True
            row.set_metadata(_("Audio unavailable. Check the file or remove it from the queue."))
            self.update_queue_size_label()
            return
        if len(streams) > 1:
            active = self.files[self.active_file_index] if self.active_file_index is not None else None
            playing = self.files[self.currently_playing_index] if self.currently_playing_index is not None else None
            index = self.file_rows.index(row)
            self._rows_by_id.pop(identity)
            self.file_rows.pop(index)
            self.files.pop(index)
            self.file_list.remove(row)
            row.cleanup()
            for offset, stream in enumerate(streams):
                extension = self._get_audio_codec_extension(stream.get("codec_name", ""))
                identifier = f"{row.media_source.path}::track{offset + 1}{extension}"
                source = MediaSource(row.media_source.path, stream["index"])
                self.track_metadata[identifier] = dict(source_video=source.path, track_index=source.stream_index,
                    codec=stream.get("codec_name", ""), channels=stream.get("channels", 0),
                    sample_rate=stream.get("sample_rate", ""), bitrate=stream.get("bit_rate", ""),
                    language=str(stream.get("tags", {}).get("language", ""))[:32],
                    title=str(stream.get("tags", {}).get("title", ""))[:160])
                item = self._create_media_row(identifier, source, index + offset)
                name = _("{name} — audio track {number}").format(name=os.path.basename(source.path), number=offset + 1)
                item.set_title(GLib.markup_escape_text(name))
                self._fill_media_row(item, info, stream)
            self.active_file_index = self.files.index(active) if active in self.files else None
            self.currently_playing_index = self.files.index(playing) if playing in self.files else None
        else:
            self._fill_media_row(row, info, streams[0])
        self.update_queue_size_label()
        if self.active_file_index is None and self.file_rows and self.file_rows[0].play_button.get_sensitive():
            first = self.file_rows[0]
            if self.on_file_added_to_empty_queue:
                self.on_file_added_to_empty_queue(first.file_path, 0)

    def _selected_identifiers(self):
        def identify(index):
            return self.files[index] if index is not None and 0 <= index < len(self.files) else None
        return identify(self.active_file_index), identify(self.currently_playing_index)

    def _restore_identifiers(self, active, playing):
        for index, row in enumerate(self.file_rows):
            row.index = index
        self.active_file_index = self.files.index(active) if active in self.files else None
        self.currently_playing_index = self.files.index(playing) if playing in self.files else None
        window = self._parent_window
        if window is not None:
            window.current_file_index = self.files.index(window.current_file) if window.current_file in self.files else -1

    def move_file(self, old_index, new_index):
        if not 0 <= old_index < len(self.files) or not 0 <= new_index < len(self.files):
            return False
        active, playing = self._selected_identifiers()
        row = self.file_rows.pop(old_index)
        identifier = self.files.pop(old_index)
        self.files.insert(new_index, identifier)
        self.file_rows.insert(new_index, row)
        self.file_list.remove(row)
        self.file_list.insert(row, new_index)
        row.remove_css_class("drag-row")
        self._restore_identifiers(active, playing)
        return True

    def cleanup(self):
        if self._disposed:
            return
        self._disposed = True
        self._media_tasks.close()
        self._sources.close()
        self.clear_queue()
        self._parent_window = None
        self._tooltip_helper = None
