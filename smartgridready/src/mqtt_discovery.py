"""MQTT discovery bridge.

Publishes one MQTT discovery message per data point of every connected
SGr device. Home Assistant picks them up automatically and creates the
matching entities. The mapping respects the EID-declared semantics:

- read-only ``BOOLEAN``                       → ``binary_sensor``
- read-only ``INT`` / ``FLOAT`` / ``STRING``  → ``sensor`` (with unit if known)
- read-only ``ENUM``                          → ``sensor`` (publishes the literal)
- writable ``BOOLEAN``                        → ``switch``
- writable ``INT`` / ``FLOAT``                → ``number`` (min/max/step/unit from EID)
- writable ``ENUM``                           → ``select`` (options from EID enum literals)
- writable ``STRING``                         → ``text``

Read-only data points are updated once per evaluation cycle by
``publish_states``. Writable data points subscribe to a command topic;
when the user changes the entity in HA, the bridge casts the payload
according to the EID-declared type and writes it through the SGr service.

The connection metadata (MQTT host, port, user, password) is read from
the Supervisor Services API exposed via ``$SUPERVISOR_TOKEN`` — no
broker credentials in the user's config file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("smartgridready.mqtt")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "x"


# -----------------------------------------------------------------------------
# SGr Units → Home Assistant unit_of_measurement strings.
# -----------------------------------------------------------------------------

UNIT_MAP: Dict[str, str] = {
    "AMPERES": "A",
    "VOLTS": "V",
    "WATTS": "W",
    "KILOWATTS": "kW",
    "WATT_HOURS": "Wh",
    "KILOWATT_HOURS": "kWh",
    "MEGAWATT_HOURS": "MWh",
    "HERTZ": "Hz",
    "OHMS": "Ω",
    "DEGREES_CELSIUS": "°C",
    "DEGREES_KELVIN": "K",
    "PERCENT": "%",
    "PERCENT_RELATIVE_HUMIDITY": "%",
    "BARS": "bar",
    "PASCALS": "Pa",
    "HOURS": "h",
    "MINUTES": "min",
    "SECONDS": "s",
    "CUBIC_METERS": "m³",
    "CUBIC_METERS_PER_SECOND": "m³/s",
    "METERS": "m",
    "SQUARE_METERS": "m²",
    "METERS_PER_SECOND": "m/s",
    "METERS_PER_SECOND_PER_SECOND": "m/s²",
    "KILOGRAMS": "kg",
    "JOULES": "J",
    "VOLT_AMPERES": "VA",
    "VOLT_AMPERES_REACTIVE": "var",
    "KILOVOLT_AMPERES": "kVA",
    "KILOVOLT_AMPERES_REACTIVE": "kvar",
    "KILOVOLT_AMPERE_HOURS": "kVAh",
    "KILOVOLT_AMPERES_REACTIVE_HOURS": "kvarh",
    "PARTS_PER_MILLION": "ppm",
    "REVOLUTIONS_PER_MINUTE": "rpm",
    "RADIANS": "rad",
    "RADIANS_PER_SECOND": "rad/s",
    "DEGREES_PHASE": "°",
    "PER_HOUR": "/h",
    "WATTS_PER_SQUARE_METER": "W/m²",
}

DEVICE_CLASS_MAP: Dict[str, str] = {
    "AMPERES": "current",
    "VOLTS": "voltage",
    "WATTS": "power",
    "KILOWATTS": "power",
    "WATT_HOURS": "energy",
    "KILOWATT_HOURS": "energy",
    "MEGAWATT_HOURS": "energy",
    "HERTZ": "frequency",
    "DEGREES_CELSIUS": "temperature",
    "DEGREES_KELVIN": "temperature",
    "PERCENT_RELATIVE_HUMIDITY": "humidity",
    "PASCALS": "pressure",
    "BARS": "pressure",
}


def _enum_name(obj: Any) -> Optional[str]:
    """Return the canonical name of an Enum-like object (or None)."""
    if obj is None:
        return None
    for attr in ("name", "value"):
        v = getattr(obj, attr, None)
        if isinstance(v, str) and v:
            return v
    return str(obj) or None


def ha_unit(unit: Any) -> Optional[str]:
    name = _enum_name(unit)
    if not name:
        return None
    return UNIT_MAP.get(name)


def ha_device_class(unit: Any) -> Optional[str]:
    name = _enum_name(unit)
    if not name:
        return None
    return DEVICE_CLASS_MAP.get(name)


# -----------------------------------------------------------------------------
# Component selection and value casting based on SGr (direction, data_type).
# -----------------------------------------------------------------------------

def _is_writable(direction_name: str) -> bool:
    return "W" in (direction_name or "")


def ha_component(direction: Any, data_type: Any) -> str:
    """Pick the HA MQTT-discovery component for a data point."""
    d = _enum_name(direction) or "R"
    t = _enum_name(data_type) or "UNDEFINED"
    writable = _is_writable(d)
    if writable:
        if t == "ENUM":
            return "select"
        if t == "BOOLEAN":
            return "switch"
        if t in ("INT", "FLOAT"):
            return "number"
        if t == "STRING":
            return "text"
        return "sensor"  # unknown writable type: fall back to read-only
    if t == "BOOLEAN":
        return "binary_sensor"
    return "sensor"


def format_state_payload(value: Any, data_type: Any) -> str:
    """Encode a value read from SGr for the MQTT state topic."""
    if value is None:
        return ""
    t = _enum_name(data_type) or "UNDEFINED"
    if t == "BOOLEAN":
        return "ON" if bool(value) else "OFF"
    return str(value)


def cast_command(payload: str, data_type: Any) -> Any:
    """Cast an incoming MQTT command payload according to the EID type."""
    s = (payload or "").strip()
    t = _enum_name(data_type) or "UNDEFINED"
    if t == "BOOLEAN":
        return s.upper() in ("ON", "TRUE", "1", "YES")
    if t == "INT":
        return int(float(s))
    if t == "FLOAT":
        return float(s)
    # ENUM, STRING, JSON, DATE_TIME, BITMAP, UNDEFINED → keep as string
    return s


def enum_options(dp: Any) -> List[str]:
    """Return the list of literal enum members declared on a data point."""
    try:
        opts = dp.options() or []
    except Exception:
        return []
    out: List[str] = []
    for entry in opts:
        if isinstance(entry, (tuple, list)) and entry:
            literal = entry[0]
        else:
            literal = entry
        if literal:
            out.append(str(literal))
    return out


def numeric_bounds(dp: Any) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return ``(minimum, maximum, step)`` for a data point if declared."""
    try:
        spec = dp.get_specification()
        desc = getattr(spec, "data_point", None)
        if desc is None:
            return None, None, None
        mn = getattr(desc, "minimum_value", None)
        mx = getattr(desc, "maximum_value", None)
        mult = getattr(desc, "unit_conversion_multiplicator", None)
        return mn, mx, mult
    except Exception:
        return None, None, None


