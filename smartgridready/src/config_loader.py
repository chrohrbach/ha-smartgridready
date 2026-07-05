"""User configuration loader.

Reads ``/addon_config/config.yaml`` (path configurable via add-on
options) and validates the structure into typed objects. On first run,
if the file does not exist, a commented example is written so the user
has something to start from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("smartgridready.config")

EXAMPLE_CONFIG_NAME = "config.example.yaml"


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------

@dataclass
class SensorMap:
    """Mapping from context variable name to Home Assistant entity_id.

    Only the keys actually present in the user's config are kept; missing
    ones default to ``None`` and produce ``0.0`` / ``False`` at evaluation
    time so rules degrade gracefully.
    """

    spot_price: Optional[str] = None
    pv_power: Optional[str] = None
    house_consumption: Optional[str] = None
    battery_soc: Optional[str] = None
    grid_export: Optional[str] = None
    grid_import: Optional[str] = None
    temperature_outdoor: Optional[str] = None
    pv_forecast_kwh: Optional[str] = None
    pv_forecast_today_remaining_kwh: Optional[str] = None
    pv_current_hour_kwh: Optional[str] = None
    pv_forecast_today_kwh: Optional[str] = None
    away_mode: Optional[str] = None
    # DSO (Verteilnetzbetreiber / VNB) curtailment signal. The SGr EMS
    # label criteria require an interface for grid-operator control —
    # the add-on itself has no incoming endpoint, but if any third-party
    # integration publishes the signal as an HA entity we surface it as
    # a first-class context variable.
    dso_curtailment_active: Optional[str] = None
    dso_curtailment_factor: Optional[str] = None
    extra: Dict[str, str] = field(default_factory=dict)
    """Free-form ``{context_key: entity_id}`` pairs for user-defined keys."""


@dataclass
class DeviceConfig:
    name: str
    eid: str
    enabled: bool = True
    properties: Dict[str, Any] = field(default_factory=dict)
    evse_safety: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleConfig:
    device: str
    profile: str
    data_point: str
    min_interval: int = 15
    conditions: List[Dict[str, Any]] = field(default_factory=list)
    smooth_transition: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VehicleConfig:
    name: str
    soc_entity: Optional[str] = None
    plugged_entity: Optional[str] = None
    charging_power_entity: Optional[str] = None
    charger_device: Optional[str] = None
    battery_capacity_kwh: float = 0.0
    v2h: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PvArrayConfig:
    """A PV array registered for the self-computed Open-Meteo forecast.

    Declared independently of ``devices:`` because the array does not
    need to be an SGr-labelled/connected device at all — this is purely
    nameplate metadata used to estimate production from solar
    irradiance. ``tilt``/``azimuth`` are optional: without them the
    forecast falls back to plain (untransposed) global horizontal
    irradiance, which is still a reasonable estimate for a roughly
    south-facing residential roof.
    """

    name: str
    kwp: float
    tilt: Optional[float] = None
    azimuth: Optional[float] = None
    efficiency: float = 0.80
    enabled: bool = True


# Recognised ``virtual_devices[].type`` values and the YAML keys that hold
# their target entities / SG-Ready mapping table. Shared with the parser
# below and with ``virtual_devices.py`` (which imports the tuple for
# validation instead of re-declaring it).
VIRTUAL_DEVICE_TYPES = ("climate_proxy", "switch_proxy", "boiler_proxy", "number_proxy")

_VIRTUAL_TARGET_KEYS = {
    "climate_proxy": "climate_entities",
    "switch_proxy": "switch_entities",
    "number_proxy": "number_entities",
}
_VIRTUAL_MAPPING_KEYS = {
    "climate_proxy": "sg_ready_to_offset",
    "switch_proxy": "sg_ready_to_switch",
    "boiler_proxy": "sg_ready_to_temperature",
    "number_proxy": "sg_ready_to_value",
}


@dataclass
class VirtualDeviceConfig:
    """A non-SGr device piloted via plain Home Assistant service calls.

    Lets the rules engine target ordinary HA entities (``climate.*``,
    ``switch.*``, ``water_heater.*``, ``number.*``) using the exact same
    SG-Ready state literals (``HP_LOCKED`` / ``HP_NORMAL`` /
    ``HP_INTENSIFIED`` / ``HP_FORCED``) already used for real SGr
    devices — one rule vocabulary covers both. This is a **Home
    Assistant orchestration layer**, not native SmartGridready
    communication — see docs/scope-and-gaps.md §3.13.
    """

    name: str
    type: str  # one of VIRTUAL_DEVICE_TYPES
    enabled: bool = True
    targets: List[str] = field(default_factory=list)
    # SG-Ready state literal (upper-cased) -> value. Meaning depends on
    # ``type``: offset in °C (climate_proxy), bool (switch_proxy),
    # target temperature in °C (boiler_proxy), or a raw number
    # (number_proxy).
    mapping: Dict[str, Any] = field(default_factory=dict)
    base_setpoint_default: float = 21.0
    base_setpoint_entity: Optional[str] = None
    min_setpoint: float = 16.0
    max_setpoint: float = 24.0
    min_value: float = 0.0
    max_value: float = 100.0


@dataclass
class OptimizerDeviceConfig:
    """One controllable load registered with the predictive-dispatch
    optimizer (MILP, ``scipy.optimize.milp`` — falls back to a greedy
    heuristic when scipy is unavailable).

    ``name`` is a free-form label, not necessarily a ``devices:`` or
    ``virtual_devices:`` name — the optimizer only *computes* a
    schedule. Wiring the scheduled watts/amps into an actual write is
    the user's job via a normal ``rules:`` entry referencing the
    injected context variables (``optimizer_<slug>_power_w`` /
    ``_current_a`` / ``_on``). See docs/configuration.md#optimizer.
    """

    name: str
    min_power_w: float = 0.0
    max_power_w: float = 3000.0
    must_run_hours: int = 0
    preferred_window: Optional[List[int]] = None  # [start_hour, end_hour], wraps midnight
    priority: int = 50
    switchable: bool = False
    # Used to also expose a `_current_a` context variable for profiles
    # that expect amps (e.g. EMS_Current_Limit) rather than watts.
    voltage: float = 230.0
    phases: int = 1


@dataclass
class OptimizerBatteryConfig:
    capacity_kwh: float = 0.0
    max_charge_w: float = 0.0
    max_discharge_w: float = 0.0
    soc_min_pct: float = 10.0
    soc_max_pct: float = 95.0
    efficiency: float = 0.95
    cycle_cost_chf_kwh: float = 0.01


@dataclass
class OptimizerGridConfig:
    pcc_import_w: float = 25000.0
    pcc_export_w: float = 12000.0
    export_price_chf_kwh: float = 0.08


@dataclass
class OptimizerConfig:
    """Top-level ``optimizer:`` section — opt-in predictive dispatch.

    Disabled (``enabled: false``) by default: the DSL ``rules:`` engine
    remains the primary — and only required — optimisation mechanism.
    """

    enabled: bool = False
    devices: List[OptimizerDeviceConfig] = field(default_factory=list)
    battery: OptimizerBatteryConfig = field(default_factory=OptimizerBatteryConfig)
    grid: OptimizerGridConfig = field(default_factory=OptimizerGridConfig)


@dataclass
class UserConfig:
    sensors: SensorMap = field(default_factory=SensorMap)
    enable_toggle: Optional[str] = None
    devices: List[DeviceConfig] = field(default_factory=list)
    rules: List[RuleConfig] = field(default_factory=list)
    vehicles: List[VehicleConfig] = field(default_factory=list)
    pv_arrays: List[PvArrayConfig] = field(default_factory=list)
    virtual_devices: List[VirtualDeviceConfig] = field(default_factory=list)
    optimizer: OptimizerConfig = field(default_factory=OptimizerConfig)
    # Watts at the point of common coupling that must not be exceeded
    # (sum of grid_import, signed). 0 disables the headroom helpers.
    grid_connection_limit_w: float = 0.0
    # Home battery total capacity in kWh, used by the battery helpers
    # (battery_room_kwh, battery_available_kwh). 0 disables them.
    battery_capacity_kwh: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)

    def device_by_name(self, name: str) -> Optional[DeviceConfig]:
        return next((d for d in self.devices if d.name == name), None)


# -----------------------------------------------------------------------------
# Example config (written on first run)
# -----------------------------------------------------------------------------

EXAMPLE_YAML = """\
# SmartGridready add-on configuration
# ----------------------------------------------------------------------
# Edit this file with the Home Assistant File Editor or VS Code add-on,
# then restart the SmartGridready add-on.
#
# Devices are identified by their EID (External Interface Description)
# from the SmartGridready product library:
#   https://library.smartgridready.ch
#
# The EID XML is downloaded and cached on first connection.

