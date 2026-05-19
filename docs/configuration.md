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
vehicles: [ … ]               # V2H / V2G vehicle declarations (optional)
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
[scope-and-gaps.md §3.1](scope-and-gaps.md#31-aggregate-power-cap-at-the-pcc--level-4-in-spirit-but-not-enforced)
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

### `enabled: false`

Set on a per-device basis to keep the configuration in the file but
skip connecting at startup. Handy for staging or for a device that is
temporarily offline.

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
