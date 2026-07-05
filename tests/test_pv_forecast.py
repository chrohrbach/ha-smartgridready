"""Tests for the self-computed PV forecast (Open-Meteo) module.

Covers the dependency-free solar-geometry helpers, the
``PvForecastService`` location-resolution / caching logic, and the
Open-Meteo fetch itself via ``httpx.MockTransport`` (no real network
call in CI).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import src.pv_forecast as pv_forecast
from src.config_loader import PvArrayConfig
from src.options import AddonOptions
from src.pv_forecast import PvForecastService, poa_irradiance_isotropic, solar_position


def make_options(latitude=None, longitude=None) -> AddonOptions:
    return AddonOptions(
        config_path=Path("/addon_config/config.yaml"),
        evaluation_interval=300,
        log_level="info",
        mqtt_discovery=False,
        mqtt_prefix="smartgridready",
        share_path=Path("/share/smartgridready"),
        timezone="Europe/Zurich",
        align_to_quarter=False,
        sg_ready_lock_cap_minutes=120,
        latitude=latitude,
        longitude=longitude,
    )


# ---------------------------------------------------------------------------
# Solar geometry
# ---------------------------------------------------------------------------

def test_solar_position_noon_faces_south_in_northern_hemisphere():
    # Zurich-ish coordinates. Civil clock noon isn't exactly solar noon
    # (Zurich sits west of the CEST reference meridian), so check a
    # broad daytime azimuth range rather than an exact 180°.
    dt = datetime(2026, 6, 21, 12, 0)
    elevation, azimuth = solar_position(dt, latitude=47.4, longitude=8.5, utc_offset_hours=2.0)
    assert elevation > 60  # high sun at noon in June
    assert 90 < azimuth < 270  # somewhere between due-east and due-west


def test_solar_position_midnight_below_horizon():
    dt = datetime(2026, 6, 21, 0, 0)
    elevation, _ = solar_position(dt, latitude=47.4, longitude=8.5, utc_offset_hours=2.0)
    assert elevation < 0


def test_poa_irradiance_zero_when_sun_below_horizon():
    result = poa_irradiance_isotropic(
        ghi=100, dni=200, dhi=50,
        tilt_deg=30, panel_azimuth_deg=180,
        elevation_deg=-5, sun_azimuth_deg=180,
    )
    assert result == 0.0


def test_poa_irradiance_positive_when_facing_sun():
    result = poa_irradiance_isotropic(
        ghi=500, dni=700, dhi=150,
        tilt_deg=30, panel_azimuth_deg=180,
        elevation_deg=45, sun_azimuth_deg=180,
    )
    assert result > 0


# ---------------------------------------------------------------------------
# PvForecastService — array registration
# ---------------------------------------------------------------------------

def test_load_arrays_filters_disabled_and_zero_kwp(tmp_path: Path):
    svc = PvForecastService(MagicMock(), tmp_path, make_options())
    arrays = [
        PvArrayConfig(name="A", kwp=5.0),
        PvArrayConfig(name="B", kwp=0.0),
        PvArrayConfig(name="C", kwp=3.0, enabled=False),
    ]
    count = svc.load_arrays(arrays)
    assert count == 1
    assert svc.enabled is True


def test_service_disabled_with_no_arrays(tmp_path: Path):
    svc = PvForecastService(MagicMock(), tmp_path, make_options())
    svc.load_arrays([])
    assert svc.enabled is False


# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_location_prefers_addon_options(tmp_path: Path):
    ha = MagicMock()
    ha.aget_config = AsyncMock(return_value={"latitude": 1.0, "longitude": 2.0})
    svc = PvForecastService(ha, tmp_path, make_options(latitude=47.0, longitude=8.0))
    location = await svc._resolve_location()
    assert location == (47.0, 8.0)
    ha.aget_config.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_location_falls_back_to_ha_config(tmp_path: Path):
    ha = MagicMock()
    ha.aget_config = AsyncMock(return_value={"latitude": 47.4, "longitude": 8.5})
    svc = PvForecastService(ha, tmp_path, make_options())
    location = await svc._resolve_location()
    assert location == (47.4, 8.5)
    # Persisted for future HA-unreachable fallback.
    assert (tmp_path / "ha_location.json").exists()


@pytest.mark.asyncio
async def test_resolve_location_uses_disk_cache_when_ha_unreachable(tmp_path: Path):
    cache = tmp_path / "ha_location.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"latitude": 46.9, "longitude": 7.4, "fetched_at": datetime.now().isoformat()}),
        encoding="utf-8",
    )
    ha = MagicMock()
    ha.aget_config = AsyncMock(return_value=None)
    svc = PvForecastService(ha, tmp_path, make_options())
    location = await svc._resolve_location()
    assert location == (46.9, 7.4)


@pytest.mark.asyncio
async def test_resolve_location_none_when_nothing_available(tmp_path: Path):
    ha = MagicMock()
    ha.aget_config = AsyncMock(return_value=None)
    svc = PvForecastService(ha, tmp_path, make_options())
    location = await svc._resolve_location()
    assert location is None


# ---------------------------------------------------------------------------
# Forecast cache
# ---------------------------------------------------------------------------

def test_get_summary_none_without_cache(tmp_path: Path):
    svc = PvForecastService(MagicMock(), tmp_path, make_options())
    assert svc.get_summary() is None


def test_get_summary_reads_persisted_cache(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pv_forecast.json").write_text(
        json.dumps({
            "fetched_at": datetime.now().isoformat(),
            "today_kwh": 12.3,
            "tomorrow_kwh": 15.0,
            "next_4h_kwh": 2.5,
        }),
        encoding="utf-8",
    )
    svc = PvForecastService(MagicMock(), tmp_path, make_options())
    summary = svc.get_summary()
    assert summary["today_kwh"] == 12.3
    assert summary["tomorrow_kwh"] == 15.0
    assert summary["next_4h_kwh"] == 2.5


@pytest.mark.asyncio
async def test_update_noop_when_disabled(tmp_path: Path):
    svc = PvForecastService(MagicMock(), tmp_path, make_options())
    svc.load_arrays([])
    assert await svc.update() is False


# ---------------------------------------------------------------------------
# update() — Open-Meteo fetch via httpx.MockTransport (no real network)
# ---------------------------------------------------------------------------

def _patch_async_client(monkeypatch, handler) -> None:
    """Route every ``httpx.AsyncClient()`` created by pv_forecast through a
    ``MockTransport`` instead of a real network connection.

    ``pv_forecast.httpx`` *is* the ``httpx`` module object (not a copy), so
    the replacement must call the ORIGINAL ``AsyncClient`` class captured
    before patching — otherwise it would recurse into itself.
    """
    original_async_client = httpx.AsyncClient

    def _fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(pv_forecast.httpx, "AsyncClient", _fake_async_client)


def _open_meteo_payload(hours: int = 48) -> dict:
    start = datetime.now().replace(minute=0, second=0, microsecond=0)
    times = [(start + timedelta(hours=h)).isoformat() for h in range(hours)]
    radiation = [500.0 if 6 <= (start + timedelta(hours=h)).hour <= 18 else 0.0 for h in range(hours)]
    return {
        "utc_offset_seconds": 3600,
        "hourly": {
            "time": times,
            "shortwave_radiation": radiation,
        },
    }


@pytest.mark.asyncio
async def test_update_fetches_and_persists_forecast(tmp_path: Path, monkeypatch):
    payload = _open_meteo_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "latitude=47.4" in str(request.url)
        assert "longitude=8.5" in str(request.url)
        return httpx.Response(200, json=payload)

    _patch_async_client(monkeypatch, handler)

    svc = PvForecastService(MagicMock(), tmp_path, make_options(latitude=47.4, longitude=8.5))
    svc.load_arrays([PvArrayConfig(name="Roof", kwp=5.0)])

    assert await svc.update() is True
    assert (tmp_path / "pv_forecast.json").exists()
    summary = svc.get_summary()
    assert summary is not None
    assert summary["today_kwh"] >= 0
    assert summary["tomorrow_kwh"] >= 0


@pytest.mark.asyncio
async def test_update_transposes_with_tilt_and_azimuth(tmp_path: Path, monkeypatch):
    payload = _open_meteo_payload()
    payload["hourly"]["direct_normal_irradiance"] = payload["hourly"]["shortwave_radiation"]
    payload["hourly"]["diffuse_radiation"] = [v * 0.3 for v in payload["hourly"]["shortwave_radiation"]]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "direct_normal_irradiance" in str(request.url)
        return httpx.Response(200, json=payload)

    _patch_async_client(monkeypatch, handler)

    svc = PvForecastService(MagicMock(), tmp_path, make_options(latitude=47.4, longitude=8.5))
    svc.load_arrays([PvArrayConfig(name="Roof", kwp=5.0, tilt=30, azimuth=180)])

    assert await svc.update() is True
    summary = svc.get_summary()
    assert summary["today_kwh"] >= 0


@pytest.mark.asyncio
async def test_update_returns_false_on_http_error(tmp_path: Path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream error")

    _patch_async_client(monkeypatch, handler)

    svc = PvForecastService(MagicMock(), tmp_path, make_options(latitude=47.4, longitude=8.5))
    svc.load_arrays([PvArrayConfig(name="Roof", kwp=5.0)])

    assert await svc.update() is False
    assert not (tmp_path / "pv_forecast.json").exists()


@pytest.mark.asyncio
async def test_update_returns_false_on_malformed_response(tmp_path: Path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hourly": {}})

    _patch_async_client(monkeypatch, handler)

    svc = PvForecastService(MagicMock(), tmp_path, make_options(latitude=47.4, longitude=8.5))
    svc.load_arrays([PvArrayConfig(name="Roof", kwp=5.0)])

    assert await svc.update() is False


@pytest.mark.asyncio
async def test_update_returns_false_when_no_location(tmp_path: Path, monkeypatch):
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_open_meteo_payload())

    _patch_async_client(monkeypatch, handler)

    ha = MagicMock()
    ha.aget_config = AsyncMock(return_value=None)
    svc = PvForecastService(ha, tmp_path, make_options())
    svc.load_arrays([PvArrayConfig(name="Roof", kwp=5.0)])

    assert await svc.update() is False
    assert called is False
