"""Persistence tests use actual configuration files, including failed writes."""

import json
from pathlib import Path

import pytest
from app.utils.config import AppConfig
from app.utils.config_store import ConfigStore


def test_failed_replace_keeps_previous_json(tmp_path, monkeypatch):
    config = AppConfig(tmp_path)
    config.set("conversion_speed", "1.5")
    assert config.flush()
    original = Path(config.config_file).read_bytes()
    def fail(*args):
        raise OSError("Simulated full filesystem")
    monkeypatch.setattr("app.utils.config_store.os.replace", fail)
    config.set("conversion_speed", "2")
    assert not config.flush()
    assert Path(config.config_file).read_bytes() == original
    assert not list(tmp_path.glob(".config-*"))
    config.close()


def test_two_instances_merge_disjoint_modified_keys(tmp_path):
    first = AppConfig(tmp_path)
    second = AppConfig(tmp_path)
    first.set("conversion_speed", "2")
    second.set("conversion_volume", "75")
    first.close()
    second.close()
    third = AppConfig(tmp_path)
    assert third.get("conversion_speed") == "2"
    assert third.get("conversion_volume") == "75"
    third.close()


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", [], {}, None])
def test_invalid_persisted_speed_cannot_reach_dsp(tmp_path, value):
    (tmp_path / "config.json").write_text(json.dumps({"conversion_speed": value}))
    config = AppConfig(tmp_path)
    assert float(config.get("conversion_speed")) == 1
    config.close()


def test_corrupted_json_is_preserved_for_diagnosis(tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    config = AppConfig(tmp_path)
    backups = list(tmp_path.glob("config.json.invalid-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "{not json"
    assert config.get("default_format") == "mp3"
    config.close()
