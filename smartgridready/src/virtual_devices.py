"""Virtual SGr devices — non-SGr Home Assistant entities piloted via
plain HA service calls, using the same SG-Ready state vocabulary as
real SGr devices.

Some hardware (a heat pump with no digital SGr interface, an EV
charger or boiler behind a simple relay, an inverter export limit
exposed only as an HA ``number``) will never speak SGr natively but
still benefits from the same optimisation rules. A "virtual device"
lets a rule target such an entity by name, exactly like a real SGr
device, and this module translates the resulting SG-Ready state
literal (``HP_LOCKED`` / ``HP_NORMAL`` / ``HP_INTENSIFIED`` /
``HP_FORCED``) into the matching HA service call:

  - ``climate_proxy``  → ``climate.set_temperature`` (base + offset)
  - ``switch_proxy``   → ``switch.turn_on`` / ``turn_off``
  - ``boiler_proxy``   → ``water_heater.set_temperature``
  - ``number_proxy``   → ``number.set_value``

**Important**: this is a Home Assistant orchestration layer, not
native SmartGridready communication. It widens practical coverage but
is not evidence of SmartGridready conformity — see
``docs/scope-and-gaps.md`` §3.13.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .config_loader import VirtualDeviceConfig

logger = logging.getLogger("smartgridready.virtual_devices")


def _normalize_state_key(value: Any) -> str:
    return str(value).strip().upper()


class VirtualDeviceManager:
    """Registry + dispatcher for virtual (non-SGr) devices."""

    def __init__(self) -> None:
        self._devices: Dict[str, VirtualDeviceConfig] = {}

    def load(self, configs: List[VirtualDeviceConfig]) -> int:
        self._devices = {vd.name: vd for vd in (configs or [])}
        if self._devices:
            logger.info(
                "Virtual devices: %d registered (%s)",
                len(self._devices),
                ", ".join(f"{n}[{vd.type}]" for n, vd in self._devices.items()),
            )
        return len(self._devices)

    def has(self, name: str) -> bool:
        return name in self._devices

    def list_devices(self) -> List[Dict[str, Any]]:
        return [
            {"name": vd.name, "type": vd.type, "targets": list(vd.targets)}
            for vd in self._devices.values()
        ]

    async def apply(
        self,
        name: str,
        value: Any,
        ha_client,
        state_map: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Apply an SG-Ready state literal to a registered virtual device."""
        vd = self._devices.get(name)
        if vd is None:
            return {"applied": False, "reason": "unknown_virtual_device"}
        if ha_client is None or not getattr(ha_client, "available", False):
            return {"applied": False, "reason": "no_ha_api"}

        key = _normalize_state_key(value)
        if vd.type == "climate_proxy":
            return await self._apply_climate_proxy(vd, key, ha_client, state_map or {})
        if vd.type == "switch_proxy":
            return await self._apply_switch_proxy(vd, key, ha_client)
        if vd.type == "boiler_proxy":
            return await self._apply_boiler_proxy(vd, key, ha_client)
        if vd.type == "number_proxy":
            return await self._apply_number_proxy(vd, key, ha_client)
        return {"applied": False, "reason": f"unknown_type:{vd.type}"}

    # ------------------------------------------------------------------
    # Per-type actions
    # ------------------------------------------------------------------

    async def _apply_climate_proxy(
        self,
        vd: VirtualDeviceConfig,
        key: str,
        ha_client,
        state_map: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        if key not in vd.mapping:
            return {"applied": False, "reason": f"no offset for state {key} (have {sorted(vd.mapping)})"}

        offset = vd.mapping[key]
        base = vd.base_setpoint_default
        if vd.base_setpoint_entity:
            raw = state_map.get(vd.base_setpoint_entity, {}).get("state")
            try:
                base = float(raw)
            except (TypeError, ValueError):
                pass
        setpoint = round(max(vd.min_setpoint, min(vd.max_setpoint, base + offset)), 1)

        ok_count = await self._call_all(ha_client, "climate", "set_temperature", vd.targets, {"temperature": setpoint})
        if ok_count == 0:
            return {"applied": False, "reason": "all targets failed"}
        logger.info(
            "virtual climate_proxy %s: SG-Ready %s -> %.1f\u00b0C on %d/%d",
            vd.name, key, setpoint, ok_count, len(vd.targets),
        )
        return {"applied": True, "reason": f"setpoint={setpoint}\u00b0C ({ok_count}/{len(vd.targets)})"}

    async def _apply_switch_proxy(self, vd: VirtualDeviceConfig, key: str, ha_client) -> Dict[str, Any]:
        if key not in vd.mapping:
            return {"applied": False, "reason": f"no switch mapping for state {key} (have {sorted(vd.mapping)})"}

        desired_on = bool(vd.mapping[key])
        service = "turn_on" if desired_on else "turn_off"
        ok_count = await self._call_all(ha_client, "switch", service, vd.targets, {})
        if ok_count == 0:
            return {"applied": False, "reason": "all targets failed"}
        logger.info(
            "virtual switch_proxy %s: SG-Ready %s -> %s on %d/%d",
            vd.name, key, service.replace("turn_", "").upper(), ok_count, len(vd.targets),
        )
        return {"applied": True, "reason": f"{service.replace('turn_', '')} ({ok_count}/{len(vd.targets)})"}

    async def _apply_boiler_proxy(self, vd: VirtualDeviceConfig, key: str, ha_client) -> Dict[str, Any]:
        if key not in vd.mapping:
            return {"applied": False, "reason": f"no temperature mapping for state {key} (have {sorted(vd.mapping)})"}

        desired_temp = max(vd.min_value, min(vd.max_value, vd.mapping[key]))
        entity_id = vd.targets[0] if vd.targets else None
        if not entity_id:
            return {"applied": False, "reason": "water_heater_entity not set"}

        ok = await ha_client.acall_service(
            "water_heater", "set_temperature", {"entity_id": entity_id, "temperature": desired_temp}
        )
        if not ok:
            return {"applied": False, "reason": "set_temperature failed"}
        logger.info(
            "virtual boiler_proxy %s: SG-Ready %s -> %.0f\u00b0C on %s", vd.name, key, desired_temp, entity_id
        )
        return {"applied": True, "reason": f"set_temperature {desired_temp:.0f}\u00b0C"}

    async def _apply_number_proxy(self, vd: VirtualDeviceConfig, key: str, ha_client) -> Dict[str, Any]:
        if key not in vd.mapping:
            return {"applied": False, "reason": f"no value mapping for state {key} (have {sorted(vd.mapping)})"}

        desired = max(vd.min_value, min(vd.max_value, vd.mapping[key]))
        ok_count = await self._call_all(ha_client, "number", "set_value", vd.targets, {"value": desired})
        if ok_count == 0:
            return {"applied": False, "reason": "all targets failed"}
        logger.info(
            "virtual number_proxy %s: SG-Ready %s -> %.1f on %d/%d",
            vd.name, key, desired, ok_count, len(vd.targets),
        )
        return {"applied": True, "reason": f"set_value {desired:.1f} ({ok_count}/{len(vd.targets)})"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _call_all(
        ha_client, domain: str, service: str, targets: List[str], extra: Dict[str, Any]
    ) -> int:
        """Call ``domain.service`` on every target entity; return success count."""
        ok_count = 0
        for entity_id in targets:
            data = {"entity_id": entity_id, **extra}
            try:
                if await ha_client.acall_service(domain, service, data):
                    ok_count += 1
            except Exception as exc:
                logger.warning("virtual device: %s.%s(%s) failed: %s", domain, service, entity_id, exc)
        return ok_count
