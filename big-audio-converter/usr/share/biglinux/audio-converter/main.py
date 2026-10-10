#!/usr/bin/env python3
"""Big Audio Converter entry point (``big-audio-converter-gui``)."""

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
import big_gtk_kit
from app.audio.converter import AudioConverter
from app.audio.media import parse_segments_arg
from app.audio.player import AudioPlayer
from app.audio.process import MediaError
from app.audio.profiles import discover_noise_plugins
from app.config import AppConfig
from app.ui.main_window import MainWindow
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

APP_NAME = "Big Audio Converter"
# The package writes its version here; a source checkout has none.
try:
    VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
except OSError:
    VERSION = "development"


class Application(Adw.Application):
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
        win = self._main_window
        if win is None or win._closed:
            win = MainWindow(application=self)
            self._main_window = win

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

        # Relative names belong to the caller's directory, not this instance's;
        # do_open reports what is not a local file.
        files = [
            command_line.create_file_for_arg(arg)
            for arg in command_line.get_arguments()[1:]
        ]
        if not files:
            self.activate()
            return 0
        self.do_open(files, len(files), "")
        paths = [file.get_path() for file in files if file.get_path()]
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
            win._update_cut_rows()
        return 0

    def _create_actions(self):
        actions = [
            ("quit", self.on_quit_action),
            ("about", self.on_about_action),
        ]
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        # Help tooltips are a preference of the person, not of one window.
        tips = Gio.SimpleAction.new_stateful(
            "toggle-tips",
            None,
            GLib.Variant.new_boolean(
                str(self.config.get("show_mouseover_tips", "true")).lower() == "true"
            ),
        )
        tips.connect("change-state", self._on_tips_changed)
        self.add_action(tips)

        self.set_accels_for_action("app.quit", ["<Control>q"])

    def _on_tips_changed(self, action, value):
        action.set_state(value)
        self.config.set(
            "show_mouseover_tips", "true" if value.get_boolean() else "false"
        )
        if self._main_window is not None:
            self._main_window.help_tooltips.refresh()

    def do_startup(self):
        Adw.Application.do_startup(self)
        big_gtk_kit.install()
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_theme.add_search_path(str(Path(__file__).resolve().parents[2] / "icons"))
        style = Gtk.CssProvider()
        style.load_from_path(str(Path(__file__).resolve().parent / "app/ui/style.css"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def do_activate(self):
        win = self._main_window
        if win is None or win._closed:
            win = MainWindow(application=self)
            self._main_window = win
        win.present()

    def on_quit_action(self, *args):
        # Through the window, so a running conversion asks before it stops.
        win = self._main_window
        if win is not None and not win._closed:
            win.close()
        else:
            self.quit()

    def do_shutdown(self):
        if self._main_window is not None:
            self._main_window.cleanup()
        self.player.cleanup()
        self.converter.cleanup()
        self.config.close()
        Adw.Application.do_shutdown(self)

    def on_about_action(self, *args):
        """Show the about dialog with the system 'big-audio-converter' icon."""
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="big-audio-converter",
            developer_name=_("BigLinux Team"),
            version=VERSION,
            developers=[_("BigLinux Team")],
            website="https://github.com/biglinux/big-audio-converter",
            license_type=Gtk.License.GPL_3_0_ONLY,
        )
        about.present(self._main_window)


def main():
    """Run the application."""
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("BAC_DEBUG") == "1" else logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    GLib.set_prgname("big-audio-converter")
    GLib.set_application_name(APP_NAME)
    app = Application()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
