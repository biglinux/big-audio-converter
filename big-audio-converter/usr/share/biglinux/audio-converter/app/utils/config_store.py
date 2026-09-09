"""Validated, atomic configuration storage with cross-process merge locking."""

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import uuid

NUMERIC = {
    "conversion_volume": (0, 1000, 100), "conversion_speed": (0.1, 5, 1),
    "noise_reduction_strength": (0, 1, 1), "noise_model": (0, 1, 0),
    "noise_speech_strength": (0, 1, 1), "noise_lookahead": (0, 500, 0),
    "noise_voice_enhance": (0, 1, 0), "gate_intensity": (0, 1, 0.5),
    "compressor_intensity": (0, 1, 1), "hpf_frequency": (10, 20000, 80),
    "transient_attack": (-1, 1, -0.5), "audio_channels": (0, 2, 0),
    "cut_audio_mode": (0, 2, 0), "cut_output_mode": (0, 1, 0),
}
FORMATS = {"copy", "mp3", "flac", "wav", "ogg", "aac", "opus"}


def normalize(values, defaults):
    if not isinstance(values, dict):
        raise ValueError("Configuration must be a JSON object")
    result = dict(defaults)
    for key, value in values.items():
        if not isinstance(key, str):
            continue
        if key in NUMERIC:
            low, high, fallback = NUMERIC[key]
            try:
                number = float(value)
                valid = not isinstance(value, bool) and math.isfinite(number) and low <= number <= high
                if key in ("noise_model", "audio_channels", "cut_audio_mode", "cut_output_mode"):
                    valid = valid and number.is_integer()
            except (TypeError, ValueError, OverflowError):
                valid = False
            if not valid:
                value = defaults.get(key, str(fallback))
        elif key in ("conversion_format", "default_format"):
            if not isinstance(value, str) or value not in FORMATS:
                value = defaults.get(key, "mp3")
        elif key == "eq_bands":
            try:
                bands = [float(item) for item in value.split(",")]
                if len(bands) != 10 or not all(math.isfinite(v) and -40 <= v <= 40 for v in bands):
                    raise ValueError("Invalid equalizer bands")
            except (AttributeError, TypeError, ValueError):
                value = "0,0,0,0,0,0,0,0,0,0"
        elif isinstance(defaults.get(key), bool):
            if isinstance(value, str) and value.lower() in ("true", "false"):
                value = value.lower() == "true"
            elif not isinstance(value, bool):
                value = defaults[key]
        elif key.endswith("_enabled") or key in ("conversion_noise_reduction", "noise_model_blend", "generate_waveforms"):
            value = str(value).lower() if str(value).lower() in ("true", "false") else "false"
        elif isinstance(defaults.get(key), str) and not isinstance(value, str):
            value = defaults[key]
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            continue
        result[key] = value
    return result


class ConfigStore:
    """Merge modified keys while holding a stable sibling lock file."""

    def __init__(self, path, defaults):
        self.path = Path(path)
        self.defaults = defaults

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self):
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                text = stream.read(1024 * 1024 + 1)
            if len(text) > 1024 * 1024:
                raise ValueError("Configuration exceeds its size limit")
            return normalize(json.loads(text), self.defaults)
        except FileNotFoundError:
            return dict(self.defaults)
        except (ValueError, UnicodeError):
            # Preserve malformed user data for diagnosis rather than overwriting it.
            backup = self.path.with_name(self.path.name + ".invalid-" + uuid.uuid4().hex)
            os.rename(self.path, backup)
            return dict(self.defaults)

    def read(self):
        with self.locked():
            return self._read()

    def save(self, updates):
        with self.locked():
            current = self._read()
            current.update(updates)
            current = normalize(current, self.defaults)
            encoded = json.dumps(current, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            if len(encoded.encode("utf-8")) > 1024 * 1024:
                raise ValueError("Configuration exceeds its size limit")
            fd, name = tempfile.mkstemp(prefix=".config-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            return current
