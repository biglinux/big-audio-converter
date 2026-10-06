"""First-run guidance, reopened from the main menu."""

import gettext

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

_ = gettext.gettext


class WelcomeDialog:
    def __init__(self, parent_window):
        self.parent_window = parent_window
        self.config = parent_window.app.config
        self.dialog = Adw.Dialog(
            title=_("Welcome"), content_width=520, content_height=500
        )
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_start=24,
            margin_end=24,
            margin_top=12,
            margin_bottom=24,
        )
        heading = Gtk.Label(
            label=_("Convert audio in three steps"), wrap=True, xalign=0
        )
        heading.add_css_class("title-1")
        content.append(heading)
        for title, description in (
            (
                _("1. Add your files"),
                _("Audio or video. Each audio track of a video becomes its own entry."),
            ),
            (
                _("2. Choose what to make"),
                _(
                    "Pick a format and, if you want, clean the voice, cut parts or adjust the sound."
                ),
            ),
            (
                _("3. Convert"),
                _("New files appear next to the originals, which are never changed."),
            ),
        ):
            block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            label = Gtk.Label(label=title, wrap=True, xalign=0)
            label.add_css_class("heading")
            block.append(label)
            block.append(Gtk.Label(label=description, wrap=True, xalign=0))
            content.append(block)
        preferences = Adw.PreferencesGroup()
        self.show_switch = Adw.SwitchRow(title=_("Show dialog on startup"))
        self.show_switch.set_active(
            str(self.config.get("show_welcome_dialog", True)).lower() == "true"
        )
        config = self.config
        self.show_switch.connect(
            "notify::active",
            lambda row, _pspec: config.set("show_welcome_dialog", row.get_active()),
        )
        preferences.add(self.show_switch)
        content.append(preferences)
        start = Gtk.Button(label=_("Let's Start"), halign=Gtk.Align.END)
        start.add_css_class("suggested-action")
        start.connect("clicked", lambda button: button.get_ancestor(Adw.Dialog).close())
        content.append(start)
        # Start on the button: focusing the switch at the end scrolls the dialog down.
        self.dialog.set_default_widget(start)
        self.dialog.set_focus(start)
        scrolled.set_child(content)
        toolbar.set_content(scrolled)
        self.dialog.set_child(toolbar)

    def present(self):
        self.dialog.present(self.parent_window)
