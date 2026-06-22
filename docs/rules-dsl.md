# Rules DSL

The condition language used in `when:` clauses is intentionally small,
safe, and **does not** use Python's `eval()`. Expressions are parsed
with simple string operations, so a malformed user config can't
execute arbitrary code.

## Building blocks

### Comparisons

```
spot_price < 0.10
surplus_pv > 3000
battery_soc >= 50
temperature_outdoor <= -5
hour == 14
grid_signal_type == 'sg_ready'
```

Supported operators: `<`, `<=`, `>`, `>=`, `==`, `!=`.

The left-hand side must be a **context variable name**. The right-hand
side may be:

- a numeric literal (a trailing `h` is tolerated for hour comparisons:
  `hour > 10h`)
- or, for `==` / `!=`, a quoted string literal such as
  `'sg_ready'` or `"load_reduction"`

If either side can't be parsed, the expression evaluates to `false`.

### Boolean context variables

When you write a context variable name alone, it evaluates to the
truthiness of that variable.

```
has_surplus
is_peak
is_offpeak
is_weekend
v2h_available
```

### Logical operators

```
surplus_pv > 3000 AND spot_price < 0.10
is_offpeak OR is_weekend
NOT is_peak
```

- `AND` and `OR` are space-separated, **uppercase**.
- `NOT` prefixes the inner expression.
- Operator precedence follows standard boolean algebra: `AND` binds
  tighter than `OR`. So `A OR B AND C` means `A OR (B AND C)`.
- Parentheses are not supported. If you need explicit grouping,
  split the logic into multiple conditions or precompute a helper
  boolean elsewhere.

### Time-range shortcut

```
10h < hour < 16h
```

Strictly less-than at both ends. Useful for solar windows or peak
hours.

## Built-in context variables

The rules engine populates these on every cycle. You can compare any
of them, combine them with `AND` / `OR` / `NOT`, or use them on their
own.

### Time

All time values follow the add-on's configured time zone
(`timezone` option, default `Europe/Zurich`) — DST shifts handled
automatically.

| Name           | Type    | Meaning                                |
|----------------|---------|----------------------------------------|
| `hour`         | int     | current hour (0–23) in local TZ        |
| `minute`       | int     | current minute (0–59)                  |
| `minute_in_quarter` | int | minute within the current quarter (0–14) |
| `weekday`      | int     | 0 = Monday … 6 = Sunday                |
| `is_weekend`   | bool    | Saturday or Sunday                     |
| `is_peak`      | bool    | 07–09 h or 17–20 h                     |
| `is_offpeak`   | bool    | before 06 h or 22 h and later          |
| `is_solar_window` | bool | 10 h – 16 h                            |

### Energy (only meaningful if the matching `sensors:` key is set)

| Name              | Type    | Meaning                                |
|-------------------|---------|----------------------------------------|
| `spot_price`      | float   | from `sensors.spot_price`              |
| `pv_power`        | float   | from `sensors.pv_power`                |
| `house_consumption` | float | from `sensors.house_consumption`       |
| `battery_soc`     | float   | from `sensors.battery_soc`             |
| `grid_export`     | float   | from `sensors.grid_export`             |
| `grid_import`     | float   | from `sensors.grid_import`             |
| `temperature_outdoor` | float | from `sensors.temperature_outdoor`    |

### Computed

| Name               | Type   | Definition                                |
|--------------------|--------|-------------------------------------------|
| `surplus_pv`       | float  | `max(0, pv_power − house_consumption)`    |
| `has_surplus`      | bool   | `surplus_pv > 500`                        |
| `home_deficit_w`   | float  | `max(0, house_consumption − pv_power)`    |
| `expect_high_pv_tomorrow` | bool | `pv_forecast_kwh > 20`                |
| `expect_low_pv_tomorrow`  | bool | `0 < pv_forecast_kwh < 8`             |
| `grid_co2_kg_per_kwh` | float | from Electricity Maps / CO2 Signal sensors if installed, else the Swiss default (0.128) |

### Point of common coupling (only meaningful with `grid_connection_limit_w` set)

| Name           | Type  | Meaning                                     |
|----------------|-------|---------------------------------------------|
| `pcc_power_w`  | float | signed `grid_import − grid_export`          |
| `pcc_limit_w`  | float | the configured `grid_connection_limit_w`    |
| `pcc_headroom_w` | float | `max(0, pcc_limit_w − grid_import)`       |
| `pcc_overload` | bool  | `grid_import > pcc_limit_w`                 |

### DSO curtailment (auto-detected or via `sensors.dso_curtailment_*`)

| Name                     | Type  | Meaning                              |
|--------------------------|-------|--------------------------------------|
| `dso_curtailment_active` | bool  | network operator is requesting curtailment |
| `dso_curtailment_factor` | float | 0–100 % factor pushed by the DSO     |

The engine auto-detects the canonical entity_ids
`binary_sensor.dso_curtailment_active` and
`sensor.dso_curtailment_factor`; pointing custom entity_ids at the
corresponding `sensors:` keys overrides the auto-detection.

### Battery helpers

| Name                  | Type  | Meaning                                |
|-----------------------|-------|----------------------------------------|
| `battery_full`        | bool  | `battery_soc > 95`                     |
| `battery_low`         | bool  | `0 < battery_soc < 20`                 |
| `battery_capacity_kwh` | float | from the top-level config field       |
| `battery_available_kwh` | float | `capacity × soc / 100`               |
| `battery_room_kwh`    | float | `capacity × (100 − soc) / 100`         |

