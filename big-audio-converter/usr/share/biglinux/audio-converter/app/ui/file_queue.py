"""
File queue UI component for managing files to be converted.
"""

import gi
import os

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Pango, Gdk, GLib
import subprocess
import logging

logger = logging.getLogger(__name__)


class FileQueueRow(Gtk.Box):
    """Row representing a file in the queue."""

    def __init__(self, file_path, index, on_remove_callback, on_play_callback):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        self.file_path = file_path
        self.index = index
        self.on_remove_callback = on_remove_callback
        self.on_play_callback = on_play_callback

        # Create file info box with more spacing for readability
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        info_box.set_hexpand(True)

        # Make filename more prominent and add ellipsize
        filename = os.path.basename(file_path)
        self.file_label = Gtk.Label(label=filename)
        self.file_label.set_halign(Gtk.Align.START)
        self.file_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        info_box.append(self.file_label)

        # Metadata label with better visual styling
        self.metadata_label = Gtk.Label(label="")
        self.metadata_label.set_halign(Gtk.Align.START)
        self.metadata_label.add_css_class("caption")
        self.metadata_label.add_css_class("dim-label")
        info_box.append(self.metadata_label)

        # Improved progress bar with transition
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_visible(False)
        self.progress_bar.add_css_class("file-progress")
        info_box.append(self.progress_bar)

        # Button box with more consistent spacing
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_box.set_halign(Gtk.Align.START)  # Changed from END to START
        button_box.set_valign(Gtk.Align.CENTER)
        button_box.set_margin_end(10)  # Add margin between buttons and text

        # Remove button with more visual alignment with play button
        remove_button = Gtk.Button.new_from_icon_name("trash-symbolic")
        remove_button.set_tooltip_text("Remove from queue")
        remove_button.add_css_class("circular")
        remove_button.connect(
            "clicked", lambda btn: self.on_remove_callback(self.index)
        )
        button_box.append(remove_button)

        # Play button with improved appearance
        self.play_button = Gtk.Button.new_from_icon_name(
            "media-playback-start-symbolic"
        )
        self.play_button.set_tooltip_text("Play this file")
        self.play_button.add_css_class("circular")
        self.play_button.connect(
            "clicked", lambda btn: self.on_play_callback(self.file_path, self.index)
        )
        button_box.append(self.play_button)

        # Changed order: now button_box comes first, then info_box
        self.append(button_box)
        self.append(info_box)

        # Consistent margins for better spacing
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(12)
        self.set_margin_end(12)

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
        self._updates_suspended = False
        self._metadata_queue = []  # Files waiting for metadata processing
        self._metadata_thread = None  # Background thread for metadata

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
        self.queue_size_label = Gtk.Label(label="0 files")
        self.queue_size_label.set_halign(Gtk.Align.START)
        self.queue_size_label.set_hexpand(True)
        self.queue_size_label.set_visible(
            False
        )  # Hide the label as it's now in headerbar

        # Create scrolled window for file list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.add_css_class("files-container")

        # Create file list container with focus handling for keyboard navigation
        self.file_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.file_list.set_focusable(True)
        self.file_list.add_css_class("file-list")
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.file_list.add_controller(key_controller)
        scrolled.set_child(self.file_list)

        # Create a more visually appealing placeholder with drop indicator
        self.placeholder = self._create_placeholder()
        self.file_list.append(self.placeholder)

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

        # Add a signal for file removal
        self.file_removed_signal = None  # Will be set by MainWindow

    def _setup_styles(self):
        """Set up custom CSS styles for the file list."""
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(
            b"""
            .file-list {
                border-radius: 12px;
                /* Removed background color to use parent background */
            }
            
            .file-row {
                transition: background-color 200ms ease, box-shadow 200ms ease;
                margin: 2px 4px;
                padding: 8px 4px;
                border-radius: 6px;
            }
            
            .file-row:hover {
                background-color: alpha(@accent_color, 0.1);
            }
            
            /* Using more compatible alternating row colors */
            .file-row.even {
                background-color: @view_bg_color;
            }
            
            .file-row.odd {
                background-color: shade(@view_bg_color, 0.95);
            }
            
            .file-row.playing {
                background-color: alpha(@success_color, 0.2);
                box-shadow: inset 3px 0 0 @accent_color;
            }
            
            .file-row.converting {
                background-color: alpha(@accent_color, 0.15);
            }
            
            /* Simplified dividers - nearly invisible */
            .file-divider {
                margin: 0;
                min-height: 1px;
                background-color: alpha(@borders, 0.3);
            }
            
            .files-container {
                border-radius: 12px;
            }
            
            .drag-highlight {
                background-color: alpha(@accent_color, 0.2);
                border: 2px dashed @accent_color;
                border-radius: 8px;
            }
            """,
            -1,
        )

        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _create_placeholder(self):
        """Create an improved placeholder with drop zone visual indicators."""
        placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        placeholder.set_valign(Gtk.Align.CENTER)
        placeholder.set_halign(Gtk.Align.CENTER)
        placeholder.set_vexpand(True)
        placeholder.add_css_class("drop-placeholder")

        # Add a more prominent drop zone indicator
        drop_zone = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        drop_zone.add_css_class("drop-zone")
        drop_zone.set_margin_top(20)
        drop_zone.set_margin_bottom(20)
        drop_zone.set_margin_start(40)
        drop_zone.set_margin_end(40)
        drop_zone.set_hexpand(True)
        drop_zone.set_vexpand(True)

        # Add empty state icon with better styling
        placeholder_icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        placeholder_icon.set_pixel_size(64)
        placeholder_icon.set_opacity(0.7)
        drop_zone.append(placeholder_icon)

        # Primary message with better typography
        placeholder_label = Gtk.Label(label="No audio files in queue")
        placeholder_label.add_css_class("title-2")  # Larger title class
        drop_zone.append(placeholder_label)

        # Secondary instruction with icon for visual cue
        instruction_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        instruction_box.set_halign(Gtk.Align.CENTER)

        drop_icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
        instruction_box.append(drop_icon)

        instruction_label = Gtk.Label(label="Drag files here or use Add Files button")
        instruction_label.add_css_class("caption")
        instruction_box.append(instruction_label)

        drop_zone.append(instruction_box)

        placeholder.append(drop_zone)
        return placeholder

    def _on_key_pressed(self, controller, keyval, keycode, state):
        """Handle keyboard navigation in the file list."""

        # Delete key removes the selected file
        if keyval == Gdk.KEY_Delete:
            if self.currently_playing_index is not None:
                self.remove_file(self.currently_playing_index)
                return True

        # Space key toggles play/pause
        elif keyval == Gdk.KEY_space:
            if self.currently_playing_index is not None:
                index = self.currently_playing_index
                self.on_play_file(self.files[index], index)
                return True

        return False

    def add_file(self, file_path):
        """Add a file to the queue without blocking for metadata."""
        import time

        start_time = time.time()

        try:
            # Check file existence only - avoid any additional I/O or metadata reading here
            if not os.path.isfile(file_path):
                return False

            # Normalize path for comparison
            normalized_path = os.path.abspath(file_path)

            # Check if file is already in queue
            for existing_file in self.files:
                if os.path.abspath(existing_file) == normalized_path:
                    logger.debug(
                        f"File already in queue: {os.path.basename(file_path)}"
                    )
                    return False

            # Add to the internal file list
            self.files.append(file_path)
            file_index = len(self.files) - 1

            # Remove placeholder if this is the first file
            if len(self.files) == 1 and self.placeholder.get_parent():
                self.file_list.remove(self.placeholder)

            # Get basic file info quickly (just size - minimal I/O)
            basic_info = self._get_quick_file_info(file_path)

            # Create row with basic file info
            row = FileQueueRow(
                file_path, file_index, self.on_remove_file, self.on_play_file
            )

            # Add file size info to the row if available
            if "size" in basic_info and hasattr(row.file_label, "set_text"):
                filename = os.path.basename(file_path)
                row.file_label.set_text(f"{filename} ({basic_info['size']})")

            # Add a loading indicator to the row's metadata
            row.metadata_label.set_text("Loading metadata...")

            self.file_rows.append(row)

            # Add to the file list with visual separator
            if self.file_rows:  # Add separator before new row (except for first row)
                divider = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                divider.set_size_request(-1, 1)
                divider.add_css_class("file-divider")
                self.file_list.append(divider)

            # Add CSS class for alternating row colors
            if len(self.file_rows) % 2 == 0:
                row.add_css_class("even")
            else:
                row.add_css_class("odd")

            # Add the row with a smooth fade-in animation
            row.set_opacity(0.0)
            self.file_list.append(row)

            # Apply fade-in animation
            row.set_opacity(1.0)

            # Only update internal state, defer any other processing
            if not self._updates_suspended:
                self.update_queue_size_label()

            # Queue this file for background metadata extraction
            self._metadata_queue.append((file_index, file_path, row))

            # Start metadata thread if not already running
            self._start_metadata_thread()

            # Log performance metrics if slow
            elapsed = time.time() - start_time
            if elapsed > 0.05:  # Log if over 50ms
                logger.debug(
                    f"Slow file addition: {os.path.basename(file_path)} took {elapsed:.3f}s"
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

                # Skip if file no longer exists
                if index >= len(self.files) or self.files[index] != file_path:
                    continue

                # Get comprehensive metadata
                metadata = self._get_file_metadata(file_path)

                # Update the UI with the metadata
                def update_ui():
                    if index < len(self.file_rows) and self.file_rows[index] == row:
                        # Update the metadata label
                        metadata_parts = []
                        if "size" in metadata:
                            metadata_parts.append(metadata["size"])
                        if "duration" in metadata:
                            metadata_parts.append(metadata["duration"])
                        if "format" in metadata:
                            metadata_parts.append(metadata["format"])
                        if "bitrate" in metadata:
                            metadata_parts.append(metadata["bitrate"])

                        # Join with bullets
                        row.metadata_label.set_text(" • ".join(metadata_parts))

                        # Remove spinner if it exists
                        if hasattr(row, "spinner_box") and row.spinner_box.get_parent():
                            info_box = row.get_first_child()
                            if info_box:
                                info_box.remove(row.spinner_box)

                    return False

                # Update UI on the main thread
                from gi.repository import GLib

                GLib.idle_add(update_ui)

                # Small delay to prevent hogging CPU
                time.sleep(0.01)

            except Exception as e:
                logger.error(f"Error processing metadata: {str(e)}")

    def _get_audio_duration(self, file_path):
        """Get just the duration of an audio file."""
        try:
            if hasattr(self.converter, "ffmpeg_path") and self.converter.ffmpeg_path:
                ffprobe_path = self.converter.ffmpeg_path.replace("ffmpeg", "ffprobe")

                if not os.path.exists(ffprobe_path):
                    ffprobe_path = "ffprobe"  # Try using command directly

                # Get duration with optimized command (minimal output)
                cmd = [
                    ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=2,  # Short timeout
                )

                if result.returncode == 0 and result.stdout.strip():
                    duration_secs = float(result.stdout.strip())
                    # Format duration as MM:SS
                    minutes = int(duration_secs // 60)
                    seconds = int(duration_secs % 60)

                    # Format as M:SS for short durations or MM:SS for longer
                    if minutes < 10:
                        return f"{minutes}:{seconds:02d}"
                    else:
                        return f"{minutes}:{seconds:02d}"

        except Exception as e:
            logger.debug(f"Could not get duration: {str(e)}")

        return None

    def _get_quick_file_info(self, file_path):
        """Get only the essential file info very quickly."""
        info = {}

        # Get just the file size - this is much faster than fetching audio metadata
        try:
            size_bytes = os.path.getsize(file_path)
            if size_bytes < 1024 * 1024:  # Less than 1MB
                info["size"] = f"{size_bytes / 1024:.1f} KB"
            else:
                info["size"] = f"{size_bytes / (1024 * 1024):.1f} MB"
        except Exception:
            # Silently fail for fast processing
            pass

        return info

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

    def _get_file_metadata(self, file_path):
        """Extract file metadata like size, duration, format."""
        info = {}

        # Get file size
        try:
            size_bytes = os.path.getsize(file_path)
            if size_bytes < 1024 * 1024:  # Less than 1MB
                info["size"] = f"{size_bytes / 1024:.1f} KB"
            else:
                info["size"] = f"{size_bytes / (1024 * 1024):.1f} MB"
        except Exception as e:
            logger.warning(f"Could not get file size: {e}")

        # Get file format/extension
        try:
            ext = os.path.splitext(file_path)[1]
            if ext.startswith("."):
                ext = ext[1:]
            info["format"] = ext.upper()
        except Exception as e:
            logger.warning(f"Could not get file extension: {e}")

        # Get audio duration and bitrate using ffprobe if available
        try:
            if hasattr(self.converter, "ffmpeg_path") and self.converter.ffmpeg_path:
                ffprobe_path = self.converter.ffmpeg_path.replace("ffmpeg", "ffprobe")

                if not os.path.exists(ffprobe_path):
                    ffprobe_path = "ffprobe"  # Try using command directly

                # Get duration
                cmd_duration = [
                    ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ]

                result = subprocess.run(
                    cmd_duration, capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    duration_secs = float(result.stdout.strip())
                    # Format duration as MM:SS
                    minutes = int(duration_secs // 60)
                    seconds = int(duration_secs % 60)
                    info["duration"] = f"{minutes}:{seconds:02d}"

                # Get bitrate
                cmd_bitrate = [
                    ffprobe_path,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=bit_rate",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ]

                result = subprocess.run(
                    cmd_bitrate, capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    try:
                        # Convert bits/s to kbps
                        bitrate = int(result.stdout.strip()) // 1000
                        info["bitrate"] = f"{bitrate} kbps"
                    except ValueError:
                        pass
        except Exception as e:
            logger.warning(f"Could not get file audio metadata: {e}")

        return info

    def on_remove_file(self, index):
        """Remove a file from the queue."""
        self.remove_file(index)

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
                    self.file_rows[index].play_button.set_icon_name(
                        "media-playback-start-symbolic"
                    )
                    self.file_rows[index].remove_css_class("playing")

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

            # Update indexes for all remaining rows
            for i, remaining_row in enumerate(self.file_rows):
                remaining_row.index = i

            # Find and prepare to remove dividers
            prev_divider = None
            next_divider = None

            # Check previous sibling for divider
            prev_child = row.get_prev_sibling()
            if (
                prev_child
                and isinstance(prev_child, Gtk.Box)
                and prev_child.has_css_class("file-divider")
            ):
                prev_divider = prev_child

            # Check next sibling for divider
            next_child = row.get_next_sibling()
            if (
                next_child
                and isinstance(next_child, Gtk.Box)
                and next_child.has_css_class("file-divider")
            ):
                next_divider = next_child

            # Handle divider removal based on position
            if prev_divider and next_divider:
                # If the row has dividers on both sides, remove both and add a new one
                self.file_list.remove(prev_divider)
                self.file_list.remove(next_divider)

                # Add a new divider between the rows that were previously separated by this row
                if index > 0 and index < len(self.file_rows):
                    divider = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                    divider.set_size_request(-1, 1)
                    divider.add_css_class("file-divider")
                    self.file_list.insert_child_after(
                        divider, self.file_rows[index - 1]
                    )
            elif prev_divider:
                # If row has a divider only before it, remove that divider
                self.file_list.remove(prev_divider)
            elif next_divider:
                # If row has a divider only after it, remove that divider
                self.file_list.remove(next_divider)

            # Now remove the row with a fade effect
            # Use a faster fade animation (100ms instead of 200ms)
            row.set_opacity(0.0)

            def remove_row_now():
                # Remove the row immediately after animation completes
                if row.get_parent():
                    self.file_list.remove(row)

                # Show placeholder if queue is now empty
                if not self.files and not self.placeholder.get_parent():
                    self.file_list.append(self.placeholder)

                # Apply alternating row colors to maintain visual pattern
                for i, remaining_row in enumerate(self.file_rows):
                    if i % 2 == 0:
                        remaining_row.remove_css_class("odd")
                        remaining_row.add_css_class("even")
                    else:
                        remaining_row.remove_css_class("even")
                        remaining_row.add_css_class("odd")

                return False

            # Use a shorter timeout for faster UI response
            GLib.timeout_add(100, remove_row_now)

            # Update the queue size label immediately for better feedback
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

        # Apply fade-out animation to whole list before clearing
        def perform_clear():
            # Remove all file rows
            for row in self.file_rows:
                self.file_list.remove(row)
            # Also remove all dividers
            dividers = [
                child
                for child in self.file_list
                if isinstance(child, Gtk.Box) and child.has_css_class("file-divider")
            ]
            for divider in dividers:
                self.file_list.remove(divider)

            self.files = []
            self.file_rows = []

            # Show placeholder
            if not self.placeholder.get_parent():
                self.file_list.append(self.placeholder)

            # Update the queue size label
            self.update_queue_size_label()
            return False

        # Apply a quick fade effect to all rows
        for row in self.file_rows:
            row.set_opacity(0.0)

        # Perform actual clearing after animation
        GLib.timeout_add(200, perform_clear)

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

            # Change row appearance during conversion
            if progress > 0 and progress < 1:
                # Highlight the row being processed
                row.add_css_class("converting")
            else:
                # Remove highlight when done
                row.remove_css_class("converting")

                # Hide progress when complete
                if progress >= 1:
                    # Use a small delay to hide progress bar for visual feedback
                    from gi.repository import GLib

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
            self.file_rows[self.currently_playing_index].play_button.set_icon_name(
                "media-playback-start-symbolic"
            )
            self.file_rows[self.currently_playing_index].remove_css_class("playing")

        # Set new playing item
        self.currently_playing_index = index
        if 0 <= index < len(self.file_rows):
            self.file_rows[index].play_button.set_icon_name(
                "media-playback-stop-symbolic"
            )
            self.file_rows[index].add_css_class("playing")

            # Ensure the playing item is visible by scrolling to it
            self.file_rows[index].grab_focus()

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
        file_list = value
        if file_list:
            for file in file_list:
                if hasattr(file, "get_path"):
                    file_path = file.get_path()
                    if file_path:
                        self.add_file(file_path)
            return True
        return False

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
