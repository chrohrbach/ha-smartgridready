"""Tests for the predictive-dispatch optimizer (MILP + greedy fallback).

Covers config parsing (``optimizer:``), the solver itself (both the
MILP path — scipy is available in this dev/CI environment — and the
greedy fallback), persistence/caching, and the ``RulesEngine``
integration (context injection + ``run_optimizer``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config_loader import (
    OptimizerBatteryConfig,
    OptimizerDeviceConfig,
    OptimizerGridConfig,
    SensorMap,
    UserConfig,
    load_user_config,
)
from src.optimizer import SGrOptimizer
from src.rules_engine import RulesEngine

# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

CONFIG_YAML = """
grid_connection_limit_w: 20000
battery_capacity_kwh: 8

optimizer:
  enabled: true
  devices:
    - name: "Wallbox"
      min_power_w: 0
      max_power_w: 11000
      must_run_hours: 2
      preferred_window: [22, 6]
      priority: 60
      switchable: false
      voltage: 230
      phases: 1
    - name: "Boiler"
      max_power_w: 2000
      switchable: true
  battery:
    max_charge_w: 5000
    max_discharge_w: 5000
    efficiency: 0.92
  grid:
    pcc_export_w: 8000
    export_price_chf_kwh: 0.06
"""


def test_load_optimizer_config(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG_YAML, encoding="utf-8")
    user = load_user_config(cfg)

    assert user.optimizer.enabled is True
    assert len(user.optimizer.devices) == 2
    wallbox = user.optimizer.devices[0]
    assert wallbox.name == "Wallbox"
    assert wallbox.max_power_w == 11000
    assert wallbox.must_run_hours == 2
    assert wallbox.preferred_window == [22, 6]
    assert wallbox.priority == 60
    assert wallbox.switchable is False

    boiler = user.optimizer.devices[1]
    assert boiler.switchable is True

    # Battery capacity defaults from the top-level battery_capacity_kwh
    # when not overridden inside optimizer.battery.
    assert user.optimizer.battery.capacity_kwh == 8.0
    assert user.optimizer.battery.max_charge_w == 5000
    assert user.optimizer.battery.efficiency == 0.92

    # Grid PCC import defaults from the top-level grid_connection_limit_w.
    assert user.optimizer.grid.pcc_import_w == 20000.0
    assert user.optimizer.grid.pcc_export_w == 8000.0
    assert user.optimizer.grid.export_price_chf_kwh == 0.06


def test_optimizer_disabled_by_default(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("devices: []\n", encoding="utf-8")
    user = load_user_config(cfg)
    assert user.optimizer.enabled is False
    assert user.optimizer.devices == []


# ---------------------------------------------------------------------------
# SGrOptimizer — solver
# ---------------------------------------------------------------------------

def _flat_prices(cheap_hours: set[int], cheap=0.05, expensive=0.30) -> list[float]:
    return [cheap if h in cheap_hours else expensive for h in range(24)]


def test_greedy_runs_must_run_device_in_cheapest_hours(tmp_path: Path):
    opt = SGrOptimizer(tmp_path)
    opt.load(
        [OptimizerDeviceConfig(name="Heater", min_power_w=0, max_power_w=2000, must_run_hours=3)],
        OptimizerBatteryConfig(),
        OptimizerGridConfig(),
    )
    prices = _flat_prices(cheap_hours={2, 3, 4})
    pv = [0.0] * 24
    result = opt._solve_greedy(prices, pv)
    sched = result.schedule["Heater"]
    on_hours = {h for h, w in enumerate(sched) if w > 0}
    assert on_hours == {2, 3, 4}


def test_milp_respects_pcc_import_limit(tmp_path: Path):
    opt = SGrOptimizer(tmp_path)
    opt.load(
        [OptimizerDeviceConfig(name="Wallbox", min_power_w=0, max_power_w=11000, must_run_hours=4)],
        OptimizerBatteryConfig(),
        OptimizerGridConfig(pcc_import_w=5000, pcc_export_w=5000),
    )
    prices = [0.10] * 24
    pv = [0.0] * 24
    result = opt.optimize(prices, pv, battery_soc_pct=0.0)
    assert result.solver == "scipy_milp"
    assert all(w <= 5000.0 + 1e-6 for w in result.grid_import_w)
    assert sum(1 for w in result.schedule["Wallbox"] if w > 0) >= 4


def test_milp_uses_battery_to_shift_load(tmp_path: Path):
    opt = SGrOptimizer(tmp_path)
    opt.load(
        [],
        OptimizerBatteryConfig(
            capacity_kwh=10, max_charge_w=3000, max_discharge_w=3000,
            soc_min_pct=10, soc_max_pct=95, efficiency=0.95,
        ),
        OptimizerGridConfig(pcc_import_w=25000, pcc_export_w=12000),
    )
    # Cheap early, expensive later — battery should charge cheap, discharge expensive.
    prices = [0.05] * 6 + [0.35] * 18
    pv = [0.0] * 24
    base_load = [500.0] * 24
    result = opt.optimize(prices, pv, battery_soc_pct=50.0, base_load_forecast_w=base_load)
    assert result.solver == "scipy_milp"
    assert sum(result.battery_charge_w[:6]) > 0
    assert sum(result.battery_discharge_w[6:]) > 0


def test_optimize_falls_back_to_greedy_without_scipy(tmp_path: Path, monkeypatch):
    opt = SGrOptimizer(tmp_path)
    opt.load(
        [OptimizerDeviceConfig(name="Heater", max_power_w=1000, must_run_hours=1)],
        OptimizerBatteryConfig(),
        OptimizerGridConfig(),
    )
    monkeypatch.setattr(opt, "_check_milp", lambda: False)
    result = opt.optimize([0.1] * 24, [0.0] * 24)
    assert result.solver == "greedy"


def test_optimize_disabled_without_devices(tmp_path: Path):
    opt = SGrOptimizer(tmp_path)
    assert opt.enabled is False
    result = opt.optimize([0.1] * 24, [0.0] * 24)
    # No devices -> greedy path with an empty schedule, still returns a result.
    assert result.schedule == {}


def test_persist_and_get_last_result(tmp_path: Path):
    opt = SGrOptimizer(tmp_path)
    opt.load([OptimizerDeviceConfig(name="Heater", max_power_w=1000, must_run_hours=1)],
              OptimizerBatteryConfig(), OptimizerGridConfig())
    result = opt.optimize([0.1] * 24, [0.0] * 24)
    assert (tmp_path / "optimizer_schedule.json").exists()

    # Fresh instance reading from disk only.
    opt2 = SGrOptimizer(tmp_path)
    loaded = opt2.get_last_result()
    assert loaded is not None
    assert loaded.schedule.keys() == result.schedule.keys()


def test_hourly_price_series_from_forecast():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
    forecast = [(now + timedelta(hours=h), 0.1 + h * 0.01) for h in range(24)]
    prices = SGrOptimizer.hourly_price_series(forecast)
    assert len(prices) == 24
    assert prices[0] == pytest.approx(0.1)
    assert prices[5] == pytest.approx(0.15)


def test_hourly_price_series_empty_forecast_uses_fallback():
    prices = SGrOptimizer.hourly_price_series([], fallback=0.22)
    assert prices == [0.22] * 24


# ---------------------------------------------------------------------------
# RulesEngine integration
# ---------------------------------------------------------------------------

@pytest.fixture
def ha_mock():
    api = MagicMock()
    api.available = True
    api.get_states.return_value = [
        {"entity_id": "sensor.spot_price", "state": "0.12"},
        {"entity_id": "sensor.soc", "state": "60"},
    ]
    api.acall_service = AsyncMock(return_value=True)
    return api


def test_build_optimizer_context_disabled(tmp_path: Path, ha_mock):
    sgr_mock = MagicMock()
    engine = RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json")
    ctx: dict = {}
    engine._build_optimizer_context(ctx, [])
    assert ctx["optimizer_enabled"] is False
    assert ctx["optimizer_savings_chf"] == 0.0


def test_build_optimizer_context_injects_per_device_keys(tmp_path: Path, ha_mock):
    sgr_mock = MagicMock()
    opt = SGrOptimizer(tmp_path)
    devices = [OptimizerDeviceConfig(name="Wallbox", max_power_w=3000, must_run_hours=24, voltage=230, phases=1)]
    opt.load(devices, OptimizerBatteryConfig(), OptimizerGridConfig())
    opt.optimize([0.1] * 24, [0.0] * 24)

    engine = RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json", optimizer=opt)
    ctx: dict = {}
    engine._build_optimizer_context(ctx, devices)

    assert ctx["optimizer_enabled"] is True
    assert ctx["optimizer_wallbox_power_w"] == pytest.approx(3000.0)
    assert ctx["optimizer_wallbox_current_a"] == pytest.approx(3000.0 / 230.0, rel=1e-2)
    assert ctx["optimizer_wallbox_on"] is True


@pytest.mark.asyncio
async def test_run_optimizer_computes_and_persists(tmp_path: Path, ha_mock):
    sgr_mock = MagicMock()
    opt = SGrOptimizer(tmp_path)
    devices = [OptimizerDeviceConfig(name="Wallbox", max_power_w=3000, must_run_hours=1)]
    opt.load(devices, OptimizerBatteryConfig(), OptimizerGridConfig())

    engine = RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json", optimizer=opt)
    config = UserConfig(sensors=SensorMap(spot_price="sensor.spot_price", battery_soc="sensor.soc"))

    result = await engine.run_optimizer(config)
    assert result is not None
    assert (tmp_path / "optimizer_schedule.json").exists()


@pytest.mark.asyncio
async def test_run_optimizer_noop_when_disabled(tmp_path: Path, ha_mock):
    sgr_mock = MagicMock()
    opt = SGrOptimizer(tmp_path)  # no devices loaded -> disabled
    engine = RulesEngine(sgr_mock, ha_mock, tmp_path / "audit.json", optimizer=opt)
    config = UserConfig(sensors=SensorMap())
    result = await engine.run_optimizer(config)
    assert result is None
