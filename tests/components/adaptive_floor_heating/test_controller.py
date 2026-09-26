"""Deterministic thermostat invariants; related: controller.py and storage.py."""

import unittest

from custom_components.adaptive_floor_heating.controller import Settings, ThermostatController, temperature_celsius
from custom_components.adaptive_floor_heating.storage import decode_state


class ControllerTests(unittest.TestCase):
    def model(self, temperature=22.5):
        model = ThermostatController(Settings())
        model.mode, model.target = "heat", 23.0
        model.startup_off_seen = True
        model.report_temperature(temperature, 0)
        return model

    def test_initial_off_and_startup_gate(self):
        model = ThermostatController(Settings())
        model.report_temperature(18, 0)
        self.assertFalse(model.decide(600, False, 0).heating)
        model.mode = "heat"
        self.assertEqual(model.decide(600, False, 0).state, "STARTUP")

    def test_minimum_off_exact_boundary(self):
        model = self.model()
        self.assertEqual(model.decide(599, False, 0).state, "WAIT_MIN_OFF")
        self.assertTrue(model.decide(600, False, 0).heating)

    def test_minimum_on_exact_boundary(self):
        model = self.model(23.5)
        model.report_temperature(23.5, 800)
        self.assertTrue(model.decide(899, True, 0).heating)
        self.assertFalse(model.decide(900, True, 0).heating)

    def test_default_start_and_stop_offsets_are_half_a_degree(self):
        model = ThermostatController(Settings(minimum_on_time=0, minimum_off_time=0))
        model.mode, model.target, model.startup_off_seen = "heat", 23.0, True
        model.report_temperature(22.5, 0)
        self.assertTrue(model.decide(1, False, 0).heating)
        model.report_temperature(23.49, 2)
        self.assertTrue(model.decide(3, True, 1).heating)
        model.report_temperature(23.5, 4)
        self.assertFalse(model.decide(5, True, 1).heating)

    def test_band_retains_observed_state(self):
        model = self.model(23.0)
        self.assertTrue(model.decide(600, True, 0).heating)
        self.assertFalse(model.decide(600, False, 0).heating)

    def test_off_and_overheat_override_minimum_on(self):
        model = self.model(22)
        model.set_mode("off", 1, True, 0)
        self.assertFalse(model.decide(1, True, 0).heating)
        model.mode = "heat"
        model.report_temperature(35, 2)
        self.assertFalse(model.decide(2, True, 0).heating)
        self.assertIn("overheat", model.faults)

    def test_stale_report_exact_boundary(self):
        model = self.model()
        self.assertTrue(model.sensor_ready(899))
        self.assertFalse(model.decide(900, True, 0).heating)
        self.assertTrue(model.sensor_fault)

    def test_unchanged_reports_are_fresh_but_reads_are_not(self):
        model = self.model()
        model.report_temperature(22.8, 800)
        self.assertTrue(model.sensor_ready(1000))
        self.assertFalse(model.sensor_ready(1700))

    def test_recovery_requires_two_spaced_reports(self):
        model = self.model()
        model.report_temperature(None, 1)
        model.report_temperature(22, 10)
        self.assertFalse(model.sensor_ready(39))
        model.report_temperature(22, 39)
        self.assertFalse(model.sensor_ready(39))
        model.report_temperature(22, 40)
        self.assertTrue(model.sensor_ready(40))

    def test_gap_resets_recovery(self):
        model = self.model()
        model.report_temperature(None, 1)
        model.report_temperature(22, 10)
        model.report_temperature(22, 910)
        self.assertFalse(model.sensor_ready(910))

    def test_sensor_recovery_does_not_clear_latches(self):
        model = self.model(35)
        model.report_temperature(None, 1)
        model.report_temperature(33, 10)
        model.report_temperature(33, 40)
        self.assertIn("overheat", model.faults)
        with self.assertRaises(ValueError):
            model.set_mode("heat", 40, False, 1)
        model.report_temperature(33, 601)
        model.set_mode("heat", 601, False, 1)
        self.assertFalse(model.faults)

    def test_target_change_preserves_mode_and_minimum_time(self):
        model = self.model(23)
        model.set_target(20)
        self.assertEqual(model.mode, "heat")
        self.assertEqual(model.decide(5, True, 0).state, "WAIT_MIN_ON")
        for value in (True, float("nan"), 4, 31, "23"):
            with self.assertRaises(ValueError):
                model.set_target(value)

    def test_temperature_conversion_and_invalid_values(self):
        self.assertEqual(temperature_celsius("68", "°F", "temperature"), 20)
        for value, unit, kind in (("NaN", "°C", "temperature"), ("inf", "°C", "temperature"),
                                  ("unavailable", "°C", "temperature"), ("20", "K", "temperature"),
                                  ("20", "°C", "humidity"), (True, "°C", "temperature")):
            self.assertIsNone(temperature_celsius(value, unit, kind))

    def test_settings_reject_invalid_values(self):
        defaults = Settings()
        self.assertEqual((defaults.home_temperature, defaults.away_temperature), (23.0, 18.0))
        self.assertEqual((defaults.cold_tolerance, defaults.hot_tolerance), (0.5, 0.5))
        configured = Settings.from_options({"home_temperature": 24.5, "away_temperature": 18.5})
        self.assertEqual((configured.home_temperature, configured.away_temperature), (24.5, 18.5))
        for data in ({"minimum_on_time": -1}, {"minimum_off_time": 1.5}, {"sensor_timeout": 0},
                     {"cold_tolerance": float("nan")}, {"hot_tolerance": True},
                     {"home_temperature": 17.9}, {"away_temperature": 30.1}):
            with self.assertRaises(ValueError):
                Settings.from_options(data)

    def test_saved_state_validation(self):
        valid = {"schema_version": 1, "mode": "heat", "target": 23, "faults": ["overheat"]}
        self.assertEqual(decode_state(valid)["mode"], "heat")
        self.assertEqual(decode_state(valid)["preset"], "home")
        self.assertEqual(decode_state({**valid, "mode": "auto"})["mode"], "auto")
        migrated = decode_state({**valid, "auto_control": True})
        self.assertEqual(migrated["mode"], "auto")
        self.assertEqual(
            decode_state({**valid, "target": 23, "preset_temperature": 23}, home_temperature=24)["target"],
            24,
        )
        self.assertEqual(
            decode_state({**valid, "target": 22, "preset_temperature": 23}, home_temperature=24)["target"],
            22,
        )
        for data in (None, {}, {**valid, "schema_version": 2}, {**valid, "target": float("nan")},
                     {**valid, "mode": "fan_only"}, {**valid, "faults": ["unknown_fault"]},
                     {**valid, "auto_control": 1}):
            with self.assertRaises(ValueError):
                decode_state(data)
