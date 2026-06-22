"""Regression tests for cross-cutting bugfixes.

Each class targets a specific spec-alignment fix:

  - ResolverNormalization: SGr library requires ``.xml``; the resolver
    must auto-append it, normalize the cache filename, honour the TTL,
    and fall back to a stale cache when the library is unreachable.
  - DSLPrecedence: ``AND`` binds tighter than ``OR`` (standard boolean
    algebra) — historically split AND first which inverted the semantics.
  - TimeRangeInclusive: ``<=`` bounds were silently treated as ``<``.
  - HysteresisPersistence: ``min_interval`` must survive add-on restarts
    to actually protect heat-pump compressors from rapid cycling.
  - DynamicParameter: ``read()`` must forward the ``parameters`` dict to
    the SDK so profiles like ``DynamicTariff`` (date) and multi-channel
    meters can be used at all.
  - LevelOfOperation: SGr declares a control level per device and per
    functional profile — surface both in ``describe()`` so callers know
    what writes are realistically supported.

Run via:  pytest tests/test_regression_fixes.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config_loader import DeviceConfig, RuleConfig, SensorMap, UserConfig
from src.rules_engine import HYSTERESIS_MAX_AGE_DAYS, RulesEngine
from src.sgr_service import EID_CACHE_TTL_DAYS, SGrDevice, SGrService


# ---------------------------------------------------------------------------
# Resolver: .xml auto-append, TTL, stale-cache fallback
# ---------------------------------------------------------------------------


class TestResolverNormalization:

    def _install_fake_lib(self, monkeypatch, response_xml: str, captured: list,
                          raise_exc: Exception | None = None):
        """Inject a fake ``sgr_commhandler.declaration_library`` that records
        the EID name passed to ``get_product_eid_xml`` and optionally raises."""
        fake_root = types.ModuleType("sgr_commhandler")
        fake_decl = types.ModuleType("sgr_commhandler.declaration_library")

        def fake_get(name: str) -> str:
            captured.append(name)
            if raise_exc is not None:
                raise raise_exc
            return response_xml

        fake_decl.get_product_eid_xml = fake_get
        fake_root.declaration_library = fake_decl
        monkeypatch.setitem(sys.modules, "sgr_commhandler", fake_root)
        monkeypatch.setitem(sys.modules, "sgr_commhandler.declaration_library", fake_decl)

    def test_resolver_appends_xml_when_missing(self, tmp_path, monkeypatch):
        captured: list = []
        self._install_fake_lib(monkeypatch, "<?xml?><DeviceFrame/>", captured)
        svc = SGrService(tmp_path)
        svc._resolve_eid("SGr_04_0015_xxxx_StiebelEltron_HeatPump_V1.0.0")
        assert captured == [
            "SGr_04_0015_xxxx_StiebelEltron_HeatPump_V1.0.0.xml"
        ], "library lookup must use the .xml suffix"

    def test_resolver_preserves_xml_when_present(self, tmp_path, monkeypatch):
        captured: list = []
        self._install_fake_lib(monkeypatch, "<?xml?><DeviceFrame/>", captured)
        svc = SGrService(tmp_path)
        svc._resolve_eid("SGr_TEST.xml")
        assert captured == ["SGr_TEST.xml"], "must not double-append .xml"

    def test_cache_filename_single_suffix(self, tmp_path, monkeypatch):
        captured: list = []
        self._install_fake_lib(monkeypatch, "<?xml?><DeviceFrame/>", captured)
        svc = SGrService(tmp_path)
        svc._resolve_eid("SGr_TEST.xml")
        files = list(tmp_path.glob("*"))
        assert len(files) == 1
        assert files[0].name == "SGr_TEST.xml", "must not produce .xml.xml"

    def test_fresh_cache_does_not_call_library(self, tmp_path, monkeypatch):
        tmp_path.mkdir(exist_ok=True)
        cache_file = tmp_path / "SGr_CACHED.xml"
        cache_file.write_text("<?xml?><DeviceFrame>cached</DeviceFrame>")
        captured: list = []
        self._install_fake_lib(monkeypatch, "<?xml?><DeviceFrame>fresh</DeviceFrame>", captured)
        svc = SGrService(tmp_path)
        result = svc._resolve_eid("SGr_CACHED")
        assert "cached" in result
        assert captured == []

    def test_stale_cache_refreshed_from_library(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "SGr_STALE.xml"
        cache_file.write_text("<?xml?><DeviceFrame>old</DeviceFrame>")
        # Make file appear ancient
        old = time.time() - 86400 * (EID_CACHE_TTL_DAYS + 5)
        os.utime(cache_file, (old, old))

        captured: list = []
        self._install_fake_lib(monkeypatch, "<?xml?><DeviceFrame>fresh</DeviceFrame>", captured)
        svc = SGrService(tmp_path)
        result = svc._resolve_eid("SGr_STALE")
        assert "fresh" in result
        assert captured == ["SGr_STALE.xml"]
        # And the cache was refreshed
        assert "fresh" in cache_file.read_text()

    def test_stale_cache_fallback_when_library_down(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "SGr_OFFLINE.xml"
        cache_file.write_text("<?xml?><DeviceFrame>stale-but-usable</DeviceFrame>")
        old = time.time() - 86400 * 60
        os.utime(cache_file, (old, old))

        captured: list = []
        self._install_fake_lib(monkeypatch, "", captured,
                               raise_exc=ConnectionError("network down"))
        svc = SGrService(tmp_path)
        result = svc._resolve_eid("SGr_OFFLINE")
        # Library failure → return the stale cache rather than crashing
        assert "stale-but-usable" in result


# ---------------------------------------------------------------------------
# DSL: AND binds tighter than OR
# ---------------------------------------------------------------------------


class TestDSLPrecedence:

    @pytest.fixture
    def engine(self, tmp_path):
        return RulesEngine(MagicMock(), MagicMock(), tmp_path / "audit.json")

    def test_and_binds_tighter_than_or(self, engine):
        """``A OR B AND C`` must parse as ``A OR (B AND C)`` (standard precedence)."""
        ctx = {"a": False, "b": True, "c": False}
        assert engine._eval_expression("a OR b AND c", ctx) is False
        ctx = {"a": False, "b": True, "c": True}
        assert engine._eval_expression("a OR b AND c", ctx) is True
        ctx = {"a": True, "b": False, "c": False}
        # A is True → short-circuits the OR
        assert engine._eval_expression("a OR b AND c", ctx) is True

    def test_real_world_precedence(self, engine):
        # is_peak OR (has_surplus AND spot_price > 0.15)
        ctx = {"is_peak": False, "has_surplus": True, "spot_price": 0.20}
        assert engine._eval_expression(
            "is_peak OR has_surplus AND spot_price > 0.15", ctx
        ) is True
        ctx["spot_price"] = 0.05
        assert engine._eval_expression(
            "is_peak OR has_surplus AND spot_price > 0.15", ctx
        ) is False


# ---------------------------------------------------------------------------
# DSL: <= time-range bounds
# ---------------------------------------------------------------------------


class TestTimeRangeInclusive:

    @pytest.fixture
    def engine(self, tmp_path):
        return RulesEngine(MagicMock(), MagicMock(), tmp_path / "audit.json")

    def test_strict_excludes_bound(self, engine):
        assert engine._eval_expression("10h < hour < 16h", {"hour": 10}) is False
        assert engine._eval_expression("10h < hour < 16h", {"hour": 16}) is False

    def test_inclusive_includes_bound(self, engine):
        assert engine._eval_expression("10h <= hour <= 16h", {"hour": 10}) is True
        assert engine._eval_expression("10h <= hour <= 16h", {"hour": 16}) is True
        assert engine._eval_expression("10h <= hour <= 16h", {"hour": 17}) is False

    def test_mixed_bounds(self, engine):
        # Low inclusive, high exclusive (the common "half-open" pattern)
        assert engine._eval_expression("10h <= hour < 16h", {"hour": 10}) is True
        assert engine._eval_expression("10h <= hour < 16h", {"hour": 16}) is False


# ---------------------------------------------------------------------------
# Hysteresis: survives engine restart, prunes ancient entries
# ---------------------------------------------------------------------------


class TestHysteresisPersistence:

    @pytest.fixture
    def ha_mock(self):
        api = MagicMock()
        api.get_states.return_value = [
            {"entity_id": "sensor.spot_price", "state": "0.12"},
            {"entity_id": "sensor.pv", "state": "100"},
            {"entity_id": "sensor.house", "state": "200"},
            {"entity_id": "sensor.soc", "state": "60"},
        ]
        return api

    @pytest.fixture
    def sensors(self):
        return SensorMap(
            spot_price="sensor.spot_price",
            pv_power="sensor.pv",
            house_consumption="sensor.house",
            battery_soc="sensor.soc",
        )

    @pytest.fixture
    def rules(self):
        return [RuleConfig(
            device="PAC",
            profile="SG-ReadyStates",
            data_point="SGReadyState",
            min_interval=15,
            conditions=[{"default": True, "value": 3}],
        )]

    @pytest.mark.asyncio
    async def test_hysteresis_survives_engine_restart(
        self, tmp_path, ha_mock, sensors, rules
    ):
        audit_path = tmp_path / "audit.json"
        sgr1 = MagicMock(); sgr1.write = AsyncMock()
        engine1 = RulesEngine(sgr1, ha_mock, audit_path)
        await engine1.evaluate(UserConfig(devices=[], rules=rules, vehicles=[], sensors=sensors))
        sgr1.write.assert_awaited_once()

        # Persistence file present
        assert (tmp_path / "hysteresis.json").exists()

        # New engine (simulates add-on restart): same audit_path → same hysteresis_path
        sgr2 = MagicMock(); sgr2.write = AsyncMock()
        engine2 = RulesEngine(sgr2, ha_mock, audit_path)
        result = await engine2.evaluate(UserConfig(devices=[], rules=rules, vehicles=[], sensors=sensors))
        # _last_values is RAM-only and resets, but hysteresis blocks the write
        sgr2.write.assert_not_called()
        assert any(s.get("reason") == "hysteresis" for s in result.get("skipped", []))

    def test_stale_entries_pruned_on_load(self, tmp_path):
        hyst_path = tmp_path / "hysteresis.json"
        ancient = (datetime.now() - timedelta(days=HYSTERESIS_MAX_AGE_DAYS + 30)).isoformat()
        fresh = (datetime.now() - timedelta(minutes=2)).isoformat()
        hyst_path.write_text(json.dumps({
            "Fresh/FP/DP": fresh,
            "Ancient/FP/DP": ancient,
        }))
        engine = RulesEngine(MagicMock(), MagicMock(), tmp_path / "audit.json")
        # _load_hysteresis runs in __init__ and looks at audit_path.with_name("hysteresis.json")
        assert "Fresh/FP/DP" in engine._last_change_times
        assert "Ancient/FP/DP" not in engine._last_change_times

    def test_corrupted_hysteresis_file_recovers(self, tmp_path):
        (tmp_path / "hysteresis.json").write_text("not json{{{")
        # Must not raise — engine starts with empty dict
        engine = RulesEngine(MagicMock(), MagicMock(), tmp_path / "audit.json")
        assert engine._last_change_times == {}


# ---------------------------------------------------------------------------
# DynamicParameter: read() must forward the parameters dict
# ---------------------------------------------------------------------------


class TestDynamicParameterPassThrough:

    def _make_service_with_fake_device(self, captured: list):
        svc = SGrService(Path("/tmp/sgr-test"))

        async def fake_get_value_async(parameters=None, **_kw):
            captured.append(parameters)
            return 42

        dp = MagicMock()
        dp.get_value_async = fake_get_value_async
        dev = MagicMock()
        dev.get_data_point = MagicMock(return_value=dp)

        cfg = DeviceConfig(name="TestDev", eid="x", properties={})
        wrap = SGrDevice(cfg, device=dev)
        wrap.connected = True
        svc.devices["TestDev"] = wrap
        return svc

    def test_read_without_parameters_passes_none(self):
        captured: list = []
        svc = self._make_service_with_fake_device(captured)
        asyncio.run(svc.read("TestDev", "FP", "DP"))
        assert captured == [None]

    def test_read_with_parameters_passes_dict(self):
        captured: list = []
        svc = self._make_service_with_fake_device(captured)
        asyncio.run(svc.read("TestDev", "FP", "DP",
                             parameters={"date": "2026-05-18"}))
        assert captured == [{"date": "2026-05-18"}]

    def test_read_multi_param_dict(self):
        captured: list = []
        svc = self._make_service_with_fake_device(captured)
        asyncio.run(svc.read("TestDev", "FP", "DP",
                             parameters={"channel": "L1", "phase": "1"}))
        assert captured == [{"channel": "L1", "phase": "1"}]

    def test_read_profile_reads_whole_profile(self):
        profile = MagicMock()
        profile.get_values_async = AsyncMock(return_value={"DP1": 42})
        dev = MagicMock()
        dev.get_functional_profile = MagicMock(return_value=profile)
        cfg = DeviceConfig(name="TestDev", eid="x", properties={})
        wrap = SGrDevice(cfg, device=dev)
        wrap.connected = True
        svc = SGrService(Path("/tmp/sgr-test"))
        svc.devices["TestDev"] = wrap

        result = asyncio.run(svc.read_profile("TestDev", "FP"))
        assert result == {"DP1": 42}


class TestOptionalWrites:

    def _make_service_with_optional_dp(self, available: bool = True):
        svc = SGrService(Path("/tmp/sgr-test"))
        dp = MagicMock()
        dp.set_value_async = AsyncMock()
        dev = MagicMock()
        dev.get_data_points = MagicMock(
            return_value={("FP", "OptionalDP"): dp} if available else {}
        )
        dev.get_data_point = MagicMock(return_value=dp)
        cfg = DeviceConfig(name="TestDev", eid="x", properties={})
        wrap = SGrDevice(cfg, device=dev)
        wrap.connected = True
        svc.devices["TestDev"] = wrap
        return svc, dp

    def test_write_if_exists_writes_when_present(self):
        svc, dp = self._make_service_with_optional_dp(available=True)
        assert asyncio.run(svc.write_if_exists("TestDev", "FP", "OptionalDP", 7)) is True
        dp.set_value_async.assert_awaited_once_with(7)

    def test_write_if_exists_skips_when_missing(self):
        svc, dp = self._make_service_with_optional_dp(available=False)
        assert asyncio.run(svc.write_if_exists("TestDev", "FP", "OptionalDP", 7)) is False
        dp.set_value_async.assert_not_called()

    def test_evse_watchdog_writes_optional_points(self, tmp_path):
        svc = SGrService(tmp_path)
        svc.write_if_exists = AsyncMock(side_effect=[True, True])
        asyncio.run(svc._configure_evse_watchdog(
            "Wallbox",
            {"safe_current": 6, "max_receive_time_sec": 120},
            ["EMS_Current_Limit"],
        ))
        assert svc.write_if_exists.await_args_list[0].args == (
            "Wallbox", "EMS_Current_Limit", "SafeCurrent", 6.0
        )
        assert svc.write_if_exists.await_args_list[1].args == (
            "Wallbox", "EMS_Current_Limit", "MaxReceiveTimeSec", 120
        )


# ---------------------------------------------------------------------------
# LevelOfOperation: exposed in to_dict, robust to missing fields
# ---------------------------------------------------------------------------


class TestLevelOfOperationExposure:

    def _device_with_levels(self, device_level: str, fp_level: str):
        fake = MagicMock()
        fake.device_frame.device_information.level_of_operation = device_level
        fake.describe.return_value = (
            "FakeName",
            {"FP1": {"DP1": ("R", "INT")}},
        )
        fp_spec = MagicMock()
        fp_spec.functional_profile.functional_profile_identification.level_of_operation = fp_level
        fp = MagicMock()
        fp.get_specification.return_value = fp_spec
        fake.get_functional_profile = MagicMock(return_value=fp)
        return fake

    def test_device_level_exposed(self):
        cfg = DeviceConfig(name="X", eid="e", properties={})
        wrap = SGrDevice(cfg, device=self._device_with_levels("4m", "4"))
        wrap.connected = True
        info = wrap.to_dict()
        assert info["level_of_operation"] == "4m"

    def test_fp_level_exposed(self):
        cfg = DeviceConfig(name="X", eid="e", properties={})
        wrap = SGrDevice(cfg, device=self._device_with_levels("4m", "2"))
        wrap.connected = True
        info = wrap.to_dict()
        assert info["functional_profiles"]["FP1"]["level_of_operation"] == "2"

    def test_data_points_under_data_points_key(self):
        cfg = DeviceConfig(name="X", eid="e", properties={})
        wrap = SGrDevice(cfg, device=self._device_with_levels("4m", "4"))
        wrap.connected = True
        fp_info = wrap.to_dict()["functional_profiles"]["FP1"]
        assert "data_points" in fp_info
        assert fp_info["data_points"]["DP1"]["direction"] == "R"

    def test_direction_uses_enum_value_not_repr(self):
        """``str(EnumMember)`` returns ``ClassName.MEMBER`` which is ugly;
        ``.value`` gives just the spec literal (``R``, ``W``, ``RW``)."""
        import enum

        class FakeDirection(enum.Enum):
            R = "R"
            W = "W"

        fake = MagicMock()
        fake.device_frame.device_information.level_of_operation = None
        fake.describe.return_value = (
            "Name",
            {"FP": {"DP": (FakeDirection.R, "INT")}},
        )
        fake.get_functional_profile = MagicMock(side_effect=AttributeError)
        cfg = DeviceConfig(name="X", eid="e", properties={})
        wrap = SGrDevice(cfg, device=fake)
        wrap.connected = True
        info = wrap.to_dict()
        dp = info["functional_profiles"]["FP"]["data_points"]["DP"]
        assert dp["direction"] == "R", f"got {dp['direction']!r}"

    def test_missing_level_does_not_crash(self):
        fake = MagicMock()
        fake.device_frame.device_information.level_of_operation = None
        fake.describe.return_value = ("Name", {})
        cfg = DeviceConfig(name="X", eid="e", properties={})
        wrap = SGrDevice(cfg, device=fake)
        wrap.connected = True
        info = wrap.to_dict()
        assert "level_of_operation" not in info
