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
from gi.repository import Gtk, Adw, Gio

# Application imports
from app.ui.main_window import MainWindow
from app.ui.welcome_dialog import WelcomeDialog
from app.utils.config import AppConfig
from app.audio.player import AudioPlayer
from app.audio.converter import AudioConverter


# Add the project root directory to the Python path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

# Create an __init__.py file if it doesn't exist to make the app directory a proper package
app_init_path = os.path.join(project_root, "app", "__init__.py")
if not os.path.exists(app_init_path):
    os.makedirs(os.path.dirname(app_init_path), exist_ok=True)
    with open(app_init_path, "w") as f:
        f.write('"""Audio Converter application package."""\n')


class Application(Adw.Application):
    """Main application class for Audio Converter."""

    def __init__(self):
        super().__init__(
            application_id="br.com.biglinux.audio.converter",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

        self.config = AppConfig()
        # Determine ARNNDN model path relative to this script (main.py)
        self.project_root = os.path.dirname(os.path.abspath(__file__))
        self.arnndn_model_path = os.path.join(
            self.project_root, "arnndn-models", "std.rnnn"
        )
        if not os.path.exists(self.arnndn_model_path):
            # Model is optional - noise reduction just won't be available
            logging.debug(
                f"ARNNDN model not found at {self.arnndn_model_path}. "
                "Noise reduction will not be available."
            )
            self.arnndn_model_path = None  # Ensure it's None if not found

        self.player = AudioPlayer(arnndn_model_path=self.arnndn_model_path)
        self.converter = AudioConverter(arnndn_model_path=self.arnndn_model_path)
        self._create_actions()

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

    def do_activate(self):
        """Called when the application is activated."""
        win = self.props.active_window
        if not win:
            win = MainWindow(application=self)
            # Show welcome dialog on first run
            if WelcomeDialog.should_show_welcome():
                self.show_welcome_dialog(win)
        win.present()

    def show_welcome_dialog(self, parent_window=None):
        """Show the welcome dialog"""
        if parent_window is None:
            parent_window = self.props.active_window
        welcome = WelcomeDialog(parent_window)
        welcome.present()

    def on_quit_action(self, *args):
        """Handle the app.quit action."""
        self.quit()

    def on_about_action(self, *args):
        """Show the about dialog with the system 'big-audio-converter' icon."""
        about = Adw.AboutWindow(
            transient_for=self.props.active_window,
            application_name=_("Audio Converter"),
            application_icon="big-audio-converter",  # Use system icon
            developer_name=_("BigLinux Team"),
            version="3.0.0",
            developers=[_("BigLinux Team")],
            website="https://github.com/biglinux/big-audio-converter",
            license_type=Gtk.License.GPL_3_0,
        )
        about.present()

    def on_show_welcome_action(self, *args):
        """Show the welcome dialog."""
        self.show_welcome_dialog()


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
