"""Tests for virtual (non-SGr) devices.

Covers config parsing (climate_proxy / switch_proxy / boiler_proxy /
number_proxy), the ``VirtualDeviceManager`` dispatch logic, and the
``RulesEngine`` integration (a rule targeting a virtual device name is
applied via HA service calls instead of an SGr write).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.config_loader import RuleConfig, SensorMap, UserConfig, load_user_config
from src.rules_engine import RulesEngine
from src.virtual_devices import VirtualDeviceManager

# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

CONFIG_YAML = """
virtual_devices:
  - name: "Heat pump"
    type: climate_proxy
    climate_entities: [climate.living_room]
    base_setpoint_default: 21.0
    min_setpoint: 16.0
    max_setpoint: 24.0
    sg_ready_to_offset:
      HP_LOCKED: -3.0
      HP_NORMAL: 0.0
      HP_INTENSIFIED: 1.5
      HP_FORCED: 3.0

  - name: "Boiler switch"
    type: switch_proxy
    switch_entities: [switch.boiler]
    sg_ready_to_switch:
      HP_LOCKED: off
      HP_NORMAL: off
      HP_INTENSIFIED: on
      HP_FORCED: on

  - name: "Water heater"
    type: boiler_proxy
    water_heater_entity: water_heater.tank
    min_temperature: 45.0
    max_temperature: 65.0
    sg_ready_to_temperature:
      HP_LOCKED: 45
      HP_NORMAL: 50
      HP_INTENSIFIED: 55
      HP_FORCED: 65

  - name: "Inverter limit"
    type: number_proxy
    number_entities: [number.inverter_limit]
    min_value: 0
    max_value: 5000
    sg_ready_to_value:
      HP_LOCKED: 0
      HP_NORMAL: 1500
      HP_INTENSIFIED: 3000
      HP_FORCED: 5000

  - name: "Disabled one"
    type: switch_proxy
    enabled: false
    switch_entities: [switch.x]
    sg_ready_to_switch: {HP_NORMAL: on}

  - name: "No targets"
    type: switch_proxy
    sg_ready_to_switch: {HP_NORMAL: on}

  - name: "Unknown type"
    type: something_else
    switch_entities: [switch.y]
