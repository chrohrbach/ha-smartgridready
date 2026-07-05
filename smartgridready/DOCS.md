# SmartGridready add-on

Universal energy-device orchestration via the Swiss SmartGridready
standard.

Important: this add-on is **not SmartGridready-certified**. It uses the
SmartGridready specifications and Python commhandler, but any formal
conformity or EMS certification would require an explicit certification
process with SmartGridready / the relevant lab.

This document is shown by Home Assistant in the **Documentation** tab
of the add-on page. The fuller, illustrated documentation lives in
the [GitHub repository](https://github.com/chrohrbach/ha-smartgridready)
— see the **Project status** section below before you rely on this for
anything critical.

## What it does

The add-on connects to any device labelled with a SmartGridready EID
profile (heat pumps, EV chargers, energy meters, PV inverters,
batteries), exposes their data points as native Home Assistant
entities via MQTT discovery, and runs a rules engine on a fixed
cadence that drives the writable data points based on real-time
context — spot price, PV surplus, presence, time-of-use, CO₂
intensity, and more.

A built-in safety layer covers V2H / V2G discharge (minimum SOC,
allow window, daily cycle cap, optional grid-operator agreement).

The condition language is a small, safe DSL — **no `eval()`** — so a
malformed configuration cannot execute arbitrary code.

## First start

The very first start of the add-on writes a commented starter file at
`/addon_config/config.yaml` (or wherever the `config_path` option
points). Edit it with the File Editor add-on, then restart this
add-on.

## Add-on options

| Option                | Default                          | Description                                |
|-----------------------|----------------------------------|--------------------------------------------|
| `config_path`         | `/addon_config/config.yaml`      | location of the user YAML configuration    |
| `evaluation_interval` | `300`                            | seconds between rule evaluations           |
| `log_level`           | `info`                           | `debug` / `info` / `warning` / `error`     |
| `mqtt_discovery`      | `true`                           | publish HA-discoverable MQTT entities      |
| `mqtt_prefix`         | `smartgridready`                 | topic + unique-id prefix                   |
| `share_path`          | `/share/smartgridready`          | where audit log and EID cache live         |
| `timezone`            | `Europe/Zurich`                  | local zone for hour / DST                  |
| `align_to_quarter`    | `false`                          | align first tick to HH:00/15/30/45         |
| `sg_ready_lock_cap_minutes` | `120`                      | BWP SG-Ready Mode-1 daily cap (0 = off)    |
| `latitude`            | *(unset)*                        | override home latitude for the self-computed PV forecast; falls back to HA's own location |
| `longitude`           | *(unset)*                        | override home longitude for the self-computed PV forecast; falls back to HA's own location |

## User configuration

The YAML configuration file declares devices, rules, sensor mappings,
and optional vehicles. See the
[full reference](https://github.com/chrohrbach/ha-smartgridready/blob/main/docs/configuration.md),
the [rules DSL](https://github.com/chrohrbach/ha-smartgridready/blob/main/docs/rules-dsl.md),
and the [scope-and-gaps document](https://github.com/chrohrbach/ha-smartgridready/blob/main/docs/scope-and-gaps.md)
for what the add-on covers vs what it does not.

A complete working example ships at
[`examples/config.yaml`](https://github.com/chrohrbach/ha-smartgridready/blob/main/examples/config.yaml).

Highlights worth knowing before you configure it:

- devices may declare optional `evse_safety` watchdog settings
  (`safe_current`, `max_receive_time_sec`) for compatible wallbox EIDs
- rules may declare optional `smooth_transition` helper writes
- `value:` supports simple `{{ key }}` placeholders resolved from the
  live rule context
- `==` / `!=` also work with quoted string literals in `when:`
- `pv_arrays:` lets the add-on compute its own PV forecast from
  Open-Meteo (nameplate `kwp`/`tilt`/`azimuth`) when no Forecast.Solar
  (or similar) integration is available
- `virtual_devices:` lets `rules:` target plain HA entities (climate /
  switch / water_heater / number) using the same SG-Ready state
  vocabulary as real SGr devices — a Home Assistant orchestration
  layer, **not** native SmartGridready communication
- `optimizer:` adds an opt-in predictive-dispatch MILP (falls back to
  a greedy heuristic without `scipy`) that schedules devices + battery
  + grid over a 24h horizon; the schedule is exposed as context
  variables consumed by a normal `rules:` entry

> Important: `virtual_devices` reuse the same rules engine for non-SGr
> hardware, but this is **not SmartGridready-native behaviour**.

## Where to find EID profiles

Public catalogue:

- Browser, filterable: <https://library.smartgridready.ch/Device>
- Raw XML, versioned: <https://github.com/SmartGridready>

The EID XML is downloaded and cached on first use, so subsequent
restarts are fully offline.

## Project status

**A foundation to build on, not a finished/field-proven product.** The
rules DSL, hysteresis, V2H safety gating, and predictive optimizer are
thoroughly unit-tested (100+ tests), but every test runs against
mocked HA/SGr clients — nobody has run this standalone add-on against
real hardware for an extended period yet. Some pieces (grid-CO₂
resolution, virtual-device dispatch, the MILP optimizer) were adapted
from patterns implemented in casasmooth; the rest is an independent
implementation written for this package. Bug reports and hardware
feedback on [GitHub](https://github.com/chrohrbach/ha-smartgridready/issues)
are very welcome — that's the point of releasing it as open source.

## Credits

Open-sourced by [teleia](https://www.teleia.ch). This add-on's design
and several of its algorithms draw on patterns implemented in
[casasmooth](https://www.casasmooth.com), which has its own,
separately-maintained SmartGridready integration since early 2026.
