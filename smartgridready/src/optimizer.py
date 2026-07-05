"""Predictive dispatch optimizer (MILP, opt-in).

Replaces the instant-condition ``rules:`` evaluation with a
cost-minimizing schedule computed over a 24h horizon for the devices
declared under ``optimizer:``. This is a proper MILP (mixed-integer
linear program) home-energy-management formulation, not a
cost-ranking heuristic. It jointly optimises, hour by hour:

    * controllable loads (continuous power, or on/off via binary unit
      commitment),
    * a battery (charge / discharge / state-of-charge dynamics with
      round-trip efficiency),
    * grid import / export bounded by the point-of-common-coupling
      (PCC) limits,

subject to an hourly power-balance constraint that ties PV production,
base load, devices, battery and grid together.

Solver chain (``scipy`` stays an optional dependency — deliberately
**not** pinned in ``requirements.txt`` so every architecture this
add-on ships for, including ``armv7``, keeps building without relying
on a prebuilt scipy/numpy wheel being available):

    scipy.optimize.milp  →  greedy heuristic fallback

The computed schedule is not applied directly — it is exposed as new
rules-DSL context variables (``optimizer_<slug>_power_w`` /
``_current_a`` / ``_on``) so the existing ``rules:`` engine (hysteresis,
audit log, real vs. virtual device dispatch — all already built) stays
the single write path. See ``docs/configuration.md#optimizer``.

Math ported from the equivalent internal module in casasmooth (a
sibling project by the same author); adapted here to read forecasts
from this add-on's own sensors/PV-forecast service instead of local
casasmooth caches.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config_loader import OptimizerBatteryConfig, OptimizerDeviceConfig, OptimizerGridConfig

logger = logging.getLogger("smartgridready.optimizer")

HOURS = 24
DEFAULT_FALLBACK_PRICE = 0.15  # CHF/kWh when no forecast is available
DEFAULT_BATTERY_CYCLE_COST = 0.01  # CHF/kWh throughput


class OptimizationResult:
    """Hourly dispatch schedule for all devices, plus battery/grid plan and KPIs."""

    def __init__(self) -> None:
        self.schedule: Dict[str, List[float]] = {}  # device name -> [power_w per hour]
        self.total_cost_chf: float = 0.0
        self.baseline_cost_chf: float = 0.0
        self.savings_chf: float = 0.0
        self.solver: str = "none"
        self.computed_at: str = ""
        self.horizon_hours: int = HOURS
        self.battery_charge_w: List[float] = []
        self.battery_discharge_w: List[float] = []
        self.battery_soc_kwh: List[float] = []
        self.grid_import_w: List[float] = []
        self.grid_export_w: List[float] = []
        self.self_consumption_pct: float = 0.0
        self.battery_cycles: float = 0.0
        self.grid_import_kwh: float = 0.0
        self.grid_export_kwh: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schedule": self.schedule,
            "total_cost_chf": round(self.total_cost_chf, 4),
            "baseline_cost_chf": round(self.baseline_cost_chf, 4),
            "savings_chf": round(self.savings_chf, 4),
            "solver": self.solver,
            "computed_at": self.computed_at,
            "horizon_hours": self.horizon_hours,
            "battery": {
                "charge_w": [round(x, 0) for x in self.battery_charge_w],
                "discharge_w": [round(x, 0) for x in self.battery_discharge_w],
                "soc_kwh": [round(x, 3) for x in self.battery_soc_kwh],
                "cycles": round(self.battery_cycles, 2),
            },
            "grid": {
                "import_w": [round(x, 0) for x in self.grid_import_w],
                "export_w": [round(x, 0) for x in self.grid_export_w],
                "import_kwh": round(self.grid_import_kwh, 3),
                "export_kwh": round(self.grid_export_kwh, 3),
            },
            "self_consumption_pct": round(self.self_consumption_pct, 1),
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "OptimizationResult":
        r = OptimizationResult()
        r.schedule = data.get("schedule", {}) or {}
        r.total_cost_chf = float(data.get("total_cost_chf", 0) or 0)
        r.baseline_cost_chf = float(data.get("baseline_cost_chf", 0) or 0)
        r.savings_chf = float(data.get("savings_chf", 0) or 0)
        r.solver = data.get("solver", "unknown")
        r.computed_at = data.get("computed_at", "")
        bat = data.get("battery", {}) or {}
        r.battery_charge_w = bat.get("charge_w", []) or []
        r.battery_discharge_w = bat.get("discharge_w", []) or []
        r.battery_soc_kwh = bat.get("soc_kwh", []) or []
        r.battery_cycles = float(bat.get("cycles", 0) or 0)
        grid = data.get("grid", {}) or {}
        r.grid_import_w = grid.get("import_w", []) or []
        r.grid_export_w = grid.get("export_w", []) or []
        r.grid_import_kwh = float(grid.get("import_kwh", 0) or 0)
        r.grid_export_kwh = float(grid.get("export_kwh", 0) or 0)
        r.self_consumption_pct = float(data.get("self_consumption_pct", 0) or 0)
        return r

    def current_hour_power(self, device_name: str, at: Optional[datetime] = None) -> Optional[float]:
        """Return the scheduled power for ``device_name`` at the current hour."""
        sched = self.schedule.get(device_name)
        if not sched:
            return None
        hour = (at or datetime.now()).hour
        if hour < len(sched):
            return sched[hour]
        return None


class SGrOptimizer:
    """Predictive dispatch optimizer using a MILP (or greedy fallback)."""

    def __init__(self, cache_path: Path) -> None:
        self.cache_path = Path(cache_path)
        self._cache_file = self.cache_path / "optimizer_schedule.json"
        self._devices: List[OptimizerDeviceConfig] = []
        self._battery: OptimizerBatteryConfig = OptimizerBatteryConfig()
        self._grid: OptimizerGridConfig = OptimizerGridConfig()
        self._last_result: Optional[OptimizationResult] = None
        self._milp_available: Optional[bool] = None

    def load(
        self,
        devices: List[OptimizerDeviceConfig],
        battery: OptimizerBatteryConfig,
        grid: OptimizerGridConfig,
    ) -> int:
        self._devices = list(devices or [])
        self._battery = battery or OptimizerBatteryConfig()
        self._grid = grid or OptimizerGridConfig()
        if self._devices:
            logger.info("Optimizer: %d device(s) registered", len(self._devices))
        return len(self._devices)

    @property
    def enabled(self) -> bool:
        return bool(self._devices)

    def _check_milp(self) -> bool:
        if self._milp_available is None:
            try:
                from scipy.optimize import milp  # noqa: F401
                self._milp_available = True
            except ImportError:
                self._milp_available = False
                logger.info("scipy.optimize.milp unavailable — using greedy optimizer fallback")
        return self._milp_available

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def optimize(
        self,
        price_forecast: List[float],
        pv_forecast_w: List[float],
        battery_soc_pct: float = 50.0,
        base_load_forecast_w: Optional[List[float]] = None,
    ) -> OptimizationResult:
        """Compute the optimal 24h dispatch schedule and persist it to cache."""
        prices = self._pad(list(price_forecast or []), DEFAULT_FALLBACK_PRICE)
        pv = self._pad(list(pv_forecast_w or []), 0.0)
        base_load = self._pad(list(base_load_forecast_w or []), 0.0)

        result: Optional[OptimizationResult] = None
        battery_enabled = self._battery.capacity_kwh > 0 and (
            self._battery.max_charge_w > 0 or self._battery.max_discharge_w > 0
        )
        if self._check_milp() and (self._devices or battery_enabled):
            try:
                result = self._solve_milp(prices, pv, base_load, battery_soc_pct)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("MILP solver exception: %s — falling back to greedy", exc)
                result = None
        if result is None:
            result = self._solve_greedy(prices, pv)

        result.computed_at = datetime.now(tz=timezone.utc).isoformat()
        self._last_result = result
        self._persist(result)
        return result

    # ------------------------------------------------------------------
    # MILP formulation
    # ------------------------------------------------------------------

    def _solve_milp(
        self,
        prices: List[float],
        pv: List[float],
        base_load: List[float],
        battery_soc_pct: float,
    ) -> OptimizationResult:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp

        H = HOURS
        bat = self._battery
        eff = max(0.5, min(1.0, bat.efficiency or 0.95))
        soc_lo = bat.capacity_kwh * bat.soc_min_pct / 100.0
        soc_hi = bat.capacity_kwh * bat.soc_max_pct / 100.0
        battery_enabled = bat.capacity_kwh > 0 and (bat.max_charge_w > 0 or bat.max_discharge_w > 0)
        soc_init = bat.capacity_kwh * max(0.0, min(100.0, battery_soc_pct)) / 100.0
        if battery_enabled:
            soc_init = max(soc_lo, min(soc_hi, soc_init))
        pcc_import = self._grid.pcc_import_w or 25000.0
        pcc_export = self._grid.pcc_export_w or 12000.0
        export_price = self._grid.export_price_chf_kwh
        cycle_cost = bat.cycle_cost_chf_kwh or DEFAULT_BATTERY_CYCLE_COST

        bounds_lo: List[float] = []
        bounds_hi: List[float] = []
        integ: List[int] = []
        cost: List[float] = []

        def _add_var(lo: float, hi: float, is_int: int, c: float) -> int:
            bounds_lo.append(lo)
            bounds_hi.append(hi)
            integ.append(is_int)
            cost.append(c)
            return len(bounds_lo) - 1

        p_idx: Dict[int, List[int]] = {}
        b_idx: Dict[int, List[int]] = {}
        for d, dev in enumerate(self._devices):
            p_idx[d] = []
            b_idx[d] = []
            in_win = self._window_mask(dev.preferred_window, H)
            for h in range(H):
                if dev.switchable:
                    p_idx[d].append(_add_var(0.0, dev.max_power_w, 0, 0.0))
                    b_hi = 1 if in_win[h] else 0
                    b_idx[d].append(_add_var(0.0, float(b_hi), 1, 0.0))
                else:
                    p_idx[d].append(_add_var(dev.min_power_w, dev.max_power_w, 0, 0.0))
                    b_idx[d].append(-1)

        imp_idx = [_add_var(0.0, pcc_import, 0, prices[h] / 1000.0) for h in range(H)]
        exp_idx = [_add_var(0.0, pcc_export, 0, -export_price / 1000.0) for h in range(H)]
        gdir_idx = [_add_var(0.0, 1.0, 1, 0.0) for _ in range(H)]

        if battery_enabled:
            ch_idx = [_add_var(0.0, bat.max_charge_w, 0, cycle_cost / 1000.0) for _ in range(H)]
            dis_idx = [_add_var(0.0, bat.max_discharge_w, 0, cycle_cost / 1000.0) for _ in range(H)]
            soc_idx = [_add_var(soc_lo, soc_hi, 0, 0.0) for _ in range(H)]
        else:
            ch_idx = dis_idx = soc_idx = []

        n_vars = len(bounds_lo)

        eq_rows: List[List[float]] = []
        eq_b: List[float] = []
        ub_rows: List[List[float]] = []
        ub_b: List[float] = []

        def _row() -> List[float]:
            return [0.0] * n_vars

        for h in range(H):
            r = _row()
            for d in range(len(self._devices)):
                r[p_idx[d][h]] -= 1.0
            r[imp_idx[h]] += 1.0
            r[exp_idx[h]] -= 1.0
            if battery_enabled:
                r[dis_idx[h]] += 1.0
                r[ch_idx[h]] -= 1.0
            eq_rows.append(r)
            eq_b.append(base_load[h] - pv[h])

        if battery_enabled:
            for h in range(H):
                r = _row()
                r[soc_idx[h]] += 1.0
                r[ch_idx[h]] -= eff / 1000.0
                r[dis_idx[h]] += 1.0 / (eff * 1000.0)
                if h == 0:
                    eq_rows.append(r)
                    eq_b.append(soc_init)
                else:
                    r[soc_idx[h - 1]] -= 1.0
                    eq_rows.append(r)
                    eq_b.append(0.0)

        for d, dev in enumerate(self._devices):
            if dev.switchable:
                for h in range(H):
                    r = _row()
                    r[p_idx[d][h]] += 1.0
                    r[b_idx[d][h]] -= dev.max_power_w
                    ub_rows.append(r)
                    ub_b.append(0.0)
                    if dev.min_power_w > 0:
                        r2 = _row()
                        r2[p_idx[d][h]] -= 1.0
                        r2[b_idx[d][h]] += dev.min_power_w
                        ub_rows.append(r2)
                        ub_b.append(0.0)
                if dev.must_run_hours > 0 and dev.max_power_w > 0:
                    r = _row()
                    for h in range(H):
                        r[p_idx[d][h]] -= 1.0
                    ub_rows.append(r)
                    ub_b.append(-float(dev.must_run_hours) * dev.max_power_w)
            else:
                if dev.must_run_hours > 0 and dev.max_power_w > 0:
                    r = _row()
                    for h in range(H):
                        r[p_idx[d][h]] -= 1.0
                    ub_rows.append(r)
                    ub_b.append(-float(dev.must_run_hours) * dev.max_power_w)

        for h in range(H):
            r = _row()
            r[imp_idx[h]] += 1.0
            r[gdir_idx[h]] -= pcc_import
            ub_rows.append(r)
            ub_b.append(0.0)
            r2 = _row()
            r2[exp_idx[h]] += 1.0
            r2[gdir_idx[h]] += pcc_export
            ub_rows.append(r2)
            ub_b.append(float(pcc_export))

        constraints = []
        if eq_rows:
            constraints.append(LinearConstraint(np.array(eq_rows), np.array(eq_b), np.array(eq_b)))
        if ub_rows:
            constraints.append(LinearConstraint(np.array(ub_rows), -np.inf, np.array(ub_b)))

        res = milp(
            c=np.array(cost),
            constraints=constraints,
            integrality=np.array(integ),
            bounds=Bounds(np.array(bounds_lo), np.array(bounds_hi)),
        )

        if not res.success or res.x is None:
            logger.warning("MILP infeasible/failed: %s — falling back to greedy", res.message)
            return self._solve_greedy(prices, pv)

        x = res.x
        result = OptimizationResult()
        result.solver = "scipy_milp"
        for d, dev in enumerate(self._devices):
            result.schedule[dev.name] = [max(0.0, round(float(x[p_idx[d][h]]), 0)) for h in range(H)]
        result.grid_import_w = [max(0.0, round(float(x[imp_idx[h]]), 0)) for h in range(H)]
        result.grid_export_w = [max(0.0, round(float(x[exp_idx[h]]), 0)) for h in range(H)]
        if battery_enabled:
            result.battery_charge_w = [max(0.0, round(float(x[ch_idx[h]]), 0)) for h in range(H)]
            result.battery_discharge_w = [max(0.0, round(float(x[dis_idx[h]]), 0)) for h in range(H)]
            result.battery_soc_kwh = [round(float(x[soc_idx[h]]), 3) for h in range(H)]

        result.total_cost_chf = sum(
            prices[h] * result.grid_import_w[h] / 1000.0 - export_price * result.grid_export_w[h] / 1000.0
            for h in range(H)
        )
        result.grid_import_kwh = sum(result.grid_import_w) / 1000.0
        result.grid_export_kwh = sum(result.grid_export_w) / 1000.0
        if battery_enabled and bat.capacity_kwh > 0:
            throughput_kwh = sum(result.battery_charge_w) / 1000.0
            result.battery_cycles = throughput_kwh / bat.capacity_kwh
        pv_total = sum(pv) / 1000.0
        if pv_total > 0:
            result.self_consumption_pct = max(
                0.0, min(100.0, (pv_total - result.grid_export_kwh) / pv_total * 100.0)
            )

        result.baseline_cost_chf = self._naive_baseline_cost(prices, base_load)
        result.savings_chf = result.baseline_cost_chf - result.total_cost_chf
        return result

    @staticmethod
    def _window_mask(window: Optional[List[int]], n: int) -> List[bool]:
        if not window:
            return [True] * n
        lo, hi = int(window[0]), int(window[1])
        mask = []
        for h in range(n):
            if lo <= hi:
                mask.append(lo <= h <= hi)
            else:  # wraps past midnight, e.g. [22, 6]
                mask.append(h >= lo or h <= hi)
        return mask

    def _naive_baseline_cost(self, prices: List[float], base_load: List[float]) -> float:
        """Cost of serving base load + device must-run energy from the grid,
        with no PV, no battery and no price-aware shifting. Reference for
        the reported savings.
        """
        H = HOURS
        grid_w = list(base_load)
        for dev in self._devices:
            run_h = dev.must_run_hours or 0
            if run_h <= 0:
                if not dev.switchable and dev.min_power_w > 0:
                    for h in range(H):
                        grid_w[h] += dev.min_power_w
                continue
            for h in range(min(run_h, H)):
                grid_w[h] += dev.max_power_w
        return sum(prices[h] * grid_w[h] / 1000.0 for h in range(H))

    # ------------------------------------------------------------------
    # Greedy fallback (used when scipy is unavailable or the MILP fails)
    # ------------------------------------------------------------------

    def _solve_greedy(self, prices: List[float], pv: List[float]) -> OptimizationResult:
        result = OptimizationResult()
        result.solver = "greedy"
        pcc_limit_w = self._grid.pcc_import_w or 25000.0

        hour_costs = []
        for h in range(HOURS):
            pv_benefit = min(pv[h], pcc_limit_w) / pcc_limit_w * prices[h] * 0.5 if pcc_limit_w else 0.0
            hour_costs.append((h, prices[h] - pv_benefit))
        hour_costs.sort(key=lambda x: x[1])

        for dev in self._devices:
            schedule = [dev.min_power_w] * HOURS
            hours_to_fill = dev.must_run_hours or HOURS
            filled = 0
            for h, _ in hour_costs:
                if filled >= hours_to_fill:
                    break
                if dev.preferred_window:
                    lo, hi = dev.preferred_window
                    if not (lo <= h <= hi or (lo > hi and (h >= lo or h <= hi))):
                        continue
                schedule[h] = dev.max_power_w
                filled += 1
            if filled < hours_to_fill:
                for h, _ in hour_costs:
                    if filled >= hours_to_fill:
                        break
                    if schedule[h] < dev.max_power_w:
                        schedule[h] = dev.max_power_w
                        filled += 1
            result.schedule[dev.name] = schedule

        result.total_cost_chf = sum(
            prices[h] * sum(result.schedule.get(dev.name, [0] * HOURS)[h] for dev in self._devices) / 1000.0
            for h in range(HOURS)
        )
        result.baseline_cost_chf = sum(
            prices[h] * sum(d.max_power_w for d in self._devices) / 1000.0 for h in range(HOURS)
        )
        result.savings_chf = result.baseline_cost_chf - result.total_cost_chf
        return result

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _pad(values: List[float], fill: float) -> List[float]:
        out = list(values[:HOURS])
        while len(out) < HOURS:
            out.append(out[-1] if out else fill)
        return out

    def _persist(self, result: OptimizationResult) -> None:
        try:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(self._cache_file)
        except OSError as exc:
            logger.warning("Optimizer: cannot persist schedule: %s", exc)

    def get_last_result(self) -> Optional[OptimizationResult]:
        """Return the last computed result (in-memory, else from disk)."""
        if self._last_result:
            return self._last_result
        if not self._cache_file.exists():
            return None
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            result = OptimizationResult.from_dict(data)
            self._last_result = result
            return result
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Forecast helpers
    # ------------------------------------------------------------------

    @staticmethod
    def hourly_price_series(
        forecast: List[tuple], hours: int = HOURS, fallback: float = DEFAULT_FALLBACK_PRICE
    ) -> List[float]:
        """Bucket a ``[(timestamp, price), ...]`` series (as returned by
        ``RulesEngine._extract_price_forecast``) into ``hours`` hour-aligned
        slots starting at the current hour.
        """
        if not forecast:
            return [fallback] * hours
        now = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
        by_hour: Dict[datetime, float] = {}
        for ts, price in forecast:
            ts_hour = ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            by_hour[ts_hour] = price

        out: List[float] = []
        last = fallback
        for h in range(hours):
            slot_time = now + timedelta(hours=h)
            if slot_time in by_hour:
                last = by_hour[slot_time]
            out.append(last)
        return out