# -----------------------------------------------------------------------------
# Supervisor lookup
# -----------------------------------------------------------------------------

async def fetch_mqtt_credentials(token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Ask the Supervisor for MQTT broker credentials."""
    token = token or os.environ.get("SUPERVISOR_TOKEN") or ""
    if not token:
        return None
    try:
        async with httpx.AsyncClient(
            base_url="http://supervisor",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        ) as client:
            r = await client.get("/services/mqtt")
            if r.status_code == 404:
                logger.info("Supervisor reports no MQTT service available")
                return None
            r.raise_for_status()
            data = r.json().get("data") or {}
            if not data.get("host"):
                return None
            return data
    except Exception as exc:
        logger.warning("Cannot fetch MQTT credentials from Supervisor: %s", exc)
        return None


# -----------------------------------------------------------------------------
# Bridge
# -----------------------------------------------------------------------------

class MqttBridge:
    """Publish SGr data points to Home Assistant via MQTT discovery."""

    def __init__(self, sgr_service, prefix: str = "smartgridready"):
        self.sgr = sgr_service
        self.prefix = prefix.strip("/")
        self._client = None
        self._published_discovery: set[str] = set()
        self._enabled = False
        self._stop_event: Optional[asyncio.Event] = None
        # (device_slug, dp_slug) → (device_name, fp_name, dp_name, DataPoint, component)
        self._dp_lookup: Dict[Tuple[str, str], Tuple[str, str, str, Any, str]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def connect(self) -> bool:
        try:
            from asyncio_mqtt import Client
        except ImportError:
            logger.warning("asyncio-mqtt not installed — MQTT discovery disabled")
            return False

        creds = await fetch_mqtt_credentials()
        if not creds:
            return False

        try:
            self._client = Client(
                hostname=creds["host"],
                port=int(creds.get("port", 1883)),
                username=creds.get("username"),
                password=creds.get("password"),
                client_id=f"{self.prefix}-addon",
            )
            await self._client.__aenter__()
            self._enabled = True
            logger.info("Connected to MQTT broker %s:%s", creds["host"], creds.get("port", 1883))
            return True
        except Exception as exc:
            logger.warning("MQTT connect failed: %s", exc)
            self._client = None
            return False

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None
        self._enabled = False
        if self._stop_event is not None:
            self._stop_event.set()

    # ------------------------------------------------------------------
    # Topic & id helpers
    # ------------------------------------------------------------------

    def _device_slug(self, device_name: str) -> str:
        return slug(device_name)

    def _dp_slug(self, dp_name: str) -> str:
        return slug(dp_name)

    def _unique_id(self, device_name: str, dp_name: str) -> str:
        return f"{self.prefix}_{slug(device_name)}_{slug(dp_name)}"

    def _state_topic(self, device_name: str, dp_name: str) -> str:
        return f"{self.prefix}/{slug(device_name)}/{slug(dp_name)}/state"

    def _command_topic(self, device_name: str, dp_name: str) -> str:
        return f"{self.prefix}/{slug(device_name)}/{slug(dp_name)}/set"

    def _availability_topic(self) -> str:
        return f"{self.prefix}/status"

    # ------------------------------------------------------------------
    # Discovery publishing
    # ------------------------------------------------------------------

    async def announce_all(self) -> int:
        """Publish discovery messages for every data point — once per session."""
        if not self._enabled or self._client is None:
            return 0
        count = 0
        self._dp_lookup.clear()
        for device_name, fp_name, dp_name, dp in self.sgr.iter_datapoint_objects():
            key = f"{device_name}/{fp_name}/{dp_name}"
            try:
                direction = dp.direction()
                data_type = dp.data_type()
            except Exception as exc:
                logger.debug("Skipping %s — cannot read direction/type: %s", key, exc)
                continue

            component = ha_component(direction, data_type)
            self._dp_lookup[(slug(device_name), slug(dp_name))] = (
                device_name, fp_name, dp_name, dp, component,
            )

            if key in self._published_discovery:
                continue

            await self._publish_discovery(device_name, fp_name, dp_name, dp, component)
            self._published_discovery.add(key)
            count += 1
        if count:
            logger.info("Published %d MQTT discovery messages", count)
        return count

    async def _publish_discovery(
        self,
        device_name: str,
        fp_name: str,
        dp_name: str,
        dp: Any,
        component: str,
    ) -> None:
        unique_id = self._unique_id(device_name, dp_name)
        topic = f"homeassistant/{component}/{unique_id}/config"

        device_block = {
            "identifiers": [f"{self.prefix}_{slug(device_name)}"],
            "name": device_name,
            "manufacturer": "SmartGridReady",
            "model": device_name,
        }
        payload: Dict[str, Any] = {
            "name": f"{device_name} {dp_name}",
            "unique_id": unique_id,
            "state_topic": self._state_topic(device_name, dp_name),
            "device": device_block,
            "availability_topic": self._availability_topic(),
        }

        unit = ha_unit(dp.unit()) if hasattr(dp, "unit") else None
        device_class = ha_device_class(dp.unit()) if hasattr(dp, "unit") else None

        if component == "sensor":
            if unit:
                payload["unit_of_measurement"] = unit
            if device_class:
                payload["device_class"] = device_class
                payload["state_class"] = "measurement"
        elif component == "binary_sensor":
            payload["payload_on"] = "ON"
            payload["payload_off"] = "OFF"
        elif component == "number":
            payload["command_topic"] = self._command_topic(device_name, dp_name)
            mn, mx, step = numeric_bounds(dp)
            if mn is not None:
                payload["min"] = mn
            if mx is not None:
                payload["max"] = mx
            if step is not None and step > 0:
                payload["step"] = step
            else:
                # Default step by data type — coarse but better than guessing 1 for floats.
                payload["step"] = 1 if _enum_name(dp.data_type()) == "INT" else 0.1
            if unit:
                payload["unit_of_measurement"] = unit
            if device_class:
                payload["device_class"] = device_class
            payload["mode"] = "auto"
        elif component == "switch":
            payload["command_topic"] = self._command_topic(device_name, dp_name)
            payload["payload_on"] = "ON"
            payload["payload_off"] = "OFF"
            payload["state_on"] = "ON"
            payload["state_off"] = "OFF"
        elif component == "select":
            payload["command_topic"] = self._command_topic(device_name, dp_name)
            opts = enum_options(dp)
            if opts:
                payload["options"] = opts
            else:
                # No enum literals exposed: fall back to text so HA still creates something usable.
                topic = f"homeassistant/text/{unique_id}/config"
                payload["command_topic"] = self._command_topic(device_name, dp_name)
        elif component == "text":
            payload["command_topic"] = self._command_topic(device_name, dp_name)

        try:
            await self._client.publish(topic, json.dumps(payload), retain=True)
        except Exception as exc:
            logger.warning("MQTT discovery publish failed for %s: %s", unique_id, exc)

    # ------------------------------------------------------------------
    # State publishing
    # ------------------------------------------------------------------

    async def publish_states(self) -> int:
        """Read every data point and publish the current value."""
        if not self._enabled or self._client is None:
            return 0
        published = 0
        for device_name, fp_name, dp_name, dp in self.sgr.iter_datapoint_objects():
            try:
                value = await self.sgr.read(device_name, fp_name, dp_name)
            except Exception as exc:
                logger.debug("Cannot read %s/%s/%s: %s", device_name, fp_name, dp_name, exc)
                continue
            try:
                data_type = dp.data_type()
            except Exception:
                data_type = None
            payload = format_state_payload(value, data_type)
            try:
                await self._client.publish(
                    self._state_topic(device_name, dp_name), payload, retain=True
                )
                published += 1
            except Exception as exc:
                logger.debug("MQTT publish failed: %s", exc)
        if published:
            try:
                await self._client.publish(self._availability_topic(), "online", retain=True)
            except Exception:
                pass
        return published

    async def publish_offline(self) -> None:
        if not self._enabled or self._client is None:
            return
        try:
            await self._client.publish(self._availability_topic(), "offline", retain=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Command subscription loop
    # ------------------------------------------------------------------

    async def command_loop(self) -> None:
        """Subscribe to all command topics and write back to SGr devices."""
        if not self._enabled or self._client is None:
            return
        self._stop_event = asyncio.Event()
        try:
            from asyncio_mqtt import Client  # noqa: F401
        except ImportError:
            return

        try:
            async with self._client.filtered_messages(f"{self.prefix}/+/+/set") as messages:
                await self._client.subscribe(f"{self.prefix}/+/+/set")
                async for message in messages:
                    if self._stop_event.is_set():
                        return
                    await self._handle_command(message)
        except Exception as exc:
            logger.warning("MQTT command loop error: %s", exc)

    async def _handle_command(self, message) -> None:
        # Topic shape: {prefix}/{device_slug}/{dp_slug}/set
        parts = message.topic.split("/")
        if len(parts) != 4 or parts[3] != "set":
            return
        device_slug, dp_slug = parts[1], parts[2]
        payload = message.payload.decode("utf-8", errors="replace").strip()

        entry = self._dp_lookup.get((device_slug, dp_slug))
        if entry is None:
            logger.info("MQTT command for unknown slug %s/%s — ignored", device_slug, dp_slug)
            return
        device_name, fp_name, dp_name, dp, _component = entry

        try:
            data_type = dp.data_type()
        except Exception:
            data_type = None
        try:
            cast_value = cast_command(payload, data_type)
        except Exception as exc:
            logger.warning(
                "MQTT command %s/%s/%s payload %r could not be cast: %s",
                device_name, fp_name, dp_name, payload, exc,
            )
            return

        try:
            await self.sgr.write(device_name, fp_name, dp_name, cast_value)
            logger.info(
                "MQTT command %s/%s/%s = %s applied",
                device_name, fp_name, dp_name, cast_value,
            )
        except Exception as exc:
            logger.warning("MQTT command write failed: %s", exc)
