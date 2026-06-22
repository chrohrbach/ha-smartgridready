# Scope and gaps — how this add-on maps to SmartGridReady

This document is the honest answer to *"how SmartGridReady is this
add-on, really?"* It cross-references the code against the official
SGr specification artefacts and the public criteria for the
SmartGridReady labels.

It is not a marketing page — items the add-on does **not** cover are
listed here so users can decide whether the gap matters for their use
case, and contributors know what is open for future work.

Last reviewed against the SGr specification revision 0.2.2 of the
schema database
([SGrSpecifications](https://github.com/SmartGridready/SGrSpecifications)).

---

## 1. What SGr defines

### 1.1 The standard's two roles

SGr splits an installation into **products** (devices that produce or
consume energy) and **communicators** (controllers that read products'
values and issue setpoints). The conformance scheme awards labels on
three independent axes:

| Label | Subject | Status |
|---|---|---|
| Component / Product | Individual devices vetted via their EID XML | Issued today |
| EMS | Energy management software / hardware | Launch announced for autumn 2026 |
| Buildings & Campus | Whole properties | Issued today |

This add-on is a **communicator / EMS** — neither component nor
building. The product label does not apply to it.

Important wording: this repository is **not certified** merely because
it uses SGr artefacts. If an EMS label / certification path becomes
available for software like this add-on, it would require an explicit
SmartGridReady certification process. Until then, the accurate wording
is "uses SmartGridReady" or "implements parts of the SmartGridReady
model", not "SmartGridReady-certified".

### 1.2 Level of operation (`m`, `1`–`6`, compound)

`LevelOfOperation` is declared per device and per functional profile
in every EID. It is the SGr standard's single most important way to
say *what depth of control a profile actually supports*. The values
defined in
[`BaseType_LevelOfOperationType.xsd`](https://github.com/SmartGridready/SGrSpecifications/blob/master/SchemaDatabase/SGr/Generic/BaseType_LevelOfOperationType.xsd)
are:

| Level | Meaning | Example |
|---|---|---|
| `m` | monitoring only — no writable data points | Submeter reading |
| `1` | binary writable (on/off, lock) | SG-Ready Mode 1 (HP_LOCKED) |
| `2` | discrete writable (multiple discrete states or scalar setpoint with a small allowed set) | SG-Ready 4-mode command, EV current limit step |
| `3` | fixed set of characteristic curves | P(U), Q(U), Heizkurve — built-in tables |
| `4` | dynamic setpoints (continuous, time-varying) | Setpoint pushed every minute |
| `5` | dynamic characteristic curves | Operator uploads new P(U) curve at runtime |
| `6` | predictive / horizon-aware | Receives forecasts, accepts a 24 h plan |
| `Nm` | level N + monitoring | Most common in the catalogue |

The add-on reads `LevelOfOperation` per device and per functional
profile and surfaces it in the ingress UI and the audit log. The
rules engine itself **only emits writes equivalent to levels 1, 2 and
4** — i.e. scalar values or enum members. See §3 for the
implications.

### 1.3 Direction codes

Per
[`BaseTypes.xsd`](https://github.com/SmartGridready/SGrSpecifications/blob/master/SchemaDatabase/SGr/Generic/BaseTypes.xsd),
`DataDirectionProduct` is one of `C`, `R`, `W`, `RW`, `RWP`. The
add-on treats anything containing `W` as writable; `C` (constant) and
`R` are read-only.

### 1.4 EMS label criteria (announced for autumn 2026)

Source: [smartgridready.ch/ems](https://smartgridready.ch/ems).
The six functional requirements:

1. **Sicherheit** — IT security, protected access to plant and data.
2. **Kommunikation** — within the building between PV inverter,
   battery, charging management, heat pump and meter using
   standardised connections.
3. **Kommunikation Netz** — read tariff information, expose a grid
   interface for DSO control, ready for dynamic tariffs.
4. **Leistungsbegrenzung** — feed-in and import limits at the
   point of common coupling (PCC), defined and respected by the EMS.
5. **Systemoptimierung** — connected devices controlled by tariff for
   an optimised electricity bill.
6. **Monitoring** — energy and power data captured and stored.

Conformity is checked at the FHNW SmartGridReady-Testlab and the BFH
PV laboratory.

---

## 2. What this add-on covers

### 2.1 Transports

| Transport | Status | Source |
|---|---|---|
| Modbus TCP / RTU | Covered (via `EasyModbus`, upstream flagged for replacement) | `sgr-commhandler` |
| REST / HTTP | Covered (Apache HTTP client) | `sgr-commhandler` |
| MQTT (device-side) | Covered (HiveMQ client) but **not used as a push trigger** for the engine — see §4 | `sgr-commhandler` |

### 2.2 Functional-profile categories actually exercised

Out of the 49 enum values in
[`BaseType_FunctionalProfileCategory.xsd`](https://github.com/SmartGridready/SGrSpecifications/blob/master/SchemaDatabase/SGr/Generic/BaseType_FunctionalProfileCategory.xsd),
the rules engine has been validated against:

- HeatPumpControl (SG-ReadyStates, DomHotWaterCtrl)
- EVSE / ChargingOutlet (EMS_Current_Limit)
- Metering (read-only)
- Inverter (read-only)
- DynamicTariff (read-only, with `parameters={"date": …}`)

Any other category works through the same generic API — write a rule,
the engine evaluates and dispatches — but has not been exercised end
to end. The EID is the authoritative source for what data points
exist on a given device.

### 2.3 EMS label criteria coverage

| Criterion | Coverage status | Detail |
|---|---|---|
| Sicherheit | **Partial.** Safe DSL (no `eval`), masked passwords in UI, `safe_load` YAML, Supervisor-injected MQTT credentials. No explicit TLS policy on REST/MQTT (depends on commhandler defaults). | §3.5 |
| Kommunikation (PV / battery / charger / HP / meter) | **Covered.** Any device with an EID in the SGr library is addressable through the generic code path. | §2.2 |
| Kommunikation Netz — tariffs | **Covered as a read path.** `DynamicTariff_*` EIDs can be connected and their forecast can be parsed if exposed as an HA attribute. | §3.4 |
| Kommunikation Netz — DSO control | **Indirect.** The add-on has no incoming HTTP/EEBus/OCPP endpoint; instead it exposes `dso_curtailment_active` / `dso_curtailment_factor` context variables that any third-party HA integration can drive. | §3.2 |
| Leistungsbegrenzung at PCC | **Indirect.** `pcc_power_w`, `pcc_headroom_w` and `pcc_overload` are first-class context variables that a rule can condition on. The add-on does **not** include an aggregate solver that automatically caps the sum of controllable loads. | §3.1 |
| Systemoptimierung by tariff | **Covered.** Core feature of the rules engine. Each rule local; no joint MILP/MPC optimisation. | §3.3 |
| Monitoring | **Partial.** The audit log records decisions (24 h rolling). Time-series measurements are delegated to HA Recorder / InfluxDB. | §3.6 |

---

## 3. Known gaps and roadmap

### 3.1 Aggregate power cap at the PCC — Level 4 in spirit but not enforced

**Status:** the rules engine exposes the headroom but does not enforce
a hard aggregate cap.

**What the add-on does today.** A rule can read `pcc_headroom_w` (=
`grid_connection_limit_w` − current `grid_import`) and decide
unilaterally to back off — for example `when: "pcc_headroom_w < 2000"
value: 6` on a wallbox rule. This is sufficient when you have a
single dominant flexible load.

**What is missing.** If you have several flexible loads (heat pump,
wallbox, immersion heater, battery), each rule is evaluated
independently in priority order. A multi-device aware allocator
would be needed to respect a hard sum-of-loads cap. This is the gap
between SGr level 4 (dynamic setpoints) and a true *constrained*
optimiser, and it is what the EMS label criterion
**Leistungsbegrenzung** requires for compliance.

Planned: a future top-level `constraints:` block that runs after rule
evaluation and clamps the action list against shared budgets. Not
implemented yet.

### 3.2 DSO control interface

**Status:** indirect.

The add-on does not provide an HTTP, OCPP, EEBus, FNN-Steuerbox or
ripple-control endpoint of its own. Instead, the convention is:

- If a third-party HA integration publishes a curtailment signal as
  `binary_sensor.dso_curtailment_active` and / or
  `sensor.dso_curtailment_factor`, the engine picks it up automatically.
- Otherwise the user maps any HA entity to those names via
  `sensors.dso_curtailment_active` / `sensors.dso_curtailment_factor`.

Inside rules:

```yaml
# Hard override — DSO command beats everything else.
- device: "Wallbox"
  profile: "EMS_Current_Limit"
  data_point: "EMSCurrentLimit"
  min_interval: 1
  conditions:
    - when: "dso_curtailment_active"
      value: 6
    - default:
      value: 16
```

A native endpoint inside the add-on is **out of scope** by design —
that integration responsibility belongs further upstream in HA.

### 3.3 Predictive / horizon-aware control (Level 6)

**Status:** rule-based on a fixed cadence.

**What the add-on does today.** The engine evaluates every
`evaluation_interval` seconds (default 300). PV forecast keys
(`pv_forecast_kwh`, `pv_forecast_today_kwh`, …) and tariff horizon
helpers (`tariff_next_3h_min/_max/_avg`,
`tariff_in_lowest_quartile_today`) are surfaced so a rule can react
to *near future* conditions without a true MPC layer.

**What the literature does.** Comparative studies of rule-based vs
MPC/MILP/RL for heat pumps and EV charging consistently report 10–60 %
gains for MPC, the upper end appearing when there is a hybrid storage
system (battery + thermal). See e.g.

- *Approximate model predictive control for heat pump systems*,
  Journal of Building Performance Simulation, 2024.
- *Economic MPC for office building with PV/HP/EV*, Energy & Buildings, 2025.
- *AI-MPC for heat pumps — a review*, RSER, 2026.

This gap is **deliberate**: a rule-based engine fits Home Assistant's
operational profile (declarative, observable, restart-safe). A true
MPC layer would require a thermal model of the building, an EV
charging schedule from user input, and either an embedded solver
(GLPK / OSQP / HiGHS) or a remote optimisation service. Neither is
in scope of this add-on as currently architected.

### 3.4 Characteristic curves (Levels 3 and 5)

**Status:** not implemented.

PV inverters in Switzerland are subject to NA/EEA-CH 2025 country
settings — P(U) active-power reduction above a voltage threshold,
Q(U) reactive-power control with a 5-second time constant, Cosφ(P)
characteristic. These are level-3 data points in SGr (fixed tables)
or level-5 if the EMS uploads a new table at runtime.

The DSL only writes **scalar** values. It cannot write a table.
A future `value_curve:` syntax is conceivable but is a sizable
architectural change (write API, MQTT exposure, UI). The honest
status for now: **out of scope**.

If your installation needs P(U)/Q(U), the inverter does this on its
own based on its country-code preset — that does not require an EMS
in the loop.

### 3.5 Security

The current posture:

- YAML loaded with `yaml.safe_load` — no Python tags executed.
- The condition DSL is a hand-written parser; no `eval()`,
  `exec()`, `compile()` or `getattr()` of user-controlled strings.
- Passwords in the user config are masked in the ingress UI and
  audit JSON via the `password` / `token` / `secret` / `api_key`
  prefix filter.
- MQTT credentials come from the Supervisor Services API, not the
  user config.

Not currently enforced:

- No mTLS guarantees on REST device transports — depends on what the
  `sgr-commhandler` and `httpx` defaults negotiate.
- No detection of malicious config (e.g. tens of thousands of
  conditions) — there is no rule-count limit.
- No CSP or auth on the ingress FastAPI app — relies on the HA
  Supervisor ingress proxy for auth.

### 3.6 Monitoring storage

**Status:** delegated to HA.

The add-on stores decisions in `audit.json` (24 h rolling, 288
entries) but does **not** keep its own time-series database. The
SGr-published values are emitted on MQTT and HA's Recorder / Long-term
Statistics integration is expected to keep them. For an EMS-label
audit trail of energy / power, point Recorder at the relevant entity
ids and / or use the official InfluxDB / Prometheus add-ons.

### 3.7 Heat-pump SG-Ready Mode 1 — utility lock ≤ 2 h / day

**Status:** implemented (Level 1 guard).

The BWP SG-Ready 1.1 specification limits utility-lock (Mode 1,
`HP_LOCKED`) to a maximum of two hours per 24-hour day. The add-on
maintains a per-rule ledger of lock periods on disk
(`sg_ready_lock.json`, next to the audit log), prunes anything older
than 24 hours on every cycle, and downgrades `HP_LOCKED` writes back
to `HP_NORMAL` automatically once the cap is hit. Cap is configurable
via the add-on option `sg_ready_lock_cap_minutes` (default 120, set
to 0 to disable).

### 3.8 Battery as a first-class profile

**Status:** partial.

The add-on knows `battery_soc` (from the user's sensor mapping),
`battery_capacity_kwh` (from the user's top-level config) and derives
`battery_full`, `battery_low`, `battery_room_kwh`,
`battery_available_kwh` for use in rules. What it does **not** do is
issue battery charge/discharge setpoints with a dedicated profile —
those would be standard SGr writes (e.g.
`BatterySystem.ActivePowerSetpoint`) through the generic write path,
just like any other writable data point.

### 3.9 Demand-charge (Leistungstarif)

**Status:** not implemented.

Many Swiss B2B tariffs charge on the maximum 15-minute average power
in a billing period. The add-on does not maintain a 15-min rolling
mean and does not constrain rule writes against a peak-power budget.
The pragmatic workaround today is to compute that average in HA via
the `statistics` integration and reference it as a custom sensor in
`sensors:` — but the constraint enforcement remains the user's
responsibility.

### 3.10 Event-driven / push triggers

**Status:** not implemented.

MQTT devices can push state changes through the broker, and
`sgr-commhandler` exposes a subscription path for those, but the
rules engine still runs on a fixed cadence. A device pushing a
"fault" or "rapid SOC change" event does not trigger an immediate
evaluation; the next scheduled tick picks it up. This is consistent
with the engine's role in energy management (15-minute averages are
the norm anyway) and *deliberately* unsuited to reactive automation,
which belongs in HA's native automation engine.

### 3.11 Time zone / DST

**Status:** implemented.

All wall-clock context variables (`hour`, `weekday`, `is_peak`,
`is_offpeak`, `is_solar_window`, `allow_window`, day-bucket key for
the V2H counter) go through a configurable time zone (add-on option
`timezone`, default `Europe/Zurich`). DST shifts are handled by the
underlying `zoneinfo` database. Delta-style state (hysteresis
timestamps, audit timestamps) is stored in UTC.

### 3.12 15-minute alignment

**Status:** implemented as an opt-in.

The add-on option `align_to_quarter` (default `false`) delays the
first evaluation tick so cadence sits on HH:00 / HH:15 / HH:30 /
HH:45. Useful when the upstream tariff is also published in
15-minute slices. The context variable `minute_in_quarter` (0–14) is
always available regardless of alignment.

### 3.13 Virtual devices / HA proxy layer

**Status:** not implemented yet in this repository.

casasmooth contains an additional mechanism for "virtual devices"
(`climate_proxy`, `switch_proxy`, `boiler_proxy`, `number_proxy`) that
maps the same rule grammar onto Home Assistant service calls instead of
native SGr device writes. This is a useful extension for non-SGr
hardware and is a plausible future addition here.

If/when added, it must be described precisely for what it is:

- a **Home Assistant proxy/orchestration layer**
- useful for applying the same EMS logic to non-SGr devices
- **not** native SmartGridReady communication
- therefore **not evidence of SmartGridReady conformity or certification**

Virtual devices would widen practical coverage, but they would also move
the add-on toward a hybrid HA energy orchestrator. They should never be
marketed as "everything behind it becomes SmartGridReady".

---

## 4. The MQTT push claim — clarified

The README states that MQTT is the *only* push-capable channel among
the three SGr transports. That is true for the transport itself: a
broker delivers value changes as they happen. The rules engine,
however, evaluates on a fixed cadence and **does not** subscribe to
device pushes for triggering. Effects:

- An MQTT-fed SGr device's *value* is read into the context on the
  next tick — at worst one `evaluation_interval` later.
- Out-of-band events (alarms, faults) modelled as a value in the EID
  are observable but not reactive — a rule referring to that value
  evaluates on schedule.
- For truly reactive behaviour (alarm → notification, motion →
  light), keep using HA's native automations against the MQTT
  entities this add-on publishes.

---

## 5. References

- [SGrSpecifications repo](https://github.com/SmartGridready/SGrSpecifications)
- [LevelOfOperation XSD](https://github.com/SmartGridready/SGrSpecifications/blob/master/SchemaDatabase/SGr/Generic/BaseType_LevelOfOperationType.xsd)
- [FunctionalProfileCategory XSD](https://github.com/SmartGridready/SGrSpecifications/blob/master/SchemaDatabase/SGr/Generic/BaseType_FunctionalProfileCategory.xsd)
- [BaseTypes XSD (directions, units)](https://github.com/SmartGridready/SGrSpecifications/blob/master/SchemaDatabase/SGr/Generic/BaseTypes.xsd)
- [EMS label criteria — smartgridready.ch/ems](https://smartgridready.ch/ems)
- [SG-Ready BWP 1.1 — `Mode 1` ≤ 2 h / day](https://www.waermepumpe.de/fileadmin/user_upload/bwp_service/SG_ready/SG_Ready_Interface_1.1.pdf)
- [Swissolar Country Settings NA/EEA-CH 2025 (P(U), Q(U))](https://www.swissolar.ch/01_wissen/planung-und-umsetzung/netzanschluss/landereinstellungen-schweiz-ch-2025-en.pdf)
- [ElCom Weisung 8/2025 — network reinforcement & flexibility](https://www.elcom.admin.ch/dam/elcom/de/dokumente/Weisungen/8_2025_weisung_netzverstaerkungen.pdf.download.pdf/8_2025%20Weisung%20Netzverst%C3%A4rkungen.pdf)
- *Comparison of MPC and rule-based for HP/BOPTEST*, JBPS 2024
  (DOI 10.1080/19401493.2023.2280577)
- *Economic MPC for PV/HP/EV/storage*, Energy & Buildings 2025
  (S0360132325007929)
