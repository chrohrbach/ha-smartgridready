# Configuration reference

The user-facing configuration lives in a single YAML file. Its path is
controlled by the add-on option `config_path` (default:
`/addon_config/config.yaml`).

The file has the following top-level sections:

```yaml
sensors: { … }                # mapping of context variable → HA entity_id
enable_toggle: …              # optional kill switch (input_boolean)
grid_connection_limit_w: …    # optional PCC limit in watts (L5 helpers)
battery_capacity_kwh: …       # optional home-battery capacity (L7 helpers)
devices: [ … ]                # SGr devices to connect to
rules: [ … ]                  # optimisation rules
vehicles: [ … ]                # V2H / V2G vehicle declarations (optional)
pv_arrays: [ … ]               # self-computed PV forecast arrays (optional)
virtual_devices: [ … ]         # non-SGr devices piloted via HA services (optional)
optimizer: { … }               # opt-in predictive-dispatch MILP (optional)
```

A complete working example is in
[`examples/config.yaml`](../examples/config.yaml).

---

## `sensors:`

Tells the rules engine where to find runtime values inside Home
Assistant. Every key is **optional**. A missing key is treated as
`0.0` (numeric) or `false` (boolean) at evaluation time, so rules that
rely on it simply do not match.

| Key                              | Type     | What it should point to                          |
|----------------------------------|----------|--------------------------------------------------|
| `spot_price`                     | numeric  | electricity spot price, CHF/kWh (or your currency) |
| `pv_power`                       | numeric  | total PV production, W                           |
| `house_consumption`              | numeric  | total house consumption, W                       |
| `battery_soc`                    | numeric  | home battery state of charge, %                  |
| `grid_export`                    | numeric  | power exported to the grid, W                    |
| `grid_import`                    | numeric  | power imported from the grid, W                  |
| `temperature_outdoor`            | numeric  | outdoor temperature, °C                          |
| `pv_forecast_kwh`                | numeric  | expected PV production tomorrow, kWh             |
| `pv_forecast_today_remaining_kwh`| numeric  | remaining PV today, kWh                          |
| `pv_current_hour_kwh`            | numeric  | PV expected this hour, kWh                       |
| `pv_forecast_today_kwh`          | numeric  | PV expected today, kWh                           |
| `away_mode`                      | boolean  | `input_boolean` reflecting whether home is empty |
| `dso_curtailment_active`         | boolean  | distribution-network curtailment is in force     |
| `dso_curtailment_factor`         | numeric  | curtailment factor, 0–100 %                      |

Any additional `key: entity_id` pair you add is exposed under the
**same key name** inside the rules DSL. Useful when you have a custom
sensor (e.g. `nuclear_grid_share: sensor.nuclear_pct`).

The DSO keys are also auto-detected when the canonical entity IDs
`binary_sensor.dso_curtailment_active` and
`sensor.dso_curtailment_factor` exist in HA — no need to declare them
explicitly in that case.

---

## `enable_toggle:`

Optional. Entity ID of an `input_boolean` that pauses the rules engine
when off. Useful during maintenance or when troubleshooting an SGr
device. The rest of the add-on (MQTT publishing, ingress UI) keeps
working.

```yaml
enable_toggle: input_boolean.smartgridready_enabled
```

Create the helper in HA: **Settings → Devices & Services → Helpers →
Toggle**.

---

## `grid_connection_limit_w:`

Optional. Watts that the home's point of common coupling is rated
for (typical Swiss single-family: 25 000 W on a 3×35 A breaker).
When set, three context variables become useful in rules:

- `pcc_power_w` (signed, positive = import)
- `pcc_headroom_w` (room left before the cap)
- `pcc_overload` (boolean, current import exceeds the cap)

