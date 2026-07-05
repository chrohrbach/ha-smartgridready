# Changelog

## 0.2.0 — 2026-07-05

- **Predictive-dispatch optimizer (`optimizer:`, opt-in)**: MILP
  (`scipy.optimize.milp`, greedy fallback without scipy) jointly
  schedules devices + battery + grid over 24h to minimise cost.
  Exposed as `optimizer_<device>_power_w` / `_current_a` / `_on`
  context variables — a normal `rules:` entry applies it, so
  hysteresis/audit/dispatch stay unchanged. `scipy` is intentionally
  not in `requirements.txt` (armv7 wheel availability).
- **Virtual devices (`virtual_devices:`)**: target plain HA entities
  (`climate`/`switch`/`water_heater`/`number`) from `rules:` using the
  same SG-Ready state literals as real SGr devices. Four types:
  `climate_proxy`, `switch_proxy`, `boiler_proxy`, `number_proxy`. A
  Home Assistant orchestration layer, not native SmartGridready
  communication.
- **Self-computed PV forecast (`pv_arrays:`)**: fetches solar
  irradiance from Open-Meteo and estimates production when no
  Forecast.Solar (or similar) sensor is mapped. New add-on options
  `latitude`/`longitude` (fall back to HA's own location).
- SG-Ready Mode-1 (`HP_LOCKED`) capped to BWP-mandated 2 h / 24 h.
- V2H cycle counter now counts *transitions* into discharge, not raw
  negative writes — `max_cycles_per_day` finally behaves as named.
- DSL warns on typos and unknown context variables instead of silent
  `false` evaluation.
- All wall-clock context uses the configured `timezone` (default
  `Europe/Zurich`) with full DST handling.
- New context variables: `pcc_power_w`, `pcc_headroom_w`,
  `pcc_overload`, `dso_curtailment_active`, `dso_curtailment_factor`,
  `battery_full`, `battery_low`, `battery_room_kwh`,
  `battery_available_kwh`, `minute_in_quarter`,
  `tariff_next_3h_min` / `_max` / `_avg`,
  `tariff_in_lowest_quartile_today`.
- New add-on options: `timezone`, `align_to_quarter`,
  `sg_ready_lock_cap_minutes`.
- New user-config fields: `grid_connection_limit_w`,
  `battery_capacity_kwh`, `sensors.dso_curtailment_active`,
  `sensors.dso_curtailment_factor`.
- New documentation: `docs/scope-and-gaps.md` mapping SGr concepts
  to add-on coverage and limitations.

## 0.1.0 — 2026-05-17

- First public release.
- SGr device wrapper based on `sgr-commhandler`.
- EID XML auto-download from the SGr product library.
- Rules engine with priority conditions, hysteresis, safe DSL.
- V2H / V2G safety layer (min SOC, window, daily cycle cap).
- MQTT discovery for HA entities (sensors + numbers).
- Ingress UI: devices, rules, audit, raw config viewer.
- 24 h rolling audit log persisted to `/share/smartgridready/audit.json`.