### Tariff horizon (only meaningful if `sensors.spot_price` exposes a `forecast`/`raw_today`/`raw_tomorrow` attribute)

| Name                              | Type  | Meaning                            |
|-----------------------------------|-------|------------------------------------|
| `tariff_next_3h_min`              | float | minimum price in the next 3 h      |
| `tariff_next_3h_max`              | float | maximum price in the next 3 h      |
| `tariff_next_3h_avg`              | float | average price in the next 3 h      |
| `tariff_in_lowest_quartile_today` | bool  | current price ≤ 25th percentile of today's known prices |

### Presence (only if `sensors.away_mode` is set)

| Name           | Type | Meaning                              |
|----------------|------|--------------------------------------|
| `away_mode`    | bool | the `away_mode` input_boolean is on  |
| `at_home`      | bool | inverse of `away_mode`               |
| `away_effective` | bool | currently equivalent to `away_mode` |

### Vehicles (per declared vehicle)

For a vehicle named `Kia EV6`, the slug is `kia_ev6`. The engine
exposes:

```
ev_kia_ev6_soc                    # %
ev_kia_ev6_plugged                # bool
ev_kia_ev6_charging_power         # W
ev_kia_ev6_v2h_available          # bool
ev_kia_ev6_v2h_reserve_kwh        # float
```

Convenience aliases for the **first** vehicle (no slug):

```
ev_soc
ev_plugged
ev_charging_power
ev_v2h_available
ev_v2h_reserve_kwh
```

Aggregate keys:

```
v2h_available           # bool — any vehicle V2H-ready
v2h_reserve_kwh_total   # float — sum across all available vehicles
```

## Patterns

### Tier the heat pump by price

```yaml
- device: "Heat Pump"
  profile: "SG-ReadyStates"
  data_point: "SGReadyOpModeCmd"
  min_interval: 15
  conditions:
    - when: "has_surplus AND spot_price < 0.08"
      value: HP_FORCED
    - when: "spot_price < 0.12"
      value: HP_NORMAL
    - when: "spot_price > 0.25"
      value: HP_LOCKED
    - when: "is_peak AND NOT has_surplus"
      value: HP_LOCKED
    - default:
      value: HP_NORMAL
```

> Values are the enum members declared in the SG-Ready functional
> profile: `HP_LOCKED`, `HP_NORMAL`, `HP_INTENSIFIED`, `HP_FORCED`.
> Write the **command** data point `SGReadyOpModeCmd`, not the
> read-only feedback `SGReadyState`.

### Use a templated value from the live context

```yaml
- device: "Battery"
  profile: "BatteryStorageCtrl"
  data_point: "TargetSoc"
  min_interval: 10
  conditions:
    - when: "battery_soc < 40"
      value: "{{ battery_soc }}"
```

`value:` supports simple `{{ key }}` placeholders. The placeholder is
resolved from the current rule context before the write. Numeric
results are cast back to `int` / `float` when possible.

### Hot-water midday boost

```yaml
- device: "Heat Pump"
  profile: "DomHotWaterCtrl"
  data_point: "DomHotWaterTempSetpoint"
  min_interval: 30
  conditions:
    - when: "has_surplus AND is_solar_window"
      value: 65
    - when: "is_offpeak AND spot_price < 0.10"
      value: 60
    - default:
      value: 50
```

### Pre-heat when tomorrow's PV will be poor

```yaml
- device: "Heat Pump"
  profile: "SG-ReadyStates"
  data_point: "SGReadyOpModeCmd"
  min_interval: 30
  conditions:
    - when: "expect_low_pv_tomorrow AND has_surplus"
      value: HP_FORCED
```

### Cap the EV during peaks

```yaml
- device: "Wallbox"
  profile: "EMS_Current_Limit"
  data_point: "EMSCurrentLimit"
  min_interval: 5
  smooth_transition:
    window: 0
    delay: 30
    duration: 0
  conditions:
    - when: "surplus_pv > 3000"
      value: 16
    - when: "is_offpeak AND spot_price < 0.10"
      value: 16
    - when: "is_peak"
      value: 6
    - default:
      value: 8
```

If the wallbox EID declares the optional helper data points
`EMSCurrentLimit.SmoothTransition_Window`,
`EMSCurrentLimit.SmoothTransition_Delay`, and
`EMSCurrentLimit.SmoothTransition_Duration`, the engine writes them
before the main command. If they are absent, they are skipped silently.

### Compare a string-valued context variable

```yaml
- device: "Heat Pump"
  profile: "SG-ReadyStates"
  data_point: "SGReadyOpModeCmd"
  min_interval: 5
  conditions:
    - when: "grid_signal_type == 'sg_ready'"
      value: HP_NORMAL
```

String comparison is supported only for `==` and `!=`.

### V2H discharge into the home during the evening peak

```yaml
- device: "Wallbox"
  profile: "EMS_Current_Limit"
  data_point: "EMSCurrentLimit"
  min_interval: 5
  conditions:
    - when: "is_peak AND v2h_available AND home_deficit_w > 1500"
      value: -16
    - when: "is_peak AND v2h_available AND home_deficit_w > 500"
      value: -10
```

(Requires a `vehicles:` block with `v2h.enabled: true` and a wallbox
EID that exposes bidirectional current limits.)
