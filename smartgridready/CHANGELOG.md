# Changelog

## Unreleased

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
