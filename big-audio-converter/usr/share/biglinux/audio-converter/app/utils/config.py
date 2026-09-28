"""Validated, atomically persisted user configuration with bounded debounce."""

import fcntl
import json
import logging
import math
import os
import tempfile
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger(__name__)


class AppConfig:
    MAX_BYTES = 1024 * 1024
    NUMBERS: ClassVar = {
        "noise_attenuation_db": (0, 100),
        "conversion_volume": (0, 1000),
        "conversion_speed": (0.1, 5),
        "gate_intensity": (0, 1),
        "compressor_intensity": (0, 1),
        "hpf_frequency": (20, 20000),
        "window_width": (320, 32768),
        "window_height": (240, 32768),
        "sidebar_width": (100, 4096),
        "visualizer_height": (80, 4096),
        "audio_channels": (0, 2),
        "cut_audio_mode": (0, 2),
        "cut_output_mode": (0, 1),
    }
    BOOLS: ClassVar = {
        "noise_reduction_enabled",
        "auto_advance_enabled",
        "show_welcome_dialog",
        "gate_enabled",
        "compressor_enabled",
        "hpf_enabled",
        "normalize_enabled",
        "prevent_clipping",
        "allow_precision_reduction",
        "generate_waveforms",
        "cut_audio_enabled",
        "show_mouseover_tips",
        "window_maximized",
    }

    def __init__(self, config_dir=None):
        base = os.environ.get("XDG_CONFIG_HOME", "")
        if not os.path.isabs(base):
            base = os.path.join(str(Path.home()), ".config")
        self.config_dir = str(config_dir or Path(base) / "audio-converter")
        os.makedirs(self.config_dir, mode=0o700, exist_ok=True)
        self.config_file = str(Path(self.config_dir) / "config.json")
        self._lock = threading.RLock()
        self._save_timer = None
        self._save_delay = 0.5
        self._generation = 0
        self._closed = False
        self.modified_keys = set()
        self.load_warning = None
        self.defaults = {
            "show_welcome_dialog": True,
            "noise_reduction_enabled": "false",
            "noise_engine": "dfn3",
            "noise_attenuation_db": "100",
            # Gate (single intensity slider, sqrt curve)
            "gate_enabled": "false",
            "gate_intensity": "0.5",
            # Compressor
            "compressor_enabled": "false",
            "compressor_intensity": "1.0",
            # HPF
            "hpf_enabled": "false",
            "hpf_frequency": "80",
            # Normalization
            "normalize_enabled": "false",
        }

        self.config = self.load_config()

    def _valid(self, key, value):
        if not isinstance(key, str) or len(key) > 128:
            return False
        if isinstance(value, str) and (len(value) > 32768 or "\0" in value):
            return False
        if key == "noise_engine":
            return value in ("dfn3", "dpdfnet")
        if key in self.BOOLS:
            return isinstance(value, bool) or value in ("true", "false")
        if key in self.NUMBERS:
            try:
                numeric = float(value)
                low, high = self.NUMBERS[key]
                return (
                    not isinstance(value, bool)
                    and math.isfinite(numeric)
                    and low <= numeric <= high
                )
            except (ValueError, TypeError, OverflowError):
                return False
        if key == "conversion_format":
            return value in ("copy", "mp3", "flac", "ogg", "wav", "aac", "opus")
        return (
            value is None
            or isinstance(value, (str, bool, int))
            or isinstance(value, float)
            and math.isfinite(value)
        )

    def _sanitize(self, values):
        if not isinstance(values, dict):
            raise TypeError("Configuration must be a JSON object")
        return {key: value for key, value in values.items() if self._valid(key, value)}

    @staticmethod
    def _reject_constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")

    def _read(self):
        with open(self.config_file, "r", encoding="utf-8") as handle:
            text = handle.read(self.MAX_BYTES + 1)
        if len(text) > self.MAX_BYTES:
            raise ValueError("Configuration exceeds its size limit")
        return self._sanitize(json.loads(text, parse_constant=self._reject_constant))

    def load_config(self):
        with self._lock:
            values = dict(self.defaults)
            try:
                values.update(self._read())
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError) as exc:
                self.load_warning = str(exc)
                logger.warning(
                    "Invalid configuration; using safe defaults. The original file is preserved."
                )
            return values

    def save_config(self):
        with self._lock:
            if self._closed:
                return False
            temporary = None
            try:
                lock_fd = os.open(
                    str(Path(self.config_dir) / ".config.lock"),
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(lock_fd, "w") as lock_file:
                    fcntl.flock(lock_file, fcntl.LOCK_EX)
                    values = dict(self.defaults)
                    try:
                        values.update(self._read())
                    except FileNotFoundError:
                        pass
                    except (OSError, ValueError, TypeError):
                        # Do not destroy a corrupt file before a replacement is
                        # ready. Copy it to a private diagnostic backup first.
                        if os.path.isfile(self.config_file) and not os.path.islink(
                            self.config_file
                        ):
                            backup = (
                                Path(self.config_dir)
                                / f"config.invalid-{uuid.uuid4().hex}.json"
                            )
                            with (
                                open(self.config_file, "rb") as source,
                                open(backup, "xb") as target,
                            ):
                                os.chmod(backup, 0o600)
                                target.write(source.read(self.MAX_BYTES))
                    values.update({key: self.config[key] for key in self.modified_keys})
                    fd, temporary = tempfile.mkstemp(
                        prefix=".config-", suffix=".json", dir=self.config_dir
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(
                            values,
                            handle,
                            ensure_ascii=False,
                            allow_nan=False,
                            indent=2,
                        )
                        handle.write("\n")
                    os.replace(temporary, self.config_file)
                    temporary = None
                    self.config = values
                    self.modified_keys.clear()
                    return True
            except (OSError, ValueError, TypeError):
                logger.exception("Could not save application configuration")
                return False
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass

    def get(self, key, default=None):
        with self._lock:
            return deepcopy(self.config.get(key, default))

    def set(self, key, value):
        if not self._valid(key, value):
            raise ValueError(f"Invalid configuration value for {key}")
        with self._lock:
            if self._closed:
                return
            if self.config.get(key) == value:
                return
            self.config[key] = deepcopy(value)
            self.modified_keys.add(key)
            self._schedule_save()

    def _schedule_save(self):
        self._generation += 1
        generation = self._generation
        if self._save_timer is not None:
            self._save_timer.cancel()

        def save():
            with self._lock:
                if not self._closed and generation == self._generation:
                    self._save_timer = None
                    self.save_config()

        self._save_timer = threading.Timer(self._save_delay, save)
        self._save_timer.name = "audio-config-save"
        self._save_timer.daemon = True
        self._save_timer.start()

    def flush(self):
        with self._lock:
            self._generation += 1
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
            return self.save_config() if self.modified_keys else True

    def close(self):
        with self._lock:
            timer = self._save_timer
            self.flush()
            self._closed = True
        if timer is not None and timer is not threading.current_thread():
            timer.join(timeout=1)
