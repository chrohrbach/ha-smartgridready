"""Tests for the user config loader."""

from __future__ import annotations

from pathlib import Path

from src.config_loader import (
    DeviceConfig,
    SensorMap,
    UserConfig,
    ensure_example,
    load_user_config,
)


def test_load_minimal_yaml(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sensors:\n"
        "  spot_price: sensor.price\n"
        "devices:\n"
        "  - name: Heat Pump\n"
        "    eid: SGr_xxxx\n"
        "    properties:\n"
        "      ip: 1.2.3.4\n"
        "      port: 502\n",
        encoding="utf-8",
    )
    user = load_user_config(cfg)
    assert isinstance(user, UserConfig)
    assert user.sensors.spot_price == "sensor.price"
    assert len(user.devices) == 1
    assert user.devices[0].name == "Heat Pump"
    assert user.devices[0].properties == {"ip": "1.2.3.4", "port": 502}


def test_load_writes_example_when_missing(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    assert not cfg.exists()
    user = load_user_config(cfg)
    assert cfg.exists()
    assert (cfg.parent / "config.example.yaml").exists()
    # Returns an empty-but-valid config from the freshly-written starter.
    assert isinstance(user, UserConfig)


def test_load_skips_invalid_devices(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "devices:\n"
        "  - name: Good\n"
        "    eid: ok\n"
        "  - name: BadNoEid\n"
        "  - eid: BadNoName\n",
        encoding="utf-8",
    )
    user = load_user_config(cfg)
    assert [d.name for d in user.devices] == ["Good"]


def test_load_rules(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "rules:\n"
        "  - device: HP\n"
        "    profile: SG-ReadyStates\n"
        "    data_point: SGReadyState\n"
        "    min_interval: 20\n"
        "    conditions:\n"
        "      - when: spot_price < 0.10\n"
        "        value: 3\n"
        "      - default: true\n"
        "        value: 1\n",
        encoding="utf-8",
    )
    user = load_user_config(cfg)
    assert len(user.rules) == 1
    assert user.rules[0].min_interval == 20
    assert len(user.rules[0].conditions) == 2


def test_sensor_map_extras(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sensors:\n"
        "  spot_price: sensor.price\n"
        "  my_custom_metric: sensor.custom\n",
        encoding="utf-8",
    )
    user = load_user_config(cfg)
    assert user.sensors.spot_price == "sensor.price"
    assert user.sensors.extra == {"my_custom_metric": "sensor.custom"}


def test_load_invalid_yaml_returns_empty(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(":::not valid yaml:::", encoding="utf-8")
    user = load_user_config(cfg)
    assert isinstance(user, UserConfig)
    assert user.devices == []


def test_device_by_name():
    user = UserConfig(devices=[
        DeviceConfig(name="A", eid="x"),
        DeviceConfig(name="B", eid="y"),
    ])
    assert user.device_by_name("B").eid == "y"
    assert user.device_by_name("missing") is None


def test_ensure_example_idempotent(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    ensure_example(cfg)
    body = cfg.read_text(encoding="utf-8")
    ensure_example(cfg)
    assert cfg.read_text(encoding="utf-8") == body
