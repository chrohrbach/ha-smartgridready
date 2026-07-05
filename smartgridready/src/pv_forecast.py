"""Self-computed PV production forecast (Open-Meteo).

Provides a zero-dependency fallback PV forecast for installs that have
no Forecast.Solar (or similar) integration mapped under
``sensors.pv_forecast_kwh``. When the user declares one or more
``pv_arrays:`` in their configuration, this module:

  1. Resolves the home's coordinates — add-on options ``latitude`` /
     ``longitude`` first, otherwise HA's own ``GET /api/config``.
  2. Fetches hourly solar irradiance (global horizontal, plus direct
     normal / diffuse when at least one array declares ``tilt`` +
     ``azimuth``) from the free Open-Meteo API.
  3. Estimates each array's production, transposing to its own
     tilt/azimuth via the isotropic-sky plane-of-array model when
     available, otherwise using plain GHI.
  4. Persists the result to ``cache/pv_forecast.json`` so a restart or a
     transient network failure does not blank the forecast for up to
     ``CACHE_TTL_HOURS``.

The solar-position and irradiance-transposition math is a standard,
dependency-free approximation (Cooper's declination, Spencer equation
of time, Liu-Jordan isotropic diffuse model) — no external astronomy
library required.

Copyright note: math ported from the equivalent internal module used
in casasmooth (a sibling project by the same author), adapted here to
run standalone against HA's REST API instead of local storage.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .config_loader import PvArrayConfig
from .options import AddonOptions

logger = logging.getLogger("smartgridready.pv_forecast")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 15.0
CACHE_TTL_HOURS = 8  # stale forecast is still served, but flagged
LOCATION_CACHE_TTL_DAYS = 30  # HA's own location rarely changes


# -----------------------------------------------------------------------------
# Solar geometry (no external dependency)
# -----------------------------------------------------------------------------

def solar_position(
    dt: datetime, latitude: float, longitude: float, utc_offset_hours: float
) -> Tuple[float, float]:
    """Return (elevation_deg, azimuth_deg) of the sun at ``dt`` (naive local time).

    Simplified solar-geometry model (Cooper's declination + a
    Spencer-style equation-of-time approximation). Azimuth convention:
    0=N, 90=E, 180=S, 270=W (matches the ``pv_arrays.azimuth`` docs).
    """
    day_of_year = dt.timetuple().tm_yday
    declination = 23.45 * math.sin(math.radians(360.0 / 365.0 * (284 + day_of_year)))
    b = math.radians(360.0 / 365.0 * (day_of_year - 81))
    equation_of_time = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)  # minutes
    local_std_meridian = 15.0 * utc_offset_hours
    time_correction_min = 4.0 * (longitude - local_std_meridian) + equation_of_time
    local_hours = dt.hour + dt.minute / 60.0
    solar_time_hours = local_hours + time_correction_min / 60.0
    hour_angle_deg = 15.0 * (solar_time_hours - 12.0)

    lat_r = math.radians(latitude)
    decl_r = math.radians(declination)
    ha_r = math.radians(hour_angle_deg)

    sin_elev = math.sin(lat_r) * math.sin(decl_r) + math.cos(lat_r) * math.cos(decl_r) * math.cos(ha_r)
    sin_elev = max(-1.0, min(1.0, sin_elev))
    elevation_deg = math.degrees(math.asin(sin_elev))
    elev_r = math.radians(elevation_deg)

    denom = math.cos(elev_r) * math.cos(lat_r)
    if abs(denom) < 1e-9:
        azimuth_deg = 180.0
    else:
        cos_az = (math.sin(decl_r) - math.sin(elev_r) * math.sin(lat_r)) / denom
        cos_az = max(-1.0, min(1.0, cos_az))
        azimuth_deg = math.degrees(math.acos(cos_az))
        if hour_angle_deg > 0:
            azimuth_deg = 360.0 - azimuth_deg

    return elevation_deg, azimuth_deg


def poa_irradiance_isotropic(
    ghi: float,
    dni: float,
    dhi: float,
    tilt_deg: float,
    panel_azimuth_deg: float,
    elevation_deg: float,
    sun_azimuth_deg: float,
    albedo: float = 0.2,
) -> float:
    """Plane-of-array irradiance (W/m2) via the isotropic-sky diffuse model
    (Liu-Jordan). Returns 0 when the sun is below the horizon.
    """
    if elevation_deg <= 0:
        return 0.0
    zenith_r = math.radians(90.0 - elevation_deg)
    tilt_r = math.radians(tilt_deg)
    aoi_cos = (
        math.cos(zenith_r) * math.cos(tilt_r)
        + math.sin(zenith_r) * math.sin(tilt_r) * math.cos(math.radians(sun_azimuth_deg - panel_azimuth_deg))
    )
    aoi_cos = max(0.0, aoi_cos)
    beam = max(0.0, dni) * aoi_cos
    diffuse = max(0.0, dhi) * (1 + math.cos(tilt_r)) / 2.0
    ground_reflected = max(0.0, ghi) * albedo * (1 - math.cos(tilt_r)) / 2.0
    return beam + diffuse + ground_reflected


# -----------------------------------------------------------------------------
# Service
# -----------------------------------------------------------------------------

class PvForecastService:
    """Fetches and caches a self-computed PV production forecast.

    Lifecycle:
        1. ``load_arrays(user_config.pv_arrays)`` — once, at startup and
           on every config reload.
        2. ``await update()`` — periodically (every few hours; cheap and
           free, but there is no reason to hammer Open-Meteo every
           evaluation cycle).
        3. ``get_summary()`` — read the (possibly stale) cached result,
           used by the rules engine as a fallback context source.
    """

    def __init__(self, ha_client, cache_path: Path, options: AddonOptions):
        self.ha_client = ha_client
        self.cache_path = cache_path
        self.options = options
        self._arrays: List[PvArrayConfig] = []
        self._forecast_cache_path = cache_path / "pv_forecast.json"
        self._location_cache_path = cache_path / "ha_location.json"

    def load_arrays(self, arrays: List[PvArrayConfig]) -> int:
        self._arrays = [a for a in (arrays or []) if a.enabled and a.kwp > 0]
        if self._arrays:
            logger.info(
                "PV forecast: %d array(s) registered for internal Open-Meteo forecast",
                len(self._arrays),
            )
        return len(self._arrays)

    @property
    def enabled(self) -> bool:
        return bool(self._arrays)

    # ------------------------------------------------------------------
    # Location resolution
    # ------------------------------------------------------------------

    async def _resolve_location(self) -> Optional[Tuple[float, float]]:
        """Add-on options first, then HA's own config, then disk cache."""
        if self.options.latitude is not None and self.options.longitude is not None:
            return self.options.latitude, self.options.longitude

        cfg = await self._aget_ha_config()
        if cfg and cfg.get("latitude") is not None and cfg.get("longitude") is not None:
            try:
                lat, lon = float(cfg["latitude"]), float(cfg["longitude"])
            except (TypeError, ValueError):
                lat = lon = None
            if lat is not None:
                self._persist_location_cache(lat, lon)
                return lat, lon

        cached = self._load_location_cache()
        if cached:
            logger.info("PV forecast: using cached HA location (HA unreachable)")
            return cached

        logger.warning(
            "PV forecast: no latitude/longitude available (options unset, "
            "HA unreachable, no cache) — skipping update"
        )
        return None

    async def _aget_ha_config(self) -> Optional[Dict[str, Any]]:
        get_config = getattr(self.ha_client, "aget_config", None)
        if get_config is not None:
            return await get_config()
        # Sync fallback for simplified/mocked clients in tests.
        sync_get = getattr(self.ha_client, "get_config", None)
        return sync_get() if sync_get else None

    def _load_location_cache(self) -> Optional[Tuple[float, float]]:
        if not self._location_cache_path.exists():
            return None
        try:
            data = json.loads(self._location_cache_path.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(data["fetched_at"])
            if (datetime.now() - fetched).total_seconds() > LOCATION_CACHE_TTL_DAYS * 86400:
                return None
            return float(data["latitude"]), float(data["longitude"])
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None

    def _persist_location_cache(self, latitude: float, longitude: float) -> None:
        try:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            payload = {
                "latitude": latitude,
                "longitude": longitude,
                "fetched_at": datetime.now().isoformat(),
            }
            tmp = self._location_cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._location_cache_path)
        except OSError as exc:
            logger.debug("PV forecast: cannot persist location cache: %s", exc)

    # ------------------------------------------------------------------
    # Forecast update
    # ------------------------------------------------------------------

    async def update(self) -> bool:
        """Fetch Open-Meteo irradiance and refresh the cached forecast."""
        if not self.enabled:
            return False

        location = await self._resolve_location()
        if not location:
            return False
        latitude, longitude = location

        any_transposition = any(a.tilt is not None and a.azimuth is not None for a in self._arrays)
        hourly_vars = "shortwave_radiation"
        if any_transposition:
            hourly_vars += ",direct_normal_irradiance,diffuse_radiation"

        params = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": hourly_vars,
            "forecast_days": 2,
            "timezone": "auto",
        }
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                r = await client.get(OPEN_METEO_URL, params=params)
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            logger.warning("PV forecast: Open-Meteo fetch failed: %s", exc)
            return False

        hourly = data.get("hourly", {}) or {}
        times = hourly.get("time", [])
        radiation = hourly.get("shortwave_radiation", [])
        dni_series = hourly.get("direct_normal_irradiance", [])
        dhi_series = hourly.get("diffuse_radiation", [])
        utc_offset_hours = float(data.get("utc_offset_seconds", 0)) / 3600.0
        if not times or len(times) != len(radiation):
            logger.warning("PV forecast: unexpected Open-Meteo response shape")
            return False
        dni_dhi_available = len(dni_series) == len(times) and len(dhi_series) == len(times)
        if any_transposition and not dni_dhi_available:
            logger.warning(
                "PV forecast: missing DNI/DHI series — falling back to GHI-only"
            )

        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        four_h_limit = now + timedelta(hours=4)

        today_kwh = 0.0
        tomorrow_kwh = 0.0
        next_4h_kwh = 0.0
        hourly_slots: List[Dict[str, Any]] = []

        for idx, (t_str, rad) in enumerate(zip(times, radiation, strict=False)):
            if rad is None:
                continue
            rad_val = float(rad)
            try:
                slot_dt = datetime.fromisoformat(t_str)
            except ValueError:
                slot_dt = None

            elevation_deg = sun_azimuth_deg = None
            dni_val = dni_series[idx] if idx < len(dni_series) else None
            dhi_val = dhi_series[idx] if idx < len(dhi_series) else None
            if (
                dni_dhi_available and slot_dt is not None
                and dni_val is not None and dhi_val is not None
            ):
                elevation_deg, sun_azimuth_deg = solar_position(
                    slot_dt, latitude, longitude, utc_offset_hours
                )

            slot_kwh = 0.0
            for arr in self._arrays:
                use_transposition = (
                    arr.tilt is not None and arr.azimuth is not None
                    and elevation_deg is not None
                )
                if use_transposition:
                    effective_irradiance = poa_irradiance_isotropic(
                        ghi=rad_val, dni=float(dni_val), dhi=float(dhi_val),
                        tilt_deg=arr.tilt, panel_azimuth_deg=arr.azimuth,
                        elevation_deg=elevation_deg, sun_azimuth_deg=sun_azimuth_deg,
                    )
                else:
                    effective_irradiance = rad_val
                slot_kwh += effective_irradiance * arr.kwp * arr.efficiency / 1000.0

            hourly_slots.append({"time": t_str, "kwh": round(slot_kwh, 3)})
            if t_str.startswith(today_str):
                today_kwh += slot_kwh
            elif t_str.startswith(tomorrow_str):
                tomorrow_kwh += slot_kwh
            if slot_dt is not None and now <= slot_dt < four_h_limit:
                next_4h_kwh += slot_kwh

        payload = {
            "fetched_at": now.isoformat(),
            "source": "open-meteo",
            "arrays": [
                {"name": a.name, "kwp": a.kwp, "tilt": a.tilt, "azimuth": a.azimuth, "efficiency": a.efficiency}
                for a in self._arrays
            ],
            "today_kwh": round(today_kwh, 2),
            "tomorrow_kwh": round(tomorrow_kwh, 2),
            "next_4h_kwh": round(next_4h_kwh, 2),
            "hourly": hourly_slots,
        }
        try:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            tmp = self._forecast_cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._forecast_cache_path)
        except OSError as exc:
            logger.warning("PV forecast: cannot persist forecast cache: %s", exc)
            return False

        logger.info(
            "PV forecast updated (%d array(s)): today=%.1f kWh, tomorrow=%.1f kWh, next_4h=%.1f kWh",
            len(self._arrays), today_kwh, tomorrow_kwh, next_4h_kwh,
        )
        return True

    # ------------------------------------------------------------------
    # Consumption
    # ------------------------------------------------------------------

    def get_summary(self) -> Optional[Dict[str, float]]:
        """Return the cached forecast (today/tomorrow/next_4h kWh), or None.

        Returns ``None`` when there is no cache yet (first boot, before
        the first successful update). A *stale* cache (older than
        ``CACHE_TTL_HOURS``) is still returned — a several-hours-old PV
        forecast is a much better fallback than none at all — but the
        caller can inspect ``fetched_at`` if freshness matters.
        """
        if not self._forecast_cache_path.exists():
            return None
        try:
            data = json.loads(self._forecast_cache_path.read_text(encoding="utf-8"))
            return {
                "today_kwh": float(data.get("today_kwh", 0.0)),
                "tomorrow_kwh": float(data.get("tomorrow_kwh", 0.0)),
                "next_4h_kwh": float(data.get("next_4h_kwh", 0.0)),
                "fetched_at": data.get("fetched_at", ""),
            }
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None

    def get_hourly_forecast_w(self, hours: int = 24) -> Optional[List[float]]:
        """Return the next ``hours`` hourly PV production estimates (W),
        starting at the current hour, from the cached Open-Meteo forecast.

        Used by the predictive-dispatch optimizer as its PV input series.
        Returns ``None`` when there is no cache yet. Hours with no matching
        cached slot (e.g. beyond the fetched 48h horizon) are treated as 0 W
        rather than carrying the last known value forward — unlike price,
        solar production does not persist from hour to hour.
        """
        if not self._forecast_cache_path.exists():
            return None
        try:
            data = json.loads(self._forecast_cache_path.read_text(encoding="utf-8"))
            hourly = data.get("hourly") or []
        except (json.JSONDecodeError, OSError):
            return None
        if not hourly:
            return None

        by_hour: Dict[datetime, float] = {}
        for slot in hourly:
            try:
                ts = datetime.fromisoformat(slot["time"]).replace(minute=0, second=0, microsecond=0)
                by_hour[ts] = float(slot.get("kwh", 0.0)) * 1000.0
            except (KeyError, ValueError, TypeError):
                continue

        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        return [by_hour.get(now + timedelta(hours=h), 0.0) for h in range(hours)]
