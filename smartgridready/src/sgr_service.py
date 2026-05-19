"""SmartGridReady device service.

Manages devices declared in the user's configuration using the
``sgr-commhandler`` library. Each device is built from an EID XML
profile (downloaded from the SGr product library and cached locally on
first use), connected, and exposed for read / write operations on its
functional profile data points.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config_loader import DeviceConfig, UserConfig

logger = logging.getLogger("smartgridready.devices")

MAX_RETRIES = 3
RETRY_DELAYS = (1, 3, 5)

# Refresh cached EID XML if older than this. Manufacturers publish
# corrections to the online library; without a TTL we would never see them.
EID_CACHE_TTL_DAYS = 30


class SGrDevice:
    """Connected device wrapper with metadata for the UI/audit."""

    def __init__(self, config: DeviceConfig, device: Any = None):
        self.config = config
        self.device = device
        self.connected = False
        self.last_error: Optional[str] = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def eid(self) -> str:
        return self.config.eid

    def to_dict(self) -> dict:
        info: Dict[str, Any] = {
            "name": self.name,
            "eid": self.eid,
            "connected": self.connected,
            "properties": {
                k: v
                for k, v in self.config.properties.items()
                if k.lower() not in ("password", "token", "secret", "api_key")
            },
        }
        if self.last_error:
            info["last_error"] = self.last_error
        if self.device and self.connected:
            try:
                dev_name, profiles = self.device.describe()
                info["device_name"] = dev_name
                # Expose SGr LevelOfOperation (per spec: 'm', '1'-'6', or
                # compound notation like '4m') for capability discovery. The
                # raw device frame holds it; the SDK's DeviceInformation
                # dataclass omits the field.
                try:
                    frame_info = self.device.device_frame.device_information
                    level = getattr(frame_info, "level_of_operation", None)
                    if level is not None:
                        info["level_of_operation"] = str(level)
                except AttributeError:
                    pass
                info["functional_profiles"] = {}
                for fp_name, dps in profiles.items():
                    fp_info: Dict[str, Any] = {
                        "data_points": {
                            dp_name: {
                                "direction": getattr(direction, "value", str(direction)),
                                "type": getattr(dtype, "value", str(dtype)),
                            }
                            for dp_name, (direction, dtype) in dps.items()
                        }
                    }
                    # Per-FP level may differ from the device-level (a single
                    # device can expose profiles at varying control depths).
                    try:
                        fp = self.device.get_functional_profile(fp_name)
                        fp_spec = fp.get_specification()
                        fp_id = fp_spec.functional_profile.functional_profile_identification
                        fp_level = getattr(fp_id, "level_of_operation", None)
                        if fp_level is not None:
                            fp_info["level_of_operation"] = str(fp_level)
                    except (AttributeError, KeyError):
                        pass
                    info["functional_profiles"][fp_name] = fp_info
            except Exception as exc:
                info["describe_error"] = str(exc)
        return info


class SGrService:
    """Service for managing SmartGridReady devices.

    Lifecycle: ``connect_all`` → ``read``/``write`` → ``disconnect_all``.
    """

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.devices: Dict[str, SGrDevice] = {}
        self._sgr_available = False
        try:
            from sgr_commhandler.device_builder import DeviceBuilder  # noqa: F401
            self._sgr_available = True
        except ImportError:
            logger.warning("sgr-commhandler is not installed — device features disabled")

    @property
    def available(self) -> bool:
        return self._sgr_available

    # ------------------------------------------------------------------
    # EID resolution
    # ------------------------------------------------------------------

    def _resolve_eid(self, eid: str) -> str:
        """Resolve an EID identifier to XML content.

        The SGr online library (``https://library.smartgridready.ch/prodx/<name>``)
        requires the trailing ``.xml`` suffix — without it the endpoint
        returns 404. We auto-append it for the library call but normalize the
        cache filename so users can write the EID with or without the suffix.

        Lookup order:
          1. Local cache (``<cache_path>/<eid>.xml``) if younger than
             ``EID_CACHE_TTL_DAYS``
          2. Direct file path if ``eid`` ends with ``.xml``
          3. Online SGr library (auto-appends ``.xml``)
          4. Stale cache fallback if the library is unreachable
        """
        # Normalize cache filename and library lookup key
        eid_basename = eid[:-4] if eid.lower().endswith(".xml") else eid
        library_name = f"{eid_basename}.xml"
        cached = self.cache_path / f"{eid_basename}.xml"

        # 1. Cache hit within TTL
        if cached.exists():
            age_days = (datetime.now().timestamp() - cached.stat().st_mtime) / 86400
            if age_days < EID_CACHE_TTL_DAYS:
                logger.debug("EID %s: using cached XML (age=%.1fd)", eid, age_days)
                return cached.read_text(encoding="utf-8")
            logger.info(
                "EID %s: cache expired (%.0fd > %dd), refreshing from library",
                eid, age_days, EID_CACHE_TTL_DAYS,
            )

        # 2. Direct filesystem path (debug / offline workflows)
        if eid.endswith(".xml"):
            direct = Path(eid)
            if direct.exists():
                return direct.read_text(encoding="utf-8")

        # 3. Online library (mandatory .xml suffix)
        from sgr_commhandler.declaration_library import get_product_eid_xml
        try:
            xml = get_product_eid_xml(library_name)
        except Exception as exc:
            # 4. Library unreachable — fall back to stale cache if available
            if cached.exists():
                logger.warning(
                    "EID %s: library fetch failed (%s) — using stale cache",
                    library_name, exc,
                )
                return cached.read_text(encoding="utf-8")
            logger.error("EID %s: failed to resolve — %s", library_name, exc)
            raise

        try:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            cached.write_text(xml, encoding="utf-8")
            logger.info("EID %s downloaded and cached", library_name)
        except OSError as exc:
            logger.warning("Cannot cache EID %s: %s", library_name, exc)
        return xml

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect_all(self, config: UserConfig) -> Dict[str, bool]:
        """Build and connect every enabled device in the config."""
        if not self._sgr_available:
            return {}

        results: Dict[str, bool] = {}
        for dev_cfg in config.devices:
            if not dev_cfg.enabled:
                logger.info("Device %s disabled in config — skipping", dev_cfg.name)
                continue
            try:
                results[dev_cfg.name] = await self._connect_device(dev_cfg)
            except Exception as exc:
                logger.error("Device %s connect failed: %s", dev_cfg.name, exc)
                self.devices[dev_cfg.name] = SGrDevice(dev_cfg)
                self.devices[dev_cfg.name].last_error = str(exc)
                results[dev_cfg.name] = False

        ok = sum(1 for v in results.values() if v)
        logger.info("SGr: %d / %d devices connected", ok, len(results))
        return results

    async def _connect_device(self, dev_cfg: DeviceConfig) -> bool:
        from sgr_commhandler.device_builder import DeviceBuilder

        xml = self._resolve_eid(dev_cfg.eid)
        # SGr requires all properties as strings.
        str_props = {k: str(v) for k, v in dev_cfg.properties.items()}

        device = (
            DeviceBuilder()
            .eid(xml)
            .properties(str_props)
            .build()
        )
        await device.connect_async()

        wrap = SGrDevice(dev_cfg, device=device)
        wrap.connected = True
        self.devices[dev_cfg.name] = wrap

        try:
            dev_name, profiles = device.describe()
            logger.info(
                "Device %s (%s) connected — profiles: %s",
                dev_cfg.name, dev_name, list(profiles.keys()),
            )
        except Exception:
            logger.info("Device %s connected", dev_cfg.name)
        return True

    async def disconnect_all(self) -> None:
        for name, wrap in list(self.devices.items()):
            if wrap.device and wrap.connected:
                try:
                    await wrap.device.disconnect_async()
                except Exception as exc:
                    logger.debug("Disconnect %s: %s", name, exc)
                wrap.connected = False

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    async def read(
        self,
        device_name: str,
        fp: str,
        dp: str,
        parameters: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Read a data point value.

        Args:
            device_name: device declared in user config
            fp: functional profile name
            dp: data point name
            parameters: optional dynamic parameters required by the data
                point's ``<parameterList>`` (e.g. ``{"date": "2026-05-18"}``
                for a DynamicTariff query, ``{"channel": "L1"}`` for a
                multi-phase meter). Required keys are EID-specific.
        """
        wrap = self._require_connected(device_name)
        data_point = wrap.device.get_data_point((fp, dp))
        return await self._retry(
            lambda: data_point.get_value_async(parameters),
            f"read {device_name}/{fp}/{dp}",
        )

    async def write(self, device_name: str, fp: str, dp: str, value: Any) -> None:
        """Write a data point value.

        Note: the SGr SDK's ``set_value_async`` does not accept dynamic
        parameters (writes are stateless commands). If a future spec
        revision adds them, extend this signature.
        """
        wrap = self._require_connected(device_name)
        data_point = wrap.device.get_data_point((fp, dp))
        await self._retry(
            lambda: data_point.set_value_async(value),
            f"write {device_name}/{fp}/{dp}={value}",
        )
        logger.info("Wrote %s = %s on %s/%s", value, dp, device_name, fp)

    async def read_all(self, device_name: str) -> Dict[Tuple[str, str], Any]:
        wrap = self._require_connected(device_name)
        return await wrap.device.get_values_async()

    async def _retry(self, operation, description: str, max_retries: int = MAX_RETRIES):
        last_error: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                return await operation()
            except Exception as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    logger.warning(
                        "SGr %s failed (attempt %d/%d): %s — retry in %ds",
                        description, attempt + 1, max_retries, exc, delay,
                    )
                    await asyncio.sleep(delay)
        logger.error("SGr %s failed after %d attempts: %s", description, max_retries, last_error)
        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_devices(self) -> List[Dict]:
        return [d.to_dict() for d in self.devices.values()]

    def describe(self, device_name: str) -> Dict:
        return self._require(device_name).to_dict()

    def writable_datapoints(self, device_name: str) -> List[Dict]:
        """List writable data points.

        Per SGr ``DataDirectionProduct``: ``C``=constant, ``R``=read,
        ``W``=write, ``RW``=read-write, ``RWP``=read-write-persistent.
        A data point is writable iff the direction contains ``W``.
        """
        wrap = self._require(device_name)
        if not wrap.device or not wrap.connected:
            return []
        writable: List[Dict] = []
        _, profiles = wrap.device.describe()
        for fp_name, dps in profiles.items():
            for dp_name, (direction, dtype) in dps.items():
                dir_str = getattr(direction, "value", str(direction))
                if "W" in dir_str:
                    writable.append({
                        "device": device_name,
                        "functional_profile": fp_name,
                        "data_point": dp_name,
                        "direction": dir_str,
                        "type": getattr(dtype, "value", str(dtype)),
                    })
        return writable

    def all_datapoints(self) -> List[Dict]:
        """List every data point of every connected device, with metadata."""
        result: List[Dict] = []
        for name, wrap in self.devices.items():
            if not wrap.device or not wrap.connected:
                continue
            try:
                _, profiles = wrap.device.describe()
            except Exception:
                continue
            for fp_name, dps in profiles.items():
                for dp_name, (direction, dtype) in dps.items():
                    result.append({
                        "device": name,
                        "functional_profile": fp_name,
                        "data_point": dp_name,
                        "direction": getattr(direction, "value", str(direction)),
                        "type": getattr(dtype, "value", str(dtype)),
                    })
        return result

    def iter_datapoint_objects(self):
        """Yield ``(device_name, fp_name, dp_name, DataPoint)`` for each connected data point.

        Unlike :meth:`all_datapoints` (flat dicts for the UI), this exposes the
        live ``sgr_commhandler.DataPoint`` so callers can read the EID-declared
        unit, type, options and min/max via its API.
        """
        for name, wrap in self.devices.items():
            if not wrap.device or not wrap.connected:
                continue
            try:
                dps = wrap.device.get_data_points()
            except Exception:
                continue
            for (fp_name, dp_name), dp in dps.items():
                yield name, fp_name, dp_name, dp

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require(self, name: str) -> SGrDevice:
        if name not in self.devices:
            available = list(self.devices.keys())
            raise ValueError(f"Device '{name}' not found. Available: {available}")
        return self.devices[name]

    def _require_connected(self, name: str) -> SGrDevice:
        wrap = self._require(name)
        if not wrap.connected:
            raise ConnectionError(f"Device '{name}' is not connected")
        return wrap
