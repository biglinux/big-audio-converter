"""The "More Information" window of a queue entry and the probe data it shows."""

import gettext
import logging
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from app.audio.media import audio_stream
from app.audio.process import MediaError
from app.utils.time_formatter import format_time_display

_ = gettext.gettext
logger = logging.getLogger(__name__)

# Tags shown first, in this order; any other tag follows.
_TAG_ORDER = (
    "title",
    "artist",
    "album",
    "album_artist",
    "date",
    "genre",
    "track",
    "disc",
    "comment",
    "composer",
    "performer",
    "copyright",
    "encoded_by",
    "encoder",
)


def shares_file(info, stream):
    """Whether the probed file holds tracks besides `stream`; embedded cover
    art is part of the file, not another track."""
    return any(
        other is not stream and not other.get("disposition", {}).get("attached_pic")
        for other in info["streams"]
    )


def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _stream_tag(stream, prefixes):
    """The first tag named `prefix` or `prefix-<language>`."""
    for key, value in (stream or {}).get("tags", {}).items():
        for prefix in prefixes:
            if key.startswith(prefix + "-") or key == prefix:
                return value
    return None


def _tag_duration(stream):
    """Seconds from a Matroska DURATION tag such as '00:20:55.072000000'."""
    raw = _stream_tag(stream, ("DURATION",))
    parts = raw.split(":") if raw else ()
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except (ValueError, OverflowError):
        logger.debug("Unrecognized duration tag %r", raw)
        return None


def _number(value, kind=float):
    try:
        return kind(value)
    except (ValueError, TypeError):
        return None


def audio_properties(stream, data, shares, path):
    """(label, value) pairs for the track.

    `shares`: the file also holds other tracks, so its size is not this
    track's and is estimated from duration and bitrate instead.
    """
    stream = stream or {}
    container = data.get("format", {})
    duration = _tag_duration(stream)
    if duration is None:
        duration = _number(stream.get("duration"))
    bitrate = _number(_stream_tag(stream, ("BPS",)), int)
    if bitrate is None:
        bitrate = _number(stream.get("bit_rate"), int)

    size = None
    if not shares:
        try:
            size = os.path.getsize(path)
        except OSError:
            pass
    elif duration is not None and bitrate is not None:
        size = int(duration * bitrate / 8)
    props = [(_("Size"), _("Unknown") if size is None else format_size(size))]

    if duration is None:
        duration = _number(container.get("duration"))
    if duration is not None:
        props.append((_("Duration"), format_time_display(duration)))

    codec = stream.get("codec_long_name") or container.get("format_long_name")
    if codec:
        props.append((_("Format"), codec))

    if bitrate is None:
        bitrate = _number(container.get("bit_rate"), int)
    if bitrate is not None:
        props.append((_("Bitrate"), f"{bitrate // 1000} kbps"))

    rate = _number(stream.get("sample_rate"), int)
    if rate is not None:
        props.append((_("Sample rate"), f"{rate / 1000:g} kHz"))
    if "channels" in stream:
        layout = stream.get("channel_layout", "")
        channels = stream["channels"]
        props.append(
            (_("Channels"), f"{channels} ({layout})" if layout else str(channels))
        )
    bits = _number(stream.get("bits_per_sample"), int)
    if bits:
        props.append((_("Bit Depth"), f"{bits} bit"))
    return props


def _metadata_items(data):
    tags = data.get("format", {}).get("tags", {})
    items = [
        (key.replace("_", " ").title(), str(tags[key])[:4096])
        for key in _TAG_ORDER
        if key in tags
    ]
    items += [
        (key.replace("_", " ").title(), str(value)[:4096])
        for key, value in list(tags.items())[:128]
        if key.lower() not in _TAG_ORDER
    ]
    return items


def _copy(text):
    Gdk.Display.get_default().get_clipboard().set(text)


def _group(title, items):
    """Label/value rows in libadwaita's property style, each value copyable."""
    group = Adw.PreferencesGroup(title=title)
    for label, value in items:
        row = Adw.ActionRow(
            title=GLib.markup_escape_text(str(label)),
            subtitle=GLib.markup_escape_text(str(value)),
            subtitle_selectable=True,
            css_classes=["property"],
        )
        copy = Gtk.Button(
            icon_name="edit-copy-symbolic",
            tooltip_text=_("Copy value"),
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular"],
        )
        copy.update_property([Gtk.AccessibleProperty.LABEL], [_("Copy value")])
        copy.connect("clicked", lambda _button, text=str(value): _copy(text))
        row.add_suffix(copy)
        group.add(row)
    return group


class FileInfoWindow(Gtk.Window):
    """Shown at once; the details arrive from a probe off the GTK thread."""

    def __init__(self, parent, probe_service, source_path, title, stream_index):
        super().__init__(
            transient_for=parent,
            modal=True,
            title=_("File Information"),
            default_width=640,
            default_height=540,
        )
        self._probes = probe_service
        self._stream_index = stream_index
        self._clipboard = []
        header = Gtk.HeaderBar()
        self._copy_button = Gtk.Button(
            icon_name="edit-copy-symbolic",
            sensitive=False,
            tooltip_text=_("Copy information to clipboard"),
        )
        self._copy_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Copy information to clipboard")]
        )
        self._copy_button.connect(
            "clicked", lambda _button: _copy("\n".join(self._clipboard))
        )
        header.pack_end(self._copy_button)
        self.set_titlebar(header)
        self._box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_start=12,
            margin_end=12,
            margin_bottom=24,
        )
        heading = Gtk.Label(
            label=title, wrap=True, margin_top=18, margin_start=18, margin_end=18
        )
        heading.add_css_class("title-2")
        self._box.append(heading)
        self._spinner = Gtk.Spinner(spinning=True, margin_top=18, margin_bottom=18)
        self._box.append(self._spinner)
        self.set_child(
            Gtk.ScrolledWindow(
                child=Adw.Clamp(child=self._box, maximum_size=600),
                vexpand=True,
                hscrollbar_policy=Gtk.PolicyType.NEVER,
            )
        )
        try:
            self._token = probe_service.request(source_path, self._ready, priority=True)
        except (MediaError, RuntimeError) as exc:
            self._token = None
            self._show_error(str(exc))
        self.connect("close-request", FileInfoWindow._closed)

    def _closed(self):
        if self._token is not None:
            self._probes.cancel(self._token)
            self._token = None
        return False

    def _show_error(self, message):
        self._spinner.stop()
        self._spinner.set_visible(False)
        label = Gtk.Label(label=message, margin_top=12, margin_bottom=12)
        label.add_css_class("dim-label")
        self._box.append(label)

    def _ready(self, _token, path, info, error):
        self._token = None
        if error or not info:
            self._show_error(error or _("No audio information is available."))
            return
        try:
            stream = audio_stream(info, self._stream_index)
            properties = audio_properties(stream, info, shares_file(info, stream), path)
        except (MediaError, ValueError, TypeError) as exc:
            self._show_error(str(exc))
            return
        self._spinner.stop()
        self._spinner.set_visible(False)
        self._box.append(_group(_("File Details"), [(_("Path"), path)]))
        self._box.append(_group(_("Audio Properties"), properties))
        self._clipboard += [path, ""] + [f"{k}: {v}" for k, v in properties]
        tags = _metadata_items(info)
        if tags:
            self._box.append(_group(_("Metadata Tags"), tags))
            self._clipboard += [""] + [f"{k}: {v}" for k, v in tags]
        self._copy_button.set_sensitive(True)
