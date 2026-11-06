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

    def async_listen(self, event_type, listener, event_filter=None):
        item = (event_type, listener, event_filter)
        self.listeners.append(item)
        def remove():
            if item in self.listeners:
                self.listeners.remove(item)
        return remove

    def async_listen_once(self, event_type, listener):
        return self.async_listen(event_type, listener)

    def fire(self, event_type, data):
        for kind, listener, filter_ in tuple(self.listeners):
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
        "homeassistant.components.select",
        "homeassistant.components.sensor",
        "homeassistant.helpers.device_registry",
        "homeassistant.helpers.entity_registry",
    )}
    for name in ("homeassistant", "homeassistant.helpers", "homeassistant.components"):
        modules[name].__path__ = []
    modules["homeassistant.core"].callback = lambda fn: fn
    modules["homeassistant.helpers.storage"].Store = FakeStore
    # Mirror device metadata imports used by climate.py.
    modules["homeassistant.helpers.device_registry"].DeviceInfo = dict
    modules["homeassistant.helpers.device_registry"].DeviceEntryType = SimpleNamespace(SERVICE="service")
    modules["homeassistant.helpers.entity_registry"].async_get = lambda hass: hass.entity_registry
    modules["homeassistant.helpers.entity_registry"].async_entries_for_config_entry = (
        lambda registry, entry_id: registry.get(entry_id, [])
    )
    const = modules["homeassistant.const"]
    const.Platform = SimpleNamespace(CLIMATE="climate", SENSOR="sensor", SELECT="select", NUMBER="number", SWITCH="switch")
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
    modules["homeassistant.components.select"].SelectEntity = FakeClimateEntity
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
        options: list[str] | None = None
    sensor.SensorEntityDescription = FakeSensorEntityDescription
    sensor.SensorEntity = FakeClimateEntity
    sensor.SensorStateClass = SimpleNamespace(MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing")
    sensor.SensorDeviceClass = SimpleNamespace(
        DURATION="duration", TEMPERATURE_DELTA="temperature_delta", TIMESTAMP="timestamp",
        TEMPERATURE="temperature", ENUM="enum",
    )
    return modules


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_wait_releases_on_large_cooling_and_expiry(self):
        await self.runtime.async_start()
        now = self.hass.loop.time()
        observation = self.runtime.observation
        observation.observe_heater(True, now - 1800, 24)
        observation.observe_heater(False, now, 24)
        self.runtime.controller.target = 24.1
        self.runtime._off_predictions = {"existing": 24.7, "curve": 24.7}
        self.runtime._off_peak_deadlines = {"existing": now + 6000, "curve": now + 6000}
        for model in ("existing", "curve"):
            self.runtime.controller.temperature = 23.9
            self.assertEqual(self.runtime._wait_after_off(now + 60, model).state, "PREDICTIVE_WAIT")
            self.runtime.controller.temperature = 23.4
            self.assertIsNone(self.runtime._wait_after_off(now + 60, model))
            self.runtime.controller.temperature = 23.9
            self.assertIsNone(self.runtime._wait_after_off(now + 6001, model))

    def test_off_peak_hold_overrides_minimum_off_restart(self):
        """A residual forecast must survive the minimum-OFF boundary."""
        from custom_components.adaptive_floor_heating.controller import Decision

        self.runtime.curve_store = SimpleNamespace(model=None)
        self.runtime.controller.temperature = 22.4
        self.runtime.actuator.observed = False
        hold = Decision(False, "PREDICTIVE_WAIT", self.hass.loop.time() + 3600)
        with patch.object(self.runtime, "_wait_after_off", return_value=hold):
            result = self.runtime._apply_curve_prediction(
                Decision(False, "WAIT_MIN_OFF"), self.hass.loop.time()
            )
        self.assertIs(result, hold)

    # Replay a 30-minute flat ON phase through actual AUTO dispatch; related: off_response.py, curve_memory.py.
    async def test_delayed_short_pulse_stops_and_waits_with_unified_curve_model(self):
        import time
        import tempfile
        from pathlib import Path
        from dataclasses import replace
        from custom_components.adaptive_floor_heating.history import CompletedCycle
        from custom_components.adaptive_floor_heating.curve_learning import CurveResult
        from custom_components.adaptive_floor_heating.curve_storage import CurveStore

        await self.runtime.async_start()
        for selected in ("curve",):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as folder:
                self.calls.clear()
                self.runtime.controller.mode = "off"
                self.write("sensor.room", "24")
                await self.settle()
                now, wall = self.hass.loop.time(), time.time()
                observation, tracker = self.runtime.observation, self.runtime.curve_tracker
                observation.history.clear()
                observation.seed_heater(False)
                observation.observe_heater(True, now - 1800, 24)
                tracker.close_incomplete(wall, "TEST_BOUNDARY")
                tracker.take_results()
                tracker.seed(False, 24, wall - 1800)
                tracker.switch(True, wall - 1800)
                for elapsed in (600, 1200, 1800):
                    observation.report_temperature(24, now - 1800 + elapsed)
                    tracker.report_temperature(24, wall - 1800 + elapsed)
                context = observation.off_context(now)
                sample = CompletedCycle(60, 0.7, 100, 0, heating_duration_minutes=30,
                                        slope_at_off=0, off_context=context)
                for _ in range(8):
                    self.runtime.thermal_model.add_cycle(sample)
                store = CurveStore(SimpleNamespace(config=SimpleNamespace(
                    path=lambda *parts: str(Path(folder).joinpath(*parts)))), "pulse")
                await store.open()
                result = CurveResult(
                    "pulse", "WARM_HEATING", "THRESHOLD_START", "PREDICTIVE_STOP",
                    "PEAK_CONFIRMED", "BALANCED", wall - 7800, wall - 6000, wall, wall,
                    {i: 0 for i in range(6)}, 0.7, None,
                    off_profile={"duration": 30, "slope": 0, "rise": 0.7,
                                 "points": [[0, 0], [10, -0.1], [30, 0], [100, 0.7]], "context": context},
                )
                for i in range(8):
                    await store.save(replace(result, cycle_id=f"pulse-{i}"))
                self.runtime.curve_store = store
                self.runtime.controller.mode = "auto"
                self.runtime.controller.target = 24.1
                self.runtime.controller.startup_off_seen = True
                self.runtime.actuator.observed = True
                self.runtime.actuator.changed_at = now - 1800
                self.runtime._heater_states["switch.heater"] = True
                self.states["switch.heater"] = SimpleNamespace(state="on", attributes={})
                self.runtime.evaluate()
                await self.settle()
                self.assertEqual(self.calls[-1][1], "turn_off")
                self.assertFalse(self.runtime.actuator.observed)
                self.assertEqual(self.runtime.decision.state, "PREDICTIVE_WAIT")
                self.assertAlmostEqual(self.runtime._off_predictions[selected], 24.7)
                from custom_components.adaptive_floor_heating.diagnostics import diagnostic_snapshot
                from custom_components.adaptive_floor_heating.storage import decode_state
                recorded = self.runtime.last_off_prediction
                self.assertEqual(recorded["selected_model"], selected)
                self.assertEqual(recorded["predictions"], self.runtime._off_predictions)
                self.assertEqual(set(recorded["confidences"]), {"curve", "existing"})
                self.assertEqual(decode_state(self.runtime.snapshot())["last_off_prediction"], recorded)
                readings = diagnostic_snapshot(self.runtime)
                self.assertEqual(readings["prediction_status"], "waiting_peak")
                self.assertEqual(readings["observation_status"], "response_wait")
                self.assertIsNotNone(observation.off_at)
                self.assertEqual(tracker.cycle.off_context, context)
                calls_before = list(self.calls)
                self.write("sensor.room", "23.9")
                await self.settle()
                self.assertEqual(self.runtime.decision.state, "PREDICTIVE_WAIT")
                self.assertEqual(self.calls, calls_before)
                self.runtime.curve_store = None
                self.runtime.controller.mode = "off"

    @classmethod
    def setUpClass(cls):
        cls.module_patch = patch.dict(sys.modules, boundary_modules())
        cls.module_patch.start()
        cls.integration = importlib.import_module("custom_components.adaptive_floor_heating")
        cls.climate = importlib.import_module("custom_components.adaptive_floor_heating.climate")
        cls.sensor = importlib.import_module("custom_components.adaptive_floor_heating.sensor")
        cls.select = importlib.import_module("custom_components.adaptive_floor_heating.select")

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
            async_entries=lambda domain: [self.entry],
        )
        self.hass.entity_registry = {}
        self.entry = self.new_entry("one")
        self.write("switch.heater", "off")
        self.write("sensor.room", "18", {"device_class": "temperature", "unit_of_measurement": "°C"})
        self.runtime = self.make_runtime()

    async def test_prediction_confidence_uses_confirmed_peak_errors_not_runtime_sample_count(self):
        confidence, count = self.runtime._prediction_confidence("curve")
        self.assertIsNone(confidence)
        self.assertEqual(count, 0)
        self.runtime.prediction_errors["curve"] = [-0.48]
        confidence, count = self.runtime._prediction_confidence("curve")
        self.assertAlmostEqual(confidence, 0.2 * __import__("math").exp(-0.48 / 0.5))
        self.assertEqual(count, 1)
        self.runtime.prediction_errors["curve"] = [0.0] * 5
        self.assertEqual(self.runtime._prediction_confidence("curve"), (1.0, 5))

    async def test_prediction_validation_errors_persist_and_restore(self):
        from custom_components.adaptive_floor_heating.storage import decode_state
        self.runtime.prediction_errors["curve"] = [-0.48, 0.2]
        restored = decode_state(self.runtime.snapshot())
        self.assertEqual(restored["prediction_errors"]["curve"], [-0.48, 0.2])

    async def test_peak_observation_hours_update_active_observers(self):
        self.runtime.curve_store = SimpleNamespace(
            max_peak_wait_minutes=360,
            model=SimpleNamespace(max_peak_wait_minutes=360),
        )
        self.runtime.set_peak_observation_hours(8)
        self.assertEqual(self.runtime.controller.settings.peak_observation_hours, 8)
        self.assertEqual(self.runtime.observation.max_peak_wait_seconds, 8 * 3600)
        self.assertEqual(self.runtime.curve_tracker.max_peak_wait_seconds, 8 * 3600)
        self.assertEqual(self.runtime.curve_store.max_peak_wait_minutes, 480)
        self.assertEqual(self.runtime.curve_store.model.max_peak_wait_minutes, 480)
        with self.assertRaises(ValueError):
            self.runtime.set_peak_observation_hours(13)

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

    def seed_off_response(self, now, temperature, rise=1.0):
        """Create real learned OFF conditions and a comparable live ON history."""
        from custom_components.adaptive_floor_heating.history import CompletedCycle, TemperatureSample
        for _ in range(8):
            self.runtime.thermal_model.add_cycle(CompletedCycle(
                10, rise, 30, 1.0, response_curve=((0, 0), (10, 1/6), (20, 1/3), (30, 0.5)),
                coast_curve=((0, 0), (10, 0.4), (30, 1)), heating_duration_minutes=30,
                slope_at_off=1.0,
            ))
        observation = self.runtime.observation
        observation.heater_state = True
        observation.heating_started = now - 1800
        observation._on_start_temperature = temperature - 0.5
        observation._on_samples = [TemperatureSample(now - 1800, temperature - 0.5),
                                   TemperatureSample(now - 900, temperature - 0.25),
                                   TemperatureSample(now, temperature)]
        observation.history.clear()
        # Isolate cutoff policy with an evenly reported rise; held-state regression is tested separately.
        for elapsed in range(0, 1801, 300):
            observation.history.add(now - 1800 + elapsed, temperature - .5 + elapsed / 3600)

    def write(self, entity_id, value, attributes=None):
        previous = self.states.get(entity_id)
        if attributes is None:
            attributes = previous.attributes if previous else {}
        self.report_time += timedelta(seconds=1)
        last_changed = (datetime.now(UTC) if previous is None or previous.state != value
                        else previous.last_changed)
        state = SimpleNamespace(state=value, attributes=attributes, last_reported=self.report_time,
                                 last_changed=last_changed)
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

    async def test_unchanged_sensor_reports_use_state_reported_listener(self):
        await self.runtime.async_start()
        reported_listener = next(
            item for item in self.hass.bus.listeners if item[0] == "state_reported"
        )
        self.assertEqual(len(reported_listener), 3)
        self.write("sensor.room", "18")
        self.assertEqual(len(self.runtime.observation.history.samples), 1)

    async def test_temperature_report_time_is_observation_data_not_a_sensor(self):
        await self.integration.async_setup_entry(self.hass, self.entry)
        runtime = self.entry.runtime_data
        initial_report = runtime.temperature_last_reported
        self.assertEqual(initial_report, self.states["sensor.room"].last_reported)
        self.write("sensor.room", "18")
        self.assertGreater(runtime.temperature_last_reported, initial_report)
        self.assertEqual(runtime.temperature_last_reported, self.states["sensor.room"].last_reported)
        self.assertNotIn("temperature_last_reported", {
            entity.entity_description.key for entity in self.sensor_entities
        })

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
        # Runtime validates held HA state without manufacturing reports; related: history.py.
        observation = self.runtime.observation
        started = observation.heating_started
        self.assertIsNotNone(started)
        self.assertIsNotNone(observation.heating_profile(self.hass.loop.time(), .02))
        self.write("sensor.room", "18.1")
        await self.settle()
        self.assertEqual(observation.heating_started, started)
        self.assertIsNotNone(observation.heating_profile(self.hass.loop.time(), .02))
        self.write("sensor.room", "unavailable")
        await self.settle()
        self.assertIsNone(observation.heating_started)
        self.assertIsNone(observation.heating_profile(self.hass.loop.time(), .02))

    async def test_minimum_off_timer_starts_without_new_sensor_event(self):
        self.runtime = self.make_runtime(Settings(minimum_on_time=0, minimum_off_time=0.02))
        await self.runtime.async_start()
        self.write("sensor.room", "18")
        self.runtime.set_mode("heat")
        self.assertFalse(self.runtime.actuator.observed)
        await asyncio.sleep(0.04)
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)

    # Validate time-driven predictive slopes using live held HA state; related: runtime.py, history.py.
    async def test_held_state_refreshes_slope_without_reports_and_resets_on_loss(self):
        await self.runtime.async_start()
        now = self.hass.loop.time()
        observation = self.runtime.observation
        observation.history.clear()
        for elapsed in range(-2700, 1, 300):
            observation.history.add(now + elapsed, 18 + elapsed / 3600)
        self.runtime._read_current_temperature(now)
        self.assertAlmostEqual(observation.temperature_slope, 1)
        reports = observation.history.samples
        self.runtime._read_current_temperature(now + 3000)
        self.assertEqual(observation.temperature_slope, 0)
        self.assertEqual(observation.history.samples, reports)
        self.assertEqual(observation.completed_cycles, 0)
        self.states["sensor.room"].state = "unavailable"
        self.runtime._read_current_temperature(now + 3001)
        self.assertIsNone(observation.temperature_slope)

    async def test_auto_schedules_held_slope_refresh_within_five_minutes(self):
        await self.runtime.async_start()
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertIsNotNone(self.runtime._timer)
        self.assertLessEqual(self.runtime._timer.when() - self.hass.loop.time(), 300)

    # Compare the exact sum used by control and confirmed-OFF diagnostics; related: peak_prediction.py, storage.py.
    async def test_additive_peak_controls_and_records_same_sum_with_isolated_feedback(self):
        import time
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.peak_prediction import sample
        await self.runtime.async_start()
        now, wall = self.hass.loop.time(), time.time()
        model = CurveStandards()
        model.peak_samples = [sample(20, 20.2, 21.2, 60, None, wall-10000-i) for i in range(6)]
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.curve_tracker.cycle = CurveCycle('additive', wall-3600, 'THRESHOLD_START',
                                                      'BALANCED', 'WARM_HEATING', 20, 20.2, wall, wall+300)
        self.runtime.controller.temperature = 20.2
        self.runtime.controller.target = 20.5
        self.runtime.actuator.observed = True
        self.runtime.actuator.changed_at = now-3600
        self.runtime.controller.settings = Settings(minimum_on_time=900, hot_tolerance=0)
        decision = self.runtime._apply_curve_prediction(Decision(True, 'HEATING'), now)
        self.assertEqual(decision.state, 'PREDICTIVE_OFF')
        self.assertEqual(self.runtime.heating_prediction_info,
                         {"model": "additive", "review_status": "not_applicable"})
        controlled = self.runtime.off_prediction['predicted_peak']
        self.assertAlmostEqual(self.runtime._predict_both_off(now)['curve'], controlled)
        self.assertIsNone(self.runtime.off_prediction['confidence'])
        self.runtime.last_off_prediction = dict(off_at=wall, predictions={'curve': controlled},
                                               confidences={'curve': None}, comparison_counts={'curve': 0},
                                               selected_model='curve', peak_forecast=self.runtime._off_peak_forecast)
        self.runtime._off_diagnostic_at = wall
        self.runtime._off_predictions = {'curve': controlled}
        self.runtime._record_peak_comparison(controlled+.2, wall, wall+600, completed_at=wall+700)
        self.assertAlmostEqual(self.runtime.additive_feedback['bias'], .04)
        self.assertEqual(len(self.runtime.additive_feedback['errors']), 1)
        self.assertEqual(self.runtime.prediction_errors['curve'], [])
        # New sum cannot bypass minimum ON or basic sensor/fault decisions.
        self.runtime.actuator.changed_at = now-100
        self.assertEqual(self.runtime._apply_curve_prediction(Decision(True, 'HEATING'), now).state, 'HEATING')
        self.assertEqual(self.runtime._apply_curve_prediction(Decision(False, 'FAULT'), now).state, 'FAULT')
        self.runtime.curve_store = None

    async def test_invalid_sensor_stops_and_valid_ha_state_recovers_immediately(self):
        await self.start_heating()
        self.write("sensor.room", "unavailable")
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        self.write("sensor.room", "18")
        await self.settle()
        self.runtime._minimum_off_until = self.hass.loop.time()
        self.runtime.evaluate()
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)

    async def test_external_switch_change_latches_off(self):
        await self.start_heating()
        self.write("switch.heater", "off")
        await self.settle()
        self.assertEqual(self.runtime.controller.mode, "off")
        self.assertIn("external_override", self.runtime.controller.faults)
        self.runtime.set_mode("heat")
        self.runtime._minimum_off_until = self.hass.loop.time()
        self.runtime.evaluate()
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

    async def test_restart_uses_elapsed_time_since_restored_heater_off(self):
        self.hass.saved["adaptive_floor_heating.one.runtime"] = {
            "schema_version": 1, "mode": "heat", "target": 23, "faults": [], "auto_control": True}
        self.write("switch.heater", "off")
        self.states["switch.heater"].last_changed = datetime.now(UTC) - timedelta(minutes=20)
        self.runtime = self.make_runtime(Settings(minimum_on_time=0, minimum_off_time=600))
        await self.runtime.async_start()
        await self.settle()
        self.assertEqual(self.runtime.decision.state, "HEATING")
        self.assertTrue(self.runtime.actuator.observed)

    async def test_startup_off_state_does_not_start_a_new_minimum_off_wait(self):
        self.hass.saved["adaptive_floor_heating.one.runtime"] = {
            "schema_version": 1, "mode": "heat", "target": 23, "faults": [], "auto_control": True}
        self.runtime = self.make_runtime(Settings(minimum_on_time=0, minimum_off_time=600))
        await self.runtime.async_start()
        await self.settle()
        self.assertEqual(self.runtime.decision.state, "HEATING")
        self.assertTrue(self.runtime.actuator.observed)

    async def test_reconnected_off_heater_resumes_without_cooldown(self):
        await self.runtime.async_start()
        self.runtime.controller.mode = "heat"
        self.runtime.controller.target = 23
        self.runtime.controller.temperature = 18
        self.runtime.controller.startup_off_seen = False
        self.runtime._heater_states["switch.heater"] = None
        self.runtime.actuator.observe(None)
        self.write("switch.heater", "off")
        await self.settle()
        self.assertEqual(self.runtime.decision.state, "HEATING")
        self.assertTrue(self.runtime.actuator.observed)

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
        self.assertEqual(len(self.sensor_entities), 3)
        self.assertEqual(
            {entity.entity_description.key for entity in self.sensor_entities},
            {"learning_status", "observation_status", "prediction_status"},
        )
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
        self.assertEqual(entity.entity_id, "climate.adaptive_heating_climate")
        with self.assertRaises(Exception):
            await entity.async_set_temperature(temperature=float("nan"))
        self.assertTrue(await self.integration.async_unload_entry(self.hass, self.entry))
        self.assertFalse(self.hass.data["adaptive_floor_heating"]["owners"])
        await self.integration.async_setup_entry(self.hass, self.entry)
        self.assertEqual(len(self.hass.bus.listeners), 3)
        self.assertEqual(len(self.entities), 1)
        self.assertEqual(entity.preset_mode, "away")

    async def test_retired_sensors_are_removed_without_touching_learning(self):
        """Clean up only retired owned sensors; related: sensor.py, diagnostics.py."""
        removed, added = [], []
        registry = SimpleNamespace(async_remove=removed.append)
        registered = [
            SimpleNamespace(domain="sensor", unique_id="one_initial_learning_status", entity_id="sensor.renamed_initial"),
            SimpleNamespace(domain="sensor", unique_id="one_water_supply", entity_id="sensor.renamed_pipe"),
            SimpleNamespace(domain="sensor", unique_id="other_water_supply", entity_id="sensor.other_pipe"),
            SimpleNamespace(domain="sensor", unique_id="one_learning_status", entity_id="sensor.learning"),
            SimpleNamespace(domain="sensor", unique_id="other_initial_learning_status", entity_id="sensor.other_initial"),
            SimpleNamespace(domain="switch", unique_id="one_initial_learning_status", entity_id="switch.unrelated"),
        ]
        before = self.runtime.snapshot()
        self.entry.runtime_data = self.runtime
        with patch.object(self.sensor.er, "async_get", return_value=registry), \
             patch.object(self.sensor.er, "async_entries_for_config_entry", return_value=registered):
            await self.sensor.async_setup_entry(self.hass, self.entry, added.extend)
        self.assertEqual(removed, ["sensor.renamed_initial", "sensor.renamed_pipe"])
        self.assertEqual({entity.entity_description.key for entity in added},
                         {"learning_status", "observation_status", "prediction_status"})
        self.assertEqual(self.runtime.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_state_sensor_metadata_contains_only_phase_enums(self):
        descriptions = self.sensor.STATE_SENSORS
        self.assertEqual(len(descriptions), 3)
        self.assertTrue(all(item.icon for item in descriptions))
        self.assertTrue(all(item.device_class == "enum" for item in descriptions))
        self.assertTrue(all(item.native_unit_of_measurement is None for item in descriptions))
        self.assertTrue(all(item.state_class is None for item in descriptions))
        self.assertEqual(
            {item.key for item in descriptions},
            {"learning_status", "observation_status", "prediction_status"},
        )

    async def test_retired_pipe_options_are_ignored_while_outdoor_remains_subscribed(self):
        # Legacy pipe options must not add listeners; related: runtime.py, config_flow.py.
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
        await self.runtime.async_start()
        now = self.hass.loop.time()
        self.assertEqual(self.runtime._optional_sensors, {"sensor.outdoor"})
        self.assertEqual(set(self.runtime._optional_reported_at), {"sensor.outdoor"})
        for kind, _, event_filter in self.hass.bus.listeners:
            if kind in ("state_changed", "state_reported"):
                self.assertTrue(event_filter({"entity_id": "sensor.outdoor"}))
                self.assertFalse(event_filter({"entity_id": "sensor.supply"}))
                self.assertFalse(event_filter({"entity_id": "sensor.return"}))
        self.assertFalse(hasattr(self.runtime, "water_observation"))
        self.assertFalse(hasattr(self.runtime, "supply_sensor"))
        self.assertFalse(hasattr(self.runtime, "return_sensor"))
        self.assertEqual(self.runtime._optional_temperature("sensor.outdoor", now), 5)

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
        self.assertNotIn("heater_unavailable", runtime.controller.faults)
        self.assertEqual(self.calls[0], ("switch", "turn_off", ["switch.heater", "switch.heater_2"]))
        self.assertFalse(runtime.actuator.observed)

    async def test_persistent_startup_heater_unavailable_latches_after_delay(self):
        self.entry.data = {
            "mode": "multi_zone_integrated", "room_count": 2,
            "zones": [{"zone_id": "1", "heater": "switch.heater"},
                      {"zone_id": "2", "heater": "switch.heater_2"}],
            "temperature_sensor": "sensor.room",
        }
        self.write("switch.heater_2", "unavailable")
        self.confirm_commands = False
        runtime = self.make_runtime()
        self.entry.runtime_data = runtime
        await runtime.async_start()
        await asyncio.sleep(10.01)
        runtime.evaluate()
        self.assertIn("heater_unavailable", runtime.controller.faults)
        self.write("switch.heater_2", "off")
        self.assertNotIn("heater_unavailable", runtime.controller.faults)

    async def test_brief_heater_unavailable_does_not_latch_fault(self):
        await self.runtime.async_start()
        now = self.hass.loop.time()
        self.runtime._heater_states["switch.heater"] = None
        self.runtime._mark_heater_unavailable(now)
        self.runtime._heater_states["switch.heater"] = False
        self.runtime.evaluate()
        self.assertNotIn("heater_unavailable", self.runtime.controller.faults)

    async def test_normal_heater_state_clears_unavailable_fault(self):
        await self.runtime.async_start()
        self.runtime.controller.faults.add("heater_unavailable")
        self.write("switch.heater", "off")
        self.assertNotIn("heater_unavailable", self.runtime.controller.faults)

    async def test_heater_recovery_clears_transient_actuation_fault(self):
        await self.runtime.async_start()
        self.runtime.controller.faults.add("actuation_fault")
        self.runtime._heater_states["switch.heater"] = None
        self.runtime.actuator.observe(None)
        self.write("switch.heater", "off")
        self.assertNotIn("actuation_fault", self.runtime.controller.faults)

    async def test_persistent_heater_unavailable_latches_after_ten_seconds(self):
        await self.runtime.async_start()
        now = self.hass.loop.time()
        self.runtime._heater_states["switch.heater"] = None
        self.runtime._mark_heater_unavailable(now)
        await asyncio.sleep(10.01)
        self.runtime.evaluate()
        self.assertIn("heater_unavailable", self.runtime.controller.faults)

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

    async def test_old_scalar_samples_alone_do_not_enable_new_predictive_cutoff(self):
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
        self.assertEqual(self.runtime.decision.state, "HEATING")
        self.assertEqual(self.calls[-1][1], "turn_on")
        self.assertTrue(self.runtime.actuator.observed)

    async def test_auto_cutoff_uses_matching_curve_over_aggregate_fallback(self):
        from custom_components.adaptive_floor_heating.history import CompletedCycle, TemperatureSample

        await self.runtime.async_start()
        self.write("sensor.room", "18")
        self.runtime.set_mode("auto")
        await self.settle()
        self.write("sensor.room", "19.7")
        now = self.hass.loop.time()
        for _ in range(8):
            self.runtime.thermal_model.add_cycle(CompletedCycle(
                30, 1.2, 20, 0.5,
                response_curve=((0, 0), (30, 0.1), (45, 0.3), (90, 0.8)),
                coast_curve=((0, 0), (20, 1)), heating_duration_minutes=90,
                slope_at_off=0.5,
            ))
        self.runtime.thermal_model.metrics["residual_rise"]["mean"] = 0.1
        observation = self.runtime.observation
        observation.heating_started = now - 5400
        observation._on_start_temperature = 18.9
        observation._on_samples = [
            TemperatureSample(now - 5400, 18.9),
            TemperatureSample(now - 2700, 19.325),
            TemperatureSample(now, 19.7),
        ]
        observation.history.clear()
        for elapsed in range(0, 2701, 300):
            observation.history.add(now - 2700 + elapsed, 19.325 + elapsed / 2700 * .375)
        self.runtime.actuator.changed_at = now - 5400
        base = self.runtime.controller.decide(now, True, self.runtime.actuator.changed_at)
        self.assertEqual(base.state, "HEATING")
        self.assertEqual(self.runtime._apply_learned_prediction(base, now).state, "PREDICTIVE_OFF")
        self.runtime.thermal_model.response_curves.clear()
        self.runtime.thermal_model.off_response_profiles.clear()
        self.assertEqual(self.runtime._apply_learned_prediction(base, now).state, "HEATING")

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

    async def test_scalar_learning_without_cooling_curve_uses_basic_start(self):
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
        for elapsed in range(-1200, 1, 300):
            self.runtime.observation.history.add(now + elapsed, 20.05 - elapsed / 3600 * .2)
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(CompletedCycle(30, 0.1, 20, 0.4))

        base = self.runtime.controller.decide(now, False, self.runtime.actuator.changed_at)
        self.assertEqual(self.runtime._apply_learned_prediction(base, now).state, "IDLE")
        self.runtime.evaluate()
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        self.assertEqual(self.runtime.decision.state, "IDLE")
        self.assertEqual(self.runtime.start_prediction["status"], "insufficient_data")
        self.assertEqual(self.calls, [])

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
        for elapsed in range(-1200, 1, 300):
            self.runtime.observation.history.add(now + elapsed, 20.05 - elapsed / 3600 * .2)
        self.runtime.actuator.changed_at = now - 30
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(CompletedCycle(30, 0.1, 20, 0.4))

        self.runtime.evaluate()
        self.assertEqual(self.runtime.decision.state, "IDLE")
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(self.runtime._timer)

        self.runtime.actuator.changed_at = now - 61
        base = self.runtime.controller.decide(now, False, self.runtime.actuator.changed_at)
        self.assertEqual(self.runtime._apply_learned_prediction(base, now).state, "IDLE")
        self.runtime.evaluate()
        await self.settle()
        self.assertIn(self.runtime.decision.state, ("HEATING", "IDLE"))
        self.assertEqual(self.calls, [])

    async def test_legacy_prediction_modes_restore_without_inventing_cooling_evidence(self):
        """Legacy scalar learning alone cannot justify a cooling-curve start."""
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        await self.runtime.async_start()
        now = self.hass.loop.time()
        self.runtime.controller.mode = "auto"
        self.runtime.controller.startup_off_seen = True
        self.states["sensor.room"] = SimpleNamespace(
            state="20.075", attributes={"device_class": "temperature", "unit_of_measurement": "°C"}
        )
        self.runtime._read_current_temperature(now)
        self.runtime.observation.history.add(now - 1200, 20.1417)
        self.runtime.observation.history.add(now - 600, 20.1083)
        self.runtime.observation.history.add(now, 20.075)
        for _ in range(10):
            self.runtime.thermal_model.add_cycle(CompletedCycle(30, 0.1, 20, 0.4))
        base = self.runtime.controller.decide(now, False, self.runtime.actuator.changed_at)
        expected = {"eco": "IDLE", "balanced": "IDLE", "comfort": "IDLE"}
        for mode, state in expected.items():
            self.runtime.prediction_mode = mode
            self.assertEqual(self.runtime._apply_learned_prediction(base, now).state, state)
        self.runtime.prediction_mode = "balanced"
        self.assertEqual(self.runtime.snapshot()["prediction_mode"], "balanced")
        from custom_components.adaptive_floor_heating.storage import decode_state
        self.assertEqual(decode_state(self.runtime.snapshot())["prediction_mode"], "balanced")

    async def test_prediction_select_updates_runtime_and_saved_intent(self):
        await self.runtime.async_start()
        selector = self.select.PredictionModeSelect(self.entry, self.runtime)
        selector.hass = self.hass
        await selector.async_added_to_hass()
        self.assertEqual(selector.current_option, "balanced")
        await selector.async_select_option("eco")
        self.assertEqual(selector.current_option, "eco")
        self.assertEqual(self.hass.saved["adaptive_floor_heating.one.runtime"]["prediction_mode"], "eco")
        with self.assertRaises(Exception):
            await selector.async_select_option("invalid")
        await selector.async_will_remove_from_hass()

    async def test_learning_model_selector_is_removed(self):
        self.assertFalse(hasattr(self.select, "LearningModelSelect"))
        self.assertFalse(hasattr(self.runtime, "set_learning_model"))

    async def test_prediction_switch_preserves_legacy_state_and_toggles(self):
        """Exercise the replacement entity and persisted intent; related: switch.py, storage.py."""
        await self.runtime.async_start()
        module = importlib.import_module("custom_components.adaptive_floor_heating.switch")
        switch = module.PredictionOnSwitch(self.entry, self.runtime)
        for mode, expected in (("eco", False), ("balanced", True), ("comfort", True)):
            self.runtime.prediction_mode = mode
            self.assertEqual(switch.is_on, expected)
        await switch.async_turn_off()
        self.assertEqual(self.runtime.snapshot()["prediction_mode"], "eco")
        await switch.async_turn_on()
        self.assertEqual(self.runtime.snapshot()["prediction_mode"], "comfort")

    async def test_curve_estimator_aims_at_auto_target_plus_stop_offset(self):
        """Curve cutoff uses confirmed OFF-to-peak rise and the AUTO upper target."""
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards

        await self.runtime.async_start()
        now = self.hass.loop.time()
        self.runtime.controller.target = 23
        self.runtime.controller.temperature = 22.5
        self.runtime.controller.settings = Settings(
            hot_tolerance=0.5, minimum_on_time=0, minimum_off_time=0
        )
        self.runtime.actuator.observed = True
        self.runtime.actuator.changed_at = now - 1200
        model = CurveStandards()
        model.buckets["WARM_HEATING"] = {i: (0.1, 12) for i in range(3)}
        model.residual["WARM_HEATING"] = (1.0, 12)
        model.off_profiles["WARM_HEATING"] = [{
            "duration": 30, "slope": 1, "rise": 1,
            "points": [[0, 0], [5, 0.2], [30, 1]],
        } for _ in range(8)]
        self.seed_off_response(now, 22.5)
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.curve_tracker.cycle = CurveCycle(
            "active", 1000, "THRESHOLD_START", "BALANCED", "WARM_HEATING",
            22.2, 22.5, 1900, 2200,
            heating_buckets={0: 0.1, 1: 0.1, 2: 0.1},
        )
        decision = self.runtime._apply_curve_prediction(Decision(True, "HEATING"), now)
        self.assertEqual(decision.state, "PREDICTIVE_OFF")
        self.runtime.controller.temperature = 22.4
        decision = self.runtime._apply_curve_prediction(Decision(True, "HEATING"), now)
        self.assertEqual(decision.state, "HEATING")
        self.runtime.curve_store = None

    async def test_curve_storage_error_uses_existing_auto_estimator(self):
        """An SQLite error does not change heater safety or availability."""
        from custom_components.adaptive_floor_heating.curve_learning import CurveResult, CurveStandards

        await self.runtime.async_start()
        class BrokenCurveStore:
            model = CurveStandards()

            async def save(self, result):
                raise OSError("disk unavailable")

        self.runtime.curve_store = BrokenCurveStore()
        result = CurveResult(
            "failed", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "BALANCED", 0, 600, 900, 1500,
            {0: 0.0, 1: 0.2}, 0.2, None,
        )
        await self.runtime._save_curve_result(result)
        self.assertTrue(self.runtime._curve_storage_failed)
        self.runtime.controller.mode = "auto"
        self.runtime.evaluate()
        self.assertEqual(self.runtime.curve_fallback_reason, "CURVE_STORAGE_ERROR")
        self.runtime.curve_store = None

    async def test_unified_curve_model_learns_completed_reports(self):
        import tempfile
        import time
        from pathlib import Path
        from custom_components.adaptive_floor_heating.curve_storage import CurveStore

        await self.runtime.async_start()
        for selected in ("curve",):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as folder:
                store = CurveStore(SimpleNamespace(config=SimpleNamespace(
                    path=lambda *parts: str(Path(folder).joinpath(*parts)))), "dual")
                await store.open()
                self.runtime.curve_store = store
                now, wall = self.hass.loop.time(), time.time()
                observation, tracker = self.runtime.observation, self.runtime.curve_tracker
                observation.history.clear()
                tracker.close_incomplete(wall, "TEST_BOUNDARY")
                tracker.take_results()
                observation.seed_heater(False)
                tracker.seed(False, 20, wall - 3600)
                observation.observe_heater(True, now - 3600, 20)
                tracker.switch(True, wall - 3600)
                for elapsed, temperature in ((600, 20.1), (1200, 20.2), (1800, 20.3)):
                    observation.report_temperature(temperature, now - 3600 + elapsed)
                    tracker.report_temperature(temperature, wall - 3600 + elapsed)
                observation.observe_heater(False, now - 1800, 20.3)
                tracker.switch(False, wall - 1800)
                for elapsed in (2100, 2300):
                    observation.report_temperature(20.5, now - 3600 + elapsed)
                    tracker.report_temperature(20.5, wall - 3600 + elapsed)
                observation.report_temperature(20.2, now - 600)
                tracker.report_temperature(20.2, wall - 600)
                before = self.runtime.thermal_model.accepted_cycles
                with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall):
                    self.write("sensor.room", "20.1")
                await self.settle()
                if self.runtime._curve_tasks:
                    await asyncio.gather(*tuple(self.runtime._curve_tasks))
                self.assertEqual(self.runtime.thermal_model.accepted_cycles, before + 1)
                self.assertEqual(store.model.accepted["WARM_HEATING"], 1)
                self.assertEqual(len(store.model.off_profiles["WARM_HEATING"]), 1)
                self.assertEqual(self.calls, [])
                tracker.close_incomplete(wall + 1, "TEST_BOUNDARY")
                tracker.take_results()
                self.runtime.curve_store = None

    async def test_prediction_failures_preserve_base_and_safety_decisions(self):
        from custom_components.adaptive_floor_heating.controller import Decision
        await self.runtime.async_start()
        base = Decision(True, "HEATING")
        with patch.object(self.runtime, "_apply_learned_prediction", side_effect=ValueError("bad model")):
            self.assertIsNone(self.runtime._safe_model_prediction("existing", base, self.hass.loop.time()))
        self.runtime.controller.faults.add("actuation_fault")
        self.runtime.evaluate()
        self.assertEqual(self.runtime.decision.state, "FAULT")
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)

    async def test_curve_predictive_on_uses_cooling_coverage_and_policy(self):
        import time
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards
        await self.runtime.async_start()
        now, wall = self.hass.loop.time(), time.time()
        model = CurveStandards()
        model.buckets["PREDICTIVE_WARM_HEATING"] = {0: (0, 3), 1: (0.1, 3)}
        model.buckets["COOLING"] = {i: (-0.1, 3) for i in range(6)}
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.controller.target = 23
        self.runtime.controller.temperature = 22.65
        self.runtime.actuator.changed_at = now - 1800
        self.runtime.curve_tracker.cycle = CurveCycle(
            "cooling", wall - 2000, "THRESHOLD_START", "BALANCED", "WARM_HEATING",
            22, 23.15, wall, wall + 300, off_at=wall - 1000,
            peak_at=wall - 300, peak_temperature=23.4,
        )
        base = Decision(False, "IDLE")
        for mode, expected in (("eco", "IDLE"), ("balanced", "PREDICTIVE_ON"), ("comfort", "PREDICTIVE_ON")):
            self.runtime.prediction_mode = mode
            result = self.runtime._apply_curve_prediction(base, now)
            self.assertEqual(result.state, expected)
        # Full ten-minute loss is 0.2 C: 22.7 C reaches the 22.5 C start threshold.
        for temperature, expected in ((22.71, "IDLE"), (22.70, "PREDICTIVE_ON"), (22.69, "PREDICTIVE_ON")):
            self.runtime.controller.temperature = temperature
            with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall):
                for path in (self.runtime._apply_curve_prediction, self.runtime._apply_learned_prediction):
                    self.assertEqual(path(base, now).state, expected)
            self.assertAlmostEqual(self.runtime.start_prediction["predicted_minimum"], temperature - .2)
        self.runtime._minimum_off_until = now + 60
        self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "IDLE")
        self.assertEqual(self.runtime.start_prediction["status"], "blocked")
        self.runtime._minimum_off_until = None
        # Completed Cooling collection and a restart must retain the measured Peak anchor.
        self.runtime.last_peak_comparison = {"peak_at": wall-300}
        self.runtime.curve_tracker.cycle = None
        model.load_runtime_samples([(30, .4, 20), (40, .8, 20), (50, 1.2, 20)])
        self.runtime.controller.temperature = 22.8
        with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall):
            self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "PREDICTIVE_ON")
        self.assertEqual(self.runtime.start_prediction["response_delay_minutes"], 20)
        self.assertAlmostEqual(self.runtime.start_prediction["predicted_minimum"], 22.4)
        model.buckets["COOLING"].clear()
        self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "IDLE")
        self.assertEqual(self.runtime.start_prediction["status"], "insufficient_data")
        self.runtime.curve_store = None

    async def test_matched_off_response_can_stop_before_review_or_cancel_reservation(self):
        """Honor validated residual predictions and configured minimum ON time."""
        import time
        from dataclasses import replace
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards
        await self.runtime.async_start()
        now, wall = self.hass.loop.time(), time.time()
        model = CurveStandards()
        model.load_runtime_samples([(20, 0.8), (40, 1.6), (60, 2.4)])
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.controller.settings = replace(
            Settings.from_options({"minimum_on_time": 1200, "minimum_off_time": 2400}), hot_tolerance=0
        )
        self.runtime.controller.temperature = 19
        self.runtime.actuator.observed = True
        self.runtime.actuator.changed_at = now - 1800
        self.runtime.curve_tracker.cycle = CurveCycle(
            "response", wall - 1800, "THRESHOLD_START", "BALANCED",
            "WARM_HEATING", 18, 19, wall, wall + 300,
        )
        with patch.object(self.runtime.observation, "heating_profile", return_value=(30, 0)), \
             patch.object(model, "predict_off_response", return_value=SimpleNamespace(rise=2.1)):
            for target, state in ((20, "HEATING"), (21, "HEATING"), (20, "PREDICTIVE_FINAL")):
                with self.subTest(target=target, state=state):
                    self.runtime.controller.target = target
                    if state == "PREDICTIVE_FINAL":
                        self.runtime._runtime_off_started_at = self.runtime.actuator.changed_at
                        self.runtime._runtime_off_execute_at = now + 300
                    result = self.runtime._apply_curve_prediction(Decision(True, state), now)
                    self.assertEqual(result.state, "PREDICTIVE_OFF")
                    self.assertIsNone(self.runtime._runtime_off_execute_at)
            self.runtime.actuator.changed_at = now - 60
            self.runtime.controller.target = 20
            result = self.runtime._apply_curve_prediction(Decision(True, "HEATING"), now)
            self.assertEqual(result.state, "HEATING")
        self.runtime.curve_store = None

    async def test_runtime_curve_predictive_off_uses_required_heater_minutes(self):
        import time
        from dataclasses import replace
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards
        await self.runtime.async_start()
        now, wall = self.hass.loop.time(), time.time()
        model = CurveStandards()
        model.load_runtime_samples([(20, 0.8), (40, 1.6), (60, 2.4)])
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.controller.settings = replace(self.runtime.controller.settings, hot_tolerance=0)
        self.runtime.controller.target = 20
        self.runtime.controller.temperature = 19
        self.runtime.actuator.observed = True
        self.runtime.actuator.changed_at = now - 49 * 60
        self.runtime.curve_tracker.cycle = CurveCycle(
            "active-runtime-curve", wall - 49 * 60, "THRESHOLD_START", "BALANCED",
            "WARM_HEATING", 18, 19, wall, wall + 300,
        )
        base = Decision(True, "HEATING")
        self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "HEATING")
        self.runtime.actuator.changed_at = now - 51 * 60
        result = self.runtime._apply_curve_prediction(base, now)
        self.assertEqual(result.state, "PREDICTIVE_OFF")
        self.assertAlmostEqual(self.runtime.off_prediction["predicted_peak"], 20.04)
        self.runtime.curve_store = None

    async def test_runtime_review_information_preserves_first_plan_and_extensions(self):
        """Keep review evidence separate from additive Peak forecasts; related: runtime.py, panel.js."""
        from custom_components.adaptive_floor_heating.controller import Decision
        await self.runtime.async_start()
        now = self.hass.loop.time()
        self.runtime.actuator.changed_at = now
        self.runtime.controller.temperature = 20
        model = SimpleNamespace(predicted_peak=lambda *args: 20)
        base = Decision(True, "HEATING")
        def decide(at, peak=20):
            return self.runtime._runtime_cutoff_decision(base, at, model, 20, 23,
                (at-now)/60, 30, peak, False, None)
        decide(now)
        info = self.runtime.heating_prediction_info
        self.assertEqual(info["review_status"], "waiting")
        original = info["initial_off_at"]
        decide(now+25*60)
        self.assertEqual(info["review_status"], "extended")
        self.assertEqual(info["extension_minutes"], 5)
        decide(now+30*60)
        self.assertEqual(info["extension_minutes"], 10)
        self.assertEqual(info["adjusted_minutes"], 40)
        self.assertEqual(info["initial_off_at"], original)
        self.assertAlmostEqual(info["planned_off_at"], original+600, places=5)
        self.assertEqual(len(info["reviews"]), 2)
        self.assertEqual(decide(now+35*60, 23).state, "PREDICTIVE_OFF")
        self.assertEqual(info["review_status"], "stop_now")
        self.runtime._clear_runtime_off_schedule()
        decide(now)
        model.predicted_peak = lambda baseline, minutes, outdoor: 20 + minutes
        decide(now+25*60)
        self.assertEqual(self.runtime.heating_prediction_info["review_status"], "stop_as_planned")

    async def test_confirmed_peak_freezes_contiguous_cooling_and_restores(self):
        """Persist the exact confirmation-time curve, with gaps and malformed data isolated; related: storage.py."""
        import time
        from copy import deepcopy
        from custom_components.adaptive_floor_heating.curve_learning import CurveStandards
        from custom_components.adaptive_floor_heating.curve_memory import Bucket
        from custom_components.adaptive_floor_heating.storage import decode_state
        await self.runtime.async_start()
        wall = time.time()
        model = CurveStandards()
        memory = model.memory["COOLING"]
        memory.current = {0: Bucket(-.1, 0, 8, wall), 1: Bucket(-.2, 0, 8, wall),
                          3: Bucket(-.5, 0, 8, wall)}
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.last_off_prediction = {"off_at": wall-1800, "predictions": {"curve": 23.3},
                                             "confidences": {"curve": None}, "selected_model": "curve"}
        self.assertIsNone(self.runtime.last_cooling_prediction)
        self.runtime._record_peak_comparison(23.4, wall-1800, wall-600, completed_at=wall)
        saved = deepcopy(self.runtime.last_cooling_prediction)
        self.assertEqual(saved["peak_at"], wall-600)
        self.assertEqual(saved["temperature"], 23.4)
        self.assertEqual(saved["status"], "ready")
        self.assertEqual(len(saved["trajectory"]), 3)
        self.assertAlmostEqual(saved["trajectory"][-1][1], -.3)
        memory.current[0].mean = -1
        self.runtime._record_peak_comparison(23.4, wall-1800, wall-600, completed_at=wall+60)
        self.assertEqual(self.runtime.last_cooling_prediction, saved)
        decoded = decode_state(self.runtime.snapshot())
        self.assertEqual(decoded["last_cooling_prediction"], saved)
        self.runtime.curve_store = None
        restarted = self.make_runtime()
        await restarted.async_start()
        self.assertEqual(restarted.last_cooling_prediction, saved)
        await restarted.async_stop()
        bad = self.runtime.snapshot()
        bad["last_cooling_prediction"] = {**saved, "trajectory": [[0, 0], [10, -.1]]}
        self.assertIsNone(decode_state(bad)["last_cooling_prediction"])
        self.assertEqual(decode_state(bad)["target"], self.runtime.controller.target)

    async def test_confirmed_peak_without_cooling_evidence_is_explicit(self):
        import time
        await self.runtime.async_start()
        wall = time.time()
        self.runtime._record_peak_comparison(23, wall-1000, wall-300, completed_at=wall)
        self.assertEqual(self.runtime.last_cooling_prediction["status"], "insufficient_data")
        self.assertEqual(self.runtime.last_cooling_prediction["trajectory"], [[0, 0]])

    async def test_auto_curve_off_uses_long_term_after_non_heating_season(self):
        import time
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveResult, CurveStandards
        from custom_components.adaptive_floor_heating.curve_memory import CURRENT_MAX_AGE
        from dataclasses import replace
        await self.runtime.async_start()
        now, wall = self.hass.loop.time(), time.time()
        old = wall - 4 * CURRENT_MAX_AGE
        model = CurveStandards()
        result = CurveResult(
            "lt-off", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "BALANCED", old-3600, old-1800, old, old,
            {i: 0.1 for i in range(6)}, 1, None,
            off_profile={"duration": 30, "slope": 1, "rise": 1,
                         "points": [[0, 0], [5, 0.1], [30, 1]]},
        )
        for i in range(8):
            self.assertTrue(model.apply(replace(result, cycle_id=f"lt-{i}", ended_at=old+i))[0])
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.controller.target = 23
        self.runtime.controller.temperature = 22.5
        self.runtime.actuator.observed = True
        self.runtime.actuator.changed_at = now - 1800
        self.seed_off_response(now, 22.5)
        self.runtime.curve_tracker.cycle = CurveCycle(
            "active", wall-1800, "THRESHOLD_START", "BALANCED", "WARM_HEATING",
            22.2, 22.5, wall, wall+300, heating_buckets={0: 0.1, 1: 0.1, 2: 0.1},
        )
        decision = self.runtime._apply_curve_prediction(Decision(True, "HEATING"), now)
        self.assertEqual(decision.state, "PREDICTIVE_OFF")
        self.assertEqual(self.runtime.off_prediction["memory_source"], "WARM_HEATING:long_term")
        self.assertEqual(self.runtime.off_prediction["current_weight"], 0)
        self.assertLess(abs(self.runtime.off_prediction["generated_at"] - time.time()), 5)
        self.assertEqual(self.runtime.off_prediction["temperature"], 22.5)
        self.assertEqual(self.runtime.off_prediction["accepted_cycles"], self.runtime.curve_store.model.accepted.get("WARM_HEATING", 0))
        self.assertGreater(len(self.runtime.off_prediction["trajectory"]), 1)
        self.runtime.curve_store = None

    async def test_curve_on_uses_long_term_heating_delay_and_cooling(self):
        import time
        from dataclasses import replace
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveResult, CurveStandards
        from custom_components.adaptive_floor_heating.curve_memory import CURRENT_MAX_AGE
        await self.runtime.async_start()
        now, wall = self.hass.loop.time(), time.time()
        old = wall - 4 * CURRENT_MAX_AGE
        model = CurveStandards()
        heating = CurveResult(
            "lt-on", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "BALANCED", old-1800, old-900, old, old,
            {0: 0, 1: 0.1, 2: 0.1}, 0.5, None,
        )
        cooling = replace(heating, curve_type="COOLING", residual_rise=None,
                          buckets={i: -0.1 for i in range(6)})
        for i in range(8):
            model.apply(replace(heating, cycle_id=f"heat-{i}", ended_at=old+i))
            model.apply(replace(cooling, cycle_id=f"cool-{i}", ended_at=old+i))
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.prediction_mode = "comfort"
        self.runtime.controller.target = 23
        self.runtime.controller.temperature = 22.65
        self.runtime.actuator.changed_at = now - 1800
        self.runtime.curve_tracker.cycle = CurveCycle(
            "active-cooling", wall-2000, "THRESHOLD_START", "BALANCED", "WARM_HEATING",
            22, 23.15, wall, wall+300, off_at=wall-1000, peak_at=wall-300,
        )
        decision = self.runtime._apply_curve_prediction(Decision(False, "IDLE"), now)
        self.assertEqual(decision.state, "PREDICTIVE_ON")
        self.assertEqual(model.memory["COOLING"].last_source, "long_term")
        self.runtime.curve_store = None

    async def test_occupancy_and_peak_comparison_survive_restart(self):
        import time
        from custom_components.adaptive_floor_heating.storage import decode_state
        await self.runtime.async_start()
        self.runtime.set_preset("away")
        self.runtime.set_preset("home")
        self.runtime.last_peak_comparison = {
            "actual_peak": 23.4, "predictions": {"existing": 23.5, "curve": 23.3},
            "errors": {"existing": 0.1, "curve": -0.1},
        }
        self.runtime.last_off_prediction = {
            "off_at": time.time(), "selected_model": "curve",
            "predictions": {"existing": 23.5, "curve": 23.3},
            "confidences": {"existing": 0.75, "curve": 0.5},
            "comparison_counts": {"existing": 5, "curve": 5},
        }
        saved = self.runtime.snapshot()
        await self.runtime.async_stop()
        self.runtime = self.make_runtime()
        await self.runtime.async_start()
        self.assertEqual(self.runtime.last_peak_comparison, saved["last_peak_comparison"])
        self.assertEqual(self.runtime.last_off_prediction, saved["last_off_prediction"])
        self.assertEqual(self.runtime._off_predictions, {})
        self.assertIsNone(self.runtime.observation.off_at)

    async def test_completed_peak_records_both_errors_without_unselected_commands(self):
        import time
        await self.runtime.async_start()
        now = self.hass.loop.time()
        observation = self.runtime.observation
        observation.seed_heater(False)
        observation.observe_heater(True, now - 2700, 22)
        for elapsed, temperature in ((600, 22.1), (1200, 22.2), (1500, 22.3)):
            observation.report_temperature(temperature, now - 2700 + elapsed)
        observation.observe_heater(False, now - 1200, 22.3)
        observation.report_temperature(22.6, now - 900)
        observation.report_temperature(22.3, now - 600)
        self.runtime._off_predictions = {"existing": 22.7, "curve": 22.5}
        self.runtime._off_diagnostic_at = confirmed_off_at = time.time() - 1200
        self.runtime.last_off_prediction = {
            "off_at": confirmed_off_at, "predictions": dict(self.runtime._off_predictions),
            "confidences": {"existing": None, "curve": None},
            "comparison_counts": {"existing": 0, "curve": 0},
        }
        self.write("sensor.room", "22.2")
        comparison = self.runtime.last_peak_comparison
        self.assertEqual(comparison["actual_peak"], 22.6)
        self.assertAlmostEqual(comparison["errors"]["existing"], 0.1)
        self.assertAlmostEqual(comparison["errors"]["curve"], -0.1)
        self.assertEqual(comparison["off_at"], confirmed_off_at)
        self.assertAlmostEqual(comparison["peak_at"] - comparison["off_at"], 300)
        self.assertAlmostEqual(comparison["completed_at"] - comparison["off_at"], 1200, delta=0.01)
        self.assertEqual(comparison["confidences"], {"existing": None, "curve": None})
        self.assertEqual(comparison["comparison_counts"], {"existing": 0, "curve": 0})
        self.assertEqual(len(self.runtime.prediction_errors["curve"]), 1)
        self.assertEqual(self.calls, [])

    async def test_state_sensors_only_expose_phases_and_never_run_predictors_or_commands(self):
        from custom_components.adaptive_floor_heating.diagnostics import DIAGNOSTIC_KEYS
        await self.runtime.async_start()
        entities = {
            d.key: self.sensor.LearningStateSensor(self.entry, self.runtime, d)
            for d in self.sensor.STATE_SENSORS
        }
        snapshot_before, calls_before = self.runtime.snapshot(), list(self.calls)
        with patch.object(self.runtime.thermal_model, "predict_off_response", side_effect=AssertionError("prediction called")):
            readings = {key: entities[key].native_value for key in DIAGNOSTIC_KEYS}
            attrs = {key: entities[key].extra_state_attributes for key in DIAGNOSTIC_KEYS}
        self.assertNotIn("initial_learning_status", readings)
        self.assertEqual(readings["learning_status"], "idle")
        self.assertIn(readings["observation_status"], entities["observation_status"].entity_description.options)
        self.assertIn(readings["prediction_status"], entities["prediction_status"].entity_description.options)
        self.assertEqual(attrs["prediction_status"]["control_state"], self.runtime.decision.state)
        self.assertEqual(self.runtime.snapshot(), snapshot_before)
        self.assertEqual(self.calls, calls_before)
        self.assertEqual(len(entities), 3)
        self.assertEqual(entities["prediction_status"]._attr_unique_id, "one_prediction_status")
        for key in DIAGNOSTIC_KEYS:
            self.assertIn(readings[key], entities[key].entity_description.options)
            self.assertIsNone(entities[key].entity_description.state_class)
            self.assertIsNone(entities[key].entity_description.native_unit_of_measurement)

    async def test_actual_peak_without_forecast_does_not_reuse_old_predictions(self):
        await self.runtime.async_start()
        self.runtime.last_off_prediction = {
            "off_at": 1790982000, "selected_model": "existing",
            "predictions": {"existing": 24.7}, "confidences": {"existing": 0.8},
        }
        now = self.hass.loop.time()
        observation = self.runtime.observation
        observation.seed_heater(False)
        observation.observe_heater(True, now - 2700, 22)
        for elapsed, temperature in ((600, 22.1), (1200, 22.2), (1500, 22.3)):
            observation.report_temperature(temperature, now - 2700 + elapsed)
        observation.observe_heater(False, now - 1200, 22.3)
        observation.report_temperature(22.6, now - 900)
        observation.report_temperature(22.3, now - 600)
        self.write("sensor.room", "22.2")
        self.assertEqual(self.runtime.last_peak_comparison["actual_peak"], 22.6)
        self.assertEqual(self.runtime.last_peak_comparison["errors"], {})
        self.assertEqual(self.runtime.last_peak_comparison["predictions"], {})
        self.assertEqual(self.calls, [])

    def test_entity_ids_gain_room_numbers_when_second_entry_is_added(self):
        from custom_components.adaptive_floor_heating.entity_naming import entity_id, update_registered_ids

        second = self.new_entry("two")
        self.hass.config_entries.async_entries = lambda domain: [self.entry, second]
        self.assertEqual(
            entity_id(self.hass, self.entry, "sensor", "learning_status"),
            "sensor.adaptive_heating_climate_room_1_learning_status",
        )
        self.assertEqual(
            entity_id(self.hass, second, "select", "prediction_mode"),
            "select.adaptive_heating_climate_room_2_prediction_mode",
        )
        first_sensor = SimpleNamespace(
            unique_id="one_learning_status", platform="adaptive_floor_heating",
            domain="sensor", entity_id="sensor.adaptive_heating_climate_learning_status",
        )
        updates = []
        self.hass.entity_registry = SimpleNamespace(
            entries={"one": [first_sensor], "two": []},
            async_update_entity=lambda old, *, new_entity_id: updates.append((old, new_entity_id)),
        )
        from homeassistant.helpers import entity_registry as er
        with patch.object(er, "async_entries_for_config_entry", side_effect=lambda reg, ident: reg.entries[ident]):
            update_registered_ids(self.hass)
        self.assertEqual(updates, [(
            "sensor.adaptive_heating_climate_learning_status",
            "sensor.adaptive_heating_climate_room_1_learning_status",
        )])

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
        self.seed_off_response(now, 20.0, rise=0.5)
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
                    if on:
                        self.seed_off_response(now, temperature)
                    else:
                        # Reuse the condition-matched forecast captured at OFF, not an unrelated global mean.
                        self.runtime._off_predictions = {"existing": temperature + 1}
                        self.runtime._off_peak_deadlines = {"existing": now + 600}
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
        self.runtime._minimum_off_until = self.hass.loop.time()
        self.runtime.evaluate()
        await self.settle()
        self.assertEqual(self.runtime.controller.mode, "auto")
        self.assertTrue(self.runtime.actuator.observed)
