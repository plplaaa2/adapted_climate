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

    async def finish_setup(self, flow):
        """Complete the ordered heating, preset, sensor, and confirmation pages."""
        heating = await flow.async_step_heating_settings({
            "cold_tolerance": 0.5, "hot_tolerance": 0.5,
            "heat_hot_tolerance": 0.0, "minimum_on_time": 900,
            "minimum_off_time": 600, "sensor_timeout": 900,
        })
        self.assertEqual(heating["step_id"], "preset_temperatures")
        sensors = await flow.async_step_preset_temperatures({
            "home_temperature": 23.0, "away_temperature": 18.0,
        })
        self.assertEqual(sensors["step_id"], "optional_sensors")
        confirm = await flow.async_step_optional_sensors({})
        self.assertEqual(confirm["step_id"], "confirm")
        return await flow.async_step_confirm({"confirm": True})

    async def test_menu_and_standalone_data(self):
        menu = await self.flow.async_step_user()
        self.assertEqual(menu["menu_options"], ["standalone", "multi_zone_integrated"])
        form = await self.flow.async_step_standalone()
        with self.assertRaises(vol.Invalid):
            form["data_schema"]({"heater": "switch.room_1"})
        result = await self.flow.async_step_standalone(self.pair(1))
        self.assertEqual(result["step_id"], "heating_settings")
        result = await self.finish_setup(self.flow)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {"mode": "standalone", **self.pair(1)})
        self.assertEqual(result["options"]["home_temperature"], 23.0)
        self.assertIsNone(result["options"]["outdoor_temperature_sensor"])

    async def test_integrated_multi_room_uses_one_sensor_and_unique_switches(self):
        result = await self.flow.async_step_multi_zone_integrated({"room_count": 2})
        self.assertEqual(result["type"], "form")
        result = await self.flow.async_step_integrated_sensor({"temperature_sensor": "sensor.room_10"})
        self.assertEqual(result["type"], "form")
        result = await self.flow.async_step_integrated_room({"heater": "switch.room_1"})
        self.assertEqual(result["description_placeholders"]["room_number"], "2")
        result = await self.flow.async_step_integrated_room({"heater": "switch.room_2"})
        self.assertEqual(result["step_id"], "heating_settings")
        result = await self.finish_setup(self.flow)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {
            "mode": "multi_zone_integrated", "room_count": 2,
            "zones": [{"zone_id": "1", "heater": "switch.room_1"},
                      {"zone_id": "2", "heater": "switch.room_2"}],
            "temperature_sensor": "sensor.room_10",
        })

    async def test_integrated_flow_rejects_too_few_rooms_and_duplicates(self):
        result = await self.flow.async_step_multi_zone_integrated({"room_count": 1})
        self.assertEqual(result["errors"], {"room_count": "invalid_integrated_room_count"})
        await self.flow.async_step_multi_zone_integrated({"room_count": 2})
        await self.flow.async_step_integrated_sensor({"temperature_sensor": "sensor.room_1"})
        await self.flow.async_step_integrated_room({"heater": "switch.room_1"})
        result = await self.flow.async_step_integrated_room({"heater": "switch.room_1"})
        self.assertEqual(result["errors"], {"heater": "duplicate_heater"})
        result = await self.flow.async_step_integrated_room({"heater": "switch.room_2"})
        result = await self.finish_setup(self.flow)
        self.assertEqual(result["type"], "create_entry")

    async def test_integrated_flow_rechecks_shared_sensor_before_saving(self):
        await self.flow.async_step_multi_zone_integrated({"room_count": 2})
        await self.flow.async_step_integrated_sensor({"temperature_sensor": "sensor.room_1"})
        await self.flow.async_step_integrated_room({"heater": "switch.room_1"})
        self.states["sensor.room_1"].state = "unavailable"
        result = await self.flow.async_step_integrated_room({"heater": "switch.room_2"})
        self.assertEqual(result, {"type": "abort", "reason": "entities_changed"})

    async def test_optional_sensors_require_temperature_class_and_remain_optional(self):
        await self.flow.async_step_standalone(self.pair(1))
        await self.flow.async_step_heating_settings({
            "cold_tolerance": 0.5, "hot_tolerance": 0.5,
            "heat_hot_tolerance": 0.0, "minimum_on_time": 900,
            "minimum_off_time": 600, "sensor_timeout": 900,
        })
        await self.flow.async_step_preset_temperatures({
            "home_temperature": 23.0, "away_temperature": 18.0,
        })
        form = await self.flow.async_step_optional_sensors()
        self.assertEqual(form["data_schema"]({}), {})
        self.assertEqual(len(form["data_schema"].schema), 3)
        self.assertTrue(all(
            field.config["domain"] == "sensor"
            and field.config["device_class"] == "temperature"
            for field in form["data_schema"].schema.values()
        ))
        self.states["sensor.room_3"].attributes["device_class"] = "humidity"
        result = await self.flow.async_step_optional_sensors({
            "outdoor_temperature_sensor": "sensor.room_3",
        })
        self.assertEqual(result["errors"], {"outdoor_temperature_sensor": "invalid_temperature"})

    async def test_setup_requires_explicit_final_confirmation(self):
        await self.flow.async_step_standalone(self.pair(1))
        result = await self.finish_setup(self.flow)
        self.assertEqual(result["type"], "create_entry")

    async def test_options_save_only_after_final_confirmation(self):
        flow = self.flow_module.ConfigFlow.async_get_options_flow(None)
        flow.config_entry = SimpleNamespace(options={"supply_temperature_sensor": "sensor.room_1"})
        flow.hass = self.hass
        result = await flow.async_step_init({"minimum_off_time": 120})
        self.assertEqual(result["step_id"], "preset_temperatures")
        result = await flow.async_step_preset_temperatures({"home_temperature": 24, "away_temperature": 19})
        self.assertEqual(result["step_id"], "optional_sensors")
        result = await flow.async_step_optional_sensors({"outdoor_temperature_sensor": "sensor.room_3"})
        self.assertEqual(result["step_id"], "confirm")
        self.assertEqual(result["description_placeholders"]["outdoor_temperature_sensor"], "sensor.room_3")
        result = await flow.async_step_confirm({"confirm": True})
        self.assertEqual(result["data"]["minimum_off_time"], 120)
        self.assertEqual(result["data"]["minimum_on_time"], 900)
        self.assertEqual(result["data"]["cold_tolerance"], 0.5)
        self.assertEqual(result["data"]["hot_tolerance"], 0.5)
        self.assertEqual(result["data"]["heat_hot_tolerance"], 0.0)
        self.assertEqual(result["data"]["home_temperature"], 24.0)
        self.assertEqual(result["data"]["away_temperature"], 19.0)
        self.assertEqual(result["data"]["outdoor_temperature_sensor"], "sensor.room_3")
        self.assertEqual(result["data"]["supply_temperature_sensor"], "sensor.room_1")

    async def test_three_rooms_preserve_matching_and_count(self):
        result = await self.flow.async_step_multi_zone_integrated({"room_count": 3.0})
        result = await self.flow.async_step_integrated_sensor({"temperature_sensor": "sensor.room_10"})
        for i in range(1, 4):
            self.assertEqual(result["description_placeholders"]["room_number"], str(i))
            result = await self.flow.async_step_integrated_room({"heater": f"switch.room_{i}"})
            if i < 3:
                self.assertEqual(result["type"], "form")
        result = await self.finish_setup(self.flow)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {
            "mode": "multi_zone_integrated", "room_count": 3,
            "zones": [{"zone_id": str(i), "heater": f"switch.room_{i}"} for i in range(1, 4)],
            "temperature_sensor": "sensor.room_10",
        })

    async def test_invalid_integrated_room_counts_do_not_advance(self):
        for value in (None, True, False, 1, 0, -1, 33, 1.5, float("nan"), float("inf"), "2"):
            with self.subTest(value=value):
                result = await self.flow.async_step_multi_zone_integrated({"room_count": value})
                self.assertEqual(result["errors"], {"room_count": "invalid_integrated_room_count"})
                self.assertEqual(self.flow._room_count, 0)

    async def test_integrated_room_count_boundaries(self):
        for count in (2, 32):
            flow = self.new_flow(f"count_{count}")
            await flow.async_step_multi_zone_integrated({"room_count": count})
            await flow.async_step_integrated_sensor({"temperature_sensor": "sensor.room_33"})
            for i in range(1, count + 1):
                result = await flow.async_step_integrated_room({"heater": f"switch.room_{i}"})
            result = await self.finish_setup(flow)
            self.assertEqual(len(result["data"]["zones"]), count)
            self.hass.progress = [p for p in self.hass.progress if p["flow_id"] != flow.flow_id]

    async def test_duplicate_integrated_room_switch_preserves_progress(self):
        await self.flow.async_step_multi_zone_integrated({"room_count": 2})
        await self.flow.async_step_integrated_sensor({"temperature_sensor": "sensor.room_1"})
        await self.flow.async_step_integrated_room({"heater": "switch.room_1"})
        result = await self.flow.async_step_integrated_room({"heater": "switch.room_1"})
        self.assertEqual(result["errors"]["heater"], "duplicate_heater")
        self.assertEqual(len(self.flow._zones), 1)
        result = await self.flow.async_step_integrated_room({"heater": "switch.room_2"})
        result = await self.finish_setup(self.flow)
        self.assertEqual(result["type"], "create_entry")

    async def test_completed_entry_switches_cannot_be_reused(self):
        for data in ({"mode": "standalone", **self.pair(1)}, {"zones": [self.pair(1)]}):
            with self.subTest(data=data):
                self.hass.entries = [SimpleNamespace(data=data)]
                result = await self.flow.async_step_standalone(self.pair(1))
                self.assertEqual(result["errors"]["heater"], "duplicate_heater")

    async def test_in_progress_reservation_and_cancel_release(self):
        await self.flow.async_step_multi_zone_integrated({"room_count": 2})
        await self.flow.async_step_integrated_sensor({"temperature_sensor": "sensor.room_10"})
        await self.flow.async_step_integrated_room({"heater": "switch.room_1"})
        other = self.new_flow("second")
        result = await other.async_step_standalone(self.pair(1))
        self.assertEqual(result["errors"]["heater"], "duplicate_heater")
        self.hass.progress = [p for p in self.hass.progress if p["flow_id"] != "first"]
        result = await other.async_step_standalone(self.pair(1))
        result = await self.finish_setup(other)
        self.assertEqual(result["type"], "create_entry")

    async def test_unavailable_previous_room_is_rechecked(self):
        await self.flow.async_step_multi_zone_integrated({"room_count": 2})
        await self.flow.async_step_integrated_sensor({"temperature_sensor": "sensor.room_1"})
        await self.flow.async_step_integrated_room({"heater": "switch.room_1"})
        self.states["sensor.room_1"].state = "unavailable"
        result = await self.flow.async_step_integrated_room({"heater": "switch.room_2"})
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
        result = await self.finish_setup(self.flow)
        self.assertEqual(result["type"], "create_entry")

    async def test_integrated_sensor_and_room_steps_require_room_count(self):
        invalid = {"type": "abort", "reason": "invalid_step"}
        self.assertEqual(await self.flow.async_step_integrated_sensor(), invalid)
        self.assertEqual(await self.flow.async_step_integrated_room(), invalid)

    async def test_control_options_validate_and_save(self):
        """Retain validation and ordered navigation in the first options page."""
        flow = self.flow_module.ConfigFlow.async_get_options_flow(None)
        flow.config_entry = SimpleNamespace(options={"supply_temperature_sensor": "sensor.room_1"})
        flow.hass = self.hass
        result = await flow.async_step_init({"minimum_on_time": -1})
        self.assertEqual(result["errors"], {"minimum_on_time": "invalid_setting"})
        result = await flow.async_step_heating_settings({"minimum_off_time": 120})
        self.assertEqual(result["step_id"], "preset_temperatures")


if __name__ == "__main__":
    unittest.main()