The engine does **not** automatically distribute headroom across
several flexible loads — rules opt in to caring about the PCC by
conditioning on these variables. See
[scope-and-gaps.md §3.1](scope-and-gaps.md#31-aggregate-power-cap-at-the-pcc--level-4-in-spirit-partially-enforced)
for the rationale.

```yaml
grid_connection_limit_w: 25000
```

---

## `battery_capacity_kwh:`

Optional. Total kWh of the home battery, used by the L7 helpers
`battery_available_kwh` and `battery_room_kwh`. Without it those
two helpers stay at 0; `battery_full` and `battery_low` work
regardless (they only need `sensors.battery_soc`).

```yaml
battery_capacity_kwh: 10
```

---

## `devices:`

Each device is identified by its **EID** — the catalogue identifier
from the SGr product library (https://library.smartgridready.ch).

```yaml
devices:
  - name: "Heat Pump"                        # arbitrary, must be unique
    eid: SGr_04_0015_xxxx_StiebelEltron_HeatPump_V1.0.0
    enabled: true                             # optional, default true
    properties:                               # device-specific connection
      ip: 192.168.1.50
      port: 502
      slave_id: 1
  - name: "Wallbox"
    eid: SGr_04_mmmm_dddd_KEBA_KeContact_P30_V0.1
    properties:
      ip: 192.168.1.60
      port: 502
      slave_id: 1
    evse_safety:
      safe_current: 6
      max_receive_time_sec: 120
```

Always cross-check the EID identifier against the public catalogue
(<https://library.smartgridready.ch/Device>) before relying on it — a
fresh release of a profile may bump the version suffix (`_V1.0.0` →
`_V1.1.0`, etc.).

### Properties by transport

| Transport     | Required properties                          |
|---------------|----------------------------------------------|
| Modbus TCP    | `ip`, `port` (default 502), `slave_id`       |
| Modbus RTU    | `serial_port`, `baud`, `slave_id`            |
| REST          | `base_uri` (and `username`/`password`/`token` if the profile demands it) |

The exact set of properties is **defined by the EID** — open the XML
file once to see what is required; the add-on validates at connect
time.

The EID XML is downloaded from the SGr library on first use and cached
to `/share/smartgridready/cache/<eid>.xml`. Subsequent restarts are
fully offline. To force a refresh, delete the cached file.

### `evse_safety:`

Optional, per device. Only useful for EV chargers whose EID exposes the
optional watchdog helper data points `SafeCurrent` and
`MaxReceiveTimeSec`.

- `safe_current`: fallback current in amps when the EMS stops talking
- `max_receive_time_sec`: watchdog timeout in seconds

These values are written once at connect time and skipped silently when
the EID does not declare the corresponding points.

### `enabled: false`

Set on a per-device basis to keep the configuration in the file but
skip connecting at startup. Handy for staging or for a device that is
temporarily offline.

---

## `virtual_devices:`

Optional. Lets `rules:` target ordinary Home Assistant entities
(`climate`, `switch`, `water_heater`, `number`) using the **same
SG-Ready state literals** already used for real SGr devices
(`HP_LOCKED` / `HP_NORMAL` / `HP_INTENSIFIED` / `HP_FORCED`) — one rule
vocabulary covers both. Reference the virtual device's `name:` as a
rule's `device:` — no `eid` required, no SGr connection is attempted
for it.

> **Important:** this is a **Home Assistant orchestration layer**, not
> native SmartGridready communication. It is useful for hardware that
> has no digital SGr interface at all (a heat pump controlled only
> through its native HA climate entity, an EV charger or boiler behind
> a simple relay, an inverter limit exposed only as an HA `number`) but
> it is **not** evidence of SmartGridready conformity — see
> [scope-and-gaps.md §3.13](scope-and-gaps.md#313-virtual-devices--ha-proxy-layer).

Four types are supported:

```yaml
virtual_devices:
  # PAC without a digital SGr interface — offset from a base setpoint.
  - name: "Heat pump (no SGr interface)"
    type: climate_proxy
    climate_entities: [climate.living_room]   # one or more climate.* entities
    base_setpoint_default: 21.0                # °C, used when base_setpoint_entity is unset
    base_setpoint_entity: input_number.target_temp  # optional — live base instead of the default
    min_setpoint: 16.0
    max_setpoint: 24.0
    sg_ready_to_offset:                        # °C offset added to the base setpoint
      HP_LOCKED: -3.0
      HP_NORMAL: 0.0
      HP_INTENSIFIED: 1.5
      HP_FORCED: 3.0

  # EV charger / boiler behind a simple relay.
  - name: "Boiler (Shelly relay)"
    type: switch_proxy
    switch_entities: [switch.boiler]           # one or more switch.* entities
    sg_ready_to_switch:
      HP_LOCKED: off
      HP_NORMAL: off
      HP_INTENSIFIED: on
      HP_FORCED: on

  # Water heater with its own thermostat entity.
  - name: "Water heater"
    type: boiler_proxy
    water_heater_entity: water_heater.tank     # exactly one water_heater.* entity
    min_temperature: 45.0
    max_temperature: 65.0
    sg_ready_to_temperature:                   # °C target
      HP_LOCKED: 45
      HP_NORMAL: 50
      HP_INTENSIFIED: 55
      HP_FORCED: 65

  # Inverter export/charge limit or any numeric setpoint.
  - name: "Inverter charge power limit"
    type: number_proxy
    number_entities: [number.inverter_charge_power_limit]
    min_value: 0
    max_value: 5000
    sg_ready_to_value:
      HP_LOCKED: 0
      HP_NORMAL: 1500
      HP_INTENSIFIED: 3000
      HP_FORCED: 5000
```

Then reference it from `rules:` exactly like a real device:

```yaml
rules:
  - device: "Boiler (Shelly relay)"    # matches a virtual_devices[].name
    profile: "SG-ReadyStates"          # kept for consistency; ignored for virtual devices
    data_point: "SGReadyOpModeCmd"     # kept for consistency; ignored for virtual devices
    conditions:
      - when: "has_surplus"
        value: HP_FORCED
      - default:
        value: HP_NORMAL
```

Mapping keys are case-insensitive (`HP_LOCKED` and `hp_locked` are
equivalent) and matched against whatever the rule's `value:` resolves
to at evaluation time. A state with no entry in the mapping table is
skipped (logged as `virtual_device_error` in the audit trail) rather
than guessed at.

`smooth_transition:` and EVSE `evse_safety:` do not apply to virtual
devices — both are SGr EID sub-data-point concepts with no HA service
equivalent.

---

## `rules:`

A list of optimisation rules evaluated on every cycle (default every
**300 s**, configurable via the add-on option
`evaluation_interval`).

```yaml
rules:
  - device: "Heat Pump"             # must match a name from `devices:`
    profile: "SG-ReadyStates"       # functional profile from the EID
    data_point: "SGReadyOpModeCmd"  # writable command (not SGReadyState — that one is R/O feedback)
    min_interval: 15                # optional: minimum minutes between writes
    conditions:
      - when: "has_surplus AND spot_price < 0.08"
        value: HP_FORCED
      - default:
        value: HP_NORMAL
```

Optional helper:

```yaml
  - device: "Wallbox"
    profile: "EMS_Current_Limit"
    data_point: "EMSCurrentLimit"
    smooth_transition:
      window: 0
      delay: 30
      duration: 0
```

When present, the engine attempts best-effort writes to the optional
sub-data-points `*.SmoothTransition_Window`, `*.SmoothTransition_Delay`,
and `*.SmoothTransition_Duration` before the main command. Missing
sub-data-points are skipped silently.

> **Pay attention to direction.** In every SGr functional profile,
> each data point declares a direction: read-only (`R`), read-write
> (`RW`), or write-only (`W`). You can only target `RW` and `W` data
> points in a rule. A common mistake is to write to a `_State`
> feedback data point — it will be rejected at runtime. The
> writable counterpart is usually named `..._Cmd` or `..._Setpoint`.

Behaviour:

- **First match wins.** Conditions are evaluated top-to-bottom; the
  first one whose `when` expression is true is applied. The optional
  `default:` entry (anywhere, but conventionally at the end) is the
  fallback when nothing matched.
- **Template values.** `value:` may reference the current context with
  `{{ key }}` placeholders, e.g. `value: "{{ battery_soc }}"`.
- **Redundant writes are suppressed.** If the value is identical to
  the one written in the previous cycle, no I/O happens.
- **Hysteresis.** `min_interval` is the minimum number of minutes
  between two *different* values for the same rule. Useful for heat
  pumps (compressor protection) and any relay-driven device.
  Default: **15 minutes**.
- **Negative values trigger V2H/V2G safety** — see below.

See [rules-dsl.md](rules-dsl.md) for the full condition syntax.

---

## `vehicles:`

Required only if you want to write **negative** current/power values
to a charger (V2H = discharge to the house, V2G = export to the grid).

```yaml
vehicles:
  - name: "Kia EV6"
    soc_entity: sensor.kia_ev6_battery_level
    plugged_entity: binary_sensor.kia_ev6_charging_cable_connected
    charging_power_entity: sensor.kia_ev6_charging_power
    charger_device: "Wallbox"           # name from `devices:` that physically discharges
    battery_capacity_kwh: 77
    v2h:
      enabled: false                     # off by default — opt-in
      min_soc: 50                        # never discharge below 50 % SOC
      max_discharge_a: 16                # absolute amp cap (sent as negative)
      allow_window: "17h-22h"            # optional time window
      require_plugged: true
      max_cycles_per_day: 1
      requires_grd_agreement: false      # true for V2G (export to grid)
      grd_agreement_signed: false        # documented opt-in flag
```

The rules engine refuses to write any negative target unless:

1. A vehicle has `v2h.enabled: true`.
2. SOC is currently **above** `min_soc + 5 %` (built-in safety margin).
3. Cable is plugged in (unless `require_plugged: false`).
4. Current hour is inside `allow_window` (or the field is omitted).
5. The daily discharge counter is below `max_cycles_per_day`.
6. If `requires_grd_agreement: true`, then `grd_agreement_signed` must
   also be `true`. Failing this gate is intended: it documents that
   exporting to the grid requires a contract with the distribution
   network operator.

The magnitude of the negative value is clamped to `max_discharge_a`
no matter what the rule asks for.

---

## `pv_arrays:`

Optional. Only needed if you have **no** Forecast.Solar (or similar)
integration mapped under `sensors.pv_forecast_kwh`. When at least one
array is declared here, the add-on fetches solar irradiance from the
free [Open-Meteo](https://open-meteo.com) API every few hours and
estimates production itself — no external PV-forecast integration
required.

```yaml
pv_arrays:
  - name: "Roof south"
    kwp: 6.5              # nameplate peak power, kWp
    tilt: 30                # degrees from horizontal (optional)
    azimuth: 180             # 0=N, 90=E, 180=S, 270=W (optional)
    efficiency: 0.80         # system losses factor, default 0.80
    enabled: true            # optional, default true
```

`tilt`/`azimuth` are optional: without them the forecast uses plain
(untransposed) global horizontal irradiance — still a reasonable
estimate for a roughly south-facing residential roof, just less
accurate for steeply tilted or non-south-facing arrays.

The array does **not** need to correspond to an SGr-connected device —
it is independent of `devices:` and purely nameplate metadata for the
forecast calculation.

Home coordinates come from the add-on options `latitude`/`longitude`
when set, otherwise from HA's own `GET /api/config` (the location you
already configured during HA onboarding). The result fills
`pv_forecast_kwh` / `pv_forecast_today_kwh` / `pv_forecast_next_4h_kwh`
in the rule DSL, but **only** when `sensors.pv_forecast_kwh` is not
mapped or reads `0` — a real sensor mapping always takes priority.

---

## `optimizer:`

Optional, **disabled by default** (`enabled: false`). The `rules:`
engine (condition → value, evaluated every cycle) remains the primary
and only *required* optimisation mechanism — `optimizer:` adds a
second, opt-in layer: a genuine predictive-dispatch **MILP**
(mixed-integer linear program) that jointly schedules controllable
devices, the home battery, and the grid connection over a 24 h horizon
to minimise cost, instead of reacting condition-by-condition.

```yaml
optimizer:
  enabled: true
  devices:
    - name: "Wallbox"          # free-form label — not necessarily a devices[]/virtual_devices[] name
      min_power_w: 0
      max_power_w: 11000
      must_run_hours: 0         # 0 = no minimum energy requirement
      preferred_window: [22, 6] # optional, wraps midnight; omit for "any hour"
      priority: 50              # currently informational (tie-breaking is price-driven)
      switchable: false         # true = binary on/off unit commitment instead of continuous power
      voltage: 230              # used only to also expose a `_current_a` context variable
      phases: 1
  battery:
    capacity_kwh: 10            # defaults to the top-level battery_capacity_kwh if omitted
    max_charge_w: 5000
    max_discharge_w: 5000
    soc_min_pct: 10
    soc_max_pct: 95
    efficiency: 0.95            # round-trip efficiency
    cycle_cost_chf_kwh: 0.01    # small per-kWh throughput cost — discourages pointless cycling
  grid:
    pcc_import_w: 25000         # defaults to the top-level grid_connection_limit_w if omitted
    pcc_export_w: 12000
    export_price_chf_kwh: 0.08  # feed-in tariff
```

### How the schedule reaches a device

The optimizer **does not write anything itself**. It computes a
24 h `device name → [W per hour]` schedule (recomputed every 30
minutes from the current price forecast, PV forecast, and battery SoC)
and exposes it as new rules-DSL context variables:

| Variable | Meaning |
|---|---|
| `optimizer_enabled` | `true` when `optimizer.enabled: true` and at least one device is registered |
| `optimizer_<slug>_power_w` | this hour's scheduled power, in watts (`<slug>` = the device `name`, lower-cased/snake-cased) |
| `optimizer_<slug>_current_a` | the same value converted to amps via `power_w / (voltage · phases)` — a plain approximation, not 3-phase-accurate, useful for profiles like `EMS_Current_Limit` that expect amps |
| `optimizer_<slug>_on` | `true` when the scheduled power is > 0 |
| `optimizer_savings_chf` | estimated savings vs. a naive flat-price baseline, over the 24 h horizon |
| `optimizer_self_consumption_pct` | estimated PV self-consumption % under the computed schedule |

You still write a normal `rules:` entry (targeting a real SGr device or
a `virtual_devices:` entry) to actually apply it:

```yaml
rules:
  - device: "Wallbox"                       # a real devices[] or virtual_devices[] name
    profile: "EMS_Current_Limit"
    data_point: "EMSCurrentLimit"
    conditions:
      - default:
        value: "{{ optimizer_wallbox_current_a }}"
```

This keeps the existing rules engine as the single write path —
hysteresis, redundant-write skipping, the audit log, and the real/
virtual device dispatch all still apply, exactly as for any other rule.
The DSL's `{{ key }}` templating only substitutes — it does not do
arithmetic — so pick whichever context variable (`_power_w` or
`_current_a`) already matches the target data point's unit.

### Solver: MILP with a greedy fallback

The MILP solver (`scipy.optimize.milp`) is used when `scipy` is
installed; otherwise the optimizer transparently falls back to a
greedy heuristic (cheapest hours first, respecting `must_run_hours`
and `preferred_window`) — still useful, just not provably optimal.

**`scipy` is deliberately not part of this add-on's `requirements.txt`.**
This add-on ships for `amd64`, `aarch64` **and `armv7`** — scipy/numpy
wheels aren't reliably prebuilt for 32-bit ARM on every Python version,
and a from-source fallback (needing BLAS/LAPACK/gfortran on Alpine)
would be slow and could outright fail the image build. If you maintain
your own image build and want the real MILP solver, add `scipy>=1.11`
to `requirements.txt` yourself — the code already prefers it when
present.

---

## Add-on options (reminder)

The four settings below sit in the add-on **Configuration** tab in
Home Assistant, **not** in the user config file:

| Option                | Default                          | Description                                |
|-----------------------|----------------------------------|--------------------------------------------|
| `config_path`         | `/addon_config/config.yaml`      | user YAML configuration path               |
| `evaluation_interval` | `300`                            | seconds between rule evaluations           |
| `log_level`           | `info`                           | `debug` / `info` / `warning` / `error`     |
| `mqtt_discovery`      | `true`                           | publish HA-discoverable MQTT entities      |
| `mqtt_prefix`         | `smartgridready`                 | topic and unique-id prefix                 |
| `share_path`          | `/share/smartgridready`          | where audit log and EID cache live         |
| `timezone`            | `Europe/Zurich`                  | local zone for `hour` / `is_peak` / `allow_window` / DST |
| `align_to_quarter`    | `false`                          | delay first tick so cadence sits on HH:00 / 15 / 30 / 45 |
| `sg_ready_lock_cap_minutes` | `120`                      | BWP SG-Ready Mode-1 (HP_LOCKED) cap per 24 h; 0 disables |
| `latitude`            | *(unset)*                        | override home latitude for the `pv_arrays:` forecast; falls back to HA's own location |
| `longitude`           | *(unset)*                        | override home longitude for the `pv_arrays:` forecast; falls back to HA's own location |