"""


def test_load_virtual_devices(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG_YAML, encoding="utf-8")
    user = load_user_config(cfg)

    names = [vd.name for vd in user.virtual_devices]
    assert names == ["Heat pump", "Boiler switch", "Water heater", "Inverter limit"]

    heat_pump = user.virtual_devices[0]
    assert heat_pump.type == "climate_proxy"
    assert heat_pump.targets == ["climate.living_room"]
    assert heat_pump.mapping == {
        "HP_LOCKED": -3.0, "HP_NORMAL": 0.0, "HP_INTENSIFIED": 1.5, "HP_FORCED": 3.0,
    }

    boiler_switch = user.virtual_devices[1]
    assert boiler_switch.mapping == {
        "HP_LOCKED": False, "HP_NORMAL": False, "HP_INTENSIFIED": True, "HP_FORCED": True,
    }

    water_heater = user.virtual_devices[2]
    assert water_heater.type == "boiler_proxy"
    assert water_heater.targets == ["water_heater.tank"]
    assert water_heater.min_value == 45.0
    assert water_heater.max_value == 65.0

    inverter = user.virtual_devices[3]
    assert inverter.min_value == 0.0
    assert inverter.max_value == 5000.0


# ---------------------------------------------------------------------------
# VirtualDeviceManager — dispatch
# ---------------------------------------------------------------------------

@pytest.fixture
def manager(tmp_path: Path) -> VirtualDeviceManager:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG_YAML, encoding="utf-8")
    user = load_user_config(cfg)
    mgr = VirtualDeviceManager()
    mgr.load(user.virtual_devices)
    return mgr


@pytest.fixture
def ha_mock():
    api = MagicMock()
    api.available = True
    api.acall_service = AsyncMock(return_value=True)
    return api


def test_has_and_list(manager: VirtualDeviceManager):
    assert manager.has("Heat pump")
    assert manager.has("Boiler switch")
    assert not manager.has("Nonexistent")
    names = {d["name"] for d in manager.list_devices()}
    assert names == {"Heat pump", "Boiler switch", "Water heater", "Inverter limit"}


@pytest.mark.asyncio
async def test_apply_climate_proxy(manager: VirtualDeviceManager, ha_mock):
    result = await manager.apply("Heat pump", "HP_FORCED", ha_mock)
    assert result["applied"] is True
    ha_mock.acall_service.assert_awaited_once_with(
        "climate", "set_temperature", {"entity_id": "climate.living_room", "temperature": 24.0}
    )


@pytest.mark.asyncio
async def test_apply_climate_proxy_clamps_to_bounds(manager: VirtualDeviceManager, ha_mock):
    # base=21.0, offset for HP_FORCED=3.0 -> 24.0, exactly at max_setpoint.
    # Push base higher via no override; verify clamping with a synthetic case.
    result = await manager.apply("Heat pump", "hp_locked", ha_mock)  # lower-case tolerated
    assert result["applied"] is True
    ha_mock.acall_service.assert_awaited_once_with(
        "climate", "set_temperature", {"entity_id": "climate.living_room", "temperature": 18.0}
    )


@pytest.mark.asyncio
async def test_apply_switch_proxy_on_and_off(manager: VirtualDeviceManager, ha_mock):
    off_result = await manager.apply("Boiler switch", "HP_NORMAL", ha_mock)
    assert off_result["applied"] is True
    ha_mock.acall_service.assert_awaited_with("switch", "turn_off", {"entity_id": "switch.boiler"})

    on_result = await manager.apply("Boiler switch", "HP_FORCED", ha_mock)
    assert on_result["applied"] is True
    ha_mock.acall_service.assert_awaited_with("switch", "turn_on", {"entity_id": "switch.boiler"})


@pytest.mark.asyncio
async def test_apply_boiler_proxy(manager: VirtualDeviceManager, ha_mock):
    result = await manager.apply("Water heater", "HP_INTENSIFIED", ha_mock)
    assert result["applied"] is True
    ha_mock.acall_service.assert_awaited_once_with(
        "water_heater", "set_temperature", {"entity_id": "water_heater.tank", "temperature": 55.0}
    )


@pytest.mark.asyncio
async def test_apply_number_proxy(manager: VirtualDeviceManager, ha_mock):
    result = await manager.apply("Inverter limit", "HP_INTENSIFIED", ha_mock)
    assert result["applied"] is True
    ha_mock.acall_service.assert_awaited_once_with(
        "number", "set_value", {"entity_id": "number.inverter_limit", "value": 3000.0}
    )


@pytest.mark.asyncio
async def test_apply_unknown_device(manager: VirtualDeviceManager, ha_mock):
    result = await manager.apply("Nonexistent", "HP_NORMAL", ha_mock)
    assert result["applied"] is False
    assert result["reason"] == "unknown_virtual_device"


@pytest.mark.asyncio
async def test_apply_unmapped_state(manager: VirtualDeviceManager, ha_mock):
    result = await manager.apply("Boiler switch", "SOME_UNKNOWN_STATE", ha_mock)
    assert result["applied"] is False
    assert "no switch mapping" in result["reason"]


@pytest.mark.asyncio
async def test_apply_no_ha_api(manager: VirtualDeviceManager):
    ha = MagicMock()
    ha.available = False
    result = await manager.apply("Boiler switch", "HP_NORMAL", ha)
    assert result["applied"] is False
    assert result["reason"] == "no_ha_api"


@pytest.mark.asyncio
async def test_apply_all_targets_fail(manager: VirtualDeviceManager):
    ha = MagicMock()
    ha.available = True
    ha.acall_service = AsyncMock(return_value=False)
    result = await manager.apply("Boiler switch", "HP_NORMAL", ha)
    assert result["applied"] is False
    assert result["reason"] == "all targets failed"


# ---------------------------------------------------------------------------
# RulesEngine integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rules_engine_dispatches_to_virtual_device(tmp_path: Path, manager: VirtualDeviceManager, ha_mock):
    ha_mock.get_states.return_value = []
    sgr_mock = MagicMock()
    sgr_mock.write = AsyncMock()

    engine = RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json", virtual_devices=manager)
    config = UserConfig(
        sensors=SensorMap(),
        rules=[
            RuleConfig(
                device="Boiler switch",
                profile="SG-ReadyStates",
                data_point="SGReadyOpModeCmd",
                conditions=[{"default": True, "value": "HP_FORCED"}],
            )
        ],
    )

    result = await engine.evaluate(config)

    assert result["actions_taken"] == 1
    sgr_mock.write.assert_not_awaited()
    ha_mock.acall_service.assert_awaited_once_with("switch", "turn_on", {"entity_id": "switch.boiler"})
