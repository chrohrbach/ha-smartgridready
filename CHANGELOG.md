# Changelog

All notable changes to the SmartGridReady Home Assistant add-on are
documented here. The format is based on [Keep a Changelog](https://keepachangelog.com)
and this project adheres to [Semantic Versioning](https://semver.org).

## [Unreleased]

### Added
- **EVSE watchdog configuration**: devices may now declare
  `evse_safety.safe_current` and `evse_safety.max_receive_time_sec`.
  When the EID exposes the optional helper data points, the add-on
  writes them once at connect time.
- **Optional data-point helpers** in the SGr service:
  `write_if_exists()` for best-effort writes to optional EID points and
  `read_profile()` to fetch all values of a functional profile in one call.
- **SmoothTransition support** on rules: optional
  `smooth_transition.window|delay|duration` writes
  `*.SmoothTransition_*` helper data points before the main command.
- **Templated rule values**: `value:` may reference the live context
  via `{{ key }}` placeholders.
- **String equality in the DSL**: `==` and `!=` now support quoted
  string literals such as `grid_signal_type == 'sg_ready'`.

### Fixed
- **EID resolver**: auto-append `.xml` to the SGr library URL — the
  `/prodx/<name>` endpoint returns 404 without it. Users may now write
  the EID with or without the suffix in their YAML.
- **DSL precedence**: `AND` now binds tighter than `OR` (standard boolean
  algebra and Python semantics). Previously `A OR B AND C` was evaluated
  as `(A OR B) AND C`, which inverted the intended grouping.
- **Time-range inclusive bounds**: `10h <= hour <= 16h` now actually
  includes the bounds; previously the `<=` was silently downgraded to `<`.
- **V2H daily cycle counter (L2)**: was incremented on every cycle the
  rule wrote a negative target, so `max_cycles_per_day: 1` blocked the
  vehicle five minutes into the first discharge session. Now only
  counts transitions from non-negative to negative — one continuous
  discharge counts as one cycle, as the option name implies.
- **DSL silent failures (L3)**: non-numeric RHS (e.g. typed comma
  instead of dot in `0,08`) and references to unknown context
  variables now emit a one-shot warning at INFO level instead of
  silently evaluating to `false`. Helps catch config typos before
  they cause inexplicable rule misses.
- **Local time (L4)**: every wall-clock context variable (`hour`,
  `weekday`, `is_peak`, `is_offpeak`, `is_solar_window`,
  `allow_window`, V2H day key) now uses the configured time zone
  (default `Europe/Zurich`) via `zoneinfo`. Internal deltas
  (hysteresis, audit timestamps) use UTC.

### Added
- **L1 — SG-Ready Mode-1 cap**: utility-lock (`HP_LOCKED`) is now
  bounded by the BWP 1.1 spec of 2 h per 24 h. A persistent ledger
  (`sg_ready_lock.json` next to the audit log) tracks lock periods
  across restarts; once the cap is reached the engine downgrades the
  command back to `HP_NORMAL` automatically. Cap configurable via
  add-on option `sg_ready_lock_cap_minutes` (default 120; 0 disables).
- **L5 — PCC headroom helpers**: new top-level user config field
  `grid_connection_limit_w` exposes `pcc_power_w`, `pcc_headroom_w`,
  `pcc_limit_w` and `pcc_overload` in the rule DSL so rules can
  back off loads when the home is near its connection cap.
- **L6 — DSO curtailment signal**: `binary_sensor.dso_curtailment_active`
  and `sensor.dso_curtailment_factor` are auto-detected, or the user
  may declare custom entities via `sensors.dso_curtailment_active` /
  `sensors.dso_curtailment_factor`. Exposed as `dso_curtailment_active`
  / `dso_curtailment_factor` in the rule DSL.
- **L7 — Battery helpers**: `battery_full`, `battery_low`, plus
  `battery_available_kwh` and `battery_room_kwh` when the new
  top-level `battery_capacity_kwh` field is set.
- **L8 — Quarter-hour alignment**: new add-on option
  `align_to_quarter` delays the first tick so cadence sits on
  HH:00 / 15 / 30 / 45, matching the 15-minute tariff slices used
  by most Swiss DSOs. `minute_in_quarter` context variable is
  always available.
- **L9 — Tariff horizon helpers**: when the spot-price entity exposes
  a `forecast` / `raw_today` / `raw_tomorrow` attribute (Nordpool,
  Tibber, Awattar, SGr `DynamicTariff_*`), the engine computes
  `tariff_next_3h_min` / `_max` / `_avg` and
  `tariff_in_lowest_quartile_today`.
- **EID cache TTL** (30 days) plus stale-cache fallback when the SGr
  library is unreachable — manufacturer corrections flow in
  automatically and an offline add-on still boots.
- **Hysteresis persistence** to `hysteresis.json` — `min_interval`
  survives add-on restarts. Without this, a reboot reset the counter
  and the heat-pump compressor could be commanded back-to-back within
  seconds. Old entries (> 30 days) are pruned at load.
- **DynamicParameter pass-through** on `SGrService.read()`. The new
  optional `parameters: dict` is forwarded to the SDK's
  `get_value_async`, unlocking profiles like `DynamicTariff` (needs a
  date) and multi-channel meters.
- **LevelOfOperation exposure** in `describe()` / `to_dict()`. Both
  device-level and per-functional-profile level are surfaced (values
  `m`, `1`–`6`, or compound notation like `4m`) so callers know what
  control depth a profile actually supports.
- **`docs/scope-and-gaps.md`** — explicit, sourced mapping of which
  SmartGridReady concepts the add-on covers and which it deliberately
  leaves out (e.g. characteristic-curve writes for L3/L5, joint MPC
  optimisation, native DSO endpoint).

### Changed
- Direction and data-type values in `describe()` output now use the
  enum's `.value` (e.g. `"R"`, `"RW"`) instead of the verbose Python
  `repr` (`"DataDirectionProduct.R"`).
- Functional-profile descriptions in `to_dict()` now nest data points
  under a `data_points` key (was previously merged with FP metadata).
- Hysteresis timestamps are stored as aware UTC ISO strings. Pre-0.2
  naive timestamps are interpreted as UTC on load — a one-off
  hysteresis miss after upgrade is possible, which is acceptable
  given the protection still holds for any subsequent cycle.

## [0.1.0] — 2026-05-17

### Added
- First public release.
- SGr device wrapper based on `sgr-commhandler` (Modbus TCP, REST).
- Automatic download and caching of EID XML profiles from the
  SmartGridReady product library.
- Rules engine with priority-ordered conditions, hysteresis, and a
  safe expression DSL (no `eval()`).
- Context variables for spot price, PV surplus, battery SOC,
  time-of-use, presence, PV forecast, grid CO₂ intensity.
- V2H/V2G safety layer: per-vehicle min SOC, allow window, daily
  cycle cap, V2G grid-agreement opt-in.
- MQTT discovery: read-only data points exposed as HA `sensor`
  entities, writable as `number` entities with command topic.
- Ingress web UI: device list, last evaluation, audit trail, raw
  config viewer.
- Audit log (24 h rolling) persisted to
  `/share/smartgridready/audit.json`.
