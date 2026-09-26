"""Observation-only thermal calculations; related: history.py and runtime.py."""

import unittest

from custom_components.adaptive_floor_heating.history import (
    HeatLossObservation, TemperatureHistory, ThermalObservation,
)


class TemperatureHistoryTests(unittest.TestCase):
    def test_regression_requires_three_reports_and_ten_minutes(self):
        history = TemperatureHistory()
        history.add(0, 20)
        history.add(300, 20.1)
        self.assertIsNone(history.slope(300))
        history.add(700, 20.2)
        self.assertAlmostEqual(history.slope(700), 1.0216216)

    def test_rejects_out_of_order_and_prunes_by_age_and_count(self):
        history = TemperatureHistory(retention=100, max_points=3)
        self.assertTrue(history.add(0, 20))
        self.assertFalse(history.add(-1, 21))
        for timestamp in (10, 20, 30):
            history.add(timestamp, 20)
        self.assertEqual(len(history.samples), 3)
        history.add(200, 20)
        self.assertEqual([sample.timestamp for sample in history.samples], [200])


class ThermalObservationTests(unittest.TestCase):
    def test_heating_response_and_post_off_peak_are_observed(self):
        observation = ThermalObservation()
        observation.seed_heater(False)
        observation.report_temperature(20.0, 0)
        observation.observe_heater(True, 0, 20.0)
        for timestamp, temperature in ((600, 20.05), (1200, 20.2), (1800, 20.4)):
            observation.report_temperature(temperature, timestamp)
        observation.observe_heater(False, 1800, 20.4)
        for timestamp, temperature in (
            (2400, 20.8), (3000, 21.0), (3600, 20.95), (4200, 20.8),
            (4800, 20.6), (5400, 20.4),
        ):
            observation.report_temperature(temperature, timestamp)
        self.assertEqual(observation.completed_cycles, 1)
        self.assertAlmostEqual(observation.last_cycle.response_delay_minutes, 20)
        self.assertIsNotNone(observation.last_cycle.heating_rate_c_per_hour)
        self.assertAlmostEqual(observation.last_cycle.residual_rise, 0.6)
        self.assertAlmostEqual(observation.last_cycle.peak_delay_minutes, 20)

    def test_startup_mid_cycle_and_sensor_loss_do_not_complete_partial_cycle(self):
        observation = ThermalObservation()
        observation.seed_heater(True)
        observation.observe_heater(False, 100, 20)
        self.assertIsNone(observation.off_at)
        observation.report_temperature(20, 200)
        observation.report_temperature(None, 300)
        self.assertEqual(observation.history.samples, ())
        self.assertEqual(observation.completed_cycles, 0)

    def test_manual_switch_transition_invalidates_partial_cycle(self):
        observation = ThermalObservation()
        observation.seed_heater(False)
        observation.report_temperature(20, 0)
        observation.observe_heater(True, 0, 20)
        observation.report_temperature(20.2, 1200)
        observation.invalidate_cycle(False)
        self.assertIsNone(observation.heating_started)
        self.assertIsNone(observation.off_at)
        self.assertEqual(observation.completed_cycles, 0)


class HeatLossObservationTests(unittest.TestCase):
    def test_learning_waits_for_confirmed_heating_and_post_peak_cooling(self):
        observation = HeatLossObservation()
        observation.seed_heater(False)
        self.assertIsNone(observation.report_temperature(22, 10, 0))
        observation.observe_heater(True, 0)
        observation.observe_heater(False, 900)
        observation.report_temperature(22, 10, 900)
        observation.report_temperature(21.9, 10, 1500)
        observation.report_temperature(21.8, 10, 2100)
        rate = observation.report_temperature(21.7, 10, 2700)
        self.assertAlmostEqual(rate, 0.6 / 11.75)
        self.assertIsNone(observation.report_temperature(21.6, 10, 3300))

    def test_requires_outdoor_delta_and_a_stable_cooling_slope(self):
        observation = HeatLossObservation()
        observation.seed_heater(False)
        observation.observe_heater(True, 0)
        observation.observe_heater(False, 0)
        observation.report_temperature(22, None, 0)
        for timestamp, temperature in ((900, 21.9), (1500, 21.8), (2100, 21.7), (2700, 21.6)):
            self.assertIsNone(observation.report_temperature(temperature, 20.8, timestamp))
        self.assertIsNone(observation.report_temperature(21.5, 10, 3300))

    def test_hot_supply_filters_samples_until_water_cools_to_room(self):
        observation = HeatLossObservation(use_supply_sensor=True)
        observation.seed_heater(False)
        observation.observe_heater(True, 0)
        observation.observe_heater(False, 0)
        observation.report_temperature(22, 10, 0, supply_temperature=22)
        for timestamp, temperature in ((900, 21.9), (1500, 21.8), (2100, 21.7)):
            self.assertIsNone(observation.report_temperature(
                temperature, 10, timestamp, supply_temperature=23
            ))
        observation.report_temperature(21.6, 10, 2700, supply_temperature=21.6)
        observation.report_temperature(21.5, 10, 3300, supply_temperature=21.5)
        rate = observation.report_temperature(21.4, 10, 3900, supply_temperature=21.4)
        self.assertGreater(rate, 0)

    def test_hot_return_filters_samples_until_water_cools_to_room(self):
        observation = HeatLossObservation(use_return_sensor=True)
        observation.seed_heater(False)
        observation.observe_heater(True, 0)
        observation.observe_heater(False, 0)
        observation.report_temperature(22, 10, 0, return_temperature=22)
        for timestamp, temperature in ((900, 21.9), (1500, 21.8), (2100, 21.7)):
            self.assertIsNone(observation.report_temperature(
                temperature, 10, timestamp, return_temperature=23
            ))
        observation.report_temperature(21.6, 10, 2700, return_temperature=21.6)
        observation.report_temperature(21.5, 10, 3300, return_temperature=21.5)
        rate = observation.report_temperature(21.4, 10, 3900, return_temperature=21.4)
        self.assertGreater(rate, 0)

if __name__ == "__main__":
    unittest.main()
