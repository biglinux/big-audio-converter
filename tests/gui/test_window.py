"""Real GTK window interaction under X11; run explicitly with xvfb-run."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="A graphical X11 session is required")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "big-audio-converter/usr/share/biglinux/audio-converter"))


def pump(predicate=lambda: False, seconds=0.25):
    from gi.repository import GLib
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from descendants(child)
        child = child.get_next_sibling()


def xdo(*args):
    return subprocess.run(["xdotool", *map(str, args)], check=True, capture_output=True, timeout=5)


def click(window, widget):
    found, bounds = widget.compute_bounds(window)
    assert found and bounds.get_width() > 0
    xid = window.get_surface().get_xid()
    xdo("windowfocus", "--sync", xid)
    xdo("mousemove", "--window", xid,
        int(bounds.get_x() + bounds.get_width() / 2), int(bounds.get_y() + bounds.get_height() / 2))
    xdo("click", 1)
    pump()


def test_window_mouse_keyboard_and_conversion(tmp_path, monkeypatch):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("GdkX11", "4.0")
    from gi.repository import Gtk, GdkX11
    from main import Application
    from app.audio.player import AudioPlayer
    from app.audio import waveform
    from app.ui.main_window import MainWindow

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, traceback: errors.append(str(value)))
    app = Application()
    app.register(None)
    app.player.cleanup()
    app.player = AudioPlayer(audio_output="null")
    window = MainWindow(application=app)
    try:
        window.present()
        assert pump(lambda: window.get_mapped(), seconds=3)
        source = tmp_path / "hello unicode 音声.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2:sample_rate=48000", str(source)], check=True, timeout=10)
        window.file_queue.add_file(str(source))
        assert pump(lambda: bool(window.file_queue.get_files()), seconds=3)
        # Exercise keyboard activation on a real, focused native button.
        button = next(widget for widget in descendants(window) if isinstance(widget, Gtk.Button) and widget.get_label() == "Add Files")
        button.grab_focus()
        xdo("windowfocus", "--sync", window.get_surface().get_xid())
        xdo("key", "space")
        pump(seconds=0.5)
        xdo("key", "Escape")
        pump(seconds=0.5)
        convert = next(widget for widget in descendants(window) if isinstance(widget, Gtk.Button) and widget.get_label() == "Convert")
        click(window, convert)
        assert pump(lambda: getattr(app.converter, "last_batch", None) is not None and bool(app.converter.last_batch.files), seconds=15)
        assert app.converter.last_batch.files[0].status == "success"
        assert all(Path(path).is_file() for path in app.converter.last_batch.outputs)
        evidence = Path(os.environ.get("BAC_EVIDENCE_DIR", "/tmp/bac-evidence"))
        evidence.mkdir(exist_ok=True, parents=True)
        subprocess.run(["import", "-window", str(window.get_surface().get_xid()), str(evidence / "native-window.png")], check=True, timeout=10)
        (evidence / "gui.json").write_text(json.dumps({"mouse_conversion": True, "keyboard_file_dialog": True,
            "width": window.get_width(), "height": window.get_height(), "callback_errors": errors}, indent=2))
        assert not errors
    finally:
        window.close()
        waveform.shutdown()
        app.player.cleanup()
        app.converter.cleanup()
        app.config.close()
        pump(seconds=0.1)
