# SmartGridReady add-on

Universal energy-device orchestration via the Swiss SmartGridReady
standard.

This document is shown by Home Assistant in the **Documentation** tab
of the add-on page. The fuller, illustrated documentation lives in
the [GitHub repository](https://github.com/chrohrbach/ha-smartgridready).

## What it does

The add-on connects to any device labelled with a SmartGridReady EID
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

## Where to find EID profiles

Public catalogue:

- Browser, filterable: <https://library.smartgridready.ch/Device>
- Raw XML, versioned: <https://github.com/SmartGridready>

The EID XML is downloaded and cached on first use, so subsequent
restarts are fully offline.

## Project status

**Early — not yet validated against physical hardware in this
standalone packaging.** The underlying rules engine and SGr device
wrapper are battle-tested inside the casasmooth platform; this
add-on is a freshly extracted, re-packaged release.

## Credits

Open-sourced by [teleia](https://www.teleia.ch) — the engine is
extracted from [casasmooth](https://www.casasmooth.com), which has
natively integrated SmartGridReady since early 2026.
