"""Add-on options loader.

Reads ``/data/options.json`` (populated by Supervisor from the user's
add-on configuration) and exposes the values as a typed dataclass.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("smartgridready.options")

DEFAULT_OPTIONS_FILE = "/data/options.json"

DEFAULTS = {
    "config_path": "/addon_config/config.yaml",
    "evaluation_interval": 300,
    "log_level": "info",
    "mqtt_discovery": True,
    "mqtt_prefix": "smartgridready",
    "share_path": "/share/smartgridready",
    # Local time zone used for hour/day/window evaluation. Without this
    # the container could run in UTC and shift Swiss tariff windows by
    # an hour around DST.
    "timezone": "Europe/Zurich",
    # When true, the first evaluation tick is delayed so the cadence
    # aligns on HH:00 / HH:15 / HH:30 / HH:45 — matches the 15-minute
    # tariff slices used by most Swiss DSOs.
    "align_to_quarter": False,
    # SG-Ready BWP 1.1 spec: Mode 1 (HP_LOCKED) is limited to a
    # maximum of two hours per day across all utility lock events.
    # Set to 0 to disable the cap entirely (not recommended).
    "sg_ready_lock_cap_minutes": 120,
    # Home coordinates for the self-computed Open-Meteo PV forecast.
    # Left unset (None) by default — the add-on then falls back to
    # whatever HA itself reports via GET /api/config, which is the
    # location the user already configured during HA onboarding.
    # Only set these explicitly when the PV array is not at the same
    # coordinates as the HA installation (e.g. a detached barn/garage).
    "latitude": None,
    "longitude": None,
}


@dataclass(frozen=True)
class AddonOptions:
    config_path: Path
    evaluation_interval: int
    log_level: str
    mqtt_discovery: bool
    mqtt_prefix: str
    share_path: Path
    timezone: str
    align_to_quarter: bool
    sg_ready_lock_cap_minutes: int
    latitude: Optional[float]
    longitude: Optional[float]

    @property
    def audit_path(self) -> Path:
        return self.share_path / "audit.json"

    @property
    def cache_path(self) -> Path:
        return self.share_path / "cache"


def load_options(options_file: str | None = None) -> AddonOptions:
    path = options_file or os.environ.get("SGR_OPTIONS_FILE", DEFAULT_OPTIONS_FILE)
    raw: dict = {}
    if path and Path(path).exists():
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Cannot read add-on options at %s (%s) — using defaults", path, exc)
    else:
        logger.info("No add-on options file at %s — using defaults", path)

    merged = {**DEFAULTS, **{k: v for k, v in raw.items() if v is not None}}

    return AddonOptions(
        config_path=Path(merged["config_path"]),
        evaluation_interval=int(merged["evaluation_interval"]),
        log_level=str(merged["log_level"]).lower(),
        mqtt_discovery=bool(merged["mqtt_discovery"]),
        mqtt_prefix=str(merged["mqtt_prefix"]).strip("/"),
        share_path=Path(merged["share_path"]),
        timezone=str(merged["timezone"]).strip() or "Europe/Zurich",
        align_to_quarter=bool(merged["align_to_quarter"]),
        sg_ready_lock_cap_minutes=int(merged["sg_ready_lock_cap_minutes"]),
        latitude=float(merged["latitude"]) if merged.get("latitude") is not None else None,
        longitude=float(merged["longitude"]) if merged.get("longitude") is not None else None,
    )
