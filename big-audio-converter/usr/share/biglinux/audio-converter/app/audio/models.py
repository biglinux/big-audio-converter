"""Media-domain values shared by conversion, tests and presentation."""

from dataclasses import dataclass, field
import math
import os


@dataclass(frozen=True)
class MediaSource:
    path: str
    stream_index: int | None = None

    @classmethod
    def resolve(cls, identifier, track_metadata=None):
        path = os.fspath(identifier)
        if not path or "\x00" in path:
            raise ValueError("A nonempty local media path is required")
        metadata = (track_metadata or {}).get(path)
        # Real filenames take precedence over legacy virtual-track identifiers.
        if not os.path.isfile(path) and metadata is not None:
            index = metadata["track_index"]
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError("Invalid audio stream index")
            return cls(os.path.abspath(metadata["source_video"]), index)
        return cls(os.path.abspath(path))


@dataclass(frozen=True)
class Segment:
    start: float
    stop: float

    @property
    def duration(self):
        return self.stop - self.start

    @classmethod
    def from_mapping(cls, value, duration=None):
        start = finite_number(value.get("start"), "Segment start", 0, 1e10)
        stop = finite_number(value.get("stop"), "Segment end", 0, 1e10)
        start, stop = sorted((start, stop))
        if stop - start < 0.1 - 1e-9:
            raise ValueError("Each segment must be at least 0.1 seconds long")
        if duration is not None and duration > 0 and stop > duration + 1e-6:
            raise ValueError("A segment extends beyond the end of the source")
        return cls(start, stop)

    def as_dict(self):
        return {"start": self.start, "stop": self.stop,
                "start_str": format_timestamp(self.start), "stop_str": format_timestamp(self.stop)}


def finite_number(value, name, minimum, maximum):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def format_timestamp(seconds):
    if seconds is None:
        return ""
    value = finite_number(seconds, "Time", 0, 1e10)
    hours = int(value // 3600)
    minutes = int(value % 3600 // 60)
    return f"{hours:02d}:{minutes:02d}:{value % 60:09.6f}"


@dataclass(frozen=True)
class FileResult:
    source: str
    status: str
    outputs: tuple[str, ...] = ()
    message: str = ""
    diagnostics: str = ""
    warnings: tuple[str, ...] = ()


@dataclass
class BatchResult:
    files: list[FileResult] = field(default_factory=list)

    @property
    def outputs(self):
        return [path for item in self.files for path in item.outputs]

    @property
    def successful_sources(self):
        return [item.source for item in self.files if item.status == "success"]

    @property
    def failed_sources(self):
        return [item.source for item in self.files if item.status == "failed"]
