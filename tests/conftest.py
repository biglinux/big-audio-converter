"""Import the installed-layout application without modifying production modules."""
import sys
from pathlib import Path

APPLICATION_ROOT = Path(__file__).resolve().parents[1] / "big-audio-converter/usr/share/biglinux/audio-converter"
sys.path.insert(0, str(APPLICATION_ROOT))