# -- Sensor mappings ---------------------------------------------------
# Tell the add-on where to find context values in Home Assistant. All
# keys are optional — missing ones default to 0 / false and rules that
# depend on them simply do not match.

sensors:
  spot_price: sensor.electricity_spot_price
  pv_power: sensor.pv_total_power
  house_consumption: sensor.house_total_power
  battery_soc: sensor.home_battery_soc
  grid_export: sensor.grid_export_power
  grid_import: sensor.grid_import_power
  temperature_outdoor: sensor.outdoor_temperature
  pv_forecast_kwh: sensor.energy_production_tomorrow
  pv_forecast_today_remaining_kwh: sensor.energy_production_today_remaining
  pv_current_hour_kwh: sensor.energy_current_hour
  pv_forecast_today_kwh: sensor.energy_production_today
  away_mode: input_boolean.away_mode
  # If your DSO publishes a curtailment signal through a third-party
  # integration (e.g. ripple control, FNN-Steuerbox bridge, custom
  # webhook), point these here and use `dso_curtailment_active` /
  # `dso_curtailment_factor` in your rules to override the optimisation.
  # dso_curtailment_active: binary_sensor.dso_curtailment_active
  # dso_curtailment_factor: sensor.dso_curtailment_factor

# Optional kill switch — when this boolean is off, the rules engine
# skips evaluation entirely. Useful for maintenance.
# enable_toggle: input_boolean.smartgridready_enabled

