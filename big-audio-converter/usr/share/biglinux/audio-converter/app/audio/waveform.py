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
    """Aggregate full-band PCM from every channel into a bounded peak envelope.

    FFmpeg supplies float32 frames in arbitrary pipe chunks. Only incomplete
    frames and one unfinished peak block survive a chunk; a full envelope is
    coarsened pairwise without losing peaks or changing their timeline.
    """

    def __init__(self, block_size, sample_rate, max_points=131072, channels=1):
        if block_size < 1 or max_points < 2 or max_points % 2:
            raise ValueError("Invalid waveform block size or capacity")
        if not 1 <= channels <= 64 or not 1 <= sample_rate <= 768000:
            raise ValueError("Unsupported audio channel count or sample rate")
        self.block_size = block_size
        self.sample_rate = sample_rate
        self.channels = channels
        self.max_points = max_points
        self.peaks = np.empty(max_points, dtype=np.float32)
        self.count = 0
        self.samples = 0
        self._remainder = b""
        self._pending_count = 0
        self._pending_peak = 0.0

    def consume(self, data):
        data = self._remainder + data
        frame_bytes = self.channels * 4
        usable = len(data) // frame_bytes * frame_bytes
        self._remainder = data[usable:]
        if not usable:
            return
        frames = np.frombuffer(data, dtype="<f4", count=usable // 4).reshape(
            -1, self.channels
        )
        if not np.isfinite(frames).all():
            raise MediaError(_("The audio contains non-finite samples."))
        peaks = np.abs(frames).max(axis=1)
        self.samples += len(peaks)
        offset = 0
        while offset < len(peaks):
            if self.count == self.max_points:
                self.peaks[: self.count // 2] = (
                    self.peaks[: self.count].reshape(-1, 2).max(axis=1)
                )
                self.count //= 2
                self.block_size *= 2
            if self._pending_count:
                size = min(self.block_size - self._pending_count, len(peaks) - offset)
                self._pending_peak = max(
                    self._pending_peak, float(peaks[offset : offset + size].max())
                )
                self._pending_count += size
                offset += size
                if self._pending_count == self.block_size:
                    self.peaks[self.count] = self._pending_peak
                    self.count += 1
                    self._pending_count = 0
                    self._pending_peak = 0.0
                continue
            blocks = min(
                (len(peaks) - offset) // self.block_size, self.max_points - self.count
            )
            if blocks:
                stop = offset + blocks * self.block_size
                self.peaks[self.count : self.count + blocks] = (
                    peaks[offset:stop].reshape(blocks, self.block_size).max(axis=1)
                )
                self.count += blocks
                offset = stop
            else:
                self._pending_count = len(peaks) - offset
                self._pending_peak = float(peaks[offset:].max())
                break

    def finish(self):
        if self._remainder:
            raise MediaError(
                _("The decoded audio ends with an incomplete sample frame.")
            )
        if not self.samples:
            raise MediaError(_("The file contains no readable audio samples."))
        if self._pending_count:
            self.peaks[self.count] = self._pending_peak
            self.count += 1
            self._pending_count = 0
        data = self.peaks[: self.count].copy()
        data.flags.writeable = False
        return {
            "levels": [data],
            "rates": [self.sample_rate / self.block_size],
            "zoom_thresholds": [1.0],
            "envelope": True,
        }, self.samples / self.sample_rate


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

    def request(
        self,
        file_path,
        converter_instance,
        visualizer,
        file_markers=None,
        zoom_control_box=None,
        track_metadata=None,
        *,
        enabled=True,
    ):
        """Called on the GTK thread; only one worker and one pending job exist."""
        with self._condition:
            if self._disposed:
                return
            self._generation += 1
            if self._runner:
                self._runner.cancel()
            runner = ProcessRunner()
            self._runner = runner
            self._pending = (
                self._generation,
                runner,
                file_path,
                converter_instance.ffmpeg_path,
                weakref.ref(visualizer),
                deepcopy(file_markers or {}),
                deepcopy(track_metadata or {}),
                enabled,
            )
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._work, name="audio-waveform", daemon=False
                )
                self._worker.start()
            self._condition.notify()

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._disposed or self._pending is not None
                )
                if self._disposed:
                    return
                job, self._pending = self._pending, None
            if job is not None:
                self._execute(*job)
            del job

    def generate(
        self,
        file_path,
        converter_instance,
        visualizer,
        file_markers=None,
        zoom_control_box=None,
        track_metadata=None,
    ):
        """Synchronous compatibility entry point for headless/worker callers."""
        self._synchronous(
            file_path,
            converter_instance,
            visualizer,
            file_markers,
            track_metadata,
            True,
        )

    def activate_without_waveform(
        self,
        file_path,
        converter_instance,
        visualizer,
        file_markers=None,
        zoom_control_box=None,
        track_metadata=None,
    ):
        self._synchronous(
            file_path,
            converter_instance,
            visualizer,
            file_markers,
            track_metadata,
            False,
        )

    def _synchronous(self, path, converter, visualizer, markers, metadata, enabled):
        with self._condition:
            if self._disposed:
                return
            self._generation += 1
            generation = self._generation
            if self._runner:
                self._runner.cancel()
            runner = self._runner = ProcessRunner()
        self._execute(
            generation,
            runner,
            path,
            converter.ffmpeg_path,
            weakref.ref(visualizer),
            deepcopy(markers or {}),
            deepcopy(metadata or {}),
            enabled,
        )

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

    def _execute(
        self, generation, runner, path, ffmpeg, target_ref, markers, metadata, enabled
    ):
        def loading():
            target = target_ref()
            if target is not None:
                target.waveform_error = None
                target.set_loading(enabled, _("Generating waveform..."))

        self._schedule(generation, loading)
        duration = 0
        try:
            with self._decode_lock:
                runner.check_cancelled()
                if not ffmpeg:
                    raise MediaError(gettext.gettext("FFmpeg is not installed."))
                source = MediaSource.resolve(path, metadata)
                stat = os.stat(source.path)
                identity = (
                    source.path,
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    source.stream_index,
                )
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
                        block = max(
                            64, math.ceil((duration or 60) * rate / self.MAX_POINTS)
                        )
                        collector = PeakCollector(
                            block, rate, self.MAX_POINTS, int(stream["channels"])
                        )
                        result = runner.run(
                            [
                                ffmpeg,
                                "-hide_banner",
                                "-nostdin",
                                "-v",
                                "error",
                                "-xerror",
                                "-protocol_whitelist",
                                "file,pipe",
                                "-i",
                                source.path,
                                "-map",
                                f"0:{stream['index']}",
                                "-vn",
                                "-sn",
                                "-dn",
                                "-c:a",
                                "pcm_f32le",
                                "-f",
                                "f32le",
                                "pipe:1",
                            ],
                            stdout_callback=collector.consume,
                        )
                        if result.returncode:
                            raise MediaError(
                                gettext.gettext("The waveform could not be generated."),
                                result.stderr,
                            )
                        payload, duration = collector.finish()
                        runner.check_cancelled()
                        current = os.stat(source.path)
                        if (
                            stat.st_dev,
                            stat.st_ino,
                            stat.st_size,
                            stat.st_mtime_ns,
                        ) != (
                            current.st_dev,
                            current.st_ino,
                            current.st_size,
                            current.st_mtime_ns,
                        ):
                            raise MediaError(
                                gettext.gettext(
                                    "The source changed while its waveform was being generated."
                                )
                            )
                        self._cache[identity] = payload, duration
                        self._cache_bytes += payload["levels"][0].nbytes
                        while (
                            self._cache_bytes > self.CACHE_BYTES
                            or len(self._cache) > self.CACHE_ENTRIES
                        ):
                            _evicted_key, (old, _evicted_duration) = (
                                self._cache.popitem(last=False)
                            )
                            self._cache_bytes -= old["levels"][0].nbytes
                runner.check_cancelled()

            def deliver():
                target = target_ref()
                if target is None:
                    return
                # Give the UI its own container. Arrays are immutable and can
                # safely be shared with the bounded cache.
                target.set_waveform(dict(payload) if payload else None, duration or 0)
                existing = (
                    target.get_marker_pairs()
                    if hasattr(target, "get_marker_pairs")
                    else []
                )
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
                    target.set_waveform(None, duration or 0)
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
