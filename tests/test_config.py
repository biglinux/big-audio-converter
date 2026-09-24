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
def config(tmp_path):
    """Exercise the real configuration lifecycle in an isolated directory."""
    cfg = AppConfig(config_dir=tmp_path / "config")
    cfg._save_delay = 0.01
    yield cfg
    cfg.close()


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


def test_legacy_noise_settings_do_not_enable_a_different_model(tmp_path):
    path = tmp_path / "config.json"
    legacy = {
        "conversion_noise_reduction": "true",
        "noise_model": "1",
        "noise_reduction_strength": "0.7",
    }
    path.write_text(json.dumps(legacy))
    config = AppConfig(config_dir=tmp_path)
    try:
        assert config.get("noise_reduction_enabled") == "false"
        config.set("noise_engine", "dpdfnet")
        config.set("noise_attenuation_db", 35)
        config.set("noise_reduction_enabled", True)
        assert config.flush()
        saved = json.loads(path.read_text())
        assert all(saved[k] == value for k, value in legacy.items())
        loaded = config.load_config()
        assert loaded["noise_engine"] == "dpdfnet"
        assert loaded["noise_attenuation_db"] == 35
        assert loaded["noise_reduction_enabled"] is True
        with pytest.raises(ValueError):
            config.set("noise_engine", "dfn3ll")
    finally:
        config.close()
