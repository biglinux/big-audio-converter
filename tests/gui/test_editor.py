"""Interact with real semantic controls through X11 keyboard events."""

import os
import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="An X11 graphical session is required")

from test_window import pump, xdo


def key_activate(window, widget):
    widget.grab_focus()
    xdo("windowfocus", "--sync", window.get_surface().get_xid())
    xdo("key", "space")
    pump(seconds=0.15)


def type_number(window, widget, value):
    widget.grab_focus()
    xdo("windowfocus", "--sync", window.get_surface().get_xid())
    xdo("key", "ctrl+a")
    xdo("type", "--clearmodifiers", str(value))
    xdo("key", "Tab")
    pump(seconds=0.1)


def test_segment_editor_add_and_apply_from_keyboard():
    import gi
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio
    from app.ui.segment_editor import SegmentEditor
    app = Adw.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    window = Adw.ApplicationWindow(application=app, default_width=700, default_height=600)
    results = []
    seeks = []
    window.present()
    editor = SegmentEditor(10, [], lambda result: results.append(result), seeks.append)
    try:
        editor.present(window)
        assert pump(lambda: editor.get_mapped(), seconds=3)
        key_activate(window, editor.add_button)
        assert len(editor._rows) == 1
        start, stop = editor._rows[0]
        type_number(window, start, 0.25)
        type_number(window, stop, 1.75)
        assert start.get_value() == pytest.approx(0.25)
        assert stop.get_value() == pytest.approx(1.75)
        key_activate(window, editor.add_button)
        assert len(editor._rows) == 2
        type_number(window, editor._rows[1][0], 3)
        type_number(window, editor._rows[1][1], 4)
        key_activate(window, editor.apply_button)
        assert pump(lambda: bool(results), seconds=2)
        assert [(item["start"], item["stop"]) for item in results[0]] == [(0.25, 1.75), (3, 4)]
    finally:
        editor.close()
        window.close()
        pump(seconds=0.1)


def test_invalid_segment_does_not_modify_the_model():
    import gi
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio
    from app.ui.segment_editor import SegmentEditor
    app = Adw.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    window = Adw.ApplicationWindow(application=app, default_width=700, default_height=600)
    results = []
    window.present()
    editor = SegmentEditor(10, [{"start": 1, "stop": 2}], lambda value: results.append(value), lambda value: None)
    try:
        editor.present(window)
        assert pump(lambda: editor.get_mapped(), seconds=3)
        type_number(window, editor._rows[0][0], 3)
        key_activate(window, editor.apply_button)
        assert not results
        assert editor.error.get_label()
        assert editor.get_mapped()
    finally:
        editor.close()
        window.close()
        pump(seconds=0.1)
