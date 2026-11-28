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
from unittest.mock import AsyncMock, patch

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
    def __init__(self, hass, version, key, **kwargs):
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
        "homeassistant.components.button",
        "homeassistant.components.switch",
        "homeassistant.components.time",
        "homeassistant.components.select",
        "homeassistant.components.sensor",
        "homeassistant.components.binary_sensor",
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
    const.Platform = SimpleNamespace(CLIMATE="climate", SENSOR="sensor", SELECT="select", NUMBER="number", SWITCH="switch", BUTTON="button", TIME="time")
    const.Platform.BINARY_SENSOR = "binary_sensor"
    const.ATTR_TEMPERATURE = "temperature"
    const.UnitOfTemperature = SimpleNamespace(CELSIUS="°C", FAHRENHEIT="°F")
    const.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic", CONFIG="config")
    exceptions = modules["homeassistant.exceptions"]
    exceptions.ConfigEntryError = type("ConfigEntryError", (Exception,), {})
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    exceptions.ServiceValidationError = type("ServiceValidationError", (Exception,), {})
    climate = modules["homeassistant.components.climate"]
    climate.ClimateEntity = FakeClimateEntity
    climate.ClimateEntityFeature = IntFlag("ClimateEntityFeature", {"TARGET_TEMPERATURE": 1, "TURN_ON": 2, "TURN_OFF": 4, "PRESET_MODE": 8})
    climate.HVACMode = StrEnum("HVACMode", {"OFF": "off", "HEAT": "heat", "AUTO": "auto"})
    climate.HVACAction = StrEnum("HVACAction", {"OFF": "off", "HEATING": "heating", "IDLE": "idle"})
    modules["homeassistant.components.switch"].SwitchEntity = FakeClimateEntity
    modules["homeassistant.components.time"].TimeEntity = FakeClimateEntity
    modules["homeassistant.components.button"].ButtonEntity = FakeClimateEntity
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
    binary_sensor = modules["homeassistant.components.binary_sensor"]
    binary_sensor.BinarySensorEntityDescription = FakeSensorEntityDescription
    binary_sensor.BinarySensorEntity = FakeClimateEntity
    sensor.SensorEntity = FakeClimateEntity
    sensor.SensorStateClass = SimpleNamespace(MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing")
    sensor.SensorDeviceClass = SimpleNamespace(
        DURATION="duration", TEMPERATURE_DELTA="temperature_delta", TIMESTAMP="timestamp",
        TEMPERATURE="temperature", ENUM="enum",
    )
    return modules


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    # Freeze the same recent MAE displayed in diagnostics; related: curve_storage.py.
    async def test_sunrise_observation_policy_mae_boundary_and_missing_evidence(self):
        from custom_components.adaptive_floor_heating.curve_storage import CurveStore
        store = object.__new__(CurveStore)
        self.runtime.curve_store = store
        self.runtime._off_predictions = {"curve": 23}
        self.states["sun.sun"] = SimpleNamespace(state="below_horizon", attributes={
            "next_rising": datetime.fromtimestamp(2000, UTC).isoformat(),
        })
        for mae, unlimited in ((None, False), (.50001, False), (.5, True), (.49, True), (0, True)):
            with self.subTest(mae=mae):
                store.heating_metrics = {"mean_absolute_peak_error": mae}
                baseline = self.runtime._sunrise_peak_baseline(1000)
                self.assertEqual(baseline is not None, unlimited)
                if unlimited:
                    self.assertTrue(baseline["ends_observation"])
                    self.assertEqual(baseline["mean_absolute_error"], mae)
        self.runtime._off_predictions = {}
        self.assertIsNone(self.runtime._sunrise_peak_baseline(1000))
        self.runtime.curve_store = None

    # Cover delayed solar sampling and a later peak after an initial decline; related: runtime.py.
    async def test_solar_sampling_waits_for_later_peak_and_stops_on_heater_activity(self):
        runtime = self.runtime
        runtime.curve_store = object()
        runtime.actuator.observed = False
        runtime._aggregate_heater_state = lambda: False
        runtime.sunrise_weather_entity = "weather.home"
        sun = self.states["sun.sun"] = SimpleNamespace(state="below_horizon", attributes={
            "next_rising": datetime.fromtimestamp(1000, UTC).isoformat(), "elevation": 0,
        })
        runtime._sensor_samples["weather.home"] = SimpleNamespace(state="sunny")
        save = AsyncMock()
        runtime._save_curve_result = save
        self.assertEqual(runtime._observe_solar_gain(0, 900), 3700)
        sun.state = "above_horizon"
        sun.attributes["next_rising"] = datetime.fromtimestamp(87400, UTC).isoformat()
        self.assertEqual(runtime._observe_solar_gain(100, 1000), 3700)
        self.assertIsNone(runtime._solar_observation)
        temperatures = (22, 22.15, 22.4, 22.25, 22.1, 22.55, 22.4, 22.25) + (22.25,) * 10
        for index, temperature in enumerate(temperatures):
            runtime.controller.temperature = temperature
            if index == 1:
                runtime._sensor_samples["weather.home"] = None
            elif index == 2:
                runtime._sensor_samples["weather.home"] = SimpleNamespace(state="cloudy")
            deadline = runtime._observe_solar_gain(3700 + index * 300, 4600 + index * 300)
            if index < len(temperatures) - 1:
                self.assertEqual(deadline, 3700 + (index + 1) * 300)
                self.assertFalse(runtime._curve_tasks)
        await asyncio.gather(*runtime._curve_tasks)
        solar = save.await_args.args[0]
        self.assertEqual(solar.started_at, 4600)
        self.assertEqual(solar.peak_at, 6100)
        self.assertEqual(solar.ended_at, 9700)
        self.assertEqual(set(solar.buckets), set(range(5)))
        self.assertEqual([x["sunny"] for x in solar.solar_samples[:3]], [1, None, 0])
        self.assertEqual(runtime._observe_solar_gain(8810, 9710), 90100)
        runtime._observe_solar_gain(90100, 91000)
        self.assertIsNotNone(runtime._solar_observation)
        runtime.actuator.observed = True
        runtime._observe_solar_gain(90101, 91001)
        self.assertIsNone(runtime._solar_observation)
        self.assertEqual(save.await_count, 1)
        runtime.curve_store = None

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
    async def test_delayed_short_pulse_keeps_runtime_stop_and_observation_without_residual_support(self):
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
                for elapsed in (600, 1200, 1799):
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
                    start_temperature=24, off_temperature=24, peak_temperature=24.7,
                    off_profile={"duration": 30, "slope": 0, "rise": 0.7,
                                 "points": [[0, 0], [10, -0.1], [30, 0], [100, 0.7]], "context": context},
                )
                for i in range(8):
                    await store.save(replace(result, cycle_id=f"pulse-{i}", started_at=result.started_at+(i-4)*90))
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
                self.assertEqual(self.runtime.decision.state, "IDLE")
                self.assertAlmostEqual(self.runtime._off_predictions[selected], 24.7)
                self.assertNotIn("curve", self.runtime._off_peak_deadlines)
                from custom_components.adaptive_floor_heating.diagnostics import diagnostic_snapshot
                from custom_components.adaptive_floor_heating.storage import decode_state
                recorded = self.runtime.last_off_prediction
                self.assertEqual(recorded["selected_model"], selected)
                self.assertEqual(recorded["predictions"], self.runtime._off_predictions)
                self.assertEqual(recorded["off_model"], "runtime")
                self.assertEqual(recorded["off_control"]["action"], "plan_off")
                self.assertFalse(recorded["off_control"]["forecast_available"])
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
                self.assertTrue(self.runtime.decision.state in ("IDLE", "WAIT_MIN_OFF"))
                self.assertEqual(self.calls, calls_before)
                await asyncio.gather(*tuple(self.runtime._curve_tasks))
                self.runtime.curve_store = None
                self.runtime.controller.mode = "off"

    @classmethod
    def setUpClass(cls):
        cls.module_patch = patch.dict(sys.modules, boundary_modules())
        cls.module_patch.start()
        cls.integration = importlib.import_module("custom_components.adaptive_floor_heating")
        cls.climate = importlib.import_module("custom_components.adaptive_floor_heating.climate")
        cls.sensor = importlib.import_module("custom_components.adaptive_floor_heating.sensor")
        cls.binary_sensor = importlib.import_module("custom_components.adaptive_floor_heating.binary_sensor")
        cls.select = importlib.import_module("custom_components.adaptive_floor_heating.select")
        cls.button = importlib.import_module("custom_components.adaptive_floor_heating.button")

    @classmethod
    def tearDownClass(cls):
        cls.module_patch.stop()

    async def asyncSetUp(self):
        self.states, self.calls, self.entities, self.sensor_entities, self.buttons = {}, [], [], [], []
        self.binary_sensor_entities = []
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

    # Verify manual dispatch and per-entry exclusion; related: runtime.py, peak_migration.py.
    async def test_learning_migration_runs_only_on_request(self):
        from custom_components.adaptive_floor_heating.curve_storage import CurveStore

        store = object.__new__(CurveStore)
        store.open = AsyncMock()
        store.cleanup = AsyncMock()
        store.control_feedback = []
        store.migrate_sunrise_saving = AsyncMock(side_effect=[2, 1])
        self.runtime.curve_store = store
        with patch("custom_components.adaptive_floor_heating.peak_migration.migrate_timeouts",
                   new_callable=AsyncMock, return_value=3) as peak, patch(
                       "custom_components.adaptive_floor_heating.peak_migration.migrate_heater_outages",
                       new_callable=AsyncMock, side_effect=[4, OSError("Recorder unavailable")],
                   ) as outage:
            await self.runtime.async_start()
            peak.assert_not_awaited()
            outage.assert_not_awaited()
            store.migrate_sunrise_saving.assert_not_awaited()
            async with self.runtime._learning_reset_lock:
                self.assertEqual(await self.runtime.async_migrate_learning(), {"status": "busy"})
            result = await self.runtime.async_migrate_learning()
            self.assertEqual(result["heater_outage"]["recovered"], 4)
            self.assertEqual(result["sunrise_heating"]["status"], "sunrise_disabled")
            self.assertEqual(result["sunrise_cooling"]["status"], "sunrise_disabled")
            store.migrate_sunrise_saving.assert_not_awaited()
            self.runtime.sunrise_enabled = True
            self.runtime.sunrise_end = self.runtime.sunrise_start.replace(hour=7)
            result = await self.runtime.async_migrate_learning()
            self.assertEqual(result["peak"]["recovered"], 3)
            self.assertEqual(result["heater_outage"]["status"], "failed")
            self.assertEqual(result["sunrise_heating"]["recovered"], 2)
            self.assertEqual(result["sunrise_cooling"]["recovered"], 1)
            self.assertEqual([call.kwargs["cooling"] for call in store.migrate_sunrise_saving.await_args_list],
                             [False, True])

    async def test_prediction_confidence_uses_confirmed_peak_errors_not_runtime_sample_count(self):
        confidence, count = self.runtime._prediction_confidence("curve")
        self.assertIsNone(confidence)
        self.assertEqual(count, 0)
        self.runtime.prediction_errors["curve"] = [-0.48]
        confidence, count = self.runtime._prediction_confidence("curve")
        self.assertAlmostEqual(confidence, 0.2 * __import__("math").exp(-0.48 / 0.5))
        self.assertEqual(count, 1)

    async def test_prediction_ready_sensors_follow_learning_and_reset(self):
        from custom_components.adaptive_floor_heating.curve_learning import CurveStandards

        model = CurveStandards()
        self.entry.runtime_data = self.runtime
        entities = []
        await self.binary_sensor.async_setup_entry(self.hass, self.entry, entities.extend)
        off, on, sunrise = entities
        for entity in entities:
            await entity.async_added_to_hass()
            self.assertEqual(entity.entity_description.entity_category, "diagnostic")
        with patch.object(self.runtime, "curve_store", SimpleNamespace(model=model)):
            self.assertFalse(off.is_on)
            self.assertFalse(on.is_on)
            self.assertFalse(sunrise.is_on)
            model.load_runtime_samples([(20, .4, 10), (30, .8, 10), (40, 1.2, 10)])
            model.buckets["COOLING"] = {0: (-.1, 3)}
            self.assertFalse(on.is_on)
            self.assertEqual(on.extra_state_attributes["reason"], "cooling_coverage_missing")
            model.buckets["COOLING"][1] = (-.1, 3)
            self.runtime.prediction_mode = "eco"
            self.runtime.sunrise_enabled = False
            self.runtime.controller.temperature = None
            self.runtime._notify()
            self.assertTrue(all(entity.is_on and entity.writes == 1 for entity in (off, on)))
            self.assertEqual(off.extra_state_attributes["sample_count"], 3)
            self.assertEqual(on.extra_state_attributes["available_models"], ["global_cooling"])
            model.load_runtime_samples([(20, .4, 10), (30, .8, 10), (40, 1.2, 10)] * 3)
            self.assertFalse(sunrise.is_on)
            self.assertEqual(sunrise.extra_state_attributes["required_runtime_samples"], 10)
            model.load_runtime_samples([(20, .4, 10), (30, .8, 10), (40, 1.2, 10)] * 3
                                       + [(40, 1.2, 10)])
            self.assertTrue(sunrise.is_on)
            for index, delay in enumerate((40, 55, 65, 55, 55, 60, 90, 55, 110, 85)):
                minutes, rise, _, completed = model.runtime_samples[index]
                model.runtime_samples[index] = (minutes, rise, delay, completed)
            self.assertEqual(self.runtime._sunrise_response_minutes(), 85)
            with patch.object(self.runtime, "_learning_reset_pending", True):
                self.assertFalse(any(entity.is_on for entity in entities))
        for entity in entities:
            await entity.async_will_remove_from_hass()

    # Confirm both Climate commands after their echoes; related: runtime.py, actuator.py.
    async def test_climate_echoes_do_not_confirm_commands_or_start_learning_early(self):
        self.hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°C"))
        self.entry.data["heater"] = "climate.boiler"
        self.entry.options.update(climate_low=18, climate_high=30)
        self.write("climate.boiler", "heat", {"temperature": 18, "hvac_action": "idle"})
        commands = []

        async def set_temperature(domain, action, data, blocking):
            commands.append((domain, action, data))

        self.hass.services.async_call = set_temperature
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.runtime = self.entry.runtime_data
        self.assertEqual(self.runtime.actuator._confirmation_delay, 1)
        self.runtime.actuator._confirmation_delay = 0.05
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertEqual(commands[-1], ("climate", "set_temperature", {
            "entity_id": "climate.boiler", "temperature": 30, "hvac_mode": "heat",
        }))
        self.assertTrue(self.runtime.actuator.busy)
        self.assertFalse(self.runtime.actuator.observed)
        self.assertIsNone(self.runtime._confirmed_on_since)
        self.write("climate.boiler", "heat", {"temperature": 30, "hvac_action": "idle"})
        await self.settle()
        self.assertTrue(self.runtime.actuator.busy)
        self.assertIsNone(self.runtime._confirmed_on_since)
        self.write("climate.boiler", "heat", {"temperature": 18, "hvac_action": "idle"})
        self.write("climate.boiler", "heat", {"temperature": 30, "hvac_action": "heating"})
        self.assertTrue(self.runtime.actuator.observed)
        self.assertFalse(self.runtime.observation.heater_state)
        self.assertEqual(len(commands), 1)
        await self.runtime.actuator._task
        self.assertTrue(self.runtime.observation.heater_state)
        self.assertEqual(self.runtime._confirmed_on_since, self.runtime.actuator.changed_at)
        self.assertEqual(self.runtime.controller.mode, "auto")
        self.assertNotIn("external_override", self.runtime.controller.faults)
        self.write("sensor.room", "24")
        await self.settle()
        self.assertEqual(commands[-1], ("climate", "set_temperature", {
            "entity_id": "climate.boiler", "temperature": 18,
        }))
        self.assertTrue(self.runtime.actuator.busy)
        self.assertTrue(self.runtime.observation.heater_state)
        self.assertFalse(self.runtime.can_reset_learning)
        self.write("climate.boiler", "heat", {"temperature": 18, "hvac_action": "heating"})
        self.write("climate.boiler", "heat", {"temperature": 30, "hvac_action": "heating"})
        self.write("climate.boiler", "heat", {"temperature": 18, "hvac_action": "idle"})
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        self.assertTrue(self.runtime.observation.heater_state)
        self.assertEqual(len(commands), 2)
        await self.runtime.actuator._task
        self.assertFalse(self.runtime.observation.heater_state)
        self.assertIsNone(self.runtime._confirmed_on_since)
        self.assertTrue(self.runtime.can_reset_learning)
        self.assertEqual(self.runtime.controller.mode, "auto")
        reload = AsyncMock()
        self.hass.config_entries.async_reload = reload
        self.entry.options["climate_high"] = 29
        await self.integration._async_update_options(self.hass, self.entry)
        reload.assert_awaited_once_with(self.entry.entry_id)
        self.write("climate.boiler", "off", {"temperature": 18})
        await self.settle()
        self.assertEqual(self.runtime.controller.mode, "off")
        self.assertIn("external_override", self.runtime.controller.faults)

    async def test_new_idle_climate_can_heat_without_skipping_later_cooldown(self):
        self.hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°C"))
        self.entry.data["heater"] = "climate.boiler"
        self.entry.options.update(climate_low=18, climate_high=30, minimum_off_time=1800)
        self.hass.saved.pop("adaptive_floor_heating.one.runtime")
        self.write("climate.boiler", "heat", {"temperature": 18, "hvac_action": "idle"})
        targets = []

        async def set_temperature(domain, action, data, blocking):
            target = data["temperature"]
            targets.append(target)
            self.write("climate.boiler", "heat", {
                "temperature": target, "hvac_action": "heating" if target == 30 else "idle",
            })

        self.hass.services.async_call = set_temperature
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.runtime = self.entry.runtime_data
        self.runtime.actuator._confirmation_delay = 0
        self.runtime.set_mode("heat")
        await self.settle()
        self.assertEqual(targets, [30])
        self.write("sensor.room", "25")
        await self.settle()
        self.assertEqual(targets, [30, 18])
        self.write("sensor.room", "17")
        await self.settle()
        self.assertEqual(self.runtime.decision.state, "WAIT_MIN_OFF")
        self.assertEqual(targets, [30, 18])

        self.assertTrue(await self.runtime.async_stop())
        await self.runtime.async_start()
        await self.settle()
        self.assertEqual(self.runtime.decision.state, "WAIT_MIN_OFF")
        self.assertEqual(targets, [30, 18])
        self.runtime.prediction_errors["curve"] = [0.0] * 5
        self.assertEqual(self.runtime._prediction_confidence("curve"), (1.0, 5))

    # Exercise an already OFF Climate through entry reload; related: __init__.py, runtime.py.
    async def test_reload_already_off_climate_resets_minimum_off_wait(self):
        self.hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°C"))
        self.entry.data["heater"] = "climate.boiler"
        self.entry.options.update(climate_low=18, climate_high=30, minimum_off_time=1800)
        self.hass.saved.pop("adaptive_floor_heating.one.runtime")
        self.write("climate.boiler", "heat", {"temperature": 18})
        targets = []

        async def set_temperature(domain, action, data, blocking):
            target = data["temperature"]
            targets.append(target)
            self.write("climate.boiler", "heat", {"temperature": target})

        self.hass.services.async_call = set_temperature
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.runtime = self.entry.runtime_data
        self.runtime.actuator._confirmation_delay = 0
        self.runtime.set_mode("auto")
        await self.settle()
        self.write("sensor.room", "25")
        await self.settle()
        self.write("sensor.room", "17")
        self.assertEqual(targets, [30, 18])
        self.assertEqual(self.runtime.decision.state, "WAIT_MIN_OFF")

        self.assertTrue(await self.integration.async_unload_entry(self.hass, self.entry))
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.runtime = self.entry.runtime_data
        self.runtime.actuator._confirmation_delay = 0
        await self.settle()
        self.assertEqual(self.runtime.controller.mode, "auto")
        self.assertEqual(targets, [30, 18, 30])

        self.write("sensor.room", "25")
        await self.settle()
        self.assertEqual(targets, [30, 18, 30, 18])
        self.assertTrue(await self.integration.async_unload_entry(self.hass, self.entry))
        await self.integration.async_remove_entry(self.hass, self.entry)
        self.entry = self.new_entry("replacement")
        self.entry.data["heater"] = "climate.boiler"
        self.entry.options.update(climate_low=18, climate_high=30, minimum_off_time=1800)
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.runtime = self.entry.runtime_data
        self.runtime.actuator._confirmation_delay = 0
        self.write("sensor.room", "17")
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertEqual(targets, [30, 18, 30, 18, 30])

    # Prevent a reload-triggered stop from losing its protection; related: __init__.py, runtime.py.
    async def test_reload_and_reinstall_keep_wait_if_unload_stopped_active_heater(self):
        self.entry.options["minimum_off_time"] = 1800
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.runtime = self.entry.runtime_data
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertIs(self.runtime.actuator.observed, True)
        self.runtime._confirmed_on_since = self.hass.loop.time() - 1200

        self.assertTrue(await self.integration.async_unload_entry(self.hass, self.entry))
        deadline = self.runtime._minimum_off_until
        self.assertGreater(deadline - self.hass.loop.time(), 2300)
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.runtime = self.entry.runtime_data
        await self.settle()
        self.assertEqual(self.runtime.decision.state, "WAIT_MIN_OFF")
        self.assertEqual(self.runtime.decision.deadline, deadline)
        self.assertEqual([call[1] for call in self.calls], ["turn_on", "turn_off"])

        self.assertTrue(await self.integration.async_unload_entry(self.hass, self.entry))
        await self.integration.async_remove_entry(self.hass, self.entry)
        self.entry = self.new_entry("replacement")
        self.entry.options["minimum_off_time"] = 1800
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.runtime = self.entry.runtime_data
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertEqual(self.runtime.decision.state, "WAIT_MIN_OFF")
        self.assertEqual(self.runtime.decision.deadline, deadline)
        self.assertEqual([call[1] for call in self.calls], ["turn_on", "turn_off"])

    async def test_prediction_validation_errors_persist_and_restore(self):
        from custom_components.adaptive_floor_heating.storage import decode_state
        self.runtime.prediction_errors["curve"] = [-0.48, 0.2]
        restored = decode_state(self.runtime.snapshot())
        self.assertEqual(restored["prediction_errors"]["curve"], [-0.48, 0.2])

    # Verify native controls and a local window across midnight; related: time.py, storage.py.
    async def test_sunrise_schedule_entities_restore_and_handle_midnight(self):
        from datetime import time, timezone

        await self.runtime.async_start()
        self.entry.runtime_data = self.runtime
        platform = importlib.import_module("custom_components.adaptive_floor_heating.time")
        switch_platform = importlib.import_module("custom_components.adaptive_floor_heating.switch")
        times, switches = [], []
        await platform.async_setup_entry(self.hass, self.entry, times.extend)
        await switch_platform.async_setup_entry(self.hass, self.entry, switches.extend)
        start, end = times
        saving = next(entity for entity in switches if isinstance(entity, switch_platform.SunriseSavingSwitch))
        self.assertFalse(saving.is_on)
        await start.async_set_value(time(23))
        await end.async_set_value(time(1))
        await saving.async_turn_on()
        self.assertTrue(await self.runtime.async_stop())
        await self.runtime.async_start()
        self.assertTrue(saving.is_on)
        self.assertEqual((start.native_value, end.native_value), (time(23), time(1)))
        self.hass.config = SimpleNamespace(time_zone="Asia/Seoul")
        seoul = timezone(timedelta(hours=9))
        with patch("custom_components.adaptive_floor_heating.runtime.ZoneInfo", return_value=seoul) as zone:
            for day, hour, active, seconds in ((1, 22, False, 3600), (1, 23, True, 7200),
                                                (2, 0, True, 3600), (2, 1, False, 79200)):
                at = datetime(2026, 1, day, hour, tzinfo=seoul).timestamp()
                self.assertEqual(self.runtime._sunrise_window(at), (active, seconds))
            zone.assert_called_with("Asia/Seoul")
        self.hass.config.time_zone = "UTC"
        await end.async_set_value(time(23))
        self.assertEqual(self.runtime._sunrise_window(at), (False, None))

    # Exercise real Sun state events outside the scheduled window; related: runtime.py, controller.py.
    async def test_sunrise_saving_applies_daylight_stop_targets(self):
        from custom_components.adaptive_floor_heating.curve_learning import CurveStandards

        await self.runtime.async_start()
        model = CurveStandards()
        model.load_runtime_samples([(20, .5), (40, 1.0)] * 5)
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.controller.mode = "auto"
        self.runtime.set_sunrise_schedule(enabled=True)
        self.assertEqual(self.runtime.sunrise_start, self.runtime.sunrise_end)
        controller = self.runtime.controller
        with patch.object(controller, "decide", wraps=controller.decide) as decide:
            self.write("sun.sun", "above_horizon")
            self.assertTrue(self.runtime.sunrise_saving_active)
            self.assertEqual(self.runtime._predictive_off_target(), controller.target)
            self.assertEqual(decide.call_args.kwargs["heating_stop_target"], controller.target)
            self.write("sun.sun", "below_horizon")
            self.assertFalse(self.runtime.sunrise_saving_active)
            self.assertEqual(self.runtime._predictive_off_target(), controller.target + controller.settings.hot_tolerance)
            self.assertIsNone(decide.call_args.kwargs["heating_stop_target"])
            self.write("sun.sun", "above_horizon")
            self.runtime.set_sunrise_schedule(enabled=False)
            self.assertFalse(self.runtime.sunrise_saving_active)
            self.assertIsNone(decide.call_args.kwargs["heating_stop_target"])

    # Drive confirmed OFF through the real actuator callback after the window closes; related: runtime.py.
    async def test_sunrise_off_confirmation_preserves_goal_and_excludes_feedback(self):
        await self._check_sunrise_off_confirmation(0, excluded=True)

    async def test_sunrise_off_keeps_feedback_for_cycle_started_before_window(self):
        await self._check_sunrise_off_confirmation(30, excluded=False)

    # Exercise both ON times with the same saving OFF decision; related: runtime.py, curve_learning.py.
    async def _check_sunrise_off_confirmation(self, start_minute, *, excluded):
        from datetime import time
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards
        from custom_components.adaptive_floor_heating.peak_prediction import sample

        await self.start_heating()
        now = self.hass.loop.time()
        wall = datetime(2026, 1, 1, 5, 59, 59, tzinfo=UTC).timestamp()
        model = CurveStandards()
        self.runtime.outdoor_sensor = "sensor.outdoor"
        self.write("sensor.outdoor", "5", {"unit_of_measurement": "°C", "device_class": "temperature"})
        self.runtime._sensor_samples["sensor.outdoor"] = self.states["sensor.outdoor"]
        model.load_runtime_samples([(20, .5), (25, .7), (45, 1.3)] * 4)
        model.peak_samples = [sample(19, 19.8, 20.1, minutes, 5, wall-10000)
            for minutes in (25, 26, 34, 35)]
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.controller.mode = "auto"
        self.runtime.actuator.changed_at = now - 1800
        tracker = self.runtime.curve_tracker
        tracker.cycle = CurveCycle("sunrise", wall-1800, "THRESHOLD_START", "BALANCED",
                                   "WARM_HEATING", 19, 19.8, wall, wall+300)
        self.confirm_commands = False
        self.write("sensor.room", "19.8")
        with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall) as clock:
            self.runtime.set_sunrise_schedule(enabled=True, start=time(5, start_minute), end=time(6))
            await self.settle()
            self.assertEqual(self.runtime.decision.state, "PREDICTIVE_OFF")
            entity = self.climate.AdaptiveFloorHeatingClimate(self.entry, self.runtime)
            self.assertTrue(entity.extra_state_attributes["sunrise_saving_enabled"])
            self.assertTrue(entity.extra_state_attributes["sunrise_saving_active"])
            self.assertIs(self.runtime.actuator.pending, False)
            clock.return_value = wall + 2
            self.runtime.evaluate()
            self.assertFalse(entity.extra_state_attributes["sunrise_saving_active"])
            self.write("switch.heater", "off")
            await self.settle()
            self.assertEqual(tracker.cycle.sunrise_saving, excluded)
            self.assertEqual(tracker.cycle.target_peak_temperature, 20)
            self.assertEqual(tracker.cycle.off_setpoint, 20)
            self.assertEqual(tracker.cycle.off_stop_tolerance, self.runtime.controller.settings.hot_tolerance)
            if excluded:
                self.assertEqual(tracker.cycle.sunrise_prediction["off_model"], "hybrid")
                self.assertAlmostEqual(tracker.cycle.sunrise_prediction["predicted_peak"], 20.1)
            else:
                self.assertIsNone(tracker.cycle.sunrise_prediction)
            self.assertEqual(self.runtime.last_off_prediction["off_model"], "hybrid")
            self.assertEqual(self.runtime.last_off_prediction["decision_target"], 20)
            self.assertAlmostEqual(self.runtime.last_off_prediction["predictions"]["curve"], 20.1)
            off_at = tracker.cycle.off_at
            before = self.runtime.snapshot()
            self.runtime._record_peak_comparison(20.2, off_at, off_at+600)
            after = self.runtime.snapshot()
            self.assertEqual(after["last_peak_comparison"].get("sunrise_saving", False), excluded)
            for key in ("prediction_bias", "prediction_errors", "hybrid_feedback"):
                self.assertEqual(after[key], before[key])
        self.runtime.curve_store = None

    # Recalculate a runtime reservation when the local window changes; related: runtime.py.
    async def test_sunrise_runtime_fallback_replans_against_lower_target(self):
        from datetime import time
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards

        await self.start_heating()
        self.runtime.controller.mode = "auto"
        now = self.hass.loop.time()
        wall = datetime(2026, 1, 1, 4, 59, 59, tzinfo=UTC).timestamp()
        model = CurveStandards()
        model.load_runtime_samples([(15, .3), (20, .5), (40, 1.5), (60, 2.0)] * 3)
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.actuator.changed_at = now - 1200
        self.runtime.curve_tracker.cycle = CurveCycle(
            "runtime-sunrise", wall-1200, "THRESHOLD_START", "BALANCED",
            "WARM_HEATING", 19, 19.2, wall, wall+300,
        )
        with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall) as clock:
            self.write("sensor.room", "19.2")
            self.runtime.set_sunrise_schedule(enabled=True, start=time(5), end=time(6))
            initial = self.runtime._runtime_off_planned_at
            self.assertEqual(self.runtime.active_off_model, "runtime")
            self.assertIsNotNone(initial)
            clock.return_value = wall + 2
            self.runtime.evaluate()
            self.assertEqual(self.runtime._predictive_off_target(), 20)
            self.assertEqual(self.runtime.active_off_model, "runtime")
            self.assertLess(self.runtime._runtime_off_planned_at, initial)
        self.runtime.curve_store = None

    # Verify legacy weather-free rows still reserve a cutoff without old error bias.
    async def test_runtime_uses_uncorrected_global_curve_for_legacy_samples(self):
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards

        await self.start_heating()
        now, wall = self.hass.loop.time(), datetime.now(UTC).timestamp()
        model = CurveStandards()
        model.load_runtime_samples([(15, .3), (20, .5), (40, 1.5), (60, 2)])
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.controller.temperature = 19.2
        self.runtime.actuator.changed_at = now - 1200
        cycle = CurveCycle("global", wall-1200, "THRESHOLD_START", "BALANCED",
                           "WARM_HEATING", 19, 19.2, wall, wall+300, outdoor_temperature=12,
                           heating_outdoor_reports=[(wall-1200, 12, wall+300)])
        self.runtime.curve_tracker.cycle = cycle
        decision = Decision(True, "HEATING")
        try:
            self.runtime.prediction_bias["curve"] = -.25
            self.runtime._apply_runtime_off(decision, now, False, 20.5, 19.2, cycle, model)
            self.assertFalse(self.runtime.off_prediction["outdoor_conditioned"])
            self.assertAlmostEqual(self.runtime.runtime_off_plan["planned_minutes"], 40)
            self.runtime.last_off_prediction = {"off_at": wall, "predictions": {"curve": 20.5}}
            self.runtime._record_peak_comparison(20.5, wall, None)
            self.assertEqual(self.runtime.prediction_bias["curve"], -.25)
        finally:
            self.runtime.curve_store = None

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

    def write(self, entity_id, value, attributes=None, *, sample=True):
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
        # Other runtime tests inject sensor readings at a check boundary; see runtime.py.
        if sample:
            entry = getattr(self, "entry", None)
            for runtime in {getattr(self, "runtime", None), getattr(entry, "runtime_data", None)} - {None}:
                if runtime.started and entity_id == runtime.sensor:
                    runtime._next_sensor_check = self.hass.loop.time()
                    runtime.evaluate()

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
            if platform == "binary_sensor":
                entities = []
                await self.binary_sensor.async_setup_entry(self.hass, entry, entities.extend)
                self.binary_sensor_entities.extend(entities)
                for entity in entities:
                    entity.hass = self.hass
                    await entity.async_added_to_hass()
            if platform == "button":
                buttons = []
                await self.button.async_setup_entry(self.hass, entry, buttons.extend)
                self.buttons.extend(buttons)
                for entity in buttons:
                    entity.hass = self.hass
                    await entity.async_added_to_hass()

    async def unload(self, entry, platforms):
        for entity in self.entities:
            await entity.async_will_remove_from_hass()
        self.entities.clear()
        for entity in self.sensor_entities:
            await entity.async_will_remove_from_hass()
        self.sensor_entities.clear()
        for entity in self.binary_sensor_entities:
            await entity.async_will_remove_from_hass()
        self.binary_sensor_entities.clear()
        for entity in self.buttons:
            await entity.async_will_remove_from_hass()
        self.buttons.clear()
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

    # Exercise the HA button, queued writes, and persistence across reload; related: button.py, storage.py.
    async def test_learning_reset_button_clears_only_its_entry_and_drains_writes(self):
        import tempfile
        from pathlib import Path
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.adaptive_floor_heating.curve_learning import CurveResult
        from custom_components.adaptive_floor_heating.curve_storage import CurveStore
        from custom_components.adaptive_floor_heating.history import CompletedCycle

        with tempfile.TemporaryDirectory() as folder:
            self.hass.config = SimpleNamespace(path=lambda *parts: str(Path(folder).joinpath(*parts)))
            self.write("sensor.room", "25")
            await self.integration.async_setup_entry(self.hass, self.entry)
            runtime = self.entry.runtime_data
            button, = self.buttons
            self.assertEqual(button.entity_id, "button.adaptive_heating_climate_learning_reset")
            self.assertTrue(button.available)
            runtime.set_target(22.5)
            runtime.set_mode("auto")
            runtime.set_prediction_mode("comfort")
            runtime.prediction_bias["curve"] = .2
            runtime.thermal_model.add_cycle(CompletedCycle(20, .5, 35, .4))
            runtime.learning_store.schedule(runtime.thermal_model.snapshot())
            from time import time
            wall = time()
            result = CurveResult("before", "WARM_HEATING", "THRESHOLD_START", "PREDICTIVE_STOP",
                                 "PEAK_CONFIRMED", "ECO", wall-3600, wall-1800, wall-300, wall, {0: .1, 1: .1}, .5,
                                 None, start_temperature=22, off_temperature=22.5, peak_temperature=23,
                                 target_peak_temperature=23.5, observed_peak_temperature=23)
            await runtime.curve_store.save(result, {"off_at": result.off_at, "actual_peak": 23,
                                                   "predictions": {"curve": 23.5}})
            self.assertEqual((await runtime.curve_store.read_cycles())["accuracy"]["count"], 1)
            other = CurveStore(self.hass, "another-entry")
            await other.open()
            await other.save(result)

            runtime.actuator.observed = True
            with self.assertRaises(ServiceValidationError):
                await button.async_press()
            runtime.actuator.observed = False
            runtime.actuator.pending = True
            with self.assertRaises(ServiceValidationError):
                await button.async_press()
            runtime.actuator.pending = None

            release = asyncio.Event()
            async def save_queued_cycle():
                from dataclasses import replace
                await release.wait()
                await runtime.curve_store.save(replace(result, cycle_id="queued"))
            queued = asyncio.create_task(save_queued_cycle())
            runtime._curve_tasks.add(queued)
            queued.add_done_callback(runtime._curve_tasks.discard)
            press = asyncio.create_task(button.async_press())
            await self.settle()
            self.assertFalse(button.available)
            with self.assertRaises(ServiceValidationError):
                await button.async_press()
            release.set()
            await press
            saved = await runtime.curve_store.read_cycles()
            self.assertEqual(saved["total"], 0)
            self.assertEqual(saved["accuracy"]["count"], 0)
            self.assertEqual(runtime.curve_store.model.shortfall_samples, [])
            self.assertEqual(runtime.curve_store.model.runtime_samples, [])
            self.assertEqual((await other.read_cycles())["total"], 1)
            self.assertIsNone(await runtime.learning_store.load())
            self.assertEqual(runtime.prediction_bias["curve"], 0)
            self.assertFalse(runtime.snapshot()["learning_reset_pending"])
            self.assertEqual((runtime.controller.target, runtime.controller.mode, runtime.prediction_mode),
                             (22.5, "auto", "comfort"))
            self.assertEqual(self.calls, [])
            self.assertTrue(await self.integration.async_unload_entry(self.hass, self.entry))
            await self.integration.async_setup_entry(self.hass, self.entry)
            self.assertEqual((await self.entry.runtime_data.curve_store.read_cycles())["total"], 0)
            self.assertEqual(self.entry.runtime_data.controller.target, 22.5)
            self.assertTrue(await self.integration.async_unload_entry(self.hass, self.entry))

    async def test_pending_learning_reset_completes_on_startup(self):
        await self.runtime.async_start()
        self.runtime.prediction_bias["curve"] = .3
        with patch.object(self.runtime.learning_store, "remove", side_effect=OSError("interrupted")):
            with self.assertLogs(level="ERROR"), self.assertRaises(OSError):
                await self.runtime.async_reset_learning()
        self.assertTrue((await self.runtime.store.load())["learning_reset_pending"])
        await self.runtime.async_stop()
        await self.runtime.async_start()
        self.assertFalse((await self.runtime.store.load())["learning_reset_pending"])
        self.assertEqual(self.runtime.prediction_bias["curve"], 0)

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
        self.assertEqual(self.runtime.curve_tracker.cycle.off_stop_tolerance,
                         self.runtime.controller.settings.heat_hot_tolerance)

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
        self.assertEqual(self.entities[0]._attr_temperature_unit, "°C")
        self.assertEqual(self.entities[0].current_temperature, 18)
        self.assertTrue(self.entities[0].available)

    # Exercise the native API without overriding deprecated names; related: climate.py.
    async def test_climate_uses_native_temperatures_when_supported(self):
        class NativeClimateEntity(FakeClimateEntity):
            @property
            def native_temperature_unit(self):
                return self._attr_native_temperature_unit

        spec = self.climate.__spec__
        climate = importlib.util.module_from_spec(spec)
        with patch("homeassistant.components.climate.ClimateEntity", NativeClimateEntity):
            spec.loader.exec_module(climate)

        entity = climate.AdaptiveFloorHeatingClimate(self.entry, self.runtime)
        await self.runtime.async_start()
        self.assertEqual(entity.native_temperature_unit, "°C")
        self.assertEqual(entity.native_current_temperature, 18)
        await entity.async_set_temperature(temperature=22)
        self.assertEqual(entity.native_target_temperature, 22)
        for name in ("_attr_temperature_unit", "current_temperature", "target_temperature"):
            self.assertNotIn(name, vars(type(entity)))

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

    async def test_unchanged_sensor_is_learned_once_per_check(self):
        await self.runtime.async_start()
        reported_listener = next(
            item for item in self.hass.bus.listeners if item[0] == "state_reported"
        )
        self.assertEqual(len(reported_listener), 3)
        self.write("sensor.room", "18", sample=False)
        self.assertEqual(len(self.runtime.observation.history.samples), 0)
        self.runtime._read_current_temperature(self.runtime._next_sensor_check)
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
        # Use an exact-second fixture so floating-point grid boundaries do not shift held reports.
        # Related: history.py TemperatureHistory.slope.
        now = int(self.hass.loop.time())
        observation = self.runtime.observation
        observation.history.clear()
        for elapsed in range(-2700, 1, 300):
            observation.history.add(now + elapsed, 18 + elapsed / 3600)
        self.runtime._read_current_temperature(now + 1)
        self.assertAlmostEqual(observation.temperature_slope, 1)
        reports = observation.history.samples
        self.runtime._read_current_temperature(now + 3000)
        self.assertAlmostEqual(observation.temperature_slope, 0)
        self.assertGreaterEqual(len(observation.history.samples), len(reports))
        self.assertEqual(observation.completed_cycles, 0)
        self.write("sensor.room", "unavailable", sample=False)
        self.runtime._read_current_temperature(now + 3300)
        self.runtime._read_current_temperature(now + 3600)
        self.assertIsNone(observation.temperature_slope)

    async def test_auto_schedules_held_slope_refresh_within_five_minutes(self):
        await self.runtime.async_start()
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertIsNotNone(self.runtime._timer)
        self.assertLessEqual(self.runtime._timer.when() - self.hass.loop.time(), 300)

    # Compare the exact sum used by control and confirmed-OFF diagnostics; related: peak_prediction.py, storage.py.

    # Exercise AWAY prediction eligibility with HOME evidence; related: runtime.py, curve_learning.py.
    async def test_away_residual_off_requires_valid_observation_and_supported_forecast(self):
        import time
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards
        from custom_components.adaptive_floor_heating.peak_prediction import sample

        await self.runtime.async_start()
        now, wall = self.hass.loop.time(), time.time()
        model = CurveStandards()
        self.runtime.outdoor_sensor = "sensor.outdoor"
        self.write("sensor.outdoor", "5", {"unit_of_measurement": "°C", "device_class": "temperature"})
        self.runtime._sensor_samples["sensor.outdoor"] = self.states["sensor.outdoor"]
        home_samples = [sample(20, 20.2, 21.2, minutes, 5, wall - 10000 - index)
                        for index, minutes in enumerate((55, 55, 60, 60, 65, 65))]
        self.runtime.curve_store = SimpleNamespace(model=model)
        cycle = CurveCycle("away", wall - 3600, "THRESHOLD_START", "BALANCED",
                           "WARM_HEATING", 20, 20.2, wall, wall + 300,
                           preset="away", invalid_reason="AWAY_CYCLE")
        self.runtime.curve_tracker.cycle = cycle
        self.runtime.controller.temperature = 20.2
        self.runtime.controller.target = 20.5
        self.runtime.controller.settings = Settings(minimum_on_time=900, hot_tolerance=0)
        self.runtime.actuator.observed = True
        heating = Decision(True, "HEATING")
        with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall):
            self.runtime.actuator.changed_at = now - 3600
            cycle.invalid_reason = "AWAY_CYCLE"
            model.peak_samples = home_samples
            self.assertIsNone(self.runtime._apply_curve_prediction(heating, now))
            self.assertEqual(self.runtime.hybrid_control_support["reason"], "NO_RUNTIME_PLAN")
            model.load_runtime_samples([(45, .2), (55, .3), (80, .6)])
            self.assertEqual(self.runtime._apply_curve_prediction(heating, now).state,
                             "PREDICTIVE_OFF")
            self.assertEqual(cycle.invalid_reason, "AWAY_CYCLE")
            self.runtime.actuator.changed_at = now - 100
            self.assertTrue(self.runtime._apply_curve_prediction(heating, now).heating)
            self.runtime.actuator.changed_at = now - 3600
            cycle.invalid_reason = "SENSOR_UNAVAILABLE"
            self.assertIsNone(self.runtime._apply_curve_prediction(heating, now))
            cycle.invalid_reason = "AWAY_CYCLE"
            model.peak_samples = []
            model.runtime_samples.clear()
            self.assertIsNone(self.runtime._apply_curve_prediction(heating, now))
        self.runtime.curve_store = None

    async def test_invalid_sensor_stops_on_second_check_and_recovers_on_next_valid_check(self):
        await self.start_heating()
        cycle = self.runtime.curve_tracker.cycle
        self.write("sensor.room", "unavailable")
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)
        self.write("sensor.room", "unknown")
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        self.assertEqual(cycle.invalid_reason, "SENSOR_UNAVAILABLE")
        self.write("sensor.room", "18")
        await self.settle()
        self.runtime._minimum_off_until = self.hass.loop.time()
        self.runtime.evaluate()
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)

    # Verify the real check deadline and per-sensor fallback. Related: runtime.py, history.py.
    async def test_five_minute_checks_share_sensor_state_with_control_and_learning(self):
        self.entry.options.update(outdoor_temperature_sensor="sensor.outdoor", sunrise_weather_entity="weather.home")
        self.write("sensor.outdoor", "5", {"device_class": "temperature", "unit_of_measurement": "°C"})
        self.write("weather.home", "sunny")
        self.runtime = self.make_runtime()
        await self.start_heating()
        runtime = self.runtime
        start = runtime._next_sensor_check

        self.write("sensor.room", "unavailable", sample=False)
        self.write("sensor.outdoor", "unknown", sample=False)
        self.write("weather.home", "unavailable", sample=False)
        with patch.object(self.hass.loop, "time", return_value=start - 1):
            runtime.evaluate()
        self.assertEqual(runtime._sensor_misses["sensor.room"], 0)

        with patch.object(self.hass.loop, "time", return_value=start):
            runtime.evaluate()
        self.assertTrue(runtime.actuator.observed)
        self.assertEqual(runtime.controller.temperature, 18)
        self.assertEqual(runtime.curve_tracker.temperature, 18)
        self.assertEqual(runtime._optional_temperature("sensor.outdoor", start), 5)
        self.assertEqual(runtime._sensor_samples["weather.home"].state, "sunny")

        self.write("sensor.room", "18.1", sample=False)
        with patch.object(self.hass.loop, "time", return_value=start + 300):
            runtime.evaluate()
        self.assertEqual(runtime._sensor_misses["sensor.room"], 0)
        self.assertEqual(runtime.curve_tracker.temperature, 18.1)
        self.assertIsNone(runtime._optional_temperature("sensor.outdoor", start + 300))
        self.assertIsNone(runtime._sensor_samples["weather.home"])
        self.assertTrue(runtime.actuator.observed)

    async def test_overheat_is_applied_at_the_next_sensor_check(self):
        await self.start_heating()
        runtime = self.runtime
        deadline = runtime._next_sensor_check
        self.write("sensor.room", "36", sample=False)
        with patch.object(self.hass.loop, "time", return_value=deadline - 1):
            runtime.evaluate()
        self.assertNotIn("overheat", runtime.controller.faults)
        self.assertTrue(runtime.actuator.observed)
        with patch.object(self.hass.loop, "time", return_value=deadline):
            runtime.evaluate()
        await self.settle()
        self.assertIn("overheat", runtime.controller.faults)
        self.assertFalse(runtime.actuator.observed)

    # Cover transient and lasting reports after reconnect; related: runtime.py, actuator.py, const.py.
    async def test_climate_reconnect_ignores_brief_setpoint_mismatch_and_sends_off(self):
        self.hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°C"))
        self.entry.data["heater"] = "climate.boiler"
        self.entry.options.update(climate_low=18, climate_high=30)
        self.write("sensor.room", "25")
        self.write("climate.boiler", "heat", {"temperature": 18})
        commands = []

        async def send_target(domain, action, data, blocking):
            commands.append(data["temperature"])

        self.hass.services.async_call = send_target
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.runtime = self.entry.runtime_data
        self.runtime.actuator._confirmation_delay = 0
        self.runtime.set_mode("auto")
        self.write("climate.boiler", "unavailable")
        self.write("climate.boiler", "heat", {"temperature": 18})
        self.write("climate.boiler", "heat", {"temperature": 30})
        await self.settle()
        self.assertEqual(commands, [18])
        self.assertEqual(self.runtime.controller.mode, "auto")
        self.assertNotIn("external_override", self.runtime.controller.faults)
        self.write("climate.boiler", "heat", {"temperature": 18})
        await self.settle()

        self.runtime._heater_recovery_deadlines["climate.boiler"] = self.hass.loop.time()
        self.runtime.evaluate()
        self.assertEqual(self.runtime.controller.mode, "auto")
        self.assertNotIn("external_override", self.runtime.controller.faults)
        self.write("climate.boiler", "heat", {"temperature": 30})
        self.assertEqual(self.runtime.controller.mode, "off")
        self.assertIn("external_override", self.runtime.controller.faults)
        self.write("climate.boiler", "heat", {"temperature": 18})
        await self.settle()

    async def test_reconnect_latches_persistent_mismatch_at_deadline_without_new_report(self):
        self.write("sensor.room", "25")
        await self.runtime.async_start()
        self.runtime.set_mode("auto")
        self.confirm_commands = False

        with patch("custom_components.adaptive_floor_heating.runtime.HEATER_RECOVERY_OVERRIDE_GRACE", 0.05):
            self.write("switch.heater", "unknown")
            self.write("switch.heater", "off")
            self.write("switch.heater", "on")
            await self.settle()
            self.assertEqual(self.runtime.controller.mode, "auto")
            self.assertNotIn("external_override", self.runtime.controller.faults)
            self.assertEqual(self.calls[-1][1], "turn_off")
            await asyncio.sleep(0.08)

        self.assertIn("external_override", self.runtime.controller.faults)
        self.assertEqual(self.runtime.controller.mode, "off")

    async def test_reconnect_climate_explicit_off_reverts_or_latches_with_same_setpoint(self):
        self.hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°C"))
        self.entry.data["heater"] = "climate.boiler"
        self.entry.options.update(climate_low=18, climate_high=30)
        self.write("sensor.room", "25")
        self.write("climate.boiler", "heat", {"temperature": 18})
        self.hass.services.async_call = AsyncMock()
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.runtime = self.entry.runtime_data
        self.runtime.actuator._confirmation_delay = 0
        self.runtime.set_mode("auto")
        self.write("climate.boiler", "unavailable")
        self.write("climate.boiler", "heat", {"temperature": 18})
        await self.settle()
        self.write("climate.boiler", "off", {"temperature": 18})
        self.assertEqual(self.runtime.controller.mode, "auto")
        self.write("climate.boiler", "heat", {"temperature": 18})
        self.assertNotIn("external_override", self.runtime.controller.faults)
        self.write("climate.boiler", "off", {"temperature": 18})
        self.runtime._heater_recovery_deadlines["climate.boiler"] = self.hass.loop.time()
        self.runtime.evaluate()
        self.assertEqual(self.runtime.controller.mode, "off")
        self.assertIn("external_override", self.runtime.controller.faults)

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

    # Verify AUTO recovery without bypassing actuator cooldown; related: runtime.py, controller.py, storage.py.
    async def test_auto_external_override_resumes_mode_during_off_wait(self):
        self.runtime = self.make_runtime(Settings(minimum_on_time=0, minimum_off_time=1800))
        self.write("sensor.room", "22")
        await self.runtime.async_start()
        self.runtime.set_mode("auto")
        self.write("switch.heater", "on")
        self.assertEqual(self.runtime.controller.mode, "off")
        self.assertTrue((await self.runtime.store.load())["auto_external_override"])

        with self.assertRaisesRegex(ValueError, "confirmed OFF"):
            self.runtime.set_mode("auto")
        self.runtime._confirmed_on_since = self.hass.loop.time() - 1200
        self.runtime.set_mode("off")
        await self.settle()
        self.assertIs(self.runtime.actuator.observed, False)
        self.write("sensor.room", "18")
        with self.assertRaisesRegex(ValueError, "minimum OFF time"):
            self.runtime.set_mode("heat")

        self.runtime.set_mode("auto")
        await self.settle()
        self.assertEqual(self.runtime.controller.mode, "auto")
        self.assertFalse(self.runtime.controller.faults)
        self.assertEqual(self.runtime.decision.state, "WAIT_MIN_OFF")
        self.assertEqual(self.runtime.decision.deadline, self.runtime._minimum_off_until)
        self.assertEqual([call[1] for call in self.calls], ["turn_off"])
        self.assertFalse((await self.runtime.store.load())["auto_external_override"])

        self.runtime.actuator.changed_at -= 1800
        self.runtime.evaluate()
        self.assertEqual(self.runtime.decision.state, "WAIT_MIN_OFF")
        self.runtime._minimum_off_until = self.hass.loop.time()
        self.runtime.evaluate()
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)
        self.assertEqual([call[1] for call in self.calls], ["turn_off", "turn_on"])

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
        self.assertEqual(len(self.sensor_entities), 6)
        self.assertEqual(len(self.binary_sensor_entities), 3)
        self.assertEqual(
            {entity.entity_description.key for entity in self.sensor_entities},
            {"prediction_status", "peak_prediction_accuracy", "last_heating_duration",
             "average_heating_duration", "last_peak_error", "mean_absolute_peak_error"},
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
            SimpleNamespace(domain="sensor", unique_id="one_experimental_status", entity_id="sensor.renamed_experiment"),
            SimpleNamespace(domain="sensor", unique_id="other_water_supply", entity_id="sensor.other_pipe"),
            SimpleNamespace(domain="sensor", unique_id="one_learning_status", entity_id="sensor.learning"),
            SimpleNamespace(domain="sensor", unique_id="one_observation_status", entity_id="sensor.observation"),
            SimpleNamespace(domain="sensor", unique_id="one_prediction_status", entity_id="sensor.renamed_prediction"),
            SimpleNamespace(domain="sensor", unique_id="other_initial_learning_status", entity_id="sensor.other_initial"),
            SimpleNamespace(domain="switch", unique_id="one_initial_learning_status", entity_id="switch.unrelated"),
        ]
        before = self.runtime.snapshot()
        self.entry.runtime_data = self.runtime
        with patch.object(self.sensor.er, "async_get", return_value=registry), \
             patch.object(self.sensor.er, "async_entries_for_config_entry", return_value=registered):
            await self.sensor.async_setup_entry(self.hass, self.entry, added.extend)
        self.assertEqual(removed, ["sensor.renamed_initial", "sensor.renamed_pipe", "sensor.renamed_experiment",
                                   "sensor.learning", "sensor.observation"])
        self.assertEqual({entity.entity_description.key for entity in added},
                         {"prediction_status", *(item.key for item in self.sensor.METRIC_SENSORS)})
        self.assertEqual(self.runtime.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_state_sensor_metadata_contains_only_phase_enums(self):
        descriptions = self.sensor.STATE_SENSORS
        self.assertEqual(len(descriptions), 1)
        self.assertTrue(all(item.icon for item in descriptions))
        self.assertTrue(all(item.device_class == "enum" for item in descriptions))
        self.assertTrue(all(item.native_unit_of_measurement is None for item in descriptions))
        self.assertTrue(all(item.state_class is None for item in descriptions))
        self.assertEqual(
            {item.key for item in descriptions},
            {"prediction_status"},
        )

    async def test_retired_pipe_options_are_ignored_while_outdoor_is_sampled(self):
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
        self.assertEqual(set(self.runtime._sensor_samples), {"sensor.room", "sensor.outdoor"})
        for kind, _, event_filter in self.hass.bus.listeners:
            if kind in ("state_changed", "state_reported"):
                self.assertFalse(event_filter({"entity_id": "sensor.outdoor"}))
                self.assertFalse(event_filter({"entity_id": "sensor.supply"}))
                self.assertFalse(event_filter({"entity_id": "sensor.return"}))
        self.assertFalse(hasattr(self.runtime, "water_observation"))
        self.assertFalse(hasattr(self.runtime, "heat_loss_observation"))
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

    async def test_maximum_runtime_limit_stops_and_keeps_coast_learning(self):
        # Exercise a capped run and its normal Peak observer; related: runtime.py.
        self.runtime = self.make_runtime(Settings(initial_maximum_on_minutes=180, minimum_on_time=0))
        await self.start_heating()
        runtime = self.runtime
        cycle = runtime.curve_tracker.cycle
        self.assertAlmostEqual(runtime._maximum_on_deadline - runtime._confirmed_on_since, 10800, places=6)
        runtime._confirmed_on_since = self.hass.loop.time() - 10800
        runtime._maximum_on_deadline = runtime._confirmed_on_since + 10800
        runtime.evaluate()
        self.assertEqual(runtime.decision.state, "MAXIMUM_RUNTIME_LIMIT")
        await self.settle()
        self.assertFalse(runtime.actuator.observed)
        self.assertEqual(cycle.off_reason, "MAXIMUM_RUNTIME_LIMIT")
        self.assertIsNotNone(cycle.off_at)
        self.assertGreater(runtime._minimum_off_deadline() - self.hass.loop.time(), 21599)
        for elapsed, temperature in ((600, 18.6), (660, 18.4), (720, 18.3)):
            with patch("custom_components.adaptive_floor_heating.runtime.time.time",
                       return_value=cycle.off_at + elapsed):
                self.write("sensor.room", str(temperature))
        self.assertEqual(cycle.peak_temperature, 18.6)
        self.assertIsNone(cycle.invalid_reason)

    async def test_maximum_runtime_limit_applies_after_learning(self):
        await self.runtime.async_start()
        runtime = self.runtime
        runtime.curve_store = SimpleNamespace(model=SimpleNamespace(runtime_accepted_cycles=10))
        runtime._record_confirmed_run(True, 100)
        self.assertEqual(runtime._maximum_on_deadline, 3700)
        runtime._record_confirmed_run(None, 110)
        runtime._record_confirmed_run(True, 120)
        self.assertEqual(runtime._maximum_on_deadline, 3700)
        runtime._record_confirmed_run(False, 130)
        self.assertIsNone(runtime._maximum_on_deadline)
        runtime._record_confirmed_run(True, 140)
        self.assertEqual(runtime._maximum_on_deadline, 3740)
        runtime.curve_store = None

    async def test_learning_resumes_after_same_heater_state_recovers(self):
        # Verify replay both before and after OFF; related: runtime.py, curve_learning.py.
        await self.start_heating()
        runtime = self.runtime
        cycle = runtime.curve_tracker.cycle
        self.write("switch.heater", "unavailable")
        self.write("sensor.room", "19.5")
        self.assertNotEqual(runtime.curve_tracker.temperature, 19.5)
        self.write("switch.heater", "on")
        self.assertIs(runtime.curve_tracker.cycle, cycle)
        self.assertEqual(runtime.curve_tracker.temperature, 19.5)
        self.assertIsNone(cycle.invalid_reason)

        self.write("sensor.room", "20.5")
        await self.settle()
        self.assertIs(runtime.actuator.observed, False)
        self.assertIsNotNone(cycle.off_at)
        self.write("switch.heater", "unavailable")
        self.write("sensor.room", "20.6")
        self.write("switch.heater", "off")
        self.assertIs(runtime.curve_tracker.cycle, cycle)
        self.assertEqual(cycle.off_reports[-1][1], 20.6)
        self.assertIsNone(cycle.invalid_reason)

    async def test_learning_rejects_outage_when_heater_state_changes(self):
        await self.start_heating()
        cycle = self.runtime.curve_tracker.cycle
        self.write("switch.heater", "unavailable")
        self.write("switch.heater", "off")
        self.assertEqual(cycle.invalid_reason, "HEATER_UNAVAILABLE")
        self.assertIsNot(self.runtime.curve_tracker.cycle, cycle)

    async def test_learning_rejects_outage_at_grace_deadline(self):
        await self.start_heating()
        runtime = self.runtime
        cycle = runtime.curve_tracker.cycle
        self.write("switch.heater", "unavailable")
        with patch.object(self.hass.loop, "time", return_value=runtime._learning_outage_deadline):
            runtime.evaluate()
        self.assertEqual(cycle.invalid_reason, "HEATER_UNAVAILABLE")
        self.assertIsNone(runtime.curve_tracker.cycle)

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

    # Verify a new thermostat target keeps valid OFF observations and the original comparison; related: runtime.py.
    async def test_target_change_while_off_keeps_peak_learning(self):
        await self.start_heating()
        self.write("sensor.room", "20.5")
        await self.settle()
        self.assertIs(self.runtime.actuator.observed, False)
        cycle = self.runtime.curve_tracker.cycle
        observed_off_at = self.runtime.observation.off_at
        off_at = self.runtime.last_off_prediction["off_at"]
        self.runtime._off_predictions = {"curve": 21.3}
        self.runtime._off_peak_deadlines = {"curve": self.hass.loop.time() + 600}
        self.runtime.set_target(19.5)
        self.assertIs(self.runtime.curve_tracker.cycle, cycle)
        self.assertIsNone(cycle.invalid_reason)
        self.assertIsNone(cycle.hold_exclusion)
        self.assertEqual(cycle.hold_settings["target_temperature"], 20)
        self.assertEqual(self.runtime.observation.off_at, observed_off_at)
        self.assertEqual(self.runtime._off_peak_deadlines, {})
        for elapsed, temperature in ((610, 21.0), (620, 20.8), (630, 20.7)):
            with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=off_at + elapsed), \
                 patch.object(self.hass.loop, "time", return_value=observed_off_at + elapsed):
                self.write("sensor.room", str(temperature))
        await self.settle()
        self.assertEqual(cycle.peak_temperature, 21.0)
        comparison = self.runtime.last_peak_comparison
        self.assertAlmostEqual(comparison["off_at"], off_at, delta=0.1)
        self.assertEqual(comparison["predictions"], {"curve": 21.3})
        self.assertEqual(comparison["actual_peak"], 21.0)
        with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=off_at + 1800), \
             patch.object(self.hass.loop, "time", return_value=observed_off_at + 1800):
            self.write("sensor.room", "20")
        await self.settle()
        self.assertEqual(cycle.hold_crossing[0], off_at + 1800)
        self.assertIs(self.runtime.actuator.observed, False)

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
        self.runtime._next_sensor_check = now
        self.runtime._read_current_temperature(now, learn=False)
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
        self.runtime._next_sensor_check = now
        self.runtime._read_current_temperature(now, learn=False)
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
        self.runtime._next_sensor_check = now
        self.runtime._read_current_temperature(now, learn=False)
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

    async def test_saved_off_model_choice_is_retired(self):
        self.hass.saved["adaptive_floor_heating.one.runtime"] = {
            **self.runtime.snapshot(), "off_model": "additive",
        }
        await self.runtime.async_start()
        self.assertNotIn("off_model", self.runtime.snapshot())
        self.assertFalse(hasattr(self.runtime, "set_off_model"))
        removed, added = [], []
        registry = SimpleNamespace(async_remove=removed.append)
        registered = [
            SimpleNamespace(domain="select", unique_id="one_off_model", entity_id="select.renamed"),
            SimpleNamespace(domain="select", unique_id="other_off_model", entity_id="select.other"),
        ]
        with patch.object(self.select.er, "async_get", return_value=registry), \
             patch.object(self.select.er, "async_entries_for_config_entry", return_value=registered):
            await self.select.async_setup_entry(self.hass, self.entry, added.extend)
        self.assertEqual(removed, ["select.renamed"])
        self.assertEqual(added, [])

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

    async def test_weighted_residual_can_advance_supported_plan_at_auto_peak_target(self):
        """Use weighted residual only for an earlier stop within the active reservation."""
        import time
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards
        from custom_components.adaptive_floor_heating.peak_prediction import sample

        await self.runtime.async_start()
        now, wall = self.hass.loop.time(), time.time()
        self.runtime.controller.target = 23
        self.runtime.controller.temperature = 22.5
        self.runtime.controller.settings = Settings(
            hot_tolerance=0.5, minimum_on_time=0, minimum_off_time=0
        )
        self.runtime.actuator.observed = True
        self.runtime.actuator.changed_at = now - 1800
        model = CurveStandards()
        self.runtime.outdoor_sensor = "sensor.outdoor"
        self.write("sensor.outdoor", "5", {"unit_of_measurement": "°C", "device_class": "temperature"})
        self.runtime._sensor_samples["sensor.outdoor"] = self.states["sensor.outdoor"]
        model.peak_samples = [sample(21, 22.5, 23.5, minutes, 5, wall-3600) for minutes in (25, 26, 34, 35)]
        model.load_runtime_samples([(25, .7), (28, 1.2), (45, 1.6)])
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.curve_tracker.cycle = CurveCycle(
            "active", wall-1800, "THRESHOLD_START", "BALANCED", "WARM_HEATING",
            22.2, 22.5, wall, wall+300,
        )
        with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall):
            self.runtime.controller.temperature = 22.5
            decision = self.runtime._apply_curve_prediction(Decision(True, "HEATING"), now)
            self.assertEqual(decision.state, "PREDICTIVE_OFF")
            self.assertEqual(self.runtime.off_prediction["off_model"], "hybrid")
            forecast = self.runtime.off_prediction["peak_forecast"]
            control = self.runtime.off_prediction["off_control"]
            self.assertEqual(forecast["kind"], "weighted_residual_v1")
            self.assertEqual(control["action"], "early_off")
            self.assertGreater(self.runtime._runtime_off_planned_at, now)
            self.runtime.controller.temperature = 22.4
            decision = self.runtime._apply_curve_prediction(Decision(True, "HEATING"), now)
            self.assertEqual(decision.state, "HEATING")
            self.assertEqual(self.runtime.hybrid_control_support["reason"], "BELOW_TARGET")
        self.runtime.last_off_prediction = {"off_at": wall, "predictions": {"curve": forecast["predicted_peak"]},
                                            "off_model": "hybrid", "peak_forecast": forecast,
                                            "off_control": control,
                                            "selected_model": "curve", "confidences": {"curve": None}}
        self.runtime._record_peak_comparison(forecast["predicted_peak"]-.2, wall, None)
        self.assertEqual(self.runtime.hybrid_feedback["bias"], 0)
        self.assertEqual(self.runtime.additive_feedback["errors"], [])
        self.assertEqual(self.runtime.last_peak_comparison["off_model"], "hybrid")
        self.assertEqual(self.runtime.last_peak_comparison["forecast_kind"], "weighted_residual_v1")
        restored = await self.runtime.store.load()
        self.assertEqual(restored["hybrid_feedback"]["bias"], 0)
        self.assertEqual(restored["last_off_prediction"]["off_model"], "hybrid")
        self.assertEqual(restored["last_off_prediction"]["peak_forecast"], forecast)
        self.assertEqual(restored["last_peak_comparison"]["off_control"], control)
        self.runtime.curve_store = None

    # Snapshot before tasks run so later sensor reports cannot rewrite historical forecasts; related: curve_storage.py.
    async def test_curve_result_captures_its_matching_comparison_before_async_save(self):
        from custom_components.adaptive_floor_heating.curve_learning import CurveResult

        result = CurveResult("history", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
                             "PEAK_CONFIRMED", "BALANCED", 100, 700, 1000, 1500,
                             {0: 0, 1: .2}, .2, None, peak_temperature=23.2)
        save = AsyncMock()
        self.runtime.curve_store = SimpleNamespace(save=save)
        self.runtime.last_peak_comparison = {"off_at": 700, "actual_peak": 23.2,
                                             "predictions": {"curve": 23.5}}
        with patch.object(self.runtime.curve_tracker, "take_results", return_value=[result]), \
                patch.object(self.runtime, "_record_peak_comparison"):
            self.runtime._queue_curve_results()
        tasks = list(self.runtime._curve_tasks)
        self.runtime.last_peak_comparison["predictions"]["curve"] = 99
        await asyncio.gather(*tasks)
        save.assert_awaited_once_with(result, {"off_at": 700, "actual_peak": 23.2,
                                             "predictions": {"curve": 23.5}})
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

    async def test_predictive_on_origin_survives_delayed_confirmation(self):
        from custom_components.adaptive_floor_heating.controller import Decision

        await self.runtime.async_start()
        self.confirm_commands = False
        with patch.object(self.runtime, "_safe_model_prediction", return_value=Decision(True, "PREDICTIVE_ON")):
            self.runtime.set_mode("auto")
            self.runtime.evaluate()
            await self.settle()
        self.assertIs(self.runtime.actuator.pending, True)
        with patch.object(self.runtime, "_safe_model_prediction", return_value=Decision(True, "HEATING")):
            self.runtime.evaluate()
            self.assertEqual(self.runtime.decision.state, "HEATING")
            self.write("switch.heater", "on")
        self.assertEqual(self.runtime.curve_tracker.cycle.start_reason, "PREDICTIVE_START")
        self.assertEqual(self.runtime.curve_tracker.cycle.curve_type, "PREDICTIVE_WARM_HEATING")
        self.assertFalse(self.runtime.actuator.predictive_start_requested)
        self.confirm_commands = True

    # Verify preset transitions through actual command confirmation, not just a model return value.
    # Related: runtime.py _predictive_start, actuator.py and curve_learning.py start attribution.
    async def _check_home_start_classification(self, temperature, predictive):
        import time
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards

        self.runtime = self.make_runtime(Settings(
            home_temperature=23, away_temperature=21, minimum_on_time=0, minimum_off_time=0))
        self.write("sensor.room", str(temperature))
        await self.runtime.async_start()
        self.runtime.set_preset("away")
        self.runtime.set_mode("auto")
        await self.settle()
        self.assertFalse(self.runtime.actuator.observed)
        now, wall = self.hass.loop.time(), time.time()
        model = CurveStandards()
        model.buckets["PREDICTIVE_WARM_HEATING"] = {0: (0, 3), 1: (.1, 3)}
        model.buckets["COOLING"] = {i: (-.1, 3) for i in range(6)}
        self.runtime.curve_store = SimpleNamespace(model=model, save=AsyncMock())
        self.runtime.prediction_mode = "balanced"
        self.runtime.actuator.changed_at = now - 1800
        self.runtime.curve_tracker.cycle = CurveCycle(
            "previous-home-cooling", wall - 2000, "THRESHOLD_START", "BALANCED", "WARM_HEATING",
            22, temperature, wall, wall + 300, off_at=wall - 1000,
            peak_at=wall - 300, peak_temperature=23.4, heating_saved=True)
        with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall):
            self.runtime.set_preset("home")
            self.assertEqual(self.runtime.decision.state, "PREDICTIVE_ON" if predictive else "HEATING")
            await self.settle()
        self.assertTrue(self.runtime.actuator.observed)
        self.assertEqual(self.runtime.curve_tracker.cycle.start_reason,
                         "PREDICTIVE_START" if predictive else "THRESHOLD_START")
        self.assertEqual(self.runtime.curve_tracker.cycle.curve_type,
                         "PREDICTIVE_WARM_HEATING" if predictive else "WARM_HEATING")
        self.runtime.curve_store = None

    async def test_home_preset_below_start_threshold_uses_normal_heating(self):
        await self._check_home_start_classification(22.4, False)

    async def test_home_preset_at_start_threshold_uses_normal_heating(self):
        await self._check_home_start_classification(22.5, False)

    async def test_home_preset_above_start_threshold_can_use_predictive_on(self):
        await self._check_home_start_classification(22.6, True)

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
        # A rebound can erase the net loss, but the earlier dip still crosses the threshold.
        self.runtime.controller.temperature = 22.7
        model.buckets["COOLING"] = {1: (-.3, 3), 2: (.35, 3)}
        with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall):
            self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "PREDICTIVE_ON")
            self.assertAlmostEqual(self.runtime.start_prediction["predicted_minimum"], 22.4)
            self.assertAlmostEqual(self.runtime.start_prediction["cooling_delta"], -.3)
            self.assertAlmostEqual(model.predict_cooling_delta(5, 10), .05)
            del model.buckets["COOLING"][2]
            self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "IDLE")
            self.assertEqual(self.runtime.start_prediction["reason"], "cooling_coverage_missing")
        model.buckets["COOLING"] = {i: (-0.1, 3) for i in range(6)}
        self.runtime._minimum_off_until = now + 60
        self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "IDLE")
        self.assertEqual(self.runtime.start_prediction["status"], "blocked")
        self.runtime._minimum_off_until = None
        # Only a measured Peak from this confirmed OFF period may drive predictive ON.
        self.runtime.last_peak_comparison = {"off_at": wall-1000, "peak_at": wall-300}
        self.runtime.curve_tracker.cycle.peak_at = None
        model.load_runtime_samples([(30, .4, 20), (40, .8, 20), (50, 1.2, 20)])
        self.runtime.controller.temperature = 22.8
        with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall):
            self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "IDLE")
            self.assertEqual(self.runtime.start_prediction["reason"], "peak_unconfirmed")
            self.runtime.curve_tracker.cycle = None
            self.runtime.curve_tracker.last_off_at = wall-600
            self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "IDLE")
            self.runtime.curve_tracker.seed(False, 22.8, wall, confirmed_off_at=wall-1000)
            self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "PREDICTIVE_ON")
        self.assertEqual(self.runtime.start_prediction["response_delay_minutes"], 20)
        self.assertAlmostEqual(self.runtime.start_prediction["predicted_minimum"], 22.4)
        model.buckets["COOLING"].clear()
        self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "IDLE")
        self.assertEqual(self.runtime.start_prediction["status"], "insufficient_data")
        self.assertEqual(self.runtime.start_prediction["reason"], "cooling_coverage_missing")
        self.assertEqual(self.runtime.start_prediction["response_delay_minutes"], 20)
        self.assertNotIn("cooling_layers", self.runtime.start_prediction)
        self.runtime.curve_store = None

    async def test_matched_off_response_does_not_control_runtime_model(self):
        """Use the thermostat when the selected runtime model lacks support."""
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
            base = Decision(True, "HEATING")
            self.runtime.controller.target = 20
            planned = self.runtime._apply_curve_prediction(base, now)
            self.assertEqual(planned.deadline, now + 1200)
            self.assertEqual(self.runtime._apply_curve_prediction(base, planned.deadline - 300), planned)
            self.runtime._clear_runtime_off_schedule()
            self.runtime.controller.target = 21
            self.assertEqual(self.runtime._apply_curve_prediction(base, now), base)
            self.assertIsNone(self.runtime._runtime_off_planned_at)
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
        with patch.object(model, "_runtime_curve", wraps=model._runtime_curve) as fitted:
            self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "HEATING")
            self.assertEqual(fitted.call_count, 1)
        self.runtime.actuator.changed_at = now - 51 * 60
        result = self.runtime._apply_curve_prediction(base, now)
        self.assertEqual(result.state, "PREDICTIVE_OFF")
        self.assertAlmostEqual(self.runtime.off_prediction["predicted_peak"], 20.04)
        self.runtime.controller.target = 20.4
        planned = self.runtime._apply_curve_prediction(base, now)
        self.assertTrue(planned.heating)
        self.assertEqual(self.runtime._apply_curve_prediction(base, planned.deadline + 0.01).state, "PREDICTIVE_OFF")
        self.runtime.curve_store = None

    # Exercise the actual reservation path, including the independent hard minimum.
    # Related: curve_learning.py runtime_shutdown_limits and controller.py minimum ON.
    async def test_relaxed_runtime_floor_reserves_learned_time_and_keeps_minimum_on(self):
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards
        await self.runtime.async_start()
        now, wall = self.hass.loop.time(), datetime.now(UTC).timestamp()
        model = CurveStandards()
        model.load_runtime_samples([(40, 1), (60, 2), (80, 3)])
        model.runtime_average_samples = [60] * 10
        model.runtime_accepted_cycles = 9
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.actuator.changed_at = now - 20 * 60
        cycle = CurveCycle("relaxed", wall - 1200, "THRESHOLD_START", "BALANCED",
                           "WARM_HEATING", 20, 20.2, wall, wall + 300)
        base = Decision(True, "HEATING")
        result = self.runtime._apply_runtime_off(base, now, False, 20.8, 20.2, cycle, model)
        self.assertTrue(result.heating)
        self.assertAlmostEqual(self.runtime.runtime_off_plan["planned_minutes"], 48)
        self.runtime._clear_runtime_off_schedule()
        model.runtime_accepted_cycles = 10
        self.runtime._apply_runtime_off(base, now, False, 20.8, 20.2, cycle, model)
        self.assertAlmostEqual(self.runtime.runtime_off_plan["planned_minutes"], 54)
        self.runtime._clear_runtime_off_schedule()
        from dataclasses import replace
        self.runtime.controller.settings = replace(self.runtime.controller.settings, minimum_on_time=55 * 60)
        self.runtime._apply_runtime_off(base, now, False, 20.8, 20.2, cycle, model)
        self.assertAlmostEqual(self.runtime.runtime_off_plan["planned_minutes"], 55)
        self.runtime.curve_store = None

    # Exercise the saving-only cutoff and its persisted control evidence; related: runtime.py.
    async def test_sunrise_off_uses_scaled_or_dedicated_floor_and_keeps_configured_minimum(self):
        from dataclasses import replace
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards

        now, wall = self.hass.loop.time(), datetime.now(UTC).timestamp()
        regular, sunrise = CurveStandards(), CurveStandards()
        regular.load_runtime_samples([(40, 1), (60, 2), (80, 3)])
        regular.runtime_average_samples = [60] * 10
        self.runtime.controller.mode = "auto"
        self.runtime.controller.target = 20.5
        self.runtime.controller.settings = replace(
            self.runtime.controller.settings, hot_tolerance=.5, minimum_on_time=25 * 60,
        )
        self.runtime._sunrise_active = True
        self.runtime.actuator.changed_at = now - 10 * 60
        cycle = CurveCycle("saving-floor", wall - 600, "THRESHOLD_START", "BALANCED",
                           "WARM_HEATING", 20, 20.1, wall, wall + 300)
        decision = Decision(True, "HEATING")
        with patch.object(self.runtime, "curve_store", SimpleNamespace(model=regular, sunrise_runtime_model=sunrise)):
            self.runtime._apply_runtime_off(decision, now, True, 20.5, 20.1, cycle, regular)
            plan = self.runtime.runtime_off_plan
            self.assertAlmostEqual(plan["planned_minutes"], 25)
            limits = plan["runtime_shutdown_limits"]
            self.assertEqual(limits["source"], "scaled_regular_average")
            self.assertEqual(limits["average_floor"], 18)
            self.assertEqual(limits["model_minutes"], 20)
            self.assertEqual(limits["planned_by"], "configured_minimum")

            self.runtime._clear_runtime_off_schedule()
            sunrise.load_runtime_samples([(20, .3), (30, .6), (40, .9)])
            sunrise.runtime_average_samples = [20, 30, 40]
            self.runtime._apply_runtime_off(decision, now, True, 20.5, 20.1, cycle, regular)
            limits = self.runtime.runtime_off_plan["runtime_shutdown_limits"]
            self.assertEqual(limits["source"], "sunrise_saving")
            self.assertEqual(limits["measured_minimum"], 30)
            self.assertAlmostEqual(limits["model_minutes"], 26.66666667)

    async def test_off_models_can_use_stable_long_term_after_recent_samples_expire(self):
        import time
        from dataclasses import replace
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards
        from custom_components.adaptive_floor_heating.curve_memory import CURRENT_MAX_AGE

        await self.runtime.async_start()
        now, wall = self.hass.loop.time(), time.time()
        model = CurveStandards()
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.controller.settings = replace(self.runtime.controller.settings, hot_tolerance=0, minimum_on_time=0)
        self.runtime.controller.target = 23.49
        self.runtime.controller.temperature = 22.5
        self.runtime.actuator.observed = True
        self.runtime.actuator.changed_at = now - 37.5 * 60
        self.runtime.curve_tracker.cycle = CurveCycle(
            "seasonal", wall - 37.5 * 60, "THRESHOLD_START", "BALANCED",
            "WARM_HEATING", 22, 22.5, wall, wall + 300, outdoor_temperature=-2,
        )
        for index in range(8):
            for minutes, peak in ((35, 23.25), (40, 23.75)):
                sample = {"start": 22, "off": 22.5, "peak": peak, "minutes": minutes,
                          "outdoor": -2, "completed": wall - 2 * CURRENT_MAX_AGE + index}
                model.learn_hybrid_residual(sample)
                model.learn_runtime_rise(sample)
        base = Decision(True, "HEATING")
        with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall):
            for preset, invalid_reason in (
                ("home", None),
                ("away", "AWAY_CYCLE"),
            ):
                self.runtime.curve_tracker.cycle.preset = preset
                self.runtime.curve_tracker.cycle.invalid_reason = invalid_reason
                self.runtime.controller.settings = replace(self.runtime.controller.settings, minimum_on_time=39*60)
                self.assertTrue(self.runtime._apply_curve_prediction(base, now).heating)
                self.assertAlmostEqual(self.runtime.runtime_off_plan["planned_minutes"], 39)
                self.runtime._clear_runtime_off_schedule()
                self.runtime.controller.settings = replace(self.runtime.controller.settings, minimum_on_time=0)
                self.assertEqual(self.runtime._apply_curve_prediction(base, now).state, "PREDICTIVE_OFF")
                self.assertEqual(self.runtime.off_prediction["off_learning_source"], "long_term")
                self.assertEqual(self.runtime.off_prediction["off_model"], "runtime")
                fault = Decision(False, "FAULT")
                self.assertEqual(self.runtime._apply_curve_prediction(fault, now), fault)
            self.runtime.controller.target = 24
            self.assertIsNone(self.runtime._apply_curve_prediction(base, now))
        self.runtime.curve_store = None

    async def test_runtime_off_plan_is_never_extended_by_residual_forecast(self):
        """Protect the reserved cutoff with or without comparable weather evidence."""
        import time
        from custom_components.adaptive_floor_heating.controller import Decision
        from custom_components.adaptive_floor_heating.curve_learning import CurveCycle, CurveStandards
        from custom_components.adaptive_floor_heating.peak_prediction import sample

        await self.runtime.async_start()
        now, wall = self.hass.loop.time(), time.time()
        model = CurveStandards()
        model.load_runtime_samples([(60, 1), (90, 2), (110, 3)])
        self.runtime.curve_store = SimpleNamespace(model=model)
        self.runtime.controller.mode = "auto"
        self.runtime.controller.settings = Settings(minimum_on_time=900, hot_tolerance=.5, sensor_timeout=3600)
        self.runtime.controller.temperature = 22.1
        self.runtime.controller.target = 23
        self.runtime.actuator.observed = True
        self.runtime.outdoor_sensor = "sensor.outdoor"
        self.write("sensor.outdoor", "1", {"unit_of_measurement": "°C", "device_class": "temperature"})
        base = Decision(True, "HEATING")
        try:
            for past_outdoor, comparable in ((8, False), (1, True)):
                with self.subTest(past_outdoor=past_outdoor):
                    self.runtime._clear_runtime_off_schedule()
                    model.peak_samples = [sample(22.5, 22.5, 23.5, minutes, past_outdoor, wall-86400-i)
                                          for i, minutes in enumerate((95, 100, 105))]
                    self.runtime.actuator.changed_at = now
                    self.runtime._sensor_samples["sensor.outdoor"] = self.states["sensor.outdoor"]
                    self.runtime.curve_tracker.cycle = CurveCycle(
                        "cold-long", wall, "THRESHOLD_START", "ECO", "WARM_HEATING",
                        21, 22.1, wall, wall+300)
                    with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=wall):
                        planned = self.runtime._apply_curve_prediction(base, now)
                    self.assertTrue(planned.heating)
                    self.assertAlmostEqual(self.runtime.runtime_off_plan["planned_minutes"], 100)
                    cutoff_wall = wall + planned.deadline - now
                    with patch("custom_components.adaptive_floor_heating.runtime.time.time", return_value=cutoff_wall):
                        result = self.runtime._apply_curve_prediction(base, planned.deadline)
                        captured = self.runtime._predict_both_off(planned.deadline, live_forecast=self.runtime.off_prediction)
                    self.assertEqual(result.state, "PREDICTIVE_OFF")
                    self.assertEqual(self.runtime.active_off_model, "runtime")
                    support = self.runtime.hybrid_control_support
                    self.assertEqual(support["action"], "plan_off")
                    self.assertEqual(support["forecast_available"], comparable)
                    self.assertEqual("residual_preview" in self.runtime.off_prediction, comparable)
                    self.assertIn("curve", captured)
                    self.assertNotIn("curve", self.runtime._off_peak_deadlines)
        finally:
            self.runtime.curve_store = None

    async def test_runtime_reservation_stops_once_at_its_deadline_if_supported(self):
        """Schedule one cutoff and cancel stale or unsupported plans; related: runtime.py, panel.js."""
        from custom_components.adaptive_floor_heating.controller import Decision
        await self.runtime.async_start()
        now = self.hass.loop.time()
        self.runtime.actuator.changed_at = now
        base = Decision(True, "HEATING")
        def decide(at, *, required=30, minimum=25, extrapolated=False):
            return self.runtime._runtime_cutoff_decision(base, at, required, minimum, extrapolated)
        planned = decide(now)
        self.assertEqual(planned.deadline, now + 30*60)
        self.assertEqual(decide(now+25*60, required=40), planned)
        self.assertEqual(decide(planned.deadline).state, "PREDICTIVE_OFF")
        self.assertEqual(self.runtime.heating_prediction_info["planned_minutes"], 30)
        decide(now)
        self.assertEqual(decide(planned.deadline, extrapolated=True), base)
        self.assertIsNone(self.runtime._runtime_off_planned_at)
        decide(now)
        self.assertEqual(decide(planned.deadline, minimum=35), base)
        self.assertIsNone(self.runtime._runtime_off_planned_at)

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

    async def test_long_term_response_alone_cannot_substitute_for_selected_runtime_model(self):
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
        self.assertIsNone(decision)
        self.assertIsNone(self.runtime.off_prediction)
        self.assertGreater(model.accepted["WARM_HEATING"], 0)
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
        self.assertEqual(self.runtime.start_prediction["cooling_source"], "global")
        self.assertEqual(self.runtime.start_prediction["cooling_layers"], ["long_term"])
        model.outdoor_cooling = {("COLD", i): (-0.02, 3) for i in range(6)}
        with patch.object(self.runtime, "_optional_temperature", return_value=-2):
            decision = self.runtime._apply_curve_prediction(Decision(False, "IDLE"), now)
            self.assertEqual(decision.state, "IDLE")
            self.assertEqual(self.runtime.start_prediction["cooling_source"], "outdoor")
            self.assertEqual(self.runtime.start_prediction["cooling_layers"], ["legacy"])
            model.outdoor_cooling.pop(("COLD", 1))
            decision = self.runtime._apply_curve_prediction(Decision(False, "IDLE"), now)
            self.assertEqual(decision.state, "PREDICTIVE_ON")
            self.assertEqual(self.runtime.start_prediction["cooling_source"], "global")
            self.assertEqual(self.runtime.start_prediction["cooling_layers"], ["long_term"])
            for index in range(6):
                for count in range(8):
                    model.outdoor_cooling_memory["COLD"].learn_conditioned_bucket(index, .02, wall-10+count)
            decision = self.runtime._apply_curve_prediction(Decision(False, "IDLE"), now)
            self.assertEqual(decision.state, "IDLE")
            self.assertEqual(self.runtime.start_prediction["cooling_source"], "outdoor")
            self.assertEqual(self.runtime.start_prediction["cooling_layers"], ["blended"])
            self.assertEqual(self.runtime.start_prediction["status"], "no_cooling")
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
        from custom_components.adaptive_floor_heating.diagnostics import DIAGNOSTIC_KEYS, diagnostic_snapshot
        await self.runtime.async_start()
        entities = {
            d.key: self.sensor.LearningStateSensor(self.entry, self.runtime, d)
            for d in self.sensor.STATE_SENSORS
        }
        snapshot_before, calls_before = self.runtime.snapshot(), list(self.calls)
        with patch.object(self.runtime.thermal_model, "predict_off_response", side_effect=AssertionError("prediction called")):
            sensor = entities["prediction_status"]
            reading = sensor.native_value
            attrs = sensor.extra_state_attributes
        self.assertNotIn("initial_learning_status", attrs)
        self.assertEqual(attrs["learning_status"], "idle")
        self.assertEqual({key: attrs[key] for key in DIAGNOSTIC_KEYS}, diagnostic_snapshot(self.runtime))
        self.assertIn(reading, sensor.entity_description.options)
        self.assertEqual(attrs["control_state"], self.runtime.decision.state)
        self.assertEqual(self.runtime.snapshot(), snapshot_before)
        self.assertEqual(self.calls, calls_before)
        self.assertEqual(len(entities), 1)
        self.assertEqual(sensor._attr_unique_id, "one_prediction_status")
        self.assertIsNone(sensor.entity_description.state_class)
        self.assertIsNone(sensor.entity_description.native_unit_of_measurement)

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
        from custom_components.adaptive_floor_heating.curve_learning import CurveStandards

        self.runtime = self.make_runtime(Settings(minimum_on_time=60, minimum_off_time=0))
        await self.runtime.async_start()
        self.write("sensor.room", "18")
        self.runtime.set_mode("auto")
        await self.settle()
        model = CurveStandards()
        model.load_runtime_samples([(20, 1), (30, 2.5), (40, 3)])
        self.runtime.curve_store = SimpleNamespace(model=model)
        now = self.hass.loop.time()
        self.write("sensor.room", "20.0")

        self.runtime.evaluate()
        await self.settle()
        self.assertTrue(self.runtime.actuator.observed)
        self.assertNotEqual(self.runtime.decision.state, "PREDICTIVE_OFF")

        self.runtime.actuator.changed_at = now - 31 * 60
        self.runtime.evaluate()
        await self.settle()
        self.assertEqual(self.calls[-1][1], "turn_off")
        self.assertFalse(self.runtime.actuator.observed)
        self.runtime.curve_store = None

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
