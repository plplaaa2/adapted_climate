"""Check detached curve snapshots and permission-scoped reads; related: curve_api.py."""

from copy import deepcopy
import asyncio
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_floor_heating.const import DOMAIN
from custom_components.adaptive_floor_heating.curve_api import curve_snapshot, heating_curve_preview, get_curve_memory, get_curve_cycles, register_curve_api
from custom_components.adaptive_floor_heating.curve_learning import CurveStandards
from custom_components.adaptive_floor_heating.curve_memory import Bucket


class CurveApiTests(unittest.TestCase):
    def setUp(self):
        self.model = CurveStandards()
        memory = self.model.memory["WARM_HEATING"]
        memory.current[0] = Bucket(0, 0, 8, 1000, 0)
        memory.current[1] = Bucket(0.1, 0.001, 8, 1000, 0)
        memory.long_term[0] = Bucket(-0.05, 0, 8, 10, 3)
        self.model.accepted["WARM_HEATING"] = 8
        self.runtime = SimpleNamespace(started=True, curve_store=SimpleNamespace(model=self.model))

    def test_snapshot_is_detached_and_does_not_mutate_memory(self):
        before = deepcopy(self.model.memory["WARM_HEATING"].dump())
        result = curve_snapshot(self.runtime, now=1000)
        curve = result["curves"]["WARM_HEATING"]
        self.assertEqual(result["unit"], "°C")
        self.assertEqual(curve["current"][0]["delta"], 0)
        self.assertEqual(curve["current"][0]["minutes"], 5)
        self.assertEqual(curve["long_term"][0]["promotions"], 3)
        self.assertEqual(curve["accepted"], 8)
        self.assertEqual(curve["promotion"]["effective_minimum_samples"], 6)
        curve["promotion"]["buckets"][0]["samples"] = 999
        curve["current"][0]["delta"] = 999
        self.assertEqual(self.model.memory["WARM_HEATING"].dump(), before)

    def test_current_recency_and_long_term_confidence_remain_distinct(self):
        memory = self.model.memory["WARM_HEATING"]
        memory.long_term[1] = Bucket(0.1, 0, 8, 10, 3)
        curve = curve_snapshot(self.runtime, now=1000+31*86400)["curves"]["WARM_HEATING"]
        self.assertEqual(curve["current_confidence"], 0)
        self.assertEqual(curve["long_term_confidence"], 1)

    def test_outdoor_cooling_snapshot_reports_both_blended_sample_counts(self):
        memory = self.model.outdoor_cooling_memory["COLD"]
        self.model.outdoor_cooling[("COLD", 0)] = (-0.3, 30)
        memory.current[0] = Bucket(-0.2, 0, 6, 1000, 0)
        memory.long_term[0] = Bucket(-0.1, 0, 12, 1000, 3)
        point = curve_snapshot(self.runtime, now=1000)["curves"]["COOLING"]["outdoor"][0]
        self.assertEqual(point["layer"], "blended")
        self.assertEqual((point["current_samples"], point["long_term_samples"]), (6, 12))
        self.assertAlmostEqual(point["delta"], -0.15)

    def preview_runtime(self, kind="WARM_HEATING"):
        self.runtime.controller = SimpleNamespace(temperature=22.0)
        self.runtime.actuator = SimpleNamespace(observed=True)
        self.runtime.curve_tracker = SimpleNamespace(cycle=SimpleNamespace(
            started_at=850, off_at=None, invalid_reason=None, curve_type=kind))
        return self.runtime

    def test_preview_integrates_partial_bucket_without_mutating_memory(self):
        runtime = self.preview_runtime()
        self.model.memory["WARM_HEATING"].current[0] = Bucket(0.2, 0, 8, 1000, 0)
        before = deepcopy(self.model.memory["WARM_HEATING"].__dict__)
        result = heating_curve_preview(runtime, now=1000)
        self.assertEqual(result["trajectory"], [[0, 0], [2.5, 0.1], [7.5, 0.2]])
        self.assertAlmostEqual(result["predicted_peak"], 22.2)
        self.assertEqual(self.model.memory["WARM_HEATING"].__dict__, before)

    def test_preview_preserves_zero_and_stops_at_missing_bucket(self):
        runtime = self.preview_runtime()
        self.model.memory["WARM_HEATING"].current.pop(1)
        self.model.memory["WARM_HEATING"].current[2] = Bucket(0.4, 0, 8, 1000, 0)
        result = heating_curve_preview(runtime, now=1000)
        self.assertEqual(result["trajectory"], [[0, 0], [2.5, 0]])

    def test_preview_fallback_and_safety_gates(self):
        runtime = self.preview_runtime("PREDICTIVE_WARM_HEATING")
        self.assertIsNotNone(heating_curve_preview(runtime, now=1000))
        runtime.actuator.observed = False
        self.assertIsNone(heating_curve_preview(runtime, now=1000))
        runtime.actuator.observed = True
        runtime.curve_tracker.cycle.off_at = 999
        self.assertIsNone(heating_curve_preview(runtime, now=1000))
        runtime.curve_tracker.cycle.off_at = None
        runtime.curve_tracker.cycle.invalid_reason = "MANUAL_TARGET_CHANGE"
        self.assertIsNone(heating_curve_preview(runtime, now=1000))

    def test_missing_or_stopped_store_is_unavailable(self):
        for runtime in (None, SimpleNamespace(started=False, curve_store=self.runtime.curve_store), SimpleNamespace(started=True, curve_store=None)):
            self.assertEqual(curve_snapshot(runtime)["status"], "unavailable")

    def run_query(self, allowed=True, admin=False, cycles=False, query=None, **overrides):
        registry_entry = SimpleNamespace(**{
            "platform": DOMAIN, "domain": "climate", "disabled_by": None,
            "hidden_by": None, "config_entry_id": "room", **overrides})
        er = SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get=lambda entity_id: registry_entry))
        helpers = ModuleType("homeassistant.helpers")
        helpers.entity_registry = er
        permissions = ModuleType("homeassistant.auth.permissions.const")
        permissions.POLICY_READ = "read"
        connection = SimpleNamespace(user=SimpleNamespace(is_admin=admin, permissions=SimpleNamespace(check_entity=Mock(return_value=allowed))), send_error=Mock(), send_result=Mock())
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=lambda entry_id: SimpleNamespace(runtime_data=self.runtime)))
        with patch.dict(sys.modules, {"homeassistant.helpers": helpers, "homeassistant.auth.permissions.const": permissions}):
            msg = {"id": 7, "entity_id": "climate.renamed", **(query or {})}
            if cycles:
                asyncio.run(get_curve_cycles(hass, connection, msg))
            else:
                get_curve_memory(hass, connection, msg)
        return connection

    def test_read_permission_denied_does_not_return_data(self):
        connection = self.run_query(allowed=False)
        connection.send_result.assert_not_called()
        self.assertEqual(connection.send_error.call_args.args[1], "unauthorized")

    def test_authorized_read_and_admin_read_return_snapshot(self):
        for admin, allowed in ((False, True), (True, False)):
            connection = self.run_query(admin=admin, allowed=allowed)
            self.assertEqual(connection.send_result.call_args.args[1]["status"], "ready")
            connection.send_error.assert_not_called()

    def test_foreign_non_climate_disabled_hidden_entities_are_rejected(self):
        for options in ({"platform": "other"}, {"domain": "sensor"}, {"disabled_by": "user"}, {"hidden_by": "user"}):
            connection = self.run_query(**options)
            connection.send_result.assert_not_called()
            self.assertEqual(connection.send_error.call_args.args[1], "not_found")

    def test_command_registration_is_idempotent(self):
        websocket = SimpleNamespace(websocket_command=lambda schema: lambda fn: fn,
                                    async_response=lambda fn: fn, async_register_command=Mock())
        components = ModuleType("homeassistant.components")
        components.websocket_api = websocket
        core = ModuleType("homeassistant.core")
        core.callback = lambda fn: fn
        helpers = ModuleType("homeassistant.helpers")
        helpers.config_validation = SimpleNamespace(entity_id=lambda value: value)
        hass = SimpleNamespace(data={})
        with patch.dict(sys.modules, {"homeassistant.components": components, "homeassistant.core": core, "homeassistant.helpers": helpers}):
            register_curve_api(hass)
            register_curve_api(hass)
        self.assertEqual(websocket.async_register_command.call_count, 2)
        self.assertEqual([call.args[1] for call in websocket.async_register_command.call_args_list],
                         [get_curve_memory, get_curve_cycles])

    # Ensure diagnostic reads cannot bypass entity authorization; related: curve_api.py, curve_storage.py.
    def test_cycles_permission_ownership_filters_and_storage_errors(self):
        import sqlite3
        read = AsyncMock(return_value={"status": "ready", "cycles": [{"quality_reason": "INCOMPLETE_PEAK"}]})
        self.runtime.curve_store.read_cycles = read
        for options in ({"allowed": False}, {"platform": "other"}, {"domain": "sensor"},
                        {"disabled_by": "user"}, {"hidden_by": "user"}):
            connection = self.run_query(cycles=True, **options)
            connection.send_result.assert_not_called()
            read.assert_not_awaited()
        query = {"limit": 5, "curve_type": "WARM_HEATING", "accepted": False}
        connection = self.run_query(cycles=True, query=query)
        read.assert_awaited_once_with(5, "WARM_HEATING", False, 0, None)
        self.assertEqual(connection.send_result.call_args.args[1]["cycles"][0]["quality_reason"], "INCOMPLETE_PEAK")
        read.reset_mock()
        connection = self.run_query(cycles=True, query={"limit": 10, "offset": 100, "snapshot": 250})
        read.assert_awaited_once_with(10, None, None, 100, 250)
        read.reset_mock()
        connection = self.run_query(cycles=True, query={"limit": 5, "heating_only": True})
        read.assert_awaited_once_with(5, None, None, 0, None, heating_only=True)
        read.side_effect = sqlite3.OperationalError("private path")
        connection = self.run_query(cycles=True)
        self.assertEqual(connection.send_error.call_args.args[1], "unavailable")
        self.assertNotIn("private", connection.send_error.call_args.args[2])
        self.runtime.started = False
        connection = self.run_query(cycles=True)
        self.assertEqual(connection.send_result.call_args.args[1]["status"], "unavailable")
