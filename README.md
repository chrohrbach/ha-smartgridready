# Home Assistant SmartGridready Add-on

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Home Assistant Add-on](https://img.shields.io/badge/Home%20Assistant-Add--on-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io/)

Universal, manufacturer-neutral control of energy devices in Home Assistant via
the Swiss **[SmartGridready](https://www.smartgridready.ch)** standard.

One add-on, one configuration file: drive any SGr-labelled heat pump, EV
charger, energy meter, PV inverter, or battery — without writing a single
Modbus register or REST call.

> Important: this repository is **not SmartGridready-certified**.
> It may use SmartGridready specifications, EID XML profiles, and the
> official commhandler stack, but any formal SmartGridready conformity
> or EMS certification would require an explicit certification process
> with SmartGridready / the relevant test lab. Using SGr does not make
> this add-on certified.

---

## What it does

SmartGridready (SGr) is a Swiss-backed standard that abstracts device-specific
protocols (Modbus TCP/RTU, REST, MQTT) behind standardised *functional
profiles* described in XML (the **EID**, External Interface Description).
The [SGr product library](https://library.smartgridready.ch) catalogues
hundreds of devices from Stiebel Eltron, KEBA, Fronius, Shelly, Siemens,
Hoval, GARO, Eaton, and many more.

This add-on:

- **Connects** to any SGr device declared in a single YAML config — no
  custom Modbus integration per manufacturer.
- **Exposes** every data point as a native Home Assistant entity via MQTT
  discovery (sensors for read-only, numbers/selects for writable).
- **Optimises** energy automatically with a built-in rules engine that
  evaluates spot price, PV surplus, presence, time-of-use, and CO₂
  intensity — and steers heat pumps and chargers accordingly.
- **Protects** hardware with hysteresis (min interval between mode
  changes) and validates V2H/V2G discharge commands against per-vehicle
  safety rules (min SOC, allowed window, daily cycle cap).
- **Audits** every decision (the last 24 h are kept in a rotating JSON
  log readable from the ingress UI).

---

## Communication model — "poll and remember", not "set and forget"

SGr is a **supervision** protocol, not an event bus. To use it well you
need to know what it does — and what it deliberately does not do.

### Supported transports

The official SGr commhandler ships with three transport bindings (see
[`sgr-commhandler`](https://github.com/SmartGridready/SGrPython)):

| Transport      | Direction                   | Initiator          | Notes                                            |
|----------------|-----------------------------|--------------------|--------------------------------------------------|
| **Modbus TCP/RTU** (`EasyModbus`, deprecated upstream) | Master ↔ slave  | The add-on (master) | Strict polling; the device cannot push anything. SGr has flagged the EasyModbus driver as no longer recommended and is migrating to a replacement; the transport contract is unchanged. |
| **REST / HTTP** (Apache HTTP client) | Client → server | The add-on (client) | Pull only — the commhandler is an HTTP client, not a server. No incoming webhook surface. |
| **MQTT** (HiveMQ client)             | Pub / sub       | Broker-mediated     | The only effectively push-capable channel: the add-on subscribes, the device publishes, the broker delivers as soon as a value changes. |

### What SGr does **not** model

- **No webhooks** in the HTTP sense. The EID-XML schema defines data
  points with a `read`, `write`, or `readwrite` direction. There is no
  `onChange`, no subscription, no callback URL.
- **No events**. A device cannot say "alarm, fault detected" through
  SGr — it can only expose a *value* that you eventually read. You can
  push that value through MQTT, but the semantics of the event are
  not part of the standard; you would be defining your own payload and
  losing the cross-vendor interoperability that is the point of SGr.

### What this means in practice

SGr follows a **poll-and-remember** pattern:

1. The communicator (this add-on) reads the data points it cares about
   on a cadence — fast enough for the kind of control loop you want
   (seconds for active steering, minutes for monitoring).
2. The EMS logic computes decisions on the basis of those readings.
3. The communicator writes setpoints when relevant.
4. Repeat. Without continuous reading, the EMS is blind — there is no
   notification when something changes on the device side.

This is why the rules engine runs on a fixed interval (default
**300 s**) rather than on triggers. It is perfectly suited to energy
management (15-minute averages are the norm anyway) and intentionally
ill-suited to reactive home automation. If you need event-driven
behaviour (alarms, presence triggers, motion-based lighting), keep
using Home Assistant's native automations — this add-on covers the
energy stack, not the reactive layer.

### How MQTT discovery closes the loop with HA

Home Assistant ships the
[official **Mosquitto broker** add-on](https://github.com/home-assistant/addons/tree/master/mosquitto)
in its default add-on store. It is a one-click install and runs
locally — no cloud, no external broker required. Once installed, it
gives both this add-on **and** the HA Core MQTT integration a shared
bus on the HA host.

This add-on uses that bus to publish a
[**Home Assistant MQTT discovery**](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
message for every data point of every connected SGr device. HA picks
them up automatically and creates the matching entities — `sensor`
for read-only points, `number` for writable numeric points. From the
HA side, your heat pump and your wallbox look like native integrations,
even though they go through SGr underneath.

```
   SGr devices                  This add-on                      Home Assistant
+----------------+        +----------------------+        +----------------------+
|  Heat pump     | Modbus |                      |        |                      |
|  (SGr profile) |<------>| sgr-commhandler      |        | MQTT integration     |
+----------------+        |  (poll loop, default |        |  (built-in)          |
                          |   every 300 s)       |        |                      |
+----------------+ Modbus |                      |        |  Discovery topic     |
|  EV charger    |<------>|         |            |        |  homeassistant/...   |
|  (SGr profile) |        |         v            |        |          ^           |
+----------------+        | Rules engine         |        |          |           |
                          |  - context build     |        |          |           |
+----------------+ REST   |  - DSL evaluator     |        |  Auto-creates:       |
|  Energy meter  |<------>|  - hysteresis, V2H   |  MQTT  |   sensor.* (R)       |
|  (SGr profile) |        |         |            |======>|   number.* (RW)      |
+----------------+        |         v            |        |                      |
                          | MQTT bridge          |        | Native dashboards,   |
+----------------+ MQTT   |  - publish discovery |        | automations, scripts |
|  Pub/sub device|<------>|  - publish states    |        | can read/write them. |
|  (SGr profile) |        |  - subscribe commands|        |                      |
+----------------+        +----------------------+        +----------------------+
                                     ^
                                     |
                            Mosquitto broker
                            (HA official add-on,
                             same host, no cloud)
```

Net effect: the user does not need to learn anything about Modbus or
REST. They install Mosquitto, install this add-on, declare devices in
a YAML file — and SGr-labelled appliances show up in Home Assistant
exactly like any other integration.

## How it fits into Home Assistant

### Two channels into HA — what each one is for

The add-on talks to Home Assistant through **two distinct channels**. They
never carry the same data:

| Channel | Direction | Role |
|---|---|---|
| **MQTT discovery** | add-on → HA, HA → add-on | *Exposes* every SGr data point as a native HA entity (`sensor` for read-only, `number` for writable). Commands from the HA UI flow back over the same bus and the add-on writes them to the SGr device. |
| **HA REST API** (`/core/api` via the Supervisor proxy) | add-on ↔ HA | *Reads* HA's own entities (spot price, PV power, battery SOC, presence…) to build the rules-engine context, and *calls* HA services from rule actions. |

MQTT is the **device-exposure** channel — without it, your heat pump and
wallbox don't appear in HA. REST is the **automation** channel — without it,
the rules engine has nothing to evaluate and most rules silently skip. The
two are complementary: if you turn MQTT off in the add-on options, the
engine keeps optimising; if HA REST is unreachable, the engine idles but
the SGr device entities still update via MQTT.

```
                    Home Assistant host
  ============================================================

  +--------------------+      +------------------------------+
  |   HA Core          |      |   SmartGridready add-on      |
  |   (entities,       |      |   (Docker container)         |
  |    automations,    |      |                              |
  |    dashboards)     |      |  +------------------------+  |
  |                    |      |  | ingress UI (FastAPI)   |  |
  |  +-------------+   |      |  |  /devices /rules /audit|  |
  |  | Supervisor  |<--|----->|  +-----------+------------+  |
  |  | proxy       |  REST    |              |               |
  |  | /core/api   |  +token  |  +-----------v------------+  |
  |  +-------------+   |      |  | rules engine (5 min)   |  |
  |        ^           |      |  |  - context builder     |  |
  |        |           |      |  |  - DSL evaluator       |  |
  |  +-----+-------+   |      |  |  - hysteresis / V2H    |  |
  |  | MQTT broker |<--|------|->|  - audit log           |  |
  |  | (Mosquitto) |MQTT      |  +-----------+------------+  |
  |  +-------------+ discovery|              |               |
  |        ^                  |  +-----------v------------+  |
  |        |                  |  | SGr service            |  |
  |   sensors / numbers       |  |  (sgr-commhandler)     |  |
  |   created automatically   |  +-----------+------------+  |
  |                           |              |               |
  +---------------------------+              |               |
                                             | Modbus TCP /  |
                                             | REST / MQTT   |
                              +--------------v-----------+   |
                              | Physical SGr devices     |   |
                              |  heat pump, EV charger,  |   |
                              |  meter, PV inverter ...  |   |
                              +--------------------------+   |
                                                             |
       config.yaml (devices + rules + sensor mapping)        |
       /addon_config/config.yaml  ←  edited via File Editor  |
  ============================================================

  Cadence (every evaluation_interval, default 300s):
    1. Read all HA states via the Supervisor REST proxy.
    2. Build the evaluation context (spot price, PV surplus, SOC,
       presence, time-of-use, grid CO2, V2H availability).
    3. Walk every rule, first matching condition wins.
    4. Skip if same value as last cycle OR within min_interval.
    5. For negative values, run the V2H/V2G safety check.
    6. Write to the SGr data point via sgr-commhandler.
    7. Publish updated states to MQTT (HA picks them up as native
       sensors / numbers thanks to MQTT discovery).
    8. Append the decision to the rolling audit log.
```

The add-on holds **no persistent state** of its own beyond the audit
log and the EID XML cache. Every cycle is recomputed from current HA
state — restart-safe, idempotent, observable.

### What the YAML replaces — and what it doesn't

The rules engine is the *only* place you express the energy-optimisation
logic for SGr devices: heat-pump SG-Ready mode, hot-water setpoint, EV
current limit, V2H discharge, battery charge/discharge. No HA
automations, no per-manufacturer Modbus/REST glue, no
`modbus.write_register` jungle — the YAML is the single source of truth
for steering SGr appliances.

HA automations stay the right tool for everything **outside** the
energy-steering loop:

| Stays in HA automations | Why |
|---|---|
| Reactive / event-driven behaviour (alarms, presence triggers, motion lighting) | The engine runs on a 5-min cadence by design — wrong tool for instant reactions |
| Notifications about engine decisions ("push me when V2H starts") | Subscribe a normal HA automation to the MQTT-published SGr entities |
| Lovelace cards and custom dashboards | The ingress UI shows the audit log, but bespoke dashboards belong in HA |

The **input sensors** the engine reads (spot price, PV power, battery
SOC, presence, weather, …) also live outside the add-on: they normally
come from existing HA integrations (Tibber, aWATTar, Nordpool, your
inverter integration, Electricity Maps, …). You just point `sensors:`
at the resulting entity_ids — see *Telling the engine which HA entities
to use* below.

### SGr fidelity — what the add-on does (and what it deliberately does not claim)

Powered by the official `sgr-commhandler` Python stack. Every device
read, every write, every discovery announcement goes through the
canonical SGr functional-profile API — there is no manufacturer-specific
code anywhere, no hand-rolled Modbus register map, no per-vendor REST
glue. EIDs are fetched directly from the SmartGridready product
library and cached locally.

The HA-side exposure preserves the EID-declared semantics rather than
flattening everything to a generic numeric entity:

| EID property | How it surfaces in HA |
|---|---|
| `dataType` (`INT`, `FLOAT`, `ENUM`, `BOOLEAN`, `STRING`) | Drives the HA component: `number`, `select`, `switch`, `text`, `sensor`, or `binary_sensor` |
| `dataDirection` (`R` / `W` / `RW`) | Read-only entity vs writable entity |
| `unit` (`AMPERES`, `KILOWATT_HOURS`, `DEGREES_CELSIUS`, …) | `unit_of_measurement` on the entity |
| `minimumValue` / `maximumValue` | `min` / `max` on `number` entities |
| `unitConversionMultiplicator` | `step` on `number` entities |
| Enum literals | `options` on `select` entities (e.g. `HP_LOCKED` / `HP_NORMAL` / `HP_INTENSIFIED` / `HP_FORCED`) |
| Inferred device-class (current, voltage, power, energy, temperature, …) | `device_class` on the entity, when the unit is unambiguous |

**No "SGr-compliant" sticker.** The SmartGridready conformance
programme applies to *devices* (vetted via their EID), not to
communicators or EMS software. There is no certification body issuing
such a label for code like this one, so claiming it would be marketing
language with no referent. What you can verify in this repository: the
commhandler dependency, the absence of manufacturer-specific I/O, and
the EID-property propagation listed above.

If SmartGridready introduces or operationalises an EMS certification
path for software like this add-on, that certification would still have
to be obtained explicitly. This repository should therefore be read as
"SGr-based" or "SGr-capable", **not** as certified by default.

The optimisation layer (rules DSL, V2H safety, hysteresis, audit log)
is the add-on's own engineering and is **not part of the SmartGridready
standard** — SGr defines a device interface, not an EMS specification.

## Why SmartGridready

The roll-out of decentralised PV, heat pumps, and EV chargers is creating
bottlenecks in distribution grids that pure copper-and-steel reinforcement
cannot keep up with. SGr's answer is to use the **flexibility** of
buildings — shift loads to cheap/clean hours, store surplus PV in
thermal mass or batteries, reduce draw during peaks — through a single
device-agnostic interface.

The label declares the communication interface of an appliance through
standardised functional profiles. The same code that drives a Stiebel
Eltron heat pump also drives a KEBA wallbox or a Fronius inverter —
because the *interface* is identical, even if the wire protocol differs.

---

## Where to find EID profiles

EID XML files are public. Two entry points:

- **Official browser-friendly library**:
  [library.smartgridready.ch/Device](https://library.smartgridready.ch/Device)
  — filter by manufacturer or device type and download EID-XML files
  one at a time. This is the easiest way to discover what is supported.
- **GitHub (raw, versioned)**:
  [github.com/SmartGridready](https://github.com/SmartGridready) — the
  `SGrSpecifications` repository contains every profile XML plus the
  schema. More practical if you want to clone the whole set or diff
  between versions.

### Technical context

SmartGridready places emphasis on Web and IP technologies and asks
manufacturers to ship device profiles as XML files. The standard
focuses on **functional profiles** and reuses the communication
protocols already present in buildings (Modbus TCP/RTU, HTTP/REST,
MQTT). Each profile describes the addressable function profiles,
data points, and attributes exposed through the SGr interface.

The EID — **External Interface Description** — is the XML file that
maps the generic SGr functional-profile model to a concrete device's
registers or endpoints. The `commhandler` library reads it at
startup and presents a uniform API on top, regardless of the wire
protocol.

For this add-on the Python flavour of the commhandler is used — there
is no Java stack involved. If you only need to integrate one fixed
device into Home Assistant you can of course parse the XML manually
and extract the relevant Modbus registers; this add-on is essentially
the *generic* version of that approach.

## Where to find the Python libraries

The SmartGridready association maintains an official Python stack on
GitHub. They are independent libraries you can use directly if you
ever need to drop down below this add-on:

| Library | What it is | Where |
|---|---|---|
| **`sgr-commhandler`** | Core Python commhandler. Loads EID XML, builds devices, exposes read/write on data points. Bundled inside this add-on. | [PyPI](https://pypi.org/project/sgr-commhandler/) · [GitHub: SGrPython](https://github.com/SmartGridready/SGrPython) |
| **`SGrPythonIntermediary`** | Optional REST gateway in front of the commhandler. Useful when your communicator is written in a language without a native SGr library. Available as a Docker image. | [GitHub](https://github.com/SmartGridready/SGrPythonIntermediary) |
| **`SGrPythonSamples`** | Working examples, including an [OpenCEM](https://opencem.org) integration. | [GitHub](https://github.com/SmartGridready/SGrPythonSamples) |
| **`SGrDeclarationTool`** | Visual editor for authoring new EIDs and functional profiles — only needed if you publish a new device profile to the library. | [GitHub](https://github.com/SmartGridready/SGrDeclarationTool) |

Install the commhandler with:

```bash
pip install sgr-commhandler        # requires Python >= 3.9
```

A Java stack exists in parallel ([`SGrJava`](https://github.com/SmartGridready/SGrJava),
[`SGrJavaDrivers`](https://github.com/SmartGridready/SGrJavaDrivers),
[`SGrJavaIntermediary`](https://github.com/SmartGridready/SGrJavaIntermediary))
with the same API surface. This add-on does not depend on it.

> Note: the commhandler currently ships three transport bindings —
> Modbus (via `EasyModbus`), HTTP/REST (via Apache HTTP client) and
> MQTT (via the HiveMQ client). The SmartGridready team has flagged
> the `EasyModbus` implementation as **no longer recommended** for
> new work; a replacement is being rolled in. The transport contract
> exposed by the commhandler is stable regardless of the underlying
> library.

## Quick start

1. Add this repository to Home Assistant:
   **Settings → Add-ons → ⋮ → Repositories**

   ```
   https://github.com/chrohrbach/ha-smartgridready
   ```

2. Install the **SmartGridready** add-on.

3. Create `/config/smartgridready/config.yaml` (the add-on writes a
   commented example on first start).

4. Start the add-on. Open the ingress UI from the sidebar to see
   connected devices, the current evaluation context, and the audit
   trail.

---

## Configuration

A minimal example:

```yaml
# /config/smartgridready/config.yaml

sensors:
  spot_price: sensor.electricity_spot_price
  pv_power: sensor.pv_total_power
  house_consumption: sensor.house_total_power
  battery_soc: sensor.home_battery_soc
  grid_export: sensor.grid_export_power

devices:
  - name: "Heat Pump"
    eid: SGr_04_0015_xxxx_StiebelEltron_HeatPump_V1.0.0
    properties:
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

rules:
  # Heat pump: write the SG-Ready command, NOT the read-only feedback.
  # The values are enum members declared in the SG-ReadyStates profile.
  - device: "Heat Pump"
    profile: "SG-ReadyStates"
    data_point: "SGReadyOpModeCmd"
    min_interval: 15
    conditions:
      - when: "has_surplus AND spot_price < 0.08"
        value: HP_FORCED        # store surplus PV in the buffer tank
      - when: "spot_price < 0.12"
        value: HP_NORMAL
      - when: "spot_price > 0.25"
        value: HP_LOCKED
      - default:
        value: HP_NORMAL

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

### Telling the engine which HA entities to use

The rules engine doesn't scan HA looking for relevant data — you point
it at each value explicitly. Three places in `config.yaml`:

| YAML key | Points to | Used for |
|---|---|---|
| `sensors:` | Map of **context variable** → HA `entity_id` | Feeds named values (`spot_price`, `pv_power`, `battery_soc`, …) into the rule DSL |
| `enable_toggle:` | A single HA boolean entity (`input_boolean.*`) | Kill switch — when it's `off`, the engine skips the whole cycle |
| `vehicles[].soc_entity` / `.plugged_entity` / `.charging_power_entity` | Per-EV entities | Feed the V2H safety check (min SOC, plug status, daily-cycle cap) |

`sensors:` accepts a fixed set of **well-known keys** that drive the
built-in derived context (`surplus_pv`, `has_surplus`, `home_deficit_w`,
`is_peak`, `is_offpeak`, `is_solar_window`, `at_home`, `grid_co2_kg_per_kwh`,
…), plus any **custom keys** you like — extra entries are passed through
to the rule DSL under the same name:

```yaml
sensors:
  spot_price:   sensor.electricity_spot_price     # well-known
  pv_power:     sensor.pv_total_power             # well-known
  my_pool_temp: sensor.pool_thermometer           # custom → "my_pool_temp"
```

Then in a rule: `when: "my_pool_temp < 22"`.

`value:` also supports simple context templating:

```yaml
value: "{{ battery_soc }}"
```

The placeholder is expanded from the current rule context before the
write. This is useful for scalar setpoints that should track a live HA
value rather than a fixed constant.

Missing entities don't crash anything: numeric ones resolve to `0.0`,
booleans to `false`, and any rule that depends on them simply doesn't
match. A few entities are **auto-detected** by their canonical entity_id
and don't need to be declared — Electricity Maps
(`sensor.electricity_maps_co2_intensity`) and CO2 Signal sensors override
the default Swiss grid-CO₂ constant when present.

See [docs/configuration.md](docs/configuration.md) for the full schema,
[docs/rules-dsl.md](docs/rules-dsl.md) for the condition language, and
[docs/supported-devices.md](docs/supported-devices.md) for an indicative
device list. For an explicit map of which SmartGridready concepts
this add-on implements and which it deliberately leaves out, see
[docs/scope-and-gaps.md](docs/scope-and-gaps.md).

---

## Project status

**Early — not yet validated against physical hardware in this
standalone packaging.** The underlying rules engine and SGr device
wrapper are battle-tested inside the casasmooth platform, but this
add-on is a freshly extracted, re-packaged release. Expect rough
edges, breaking changes, and missing translations until the first
0.x.x tags settle.

Pull requests welcome — especially:
- bug reports from real installations,
- device profiles (EID identifiers) for hardware not yet listed,
- V2H / V2G hardware reports (wallbox EIDs that actually accept a
  negative current target),
- translations (the ingress UI is currently English-only).

---

## Credits

This add-on was contributed to the open-source community by
[**teleia**](https://www.teleia.ch), which has natively integrated
SmartGridready into its smart-home platform
[**casasmooth**](https://www.casasmooth.com). The code released here
is the same engine that drives heat pumps, EV chargers, and energy
meters in casasmooth installations — extracted, decoupled, and
re-packaged as a standalone Home Assistant add-on so the wider
community can benefit from a manufacturer-neutral energy stack.

## License

MIT — see [LICENSE](LICENSE). The SmartGridready label and the
referenced EID XML profiles are property of the SmartGridready
association ([www.smartgridready.ch](https://www.smartgridready.ch)).
