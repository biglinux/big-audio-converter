"""Media identity, probing and edit validation, independent of GTK."""

import gettext
import hashlib
import json
import math
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .process import MediaError, ProcessRunner


@dataclass(frozen=True)
class MediaSource:
    path: str
    stream_index: int | None = None

    @classmethod
    def resolve(cls, identifier, track_metadata=None):
        identifier = os.fspath(identifier)
        if not identifier or "\0" in identifier:
            raise MediaError(gettext.gettext('Select a valid local media file.'))
        # '::' is legal in a filename. Only explicit queue metadata identifies
        # a virtual track, and a real file always takes precedence.
        if os.path.isfile(identifier):
            return cls(os.path.abspath(identifier))
        metadata = (track_metadata or {}).get(identifier)
        if metadata is None:
            raise MediaError(gettext.gettext('The source file is missing or is not a regular file.'))
        source = metadata.get("source_video", "")
        index = metadata.get("track_index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise MediaError(gettext.gettext('The selected audio track is invalid. Add the file again.'))
        if not os.path.isfile(source):
            raise MediaError(gettext.gettext('The source video is missing. Add the file again.'))
        return cls(os.path.abspath(source), index)


@dataclass(frozen=True)
class Segment:
    start: float
    stop: float
    number: int = 0

    @classmethod
    def from_mapping(cls, value, duration=None, sample_rate=None):
        try:
            start, stop = float(value["start"]), float(value["stop"])
            number = int(value.get("segment_index", 0))
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise MediaError(gettext.gettext('A segment has invalid start or end times.')) from exc
        if not (math.isfinite(start) and math.isfinite(stop) and 0 <= start < stop):
            raise MediaError(gettext.gettext('Every segment must have a finite end time after its start.'))
        if duration is not None and (start >= duration or stop > duration + 1e-6):
            raise MediaError(gettext.gettext('A segment extends beyond the source audio. Adjust its end time.'))
        if sample_rate and round(stop * sample_rate) <= round(start * sample_rate):
            raise MediaError(gettext.gettext('A segment must contain at least one audio sample.'))
        return cls(start, stop, number)

    def as_mapping(self):
        return {"start": self.start, "stop": self.stop,
                "start_str": f"{self.start:.9f}", "stop_str": f"{self.stop:.9f}",
                "segment_index": self.number}


@dataclass(frozen=True)
class ConversionResult:
    source: str
    status: str
    outputs: tuple[str, ...] = ()
    message: str = ""
    details: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def successful(self):
        return self.status == "success"


@dataclass
class BatchResult:
    items: list[ConversionResult] = field(default_factory=list)

    @property
    def outputs(self):
        return tuple(path for item in self.items for path in item.outputs)

    @property
    def successful_sources(self):
        return [item.source for item in self.items if item.successful]


def track_identifier(path, stream_index):
    """Real queue paths are absolute; generated IDs occupy a separate namespace."""
    payload = os.fsencode(os.path.abspath(path)) + b"\0" + str(stream_index).encode("ascii")
    return "bac-track:" + hashlib.sha256(payload).hexdigest()


def source_label(identifier, metadata=None):
    entry = (metadata or {}).get(identifier, {})
    return entry.get("display_name") or os.path.basename(identifier)


def ffprobe_executable(ffmpeg_path=None):
    if ffmpeg_path:
        sibling = Path(ffmpeg_path).with_name("ffprobe")
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
    probe = shutil.which("ffprobe")
    if probe is None:
        raise MediaError(gettext.gettext('FFprobe is not installed. Install the FFmpeg package and try again.'))
    return probe


def probe_media(path, runner=None, ffmpeg_path=None):
    runner = runner or ProcessRunner()
    result = runner.run(
        [ffprobe_executable(ffmpeg_path), "-v", "error", "-show_format", "-show_streams",
         "-of", "json", "-protocol_whitelist", "file,pipe", "-i", os.path.abspath(path)], timeout=15,
    )
    if result.returncode:
        raise MediaError(gettext.gettext('The media file could not be read. It may be damaged or unsupported.'), result.stderr)
    try:
        info = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise MediaError(gettext.gettext('FFprobe returned invalid media information.'), result.stdout[-4096:]) from exc
    if not isinstance(info, dict) or not isinstance(info.get("streams"), list):
        raise MediaError(gettext.gettext('FFprobe returned incomplete media information.'))
    return info


def audio_stream(info, index=None):
    streams = [stream for stream in info["streams"] if stream.get("codec_type") == "audio"]
    if index is not None:
        streams = [stream for stream in streams if stream.get("index") == index]
    if not streams:
        raise MediaError(gettext.gettext('This file does not contain the selected audio track.'))
    # Explicit and stable, unlike FFmpeg's automatic most-channels selection.
    return streams[0]


def audio_duration(info, stream):
    for raw in (stream.get("duration"), info.get("format", {}).get("duration")):
        try:
            duration = float(raw)
            if math.isfinite(duration) and duration > 0:
                return duration
        except (ValueError, TypeError):
            pass
    return None


def pcm_codec(stream):
    """Preserve integer precision or floating-point representation in WAV."""
    sample_format = stream.get("sample_fmt", "")
    codec = stream.get("codec_name", "")
    if sample_format in ("dbl", "dblp") or codec.startswith("pcm_f64"):
        return "pcm_f64le"
    if sample_format in ("flt", "fltp") or codec.startswith("pcm_f32"):
        return "pcm_f32le"
    try:
        bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)
    except (TypeError, ValueError):
        bits = 0
    if bits == 0:
        bits = {"u8": 8, "s16": 16, "s32": 32, "s64": 64}.get(sample_format.rstrip("p"), 16)
    if bits <= 8:
        return "pcm_u8"
    if bits <= 16:
        return "pcm_s16le"
    if bits <= 24:
        return "pcm_s24le"
    if bits <= 32:
        return "pcm_s32le"
    raise MediaError(gettext.gettext('This PCM precision cannot be preserved by the selected WAV encoder.'))
