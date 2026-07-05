"""SmartGridready add-on entry point.

Orchestrates the lifecycle of all components:

  1. Load add-on options from ``/data/options.json``.
  2. Load the user's YAML configuration (devices + rules + sensors).
  3. Connect every enabled SGr device.
  4. Optionally connect to the HA MQTT broker and publish discovery.
  5. Start the ingress FastAPI app (port 8099).
  6. Run an evaluation loop on the configured cadence.
  7. Periodically publish state to MQTT.
  8. On shutdown, publish ``offline`` and disconnect cleanly.

The whole thing runs in a single asyncio event loop. Graceful shutdown
on SIGTERM / SIGINT.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import uvicorn

from . import __version__
from .config_loader import UserConfig, load_user_config
from .ha_client import HomeAssistantClient
from .mqtt_discovery import MqttBridge
from .optimizer import SGrOptimizer
from .options import AddonOptions, load_options
from .pv_forecast import PvForecastService
from .rules_engine import RulesEngine
from .sgr_service import SGrService
from .virtual_devices import VirtualDeviceManager
from .webui import build_app

LOG_LEVEL_MAP = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


@dataclass
class AppState:
    version: str = __version__
    options: AddonOptions = field(default_factory=load_options)
    ha_client: Optional[HomeAssistantClient] = None
    sgr_service: Optional[SGrService] = None
    mqtt_bridge: Optional[MqttBridge] = None
    rules_engine: Optional[RulesEngine] = None
    pv_forecast_service: Optional[PvForecastService] = None
    virtual_devices: Optional[VirtualDeviceManager] = None
    optimizer: Optional[SGrOptimizer] = None
    user_config: Optional[UserConfig] = None
    shutdown_event: Optional[asyncio.Event] = None


def configure_logging(level_name: str) -> None:
    level = LOG_LEVEL_MAP.get((level_name or "info").lower(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Silence overly chatty third-party loggers.
    for noisy in ("httpx", "httpcore", "asyncio_mqtt", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


async def evaluation_loop(state: AppState) -> None:
    log = logging.getLogger("smartgridready.loop")
    assert state.rules_engine is not None
    assert state.user_config is not None
    assert state.shutdown_event is not None
    interval = state.options.evaluation_interval

    # Initial small delay so devices have time to settle on HA boot.
    # When `align_to_quarter` is on we hold the first tick until the
    # next HH:00 / HH:15 / HH:30 / HH:45 in the engine's local zone so
    # subsequent evaluations sit at the same boundary as the typical
    # Swiss 15-minute tariff slices.
    initial_delay = min(60, interval)
    if state.options.align_to_quarter:
        now_local = state.rules_engine._now_local()
        seconds_into_quarter = (now_local.minute % 15) * 60 + now_local.second
        align_delay = (15 * 60) - seconds_into_quarter
        # Take whichever delay puts us at the next quarter without
        # waiting more than one interval — booting on HH:14:55 should
        # not blackhole evaluation for 14 minutes.
        initial_delay = min(max(initial_delay, align_delay), interval)
    log.info("Evaluation loop starting (interval=%ds, first run in %ds)", interval, initial_delay)
    try:
        await asyncio.wait_for(state.shutdown_event.wait(), timeout=initial_delay)
        return
    except asyncio.TimeoutError:
        pass

    while not state.shutdown_event.is_set():
        try:
            await state.rules_engine.evaluate(state.user_config)
        except Exception:
            log.exception("Evaluation cycle crashed — continuing")
        if state.mqtt_bridge and state.mqtt_bridge.enabled:
            try:
                await state.mqtt_bridge.publish_states()
            except Exception:
                log.exception("MQTT publish failed — continuing")
        try:
            await asyncio.wait_for(state.shutdown_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            continue


PV_FORECAST_INTERVAL_SECONDS = 6 * 3600  # Open-Meteo is free but there is no
# reason to poll it every evaluation cycle — solar irradiance forecasts
# don't change meaningfully faster than a few times a day.


async def pv_forecast_loop(state: AppState) -> None:
    """Periodically refresh the self-computed PV forecast (Open-Meteo).

    No-op when the user has not declared any ``pv_arrays:`` — the initial
    ``update()`` call returns False immediately in that case, cheaply.
    """
    log = logging.getLogger("smartgridready.pv_forecast_loop")
    assert state.pv_forecast_service is not None
    assert state.shutdown_event is not None
    if not state.pv_forecast_service.enabled:
        return

    # Small initial delay so HA/the network have time to settle on boot,
    # then fetch immediately rather than waiting a full interval.
    try:
        await asyncio.wait_for(state.shutdown_event.wait(), timeout=30)
        return
    except asyncio.TimeoutError:
        pass

    while not state.shutdown_event.is_set():
        try:
            await state.pv_forecast_service.update()
        except Exception:
            log.exception("PV forecast update crashed — continuing")
        try:
            await asyncio.wait_for(
                state.shutdown_event.wait(), timeout=PV_FORECAST_INTERVAL_SECONDS
            )
            return
        except asyncio.TimeoutError:
            continue


OPTIMIZER_INTERVAL_SECONDS = 30 * 60  # price/PV forecasts and battery SoC
# move faster than PV irradiance but still don't need a fresh 24h schedule
# every 5-min evaluation cycle.


async def optimizer_loop(state: AppState) -> None:
    """Periodically recompute the predictive-dispatch schedule (opt-in).

    No-op when the user has not enabled ``optimizer:`` / declared any
    ``optimizer.devices`` — ``run_optimizer()`` returns None immediately.
    """
    log = logging.getLogger("smartgridready.optimizer_loop")
    assert state.rules_engine is not None
    assert state.user_config is not None
    assert state.shutdown_event is not None
    if not state.optimizer or not state.optimizer.enabled:
        return

    # Small initial delay so devices/HA have settled, then compute
    # immediately rather than waiting a full interval.
    try:
        await asyncio.wait_for(state.shutdown_event.wait(), timeout=45)
        return
    except asyncio.TimeoutError:
        pass

    while not state.shutdown_event.is_set():
        try:
            await state.rules_engine.run_optimizer(state.user_config)
        except Exception:
            log.exception("Optimizer cycle crashed — continuing")
        try:
            await asyncio.wait_for(
                state.shutdown_event.wait(), timeout=OPTIMIZER_INTERVAL_SECONDS
            )
            return
        except asyncio.TimeoutError:
            continue


async def run() -> int:
    state = AppState()
    configure_logging(state.options.log_level)
    log = logging.getLogger("smartgridready")
    log.info("Starting SmartGridready add-on v%s", state.version)

    # Prepare paths.
    state.options.share_path.mkdir(parents=True, exist_ok=True)
    state.options.cache_path.mkdir(parents=True, exist_ok=True)

    # Load user config (writes example if missing).
    state.user_config = load_user_config(state.options.config_path)
    log.info(
        "Loaded user config: %d device(s), %d rule(s), %d vehicle(s), %d virtual device(s), %d optimizer device(s)",
        len(state.user_config.devices),
        len(state.user_config.rules),
        len(state.user_config.vehicles),
        len(state.user_config.virtual_devices),
        len(state.user_config.optimizer.devices) if state.user_config.optimizer.enabled else 0,
    )

    # Build core services.
    state.ha_client = HomeAssistantClient()
    state.sgr_service = SGrService(state.options.cache_path)
    state.pv_forecast_service = PvForecastService(
        state.ha_client, state.options.cache_path, state.options
    )
    state.pv_forecast_service.load_arrays(state.user_config.pv_arrays)
    state.virtual_devices = VirtualDeviceManager()
    state.virtual_devices.load(state.user_config.virtual_devices)
    state.optimizer = SGrOptimizer(state.options.cache_path)
    if state.user_config.optimizer.enabled:
        state.optimizer.load(
            state.user_config.optimizer.devices,
            state.user_config.optimizer.battery,
            state.user_config.optimizer.grid,
        )
    state.rules_engine = RulesEngine(
        state.sgr_service,
        state.ha_client,
        state.options.audit_path,
        tz_name=state.options.timezone,
        sg_ready_lock_cap_minutes=state.options.sg_ready_lock_cap_minutes,
        pv_forecast_service=state.pv_forecast_service,
        virtual_devices=state.virtual_devices,
        optimizer=state.optimizer,
    )

    # Connect devices (best-effort: failures do not abort the add-on).
    await state.sgr_service.connect_all(state.user_config)

    # Optional MQTT discovery.
    if state.options.mqtt_discovery:
        state.mqtt_bridge = MqttBridge(state.sgr_service, prefix=state.options.mqtt_prefix)
        if await state.mqtt_bridge.connect():
            await state.mqtt_bridge.announce_all()
            asyncio.create_task(state.mqtt_bridge.command_loop())
        else:
            log.info("MQTT discovery skipped (no broker or asyncio-mqtt missing)")

    # Wire shutdown signals.
    state.shutdown_event = asyncio.Event()

    def _on_signal() -> None:
        log.info("Shutdown signal received")
        if state.shutdown_event is not None:
            state.shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread fallback
            signal.signal(sig, lambda *_: _on_signal())

    # Ingress web UI.
    app = build_app(state)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8099,
        log_level=state.options.log_level if state.options.log_level in ("debug", "info", "warning", "error") else "info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    web_task = asyncio.create_task(server.serve(), name="webui")
    eval_task = asyncio.create_task(evaluation_loop(state), name="evaluation")
    pv_forecast_task = asyncio.create_task(pv_forecast_loop(state), name="pv_forecast")
    optimizer_task = asyncio.create_task(optimizer_loop(state), name="optimizer")

    # Wait for shutdown signal.
    await state.shutdown_event.wait()
    log.info("Shutting down…")

    # Graceful drain.
    server.should_exit = True
    eval_task.cancel()
    pv_forecast_task.cancel()
    optimizer_task.cancel()
    try:
        await eval_task
    except asyncio.CancelledError:
        pass
    try:
        await pv_forecast_task
    except asyncio.CancelledError:
        pass
    try:
        await optimizer_task
    except asyncio.CancelledError:
        pass
    try:
        await web_task
    except Exception:
        pass

    if state.mqtt_bridge and state.mqtt_bridge.enabled:
        await state.mqtt_bridge.publish_offline()
        await state.mqtt_bridge.disconnect()
    await state.sgr_service.disconnect_all()
    if state.ha_client is not None:
        state.ha_client.close()
        await state.ha_client.aclose()

    log.info("Bye.")
    return 0


def main() -> None:
    try:
        rc = asyncio.run(run())
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
