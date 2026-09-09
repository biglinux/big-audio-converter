"""Bounded peak envelopes with one cancellable, latest-request-wins worker."""

import gettext
import logging
import math
import os
import threading
import weakref
from collections import OrderedDict
from copy import deepcopy

import numpy as np

from .media import MediaSource, audio_duration, audio_stream, probe_media
from .process import CancelledError, MediaError, ProcessRunner

_ = gettext.gettext
logger = logging.getLogger(__name__)


class PeakCollector:
    """Consume FFmpeg frame metadata with bounded storage, even without duration.

    astats measures the original samples across *all* channels before temporal
    aggregation. No channel downmix, phase cancellation, or audio resampling is
    involved. Pairwise reduction preserves maxima when duration was unknown.
    """

    def __init__(self, block_size, sample_rate, max_points=131072):
        self.block_size = block_size
        self.sample_rate = sample_rate
        self.max_points = max_points
        self.peaks = np.empty(max_points, dtype=np.float32)
        self.count = 0
        self.samples = 0
        self._pending = bytearray()
        self._peak = None
        self._group_size = 1
        self._group_count = 0
        self._group_peak = 0.0

    def consume(self, data):
        self._pending.extend(data)
        lines = self._pending.split(b"\n")
        self._pending[:] = lines.pop()
        if len(self._pending) > 4096:
            raise MediaError(gettext.gettext('Invalid waveform metadata.'))
        for line in lines:
            if line.startswith(b"lavfi.astats.Overall.Peak_level="):
                decibels = float(line.split(b"=", 1)[1])
                if decibels == -math.inf:
                    self._peak = 0.0
                elif not math.isfinite(decibels):
                    raise MediaError(gettext.gettext('The audio contains non-finite samples.'))
                else:
                    self._peak = 10 ** (decibels / 20)
            elif line.startswith(b"lavfi.astats.Overall.Number_of_samples="):
                size = int(float(line.split(b"=", 1)[1]))
                if size <= 0 or self._peak is None:
                    raise MediaError(gettext.gettext('Invalid waveform sample count.'))
                self.samples += size
                self._group_peak = max(self._group_peak, self._peak)
                self._group_count += 1
                if (self._group_count == self._group_size) and (self._append(self._group_peak)):
                    self._group_count = 0
                    self._group_peak = 0.0
                self._peak = None

    def _append(self, peak):
        if self.count == self.max_points:
            self.peaks[:self.count // 2] = self.peaks[:self.count].reshape(-1, 2).max(axis=1)
            self.count //= 2
            self._group_size *= 2
            # The new, not-yet-inserted group is the first half of a group
            # at the new resolution, not a complete point at that resolution.
            self._group_count = self._group_size // 2
            self._group_peak = peak
            return False
        self.peaks[self.count] = peak
        self.count += 1
        return True

    def finish(self):
        if self._pending:
            self.consume(b"\n")
        if not self.samples:
            raise MediaError(gettext.gettext('The file contains no readable audio samples.'))
        if self._group_count:
            if self.count == self.max_points:
                self.peaks[:self.count // 2] = self.peaks[:self.count].reshape(-1, 2).max(axis=1)
                self.count //= 2
                self._group_size *= 2
            self.peaks[self.count] = self._group_peak
            self.count += 1
        data = self.peaks[:self.count].copy()
        data.flags.writeable = False
        return {"levels": [data], "rates": [self.sample_rate / (self.block_size * self._group_size)],
                "zoom_thresholds": [1.0], "envelope": True}, self.samples / self.sample_rate


class WaveformGenerator:
    MAX_POINTS = 131072
    CACHE_BYTES = 16 * 1024 * 1024
    CACHE_ENTRIES = 32

    def __init__(self):
        self._condition = threading.Condition()
        self._decode_lock = threading.Lock()
        self._runner = None
        self._generation = 0
        self._pending = None
        self._worker = None
        self._disposed = False
        self._sources = set()
        self._cache = OrderedDict()
        self._cache_bytes = 0

    def request(self, file_path, converter_instance, visualizer, file_markers=None,
                zoom_control_box=None, track_metadata=None, *, enabled=True):
        """Called on the GTK thread; only one worker and one pending job exist."""
        with self._condition:
            if self._disposed:
                return
            self._generation += 1
            if self._runner:
                self._runner.cancel()
            runner = ProcessRunner()
            self._runner = runner
            self._pending = (self._generation, runner, file_path, converter_instance.ffmpeg_path,
                             weakref.ref(visualizer), deepcopy(file_markers or {}),
                             deepcopy(track_metadata or {}), enabled)
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="audio-waveform", daemon=False)
                self._worker.start()
            self._condition.notify()

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._disposed or self._pending is not None)
                if self._disposed:
                    return
                job, self._pending = self._pending, None
            if job is not None:
                self._execute(*job)

    def generate(self, file_path, converter_instance, visualizer, file_markers=None,
                 zoom_control_box=None, track_metadata=None):
        """Synchronous compatibility entry point for headless/worker callers."""
        self._synchronous(file_path, converter_instance, visualizer, file_markers, track_metadata, True)

    def activate_without_waveform(self, file_path, converter_instance, visualizer,
                                  file_markers=None, zoom_control_box=None, track_metadata=None):
        self._synchronous(file_path, converter_instance, visualizer, file_markers, track_metadata, False)

    def _synchronous(self, path, converter, visualizer, markers, metadata, enabled):
        with self._condition:
            if self._disposed:
                return
            self._generation += 1
            generation = self._generation
            if self._runner:
                self._runner.cancel()
            runner = self._runner = ProcessRunner()
        self._execute(generation, runner, path, converter.ffmpeg_path, weakref.ref(visualizer),
                      deepcopy(markers or {}), deepcopy(metadata or {}), enabled)

    def _schedule(self, generation, callback):
        from gi.repository import GLib

        with self._condition:
            if self._disposed or generation != self._generation:
                return
            source = GLib.idle_source_new()
            holder: list[int] = []

            def deliver(*_args):
                with self._condition:
                    self._sources.discard(holder[0])
                    current = not self._disposed and generation == self._generation
                if current:
                    callback()
                return False

            source.set_callback(deliver)
            identifier = source.attach(GLib.MainContext.default())
            holder.append(identifier)
            self._sources.add(identifier)

    def _execute(self, generation, runner, path, ffmpeg, target_ref, markers, metadata, enabled):
        def loading():
            target = target_ref()
            if target is not None:
                target.waveform_error = None
                target.set_loading(enabled, _("Generating waveform..."))
        self._schedule(generation, loading)
        try:
            with self._decode_lock:
                runner.check_cancelled()
                if not ffmpeg:
                    raise MediaError(gettext.gettext('FFmpeg is not installed.'))
                source = MediaSource.resolve(path, metadata)
                stat = os.stat(source.path)
                identity = (source.path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, source.stream_index)
                if enabled and identity in self._cache:
                    payload, duration = self._cache[identity]
                    self._cache.move_to_end(identity)
                else:
                    info = probe_media(source.path, runner, ffmpeg)
                    stream = audio_stream(info, source.stream_index)
                    duration = audio_duration(info, stream)
                    payload = None
                    if enabled:
                        rate = int(stream["sample_rate"])
                        block = max(64, math.ceil((duration or 60) * rate / self.MAX_POINTS))
                        collector = PeakCollector(block, rate, self.MAX_POINTS)
                        graph = (f"asetnsamples=n={block}:p=0,"
                                 "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=Peak_level+Number_of_samples,"
                                 "ametadata=mode=print:file=/dev/stdout:direct=1")
                        result = runner.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", source.path,
                                             "-map", f"0:{stream['index']}", "-af", graph, "-f", "null", "-"],
                                            stdout_callback=collector.consume)
                        if result.returncode:
                            raise MediaError(gettext.gettext('The waveform could not be generated.'), result.stderr)
                        payload, duration = collector.finish()
                        runner.check_cancelled()
                        current = os.stat(source.path)
                        if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
                            raise MediaError(gettext.gettext('The source changed while its waveform was being generated.'))
                        self._cache[identity] = payload, duration
                        self._cache_bytes += payload["levels"][0].nbytes
                        while self._cache_bytes > self.CACHE_BYTES or len(self._cache) > self.CACHE_ENTRIES:
                            _evicted_key, (old, _evicted_duration) = self._cache.popitem(last=False)
                            self._cache_bytes -= old["levels"][0].nbytes
                runner.check_cancelled()

            def deliver():
                target = target_ref()
                if target is None:
                    return
                # Give the UI its own container. Arrays are immutable and can
                # safely be shared with the bounded cache.
                target.set_waveform(dict(payload) if payload else None, duration or 0)
                existing = target.get_marker_pairs() if hasattr(target, "get_marker_pairs") else []
                if not existing and markers.get(path) and target.markers_enabled:
                    target.restore_markers(markers[path])
            self._schedule(generation, deliver)
        except CancelledError:
            pass
        except (MediaError, OSError, ValueError, TypeError) as exc:
            logger.debug("Waveform generation failed: %s", exc)
            message = str(exc)

            def failed():
                target = target_ref()
                if target is not None:
                    target.set_loading(False)
                    target.set_waveform(None, 0)
                    target.waveform_error = message
            self._schedule(generation, failed)
        finally:
            with self._condition:
                if self._runner is runner:
                    self._runner = None

    def cancel(self):
        from gi.repository import GLib

        with self._condition:
            self._generation += 1
            self._pending = None
            if self._runner:
                self._runner.cancel()
            for identifier in self._sources:
                GLib.source_remove(identifier)
            self._sources.clear()

    _cancel_current = cancel

    def cleanup(self):
        self.cancel()
        with self._condition:
            self._disposed = True
            self._condition.notify_all()
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=2)
        with self._decode_lock:
            self._cache.clear()
            self._cache_bytes = 0


_generator = WaveformGenerator()
generate = _generator.generate
activate_without_waveform = _generator.activate_without_waveform
