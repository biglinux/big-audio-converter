"""Latest-request-only waveform jobs with explicit lifetime and cache budgets."""

from collections import OrderedDict
import copy
from dataclasses import dataclass
import gettext
import logging
import os
import threading
import weakref

from app.utils.main_context import SourceGroup
from .media_probe import audio_stream, media_duration, probe_media
from .models import MediaSource
from .process_runner import OperationCancelled, ProcessRunner

_ = gettext.gettext
logger = logging.getLogger(__name__)
_services = weakref.WeakSet()


@dataclass(frozen=True)
class WaveformRequest:
    generation: int
    identifier: str
    ffmpeg: str
    markers: dict
    zoom: object
    track_metadata: dict
    enabled: bool


def file_key(source, ffmpeg):
    status = os.stat(source.path)
    return (source.path, source.stream_index, ffmpeg, status.st_dev, status.st_ino,
            status.st_size, status.st_mtime_ns, status.st_ctime_ns)


class WaveformJobs:
    """Own one decoder and at most one pending request for one visualizer."""

    def __init__(self, visualizer):
        self._visualizer = weakref.ref(visualizer)
        self._condition = threading.Condition()
        self._generation = 0
        self._pending = None
        self._runner = None
        self._worker = None
        self._sources = SourceGroup()
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self.closed = False
        self._unrealize_handler = None
        if hasattr(visualizer, "connect"):
            self._unrealize_handler = visualizer.connect("unrealize", self._on_unrealize)
        _services.add(self)

    def _on_unrealize(self, widget):
        self.close(wait=False)

    def request(self, identifier, ffmpeg, markers, zoom, track_metadata, enabled):
        with self._condition:
            if self.closed:
                return
            self._generation += 1
            if self._runner is not None:
                self._runner.cancel()
            self._sources.close()
            self._sources = SourceGroup()
            metadata = {identifier: copy.deepcopy(track_metadata[identifier])} if track_metadata and identifier in track_metadata else {}
            task = WaveformRequest(self._generation, identifier, ffmpeg, markers or {},
                                   weakref.ref(zoom) if zoom is not None else None,
                                   metadata, enabled)
            self._pending = task
            self._post(task, self._begin)
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="bac-waveform", daemon=True)
                self._worker.start()
            self._condition.notify_all()

    def _post(self, task, callback, *args):
        def invoke():
            with self._condition:
                if self.closed or task.generation != self._generation:
                    return False
            widget = self._visualizer()
            if widget is not None:
                callback(widget, task, *args)
            return False
        with self._condition:
            if not self.closed and task.generation == self._generation:
                self._sources.idle(invoke)

    def _begin(self, widget, task):
        widget.analysis_error = None
        widget.set_waveform(None, 0)
        widget.set_loading(task.enabled)
        zoom = task.zoom() if task.zoom is not None else None
        if zoom is not None:
            zoom.set_sensitive(False)

    def _deliver(self, widget, task, result):
        data, rate, duration = result
        payload = {"levels": [data], "rates": [rate], "zoom_thresholds": [1.0]} if data is not None else None
        widget.analysis_error = None
        widget.set_waveform(payload, duration)
        if getattr(widget, "markers_enabled", False) and task.identifier in task.markers:
            widget.restore_markers(task.markers[task.identifier])
        zoom = task.zoom() if task.zoom is not None else None
        if zoom is not None:
            zoom.set_sensitive(data is not None)

    def _error(self, widget, task, message):
        widget.set_waveform(None, 0)
        widget.analysis_error = message
        widget.set_loading(False)

    def _work(self):
        from .waveform import decode_envelope
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self.closed or self._pending is not None)
                if self.closed:
                    return
                task = self._pending
                self._pending = None
                runner = ProcessRunner()
                self._runner = runner
            try:
                source = MediaSource.resolve(task.identifier, task.track_metadata)
                key = file_key(source, task.ffmpeg)
                with self._condition:
                    cached = self._cache.get(key) if task.enabled else None
                    if cached is not None:
                        self._cache.move_to_end(key)
                if cached is not None:
                    result = cached
                elif task.enabled:
                    result = decode_envelope(source, task.ffmpeg, runner)
                    if key != file_key(source, task.ffmpeg):
                        raise ValueError("The media changed during waveform analysis")
                    with self._condition:
                        if not self.closed and task.generation == self._generation:
                            self._cache[key] = result
                            self._cache_bytes += result[0].nbytes
                            while len(self._cache) > 8 or self._cache_bytes > 8 * 1024 * 1024:
                                _, removed = self._cache.popitem(last=False)
                                self._cache_bytes -= removed[0].nbytes
                else:
                    info = probe_media(source.path, task.ffmpeg, runner)
                    result = (None, 0, media_duration(info, audio_stream(info, source.stream_index)))
                self._post(task, self._deliver, result)
            except OperationCancelled:
                pass
            except Exception as error:
                logger.warning("Waveform analysis did not complete: %s", error)
                self._post(task, self._error, _("Waveform unavailable. Check that the file contains readable audio."))
            finally:
                with self._condition:
                    if self._runner is runner:
                        self._runner = None

    def cancel(self):
        with self._condition:
            self._generation += 1
            self._pending = None
            if self._runner is not None:
                self._runner.cancel()
            self._sources.close()
            self._sources = SourceGroup()

    def close(self, wait=True):
        with self._condition:
            self.closed = True
            self._generation += 1
            self._pending = None
            if self._runner is not None:
                self._runner.cancel()
            self._cache.clear()
            self._cache_bytes = 0
            self._condition.notify_all()
        self._sources.close()
        widget = self._visualizer()
        if widget is not None and self._unrealize_handler is not None:
            widget.disconnect(self._unrealize_handler)
            self._unrealize_handler = None
        if wait and self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=2)
            if self._worker.is_alive():
                logger.error("Waveform worker did not acknowledge shutdown")


def close_all():
    for service in list(_services):
        service.close()
