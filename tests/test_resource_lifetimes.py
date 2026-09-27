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
