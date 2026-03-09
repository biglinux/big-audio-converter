"""
Application configuration management.
"""

import gettext
import json
import logging
import os
from pathlib import Path
from threading import Timer

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class AppConfig:
    """Manage application configuration settings."""

    def __init__(self):
        """Initialize the configuration manager."""
        # Determine config directory
        self.config_dir = os.path.join(
            os.path.expanduser("~"), ".config", "audio-converter"
        )

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
        """Load configuration from file or create defaults if not found."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    config = json.load(f)
                    logger.info(f"Loaded configuration from {self.config_file}")

                    # Make sure all default keys are present
                    for key, value in self.defaults.items():
                        if key not in config:
                            config[key] = value

                    return config
            except Exception as e:
                logger.error(f"Error loading configuration: {str(e)}")

        # If file doesn't exist or there's an error, use defaults
        logger.info("Using default configuration")
        self.save_config(self.defaults)  # Save defaults for next time
        return self.defaults.copy()

    def save_config(self, config=None):
        """Save configuration to file."""
        if config is None:
            # Reload the file first to get latest values from other instances
            try:
                if os.path.exists(self.config_file):
                    with open(self.config_file, "r") as f:
                        file_config = json.load(f)
                    # Update our config with file values, but preserve our modified keys
                    for key, value in file_config.items():
                        if key not in self.modified_keys:
                            self.config[key] = value
            except Exception as e:
                logger.warning(f"Could not reload config before save: {str(e)}")

            config = self.config

        try:
            fd = os.open(self.config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(config, f, indent=2)
            logger.info(f"Saved configuration to {self.config_file}")
            return True
        except Exception as e:
            logger.error(f"Error saving configuration: {str(e)}")
            return False

    def get(self, key, default=None):
        """Get a configuration value."""
        return self.config.get(key, default)

    def set(self, key, value):
        """Set a configuration value with debounced save."""
        self.config[key] = value
        self.modified_keys.add(key)
        self._schedule_save()

    def _schedule_save(self):
        """Schedule a debounced save to disk."""
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = Timer(self._save_delay, self.save_config)
        self._save_timer.daemon = True
        self._save_timer.start()

    def flush(self):
        """Force an immediate save of pending changes."""
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None
        if self.modified_keys:
            self.save_config()
