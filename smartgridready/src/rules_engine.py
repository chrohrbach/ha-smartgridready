"""Rules engine — evaluate optimisation rules and apply actions.

Runs on a fixed cadence (configurable, default 5 min). For every rule it
walks the condition list in priority order, picks the first match, and
writes the resulting value to the target SGr data point.

Safety guarantees:
  - Hysteresis (``min_interval``) prevents rapid mode changes.
  - Redundant writes are skipped (same value as last cycle).
  - Negative values (V2H/V2G discharge) require explicit per-vehicle
    authorisation: enabled flag, plug status, min SOC + safety margin,
    optional time window, daily cycle cap. The cap counts true
    *transitions* into discharge, not raw negative writes.
  - SG-Ready ``HP_LOCKED`` writes are capped at the BWP-mandated 2 h
    per day. Excess locking is auto-replaced with ``HP_NORMAL``.
  - The condition DSL is parsed with simple string operations — no
    ``eval()`` — so user config files cannot execute arbitrary code.
  - All wall-clock comparisons go through a configurable time zone so
    rule windows track local time even across DST shifts.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover — Python <3.9
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment]

from .config_loader import RuleConfig, SensorMap, UserConfig, VehicleConfig

logger = logging.getLogger("smartgridready.rules")

MAX_AUDIT_ENTRIES = 288  # 24 h × 12 ticks / h at 5-min cadence
DEFAULT_MIN_INTERVAL = 15
V2H_SOC_SAFETY_MARGIN = 5  # always keep this many points above declared min_soc

# Drop persisted hysteresis entries older than this on load — protects
# against unbounded growth when rule definitions churn over time.
HYSTERESIS_MAX_AGE_DAYS = 30

# Default Swiss grid CO₂ intensity (kg CO₂ / kWh consumed mix, ElCom 2024).
# Overridden by Electricity Maps / CO2 Signal sensors if present.
DEFAULT_GRID_CO2_KG_PER_KWH = 0.128

# BWP SG-Ready 1.1 spec: utility lock (Mode 1) may not exceed two hours
# per 24 h. Default cap kept here as a constant; per-instance value
# overrides it through the constructor.
DEFAULT_SG_READY_LOCK_CAP_MINUTES = 120

# Canonical entity_ids auto-detected from the HA state map.
CO2_INTENSITY_CANDIDATES = (
    "sensor.electricity_maps_co2_intensity",
    "sensor.electricity_maps_intensite_carbone",
    "sensor.co2_signal_co2_intensity",
)
CO2_FOSSIL_PCT_CANDIDATES = (
    "sensor.electricity_maps_grid_fossil_fuel_percentage",
    "sensor.electricity_maps_pourcentage_d_energies_fossiles_du_reseau",
    "sensor.co2_signal_grid_fossil_fuel_percentage",
)
DSO_ACTIVE_CANDIDATES = (
    "binary_sensor.dso_curtailment_active",
    "binary_sensor.grid_curtailment_active",
)
DSO_FACTOR_CANDIDATES = (
    "sensor.dso_curtailment_factor",
    "sensor.grid_curtailment_factor",
)


def resolve_grid_co2(state_map: Dict[str, Dict]) -> float:
    """Return kg CO₂ per kWh.

    Looks for Electricity Maps / CO2 Signal entities in the state map. If
    none are available or parseable, returns the Swiss default constant.
    Direct intensity sensors win over fossil-percentage sensors.
    """

    def _num(eid: str) -> Optional[float]:
        raw = state_map.get(eid, {}).get("state")
        if raw in (None, "", "unknown", "unavailable"):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    for eid in CO2_INTENSITY_CANDIDATES:
        v = _num(eid)
        if v is not None and v > 0:
            return round(v / 1000.0, 4)

    for eid in CO2_FOSSIL_PCT_CANDIDATES:
        v = _num(eid)
        if v is not None and 0 <= v <= 100:
            return round(0.05 + 0.005 * v, 4)

    return DEFAULT_GRID_CO2_KG_PER_KWH


class RulesEngine:
    """Evaluate rules against current HA state and write SGr commands."""

    def __init__(
        self,
        sgr_service,
        ha_client,
        audit_path: Path,
        tz_name: str = "Europe/Zurich",
        sg_ready_lock_cap_minutes: int = DEFAULT_SG_READY_LOCK_CAP_MINUTES,
    ):
        self.sgr = sgr_service
        self.ha = ha_client
        self.audit_path = audit_path
        self.tz_name = tz_name or "Europe/Zurich"
        self.sg_ready_lock_cap_minutes = max(0, int(sg_ready_lock_cap_minutes))
        self._tz = self._resolve_tz(self.tz_name)
        # Hysteresis state lives next to the audit log so both survive
        # add-on restarts. Without persistence a reboot resets the
        # min_interval counter — a heat-pump compressor could be commanded
        # back-to-back within seconds.
        self.hysteresis_path = audit_path.with_name("hysteresis.json")
        # SG-Ready lock-time ledger (per rule_id → list of (start, end)
        # ISO-UTC timestamps; an open period has end=None).
        self.sg_ready_lock_path = audit_path.with_name("sg_ready_lock.json")
        self._last_values: Dict[str, Any] = {}
        self._last_change_times: Dict[str, datetime] = {}
        self._v2h_count_today: Dict[str, int] = {}
        self._v2h_day: Optional[str] = None
        self._sg_lock_ledger: Dict[str, List[List[Optional[str]]]] = {}
        # One-shot per-expression warning suppression so a typo in a
        # condition only logs once per add-on lifetime instead of every
        # cycle.
        self._dsl_warned: set[str] = set()
        self.last_result: Dict[str, Any] = {}
        self._load_hysteresis()
        self._load_sg_lock_ledger()

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_tz(name: str):
        """Return a tzinfo for ``name`` with UTC fallback."""
        if ZoneInfo is None:
            logger.warning("zoneinfo not available — falling back to UTC")
            return timezone.utc
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown timezone %r — falling back to UTC", name)
            return timezone.utc

    def _now_utc(self) -> datetime:
        """Aware UTC ``datetime`` — used for hysteresis & deltas."""
        return datetime.now(tz=timezone.utc)

    def _now_local(self) -> datetime:
        """Aware local ``datetime`` — used for hour/day/window context."""
        return self._now_utc().astimezone(self._tz)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def evaluate(self, config: UserConfig) -> Dict[str, Any]:
        """Run one evaluation cycle and return the result summary."""
        states_raw = self.ha.get_states() if self.ha else []
        state_map = {s.get("entity_id"): s for s in (states_raw or [])}

        # Honour the optional kill switch.
        if config.enable_toggle:
            toggle = state_map.get(config.enable_toggle, {})
            if toggle.get("state") == "off":
                result = {
                    "timestamp": self._now_utc().isoformat(),
                    "skipped": "disabled",
                    "reason": f"{config.enable_toggle} is off",
                }
                self.last_result = result
                return result

        context = self._build_context(
            config.sensors,
            config.vehicles,
            state_map,
            grid_connection_limit_w=config.grid_connection_limit_w,
            battery_capacity_kwh=config.battery_capacity_kwh,
        )
        actions_taken: List[Dict[str, Any]] = []
        actions_skipped: List[Dict[str, Any]] = []

        for rule in config.rules:
            rule_id = f"{rule.device}/{rule.profile}/{rule.data_point}"
            try:
                value = self._evaluate_conditions(rule.conditions, context)
                if value is None:
                    actions_skipped.append({"rule": rule_id, "reason": "no_condition_matched"})
                    continue

                # SG-Ready Mode-1 (HP_LOCKED) is capped to 2 h / 24 h
                # by the BWP spec. Pre-empt any rule that would exceed
                # the cap and switch the command back to HP_NORMAL.
                guard = self._apply_sg_ready_lock_cap(rule_id, rule, value)
                if guard is not None:
                    value, lock_note = guard
                    if lock_note:
                        actions_skipped.append({
                            "rule": rule_id, "reason": "sg_ready_lock_cap",
                            "detail": lock_note,
                        })

                # V2H/V2G safety check for negative targets
                if isinstance(value, (int, float)) and value < 0:
                    check = self._check_v2h_authorization(rule.device, value, config.vehicles, context)
                    if not check["allowed"]:
                        actions_skipped.append({
                            "rule": rule_id, "reason": "v2h_blocked",
                            "value": value, "detail": check["reason"],
                        })
                        continue
                    value = check["clamped_value"]

                # Skip redundant writes
                previous_value = self._last_values.get(rule_id)
                if previous_value == value:
                    actions_skipped.append({"rule": rule_id, "reason": "already_set", "value": value})
                    continue

                # Hysteresis
                min_interval = rule.min_interval or DEFAULT_MIN_INTERVAL
                if rule_id in self._last_change_times:
                    last = self._last_change_times[rule_id]
                    if last.tzinfo is None:
                        # Pre-0.2 naive timestamps and direct test pokes.
                        last = last.replace(tzinfo=timezone.utc)
                    elapsed_min = (self._now_utc() - last).total_seconds() / 60
                    if elapsed_min < min_interval:
                        actions_skipped.append({
                            "rule": rule_id, "reason": "hysteresis",
                            "value": value,
                            "wait_minutes": round(min_interval - elapsed_min, 1),
                        })
                        continue

                if rule.smooth_transition:
                    await self._apply_smooth_transition(
                        rule.device,
                        rule.profile,
                        rule.data_point,
                        rule.smooth_transition,
                    )
                await self.sgr.write(rule.device, rule.profile, rule.data_point, value)
                self._last_change_times[rule_id] = self._now_utc()
                # V2H discharge is counted as a *cycle* only when the
                # written value transitions from a non-negative (or
                # absent) state into a negative one. Cumulative bursts
                # of negative writes count as a single cycle, which is
                # what max_cycles_per_day is supposed to mean.
                if (
                    isinstance(value, (int, float))
                    and value < 0
                    and not (
                        isinstance(previous_value, (int, float))
                        and previous_value < 0
                    )
                ):
                    self._track_v2h_discharge(rule.device, config.vehicles)
                self._last_values[rule_id] = value
                self._update_sg_ready_lock_ledger(rule_id, rule, value)

                matched = self._get_matched_condition(rule.conditions, context)
                actions_taken.append({"rule": rule_id, "value": value, "reason": matched})
                logger.info("Rule %s → %s (%s)", rule_id, value, matched)

            except Exception as exc:
                logger.error("Rule %s failed: %s", rule_id, exc)
                actions_skipped.append({"rule": rule_id, "reason": "error", "error": str(exc)})

        result = {
            "timestamp": self._now_utc().isoformat(),
            "context": {k: v for k, v in context.items() if not k.startswith("_")},
            "actions_taken": len(actions_taken),
            "actions_skipped": len(actions_skipped),
            "details": actions_taken,
            "skipped": actions_skipped,
        }
        self.last_result = result

        if actions_taken:
            logger.info(
                "Rules: %d action(s) taken, %d skipped",
                len(actions_taken), len(actions_skipped),
            )

        try:
            self._persist_audit(result)
        except Exception:
            logger.debug("Audit persistence failed", exc_info=True)
        try:
            self._persist_hysteresis()
        except Exception:
            logger.debug("Hysteresis persistence failed", exc_info=True)
        try:
            self._persist_sg_lock_ledger()
        except Exception:
            logger.debug("SG-Ready lock ledger persistence failed", exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def _persist_audit(self, result: Dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        entries: List[Dict[str, Any]] = []
        if self.audit_path.exists():
            try:
                entries = json.loads(self.audit_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                entries = []
        if not isinstance(entries, list):
            entries = []
        entries.append(result)
        entries = entries[-MAX_AUDIT_ENTRIES:]
        tmp = self.audit_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.audit_path)

    def load_audit(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        try:
            data = json.loads(self.audit_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, list):
            return []
        return list(reversed(data[-limit:]))

    # ------------------------------------------------------------------
    # Hysteresis persistence
    # ------------------------------------------------------------------

    def _load_hysteresis(self) -> None:
        """Restore last-change timestamps from disk (best-effort).

        Entries older than ``HYSTERESIS_MAX_AGE_DAYS`` are pruned at load
        to avoid unbounded growth from removed / renamed rules. Pre-0.2
        timestamps were naive local time; we interpret those as UTC,
        which means a one-off hysteresis miss after upgrade — acceptable.
        """
        if not self.hysteresis_path.exists():
            return
        try:
            data = json.loads(self.hysteresis_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Hysteresis state corrupted (%s) — starting fresh", exc)
            return
        if not isinstance(data, dict):
            return
        cutoff = self._now_utc() - timedelta(days=HYSTERESIS_MAX_AGE_DAYS)
        restored = 0
        for key, iso_ts in data.items():
            try:
                dt = datetime.fromisoformat(iso_ts)
            except (TypeError, ValueError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > cutoff:
                self._last_change_times[key] = dt
                restored += 1
        if restored:
            logger.info("Hysteresis: restored %d entries from disk", restored)

    def _persist_hysteresis(self) -> None:
        """Persist last-change timestamps atomically (UTC)."""
        if not self._last_change_times:
            return
        self.hysteresis_path.parent.mkdir(parents=True, exist_ok=True)
        serializable = {
            k: (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
            for k, v in self._last_change_times.items()
        }
        tmp = self.hysteresis_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(serializable, indent=1), encoding="utf-8")
        tmp.replace(self.hysteresis_path)

    # ------------------------------------------------------------------
    # SG-Ready lock-time ledger (BWP 1.1 §3.2 — Mode 1 ≤ 2 h / 24 h)
    # ------------------------------------------------------------------

    def _load_sg_lock_ledger(self) -> None:
        if not self.sg_ready_lock_path.exists():
            return
        try:
            data = json.loads(self.sg_ready_lock_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        # Each entry is a list of [start_iso, end_iso_or_null] pairs.
        for key, periods in data.items():
            if not isinstance(periods, list):
                continue
            self._sg_lock_ledger[key] = [
                p for p in periods
                if isinstance(p, list) and len(p) == 2 and isinstance(p[0], str)
            ]

    def _persist_sg_lock_ledger(self) -> None:
        if not self._sg_lock_ledger:
            return
        self.sg_ready_lock_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.sg_ready_lock_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._sg_lock_ledger, indent=1, default=str),
            encoding="utf-8",
        )
        tmp.replace(self.sg_ready_lock_path)

    @staticmethod
    def _is_sg_ready_lock_rule(rule: RuleConfig, value: Any) -> bool:
        """True iff the (rule, value) combination is an SG-Ready Mode-1 lock."""
        profile = (rule.profile or "").lower()
        if not any(tag in profile for tag in ("sg-ready", "sgready", "sg_ready")):
            return False
        val_str = str(value).upper() if value is not None else ""
        # Tolerate both the enum literal and the BWP ordinal "1".
        return "LOCKED" in val_str or val_str == "1"

    def _sg_lock_minutes_in_window(self, rule_id: str, now: datetime) -> float:
        """Total minutes the rule has been in HP_LOCKED over the last 24 h."""
        cutoff = now - timedelta(hours=24)
        total = 0.0
        kept: List[List[Optional[str]]] = []
        for start_iso, end_iso in self._sg_lock_ledger.get(rule_id, []):
            try:
                start = datetime.fromisoformat(start_iso)
            except (TypeError, ValueError):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end_iso is None:
                end = now
            else:
                try:
                    end = datetime.fromisoformat(end_iso)
                except (TypeError, ValueError):
                    continue
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
            if end <= cutoff:
                continue  # too old → prune on persist
            window_start = max(start, cutoff)
            total += max(0.0, (end - window_start).total_seconds() / 60.0)
            kept.append([start_iso, end_iso])
        # Side effect: prune stale periods.
        if kept != self._sg_lock_ledger.get(rule_id):
            self._sg_lock_ledger[rule_id] = kept
        return total

    def _apply_sg_ready_lock_cap(
        self, rule_id: str, rule: RuleConfig, value: Any
    ) -> Optional[Tuple[Any, Optional[str]]]:
        """Replace HP_LOCKED with HP_NORMAL when the daily cap is exhausted.

        Returns ``None`` when nothing happened (so the caller can keep
        the original ``value``); otherwise returns ``(new_value, note)``
        where ``note`` is a short explanation pushed into the audit log.
        """
        if self.sg_ready_lock_cap_minutes <= 0:
            return None
        if not self._is_sg_ready_lock_rule(rule, value):
            return None
        now = self._now_utc()
        used = self._sg_lock_minutes_in_window(rule_id, now)
        if used >= self.sg_ready_lock_cap_minutes:
            note = (
                f"daily lock cap reached: {used:.0f} min already locked "
                f"in last 24 h (max {self.sg_ready_lock_cap_minutes}) — "
                "downgrading to HP_NORMAL"
            )
            logger.warning("Rule %s: %s", rule_id, note)
            return ("HP_NORMAL", note)
        return None

    def _update_sg_ready_lock_ledger(
        self, rule_id: str, rule: RuleConfig, written_value: Any
    ) -> None:
        """Open or close a lock period in response to a successful write."""
        now_iso = self._now_utc().isoformat()
        periods = self._sg_lock_ledger.setdefault(rule_id, [])
        is_lock = self._is_sg_ready_lock_rule(rule, written_value)

        # Find the trailing open period, if any.
        open_idx = next(
            (i for i in range(len(periods) - 1, -1, -1)
             if isinstance(periods[i], list) and len(periods[i]) == 2 and periods[i][1] is None),
            None,
        )
        if is_lock:
            if open_idx is None:
                periods.append([now_iso, None])
        else:
            if open_idx is not None:
                periods[open_idx][1] = now_iso

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def _build_context(
        self,
        sensors: SensorMap,
        vehicles: List[VehicleConfig],
        state_map: Dict[str, Dict],
        grid_connection_limit_w: float = 0.0,
        battery_capacity_kwh: float = 0.0,
    ) -> Dict[str, Any]:
        now = self._now_local()
        minute = now.minute
        ctx: Dict[str, Any] = {
            "hour": now.hour,
            "minute": minute,
            "minute_in_quarter": minute % 15,
            "weekday": now.weekday(),
            "is_weekend": now.weekday() >= 5,
        }

        # Numeric sensor mapping (all known + extras)
        numeric_fields = [
            "spot_price", "pv_power", "house_consumption", "battery_soc",
            "grid_export", "grid_import", "temperature_outdoor",
            "pv_forecast_kwh", "pv_forecast_today_remaining_kwh",
            "pv_current_hour_kwh", "pv_forecast_today_kwh",
        ]
        for key in numeric_fields:
            entity_id = getattr(sensors, key, None)
            ctx[key] = self._safe_float(state_map, entity_id)
        for key, entity_id in sensors.extra.items():
            ctx[key] = self._safe_float(state_map, entity_id)

        pv = ctx.get("pv_power", 0)
        consumption = ctx.get("house_consumption", 0)
        ctx["surplus_pv"] = max(0, pv - consumption)
        ctx["has_surplus"] = ctx["surplus_pv"] > 500
        ctx["home_deficit_w"] = max(0, consumption - pv)

        hour = ctx["hour"]
        ctx["is_peak"] = 7 <= hour <= 9 or 17 <= hour <= 20
        ctx["is_offpeak"] = hour < 6 or hour >= 22
        ctx["is_solar_window"] = 10 <= hour <= 16

        pv_tomorrow = ctx.get("pv_forecast_kwh", 0)
        ctx["expect_high_pv_tomorrow"] = pv_tomorrow > 20
        ctx["expect_low_pv_tomorrow"] = 0 < pv_tomorrow < 8

        # Presence
        away_state = state_map.get(sensors.away_mode or "", {}).get("state")
        ctx["away_mode"] = away_state == "on"
        ctx["at_home"] = not ctx["away_mode"]
        ctx["away_effective"] = ctx["away_mode"]

        # Grid CO₂
        ctx["grid_co2_kg_per_kwh"] = resolve_grid_co2(state_map)

        # PCC headroom (L5)
        self._build_pcc_context(ctx, grid_connection_limit_w)

        # Battery helpers (L7)
        self._build_battery_context(ctx, battery_capacity_kwh)

        # DSO curtailment signal (L6)
        self._build_dso_context(ctx, sensors, state_map)

        # Tariff horizon (L9) — pulls forecast attribute from the spot
        # price entity if it exposes one.
        self._build_tariff_horizon(ctx, sensors, state_map)

        # Vehicles
        self._build_vehicle_context(ctx, vehicles, state_map)

        return ctx

    @staticmethod
    def _build_pcc_context(ctx: Dict[str, Any], limit_w: float) -> None:
        gi = float(ctx.get("grid_import", 0) or 0)
        ge = float(ctx.get("grid_export", 0) or 0)
        ctx["pcc_power_w"] = gi - ge  # positive = drawing from grid
        ctx["pcc_limit_w"] = float(limit_w or 0)
        if limit_w and limit_w > 0:
            ctx["pcc_headroom_w"] = max(0.0, float(limit_w) - max(0.0, gi))
            ctx["pcc_overload"] = gi > float(limit_w)
        else:
            ctx["pcc_headroom_w"] = 0.0
            ctx["pcc_overload"] = False

    @staticmethod
    def _build_battery_context(ctx: Dict[str, Any], capacity_kwh: float) -> None:
        soc = float(ctx.get("battery_soc", 0) or 0)
        ctx["battery_full"] = soc > 95
        ctx["battery_low"] = 0 < soc < 20
        if capacity_kwh and capacity_kwh > 0:
            ctx["battery_capacity_kwh"] = float(capacity_kwh)
            ctx["battery_available_kwh"] = round(capacity_kwh * soc / 100.0, 3)
            ctx["battery_room_kwh"] = round(capacity_kwh * max(0.0, 100.0 - soc) / 100.0, 3)
        else:
            ctx["battery_capacity_kwh"] = 0.0
            ctx["battery_available_kwh"] = 0.0
            ctx["battery_room_kwh"] = 0.0

    def _build_dso_context(
        self, ctx: Dict[str, Any], sensors: SensorMap, state_map: Dict[str, Dict]
    ) -> None:
        active_eid = sensors.dso_curtailment_active or next(
            (eid for eid in DSO_ACTIVE_CANDIDATES if eid in state_map), None
        )
        factor_eid = sensors.dso_curtailment_factor or next(
            (eid for eid in DSO_FACTOR_CANDIDATES if eid in state_map), None
        )
        if active_eid:
            ctx["dso_curtailment_active"] = (
                state_map.get(active_eid, {}).get("state") == "on"
            )
        else:
            ctx["dso_curtailment_active"] = False
        if factor_eid:
            ctx["dso_curtailment_factor"] = self._safe_float(state_map, factor_eid)
        else:
            ctx["dso_curtailment_factor"] = 0.0

    def _build_tariff_horizon(
        self, ctx: Dict[str, Any], sensors: SensorMap, state_map: Dict[str, Dict]
    ) -> None:
        ctx["tariff_next_3h_min"] = 0.0
        ctx["tariff_next_3h_max"] = 0.0
        ctx["tariff_next_3h_avg"] = 0.0
        ctx["tariff_in_lowest_quartile_today"] = False

        eid = sensors.spot_price
        if not eid:
            return
        state = state_map.get(eid)
        if not state:
            return
        attrs = state.get("attributes") or {}
        forecast = self._extract_price_forecast(attrs)
        if not forecast:
            return

        now = self._now_utc()
        horizon = now + timedelta(hours=3)
        next3h = [p for ts, p in forecast if now <= ts < horizon]
        if next3h:
            ctx["tariff_next_3h_min"] = round(min(next3h), 4)
            ctx["tariff_next_3h_max"] = round(max(next3h), 4)
            ctx["tariff_next_3h_avg"] = round(sum(next3h) / len(next3h), 4)

        today = now.astimezone(self._tz).date()
        today_prices = [
            p for ts, p in forecast
            if ts.astimezone(self._tz).date() == today
        ]
        if today_prices:
            sorted_prices = sorted(today_prices)
            q1_idx = max(0, len(sorted_prices) // 4 - 1)
            q1 = sorted_prices[q1_idx]
            try:
                current = float(state.get("state"))
            except (TypeError, ValueError):
                current = None
            if current is not None:
                ctx["tariff_in_lowest_quartile_today"] = current <= q1

    @staticmethod
    def _extract_price_forecast(attrs: Dict[str, Any]) -> List[Tuple[datetime, float]]:
        """Best-effort parse of common HA-tariff forecast attribute shapes.

        Accepts a list of dicts with one of ``start``/``from``/``time``/
        ``timestamp`` for the time key and ``value``/``price``/``total``
        for the price key. Combines ``raw_today`` + ``raw_tomorrow``
        (Nordpool) when present.
        """
        raw: List[Any] = []
        for key in ("forecast", "prices"):
            value = attrs.get(key)
            if isinstance(value, list):
                raw.extend(value)
        for key in ("raw_today", "raw_tomorrow"):
            value = attrs.get(key)
            if isinstance(value, list):
                raw.extend(value)
        out: List[Tuple[datetime, float]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            ts_raw = (
                item.get("start") or item.get("from")
                or item.get("time") or item.get("timestamp")
            )
            price = item.get("value", item.get("price", item.get("total")))
            if ts_raw is None or price is None:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                out.append((ts, float(price)))
            except (ValueError, TypeError):
                continue
        return out

    def _build_vehicle_context(
        self,
        ctx: Dict[str, Any],
        vehicles: List[VehicleConfig],
        state_map: Dict[str, Dict],
    ) -> None:
        if not vehicles:
            ctx["v2h_available"] = False
            ctx["v2h_reserve_kwh_total"] = 0.0
            return

        any_avail = False
        total_reserve = 0.0
        for veh in vehicles:
            safe = re.sub(r"[^a-z0-9]+", "_", veh.name.lower()).strip("_") or "ev"

            soc = self._safe_float(state_map, veh.soc_entity)
            plugged = state_map.get(veh.plugged_entity or "", {}).get("state") == "on"
            power = self._safe_float(state_map, veh.charging_power_entity)

            ctx[f"ev_{safe}_soc"] = soc
            ctx[f"ev_{safe}_plugged"] = plugged
            ctx[f"ev_{safe}_charging_power"] = power

            v2h_cfg = veh.v2h or {}
            min_soc = float(v2h_cfg.get("min_soc", 50))
            cap = float(veh.battery_capacity_kwh or 0)
            reserve_kwh = max(0.0, (soc - min_soc) / 100.0 * cap) if cap > 0 else 0.0
            in_window = self._in_window(v2h_cfg.get("allow_window"), ctx["hour"])
            require_plugged = bool(v2h_cfg.get("require_plugged", True))

            available = bool(
                v2h_cfg.get("enabled", False)
                and (plugged or not require_plugged)
                and soc > min_soc + V2H_SOC_SAFETY_MARGIN
                and in_window
                and reserve_kwh > 0
            )
            ctx[f"ev_{safe}_v2h_available"] = available
            ctx[f"ev_{safe}_v2h_reserve_kwh"] = round(reserve_kwh, 2)
            if available:
                any_avail = True
                total_reserve += reserve_kwh

        # First-vehicle convenience aliases (simple rules with one EV)
        first = vehicles[0]
        first_safe = re.sub(r"[^a-z0-9]+", "_", first.name.lower()).strip("_") or "ev"
        for short in ("soc", "plugged", "charging_power", "v2h_available", "v2h_reserve_kwh"):
            ctx[f"ev_{short}"] = ctx.get(
                f"ev_{first_safe}_{short}",
                0 if any(s in short for s in ("soc", "power", "kwh")) else False,
            )

        ctx["v2h_available"] = any_avail
        ctx["v2h_reserve_kwh_total"] = round(total_reserve, 2)

    @staticmethod
    def _safe_float(state_map: Dict[str, Dict], entity_id: Optional[str]) -> float:
        if not entity_id:
            return 0.0
        raw = state_map.get(entity_id, {}).get("state")
        if raw in (None, "", "unknown", "unavailable"):
            return 0.0
        try:
            return float(raw)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _in_window(window: Optional[str], hour: int) -> bool:
        if not window:
            return True
        m = re.match(r"\s*(\d+)h?\s*-\s*(\d+)h?\s*", window)
        if not m:
            return True
        start, end = int(m.group(1)), int(m.group(2))
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    # ------------------------------------------------------------------
    # V2H safety
    # ------------------------------------------------------------------

    def _check_v2h_authorization(
        self,
        device: str,
        target_value: float,
        vehicles: List[VehicleConfig],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        veh = next((v for v in vehicles if v.charger_device == device), None)
        if veh is None:
            veh = next((v for v in vehicles if (v.v2h or {}).get("enabled")), None)
        if veh is None:
            return {"allowed": False, "reason": "no_vehicle_bound_to_device"}

        v2h_cfg = veh.v2h or {}
        if not v2h_cfg.get("enabled"):
            return {"allowed": False, "reason": "v2h_disabled_in_config"}
        if not context.get("v2h_available", False):
            return {"allowed": False, "reason": "v2h_not_available_now"}

        max_cycles = int(v2h_cfg.get("max_cycles_per_day", 1))
        today = self._now_local().strftime("%Y-%m-%d")
        if self._v2h_day != today:
            self._v2h_day = today
            self._v2h_count_today = {}
        if self._v2h_count_today.get(veh.name, 0) >= max_cycles:
            return {"allowed": False, "reason": "v2h_daily_cycle_limit_reached"}

        if v2h_cfg.get("requires_grd_agreement") and not v2h_cfg.get("grd_agreement_signed"):
            return {"allowed": False, "reason": "v2g_grd_agreement_missing"}

        max_amps = float(v2h_cfg.get("max_discharge_a", 16))
        clamped = max(-abs(max_amps), float(target_value))
        return {"allowed": True, "reason": "ok", "clamped_value": clamped}

    def _track_v2h_discharge(self, device: str, vehicles: List[VehicleConfig]) -> None:
        """Increment the per-day cycle counter — call once per *transition*.

        The caller in :meth:`evaluate` only invokes this helper when the
        previous written value was non-negative, so successive negative
        writes in a single discharge session count as one cycle.
        """
        veh = next((v for v in vehicles if v.charger_device == device), None)
        if veh is None:
            veh = next((v for v in vehicles if (v.v2h or {}).get("enabled")), None)
        if veh is None:
            return
        today = self._now_local().strftime("%Y-%m-%d")
        if self._v2h_day != today:
            self._v2h_day = today
            self._v2h_count_today = {}
        self._v2h_count_today[veh.name] = self._v2h_count_today.get(veh.name, 0) + 1

    # ------------------------------------------------------------------
    # Condition DSL
    # ------------------------------------------------------------------

    def _evaluate_conditions(
        self, conditions: List[Dict], context: Dict[str, Any]
    ) -> Optional[Any]:
        for cond in conditions:
            if "default" in cond:
                return self._resolve_value(cond.get("value"), context)
            if self._eval_expression(str(cond.get("when", "")), context):
                return self._resolve_value(cond.get("value"), context)
        return None

    def _get_matched_condition(
        self, conditions: List[Dict], context: Dict[str, Any]
    ) -> str:
        for cond in conditions:
            if "default" in cond:
                return "default"
            when = str(cond.get("when", ""))
            if self._eval_expression(when, context):
                return when
        return "none"

    def _eval_expression(self, expr: str, ctx: Dict[str, Any]) -> bool:
        """Evaluate a condition expression against context.

        Operator precedence follows standard boolean algebra and Python:
        AND binds tighter than OR. So ``A OR B AND C`` means
        ``A OR (B AND C)``. Parentheses are not supported; rewrite
        expressions in canonical ``(A AND B) OR (C AND D)`` form if a
        different grouping is needed.
        """
        expr = (expr or "").strip()
        if not expr:
            return False

        # OR first (lowest precedence) — matches standard boolean algebra.
        if " OR " in expr:
            return any(self._eval_expression(p.strip(), ctx) for p in expr.split(" OR "))
        # AND binds tighter than OR.
        if " AND " in expr:
            return all(self._eval_expression(p.strip(), ctx) for p in expr.split(" AND "))
        if expr.startswith("NOT "):
            return not self._eval_expression(expr[4:].strip(), ctx)

        # Time-range expressions, with strict (``<``) or inclusive (``<=``)
        # bounds independently on each side.
        if re.search(r"<=?\s*hour\s*<=?", expr):
            return self._eval_time_range(expr, ctx)

        for op, fn in (
            (">=", lambda a, b: a >= b),
            ("<=", lambda a, b: a <= b),
            ("!=", lambda a, b: a != b),
            (">", lambda a, b: a > b),
            ("<", lambda a, b: a < b),
            ("==", lambda a, b: a == b),
        ):
            if op in expr:
                left, right = expr.split(op, 1)
                key = left.strip()
                val_str = right.strip().strip("'\"")
                try:
                    compare = float(val_str.rstrip("h"))
                except (ValueError, TypeError):
                    if op in ("==", "!="):
                        return fn(str(ctx.get(key, "")), val_str)
                    self._warn_dsl(
                        expr,
                        f"right-hand side {val_str!r} is not numeric — "
                        "expression is treated as false. Use a dot for "
                        "decimals (e.g. 0.08, not 0,08).",
                    )
                    return False
                try:
                    actual = float(ctx.get(key, 0))
                except (ValueError, TypeError):
                    if op in ("==", "!="):
                        return fn(str(ctx.get(key, "")), val_str)
                    self._warn_dsl(
                        expr,
                        f"left-hand side {key!r} resolved to a non-numeric value — "
                        "check the sensor mapping for that key.",
                    )
                    return False
                return fn(actual, compare)

        # Bare context variable — treated as truthiness.
        if expr not in ctx:
            self._warn_dsl(
                expr,
                f"unknown context variable {expr!r} — expression is treated as false. "
                "See docs/rules-dsl.md for the list of built-in variables, or declare "
                "your own in `sensors:`.",
            )
        return bool(ctx.get(expr, False))

    def _resolve_value(self, value: Any, context: Dict[str, Any]) -> Any:
        """Expand ``{{ key }}`` placeholders in rule values."""
        if not isinstance(value, str) or "{{" not in value:
            return value
        rendered = re.sub(
            r"\{\{\s*(\w+)\s*\}\}",
            lambda m: str(context.get(m.group(1), m.group(0))),
            value,
        )
        try:
            return int(float(rendered))
        except (ValueError, TypeError):
            try:
                return float(rendered)
            except (ValueError, TypeError):
                return rendered

    async def _apply_smooth_transition(
        self,
        device_name: str,
        fp: str,
        dp: str,
        st_config: Dict[str, Any],
    ) -> None:
        """Write optional SmoothTransition helper data points if present."""
        if not hasattr(self.sgr, "write_if_exists"):
            return
        for sub_dp, raw in (
            (f"{dp}.SmoothTransition_Window", st_config.get("window", 0)),
            (f"{dp}.SmoothTransition_Delay", st_config.get("delay", 0)),
            (f"{dp}.SmoothTransition_Duration", st_config.get("duration", 0)),
        ):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            await self.sgr.write_if_exists(device_name, fp, sub_dp, value)

    def _warn_dsl(self, expr: str, detail: str) -> None:
        """Log a DSL diagnostic once per unique expression."""
        if expr in self._dsl_warned:
            return
        self._dsl_warned.add(expr)
        logger.warning("DSL %r: %s", expr, detail)

    @staticmethod
    def _eval_time_range(expr: str, ctx: Dict[str, Any]) -> bool:
        """Evaluate time-range expressions like ``10h < hour < 16h``.

        Honors ``<`` (strict) and ``<=`` (inclusive) independently on each
        side: ``10h <= hour < 16h`` is inclusive on the low end, exclusive
        on the high end.
        """
        m = re.match(r"(\d+)h?\s*(<=?)\s*hour\s*(<=?)\s*(\d+)h?", expr)
        if not m:
            return False
        low = int(m.group(1))
        low_op = m.group(2)
        high_op = m.group(3)
        high = int(m.group(4))
        hour = int(ctx.get("hour", 0))
        low_ok = hour >= low if low_op == "<=" else hour > low
        high_ok = hour <= high if high_op == "<=" else hour < high
        return low_ok and high_ok