# Watts at the point of common coupling that must not be exceeded.
# Exposes `pcc_power_w`, `pcc_headroom_w` and `pcc_overload` in the
# rule DSL so rules can cap loads when the home is near its limit.
# grid_connection_limit_w: 25000

# Total kWh of the home battery (if any). Required for the
# `battery_room_kwh` / `battery_available_kwh` helpers.
# battery_capacity_kwh: 10

# -- Devices ----------------------------------------------------------

devices:

  # Heat pump (Modbus TCP) — Stiebel Eltron generic EID
  # - name: "Heat Pump"
  #   eid: SGr_04_0015_xxxx_StiebelEltron_HeatPump_V1.0.0
  #   properties:
  #     ip: 192.168.1.50
  #     port: 502
  #     slave_id: 1

  # EV charger (Modbus TCP) — KEBA KeContact P30 generic EID
  # - name: "Wallbox"
  #   eid: SGr_04_mmmm_dddd_KEBA_KeContact_P30_V0.1
  #   properties:
  #     ip: 192.168.1.60
  #     port: 502
  #     slave_id: 1
  #   evse_safety:
  #     safe_current: 6
  #     max_receive_time_sec: 120

  # Energy meter (local REST with basic auth) — Shelly Pro 3EM
  # - name: "Shelly 3EM"
  #   eid: SGr_00_mmmm_dddd_Shelly_Pro3EM_RestAPILocalBasicAuth_V1.0
  #   properties:
  #     base_uri: http://192.168.1.70
  #     username: admin
  #     password: secret

# -- Optimisation rules -----------------------------------------------
# Evaluated every `evaluation_interval` seconds (default 300 = 5 min).
# First matching condition wins. See docs/rules-dsl.md for the full DSL.

rules: []

  # Example: heat pump SG-Ready command.
  # Note: write SGReadyOpModeCmd (RW), not SGReadyState (read-only).
  # The values are the enum members declared in the SG-Ready profile.
  # - device: "Heat Pump"
  #   profile: "SG-ReadyStates"
  #   data_point: "SGReadyOpModeCmd"
  #   min_interval: 15
  #   conditions:
  #     - when: "has_surplus AND spot_price < 0.08"
  #       value: HP_FORCED       # surplus PV + cheap: heat the buffer
  #     - when: "spot_price < 0.12"
  #       value: HP_NORMAL
  #     - when: "spot_price > 0.25"
  #       value: HP_LOCKED
  #     - when: "is_peak AND NOT has_surplus"
  #       value: HP_LOCKED
  #     - default:
  #       value: HP_NORMAL

  # Example: EV current limit with optional SmoothTransition helper
  # sub-data-points. These writes are best-effort and silently skipped
  # when the device EID does not declare them.
  # - device: "Wallbox"
  #   profile: "EMS_Current_Limit"
  #   data_point: "EMSCurrentLimit"
  #   min_interval: 5
  #   smooth_transition:
  #     window: 0
  #     delay: 30
  #     duration: 0
  #   conditions:
  #     - when: "surplus_pv > 3000"
  #       value: 16
  #     - default:
  #       value: 8

