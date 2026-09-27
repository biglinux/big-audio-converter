"""Native finalization contracts; run only inside an isolated graphical session."""

import ctypes
import gc
import os
import time
from collections import Counter
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("BIGAGENTS_RUN_UI_SMOKE"),
    reason="Run through the isolated headless gate",
)


@pytest.fixture
def census():
    # Unlike weak-ref notification (dispose), qdata destruction proves finalize.
    library = ctypes.CDLL("libgobject-2.0.so.0")
    notify_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
    library.g_quark_from_string.argtypes = [ctypes.c_char_p]
    library.g_quark_from_string.restype = ctypes.c_uint
    library.g_object_set_qdata_full.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        notify_type,
    ]
    counts = Counter()

    @notify_type
    def finalized(_pointer):
        counts["fin"] += 1

    quark = library.g_quark_from_string(b"bac-test-finalization")

    def track(obj):
        counts["new"] += 1
        library.g_object_set_qdata_full(hash(obj), quark, 1, finalized)

    # Keep C callbacks alive even when an assertion exposes surviving objects.
    _callbacks.append(finalized)
    return track, counts


_callbacks = []


def settle():
    from gi.repository import GLib

    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline:
        context = GLib.MainContext.default()
        for _ in range(200):
            if not context.pending():
                break
            context.iteration(False)
        time.sleep(0.005)
    gc.collect()


@pytest.fixture
def parent(tmp_path):
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from app.utils.config import AppConfig
    from gi.repository import Adw

    Adw.init()
    window = Adw.Window()
    config = AppConfig(config_dir=tmp_path)
    window.app = SimpleNamespace(config=config)
    window.present()
    settle()
    yield window
    window.destroy()
    config.close()
    settle()


def test_welcome_dialog_finalizes(parent, census):
    from app.ui.welcome_dialog import WelcomeDialog
    from gi.repository import Adw, Gtk

    track, counts = census
    for _ in range(5):
        owner = WelcomeDialog(parent)
        dialog = owner.dialog
        track(dialog)
        owner.present()
        settle()

        # Find and activate the actual close control; no synthetic teardown.
        def close_button(widget):
            if isinstance(widget, Gtk.Button) and widget.get_label() == "Let's Start":
                return widget
            child = widget.get_first_child()
            while child is not None:
                result = close_button(child)
                if result is not None:
                    return result
                child = child.get_next_sibling()
            return None

        button = close_button(dialog)
        assert button is not None
        assert button.get_ancestor(Adw.Dialog) == dialog
        button.emit("clicked")
        del button, dialog, owner
        settle()
    assert counts["new"] == counts["fin"] == 5


def test_segment_editor_and_removed_rows_finalize(parent, census):
    from app.ui.segment_editor import SegmentEditor

    parent.active_audio_id = "sample"
    parent.visualizer = SimpleNamespace(duration=10, get_marker_pairs=list)
    parent.player = SimpleNamespace(_position=0)
    track, counts = census
    for _ in range(5):
        editor = SegmentEditor(parent)
        track(editor)
        editor.present(parent)
        editor.add_segment(0, 1)
        track(editor.rows[0]["group"])
        editor.rows[0]["remove"].emit("clicked")
        settle()
        assert not editor.rows
        editor.close()
        del editor
        settle()
    assert counts["new"] == counts["fin"] == 10


@pytest.mark.parametrize("with_menu", [False, True])
def test_queue_rows_finalize(parent, census, with_menu):
    from app.ui.file_queue import FileQueueRow
    from gi.repository import Gtk

    box = Gtk.Box()
    parent.set_content(box)
    track, counts = census
    for index in range(5):
        row = FileQueueRow("/sample.wav", index, None, None, None)
        track(row)
        box.append(row)
        if with_menu:
            row.more_button.popup()
            settle()
            row.more_button.popdown()
        row.cleanup()
        box.remove(row)
        del row
        settle()
    assert counts["new"] == counts["fin"] == 5


@pytest.mark.parametrize("kind", ["SeekBar", "AudioVisualizer"])
def test_drawing_widgets_finalize(parent, census, kind):
    from app.ui import visualizer

    track, counts = census
    for _ in range(5):
        widget = getattr(visualizer, kind)()
        track(widget)
        parent.set_content(widget)
        settle()
        if kind == "AudioVisualizer":
            widget.cleanup()
        parent.set_content(None)
        del widget
        settle()
    assert counts["new"] == counts["fin"] == 5


def test_conversion_results_finalize(parent, census):
    from app.audio.media import BatchResult
    from app.ui.conversion_controller import ConversionController

    parent.converter = SimpleNamespace()
    controller = ConversionController(parent)
    controller.last_batch = BatchResult()
    track, counts = census
    for _ in range(5):
        controller.show_results()
        dialog = controller.result_dialog
        track(dialog)
        settle()
        dialog.close()
        del dialog
        settle()
        assert controller.result_dialog is None
    assert counts["new"] == counts["fin"] == 5


def test_main_windows_finalize_while_application_lives(tmp_path, census):
    import main
    from app.ui.main_window import MainWindow
    from gi.repository import Gio

    app = main.Application()
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    app.config.set("show_welcome_dialog", False)
    app.register(None)
    track, counts = census
    for _ in range(5):
        window = MainWindow(application=app)
        app._main_window = window
        track(window)
        track(window.volume_popover)
        track(window.speed_popover)
        track(window.zoom_popover)
        track(window._css_provider)
        window.present()
        settle()
        window.close()
        del window
        settle()
        assert app._main_window is None
    app.config.close()
    app.quit()
    assert counts["new"] == counts["fin"] == 25


def test_queue_and_drag_controllers_finalize(parent, census, tmp_path):
    import wave

    from app.audio.converter import AudioConverter
    from app.ui.file_queue import FileQueue
    from gi.repository import Gtk

    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 8000)
    converter = AudioConverter()
    track, counts = census
    for _ in range(5):
        queue = FileQueue(converter)
        track(queue)
        track(queue._css_provider)
        parent.set_content(queue)
        queue.add_file(str(path))
        deadline = time.monotonic() + 5
        while queue.pending and time.monotonic() < deadline:
            settle()
        assert not queue.pending
        row = queue.file_rows[0]
        track(row)
        controllers = row.observe_controllers()
        drag = next(
            controller
            for controller in controllers
            if isinstance(controller, Gtk.DragSource)
        )
        assert drag.emit("prepare", 0.0, 0.0) is not None
        queue._on_row_drag_begin(drag, None)
        assert row.has_css_class("drag-row")
        queue._on_row_drag_end(drag, None, False)
        assert not row.has_css_class("drag-row")
        del controllers, drag, row
        queue.clear_queue()
        queue.cleanup()
        parent.set_content(None)
        del queue
        settle()
    converter.cleanup()
    assert counts["new"] == counts["fin"] == 15
