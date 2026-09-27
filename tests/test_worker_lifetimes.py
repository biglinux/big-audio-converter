"""Idle workers release completed requests without needing another request."""

import gc
import time
import weakref
from types import SimpleNamespace

from app.audio.probe_service import ProbeService
from app.audio.waveform import WaveformGenerator
from gi.repository import GLib


def test_probe_worker_releases_callback_after_delivery(tmp_path):
    class Owner:
        done = False

        def ready(self, *_args):
            self.done = True

    owner = Owner()
    reference = weakref.ref(owner)
    service = ProbeService("ffmpeg")
    try:
        service.request(str(tmp_path / "missing.wav"), owner.ready)
        deadline = time.monotonic() + 5
        while not owner.done and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.005)
        assert owner.done
        del owner
        gc.collect()
        assert reference() is None
    finally:
        service.cleanup()
    assert not service._worker.is_alive()


def test_waveform_worker_releases_completed_snapshot(monkeypatch):
    import threading

    finished = threading.Event()
    references = []

    class Snapshot(dict):
        pass

    def execute(*job):
        references.append(weakref.ref(job[-3]))
        finished.set()

    worker = WaveformGenerator()
    monkeypatch.setattr(worker, "_execute", execute)

    # The job holds a copied marker snapshot; an idle thread must not retain it.
    class Target:
        pass

    target = Target()
    try:
        worker.request(
            "sample.wav",
            SimpleNamespace(ffmpeg_path="ffmpeg"),
            target,
            file_markers=Snapshot(sample=[{"start": 0, "stop": 1}]),
        )
        assert finished.wait(5)
        deadline = time.monotonic() + 5
        while references[0]() is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert references[0]() is None
    finally:
        worker.cleanup()
    assert not worker._worker.is_alive()