# -- Vehicles (V2H / V2G) ---------------------------------------------
# Optional. Required to allow negative current targets (bidirectional
# charging). The rules engine refuses negative writes unless a vehicle
# explicitly enables v2h.

vehicles: []

# -- PV arrays (self-computed forecast) --------------------------------
# Optional. Declared independently of `devices:` — the array does not
# need to be an SGr-labelled/connected device. When at least one array
# is listed here, the add-on fetches solar irradiance from Open-Meteo
# every few hours and estimates production, filling `pv_forecast_kwh` /
# `pv_forecast_today_kwh` / `pv_forecast_next_4h_kwh` in the rule DSL
# whenever no `sensors.pv_forecast_kwh` entity is mapped (e.g. no
# Forecast.Solar integration installed). Home coordinates come from the
# add-on options `latitude`/`longitude`, or from HA's own location if
# left unset.
#
# pv_arrays:
#   - name: "Roof south"
#     kwp: 6.5              # nameplate peak power, kWp
#     tilt: 30               # degrees from horizontal (optional)
#     azimuth: 180            # 0=N, 90=E, 180=S, 270=W (optional)
#     efficiency: 0.80        # system losses factor (default 0.80)

pv_arrays: []

# -- Virtual devices (non-SGr, piloted via HA services) ----------------
# Optional. Lets `rules:` target ordinary Home Assistant entities
# (climate / switch / water_heater / number) with the same SG-Ready
# state literals used for real SGr devices (HP_LOCKED / HP_NORMAL /
# HP_INTENSIFIED / HP_FORCED) — one rule vocabulary for both. This is
# a Home Assistant orchestration layer, NOT native SmartGridready
# communication (see docs/scope-and-gaps.md §3.13). Reference the
# virtual device's `name:` as the rule's `device:` — no `eid` needed.
#
# virtual_devices:
#   # PAC without a digital SGr interface — offset from a base setpoint.
#   - name: "Heat pump (no SGr interface)"
#     type: climate_proxy
#     climate_entities: [climate.living_room]
#     base_setpoint_default: 21.0
#     min_setpoint: 16.0
#     max_setpoint: 24.0
#     sg_ready_to_offset:
#       HP_LOCKED: -3.0
#       HP_NORMAL: 0.0
#       HP_INTENSIFIED: 1.5
#       HP_FORCED: 3.0
#
#   # EV charger / boiler behind a simple relay.
#   - name: "Boiler (Shelly relay)"
#     type: switch_proxy
#     switch_entities: [switch.boiler]
#     sg_ready_to_switch:
#       HP_LOCKED: off
#       HP_NORMAL: off
#       HP_INTENSIFIED: on
#       HP_FORCED: on
#
#   # Water heater with its own thermostat entity.
#   - name: "Water heater"
#     type: boiler_proxy
#     water_heater_entity: water_heater.tank
#     min_temperature: 45.0
#     max_temperature: 65.0
#     sg_ready_to_temperature:
#       HP_LOCKED: 45
#       HP_NORMAL: 50
#       HP_INTENSIFIED: 55
#       HP_FORCED: 65
#
#   # Inverter export/charge limit or any numeric setpoint.
#   - name: "Inverter charge power limit"
#     type: number_proxy
#     number_entities: [number.inverter_charge_power_limit]
#     min_value: 0
#     max_value: 5000
#     sg_ready_to_value:
#       HP_LOCKED: 0
#       HP_NORMAL: 1500
#       HP_INTENSIFIED: 3000
#       HP_FORCED: 5000

virtual_devices: []
"""


# -----------------------------------------------------------------------------
# Loader
# -----------------------------------------------------------------------------

def ensure_example(config_path: Path) -> None:
    """Write a commented example next to the user's config if no file exists."""
    if config_path.exists():
        return
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create %s: %s", config_path.parent, exc)
        return
    example = config_path.with_name(EXAMPLE_CONFIG_NAME)
    try:
        example.write_text(EXAMPLE_YAML, encoding="utf-8")
        # Also drop a starter config so the user can edit in place.
        config_path.write_text(EXAMPLE_YAML, encoding="utf-8")
        logger.info("Wrote starter configuration at %s", config_path)
    except OSError as exc:
        logger.warning("Cannot write example config: %s", exc)


