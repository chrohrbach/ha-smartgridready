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
class UserConfig:
    sensors: SensorMap = field(default_factory=SensorMap)
    enable_toggle: Optional[str] = None
    devices: List[DeviceConfig] = field(default_factory=list)
    rules: List[RuleConfig] = field(default_factory=list)
    vehicles: List[VehicleConfig] = field(default_factory=list)
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
# SmartGridReady add-on configuration
# ----------------------------------------------------------------------
# Edit this file with the Home Assistant File Editor or VS Code add-on,
# then restart the SmartGridReady add-on.
#
# Devices are identified by their EID (External Interface Description)
# from the SmartGridReady product library:
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

    try:
        grid_limit = float(raw.get("grid_connection_limit_w") or 0)
    except (TypeError, ValueError):
        grid_limit = 0.0
    try:
        battery_kwh = float(raw.get("battery_capacity_kwh") or 0)
    except (TypeError, ValueError):
        battery_kwh = 0.0

    return UserConfig(
        sensors=sensors,
        enable_toggle=raw.get("enable_toggle"),
        devices=devices,
        rules=rules,
        vehicles=vehicles,
        grid_connection_limit_w=grid_limit,
        battery_capacity_kwh=battery_kwh,
        raw=raw,
    )
