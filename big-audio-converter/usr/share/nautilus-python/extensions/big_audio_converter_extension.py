#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Big Audio Converter - Nautilus Extension
Adds a context menu option to convert audio files using the
Big Audio Converter application.
"""

import gettext
import subprocess
from pathlib import Path
from urllib.parse import unquote

# Import 'gi' and explicitly require GTK and Nautilus versions.
# This is mandatory in modern PyGObject to prevent warnings and ensure API compatibility.
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Nautilus', '4.0')

from gi.repository import GObject, Nautilus

# --- Internationalization (i18n) Setup ---
APP_NAME = "big-audio-converter"

try:
    # Set the default domain for this script. gettext will automatically find
    # the message catalogs in the system's standard locale directories.
    gettext.textdomain(APP_NAME)
except Exception as e:
    print(f"Big Audio Converter Extension: Could not set up localization: {e}")

# Define the global translation function.
_ = gettext.gettext


class BigAudioConverterExtension(GObject.GObject, Nautilus.MenuProvider):
    """
    Provides the context menu items for Nautilus to allow audio conversion.
    """

    def __init__(self):
        """Initializes the extension."""
        super().__init__()
        self.app_executable = 'big-audio-converter-gui'

        # Using a set provides O(1) lookup time, which is more efficient than a list.
        self.supported_mimetypes = {
            "audio/mpeg",
            "audio/mp4",
            "audio/x-wav",
            "audio/x-flac",
            "audio/ogg",
            "audio/x-vorbis+ogg",
            "audio/x-opus+ogg",
            "audio/aac",
            "audio/x-aac",
            "audio/x-m4a",
            "audio/x-ms-wma",
            "audio/webm",
            "audio/x-ape",
            "audio/x-matroska",
            "audio/ac3",
            "audio/eac3",
            "audio/x-aiff",
            "audio/x-mpeg",
            "video/mp4",
            "video/x-matroska",
            "video/webm",
            "video/x-msvideo",
            "video/quicktime",
            "video/x-flv",
            "video/mpeg",
            "video/x-ms-asf",
            "video/x-avi",
            "video/ogg",
            "video/x-theora+ogg",
        }

    def get_file_items(self, files: list[Nautilus.FileInfo]) -> list[Nautilus.MenuItem]:
        """
        Returns menu items for the selected files.
        The menu is only shown if one or more supported audio/video files are selected.
        """
        supported_files = [f for f in files if self._is_supported_file(f)]
        if not supported_files:
            return []

        # Determine the type: 'audio', 'video', or 'mixed'
        file_types = [self._get_file_type(f) for f in supported_files]
        if all(t == "audio" for t in file_types):
            action_type = "audio"
        elif all(t == "video" for t in file_types):
            action_type = "video"
        else:
            action_type = "mixed"

        num_files = len(supported_files)

        # Define the label based on the type and number of selected files.
        if action_type == "audio":
            if num_files == 1:
                label = _("Convert or Edit Audio")
            else:
                label = _("Convert or Edit {0} Audio Files").format(num_files)
            name = 'BigAudioConverter::Convert'
        elif action_type == "video":
            if num_files == 1:
                label = _("Extract Audio")
            else:
                label = _("Extract Audio from {0} Video Files").format(num_files)
            name = "BigAudioConverter::Extract"
        else:  # mixed
            label = _("Process {0} Audio/Video Files").format(num_files)
            name = "BigAudioConverter::Process"

        menu_item = Nautilus.MenuItem(name=name, label=label)
        menu_item.connect("activate", self._launch_application, supported_files)
        return [menu_item]

    def _is_supported_file(self, file_info: Nautilus.FileInfo) -> bool:
        """
        Checks if a file is a supported audio or video by its mimetype.
        """
        if not file_info or file_info.is_directory():
            return False

        return file_info.get_mime_type() in self.supported_mimetypes

    def _get_file_type(self, file_info: Nautilus.FileInfo) -> str:
        """
        Returns 'audio' or 'video' based on the mimetype.
        """
        mime = file_info.get_mime_type()
        if mime.startswith("audio/"):
            return "audio"
        elif mime.startswith("video/"):
            return "video"
        else:
            return "unknown"

    def _get_file_path(self, file_info: Nautilus.FileInfo) -> str | None:
        """
        Gets the local file path from a Nautilus.FileInfo object by parsing its URI.
        """
        uri = file_info.get_uri()
        if not uri.startswith('file://'):
            return None
        # Decode URL-encoded characters (e.g., %20 -> space) and remove the prefix.
        return unquote(uri[7:])

    def _launch_application(self, menu_item: Nautilus.MenuItem, files: list[Nautilus.FileInfo]):
        """
        Launches the Big Audio Converter application with the selected files.
        """
        file_paths = []
        for f in files:
            path = self._get_file_path(f)
            if path and Path(path).exists():
                file_paths.append(path)

        if not file_paths:
            self._show_error_notification(
                _("No valid local files selected"),
                _("Could not get the path for the selected audio files.")
            )
            return

        try:
            cmd = [self.app_executable] + file_paths
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        except Exception as e:
            print(f"Error launching '{self.app_executable}': {e}")
            self._show_error_notification(
                _("Application Launch Error"),
                _("Failed to start Big Audio Converter: {0}").format(str(e))
            )

    def _show_error_notification(self, title: str, message: str):
        """
        Displays a desktop error notification using 'notify-send'.
        """
        try:
            subprocess.run([
                'notify-send',
                '--icon=dialog-error',
                f'--app-name={APP_NAME}',
                title,
                message
            ], check=False)
        except FileNotFoundError:
            # Fallback if 'notify-send' is not installed.
            print(f"ERROR: [{title}] {message}")
