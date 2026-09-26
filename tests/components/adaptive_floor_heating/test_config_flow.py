"""Flow behavior tests with HA boundary doubles; related: config_flow.py.

Run using unittest and real voluptuous. These checks do not replace HA integration
or frontend tests; the doubles model only the flow return values and state APIs.
"""

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import voluptuous as vol


class FlowBoundary:
    """Model HA's form results and completed/in-progress entry lookups."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def async_show_menu(self, **kwargs):
        return {"type": "menu", **kwargs}

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}

    def _async_current_entries(self):
        return self.hass.entries

    def _async_in_progress(self):
        return self.hass.progress


class SelectorBoundary:
    """Retain selector metadata while voluptuous validates required fields."""

    def __init__(self, config):
        self.config = config

    def __call__(self, value):
        return value


def ha_boundaries():
    """Build isolated HA interfaces without installing or starting Home Assistant."""
    modules = {
        name: types.ModuleType(name)
        for name in (
            "homeassistant", "homeassistant.config_entries", "homeassistant.core",
            "homeassistant.const", "homeassistant.helpers", "homeassistant.helpers.selector",
        )
    }
    modules["homeassistant"].__path__ = []
    modules["homeassistant.helpers"].__path__ = []
    entries = modules["homeassistant.config_entries"]
    entries.ConfigFlow = FlowBoundary
    entries.OptionsFlow = FlowBoundary
    entries.ConfigEntry = SimpleNamespace
    modules["homeassistant.core"].HomeAssistant = SimpleNamespace
    modules["homeassistant.core"].callback = lambda func: func
    const = modules["homeassistant.const"]
    for key, value in {
        "ATTR_DEVICE_CLASS": "device_class", "ATTR_UNIT_OF_MEASUREMENT": "unit_of_measurement",
        "STATE_OFF": "off", "STATE_ON": "on", "STATE_UNAVAILABLE": "unavailable",
        "STATE_UNKNOWN": "unknown",
    }.items():
        setattr(const, key, value)
    const.UnitOfTemperature = SimpleNamespace(CELSIUS="°C", FAHRENHEIT="°F")
    selector = modules["homeassistant.helpers.selector"]
    selector.EntitySelector = selector.NumberSelector = SelectorBoundary
    selector.EntitySelectorConfig = selector.NumberSelectorConfig = dict
    selector.NumberSelectorMode = SimpleNamespace(BOX="box")
    return modules


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    """Exercise branches, invalid input, reservation conflicts, and final data."""

    @classmethod
    def setUpClass(cls):
        cls.module_patch = patch.dict(sys.modules, ha_boundaries())
        cls.module_patch.start()
        cls.flow_module = importlib.import_module(
            "custom_components.adaptive_floor_heating.config_flow"
        )

    @classmethod
    def tearDownClass(cls):
        cls.module_patch.stop()

    def setUp(self):
        self.states = {}
        self.hass = SimpleNamespace(
            states=SimpleNamespace(get=self.states.get), entries=[], progress=[],
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        for i in range(1, 34):
            self.states[f"switch.room_{i}"] = SimpleNamespace(state="off", attributes={})
            self.states[f"sensor.room_{i}"] = SimpleNamespace(
                state="21.5", attributes={"device_class": "temperature", "unit_of_measurement": "°C"}
            )
        self.flow = self.new_flow("first")

    def tearDown(self):
        self.hass.services.async_call.assert_not_called()

    def new_flow(self, flow_id):
        flow = self.flow_module.ConfigFlow()
        flow.hass, flow.flow_id, flow.context = self.hass, flow_id, {}
        self.hass.progress.append({"flow_id": flow_id, "context": flow.context})
        return flow

    @staticmethod
    def pair(i):
        return {"heater": f"switch.room_{i}", "temperature_sensor": f"sensor.room_{i}"}

    async def test_menu_and_standalone_data(self):
        menu = await self.flow.async_step_user()
        self.assertEqual(menu["menu_options"], ["standalone", "multi_zone"])
        form = await self.flow.async_step_standalone()
        with self.assertRaises(vol.Invalid):
            form["data_schema"]({"heater": "switch.room_1"})
        result = await self.flow.async_step_standalone(self.pair(1))
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {"mode": "standalone", **self.pair(1)})

    async def test_three_rooms_preserve_matching_and_count(self):
        result = await self.flow.async_step_multi_zone({"room_count": 3.0})
        for i in range(1, 4):
            self.assertEqual(result["description_placeholders"]["room_number"], str(i))
            result = await self.flow.async_step_room(self.pair(i))
            if i < 3:
                self.assertEqual(result["type"], "form")
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {
            "mode": "multi_zone", "room_count": 3,
            "zones": [{"zone_id": str(i), **self.pair(i)} for i in range(1, 4)],
        })

    async def test_invalid_room_counts_do_not_advance(self):
        for value in (None, True, False, 0, -1, 33, 1.5, float("nan"), float("inf"), "2"):
            with self.subTest(value=value):
                result = await self.flow.async_step_multi_zone({"room_count": value})
                self.assertEqual(result["errors"], {"room_count": "invalid_room_count"})
                self.assertEqual(self.flow._room_count, 0)

    async def test_room_count_boundaries(self):
        for count in (1, 32):
            flow = self.new_flow(f"count_{count}")
            await flow.async_step_multi_zone({"room_count": count})
            for i in range(1, count + 1):
                result = await flow.async_step_room(self.pair(i))
            self.assertEqual(len(result["data"]["zones"]), count)
            self.hass.progress = [p for p in self.hass.progress if p["flow_id"] != flow.flow_id]

    async def test_duplicate_room_switch_preserves_progress(self):
        await self.flow.async_step_multi_zone({"room_count": 2})
        await self.flow.async_step_room(self.pair(1))
        result = await self.flow.async_step_room(self.pair(1))
        self.assertEqual(result["errors"]["heater"], "duplicate_heater")
        self.assertEqual(len(self.flow._zones), 1)
        result = await self.flow.async_step_room(self.pair(2))
        self.assertEqual(result["type"], "create_entry")

    async def test_completed_entry_switches_cannot_be_reused(self):
        for data in ({"mode": "standalone", **self.pair(1)}, {"zones": [self.pair(1)]}):
            with self.subTest(data=data):
                self.hass.entries = [SimpleNamespace(data=data)]
                result = await self.flow.async_step_standalone(self.pair(1))
                self.assertEqual(result["errors"]["heater"], "duplicate_heater")

    async def test_in_progress_reservation_and_cancel_release(self):
        await self.flow.async_step_multi_zone({"room_count": 2})
        await self.flow.async_step_room(self.pair(1))
        other = self.new_flow("second")
        result = await other.async_step_standalone(self.pair(1))
        self.assertEqual(result["errors"]["heater"], "duplicate_heater")
        self.hass.progress = [p for p in self.hass.progress if p["flow_id"] != "first"]
        result = await other.async_step_standalone(self.pair(1))
        self.assertEqual(result["type"], "create_entry")

    async def test_unavailable_previous_room_is_rechecked(self):
        await self.flow.async_step_multi_zone({"room_count": 2})
        await self.flow.async_step_room(self.pair(1))
        self.states["sensor.room_1"].state = "unavailable"
        result = await self.flow.async_step_room(self.pair(2))
        self.assertEqual(result, {"type": "abort", "reason": "entities_changed"})

    async def test_invalid_entities_and_sensor_values(self):
        for field, value in (("heater", "sensor.room_1"), ("heater", "switch.missing"),
                             ("temperature_sensor", "switch.room_1"), ("temperature_sensor", None)):
            result = await self.flow.async_step_standalone({**self.pair(1), field: value})
            self.assertEqual(result["errors"][field], "invalid_entity")
        for value in ("unknown", "unavailable", "NaN", "inf", "-inf", "bad"):
            self.states["sensor.room_1"].state = value
            result = await self.flow.async_step_standalone(self.pair(1))
            self.assertIn("temperature_sensor", result["errors"])

    async def test_sensor_unit_class_and_switch_state(self):
        sensor = self.states["sensor.room_1"]
        sensor.attributes["unit_of_measurement"] = "K"
        result = await self.flow.async_step_standalone(self.pair(1))
        self.assertEqual(result["errors"]["temperature_sensor"], "invalid_unit")
        sensor.attributes["unit_of_measurement"] = "°F"
        sensor.attributes["device_class"] = "humidity"
        result = await self.flow.async_step_standalone(self.pair(1))
        self.assertEqual(result["errors"]["temperature_sensor"], "invalid_temperature")
        sensor.attributes["device_class"] = "temperature"
        self.states["switch.room_1"].state = "opening"
        result = await self.flow.async_step_standalone(self.pair(1))
        self.assertEqual(result["errors"]["heater"], "invalid_switch")
        self.states["switch.room_1"].state = "on"
        result = await self.flow.async_step_standalone(self.pair(1))
        self.assertEqual(result["type"], "create_entry")

    async def test_room_step_requires_count(self):
        self.assertEqual(await self.flow.async_step_room(), {"type": "abort", "reason": "invalid_step"})

    async def test_shared_temperature_sensor_is_allowed(self):
        await self.flow.async_step_multi_zone({"room_count": 2})
        await self.flow.async_step_room(self.pair(1))
        result = await self.flow.async_step_room({**self.pair(2), "temperature_sensor": "sensor.room_1"})
        self.assertEqual(result["type"], "create_entry")

    async def test_control_options_validate_and_save(self):
        flow = self.flow_module.ConfigFlow.async_get_options_flow(None)
        flow.config_entry = SimpleNamespace(options={})
        result = await flow.async_step_init({"minimum_on_time": -1})
        self.assertEqual(result["errors"], {"minimum_on_time": "invalid_setting"})
        result = await flow.async_step_init({"minimum_off_time": 120})
        self.assertEqual(result["data"]["minimum_off_time"], 120)
        self.assertEqual(result["data"]["minimum_on_time"], 900)
        self.assertEqual(result["data"]["cold_tolerance"], 0.5)
        self.assertEqual(result["data"]["hot_tolerance"], 0.5)
        self.assertEqual(result["data"]["heat_hot_tolerance"], 0.0)
        result = await flow.async_step_init({
            "cold_tolerance": 0.8, "hot_tolerance": 0.6, "heat_hot_tolerance": 0.2,
        })
        self.assertEqual(result["data"]["cold_tolerance"], 0.8)
        self.assertEqual(result["data"]["hot_tolerance"], 0.6)
        self.assertEqual(result["data"]["heat_hot_tolerance"], 0.2)


if __name__ == "__main__":
    unittest.main()
