"""HA boundary lifecycle tests with virtual switches; related: runtime.py, climate.py."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntFlag, StrEnum
import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.adaptive_floor_heating.controller import Settings
from custom_components.adaptive_floor_heating.runtime import HeatingRuntime


class FakeBus:
    def __init__(self):
        self.listeners = []

    def async_listen(self, event_type, listener, event_filter=None, *, run_immediately=False):
        item = (event_type, listener, event_filter, run_immediately)
        self.listeners.append(item)
        def remove():
            if item in self.listeners:
                self.listeners.remove(item)
        return remove

    def async_listen_once(self, event_type, listener):
        return self.async_listen(event_type, listener)

    def fire(self, event_type, data):
        for kind, listener, filter_, _ in tuple(self.listeners):
            if kind == event_type and (filter_ is None or filter_(data)):
                listener(SimpleNamespace(data=data))


class FakeStore:
    def __init__(self, hass, version, key):
        self.hass, self.key = hass, key

    async def async_load(self):
        return self.hass.saved.get(self.key)

    def async_delay_save(self, producer, delay):
        self.hass.saved[self.key] = producer()

    async def async_save(self, data):
        self.hass.saved[self.key] = data

    async def async_remove(self):
        self.hass.saved.pop(self.key, None)


class FakeClimateEntity:
    async def async_added_to_hass(self):
        self.remove_callbacks = []

    async def async_will_remove_from_hass(self):
        for callback in self.remove_callbacks:
            callback()

    def async_on_remove(self, callback):
        self.remove_callbacks.append(callback)

    def async_write_ha_state(self):
        self.writes = getattr(self, "writes", 0) + 1


def boundary_modules():
    modules = {name: types.ModuleType(name) for name in (
        "homeassistant", "homeassistant.core", "homeassistant.const", "homeassistant.helpers",
        "homeassistant.helpers.storage", "homeassistant.exceptions", "homeassistant.components",
        "homeassistant.components.climate",
        "homeassistant.components.switch",
        "homeassistant.components.sensor",
        "homeassistant.helpers.device_registry",
    )}
    for name in ("homeassistant", "homeassistant.helpers", "homeassistant.components"):
        modules[name].__path__ = []
    modules["homeassistant.core"].callback = lambda fn: fn
    modules["homeassistant.helpers.storage"].Store = FakeStore
    # Mirror device metadata imports used by climate.py.
    modules["homeassistant.helpers.device_registry"].DeviceInfo = dict
    modules["homeassistant.helpers.device_registry"].DeviceEntryType = SimpleNamespace(SERVICE="service")
    const = modules["homeassistant.const"]
    const.Platform = SimpleNamespace(CLIMATE="climate", SENSOR="sensor", SWITCH="switch")
    const.ATTR_TEMPERATURE = "temperature"
    const.UnitOfTemperature = SimpleNamespace(CELSIUS="°C", FAHRENHEIT="°F")
    const.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic")
    exceptions = modules["homeassistant.exceptions"]
    exceptions.ConfigEntryError = type("ConfigEntryError", (Exception,), {})
    exceptions.ServiceValidationError = type("ServiceValidationError", (Exception,), {})
    climate = modules["homeassistant.components.climate"]
    climate.ClimateEntity = FakeClimateEntity
    climate.ClimateEntityFeature = IntFlag("ClimateEntityFeature", {"TARGET_TEMPERATURE": 1, "TURN_ON": 2, "TURN_OFF": 4, "PRESET_MODE": 8})
    climate.HVACMode = StrEnum("HVACMode", {"OFF": "off", "HEAT": "heat", "AUTO": "auto"})
    climate.HVACAction = StrEnum("HVACAction", {"OFF": "off", "HEATING": "heating", "IDLE": "idle"})
    modules["homeassistant.components.switch"].SwitchEntity = FakeClimateEntity
    sensor = modules["homeassistant.components.sensor"]
    @dataclass(frozen=True, kw_only=True)
    class FakeSensorEntityDescription:
        key: str
        translation_key: str | None = None
        name: str | None = None
        icon: str | None = None
        device_class: str | None = None
        native_unit_of_measurement: str | None = None
        suggested_display_precision: int | None = None
        state_class: str | None = None
        entity_category: str | None = None
        entity_registry_enabled_default: bool = True
    sensor.SensorEntityDescription = FakeSensorEntityDescription
    sensor.SensorEntity = FakeClimateEntity
    sensor.SensorStateClass = SimpleNamespace(MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing")
    sensor.SensorDeviceClass = SimpleNamespace(
        DURATION="duration", TEMPERATURE_DELTA="temperature_delta", TIMESTAMP="timestamp",
        TEMPERATURE="temperature",
    )
    return modules


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module_patch = patch.dict(sys.modules, boundary_modules())
        cls.module_patch.start()
        cls.integration = importlib.import_module("custom_components.adaptive_floor_heating")
        cls.climate = importlib.import_module("custom_components.adaptive_floor_heating.climate")
        cls.sensor = importlib.import_module("custom_components.adaptive_floor_heating.sensor")

    @classmethod
    def tearDownClass(cls):
        cls.module_patch.stop()

    async def asyncSetUp(self):
        self.states, self.calls, self.entities, self.sensor_entities = {}, [], [], []
        self.report_time = datetime(2026, 1, 1, tzinfo=UTC)
        self.confirm_commands = True
        self.hass = SimpleNamespace(
            loop=asyncio.get_running_loop(), bus=FakeBus(), saved={}, data={},
            states=SimpleNamespace(get=self.states.get),
            services=SimpleNamespace(async_call=self.call),
        )
        self.hass.config_entries = SimpleNamespace(
            async_forward_entry_setups=self.forward,
            async_unload_platforms=self.unload,
        )
        self.entry = self.new_entry("one")
        self.write("switch.heater", "off")
        self.write("sensor.room", "18", {"device_class": "temperature", "unit_of_measurement": "°C"})
        self.runtime = self.make_runtime()

    def new_entry(self, entry_id):
        return SimpleNamespace(
            entry_id=entry_id, title="Test thermostat", options={"minimum_on_time": 0, "minimum_off_time": 0},
            data={"mode": "standalone", "heater": "switch.heater", "temperature_sensor": "sensor.room"},
            runtime_data=None, async_on_unload=lambda fn: None,
            add_update_listener=lambda fn: (lambda: None),
        )

    def make_runtime(self, settings=None):
        self.hass.saved.setdefault("adaptive_floor_heating.one.runtime", {
            "schema_version": 1, "target": 20, "mode": "off", "faults": [], "preset": "home",
        })
        runtime = HeatingRuntime(self.hass, self.entry, settings or Settings(minimum_on_time=0, minimum_off_time=0))
        runtime.actuator._timeout = 0.01
        runtime.actuator._retry_delays = (0, 0)
        return runtime

    def write(self, entity_id, value, attributes=None):
        previous = self.states.get(entity_id)
        if attributes is None:
            attributes = previous.attributes if previous else {}
        self.report_time += timedelta(seconds=1)
        state = SimpleNamespace(state=value, attributes=attributes, last_reported=self.report_time)
        self.states[entity_id] = state
        kind = "state_reported" if previous and previous.state == value and previous.attributes == attributes else "state_changed"
        self.hass.bus.fire(kind, {"entity_id": entity_id, "new_state": state, "old_state": previous})

    async def call(self, domain, action, data, blocking):
        self.calls.append((domain, action, data["entity_id"]))
        if self.confirm_commands:
            entity_ids = data["entity_id"]
            for entity_id in entity_ids if isinstance(entity_ids, list) else [entity_ids]:
                self.write(entity_id, "on" if action == "turn_on" else "off")

    async def settle(self):
        for _ in range(12):
            await asyncio.sleep(0)

    async def forward(self, entry, platforms):
        for platform in platforms:
            if platform == "climate":
                entity = self.climate.AdaptiveFloorHeatingClimate(entry, entry.runtime_data)
                entity.hass = self.hass
                self.entities.append(entity)
                await entity.async_added_to_hass()
            if platform == "sensor":
                await self.sensor.async_setup_entry(self.hass, entry, self.sensor_entities.extend)
                for entity in self.sensor_entities:
                    entity.hass = self.hass
                    await entity.async_added_to_hass()

    async def unload(self, entry, platforms):
        for entity in self.entities:
            await entity.async_will_remove_from_hass()
        self.entities.clear()
        for entity in self.sensor_entities:
            await entity.async_will_remove_from_hass()
        self.sensor_entities.clear()
        return True

    async def asyncTearDown(self):
        self.confirm_commands = True
        runtimes = {self.runtime}
        if self.entry.runtime_data:
            runtimes.add(self.entry.runtime_data)
        for runtime in runtimes:
            await runtime.async_stop()
            await runtime._close()

    async def start_heating(self):
        await self.runtime.async_start()
        self.write("sensor.room", "18")
        self.runtime.set_mode("heat")
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)

    async def test_initial_off_then_heat_and_temperature_cutoff(self):
        await self.runtime.async_start()
        await self.settle()
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(self.runtime.controller.last_report)
        self.assertEqual(self.runtime.controller.temperature, 18)
        self.write("sensor.room", "18")
        self.runtime.set_mode("heat")
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)
        self.write("sensor.room", "20.0")
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        self.assertFalse(self.runtime.controller.faults)

    async def test_same_temperature_report_is_consumed(self):
        await self.runtime.async_start()
        first = self.runtime.controller.last_report
        self.assertIsNotNone(first)
        self.write("sensor.room", "18")
        await asyncio.sleep(0)
        self.write("sensor.room", "18")
        self.assertGreater(self.runtime.controller.last_report, first)

    async def test_existing_restored_temperature_makes_climate_available(self):
        self.states["sensor.room"].attributes["restored"] = True
        await self.integration.async_setup_entry(self.hass, self.entry)
        self.assertEqual(self.entities[0].current_temperature, 18)
        self.assertTrue(self.entities[0].available)

    async def test_unavailable_initial_temperature_keeps_climate_unavailable(self):
        attrs = {"device_class": "temperature", "unit_of_measurement": "°C"}
        for invalid in ("unknown", "unavailable"):
            self.assertIsNone(self.runtime._temperature(SimpleNamespace(state=invalid, attributes=attrs)))
        self.write("sensor.room", "unavailable")
        await self.integration.async_setup_entry(self.hass, self.entry)
        self.assertFalse(self.entities[0].available)
        self.assertTrue(self.entry.runtime_data.controller.sensor_fault)

    async def test_concurrent_entity_starts_subscribe_only_once(self):
        original_load = self.runtime.store.load

        async def delayed_load(**kwargs):
            await asyncio.sleep(0)
            return await original_load(**kwargs)

        self.runtime.store.load = delayed_load
        await asyncio.gather(self.runtime.async_start(), self.runtime.async_start())
        self.assertEqual(len(self.hass.bus.listeners), 3)
        self.assertTrue(self.runtime.started)

    async def test_unchanged_sensor_reports_use_immediate_state_reported_listener(self):
        await self.runtime.async_start()
        reported_listener = next(
            item for item in self.hass.bus.listeners if item[0] == "state_reported"
        )
        self.assertTrue(reported_listener[3])
        self.write("sensor.room", "18")
        self.assertEqual(len(self.runtime.observation.history.samples), 1)

    async def test_last_temperature_report_sensor_tracks_unchanged_reports(self):
        await self.integration.async_setup_entry(self.hass, self.entry)
        timestamp_sensor = next(
            entity for entity in self.sensor_entities
            if entity.entity_description.key == "temperature_last_reported"
        )
        initial_report = timestamp_sensor.native_value
        self.assertEqual(initial_report, self.states["sensor.room"].last_reported)
        self.write("sensor.room", "18")
        self.assertGreater(timestamp_sensor.native_value, initial_report)
        self.assertEqual(timestamp_sensor.native_value, self.states["sensor.room"].last_reported)

    async def test_sensor_report_gap_preserves_observation_history(self):
        self.runtime = self.make_runtime(Settings(minimum_on_time=0, minimum_off_time=0, sensor_timeout=0.02))
        await self.start_heating()
        await self.integration.async_setup_entry(self.hass, self.entry)
        await asyncio.sleep(0.04)
        await self.settle()
        self.assertFalse(self.runtime.controller.sensor_fault)
        self.assertEqual(self.runtime.controller.temperature, 18)
        self.assertTrue(self.runtime.actuator.observed)
        self.assertEqual(len(self.runtime.observation.history.samples), 1)
        self.assertTrue(self.entities[0].available)

    async def test_minimum_off_timer_starts_without_new_sensor_event(self):
        self.runtime = self.make_runtime(Settings(minimum_on_time=0, minimum_off_time=0.02))
        await self.runtime.async_start()
        self.write("sensor.room", "18")
        self.runtime.set_mode("heat")
        self.assertFalse(self.runtime.actuator.observed)
        await asyncio.sleep(0.04)
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)

    async def test_invalid_sensor_stops_and_valid_ha_state_recovers_immediately(self):
        await self.start_heating()
        self.write("sensor.room", "unavailable")
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        self.write("sensor.room", "18")
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)

    async def test_external_switch_change_latches_off(self):
        await self.start_heating()
        self.write("switch.heater", "off")
        await self.settle()
        self.assertEqual(self.runtime.controller.mode, "off")
        self.assertIn("external_override", self.runtime.controller.faults)
        self.runtime.set_mode("heat")
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)

    async def test_restart_confirms_off_and_waits_for_cooldown(self):
        self.hass.saved["adaptive_floor_heating.one.runtime"] = {
            "schema_version": 1, "mode": "heat", "target": 23, "faults": [], "auto_control": True}
        self.write("switch.heater", "on")
        self.runtime = self.make_runtime(Settings())
        await self.runtime.async_start()
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        self.assertEqual(self.runtime.controller.target, 23)
        self.assertEqual(self.runtime.controller.mode, "auto")
        self.write("sensor.room", "18")
        await self.settle()
        self.assertEqual(self.runtime.decision.state, "WAIT_MIN_OFF")
        self.assertEqual([call[1] for call in self.calls], ["turn_off"])

    async def test_storage_corruption_starts_off(self):
        self.hass.saved["adaptive_floor_heating.one.runtime"] = {"schema_version": 9, "mode": "heat"}
        await self.runtime.async_start()
        self.write("sensor.room", "18")
        await self.settle()
        self.assertEqual(self.runtime.controller.mode, "off")
        self.assertEqual(self.calls, [])

    async def test_learned_model_restores_and_flushes_separately(self):
        from custom_components.adaptive_floor_heating.thermal_model import ThermalLearningModel

        model = ThermalLearningModel()
        model.add_cycle(SimpleNamespace(response_delay_minutes=45, residual_rise=0.7, peak_delay_minutes=35))
        self.hass.saved["adaptive_floor_heating.one.learning"] = model.snapshot()
        await self.runtime.async_start()
        self.assertEqual(self.runtime.thermal_model.metrics["residual_rise"]["mean"], 0.7)
        self.runtime.thermal_model.add_cycle(
            SimpleNamespace(response_delay_minutes=50, residual_rise=0.9, peak_delay_minutes=45)
        )
        self.assertTrue(await self.runtime.async_stop())
        saved = self.hass.saved["adaptive_floor_heating.one.learning"]
        self.assertEqual(saved["metrics"]["residual_rise"]["samples"], 2)
        self.assertEqual(self.hass.saved["adaptive_floor_heating.one.runtime"]["mode"], "off")

    async def test_completed_observation_trains_model_without_heater_command(self):
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        await self.runtime.async_start()

        def complete_cycle(temperature, now):
            self.runtime.observation.completed_cycles += 1
            self.runtime.observation.last_cycle = CompletedCycle(45, 0.7, 35)

        self.runtime.observation.report_temperature = complete_cycle
        self.write("sensor.room", "19")
        await self.settle()
        self.assertEqual(self.runtime.thermal_model.accepted_cycles, 1)
        self.assertEqual(self.runtime.thermal_model.metrics["residual_rise"]["mean"], 0.7)
        self.assertEqual(self.calls, [])

    async def test_failed_unload_keeps_subscriptions_and_bounds_retries(self):
        await self.start_heating()
        self.confirm_commands = False
        self.assertFalse(await self.runtime.async_stop())
        self.assertTrue(self.runtime.started)
        self.assertEqual(len(self.hass.bus.listeners), 3)
        self.assertEqual([call[1] for call in self.calls].count("turn_off"), 3)
        self.runtime.evaluate()
        await self.settle()
        self.assertEqual([call[1] for call in self.calls].count("turn_off"), 3)

    async def test_successful_stop_removes_all_listeners_and_timers(self):
        await self.start_heating()
        self.assertTrue(await self.runtime.async_stop())
        self.assertEqual(len(self.hass.bus.listeners), 0)
        self.assertIsNone(self.runtime._timer)
        self.assertIsNone(self.runtime._queued)
        self.assertFalse(self.runtime.started)

    async def test_climate_entity_services_and_entry_reload(self):
        self.hass.saved.pop("adaptive_floor_heating.one.runtime", None)
        await self.integration.async_setup_entry(self.hass, self.entry)
        self.assertEqual(len(self.sensor_entities), 34)
        self.assertEqual(len(self.sensor.WATER_EXPERIMENTS), 12)
        self.assertTrue(all(not d.entity_registry_enabled_default for d in self.sensor.WATER_EXPERIMENTS))
        self.assertEqual(len(self.sensor.EXPERIMENTS), 7)
        self.assertTrue(all(not d.entity_registry_enabled_default for d in self.sensor.EXPERIMENTS))
        slope_sensor = next(
            entity for entity in self.sensor_entities
            if entity.entity_description.key == "temperature_slope"
        )
        self.assertEqual(slope_sensor._attr_unique_id, "one_temperature_slope")
        entity = self.entities[0]
        self.assertEqual(entity.target_temperature, 23)
        self.assertTrue(entity.available)
        self.write("sensor.room", "18")
        self.assertGreater(self.sensor_entities[0].writes, 0)
        self.assertTrue(entity.available)
        await entity.async_set_temperature(temperature=23)
        await entity.async_turn_on()
        await self.settle()
        self.assertEqual(entity.hvac_mode, "heat")
        self.assertEqual(entity.hvac_action, "heating")
        self.assertEqual(entity.target_temperature, 23)
        await entity.async_set_preset_mode("away")
        self.assertEqual(entity.preset_mode, "away")
        self.assertEqual(entity.target_temperature, 18)
        self.assertEqual(entity._attr_unique_id, "one_climate")
        with self.assertRaises(Exception):
            await entity.async_set_temperature(temperature=float("nan"))
        self.assertTrue(await self.integration.async_unload_entry(self.hass, self.entry))
        self.assertFalse(self.hass.data["adaptive_floor_heating"]["owners"])
        await self.integration.async_setup_entry(self.hass, self.entry)
        self.assertEqual(len(self.hass.bus.listeners), 3)
        self.assertEqual(len(self.entities), 1)
        self.assertEqual(entity.preset_mode, "away")

    def test_diagnostic_sensor_metadata_matches_metric_meaning(self):
        descriptions = {item.key: item for item in self.sensor.OBSERVATIONS}
        self.assertEqual(descriptions["temperature_last_reported"].device_class, "timestamp")
        for key in ("heating_response_delay", "peak_delay", "learned_heating_response_delay", "learned_peak_delay"):
            self.assertEqual(descriptions[key].device_class, "duration")
        for key in ("residual_rise", "learned_residual_rise"):
            self.assertEqual(descriptions[key].device_class, "temperature_delta")
        for key in ("observed_cycles", "accepted_learning_cycles", "rejected_learning_cycles", "rejected_metric_samples"):
            self.assertEqual(descriptions[key].state_class, "total_increasing")
        self.assertTrue(all(item.icon for item in descriptions.values()))
        self.assertIsNone(descriptions["temperature_slope"].device_class)
        self.assertIsNone(descriptions["learned_heating_rate"].device_class)
        self.assertEqual(
            descriptions["learned_heat_loss_rate"].native_unit_of_measurement, "1/h"
        )
        self.assertIsNone(descriptions["learned_heat_loss_rate"].device_class)
        self.assertIsNone(descriptions["learning_confidence"].device_class)

    async def test_configured_outdoor_and_water_sensors_enable_heat_loss_learning(self):
        self.entry.options.update({
            "outdoor_temperature_sensor": "sensor.outdoor",
            "supply_temperature_sensor": "sensor.supply",
            "return_temperature_sensor": "sensor.return",
        })
        for entity_id, temperature in (
            ("sensor.outdoor", "5"), ("sensor.supply", "40"), ("sensor.return", "30"),
        ):
            self.states[entity_id] = SimpleNamespace(
                state=temperature,
                attributes={"device_class": "temperature", "unit_of_measurement": "°C"},
            )
        self.runtime = self.make_runtime()
        now = self.hass.loop.time()
        self.runtime._optional_reported_at.update({
            "sensor.outdoor": now, "sensor.supply": now, "sensor.return": now,
        })
        self.assertTrue(self.runtime.heat_loss_observation.use_supply_sensor)
        self.assertTrue(self.runtime.heat_loss_observation.use_return_sensor)
        self.assertEqual(self.runtime._optional_temperature("sensor.outdoor", now), 5)
        self.assertEqual(self.runtime._optional_temperature("sensor.supply", now), 40)

    async def test_exclusive_runtime_ownership(self):
        await self.integration.async_setup_entry(self.hass, self.entry)
        other = self.new_entry("two")
        with self.assertRaisesRegex(Exception, "already owned"):
            await self.integration.async_setup_entry(self.hass, other)
        self.assertEqual(self.hass.data["adaptive_floor_heating"]["owners"], {"switch.heater": "one"})

    async def test_integrated_entry_reserves_every_heater(self):
        self.entry.data = {
            "mode": "multi_zone_integrated", "room_count": 2,
            "zones": [{"zone_id": "1", "heater": "switch.heater"},
                      {"zone_id": "2", "heater": "switch.heater_2"}],
            "temperature_sensor": "sensor.room",
        }
        self.write("switch.heater_2", "off")
        await self.integration.async_setup_entry(self.hass, self.entry)
        owners = self.hass.data["adaptive_floor_heating"]["owners"]
        self.assertEqual(owners, {"switch.heater": "one", "switch.heater_2": "one"})
        other = self.new_entry("two")
        other.data = {"mode": "standalone", "heater": "switch.heater_2", "temperature_sensor": "sensor.room"}
        with self.assertRaisesRegex(Exception, "already owned"):
            await self.integration.async_setup_entry(self.hass, other)
        self.assertEqual(len(self.entities), 1)

    async def test_multi_zone_remains_configuration_only(self):
        self.entry.data = {"mode": "multi_zone", "room_count": 1, "zones": []}
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.assertIsNone(self.entry.runtime_data)
        self.assertEqual(self.calls, [])

    async def test_integrated_multi_room_controls_all_switches_as_one_climate(self):
        self.entry.data = {
            "mode": "multi_zone_integrated", "room_count": 2,
            "zones": [{"zone_id": "1", "heater": "switch.heater"},
                      {"zone_id": "2", "heater": "switch.heater_2"}],
            "temperature_sensor": "sensor.room",
        }
        self.write("switch.heater_2", "off")
        runtime = self.make_runtime()
        self.entry.runtime_data = runtime
        await runtime.async_start()
        runtime.set_mode("heat")
        await self.settle()
        self.assertEqual(runtime.heaters, ("switch.heater", "switch.heater_2"))
        self.assertTrue(runtime.actuator.observed)
        self.assertEqual(self.calls[0], ("switch", "turn_on", ["switch.heater", "switch.heater_2"]))
        self.assertEqual(self.states["switch.heater"].state, "on")
        self.assertEqual(self.states["switch.heater_2"].state, "on")
        runtime.set_mode("off")
        await self.settle()
        self.assertFalse(runtime.actuator.observed)
        self.assertEqual(self.states["switch.heater"].state, "off")
        self.assertEqual(self.states["switch.heater_2"].state, "off")

    async def test_integrated_mixed_startup_locks_and_requests_group_off(self):
        self.entry.data = {
            "mode": "multi_zone_integrated", "room_count": 2,
            "zones": [{"zone_id": "1", "heater": "switch.heater"},
                      {"zone_id": "2", "heater": "switch.heater_2"}],
            "temperature_sensor": "sensor.room",
        }
        self.write("switch.heater_2", "on")
        runtime = self.make_runtime()
        self.entry.runtime_data = runtime
        await runtime.async_start()
        await self.settle()
        self.assertIn("heater_unavailable", runtime.controller.faults)
        self.assertEqual(self.calls[0], ("switch", "turn_off", ["switch.heater", "switch.heater_2"]))
        self.assertFalse(runtime.actuator.observed)

    async def test_integrated_external_room_change_turns_every_switch_off(self):
        self.entry.data = {
            "mode": "multi_zone_integrated", "room_count": 2,
            "zones": [{"zone_id": "1", "heater": "switch.heater"},
                      {"zone_id": "2", "heater": "switch.heater_2"}],
            "temperature_sensor": "sensor.room",
        }
        self.write("switch.heater_2", "off")
        runtime = self.make_runtime()
        self.entry.runtime_data = runtime
        await runtime.async_start()
        runtime.set_mode("heat")
        await self.settle()
        self.write("switch.heater_2", "off")
        await self.settle()
        self.assertIn("external_override", runtime.controller.faults)
        self.assertEqual(runtime.controller.mode, "off")
        self.assertFalse(runtime.actuator.observed)
        self.assertEqual(self.states["switch.heater"].state, "off")
        self.assertEqual(self.states["switch.heater_2"].state, "off")
        self.assertEqual(self.calls[-1], ("switch", "turn_off", ["switch.heater", "switch.heater_2"]))

    async def test_off_then_late_on_is_stopped_and_latched(self):
        await self.runtime.async_start()
        self.write("sensor.room", "18")
        self.confirm_commands = False
        self.runtime.set_mode("heat")
        await asyncio.sleep(0)
        self.runtime.set_mode("off")
        await self.settle()
        self.write("switch.heater", "off")
        self.confirm_commands = True
        self.write("switch.heater", "on")
        await asyncio.sleep(0.02)
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        self.assertIn("external_override", self.runtime.controller.faults)
        self.assertEqual(self.runtime.controller.mode, "off")

    async def test_off_state_changed_during_storage_prevents_detachment(self):
        await self.start_heating()
        original_save = self.runtime.store.save
        async def save_with_late_on(data):
            await original_save(data)
            self.confirm_commands = False
            self.write("switch.heater", "on")
        self.runtime.store.save = save_with_late_on
        self.assertFalse(await self.runtime.async_stop())
        self.assertTrue(self.runtime.started)
        self.assertEqual(len(self.hass.bus.listeners), 3)
        self.runtime.store.save = original_save

    async def test_repeated_off_reports_preserve_cancelled_on_protection(self):
        await self.runtime.async_start()
        self.confirm_commands = False
        self.runtime.set_mode("heat")
        await asyncio.sleep(0)
        self.runtime.set_mode("off")
        await self.settle()
        self.write("switch.heater", "off")
        self.runtime.set_mode("off")
        self.write("switch.heater", "unavailable")
        self.confirm_commands = True
        self.write("switch.heater", "on")
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        self.assertIn("external_override", self.runtime.controller.faults)
        self.assertEqual(self.calls[-1][1], "turn_off")

    async def test_reselecting_auto_does_not_interrupt_heating(self):
        await self.runtime.async_start()
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)
        started = self.runtime.observation.heating_started
        self.calls.clear()
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)
        self.assertEqual(self.runtime.observation.heating_started, started)
        self.assertEqual(self.calls, [])

    async def test_reselecting_preset_reapplies_configured_temperature(self):
        await self.runtime.async_start()
        for preset, target in (("home", 23), ("away", 18)):
            self.runtime.set_preset(preset)
            self.runtime.set_target(25)
            self.runtime.set_preset(preset)
            self.assertEqual(self.runtime.controller.target, target)
            self.assertEqual(self.runtime.controller.mode, "off")

    async def test_entry_unload_failure_retains_heater_owner(self):
        await self.integration.async_setup_entry(self.hass, self.entry)
        runtime = self.entry.runtime_data
        runtime.actuator._timeout = 0.01
        runtime.actuator._retry_delays = (0, 0)
        self.write("sensor.room", "18")
        runtime.set_mode("auto")
        await self.settle()
        self.confirm_commands = False
        self.assertFalse(await self.integration.async_unload_entry(self.hass, self.entry))
        self.assertEqual(self.hass.data["adaptive_floor_heating"]["owners"], {"switch.heater": "one"})
        self.assertEqual(len(self.entities), 1)

    async def test_off_mode_observes_external_heating_without_actuating(self):
        self.hass.saved["adaptive_floor_heating.one.runtime"] = {
            "schema_version": 1, "target": 20, "mode": "off", "faults": [], "preset": "home",
        }
        await self.runtime.async_start()
        self.write("sensor.room", "19")
        self.write("switch.heater", "on")
        await self.settle()
        self.assertTrue(self.runtime.observation.heater_state)
        self.assertFalse(self.runtime.controller.faults)
        self.assertEqual(self.runtime.decision.state, "OFF")
        self.assertEqual(self.calls, [])

    async def test_heat_mode_always_controls_and_off_mode_observes_only(self):
        await self.start_heating()
        self.runtime.set_mode("off")
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        call_count = len(self.calls)
        self.write("switch.heater", "on")
        await self.settle()
        self.assertEqual(len(self.calls), call_count)
        self.assertNotIn("external_override", self.runtime.controller.faults)
        self.assertTrue(self.runtime.observation.heater_state)

    async def test_auto_control_uses_confidence_weighted_residual_cutoff(self):
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        await self.runtime.async_start()
        self.write("sensor.room", "18")
        self.runtime.set_mode("auto")
        await self.settle()
        cycle = CompletedCycle(45, 0.5, 35, 0.8)
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(cycle)
        self.write("sensor.room", "20.0")
        self.runtime.evaluate()
        await self.settle()
        self.assertEqual(self.runtime.decision.state, "IDLE")
        self.assertEqual(self.calls[-1][1], "turn_off")
        self.assertFalse(self.runtime.actuator.observed)

    async def test_auto_control_waits_for_residual_prediction_before_restart(self):
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        await self.runtime.async_start()
        self.write("sensor.room", "19.9")
        self.runtime.set_mode("auto")
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(CompletedCycle(45, 1.1, 35, 0.8))
        now = self.hass.loop.time()
        self.runtime.observation.observe_heater(True, now - 900, 19)
        self.runtime.observation.observe_heater(False, now, 19.4)
        self.write("sensor.room", "19.4")
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        self.assertEqual(self.runtime.decision.state, "PREDICTIVE_WAIT")
        self.assertEqual(self.calls, [])

    async def test_old_learning_does_not_block_start_without_a_coast(self):
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        self.runtime = self.make_runtime(Settings(minimum_on_time=900, minimum_off_time=0))
        await self.runtime.async_start()
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(CompletedCycle(30, 1, 30, 0.5))
        self.runtime.actuator.changed_at -= 86400
        self.write("sensor.room", "19.5")
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)

    async def test_residual_wait_expires_without_another_temperature_report(self):
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        self.runtime = self.make_runtime(Settings(minimum_on_time=900, minimum_off_time=0))
        await self.runtime.async_start()
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(CompletedCycle(30, 1, 0.001, 0.5))
        now = self.hass.loop.time()
        self.runtime.observation.observe_heater(True, now - 900, 19)
        self.runtime.observation.observe_heater(False, now, 19.5)
        self.write("sensor.room", "19.5")
        self.runtime.set_mode("auto")
        self.assertEqual(self.runtime.decision.state, "PREDICTIVE_WAIT")
        await asyncio.sleep(0.08)
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)

    async def test_residual_wait_does_not_add_already_observed_rise_twice(self):
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        self.runtime = self.make_runtime(Settings(minimum_on_time=900, minimum_off_time=0))
        await self.runtime.async_start()
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(CompletedCycle(30, 0.6, 30, 0.5))
        now = self.hass.loop.time()
        self.runtime.observation.observe_heater(True, now - 900, 18)
        self.runtime.observation.observe_heater(False, now, 19)
        self.write("sensor.room", "19.5")
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)

    async def test_predictive_on_starts_before_hysteresis_lower_bound(self):
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        self.runtime = self.make_runtime(Settings(minimum_on_time=900, minimum_off_time=0))
        await self.runtime.async_start()
        now = self.hass.loop.time()
        self.runtime.controller.mode = "auto"
        self.runtime.controller.startup_off_seen = True
        self.states["sensor.room"] = SimpleNamespace(
            state="20.05", attributes={"device_class": "temperature", "unit_of_measurement": "°C"}
        )
        self.runtime._read_current_temperature(now)
        self.runtime.observation.history.add(now - 1200, 20.1167)
        self.runtime.observation.history.add(now - 600, 20.0833)
        self.runtime.observation.history.add(now, 20.05)
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(CompletedCycle(30, 0.1, 20, 0.4))

        base = self.runtime.controller.decide(now, False, self.runtime.actuator.changed_at)
        self.assertEqual(self.runtime._apply_learned_prediction(base, now).state, "PREDICTIVE_ON")
        self.runtime.evaluate()
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)
        self.assertEqual(self.runtime.decision.state, "HEATING")
        self.assertEqual(self.calls[-1][1], "turn_on")

    async def test_predictive_on_never_shortens_minimum_off_time(self):
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        self.runtime = self.make_runtime(Settings(minimum_off_time=60, minimum_on_time=900))
        await self.runtime.async_start()
        now = self.hass.loop.time()
        self.runtime.controller.mode = "auto"
        self.runtime.controller.startup_off_seen = True
        self.states["sensor.room"] = SimpleNamespace(
            state="20.05", attributes={"device_class": "temperature", "unit_of_measurement": "°C"}
        )
        self.runtime._read_current_temperature(now)
        self.runtime.observation.history.add(now - 1200, 20.1167)
        self.runtime.observation.history.add(now - 600, 20.0833)
        self.runtime.observation.history.add(now, 20.05)
        self.runtime.actuator.changed_at = now - 30
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(CompletedCycle(30, 0.1, 20, 0.4))

        self.runtime.evaluate()
        self.assertEqual(self.runtime.decision.state, "IDLE")
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(self.runtime._timer)

        self.runtime.actuator.changed_at = now - 61
        base = self.runtime.controller.decide(now, False, self.runtime.actuator.changed_at)
        self.assertEqual(self.runtime._apply_learned_prediction(base, now).state, "PREDICTIVE_ON")
        self.runtime.evaluate()
        await self.settle()
        self.assertIn(self.runtime.decision.state, ("HEATING", "IDLE"))
        self.assertEqual(self.calls[-1][1], "turn_on")

    async def test_predictive_off_waits_for_minimum_on_time(self):
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        self.runtime = self.make_runtime(Settings(minimum_on_time=60, minimum_off_time=0))
        await self.runtime.async_start()
        self.write("sensor.room", "18")
        self.runtime.set_mode("auto")
        await self.settle()
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(CompletedCycle(30, 0.5, 20, 0.4))
        now = self.hass.loop.time()
        self.write("sensor.room", "20.0")

        self.runtime.evaluate()
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)
        self.assertNotEqual(self.runtime.decision.state, "PREDICTIVE_OFF")

        self.runtime.actuator.changed_at = now - 61
        self.runtime.evaluate()
        await self.settle()
        self.assertEqual(self.calls[-1][1], "turn_off")
        self.assertFalse(self.runtime.actuator.observed)

    async def test_home_away_presets_apply_configured_setpoints(self):
        self.runtime.controller.settings = Settings(home_temperature=23, away_temperature=18)
        await self.runtime.async_start()
        self.runtime.set_preset("away")
        self.assertEqual(self.runtime.controller.target, 18)
        self.runtime.set_preset("home")
        self.assertEqual(self.runtime.controller.target, 23)
        with self.assertRaises(ValueError):
            self.runtime.set_preset("comfort")

    async def test_pipe_diagnostics_do_not_change_auto_decision_or_commands(self):
        # Diagnostic reads must have no actuator/model side effects; related: water_observation.py.
        await self.runtime.async_start()
        self.runtime.set_mode("auto")
        await self.settle()
        now = self.hass.loop.time()
        before = (self.runtime.decision, list(self.calls), self.runtime.thermal_model.snapshot())
        for key, value in (("supply", 40), ("return", 35), ("room", 23)):
            self.runtime.water_observation.report(key, value, now)
        for _ in range(3):
            self.runtime.water_observation.snapshot(now)
        self.assertEqual(before, (
            self.runtime.decision, self.calls, self.runtime.thermal_model.snapshot()
        ))

    async def test_residual_target_tracks_auto_stop_offset_for_cutoff_and_wait(self):
        # Check upper-target boundaries for both residual paths; related: runtime.py.
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        await self.runtime.async_start()
        self.runtime.controller.target = 23
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(CompletedCycle(30, 1, 60, 0.4))
        now = self.hass.loop.time()
        for offset in (0.5, 1.0):
            self.runtime.controller.settings = Settings(
                hot_tolerance=offset, minimum_on_time=0, minimum_off_time=0
            )
            for on in (True, False):
                self.runtime.actuator.observed = on
                self.runtime.actuator.changed_at = now - 1200
                for below in (True, False):
                    temperature = 23 + offset - 1 - (0.01 if below else 0)
                    self.runtime.controller.temperature = temperature
                    self.runtime.observation.seed_heater(False)
                    self.runtime.observation.observe_heater(True, now - 1800, temperature)
                    self.runtime.observation.observe_heater(False, now - 1200, temperature)
                    result = self.runtime._apply_learned_prediction(
                        Decision(True, "HEATING"), now
                    )
                    expected = "HEATING" if below else (
                        "PREDICTIVE_OFF" if on else "PREDICTIVE_WAIT"
                    )
                    self.assertEqual(result.state, expected, (offset, on, below))

    async def test_heat_and_auto_are_distinct_control_modes(self):
        await self.runtime.async_start()
        self.write("sensor.room", "18")
        self.runtime.set_mode("heat")
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)
        self.runtime.set_mode("off")
        await self.settle()
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertEqual(self.runtime.controller.mode, "auto")
        self.assertTrue(self.runtime.actuator.observed)
