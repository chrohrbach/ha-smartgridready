"""Tests for the rules engine.

Covers the DSL evaluator, condition selection (first-match-wins),
hysteresis, redundancy skip, V2H safety, and audit log persistence.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config_loader import RuleConfig, SensorMap, UserConfig, VehicleConfig
from src.rules_engine import (
    DEFAULT_GRID_CO2_KG_PER_KWH,
    MAX_AUDIT_ENTRIES,
    RulesEngine,
    resolve_grid_co2,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sgr_mock():
    svc = MagicMock()
    svc.write = AsyncMock()
    return svc


@pytest.fixture
def ha_mock():
    api = MagicMock()
    api.get_states.return_value = [
        {"entity_id": "sensor.spot_price", "state": "0.12"},
        {"entity_id": "sensor.pv", "state": "4500"},
        {"entity_id": "sensor.house", "state": "1200"},
        {"entity_id": "sensor.soc", "state": "75"},
    ]
    return api


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.json"


@pytest.fixture
def sensors() -> SensorMap:
    return SensorMap(
        spot_price="sensor.spot_price",
        pv_power="sensor.pv",
        house_consumption="sensor.house",
        battery_soc="sensor.soc",
    )


@pytest.fixture
def engine(sgr_mock, ha_mock, audit_path) -> RulesEngine:
    return RulesEngine(sgr_mock, ha_mock, audit_path)


# ---------------------------------------------------------------------------
# DSL — comparisons
# ---------------------------------------------------------------------------

def test_eval_simple_less_than(engine):
    assert engine._eval_expression("spot_price < 0.20", {"spot_price": 0.12}) is True
    assert engine._eval_expression("spot_price < 0.10", {"spot_price": 0.12}) is False


def test_eval_simple_greater_than(engine):
    assert engine._eval_expression("surplus_pv > 3000", {"surplus_pv": 4500}) is True
    assert engine._eval_expression("surplus_pv > 5000", {"surplus_pv": 4500}) is False


def test_eval_equality(engine):
    assert engine._eval_expression("battery_soc == 75", {"battery_soc": 75}) is True
    assert engine._eval_expression("battery_soc == 76", {"battery_soc": 75}) is False


def test_eval_bare_boolean(engine):
    assert engine._eval_expression("is_peak", {"is_peak": True}) is True
    assert engine._eval_expression("is_peak", {"is_peak": False}) is False
    assert engine._eval_expression("unknown_key", {}) is False


def test_eval_and(engine):
    ctx = {"surplus_pv": 4500, "spot_price": 0.08}
    assert engine._eval_expression("surplus_pv > 3000 AND spot_price < 0.10", ctx) is True
    assert engine._eval_expression("surplus_pv > 3000 AND spot_price < 0.05", ctx) is False


def test_eval_or(engine):
    ctx = {"is_offpeak": False, "is_weekend": True}
    assert engine._eval_expression("is_offpeak OR is_weekend", ctx) is True
    assert engine._eval_expression("is_offpeak OR is_peak", ctx) is False


def test_eval_not(engine):
    assert engine._eval_expression("NOT is_peak", {"is_peak": False}) is True
    assert engine._eval_expression("NOT is_peak", {"is_peak": True}) is False


def test_eval_time_range(engine):
    assert engine._eval_expression("10h < hour < 16h", {"hour": 14}) is True
    assert engine._eval_expression("10h < hour < 16h", {"hour": 9}) is False


def test_eval_empty(engine):
    assert engine._eval_expression("", {}) is False
    assert engine._eval_expression("   ", {}) is False


# ---------------------------------------------------------------------------
# Condition selection
# ---------------------------------------------------------------------------

def test_conditions_first_match_wins(engine):
    conditions = [
        {"when": "spot_price < 0.05", "value": 4},
        {"when": "spot_price < 0.15", "value": 3},
        {"when": "spot_price > 0.25", "value": 2},
        {"default": True, "value": 1},
    ]
    assert engine._evaluate_conditions(conditions, {"spot_price": 0.12}) == 3
    assert engine._evaluate_conditions(conditions, {"spot_price": 0.04}) == 4
    assert engine._evaluate_conditions(conditions, {"spot_price": 0.30}) == 2
    assert engine._evaluate_conditions(conditions, {"spot_price": 0.20}) == 1


def test_conditions_returns_none_when_no_match(engine):
    conditions = [
        {"when": "spot_price < 0.05", "value": 4},
        {"when": "spot_price > 0.99", "value": 2},
    ]
    assert engine._evaluate_conditions(conditions, {"spot_price": 0.12}) is None


def test_get_matched_condition(engine):
    conditions = [
        {"when": "spot_price < 0.10", "value": 4},
        {"when": "spot_price < 0.20", "value": 3},
    ]
    assert engine._get_matched_condition(conditions, {"spot_price": 0.15}) == "spot_price < 0.20"
    assert engine._get_matched_condition(conditions, {"spot_price": 0.05}) == "spot_price < 0.10"


# ---------------------------------------------------------------------------
# evaluate() — end to end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evaluate_writes_value(engine, sgr_mock, sensors):
    config = UserConfig(
        sensors=sensors,
        rules=[RuleConfig(
            device="Heat Pump",
            profile="SG-ReadyStates",
            data_point="SGReadyState",
            min_interval=0,
            conditions=[
                {"when": "spot_price < 0.20", "value": 3},
                {"default": True, "value": 1},
            ],
        )],
    )
    result = await engine.evaluate(config)
    sgr_mock.write.assert_awaited_once_with("Heat Pump", "SG-ReadyStates", "SGReadyState", 3)
    assert result["actions_taken"] == 1


@pytest.mark.asyncio
async def test_evaluate_skips_when_disabled(engine, sgr_mock, sensors, ha_mock):
    ha_mock.get_states.return_value = ha_mock.get_states.return_value + [
        {"entity_id": "input_boolean.kill", "state": "off"},
    ]
    config = UserConfig(
        sensors=sensors,
        enable_toggle="input_boolean.kill",
        rules=[RuleConfig("X", "Y", "Z", 0, [{"default": True, "value": 1}])],
    )
    result = await engine.evaluate(config)
    sgr_mock.write.assert_not_awaited()
    assert result.get("skipped") == "disabled"


@pytest.mark.asyncio
async def test_evaluate_skips_redundant_value(engine, sgr_mock, sensors):
    config = UserConfig(
        sensors=sensors,
        rules=[RuleConfig("X", "Y", "Z", 0, [{"default": True, "value": 7}])],
    )
    await engine.evaluate(config)
    sgr_mock.write.reset_mock()
    result = await engine.evaluate(config)
    sgr_mock.write.assert_not_awaited()
    assert any(s["reason"] == "already_set" for s in result["skipped"])


@pytest.mark.asyncio
async def test_evaluate_respects_hysteresis(engine, sgr_mock, sensors):
    rule_id = "X/Y/Z"
    engine._last_values[rule_id] = 1
    engine._last_change_times[rule_id] = datetime.now() - timedelta(minutes=2)
    config = UserConfig(
        sensors=sensors,
        rules=[RuleConfig("X", "Y", "Z", 15, [{"default": True, "value": 2}])],
    )
    result = await engine.evaluate(config)
    sgr_mock.write.assert_not_awaited()
    assert any(s["reason"] == "hysteresis" for s in result["skipped"])


# ---------------------------------------------------------------------------
# V2H safety
# ---------------------------------------------------------------------------

def test_v2h_blocked_when_disabled(engine):
    vehicles = [VehicleConfig(
        name="ev",
        charger_device="Wallbox",
        v2h={"enabled": False},
    )]
    res = engine._check_v2h_authorization("Wallbox", -10, vehicles, {"v2h_available": False})
    assert not res["allowed"]
    assert "v2h_disabled_in_config" in res["reason"]


def test_v2h_clamps_to_max_discharge_a(engine):
    vehicles = [VehicleConfig(
        name="ev",
        charger_device="Wallbox",
        v2h={"enabled": True, "max_discharge_a": 16},
    )]
    res = engine._check_v2h_authorization(
        "Wallbox", -32, vehicles, {"v2h_available": True}
    )
    assert res["allowed"]
    assert res["clamped_value"] == -16


def test_v2h_daily_limit(engine):
    vehicles = [VehicleConfig(
        name="ev",
        charger_device="Wallbox",
        v2h={"enabled": True, "max_discharge_a": 16, "max_cycles_per_day": 1},
    )]
    today = datetime.now().strftime("%Y-%m-%d")
    engine._v2h_day = today
    engine._v2h_count_today["ev"] = 1
    res = engine._check_v2h_authorization(
        "Wallbox", -10, vehicles, {"v2h_available": True}
    )
    assert not res["allowed"]
    assert "daily_cycle_limit_reached" in res["reason"]


def test_v2h_grid_agreement_required(engine):
    vehicles = [VehicleConfig(
        name="ev",
        charger_device="Wallbox",
        v2h={
            "enabled": True,
            "max_discharge_a": 16,
            "requires_grd_agreement": True,
            "grd_agreement_signed": False,
        },
    )]
    res = engine._check_v2h_authorization(
        "Wallbox", -10, vehicles, {"v2h_available": True}
    )
    assert not res["allowed"]
    assert "grd_agreement_missing" in res["reason"]


# ---------------------------------------------------------------------------
# Audit persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_persists(engine, audit_path, sensors):
    config = UserConfig(
        sensors=sensors,
        rules=[RuleConfig("X", "Y", "Z", 0, [{"default": True, "value": 1}])],
    )
    await engine.evaluate(config)
    assert audit_path.exists()
    data = json.loads(audit_path.read_text())
    assert isinstance(data, list)
    assert len(data) == 1


@pytest.mark.asyncio
async def test_audit_trims_to_max(engine, audit_path, sensors):
    # Pre-fill with synthetic entries beyond the cap.
    entries = [{"timestamp": f"t{i}"} for i in range(MAX_AUDIT_ENTRIES + 50)]
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(entries), encoding="utf-8")

    config = UserConfig(
        sensors=sensors,
        rules=[RuleConfig("X", "Y", "Z", 0, [{"default": True, "value": 1}])],
    )
    await engine.evaluate(config)
    data = json.loads(audit_path.read_text())
    assert len(data) <= MAX_AUDIT_ENTRIES


# ---------------------------------------------------------------------------
# Grid CO2 resolution
# ---------------------------------------------------------------------------

def test_resolve_grid_co2_default():
    assert resolve_grid_co2({}) == DEFAULT_GRID_CO2_KG_PER_KWH


def test_resolve_grid_co2_from_intensity_sensor():
    state_map = {
        "sensor.electricity_maps_co2_intensity": {"state": "180"},
    }
    assert resolve_grid_co2(state_map) == 0.18


def test_resolve_grid_co2_from_fossil_pct():
    state_map = {
        "sensor.electricity_maps_grid_fossil_fuel_percentage": {"state": "20"},
    }
    # 0.05 + 0.005 * 20 = 0.15
    assert resolve_grid_co2(state_map) == 0.15


def test_resolve_grid_co2_ignores_unavailable():
    state_map = {
        "sensor.electricity_maps_co2_intensity": {"state": "unavailable"},
        "sensor.electricity_maps_grid_fossil_fuel_percentage": {"state": "20"},
    }
    assert resolve_grid_co2(state_map) == 0.15
