"""Import the installed-layout application without modifying production modules."""

import os
import sys
from pathlib import Path

# Tests assert the English messages. main.py binds the application's
# translation domain process-wide, so a translated desktop session would
# otherwise leak its language into every test that runs after the GUI tests.
os.environ["LANGUAGE"] = "C"
# xdotool drives the keyboard tests through X11 windows; set before GTK loads.
os.environ.setdefault("GDK_BACKEND", "x11")

APPLICATION_ROOT = (
    Path(__file__).resolve().parents[1]
    / "big-audio-converter/usr/share/biglinux/audio-converter"
)
sys.path.insert(0, str(APPLICATION_ROOT))
