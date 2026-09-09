#!/usr/bin/env python3
"""
Audio Converter - Main Application Entry Point
This script should be run from the project root directory.
"""


import os
import sys
import logging
import gettext
import gi
gettext.textdomain("big-audio-converter")
_ = gettext.gettext

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib

# Application imports
from app.ui.main_window import MainWindow
from app.ui.welcome_dialog import WelcomeDialog
from app.utils.config import AppConfig
from app.audio.player import AudioPlayer
from app.audio.converter import AudioConverter


# Add the project root directory to the Python path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)


class Application(Adw.Application):
    """Main application class for Audio Converter."""

    def __init__(self):
        super().__init__(
            application_id="br.com.biglinux.audio.converter",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS
            | Gio.ApplicationFlags.HANDLES_OPEN,
        )

        self.config = AppConfig()
        # Check if GTCRN LADSPA plugin is available for noise reduction
        self.gtcrn_ladspa_path = "/usr/lib/ladspa/libgtcrn_ladspa.so"
        if not os.path.exists(self.gtcrn_ladspa_path):
            logging.debug(
                f"GTCRN LADSPA plugin not found at {self.gtcrn_ladspa_path}. "
                "Noise reduction will not be available."
            )
            self.gtcrn_ladspa_path = None

        self.player = AudioPlayer(gtcrn_ladspa_path=self.gtcrn_ladspa_path)
        self.converter = AudioConverter(gtcrn_ladspa_path=self.gtcrn_ladspa_path)
        self.logger = logging.getLogger(__name__)
        self._create_actions()

    def _present_window_and_request_focus(self, window):
        """Respect the window manager's focus policy without transient-window tricks."""
        window.present()

    def do_open(self, files, n_files, hint):
        """Handle files opened from command line or file manager."""
        # Get the active window (MainWindow)
        win = self.props.active_window
        if not win:
            # If no window exists yet, create one
            win = MainWindow(application=self)

        # Always present and request focus for the window
        self._present_window_and_request_focus(win)

        # Add each file to the queue
        for i in range(n_files):
            file = files[i]
            if isinstance(file, Gio.File):
                path = file.get_path()
                if path:
                    win.file_queue.add_file(path)

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

    def do_activate(self):
        """Called when the application is activated."""
        win = self.props.active_window
        if not win:
            win = MainWindow(application=self)
            # Show welcome dialog on first run
            if WelcomeDialog.should_show_welcome():
                self.show_welcome_dialog(win)
        self._present_window_and_request_focus(win)

    def show_welcome_dialog(self, parent_window=None):
        """Show the welcome dialog"""
        if parent_window is None:
            parent_window = self.props.active_window
        welcome = WelcomeDialog(parent_window)
        welcome.present()

    def on_quit_action(self, *args):
        window = getattr(self, "_main_window", None)
        if window is not None:
            window.close()
        else:
            self.quit()

    def on_about_action(self, *args):
        """Show the about dialog with the system 'big-audio-converter' icon."""
        about = Adw.AboutDialog(
            application_name=_("Audio Converter"),
            application_icon="big-audio-converter",
            developer_name=_("BigLinux Team"),
            version="3.0.0",
            developers=[_("BigLinux Team")],
            website="https://github.com/biglinux/big-audio-converter",
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self.props.active_window)

    def on_show_welcome_action(self, *args):
        """Show the welcome dialog."""
        self.show_welcome_dialog()

    def do_shutdown(self):
        window = getattr(self, "_main_window", None)
        if window is not None:
            window.shutdown()
        from app.audio import waveform
        waveform.shutdown()
        self.player.cleanup()
        self.converter.cleanup()
        self.config.close()
        Adw.Application.do_shutdown(self)


def main():
    """Run the application."""
    # Setup basic logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    app = Application()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
