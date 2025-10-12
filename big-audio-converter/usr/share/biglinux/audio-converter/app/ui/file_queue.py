"""
File queue UI component for managing files to be converted.
"""

import gettext
import gi
import os

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GLib, Adw
import logging

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
        self.play_button.set_tooltip_text("Play this file")
        self.play_button.add_css_class("flat")
        self.play_button.set_valign(Gtk.Align.CENTER)
        self.play_button.connect(
            "clicked", lambda btn: self.on_play_callback(self.file_path, self.index)
        )
        self.add_prefix(self.play_button)

        # Remove from queue button (left side, after play button)
        remove_button = Gtk.Button.new_from_icon_name("edit-delete-remove")
        remove_button.set_tooltip_text("Remove from queue")
        remove_button.add_css_class("flat")
        remove_button.set_valign(Gtk.Align.CENTER)
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

    def _setup_context_menu(self):
        """Setup right-click context menu for the file row."""
        from gi.repository import Gio

        # Create popup menu
        menu = Gtk.PopoverMenu()
        menu_model = Gio.Menu()

        # Delete file from filesystem action
        menu_model.append(_("Delete File"), "row.delete")

        # Open containing folder action
        menu_model.append(_("Open Containing Folder"), "row.open_folder")

        # More information action
        menu_model.append(_("More Information..."), "row.info")

        menu.set_menu_model(menu_model)
        menu.set_parent(self)

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

        # Add right-click gesture
        right_click = Gtk.GestureClick.new()
        right_click.set_button(3)  # Right mouse button
        right_click.connect("pressed", lambda g, n, x, y: menu.popup())
        self.add_controller(right_click)

    def _on_open_folder(self, action, param):
        """Open the folder containing the file."""
        import subprocess

        # Handle virtual track paths (contain ::)
        actual_path = (
            self.file_path.split("::")[0] if "::" in self.file_path else self.file_path
        )

        if os.path.isfile(actual_path):
            folder_path = os.path.dirname(actual_path)
            try:
                # Open file manager at folder location
                subprocess.Popen(["xdg-open", folder_path])
            except Exception as e:
                logger.error(f"Failed to open folder: {e}")

    def _on_show_info(self, action, param):
        """Show detailed information dialog about the file."""
        import subprocess
        import json

        # Get parent window
        widget = self.get_parent()
        while widget and not isinstance(widget, Gtk.Window):
            widget = widget.get_parent()

        # Handle virtual track paths
        is_video_track = "::" in self.file_path
        actual_path = (
            self.file_path.split("::")[0] if is_video_track else self.file_path
        )

        if not os.path.isfile(actual_path):
            return

        # Create dialog window
        dialog = Gtk.Window(
            transient_for=widget,
            modal=True,
            title=_("File Information"),
            default_width=800,
            default_height=600,
        )

        # Create header bar
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)

        # Copy button
        copy_button = Gtk.Button()
        copy_button.set_icon_name("edit-copy-symbolic")
        copy_button.set_tooltip_text(_("Copy information to clipboard"))
        copy_button.add_css_class("flat")
        header.pack_end(copy_button)

        dialog.set_titlebar(header)

        # Create scrolled window for content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        dialog.set_child(scrolled)

        # Create main content box
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scrolled.set_child(main_box)

        # File name as title (large, prominent)
        title_label = Gtk.Label()
        title_label.set_markup(
            f"<span size='large' weight='bold'>{GLib.markup_escape_text(os.path.basename(self.file_path))}</span>"
        )
        title_label.set_wrap(True)
        title_label.set_margin_top(24)
        title_label.set_margin_bottom(12)
        title_label.set_margin_start(24)
        title_label.set_margin_end(24)
        main_box.append(title_label)

        # File path (secondary text)
        path_label = Gtk.Label()
        path_label.set_text(actual_path)
        path_label.set_wrap(True)
        path_label.set_xalign(0)
        path_label.add_css_class("dim-label")
        path_label.set_margin_bottom(24)
        path_label.set_margin_start(24)
        path_label.set_margin_end(24)
        main_box.append(path_label)

        # Store all info for copying
        info_text = []
        info_text.append(f"File: {os.path.basename(self.file_path)}")
        info_text.append(f"Path: {actual_path}")
        info_text.append("")

        # Get file basic info
        size_str = "Unknown"
        basic_items = []

        # We'll calculate size from ffprobe data regardless of file type
        size_str = "Calculating..."
        if is_video_track:
            basic_items = [("Size (Audio Track)", size_str)]
        else:
            basic_items = [("Size", size_str)]

        # Create groups for different types of information
        # We'll update basic_group after getting stream info
        basic_group = None

        # Get complete metadata using ffprobe
        try:
            cmd = [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                actual_path,
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            if result.returncode == 0:
                data = json.loads(result.stdout)

                # Audio properties group - will be built in specific order
                audio_props = []

                # Stream information - get this first to calculate size
                audio_stream = None
                if "streams" in data:
                    # Find the correct audio stream
                    if is_video_track:
                        # Extract track number from virtual path (format: ...::track1.ext)
                        try:
                            track_part = self.file_path.split("::")[-1]
                            track_num = int(track_part.split("track")[-1].split(".")[0])
                            # Get all audio streams
                            audio_streams = [
                                s
                                for s in data["streams"]
                                if s.get("codec_type") == "audio"
                            ]
                            if track_num <= len(audio_streams):
                                audio_stream = audio_streams[track_num - 1]
                        except:
                            # Fallback to first audio stream
                            for s in data["streams"]:
                                if s.get("codec_type") == "audio":
                                    audio_stream = s
                                    break
                    else:
                        # For regular files, get first audio stream
                        for s in data["streams"]:
                            if s.get("codec_type") == "audio":
                                audio_stream = s
                                break
                        if not audio_stream and len(data["streams"]) > 0:
                            audio_stream = data["streams"][0]

                # 1. Calculate and add SIZE first
                size_calculated = False
                if audio_stream:
                    # Try to get duration and bitrate from tags first (more accurate for video files)
                    duration_value = None
                    bitrate_value = None

                    # Check tags for language-specific values
                    if "tags" in audio_stream:
                        tags = audio_stream["tags"]
                        # Find DURATION-* and BPS-* tags (or DURATION/BPS without suffix)
                        for key, value in tags.items():
                            if key.startswith("DURATION-") or key == "DURATION":
                                # Parse duration like "00:20:55.072000000"
                                try:
                                    time_parts = value.split(":")
                                    if len(time_parts) == 3:
                                        hours = int(time_parts[0])
                                        minutes = int(time_parts[1])
                                        seconds = float(time_parts[2])
                                        duration_value = (
                                            hours * 3600 + minutes * 60 + seconds
                                        )
                                except:
                                    pass
                            elif key.startswith("BPS-") or key == "BPS":
                                try:
                                    bitrate_value = int(value)
                                except:
                                    pass

                    # Fallback to stream-level values if tags not found
                    if duration_value is None and "duration" in audio_stream:
                        duration_value = float(audio_stream["duration"])
                    if bitrate_value is None and "bit_rate" in audio_stream:
                        bitrate_value = int(audio_stream["bit_rate"])

                    # Calculate size if we have both values
                    if duration_value is not None and bitrate_value is not None:
                        try:
                            size_bytes = int((duration_value * bitrate_value) / 8)

                            if size_bytes < 1024:
                                size_str = f"{size_bytes} B"
                            elif size_bytes < 1024 * 1024:
                                size_str = f"{size_bytes / 1024:.1f} KB"
                            elif size_bytes < 1024 * 1024 * 1024:
                                size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
                            else:
                                size_str = f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

                            audio_props.append(("Size", size_str))
                            size_calculated = True
                        except:
                            pass

                if not size_calculated:
                    # Only use file size as fallback for non-video files
                    if not is_video_track:
                        try:
                            size_bytes = os.path.getsize(actual_path)
                            if size_bytes < 1024:
                                size_str = f"{size_bytes} B"
                            elif size_bytes < 1024 * 1024:
                                size_str = f"{size_bytes / 1024:.1f} KB"
                            elif size_bytes < 1024 * 1024 * 1024:
                                size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
                            else:
                                size_str = f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
                            audio_props.append(("Size", size_str))
                        except:
                            audio_props.append(("Size", "Unknown"))
                    else:
                        audio_props.append(("Size", "Unknown"))

                # 2. Add DURATION
                duration_added = False
                if audio_stream and "tags" in audio_stream:
                    # Try to get duration from tags first
                    for key, value in audio_stream["tags"].items():
                        if key.startswith("DURATION-") or key == "DURATION":
                            # Parse and format duration
                            try:
                                time_parts = value.split(":")
                                if len(time_parts) == 3:
                                    hours = int(time_parts[0])
                                    minutes = int(time_parts[1])
                                    seconds = float(time_parts[2])
                                    total_secs = hours * 3600 + minutes * 60 + seconds

                                    hours_int = int(total_secs // 3600)
                                    minutes_int = int((total_secs % 3600) // 60)
                                    seconds_int = int(total_secs % 60)

                                    if hours_int > 0:
                                        duration_str = f"{hours_int}:{minutes_int:02d}:{seconds_int:02d}"
                                    else:
                                        duration_str = (
                                            f"{minutes_int}:{seconds_int:02d}"
                                        )
                                    audio_props.append(("Duration", duration_str))
                                    duration_added = True
                                    break
                            except:
                                pass

                if not duration_added:
                    # Fallback to format duration
                    if "format" in data and "duration" in data["format"]:
                        duration_secs = float(data["format"]["duration"])
                        hours = int(duration_secs // 3600)
                        minutes = int((duration_secs % 3600) // 60)
                        seconds = int(duration_secs % 60)
                        if hours > 0:
                            duration_str = f"{hours}:{minutes:02d}:{seconds:02d}"
                        else:
                            duration_str = f"{minutes}:{seconds:02d}"
                        audio_props.append(("Duration", duration_str))

                # 3. Add FORMAT (codec)
                if audio_stream and "codec_long_name" in audio_stream:
                    audio_props.append(("Format", audio_stream["codec_long_name"]))
                elif "format" in data and "format_long_name" in data["format"]:
                    audio_props.append(("Format", data["format"]["format_long_name"]))

                # 4. Add BITRATE
                bitrate_added = False
                if audio_stream and "tags" in audio_stream:
                    # Try to get bitrate from tags first
                    for key, value in audio_stream["tags"].items():
                        if key.startswith("BPS-") or key == "BPS":
                            try:
                                bitrate = int(value) // 1000
                                audio_props.append(("Bitrate", f"{bitrate} kbps"))
                                bitrate_added = True
                                break
                            except:
                                pass

                if not bitrate_added:
                    if audio_stream and "bit_rate" in audio_stream:
                        bitrate = int(audio_stream["bit_rate"]) // 1000
                        audio_props.append(("Bitrate", f"{bitrate} kbps"))
                    elif "format" in data and "bit_rate" in data["format"]:
                        bitrate = int(data["format"]["bit_rate"]) // 1000
                        audio_props.append(("Bitrate", f"{bitrate} kbps"))

                # 5. Add other audio stream properties
                if audio_stream:
                    if "sample_rate" in audio_stream:
                        sample_rate = int(audio_stream["sample_rate"]) // 1000
                        audio_props.append(("Sample Rate", f"{sample_rate} kHz"))

                    if "channels" in audio_stream:
                        channels = audio_stream["channels"]
                        channel_layout = audio_stream.get("channel_layout", "")
                        if channel_layout:
                            audio_props.append((
                                "Channels",
                                f"{channels} ({channel_layout})",
                            ))
                        else:
                            audio_props.append(("Channels", str(channels)))

                    if (
                        "bits_per_sample" in audio_stream
                        and audio_stream["bits_per_sample"] > 0
                    ):
                        audio_props.append((
                            "Bit Depth",
                            f"{audio_stream['bits_per_sample']} bit",
                        ))

                # Add basic group with file path info
                basic_items = [("Path", actual_path)]
                basic_group = self._create_info_group(_("File Details"), basic_items)
                main_box.append(basic_group)

                if audio_props:
                    audio_group = self._create_info_group(
                        _("Audio Properties"), audio_props
                    )
                    main_box.append(audio_group)

                # Metadata tags group
                if "format" in data and "tags" in data["format"]:
                    tags = data["format"]["tags"]
                    metadata_items = []

                    # Order common tags first
                    tag_order = [
                        "title",
                        "artist",
                        "album",
                        "album_artist",
                        "date",
                        "genre",
                        "track",
                        "disc",
                        "comment",
                        "composer",
                        "performer",
                        "copyright",
                        "encoded_by",
                        "encoder",
                    ]

                    # Add common tags in order
                    for tag_key in tag_order:
                        if tag_key in tags:
                            label = tag_key.replace("_", " ").title()
                            metadata_items.append((label, tags[tag_key]))

                    # Add any other tags
                    for key, value in tags.items():
                        if key.lower() not in tag_order:
                            label = key.replace("_", " ").title()
                            metadata_items.append((label, value))

                    if metadata_items:
                        metadata_group = self._create_info_group(
                            _("Metadata Tags"), metadata_items
                        )
                        main_box.append(metadata_group)

        except subprocess.TimeoutExpired:
            error_label = Gtk.Label(label=_("Timeout getting metadata"))
            error_label.add_css_class("dim-label")
            error_label.set_margin_top(12)
            error_label.set_margin_bottom(12)
            main_box.append(error_label)
        except json.JSONDecodeError:
            error_label = Gtk.Label(label=_("Failed to parse metadata"))
            error_label.add_css_class("dim-label")
            error_label.set_margin_top(12)
            error_label.set_margin_bottom(12)
            main_box.append(error_label)
        except Exception as e:
            logger.error(f"Error getting metadata: {e}")
            error_label = Gtk.Label(label=_("Error: {0}").format(str(e)))
            error_label.add_css_class("dim-label")
            error_label.set_margin_top(12)
            error_label.set_margin_bottom(12)
            main_box.append(error_label)

        # Connect copy button to copy all info
        clipboard_text = "\n".join(info_text)
        copy_button.connect(
            "clicked", lambda btn: self._copy_to_clipboard(clipboard_text, dialog)
        )

        dialog.present()

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
        self._metadata_queue = []  # Files waiting for metadata processing
        self._metadata_thread = None  # Background thread for metadata
        self._parent_window = None  # Will be set by MainWindow for dialogs

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
        return codec_map.get(codec_name.lower(), ".aac")

    def _get_audio_tracks(self, file_path):
        """Extract audio track information from video file using ffprobe.

        Args:
            file_path: Path to video file

        Returns:
            list: List of dictionaries with track info:
                  [{'index': 0, 'codec': 'aac', 'channels': 2, 'language': 'eng', 'title': 'Stereo'}, ...]
                  Returns empty list if no tracks found or on error
        """
        import subprocess
        import json

        try:
            # Run ffprobe to get stream information in JSON format
            cmd = [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-select_streams",
                "a",  # Only audio streams
                file_path,
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            if result.returncode != 0:
                logger.error(f"ffprobe failed for {file_path}: {result.stderr}")
                return []

            # Parse JSON output
            data = json.loads(result.stdout)
            streams = data.get("streams", [])

            if not streams:
                logger.info(f"No audio tracks found in {file_path}")
                return []

            # Extract track information
            tracks = []
            for stream in streams:
                track_info = {
                    "index": stream.get("index", 0),
                    "codec": stream.get("codec_name", "unknown"),
                    "channels": stream.get("channels", 0),
                    "sample_rate": stream.get("sample_rate", ""),
                    "bitrate": stream.get("bit_rate", ""),
                    "language": stream.get("tags", {}).get("language", ""),
                    "title": stream.get("tags", {}).get("title", ""),
                }
                tracks.append(track_info)

            logger.info(f"Found {len(tracks)} audio track(s) in {file_path}")
            return tracks

        except subprocess.TimeoutExpired:
            logger.error(f"ffprobe timeout for {file_path}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse ffprobe JSON output: {e}")
            return []
        except Exception as e:
            logger.error(f"Error extracting audio tracks from {file_path}: {e}")
            return []

    def _get_track_duration(self, file_path):
        """Get the duration of a video/audio file in seconds.

        Args:
            file_path: Path to the video/audio file

        Returns:
            float: Duration in seconds, or 0 if unable to determine
        """
        import subprocess
        import json

        try:
            cmd = [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                file_path,
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

            if result.returncode == 0:
                data = json.loads(result.stdout)
                if "format" in data and "duration" in data["format"]:
                    return float(data["format"]["duration"])
        except:
            pass

        return 0

    def _add_track_entry(self, video_file, track_info, track_display_index):
        """Add a single audio track from a video file to the queue.

        Args:
            video_file: Path to source video file
            track_info: Dictionary with track information (index, codec, channels, etc.)
            track_display_index: Display index (1-based) for user-friendly naming

        Returns:
            bool: True if track was added successfully
        """
        try:
            # Generate a virtual file path for this track
            # Format: video_filename_track1.extension
            video_basename = os.path.splitext(os.path.basename(video_file))[0]
            extension = self._get_audio_codec_extension(track_info["codec"])
            virtual_path = f"{video_file}::track{track_display_index}{extension}"

            # Check if this track is already in queue
            normalized_path = os.path.abspath(virtual_path)
            for existing_file in self.files:
                if os.path.abspath(existing_file) == normalized_path:
                    logger.debug(f"Track already in queue: {virtual_path}")
                    return False

            # Add to internal file list
            self.files.append(virtual_path)
            file_index = len(self.files) - 1

            # Store track metadata
            self.track_metadata[virtual_path] = {
                "source_video": video_file,
                "track_index": track_info["index"],
                "codec": track_info["codec"],
                "channels": track_info["channels"],
                "sample_rate": track_info.get("sample_rate", ""),
                "bitrate": track_info.get("bitrate", ""),
                "language": track_info.get("language", ""),
                "title": track_info.get("title", ""),
            }

            # Create row with track-specific title
            row = FileQueueRow(
                virtual_path,
                file_index,
                self.on_remove_file,
                self.on_play_file,
                self.on_delete_file,
                self.on_activate_file,
            )

            # Set initial title with track indicator (escape special characters)
            track_title = f"🎬 {video_basename}_track{track_display_index}{extension}"
            row.set_title(GLib.markup_escape_text(track_title))

            # Build subtitle with track info - similar to regular audio files
            subtitle_parts = []

            # 1. Calculate and add size from duration and bitrate if available
            if track_info.get("bitrate") and track_info.get("sample_rate"):
                try:
                    # Get duration from video file
                    duration_secs = self._get_track_duration(video_file)
                    if duration_secs > 0:
                        bitrate_val = int(track_info["bitrate"])
                        size_bytes = int((duration_secs * bitrate_val) / 8)

                        if size_bytes < 1024:
                            size_str = f"{size_bytes} B"
                        elif size_bytes < 1024 * 1024:
                            size_str = f"{size_bytes / 1024:.1f} KB"
                        elif size_bytes < 1024 * 1024 * 1024:
                            size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
                        else:
                            size_str = f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
                        subtitle_parts.append(size_str)

                        # 2. Add duration
                        hours = int(duration_secs // 3600)
                        minutes = int((duration_secs % 3600) // 60)
                        seconds = int(duration_secs % 60)
                        if hours > 0:
                            duration_str = f"{hours}:{minutes:02d}:{seconds:02d}"
                        else:
                            duration_str = f"{minutes}:{seconds:02d}"
                        subtitle_parts.append(duration_str)
                except:
                    pass

            # 3. Add codec (format) - without "Codec:" label
            subtitle_parts.append(track_info["codec"].upper())

            # 4. Add bitrate
            if track_info.get("bitrate"):
                try:
                    bitrate_kbps = int(track_info["bitrate"]) // 1000
                    subtitle_parts.append(f"{bitrate_kbps} kbps")
                except:
                    pass

            row.set_metadata(" • ".join(subtitle_parts))

            self.file_rows.append(row)
            self.file_list.append(row)

            # Enable drag source for reordering
            drag_source = Gtk.DragSource()
            drag_source.set_actions(Gdk.DragAction.MOVE)
            drag_source.connect("prepare", self._on_row_drag_prepare, row)
            drag_source.connect("drag-begin", self._on_row_drag_begin, row)
            row.add_controller(drag_source)

            logger.info(
                f"Added track {track_display_index} from {os.path.basename(video_file)}"
            )
            return True

        except Exception as e:
            logger.error(f"Error adding track entry: {e}")
            return False

    def add_file(self, file_path):
        """Add a file to the queue without blocking for metadata.

        If the file is a video with multiple audio tracks, each track will be added
        as a separate item in the queue.
        """
        try:
            # Check file existence
            if not os.path.isfile(file_path):
                return False

            # Quick validation by extension only (fast)
            if not self._is_valid_media_file_quick(file_path):
                logger.info(f"Skipping non-media file: {os.path.basename(file_path)}")
                return False

            # Normalize path for comparison
            normalized_path = os.path.abspath(file_path)

            # Check if file is already in queue
            for existing_file in self.files:
                # Skip track entries (they contain ::)
                if "::" in existing_file:
                    continue
                if os.path.abspath(existing_file) == normalized_path:
                    logger.debug(
                        f"File already in queue: {os.path.basename(file_path)}"
                    )
                    return False

            # Check if queue was empty before adding
            was_empty = len(self.files) == 0

            # Check if this is a video file with multiple audio tracks
            if self._is_video_file(file_path):
                logger.info(f"Detected video file: {os.path.basename(file_path)}")
                tracks = self._get_audio_tracks(file_path)

                if len(tracks) > 1:
                    # Multiple tracks - add each as separate entry
                    logger.info(f"Found {len(tracks)} audio tracks in video")
                    added_any = False
                    for i, track in enumerate(tracks):
                        if self._add_track_entry(file_path, track, i + 1):
                            added_any = True

                    # Update queue size
                    if not self._updates_suspended and added_any:
                        self.update_queue_size_label()

                    # If queue was empty and we added tracks, trigger waveform for first
                    if was_empty and added_any and self.on_file_added_to_empty_queue:
                        first_track_path = self.files[0]
                        logger.info(
                            "Queue was empty, triggering waveform for first track"
                        )
                        GLib.idle_add(
                            self.on_file_added_to_empty_queue, first_track_path, 0
                        )

                    return added_any
                elif len(tracks) == 1:
                    # Single track - treat as regular audio extraction from video
                    logger.info("Video has single audio track, adding as regular file")
                    # Fall through to regular file handling
                else:
                    logger.warning(f"No audio tracks found in video: {file_path}")
                    return False

            # Regular file handling (audio files or single-track videos)
            # Add to the internal file list
            self.files.append(file_path)
            file_index = len(self.files) - 1

            # Create row
            row = FileQueueRow(
                file_path,
                file_index,
                self.on_remove_file,
                self.on_play_file,
                self.on_delete_file,
                self.on_activate_file,
            )
            row.set_metadata("Loading...")

            self.file_rows.append(row)

            # Add to ListBox
            self.file_list.append(row)

            # Enable drag source for this row to allow reordering
            drag_source = Gtk.DragSource()
            drag_source.set_actions(Gdk.DragAction.MOVE)
            drag_source.connect("prepare", self._on_row_drag_prepare, row)
            drag_source.connect("drag-begin", self._on_row_drag_begin, row)
            row.add_controller(drag_source)

            # Update queue size
            if not self._updates_suspended:
                self.update_queue_size_label()

            # Queue for background metadata extraction
            self._metadata_queue.append((file_index, file_path, row))
            self._start_metadata_thread()

            # If queue was empty and callback is set, trigger it
            if was_empty and self.on_file_added_to_empty_queue:
                logger.info(
                    f"Queue was empty, triggering waveform generation for first file: {file_path}"
                )
                # Use GLib.idle_add to ensure UI is ready
                GLib.idle_add(self.on_file_added_to_empty_queue, file_path, file_index)
            elif was_empty:
                logger.warning(
                    "Queue was empty but on_file_added_to_empty_queue callback is not set"
                )

            return True
        except Exception as e:
            logger.error(f"Error adding file {file_path}: {str(e)}")
            return False

    def _start_metadata_thread(self):
        """Start the background thread to process metadata if needed."""
        import threading

        # Check if thread is already running
        if self._metadata_thread and self._metadata_thread.is_alive():
            return

        # Start new thread
        self._metadata_thread = threading.Thread(
            target=self._process_metadata_queue,
            daemon=True,  # Make thread exit when main program exits
        )
        self._metadata_thread.start()

    def _process_metadata_queue(self):
        """Process files in the metadata queue in the background."""
        import time

        while self._metadata_queue:
            try:
                # Get next file from queue
                index, file_path, row = self._metadata_queue.pop(0)

                # Skip if file no longer exists in queue
                if index >= len(self.files) or self.files[index] != file_path:
                    continue

                # Get comprehensive metadata - this already uses ffprobe to validate
                metadata = self.converter.get_file_metadata(file_path)

                # If no duration was found, the file is not a valid media file
                if "duration" not in metadata:
                    logger.warning(
                        f"Removing invalid media file from queue: {os.path.basename(file_path)}"
                    )

                    def remove_invalid_file():
                        # Check if file is still in the queue at the same index
                        if index < len(self.files) and self.files[index] == file_path:
                            self.remove_file(index)
                        return False

                    # Schedule removal on main thread
                    GLib.idle_add(remove_invalid_file)
                    continue

                # Update the UI with the metadata
                def update_ui():
                    if index < len(self.file_rows) and self.file_rows[index] == row:
                        # Build metadata string
                        metadata_parts = []
                        if "size" in metadata:
                            metadata_parts.append(metadata["size"])
                        if "duration" in metadata:
                            metadata_parts.append(metadata["duration"])
                        if "format" in metadata:
                            metadata_parts.append(metadata["format"])
                        if "bitrate" in metadata:
                            metadata_parts.append(metadata["bitrate"])

                        # Update subtitle
                        row.set_metadata(" • ".join(metadata_parts))

                    return False

                # Update UI on the main thread
                GLib.idle_add(update_ui)

                # Small delay to prevent hogging CPU
                time.sleep(0.01)

            except Exception as e:
                logger.error(f"Error processing metadata: {str(e)}")

    def update_queue_size_label(self):
        """Update the queue size label."""
        count = len(self.files)
        text = f"{count} file{'s' if count != 1 else ''}"
        self.queue_size_label.set_text(text)

        # Notify listeners about the change
        if hasattr(self, "on_queue_size_changed") and callable(
            self.on_queue_size_changed
        ):
            self.on_queue_size_changed(count, text)

    def get_queue_size_text(self):
        """Get the current queue size as text."""
        count = len(self.files)
        return f"{count} file{'s' if count != 1 else ''}"

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
        """Handle delete confirmation dialog response."""
        if response == "delete":
            try:
                # Remove from queue first
                self.remove_file(index)

                # Delete the actual file
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"Deleted file: {file_path}")
                else:
                    logger.warning(f"File not found for deletion: {file_path}")

            except Exception as e:
                logger.error(f"Error deleting file {file_path}: {e}")
                # Show error dialog
                error_dialog = Adw.MessageDialog(
                    transient_for=self._parent_window,
                    heading=_("Error Deleting File"),
                    body=f"Could not delete the file: {str(e)}",
                )
                error_dialog.add_response("ok", _("OK"))
                error_dialog.present()

    def remove_file(self, index):
        """Remove a file from the queue."""
        if 0 <= index < len(self.files):
            # Store the file_id before removing it
            file_id = self.files[index]  # The file path is the ID

            # Check if this file is currently playing and stop playback if it is
            if self.currently_playing_index == index:
                logger.debug(f"Stopping playback of file being removed (index {index})")
                # Call the stop playback callback if it exists and is callable
                if self.on_stop_playback and callable(self.on_stop_playback):
                    self.on_stop_playback()

                # Reset the UI state for the playing file
                if 0 <= index < len(self.file_rows):
                    row = self.file_rows[index]
                    row.play_button.set_icon_name("media-playback-start-symbolic")
                    row.remove_css_class("accent")

                # Reset playing index immediately to avoid further references to this file
                self.currently_playing_index = None

            # Always notify about file removal - regardless of whether it was playing
            # This ensures the visualizer can clear if needed
            if self.on_playing_file_removed and callable(self.on_playing_file_removed):
                logger.debug("Notifying file removal to allow visualizer cleanup")
                self.on_playing_file_removed()

            # Store references before removing
            row = self.file_rows[index]

            # Continue with existing removal logic...
            # Immediately update internal data structures
            self.files.pop(index)
            self.file_rows.pop(index)

            # Update currently playing index if needed
            if self.currently_playing_index is not None:
                if index == self.currently_playing_index:
                    self.currently_playing_index = None
                elif index < self.currently_playing_index:
                    self.currently_playing_index -= 1

            # Update active file index if needed
            if self.active_file_index is not None:
                if index == self.active_file_index:
                    self.active_file_index = None
                elif index < self.active_file_index:
                    self.active_file_index -= 1

            # Update indexes for all remaining rows
            for i, remaining_row in enumerate(self.file_rows):
                remaining_row.index = i

            # Remove the row from ListBox
            self.file_list.remove(row)

            # Update the queue size label
            self.update_queue_size_label()

            # Notify about the removal with the file ID
            if self.file_removed_signal:
                logger.debug(f"Notifying file removal: file_id={file_id}")
                self.file_removed_signal(file_id)

            return True
        return False

    def on_clear_queue(self, button):
        """Clear the entire queue."""
        self.clear_queue()

    def clear_queue(self):
        """Remove all files from the queue."""
        # Remove all rows from ListBox
        for row in self.file_rows:
            self.file_list.remove(row)

        self.files = []
        self.file_rows = []

        # Update the queue size label
        self.update_queue_size_label()

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

                GLib.timeout_add(1500, hide_progress)

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

            # Determine if we're moving up or down
            if old_index < new_index:
                # Moving down - insert after target
                new_index = new_index
            else:
                # Moving up - insert before target (or at target position)
                pass

            # Move the file in the internal list
            file_path = self.files.pop(old_index)
            self.files.insert(new_index, file_path)

            # Move the row in the visual list
            row_widget = self.file_rows.pop(old_index)
            self.file_rows.insert(new_index, row_widget)

            # Update row indices
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

            logger.debug(f"Reordered file from index {old_index} to {new_index}")
            return True

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