def load_user_config(config_path: Path) -> UserConfig:
    """Parse the user's YAML configuration."""
    if not config_path.exists():
        ensure_example(config_path)
        logger.warning(
            "Configuration file %s did not exist — wrote a starter example. "
            "Edit it and restart the add-on.",
            config_path,
        )
        return UserConfig()

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.error("Invalid YAML in %s: %s", config_path, exc)
        return UserConfig()

    if not isinstance(raw, dict):
        logger.error("Top-level of %s must be a mapping, got %s", config_path, type(raw).__name__)
        return UserConfig()

    sensors_raw = raw.get("sensors") or {}
    known_keys = {f for f in SensorMap.__dataclass_fields__ if f != "extra"}
    sensors = SensorMap(
        **{k: sensors_raw[k] for k in known_keys if k in sensors_raw},
        extra={k: v for k, v in sensors_raw.items() if k not in known_keys and isinstance(v, str)},
    )

    devices = [
        DeviceConfig(
            name=str(d.get("name", "")).strip(),
            eid=str(d.get("eid", "")).strip(),
            enabled=bool(d.get("enabled", True)),
            properties=dict(d.get("properties") or {}),
            evse_safety=dict(d.get("evse_safety") or {}),
        )
        for d in (raw.get("devices") or [])
        if isinstance(d, dict) and d.get("name") and d.get("eid")
    ]

    rules = [
        RuleConfig(
            device=str(r.get("device", "")).strip(),
            profile=str(r.get("profile", "")).strip(),
            data_point=str(r.get("data_point", "")).strip(),
            min_interval=int(r.get("min_interval", 15)),
            conditions=list(r.get("conditions") or []),
            smooth_transition=dict(r.get("smooth_transition") or {}),
        )
        for r in (raw.get("rules") or [])
        if isinstance(r, dict) and r.get("device") and r.get("profile") and r.get("data_point")
    ]

    vehicles = [
        VehicleConfig(
            name=str(v.get("name", "")).strip() or "ev",
            soc_entity=v.get("soc_entity"),
            plugged_entity=v.get("plugged_entity"),
            charging_power_entity=v.get("charging_power_entity"),
            charger_device=v.get("charger_device"),
            battery_capacity_kwh=float(v.get("battery_capacity_kwh") or 0.0),
            v2h=dict(v.get("v2h") or {}),
        )
        for v in (raw.get("vehicles") or [])
        if isinstance(v, dict)
    ]

    pv_arrays = []
    for a in (raw.get("pv_arrays") or []):
        if not isinstance(a, dict):
            continue
        try:
            kwp_val = float(a.get("kwp") or 0)
        except (TypeError, ValueError):
            continue
        if kwp_val <= 0:
            continue
        tilt = a.get("tilt")
        azimuth = a.get("azimuth")
        pv_arrays.append(PvArrayConfig(
            name=str(a.get("name", "")).strip() or "pv",
            kwp=kwp_val,
            tilt=float(tilt) if tilt is not None else None,
            azimuth=float(azimuth) if azimuth is not None else None,
            efficiency=float(a.get("efficiency", 0.80) or 0.80),
            enabled=bool(a.get("enabled", True)),
        ))

    virtual_devices = []
    for vd in (raw.get("virtual_devices") or []):
        if not isinstance(vd, dict):
            continue
        vtype = str(vd.get("type", "")).strip()
        if vtype not in VIRTUAL_DEVICE_TYPES:
            continue
        name = str(vd.get("name", "")).strip()
        if not name or not bool(vd.get("enabled", True)):
            continue

        if vtype == "boiler_proxy":
            wh = vd.get("water_heater_entity")
            targets = [wh] if wh else []
        else:
            targets = list(vd.get(_VIRTUAL_TARGET_KEYS[vtype]) or [])
        if not targets:
            continue

        raw_mapping = vd.get(_VIRTUAL_MAPPING_KEYS[vtype]) or {}
        mapping: Dict[str, Any] = {}
        for k, v in raw_mapping.items():
            key = str(k).strip().upper()
            if vtype == "switch_proxy":
                if isinstance(v, bool):
                    mapping[key] = v
                elif isinstance(v, str):
                    mapping[key] = v.strip().lower() in ("on", "true", "1", "yes")
                elif isinstance(v, (int, float)):
                    mapping[key] = v != 0
                else:
                    mapping[key] = False
            else:
                try:
                    mapping[key] = float(v)
                except (TypeError, ValueError):
                    continue

        # Bounds: boiler_proxy uses temperature-flavoured YAML keys for
        # readability (min_temperature/max_temperature); everything else
        # (number_proxy) uses the generic min_value/max_value. Both are
        # stored uniformly on VirtualDeviceConfig.min_value/max_value.
        if vtype == "boiler_proxy":
            min_val = float(vd.get("min_temperature", 45.0) or 45.0)
            max_val = float(vd.get("max_temperature", 65.0) or 65.0)
        else:
            min_val = float(vd.get("min_value", 0.0) or 0.0)
            max_val = float(vd.get("max_value", 100.0) or 100.0)

        virtual_devices.append(VirtualDeviceConfig(
            name=name,
            type=vtype,
            enabled=True,
            targets=targets,
            mapping=mapping,
            base_setpoint_default=float(vd.get("base_setpoint_default", 21.0) or 21.0),
            base_setpoint_entity=vd.get("base_setpoint_entity"),
            min_setpoint=float(vd.get("min_setpoint", 16.0) or 16.0),
            max_setpoint=float(vd.get("max_setpoint", 24.0) or 24.0),
            min_value=min_val,
            max_value=max_val,
        ))

    try:
        grid_limit = float(raw.get("grid_connection_limit_w") or 0)
    except (TypeError, ValueError):
        grid_limit = 0.0
    try:
        battery_kwh = float(raw.get("battery_capacity_kwh") or 0)
    except (TypeError, ValueError):
        battery_kwh = 0.0

    opt_raw = raw.get("optimizer") or {}
    optimizer = OptimizerConfig()
    if isinstance(opt_raw, dict):
        opt_devices = []
        for d in (opt_raw.get("devices") or []):
            if not isinstance(d, dict) or not d.get("name"):
                continue
            window = d.get("preferred_window")
            opt_devices.append(OptimizerDeviceConfig(
                name=str(d["name"]).strip(),
                min_power_w=float(d.get("min_power_w", 0) or 0),
                max_power_w=float(d.get("max_power_w", 3000) or 3000),
                must_run_hours=int(d.get("must_run_hours", 0) or 0),
                preferred_window=[int(window[0]), int(window[1])] if isinstance(window, list) and len(window) == 2 else None,
                priority=int(d.get("priority", 50) or 50),
                switchable=bool(d.get("switchable", False)),
                voltage=float(d.get("voltage", 230.0) or 230.0),
                phases=int(d.get("phases", 1) or 1),
            ))

        bat_raw = opt_raw.get("battery") or {}
        # Defaults to the top-level battery_capacity_kwh when not overridden,
        # so users don't have to declare the same capacity twice.
        battery = OptimizerBatteryConfig(
            capacity_kwh=float(bat_raw.get("capacity_kwh", battery_kwh) or 0),
            max_charge_w=float(bat_raw.get("max_charge_w", 0) or 0),
            max_discharge_w=float(bat_raw.get("max_discharge_w", 0) or 0),
            soc_min_pct=float(bat_raw.get("soc_min_pct", 10.0) or 10.0),
            soc_max_pct=float(bat_raw.get("soc_max_pct", 95.0) or 95.0),
            efficiency=float(bat_raw.get("efficiency", 0.95) or 0.95),
            cycle_cost_chf_kwh=float(bat_raw.get("cycle_cost_chf_kwh", 0.01) or 0.01),
        )

        grid_raw = opt_raw.get("grid") or {}
        # Defaults to the top-level grid_connection_limit_w when not
        # overridden — same reasoning as the battery capacity above.
        grid_cfg = OptimizerGridConfig(
            pcc_import_w=float(grid_raw.get("pcc_import_w", grid_limit) or grid_limit or 25000.0),
            pcc_export_w=float(grid_raw.get("pcc_export_w", 12000.0) or 12000.0),
            export_price_chf_kwh=float(grid_raw.get("export_price_chf_kwh", 0.08) or 0.08),
        )

        optimizer = OptimizerConfig(
            enabled=bool(opt_raw.get("enabled", False)),
            devices=opt_devices,
            battery=battery,
            grid=grid_cfg,
        )

    return UserConfig(
        sensors=sensors,
        enable_toggle=raw.get("enable_toggle"),
        devices=devices,
        rules=rules,
        vehicles=vehicles,
        pv_arrays=pv_arrays,
        virtual_devices=virtual_devices,
        optimizer=optimizer,
        grid_connection_limit_w=grid_limit,
        battery_capacity_kwh=battery_kwh,
        raw=raw,
    )
