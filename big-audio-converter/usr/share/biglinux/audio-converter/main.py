#!/usr/bin/env python3
"""Audio Converter application entry point (``big-audio-converter-gui``)."""

import gettext
import locale
import logging
import os
import sys
from pathlib import Path

import gi

try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass
gettext.bindtextdomain(
    "big-audio-converter", str(Path(__file__).resolve().parents[2] / "locale")
)
gettext.textdomain("big-audio-converter")
_ = gettext.gettext

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from app.audio.converter import AudioConverter
from app.audio.media import parse_segments_arg
from app.audio.player import AudioPlayer
from app.audio.process import MediaError
from app.audio.profiles import discover_noise_plugins
from app.ui.main_window import MainWindow
from app.ui.welcome_dialog import WelcomeDialog
from app.utils.config import AppConfig
from gi.repository import Adw, Gdk, Gio, GLib, Gtk


class Application(Adw.Application):
    """Main application class for Audio Converter."""

    def __init__(self):
        super().__init__(
            application_id="br.com.biglinux.audio.converter",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS
            | Gio.ApplicationFlags.HANDLES_OPEN
            | Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        # Players hand a file over with the cuts the user marked there.
        self.add_main_option(
            "segments",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.STRING,
            "Cuts to keep, in seconds: START-END[,START-END...]",
            "RANGES",
        )

        self.config = AppConfig()
        noise_plugins = discover_noise_plugins()
        self.player = AudioPlayer(noise_plugins=noise_plugins)
        self.converter = AudioConverter(noise_plugins=noise_plugins)
        self._main_window = None
        self._create_actions()

    def do_open(self, files, n_files, hint):
        """Handle files opened from command line or file manager."""
        # Get the active window (MainWindow)
        win = self._main_window
        if win is None or win._closed:
            # If no window exists yet, create one
            win = MainWindow(application=self)
            self._main_window = win

        # Let the desktop handle activation; never steal focus with a fake dialog.
        win.present()

        # Add each file to the queue in bounded GTK work slices.
        paths = []
        for i in range(n_files):
            file = files[i]
            if isinstance(file, Gio.File):
                path = file.get_path()
                if path:
                    paths.append(path)
                else:
                    win._show_message(
                        _("Local files only"),
                        _("Download this file to a local folder before adding it."),
                    )

        win.file_queue.add_files(paths)

    def do_command_line(self, command_line):
        """Open files given on the command line, with optional cuts."""
        options = command_line.get_options_dict().end().unpack()
        segments = None
        try:
            if "segments" in options:
                segments = parse_segments_arg(options["segments"])
        except (MediaError, ValueError) as exc:
            # Refuse rather than queue the whole file as if no cuts were asked for.
            command_line.printerr(f"big-audio-converter: {exc}\n")
            return 2

        paths = [
            os.path.abspath(arg)
            for arg in command_line.get_arguments()[1:]
            if os.path.isfile(arg)
        ]
        if not paths:
            self.activate()
            return 0
        self.do_open([Gio.File.new_for_path(p) for p in paths], len(paths), "")
        win = self._main_window
        if segments is not None and win is not None:
            tracks = win.file_queue.track_metadata
            for path in paths:
                # A multi-track file already queued is listed as its tracks;
                # the queue splits a newly added one and carries its cuts.
                entries = [
                    entry
                    for entry, track in tracks.items()
                    if track["source_video"] == path
                ] or [path]
                for entry in entries:
                    win.file_markers[entry] = [dict(s) for s in segments]
                    # A file already on screen keeps its old markers until redrawn.
                    if entry == win.active_audio_id:
                        win.visualizer.restore_markers(win.file_markers[entry])
            # Cut on, in timeline order; join/split stays the user's choice here.
            win.cut_row.set_selected(1)
        return 0

    def _create_actions(self):
        """Create application actions."""
        actions = [
            ("quit", self.on_quit_action),
            ("about", self.on_about_action),
            ("show-welcome", self.on_show_welcome_action),
        ]
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        # Keyboard accelerators
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_startup(self):
        Adw.Application.do_startup(self)
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_theme.add_search_path(str(Path(__file__).resolve().parents[2] / "icons"))

    def do_activate(self):
        """Called when the application is activated."""
        win = self._main_window
        if win is None or win._closed:
            win = MainWindow(application=self)
            self._main_window = win
            # Show welcome dialog on first run
            if str(self.config.get("show_welcome_dialog", True)).lower() == "true":
                self.show_welcome_dialog(win)
        win.present()

    def show_welcome_dialog(self, parent_window=None):
        """Show the welcome dialog"""
        if parent_window is None:
            parent_window = self._main_window
        welcome = WelcomeDialog(parent_window)
        welcome.present()

    def on_quit_action(self, *args):
        self.quit()

    def do_shutdown(self):
        if self._main_window is not None:
            self._main_window.cleanup()
        else:
            self.player.cleanup()
            self.converter.cleanup()
        self.config.close()
        Adw.Application.do_shutdown(self)

    def on_about_action(self, *args):
        """Show the about dialog with the system 'big-audio-converter' icon."""
        about = Adw.AboutDialog(
            application_name=_("Audio Converter"),
            application_icon="big-audio-converter",
            developer_name=_("BigLinux Team"),
            version="3.0.0",
            developers=[_("BigLinux Team")],
            website="https://github.com/biglinux/big-audio-converter",
            license_type=Gtk.License.GPL_3_0_ONLY,
        )
        about.present(self._main_window)

    def on_show_welcome_action(self, *args):
        """Show the welcome dialog."""
        self.show_welcome_dialog()


def main():
    """Run the application."""
    # Setup basic logging
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("BAC_DEBUG") == "1" else logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    GLib.set_prgname("big-audio-converter")
    GLib.set_application_name(_("Audio Converter"))
    app = Application()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
