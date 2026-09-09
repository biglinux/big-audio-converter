"""
Application configuration management.
"""

import gettext
from .config_store import ConfigStore, normalize
import json
import logging
import os
from pathlib import Path
from threading import Timer, RLock

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class AppConfig:
    """Manage application configuration settings."""

    def __init__(self, config_dir=None):
        """Initialize the configuration manager."""
        # Determine config directory
        root = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        if not os.path.isabs(root):
            root = str(Path.home() / ".config")
        self.config_dir = os.fspath(config_dir) if config_dir is not None else os.path.join(root, "audio-converter")
        self._lock = RLock()
        self._io_lock = RLock()
        self._closed = False

        # Ensure config directory exists with restricted permissions
        os.makedirs(self.config_dir, mode=0o700, exist_ok=True)

        self.config_file = os.path.join(self.config_dir, "config.json")

        # Default settings
        self.defaults = {
            "last_directory": str(Path.home()),
            "default_output_directory": str(Path.home()),
            "default_format": "mp3",
            "default_preset": _("MP3 Standard"),
            "auto_play_preview": True,
            "confirm_overwrite": True,
            "show_welcome_dialog": True,
            # Noise reduction
            "conversion_noise_reduction": "false",
            "noise_reduction_strength": "1.0",
            # GTCRN advanced controls
            "noise_model": "0",
            "noise_speech_strength": "1.0",
            "noise_lookahead": "0",
            "noise_voice_enhance": "0.0",
            "noise_model_blend": "false",
            # Gate (single intensity slider, sqrt curve)
            "gate_enabled": "false",
            "gate_intensity": "0.5",
            # Compressor
            "compressor_enabled": "false",
            "compressor_intensity": "1.0",
            # HPF
            "hpf_enabled": "false",
            "hpf_frequency": "80",
            # Transient
            "transient_enabled": "false",
            "transient_attack": "-0.5",
            # EQ
            "eq_enabled": "false",
            "eq_preset": "flat",
            "eq_bands": "0,0,0,0,0,0,0,0,0,0",
            # Normalization
            "normalize_enabled": "false",
        }

        # Load configuration
        self.config = self.load_config()

        # Track which keys have been modified in this instance
        self.modified_keys = set()

        # Debounce timer for batching saves
        self._save_timer = None
        self._save_delay = 0.5  # seconds

    def load_config(self):
        try:
            return ConfigStore(self.config_file, self.defaults).read()
        except OSError as error:
            logger.warning("Preferences could not be read: %s", error)
            return self.defaults.copy()

    def save_config(self, config=None):
        """Merge only changed keys and retain them if publication fails."""
        with self._io_lock:
            with self._lock:
                updates = dict(config) if config is not None else {key: self.config[key] for key in self.modified_keys}
            if not updates:
                return True
            try:
                saved = ConfigStore(self.config_file, self.defaults).save(updates)
            except (OSError, ValueError, TypeError) as error:
                logger.warning("Preferences could not be saved: %s", error)
                return False
            with self._lock:
                for key, value in updates.items():
                    if self.config.get(key) == value:
                        self.modified_keys.discard(key)
                for key, value in saved.items():
                    if key not in self.modified_keys:
                        self.config[key] = value
            return True

    def get(self, key, default=None):
        with self._lock:
            return self.config.get(key, default)

    def set(self, key, value):
        with self._lock:
            if self._closed:
                return
            self.config[key] = normalize({key: value}, self.defaults).get(key, self.defaults.get(key))
            self.modified_keys.add(key)
            self._schedule_save()

    def _schedule_save(self):
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            if self._closed:
                return
            self._save_timer = Timer(self._save_delay, self.save_config)
            self._save_timer.daemon = True
            self._save_timer.start()

    def flush(self):
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
        return self.save_config()

    def close(self):
        """Stop scheduling new writes and finish the final merge."""
        with self._lock:
            self._closed = True
        return self.flush()
