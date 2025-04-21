#!/usr/bin/env python3
"""
Audio Converter - Main Application Entry Point
This script should be run from the project root directory.
"""

import os
import sys
import logging
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio

# Application imports
from app.ui.main_window import MainWindow
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
        self.player = AudioPlayer()
        self.converter = AudioConverter()

        self._create_actions()

    def _create_actions(self):
        """Create application actions."""
        actions = [
            ("quit", self.on_quit_action),
            ("about", self.on_about_action),
            ("preferences", self.on_preferences_action),
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
        win.present()

    def on_quit_action(self, *_):
        """Handle the app.quit action."""
        self.quit()

    def on_about_action(self, *_):
        """Show the about dialog."""
        about = Adw.AboutWindow(
            transient_for=self.props.active_window,
            application_name="Audio Converter",
            application_icon="audio-x-generic",
            developer_name="Audio Converter Team",
            version="1.0.0",
            developers=["Audio Converter Team"],
            copyright="© 2023 Audio Converter Team",
            website="https://github.com/audio-converter",
            license_type=Gtk.License.GPL_3_0,
        )
        about.present()

    def on_preferences_action(self, *_):
        """Show the preferences dialog."""
        # TODO: Implement preferences dialog
        pass


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
