"""Import the installed-layout application and give the tests a session of their own."""

import atexit
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from gi.repository import GLib

# Tests assert the English messages. main.py binds the application's
# translation domain process-wide, so a translated desktop session would
# otherwise leak its language into every test that runs after the GUI tests.
os.environ["LANGUAGE"] = "C"
# The Trash lives under XDG_DATA_HOME, which GLib reads once per process: tests
# that move originals there must never reach the Trash of the person running them.
DATA_HOME = tempfile.mkdtemp(prefix="bac-test-data-")
atexit.register(shutil.rmtree, DATA_HOME, True)
os.environ["XDG_DATA_HOME"] = DATA_HOME
# Read once here, so a fixture that later points XDG_DATA_HOME at its own
# home cannot move the Trash out from under the tests that check it.
GLib.get_user_data_dir()

APPLICATION_ROOT = (
    Path(__file__).resolve().parents[1]
    / "big-audio-converter/usr/share/biglinux/audio-converter"
)
sys.path.insert(0, str(APPLICATION_ROOT))


def _private_session():
    """Give the tests a display and a session bus of their own.

    Windows must never reach the desktop of the person running the tests:
    without WAYLAND_DISPLAY libwayland falls back to the live desktop's socket,
    and a GApplication on the live bus would hand its activation to a converter
    the person has open. A session already in a private runtime directory is
    reused.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if os.environ.get("BAC_TESTS_KEEP_SESSION") or (
        runtime and runtime != f"/run/user/{os.getuid()}"
    ):
        return
    for name in ("WAYLAND_DISPLAY", "DISPLAY", "AT_SPI_BUS_ADDRESS"):
        os.environ.pop(name, None)
    # xdotool drives the keyboard tests through X11 windows.
    os.environ["GDK_BACKEND"] = "x11"
    started = []
    atexit.register(
        lambda: [os.killpg(p.pid, signal.SIGTERM) for p in started if p.poll() is None]
    )

    if shutil.which("Xvfb"):
        read_end, write_end = os.pipe()
        started.append(
            subprocess.Popen(
                ["Xvfb", "-displayfd", str(write_end), "-screen", "0", "1366x900x24"]
                + ["-nolisten", "tcp"],
                pass_fds=[write_end],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        )
        os.close(write_end)
        with os.fdopen(read_end) as reader:
            number = reader.readline().strip()
        if number:
            os.environ["DISPLAY"] = f":{number}"

    if not shutil.which("dbus-daemon"):
        return
    # Only AT-SPI may be activated: portals and secret services started on a
    # throwaway bus outlive it.
    bus_dir = Path(tempfile.mkdtemp(prefix="bac-test-bus-"))
    atexit.register(shutil.rmtree, bus_dir, True)
    (bus_dir / "services").mkdir()
    for service in Path("/usr/share/dbus-1/services").glob("org.a11y.*.service"):
        (bus_dir / "services" / service.name).symlink_to(service)
    (bus_dir / "session.conf").write_text(
        "<busconfig><type>session</type>"
        f"<listen>unix:dir={bus_dir}</listen><servicedir>{bus_dir}/services</servicedir>"
        '<policy context="default"><allow send_destination="*" eavesdrop="true"/>'
        '<allow eavesdrop="true"/><allow own="*"/></policy></busconfig>',
        encoding="utf-8",
    )
    bus = subprocess.Popen(
        [
            "dbus-daemon",
            "--nofork",
            f"--config-file={bus_dir}/session.conf",
            "--print-address=1",
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started.append(bus)
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = bus.stdout.readline().strip()


_private_session()


def command(*args, timeout=20):
    return subprocess.run(list(args), check=True, capture_output=True, timeout=timeout)


def probe(path):
    return json.loads(
        command(
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ).stdout
    )


def pcm(path, representation="f64le"):
    """The first audio stream's samples as raw bytes."""
    return command(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-f",
        representation,
        "-",
    ).stdout


def convert_batch(files, settings, engine=None):
    """Convert ``files`` as the window does and return the batch's items."""
    from app.audio.converter import AudioConverter

    converter = engine or AudioConverter()
    converter.start_batch(files, settings, lambda *_: None, lambda: None).join(120)
    items = converter.last_batch_result.items
    if engine is None:
        converter.cleanup()
    return items


def pump(predicate=lambda: True, timeout=8):
    """Run the GLib main context until ``predicate`` holds."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while True:
        # Bounded: an idle source that keeps rescheduling would never drain.
        for _ in range(200):
            if not context.pending():
                break
            context.iteration(False)
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise AssertionError("The main loop did not reach the expected state")
        time.sleep(0.005)


def start_window():
    """A running application window, configured under $XDG_CONFIG_HOME."""
    spec = importlib.util.spec_from_file_location(
        "bac_test_main", APPLICATION_ROOT / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from gi.repository import Gio

    app = module.Application()
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    app.set_application_id("br.com.biglinux.audio.converter.Test" + uuid.uuid4().hex)
    app.player._mpv_options["ao"] = "null"
    app.register(None)
    app.activate()
    win = app.get_active_window()
    assert win is not None
    win._test_errors = []
    win.file_queue.on_probe_error = lambda path, message: win._test_errors.append(
        (path, message)
    )
    return win


def stop_window(win):
    app = win.get_application()
    win.cleanup()
    win.destroy()
    app.player.cleanup()
    app.converter.cleanup()
    app.config.close()
    app.quit()


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    win = start_window()
    yield win
    stop_window(win)


@pytest.fixture
def audio(tmp_path):
    result = tmp_path / 'speech 東京 :: [sample] " & ;.wav'
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=44100:duration=2",
            str(result),
        ],
        check=True,
        timeout=10,
    )
    return result
