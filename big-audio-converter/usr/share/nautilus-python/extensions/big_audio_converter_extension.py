"""Native, nonblocking Nautilus integration for local audio and video files."""

import gettext
import logging

import gi

gi.require_version("Nautilus", "4.0")
from gi.repository import Gio, GLib, GObject, Nautilus

_translation = gettext.translation("big-audio-converter", "/usr/share/locale", fallback=True)
_ = _translation.gettext
logger = logging.getLogger(__name__)


class BigAudioConverterExtension(GObject.GObject, Nautilus.MenuProvider):
    def get_file_items(self, files):
        selected = [item for item in files if self._is_supported_file(item)]
        if not selected:
            return []
        video_only = all(item.get_mime_type().startswith("video/") for item in selected)
        label = _("Extract Audio") if video_only else _("Convert or Edit Audio")
        item = Nautilus.MenuItem(name="BigAudioConverter::Open", label=label)
        item.connect("activate", self._launch_application, selected)
        return [item]

    @staticmethod
    def _is_supported_file(info):
        if info is None or info.is_directory():
            return False
        media = info.get_mime_type() or ""
        file = Gio.File.new_for_uri(info.get_uri())
        return file.is_native() and bool(file.get_path()) and (media.startswith(("audio/", "video/")) or media == "application/ogg")

    @staticmethod
    def _get_file_path(info):
        # GIO validates URI hosts and escaping; slicing file:// is not a URI parser.
        return Gio.File.new_for_uri(info.get_uri()).get_path()

    def _launch_application(self, item, files):
        selected = [Gio.File.new_for_uri(info.get_uri()) for info in files if self._is_supported_file(info)]
        if not selected:
            self._show_error_notification(_("No local media files"), _("Download remote files before converting them."))
            return
        try:
            application = Gio.DesktopAppInfo.new("br.com.biglinux.audio.converter.desktop")
            if application is None:
                application = Gio.AppInfo.create_from_commandline(
                    "big-audio-converter-gui %F", "Big Audio Converter", Gio.AppInfoCreateFlags.NONE
                )
            # GIO owns child reaping, field-code expansion, quoting and activation.
            application.launch(selected, None)
        except GLib.Error:
            logger.debug("Could not launch the audio converter", exc_info=True)
            self._show_error_notification(_("Application Launch Error"), _("Big Audio Converter could not be started. Check that it is installed."))

    @staticmethod
    def _show_error_notification(title, message):
        application = Gio.Application.get_default()
        if application is not None and application.get_is_registered():
            notification = Gio.Notification.new(title)
            notification.set_body(message)
            application.send_notification("big-audio-converter-launch", notification)
        else:
            logger.warning("%s: %s", title, message)
