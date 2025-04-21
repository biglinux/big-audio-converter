"""
Application configuration management.
"""

import os
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class AppConfig:
    """Manage application configuration settings."""

    def __init__(self):
        """Initialize the configuration manager."""
        # Determine config directory
        self.config_dir = os.path.join(
            os.path.expanduser("~"), ".config", "audio-converter"
        )

        # Ensure config directory exists
        os.makedirs(self.config_dir, exist_ok=True)

        self.config_file = os.path.join(self.config_dir, "config.json")

        # Default settings
        self.defaults = {
            "last_directory": str(Path.home()),
            "default_output_directory": str(Path.home()),
            "default_format": "mp3",
            "default_preset": "MP3 Standard",
            "auto_play_preview": True,
            "confirm_overwrite": True,
        }

        # Load configuration
        self.config = self.load_config()

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
            config = self.config

        try:
            with open(self.config_file, "w") as f:
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
        """Set a configuration value and save it."""
        self.config[key] = value
        self.save_config()
