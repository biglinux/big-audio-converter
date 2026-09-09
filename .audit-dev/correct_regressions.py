"""Fix a translated error path exposed by native GLib execution."""

from pathlib import Path
from editing import ROOT, replace


def apply():
    jobs = ROOT / "app/audio/waveform_jobs.py"
    replace(jobs, '_, removed = self._cache.popitem(last=False)', 'removed_key, removed = self._cache.popitem(last=False)')
    tests = Path("tests/test_audit_player.py")
    tests.write_text(tests.read_text().replace("instance.get_position()", "instance._position"))
