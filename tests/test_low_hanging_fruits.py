"""Tests for the L1–L9 enhancements.

Each class corresponds to one item from docs/scope-and-gaps.md:

  L1 — SG-Ready HP_LOCKED cap (≤ 2 h / 24 h, BWP 1.1)
  L2 — V2H counter on transitions, not on every negative write
  L3 — DSL warnings on silent failure modes
  L4 — Timezone-aware context (hour/day/window robust to DST)
  L5 — PCC headroom helpers
  L6 — DSO curtailment signal context
  L7 — Battery helpers (full/low/room_kwh/available_kwh)
  L8 — minute_in_quarter context variable
  L9 — Tariff forecast horizon (next 3 h, today's lowest quartile)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config_loader import RuleConfig, SensorMap, UserConfig, VehicleConfig
from src.rules_engine import (
    DEFAULT_SG_READY_LOCK_CAP_MINUTES,
    RulesEngine,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ha_mock():
    api = MagicMock()
    api.get_states.return_value = [
        {"entity_id": "sensor.spot_price", "state": "0.12"},
        {"entity_id": "sensor.pv", "state": "1000"},
        {"entity_id": "sensor.house", "state": "1500"},
        {"entity_id": "sensor.soc", "state": "75"},
    ]
    return api


@pytest.fixture
def sgr_mock():
    svc = MagicMock()
    svc.write = AsyncMock()
    return svc


@pytest.fixture
def sensors() -> SensorMap:
    return SensorMap(
        spot_price="sensor.spot_price",
        pv_power="sensor.pv",
        house_consumption="sensor.house",
        battery_soc="sensor.soc",
    )


@pytest.fixture
def engine(sgr_mock, ha_mock, tmp_path) -> RulesEngine:
    return RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json")


# ---------------------------------------------------------------------------
# L1 — SG-Ready HP_LOCKED cap (≤ 2 h / 24 h)
# ---------------------------------------------------------------------------

class TestSGReadyLockCap:

    @pytest.mark.asyncio
    async def test_lock_passes_through_when_under_cap(
        self, sgr_mock, ha_mock, sensors, tmp_path
    ):
        engine = RulesEngine(
            sgr_mock, ha_mock, tmp_path / "audit.json",
            sg_ready_lock_cap_minutes=120,
        )
        rule = RuleConfig(
            "Heat Pump", "SG-ReadyStates", "SGReadyOpModeCmd", 0,
            [{"default": True, "value": "HP_LOCKED"}],
        )
        await engine.evaluate(UserConfig(sensors=sensors, rules=[rule]))
        sgr_mock.write.assert_awaited_once_with(
            "Heat Pump", "SG-ReadyStates", "SGReadyOpModeCmd", "HP_LOCKED",
        )

    @pytest.mark.asyncio
    async def test_lock_downgraded_to_normal_when_cap_exhausted(
        self, sgr_mock, ha_mock, sensors, tmp_path
    ):
        engine = RulesEngine(
            sgr_mock, ha_mock, tmp_path / "audit.json",
            sg_ready_lock_cap_minutes=120,
        )
        rule_id = "Heat Pump/SG-ReadyStates/SGReadyOpModeCmd"
        # Pre-load a 130-minute lock period in the last 24 h.
        long_ago = (engine._now_utc() - timedelta(minutes=130)).isoformat()
        ten_min_ago = (engine._now_utc() - timedelta(minutes=0)).isoformat()
        engine._sg_lock_ledger[rule_id] = [[long_ago, ten_min_ago]]

        rule = RuleConfig(
            "Heat Pump", "SG-ReadyStates", "SGReadyOpModeCmd", 0,
            [{"default": True, "value": "HP_LOCKED"}],
        )
        await engine.evaluate(UserConfig(sensors=sensors, rules=[rule]))
        sgr_mock.write.assert_awaited_once_with(
            "Heat Pump", "SG-ReadyStates", "SGReadyOpModeCmd", "HP_NORMAL",
        )

    @pytest.mark.asyncio
    async def test_cap_zero_disables_guard(
        self, sgr_mock, ha_mock, sensors, tmp_path
    ):
        engine = RulesEngine(
            sgr_mock, ha_mock, tmp_path / "audit.json",
            sg_ready_lock_cap_minutes=0,
        )
        rule_id = "Heat Pump/SG-ReadyStates/SGReadyOpModeCmd"
        long_ago = (engine._now_utc() - timedelta(hours=23)).isoformat()
        now = engine._now_utc().isoformat()
        engine._sg_lock_ledger[rule_id] = [[long_ago, now]]

        rule = RuleConfig(
            "Heat Pump", "SG-ReadyStates", "SGReadyOpModeCmd", 0,
            [{"default": True, "value": "HP_LOCKED"}],
        )
        await engine.evaluate(UserConfig(sensors=sensors, rules=[rule]))
        sgr_mock.write.assert_awaited_once_with(
            "Heat Pump", "SG-ReadyStates", "SGReadyOpModeCmd", "HP_LOCKED",
        )

    def test_is_lock_rule_detects_profile_variants(self, engine):
        for profile in ("SG-ReadyStates", "SG_Ready_States", "SGReadyStates"):
            rule = RuleConfig("d", profile, "dp", 0, [])
            assert engine._is_sg_ready_lock_rule(rule, "HP_LOCKED") is True

    def test_is_lock_rule_does_not_match_other_profiles(self, engine):
        rule = RuleConfig("d", "EMS_Current_Limit", "dp", 0, [])
        assert engine._is_sg_ready_lock_rule(rule, "HP_LOCKED") is False

    def test_lock_ledger_persists_and_reloads(
        self, sgr_mock, ha_mock, tmp_path
    ):
        engine = RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json")
        rule_id = "PAC/SG-Ready/Cmd"
        ts = engine._now_utc().isoformat()
        engine._sg_lock_ledger[rule_id] = [[ts, None]]
        engine._persist_sg_lock_ledger()

        engine2 = RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json")
        assert engine2._sg_lock_ledger.get(rule_id) == [[ts, None]]


# ---------------------------------------------------------------------------
# L2 — V2H counter increments only on transitions
# ---------------------------------------------------------------------------

class TestV2HCycleTransitions:

    @pytest.mark.asyncio
    async def test_continuous_discharge_counts_once(
        self, sgr_mock, ha_mock, tmp_path
    ):
        ha_mock.get_states.return_value = [
            {"entity_id": "sensor.soc", "state": "80"},
            {"entity_id": "binary_sensor.plug", "state": "on"},
        ]
        engine = RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json")
        vehicles = [VehicleConfig(
            name="ev",
            soc_entity="sensor.soc",
            plugged_entity="binary_sensor.plug",
            charger_device="Wallbox",
            battery_capacity_kwh=70,
            v2h={
                "enabled": True, "min_soc": 50,
                "max_discharge_a": 16, "max_cycles_per_day": 1,
            },
        )]
        # Inject a rule that keeps writing -16 every cycle.
        rule = RuleConfig(
            "Wallbox", "EMS_Current_Limit", "EMSCurrentLimit", 0,
            [{"default": True, "value": -16}],
        )
        sensors = SensorMap(battery_soc="sensor.soc")

        # First cycle: transition 0 → -16. Counter becomes 1.
        await engine.evaluate(UserConfig(
            sensors=sensors, vehicles=vehicles, rules=[rule],
        ))
        assert engine._v2h_count_today.get("ev") == 1

        # Second cycle: redundant write skipped, counter must stay at 1.
        sgr_mock.write.reset_mock()
        await engine.evaluate(UserConfig(
            sensors=sensors, vehicles=vehicles, rules=[rule],
        ))
        assert engine._v2h_count_today.get("ev") == 1

    @pytest.mark.asyncio
    async def test_two_separate_discharge_sessions_count_twice(
        self, sgr_mock, ha_mock, tmp_path
    ):
        ha_mock.get_states.return_value = [
            {"entity_id": "sensor.soc", "state": "80"},
            {"entity_id": "binary_sensor.plug", "state": "on"},
        ]
        engine = RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json")
        vehicles = [VehicleConfig(
            name="ev",
            soc_entity="sensor.soc",
            plugged_entity="binary_sensor.plug",
            charger_device="Wallbox",
            battery_capacity_kwh=70,
            v2h={
                "enabled": True, "min_soc": 50,
                "max_discharge_a": 16, "max_cycles_per_day": 5,
            },
        )]
        sensors = SensorMap(battery_soc="sensor.soc")

        cond_neg = RuleConfig(
            "Wallbox", "EMS_Current_Limit", "EMSCurrentLimit", 0,
            [{"default": True, "value": -16}],
        )
        cond_pos = RuleConfig(
            "Wallbox", "EMS_Current_Limit", "EMSCurrentLimit", 0,
            [{"default": True, "value": 8}],
        )
        rule_id = "Wallbox/EMS_Current_Limit/EMSCurrentLimit"

        # Cycle 1: 0 → −16 → counter becomes 1
        await engine.evaluate(UserConfig(sensors=sensors, vehicles=vehicles, rules=[cond_neg]))
        # Reset the hysteresis timer so the next *different* write isn't blocked.
        engine._last_change_times.pop(rule_id, None)
        # Cycle 2: −16 → +8 (back to positive)
        await engine.evaluate(UserConfig(sensors=sensors, vehicles=vehicles, rules=[cond_pos]))
        engine._last_change_times.pop(rule_id, None)
        # Cycle 3: +8 → −16 → counter must reach 2 (new transition)
        await engine.evaluate(UserConfig(sensors=sensors, vehicles=vehicles, rules=[cond_neg]))
        assert engine._v2h_count_today.get("ev") == 2


# ---------------------------------------------------------------------------
# L3 — DSL warnings
# ---------------------------------------------------------------------------

class TestDSLWarnings:

    def test_bad_rhs_logs_once(self, engine, caplog):
        caplog.set_level("WARNING")
        engine._eval_expression("spot_price < zero", {"spot_price": 0.1})
        engine._eval_expression("spot_price < zero", {"spot_price": 0.1})
        warnings = [r for r in caplog.records if "DSL" in r.message]
        assert len(warnings) == 1
        assert "not numeric" in warnings[0].message

    def test_unknown_variable_warns(self, engine, caplog):
        caplog.set_level("WARNING")
        engine._eval_expression("frobnitz_is_set", {})
        warnings = [r for r in caplog.records if "DSL" in r.message]
        assert any("unknown context variable" in r.message for r in warnings)

    def test_known_variable_does_not_warn(self, engine, caplog):
        caplog.set_level("WARNING")
        engine._eval_expression("has_surplus", {"has_surplus": True})
        warnings = [r for r in caplog.records if "DSL" in r.message]
        assert warnings == []


# ---------------------------------------------------------------------------
# L4 — Timezone awareness
# ---------------------------------------------------------------------------

class TestTimezone:

    def test_resolves_known_zone(self, sgr_mock, ha_mock, tmp_path):
        engine = RulesEngine(
            sgr_mock, ha_mock, tmp_path / "audit.json",
            tz_name="Europe/Zurich",
        )
        assert engine._now_local().tzinfo is not None

    def test_unknown_zone_falls_back_to_utc(self, sgr_mock, ha_mock, tmp_path, caplog):
        caplog.set_level("WARNING")
        engine = RulesEngine(
            sgr_mock, ha_mock, tmp_path / "audit.json",
            tz_name="Mars/Olympus_Mons",
        )
        assert engine._now_local().tzinfo == timezone.utc
        assert any("Unknown timezone" in r.message for r in caplog.records)

    def test_context_uses_local_hour(self, engine):
        ctx = engine._build_context(SensorMap(), [], {})
        local_hour = engine._now_local().hour
        assert ctx["hour"] == local_hour


# ---------------------------------------------------------------------------
# L5 — PCC headroom helpers
# ---------------------------------------------------------------------------

class TestPCCHeadroom:

    def test_disabled_when_no_limit_set(self, engine):
        ctx = engine._build_context(SensorMap(), [], {})
        assert ctx["pcc_limit_w"] == 0
        assert ctx["pcc_headroom_w"] == 0
        assert ctx["pcc_overload"] is False

    def test_headroom_computed_from_import(self, engine):
        sensors = SensorMap(grid_import="sensor.grid_in")
        state_map = {"sensor.grid_in": {"state": "8000"}}
        ctx = engine._build_context(
            sensors, [], state_map, grid_connection_limit_w=25000,
        )
        assert ctx["pcc_limit_w"] == 25000
        assert ctx["pcc_headroom_w"] == 17000
        assert ctx["pcc_overload"] is False

    def test_overload_flag(self, engine):
        sensors = SensorMap(grid_import="sensor.grid_in")
        state_map = {"sensor.grid_in": {"state": "30000"}}
        ctx = engine._build_context(
            sensors, [], state_map, grid_connection_limit_w=25000,
        )
        assert ctx["pcc_overload"] is True
        assert ctx["pcc_headroom_w"] == 0

    def test_pcc_power_signed(self, engine):
        sensors = SensorMap(
            grid_import="sensor.grid_in", grid_export="sensor.grid_out",
        )
        state_map = {
            "sensor.grid_in": {"state": "0"},
            "sensor.grid_out": {"state": "4000"},
        }
        ctx = engine._build_context(sensors, [], state_map)
        assert ctx["pcc_power_w"] == -4000


# ---------------------------------------------------------------------------
# L6 — DSO curtailment signal
# ---------------------------------------------------------------------------

class TestDSOSignal:

    def test_autodetects_canonical_entity(self, engine):
        state_map = {
            "binary_sensor.dso_curtailment_active": {"state": "on"},
            "sensor.dso_curtailment_factor": {"state": "60"},
        }
        ctx = engine._build_context(SensorMap(), [], state_map)
        assert ctx["dso_curtailment_active"] is True
        assert ctx["dso_curtailment_factor"] == 60.0

    def test_explicit_sensor_override(self, engine):
        sensors = SensorMap(
            dso_curtailment_active="binary_sensor.my_dso",
            dso_curtailment_factor="sensor.my_factor",
        )
        state_map = {
            "binary_sensor.my_dso": {"state": "on"},
            "sensor.my_factor": {"state": "75"},
        }
        ctx = engine._build_context(sensors, [], state_map)
        assert ctx["dso_curtailment_active"] is True
        assert ctx["dso_curtailment_factor"] == 75.0

    def test_default_off_when_no_signal(self, engine):
        ctx = engine._build_context(SensorMap(), [], {})
        assert ctx["dso_curtailment_active"] is False
        assert ctx["dso_curtailment_factor"] == 0.0


# ---------------------------------------------------------------------------
# L7 — Battery helpers
# ---------------------------------------------------------------------------

class TestBatteryHelpers:

    def test_full_and_low_thresholds(self, engine):
        sensors = SensorMap(battery_soc="sensor.soc")
        ctx_full = engine._build_context(
            sensors, [], {"sensor.soc": {"state": "98"}},
        )
        assert ctx_full["battery_full"] is True
        assert ctx_full["battery_low"] is False

        ctx_low = engine._build_context(
            sensors, [], {"sensor.soc": {"state": "12"}},
        )
        assert ctx_low["battery_full"] is False
        assert ctx_low["battery_low"] is True

    def test_room_and_available_when_capacity_set(self, engine):
        sensors = SensorMap(battery_soc="sensor.soc")
        ctx = engine._build_context(
            sensors, [], {"sensor.soc": {"state": "60"}},
            battery_capacity_kwh=10,
        )
        assert ctx["battery_available_kwh"] == 6.0
        assert ctx["battery_room_kwh"] == 4.0

    def test_room_zero_when_no_capacity(self, engine):
        sensors = SensorMap(battery_soc="sensor.soc")
        ctx = engine._build_context(
            sensors, [], {"sensor.soc": {"state": "60"}},
        )
        assert ctx["battery_room_kwh"] == 0


# ---------------------------------------------------------------------------
# L8 — minute_in_quarter
# ---------------------------------------------------------------------------

class TestQuarterAlignment:

    def test_minute_in_quarter_in_context(self, engine):
        ctx = engine._build_context(SensorMap(), [], {})
        assert 0 <= ctx["minute_in_quarter"] < 15
        # And it equals minute % 15
        assert ctx["minute_in_quarter"] == ctx["minute"] % 15


# ---------------------------------------------------------------------------
# L9 — Tariff forecast horizon
# ---------------------------------------------------------------------------

class TestTariffHorizon:

    def test_no_forecast_yields_zero(self, engine):
        sensors = SensorMap(spot_price="sensor.price")
        ctx = engine._build_context(
            sensors, [],
            {"sensor.price": {"state": "0.10", "attributes": {}}},
        )
        assert ctx["tariff_next_3h_avg"] == 0.0
        assert ctx["tariff_in_lowest_quartile_today"] is False

    def test_next_3h_from_forecast_list(self, engine):
        now = engine._now_utc()
        forecast = [
            {"start": (now + timedelta(minutes=30)).isoformat(), "value": 0.05},
            {"start": (now + timedelta(hours=1)).isoformat(), "value": 0.07},
            {"start": (now + timedelta(hours=2)).isoformat(), "value": 0.09},
            {"start": (now + timedelta(hours=5)).isoformat(), "value": 0.30},  # outside 3h
        ]
        sensors = SensorMap(spot_price="sensor.price")
        ctx = engine._build_context(sensors, [], {
            "sensor.price": {
                "state": "0.06",
                "attributes": {"forecast": forecast},
            },
        })
        assert ctx["tariff_next_3h_min"] == 0.05
        assert ctx["tariff_next_3h_max"] == 0.09
        assert ctx["tariff_next_3h_avg"] == round((0.05 + 0.07 + 0.09) / 3, 4)

    def test_nordpool_raw_today_attribute(self, engine):
        now = engine._now_utc()
        prices = [
            {"start": (now + timedelta(minutes=15)).isoformat(), "value": 0.02},
        ]
        sensors = SensorMap(spot_price="sensor.price")
        ctx = engine._build_context(sensors, [], {
            "sensor.price": {
                "state": "0.02",
                "attributes": {"raw_today": prices},
            },
        })
        assert ctx["tariff_next_3h_min"] == 0.02


# ---------------------------------------------------------------------------
# Hysteresis still works after TZ refactor
# ---------------------------------------------------------------------------

class TestHysteresisStillWorks:

    @pytest.mark.asyncio
    async def test_aware_utc_timestamps_round_trip(
        self, sgr_mock, ha_mock, sensors, tmp_path
    ):
        engine = RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json")
        rule = RuleConfig("X", "Y", "Z", 15, [{"default": True, "value": 1}])
        await engine.evaluate(UserConfig(sensors=sensors, rules=[rule]))

        engine2 = RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json")
        # Restored timestamp must be aware UTC so the comparison succeeds.
        ts = engine2._last_change_times.get("X/Y/Z")
        assert ts is not None
        assert ts.tzinfo is not None
