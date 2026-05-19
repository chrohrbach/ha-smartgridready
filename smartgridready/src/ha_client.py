"""Home Assistant REST client.

Uses the Supervisor proxy endpoint at ``http://supervisor/core/api/``
authenticated with ``$SUPERVISOR_TOKEN`` (injected automatically when
``homeassistant_api: true`` is declared in the add-on manifest).

Only the small surface needed by the rules engine and MQTT bridge is
implemented:

- ``get_states()`` — list all states (used to build the evaluation context)
- ``get_state(entity_id)`` — single state
- ``call_service(domain, service, data)`` — invoke a service
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("smartgridready.ha")

DEFAULT_BASE_URL = "http://supervisor/core/api"
REQUEST_TIMEOUT = 15.0


class HomeAssistantClient:
    """Minimal HA REST client over the Supervisor proxy."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = token or os.environ.get("SUPERVISOR_TOKEN") or ""
        self._timeout = timeout
        self._client: Optional[httpx.Client] = None
        self._async_client: Optional[httpx.AsyncClient] = None

    # -- low-level transport --------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    @property
    def available(self) -> bool:
        return bool(self.token)

    def _sync(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=self._headers(),
                timeout=self._timeout,
            )
        return self._client

    async def _async(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._headers(),
                timeout=self._timeout,
            )
        return self._async_client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def aclose(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    # -- public sync API -----------------------------------------------------

    def get_states(self) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        try:
            r = self._sync().get("/states")
            r.raise_for_status()
            return r.json() or []
        except Exception as exc:
            logger.warning("HA get_states failed: %s", exc)
            return []

    def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        if not self.available or not entity_id:
            return None
        try:
            r = self._sync().get(f"/states/{entity_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("HA get_state(%s) failed: %s", entity_id, exc)
            return None

    def call_service(
        self,
        domain: str,
        service: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not self.available:
            return False
        try:
            r = self._sync().post(f"/services/{domain}/{service}", json=data or {})
            r.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("HA call_service(%s.%s) failed: %s", domain, service, exc)
            return False

    # -- public async API ----------------------------------------------------

    async def aget_states(self) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        try:
            client = await self._async()
            r = await client.get("/states")
            r.raise_for_status()
            return r.json() or []
        except Exception as exc:
            logger.warning("HA aget_states failed: %s", exc)
            return []

    async def acall_service(
        self,
        domain: str,
        service: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not self.available:
            return False
        try:
            client = await self._async()
            r = await client.post(f"/services/{domain}/{service}", json=data or {})
            r.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("HA acall_service(%s.%s) failed: %s", domain, service, exc)
            return False

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def state_to_float(state: Optional[Dict[str, Any]]) -> float:
        if not state:
            return 0.0
        v = state.get("state")
        if v in (None, "", "unknown", "unavailable"):
            return 0.0
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def state_to_bool(state: Optional[Dict[str, Any]]) -> bool:
        if not state:
            return False
        v = state.get("state")
        return str(v).lower() in ("on", "true", "open", "home", "1")
