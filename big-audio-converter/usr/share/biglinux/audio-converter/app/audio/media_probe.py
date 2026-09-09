"""Bounded, cancellable local-media probing with a size-limited stat-keyed cache."""

from collections import OrderedDict
import copy
import json
import math
import os
from pathlib import Path
import shutil
import threading

from .process_runner import ProcessRunner

_cache = OrderedDict()
_cache_lock = threading.Lock()
_cache_bytes = 0
_MAX_CACHE_BYTES = 8 * 1024 * 1024
_MAX_CACHE_ITEMS = 64


def probe_media(path, ffmpeg="ffmpeg", runner=None):
    global _cache_bytes
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ValueError("Select a readable local media file")
    candidate = str(Path(ffmpeg).with_name("ffprobe"))
    executable = candidate if os.path.isfile(candidate) else shutil.which("ffprobe")
    if not executable:
        raise ValueError("FFprobe is not installed")
    status = os.stat(path)
    key = (path, status.st_dev, status.st_ino, status.st_size,
           status.st_mtime_ns, status.st_ctime_ns, executable)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return copy.deepcopy(cached[0])
    runner = runner if runner is not None else ProcessRunner()
    result = runner.run([executable, "-v", "error", "-protocol_whitelist", "file,pipe",
                         "-show_streams", "-show_format", "-of", "json", path], timeout=10)
    if result.returncode:
        raise ValueError("The media could not be read. It may be damaged or unsupported.")
    info = json.loads(result.stdout)
    if not isinstance(info, dict) or not isinstance(info.get("streams", []), list):
        raise ValueError("FFprobe returned invalid media information")
    after = os.stat(path)
    if (status.st_size, status.st_mtime_ns, status.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("The media file changed while it was being analyzed")
    size = len(result.stdout)
    with _cache_lock:
        if size <= _MAX_CACHE_BYTES:
            old = _cache.pop(key, None)
            if old is not None:
                _cache_bytes -= old[1]
            _cache[key] = (copy.deepcopy(info), size)
            _cache_bytes += size
            while len(_cache) > _MAX_CACHE_ITEMS or _cache_bytes > _MAX_CACHE_BYTES:
                _, (_, removed_size) = _cache.popitem(last=False)
                _cache_bytes -= removed_size
    return info


def audio_stream(info, index=None):
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "audio" and (index is None or stream.get("index") == index):
            return stream
    raise ValueError("The selected file or stream does not contain audio")


def media_duration(info, stream=None):
    value = (stream or {}).get("duration") or info.get("format", {}).get("duration")
    try:
        duration = float(value)
        return duration if math.isfinite(duration) and duration > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def clear_probe_cache():
    global _cache_bytes
    with _cache_lock:
        _cache.clear()
        _cache_bytes = 0
