"""Accessible, adaptive first-run guidance without marketing overclaims."""

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
        self.dialog = Adw.Dialog(title=_("Welcome"), content_width=560, content_height=600)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                          margin_start=24, margin_end=24, margin_top=12, margin_bottom=24)
        heading = Gtk.Label(label=_("Convert audio in three steps"), wrap=True, xalign=0)
        heading.add_css_class("title-1")
        content.append(heading)
        for title, description in (
            (_("1. Add your files"), _("Add audio or video files. Videos with several audio tracks show one entry for each track.")),
            (_("2. Choose the output"), _("Choose MP3, FLAC, Ogg Vorbis, WAV, AAC or Opus and a destination folder. Original files are never replaced.")),
            (_("3. Convert"), _("The results list each output and any files that need another attempt. Your queue stays available for repeating an operation.")),
            (_("Editing and effects"), _("Edit Segments provides start and end times and keyboard controls. Volume, speed, equalizer and other enabled effects also change exported audio.")),
            (_("Fast Copy"), _("Copy encoded audio without re-encoding. Cut boundaries are approximate; effects are bypassed. Container support determines which tags and artwork can be kept.")),
            (_("Speech cleanup"), _("Neural noise reduction requires the optional GTCRN plugin and is intended for speech, not music. Unavailable effects are identified in settings.")),
        ):
            block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            label = Gtk.Label(label=title, wrap=True, xalign=0)
            label.add_css_class("heading")
            block.append(label)
            block.append(Gtk.Label(label=description, wrap=True, xalign=0))
            content.append(block)
        preferences = Adw.PreferencesGroup()
        self.show_switch = Adw.SwitchRow(title=_("Show dialog on startup"))
        self.show_switch.set_active(str(self.config.get("show_welcome_dialog", True)).lower() == "true")
        self.show_switch.connect("notify::active", lambda row, _pspec: self.config.set("show_welcome_dialog", row.get_active()))
        preferences.add(self.show_switch)
        content.append(preferences)
        start = Gtk.Button(label=_("Let's Start"), halign=Gtk.Align.END)
        start.add_css_class("suggested-action")
        start.connect("clicked", lambda *_: self.dialog.close())
        content.append(start)
        scrolled.set_child(content)
        toolbar.set_content(scrolled)
        self.dialog.set_child(toolbar)

    def present(self):
        self.dialog.present(self.parent_window)
