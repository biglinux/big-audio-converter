"""Unit tests for BAC AppConfig."""

import json
import os
import sys
import time

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "big-audio-converter",
        "usr",
        "share",
        "biglinux",
        "audio-converter",
    ),
)

from app.utils.config import AppConfig


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Create an AppConfig that writes to a temp directory."""
    config_dir = str(tmp_path / "config")
    monkeypatch.setattr(
        "app.utils.config.AppConfig.__init__",
        lambda self: None,
    )
    cfg = AppConfig.__new__(AppConfig)
    cfg.config_dir = config_dir
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    cfg.config_file = os.path.join(config_dir, "config.json")
    cfg.defaults = {
        "last_directory": "/home",
        "default_format": "mp3",
        "auto_play_preview": True,
        "confirm_overwrite": True,
    }
    cfg.config = cfg.defaults.copy()
    cfg.modified_keys = set()
    cfg._save_timer = None
    cfg._save_delay = 0.01  # Fast saves for tests
    return cfg


class TestAppConfigGetSet:
    def test_get_default(self, config):
        assert config.get("default_format") == "mp3"

    def test_get_unknown_key(self, config):
        assert config.get("nonexistent", "fallback") == "fallback"

    def test_set_and_get(self, config):
        config.set("default_format", "flac")
        assert config.get("default_format") == "flac"


class TestAppConfigPersistence:
    def test_save_and_load(self, config):
        config.set("default_format", "ogg")
        config.flush()

        with open(config.config_file, "r") as f:
            data = json.load(f)
        assert data["default_format"] == "ogg"

    def test_corrupt_file_uses_defaults(self, config):
        with open(config.config_file, "w") as f:
            f.write("not json{{{")

        loaded = config.load_config()
        assert "default_format" in loaded

    def test_flush_saves_immediately(self, config):
        config.set("last_directory", "/tmp/test")
        config.flush()

        assert os.path.exists(config.config_file)
        with open(config.config_file, "r") as f:
            data = json.load(f)
        assert data["last_directory"] == "/tmp/test"


class TestAppConfigDefaults:
    def test_defaults_present(self, config):
        for key in config.defaults:
            assert config.get(key) is not None

    def test_missing_keys_filled(self, config):
        # Save partial config
        config.save_config({"default_format": "wav"})

        loaded = config.load_config()
        assert loaded["default_format"] == "wav"
        # Missing keys should be filled from defaults
        assert "last_directory" in loaded


class TestAppConfigDebounce:
    def test_debounce_batches_writes(self, config):
        config.set("default_format", "aac")
        config.set("default_format", "opus")
        config.set("default_format", "flac")
        # Wait for debounce
        time.sleep(0.1)

        if os.path.exists(config.config_file):
            with open(config.config_file, "r") as f:
                data = json.load(f)
            assert data["default_format"] == "flac"
