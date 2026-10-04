"""Check detached curve snapshots and permission-scoped reads; related: curve_api.py."""

from copy import deepcopy
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

from custom_components.adaptive_floor_heating.const import DOMAIN
from custom_components.adaptive_floor_heating.curve_api import curve_snapshot, get_curve_memory, register_curve_api
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
        curve["current"][0]["delta"] = 999
        self.assertEqual(self.model.memory["WARM_HEATING"].dump(), before)

    def test_current_recency_and_long_term_confidence_remain_distinct(self):
        memory = self.model.memory["WARM_HEATING"]
        memory.long_term[1] = Bucket(0.1, 0, 8, 10, 3)
        curve = curve_snapshot(self.runtime, now=1000+31*86400)["curves"]["WARM_HEATING"]
        self.assertEqual(curve["current_confidence"], 0)
        self.assertEqual(curve["long_term_confidence"], 1)

    def test_missing_or_stopped_store_is_unavailable(self):
        for runtime in (None, SimpleNamespace(started=False, curve_store=self.runtime.curve_store), SimpleNamespace(started=True, curve_store=None)):
            self.assertEqual(curve_snapshot(runtime)["status"], "unavailable")

    def run_query(self, allowed=True, admin=False, **overrides):
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
            get_curve_memory(hass, connection, {"id": 7, "entity_id": "climate.renamed"})
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
        websocket = SimpleNamespace(websocket_command=lambda schema: lambda fn: fn, async_register_command=Mock())
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
        websocket.async_register_command.assert_called_once_with(hass, get_curve_memory)
