"""Pauses in speech, found on audio cleaned by DPDFNet.

Background noise fills the pauses of a raw recording. DPDFNet at full strength
leaves it near -70 dBFS, so a threshold set from the speech level separates the
words from the pauses between them. The analysis is mono: a pause on one
channel only, such as an interview recorded on separate channels, reads as
sound.
"""

import gettext
import logging
import math
import os
import threading
from array import array
from collections import OrderedDict

from app.main_loop import MainLoopSources

from .media import audio_duration, audio_stream, file_identity, probe_media
from .process import CancelledError, MediaError, ProcessRunner
from .profiles import NOISE_PLUGINS

_ = gettext.gettext
logger = logging.getLogger(__name__)

STEP = 0.01  # seconds per level: 480 samples at 48 kHz
KEEP = 0.15  # seconds of each pause kept beside speech, so words do not collide
PAUSE_LENGTHS = (0.5, 1.0, 2.0)

LEVEL = b"lavfi.astats.Overall.Peak_level="
# One peak level per 10 ms of mono audio, cleaned at the model's full strength.
PIPELINE = (
    '"$@" | "$DENOISER" --channels 1 --attenuation "$ATTENUATION" | '
    '"$FFMPEG" -hide_banner -nostdin -v error -f f32le -ar 48000 -ac 1 -i pipe:0 '
    "-af asetnsamples=n=480:p=0,astats=metadata=1:reset=1:measure_perchannel=none:"
    "measure_overall=Peak_level,ametadata=mode=print:"
    "key=lavfi.astats.Overall.Peak_level:file=- -f null -"
)


class LevelReader:
    """Collect the levels from FFmpeg's metadata lines, split across chunks."""

    def __init__(self, duration=0, progress=None):
        self.levels = array("f")
        self.duration = duration
        self.progress = progress
        self._rest = b""

    def consume(self, data):
        lines = (self._rest + data).split(b"\n")
        self._rest = lines.pop()
        for line in lines:
            if line.startswith(LEVEL):
                level = float(line[len(LEVEL) :])
                if math.isnan(level):
                    raise MediaError(_("The pauses could not be measured."))
                self.levels.append(level)
        if self.progress and self.duration:
            self.progress(min(1.0, len(self.levels) * STEP / self.duration))


def measure_levels(source, ffmpeg, denoiser, runner, progress=None):
    """Peak levels in dBFS, one per STEP, of the cleaned mono source."""
    info = probe_media(source.path, runner, ffmpeg)
    stream = audio_stream(info, source.stream_index)
    reader = LevelReader(audio_duration(info, stream), progress)
    decode = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        source.path,
        "-map",
        f"0:{stream['index']}",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    ]
    result = runner.run(
        ["bash", "-o", "pipefail", "-c", PIPELINE, "bash", *decode],
        stdout_callback=reader.consume,
        env=os.environ
        | {
            "DENOISER": denoiser,
            "FFMPEG": ffmpeg,
            "ATTENUATION": str(NOISE_PLUGINS["dpdfnet"][3]),
        },
    )
    if result.returncode:
        raise MediaError(_("The pauses could not be measured."), result.stderr)
    return reader.levels


def find_pauses(levels, min_pause):
    """The pauses of at least ``min_pause`` seconds, less KEEP beside speech.

    Silence is anything 35 dB under the loud end of the speech, bounded so a
    quiet recording still has a floor and a loud one cannot lose soft words.
    """
    if not levels:
        return []
    ordered = sorted(levels)
    threshold = min(-40.0, max(-65.0, ordered[int(0.95 * (len(ordered) - 1))] - 35))
    end = len(levels)
    pauses = []

    def add(first, last):
        if (last - first) * STEP < min_pause:
            return
        start = first * STEP + (KEEP if first else 0)
        stop = last * STEP - (KEEP if last < end else 0)
        pauses.append({"start": round(start, 3), "stop": round(stop, 3)})

    first = None
    for index, level in enumerate(levels):
        if level < threshold:
            if first is None:
                first = index
        elif first is not None:
            add(first, index)
            first = None
    if first is not None:
        add(first, end)
    return pauses


def find_speech(levels, min_pause):
    """The parts between the pauses: what marking keeps and exports."""
    speech, start = [], 0.0
    for pause in find_pauses(levels, min_pause):
        if pause["start"] > start:
            speech.append({"start": start, "stop": pause["start"]})
        start = pause["stop"]
    end = round(len(levels) * STEP, 3)
    if end > start:
        speech.append({"start": start, "stop": end})
    return speech


class PauseFinder:
    """Measure one source at a time off the GTK thread; keep the last four.

    Callbacks run on the GTK thread and never for a superseded analysis.
    """

    CACHE_ENTRIES = 4

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._runner = None
        self._thread = None
        self._cache = OrderedDict()
        self._sources = MainLoopSources()

    @staticmethod
    def identity(source):
        return (source.path, *file_identity(source.path), source.stream_index)

    def cached(self, key):
        with self._lock:
            levels = self._cache.get(key)
            if levels is not None:
                self._cache.move_to_end(key)
            return levels

    def start(self, key, source, ffmpeg, denoiser, progress, done, failed):
        self.cancel()
        with self._lock:
            generation = self._generation
            runner = self._runner = ProcessRunner()
        shown = [-1]

        def report(fraction):
            percent = int(fraction * 100)
            if percent != shown[0]:
                shown[0] = percent
                self._deliver(generation, progress, fraction)

        def finish():
            with self._lock:
                if self._runner is runner:
                    self._runner = None

        def work():
            # Every outcome clears the runner before it is reported, so the
            # popover never shows a finished analysis as still running.
            try:
                levels = measure_levels(source, ffmpeg, denoiser, runner, report)
            except CancelledError:
                return
            except (MediaError, OSError, ValueError) as exc:
                message = str(exc)
            except Exception:
                logger.exception("Unexpected failure while measuring pauses")
                message = _("The pauses could not be measured.")
            else:
                with self._lock:
                    self._cache[key] = levels
                    while len(self._cache) > self.CACHE_ENTRIES:
                        self._cache.popitem(last=False)
                finish()
                self._deliver(generation, done, key, levels)
                return
            finish()
            self._deliver(generation, failed, message)

        self._thread = threading.Thread(target=work, name="audio-pauses")
        self._thread.start()

    def _deliver(self, generation, callback, *args):
        self._sources.idle(self._if_current, generation, callback, args)

    def _if_current(self, generation, callback, args):
        if generation == self._generation:
            callback(*args)

    @property
    def busy(self):
        return self._runner is not None

    def cancel(self):
        with self._lock:
            self._generation += 1
            if self._runner:
                self._runner.cancel()
                self._runner = None

    def cleanup(self):
        self.cancel()
        self._sources.close()
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            self._cache.clear()
