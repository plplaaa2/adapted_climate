"""Observation-only thermal calculations; related: history.py and runtime.py."""

import unittest

from custom_components.adaptive_floor_heating.history import (
    HeatLossObservation, PeakTracker, TemperatureHistory, ThermalObservation,
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


class PeakTrackerTests(unittest.TestCase):
    # Validate genuine consecutive falling reports, not timers or repeated cached values; related: curve_learning.py.
    def test_jitter_and_repeated_low_values_wait_for_additional_drop(self):
        tracker = PeakTracker(0, 0, 26.6, 26.0, True)
        for at, temperature in ((1,26.5),(2,26.4),(3,26.4),(4,26.4)):
            self.assertFalse(tracker.report(temperature, at))
        self.assertTrue(tracker.report(26.3, 5))
        self.assertEqual(tracker.peak_temperature, 26.6)
        self.assertEqual(tracker.peak_at, 0)
        self.assertAlmostEqual(tracker.confirmation_extra_drop, .1)

    def test_duplicate_and_out_of_order_reports_cannot_confirm(self):
        tracker = PeakTracker(0, 0, 26.6, 26.0, True)
        self.assertFalse(tracker.report(26.3, 10))
        self.assertFalse(tracker.report(26.2, 10))
        self.assertFalse(tracker.report(26.2, 9))
        self.assertTrue(tracker.report(26.2, 11))

    def test_rebound_above_threshold_and_new_peak_reset_confirmation(self):
        tracker = PeakTracker(0, 0, 26.6, 26.0, True)
        for at, temperature in ((1,26.4),(2,26.5),(3,26.4),(4,26.7),(5,26.5)):
            self.assertFalse(tracker.report(temperature, at))
        self.assertTrue(tracker.report(26.4, 6))
        self.assertEqual(tracker.peak_at, 4)
        self.assertEqual(tracker.peak_temperature, 26.7)

    def test_two_small_falls_do_not_accumulate_into_next_report_threshold(self):
        tracker = PeakTracker(0, 0, 26.6, 26.0, True)
        self.assertFalse(tracker.report(26.3, 1))
        self.assertFalse(tracker.report(26.25, 2))
        self.assertFalse(tracker.report(26.2, 3))
        self.assertTrue(tracker.report(26.1, 4))

    def test_invalid_report_and_unobserved_response_cannot_confirm(self):
        tracker = PeakTracker(0, 0, 26.6, 26.6, False)
        self.assertFalse(tracker.report(26.3, 60))
        self.assertFalse(tracker.report(26.2, 120))
        tracker.responded = True
        self.assertFalse(tracker.report(26.3, 180))
        self.assertFalse(tracker.report(float("nan"), 181))
        self.assertFalse(tracker.report(26.2, 182))
        self.assertTrue(tracker.report(26.1, 183))


class ThermalObservationTests(unittest.TestCase):
    def test_immediate_cooling_after_observed_on_response_is_a_valid_zero_coast(self):
        observation = ThermalObservation()
        observation.seed_heater(False)
        observation.observe_heater(True, 0, 24)
        for minute, temperature in ((10, 24.1), (20, 24.2), (30, 24.3)):
            observation.report_temperature(temperature, minute * 60)
        observation.observe_heater(False, 1800, 24.3)
        observation.report_temperature(24.0, 2400)
        observation.report_temperature(23.9, 3000)
        self.assertEqual(observation.completed_cycles, 1)
        self.assertEqual(observation.last_cycle.residual_rise, 0)
        self.assertEqual(observation.last_cycle.peak_delay_minutes, 0)

    # A short pulse can respond only after OFF; related: thermal_model.py, curve_learning.py.
    def test_thirty_minute_pulse_tracks_delayed_rise_and_ignores_single_reversal(self):
        observation = ThermalObservation()
        observation.seed_heater(False)
        for minute, temperature in ((-20, 24.1), (-10, 24.05), (0, 24)):
            observation.report_temperature(temperature, minute * 60)
        observation.observe_heater(True, 0, 24)
        for minute in (10, 20, 30):
            observation.report_temperature(24, minute * 60)
        observation.observe_heater(False, 1800, 24)
        for minute, temperature in ((40, 23.9), (50, 23.9), (60, 24), (70, 24.2),
                                    (80, 24.4), (90, 24.6), (100, 24.7), (110, 24.7),
                                    (120, 24.6), (130, 24.7), (140, 24.7), (150, 24.6)):
            observation.report_temperature(temperature, minute * 60)
            self.assertEqual(observation.completed_cycles, 0)
        observation.report_temperature(24.4, 160 * 60)
        self.assertEqual(observation.completed_cycles, 0)
        observation.report_temperature(24.3, 170 * 60)
        result = observation.last_cycle
        self.assertEqual(result.response_delay_minutes, 60)
        self.assertAlmostEqual(result.residual_rise, 0.7)
        self.assertEqual(result.peak_delay_minutes, 110)
        self.assertEqual(result.off_context["start_temperature"], 24)
        self.assertEqual(result.off_context["on_delta"], 0)
        self.assertLess(result.off_context["pre_slope"], 0)
        self.assertLess(result.coast_curve[1][1], 0)

    def test_no_response_timeout_does_not_learn_initial_cooling_as_peak(self):
        observation = ThermalObservation()
        observation.seed_heater(False)
        observation.observe_heater(True, 0, 24)
        for minute in (10, 20, 30):
            observation.report_temperature(24, minute * 60)
        observation.observe_heater(False, 1800, 24)
        for minute in range(40, 401, 10):
            observation.report_temperature(24 - (minute - 30) * 0.002, minute * 60)
        self.assertEqual(observation.completed_cycles, 0)
        self.assertIsNone(observation.off_at)

    # Predictive slopes use held grids without changing actual reports; related: history.py, runtime.py.
    def test_predictive_slope_flattens_without_new_reports(self):
        observation = ThermalObservation()
        for timestamp in range(0, 2701, 300):
            observation.report_temperature(20 + timestamp / 3600, timestamp)
        self.assertAlmostEqual(observation.temperature_slope, 1)
        before = observation.history.samples
        observation.advance_time(5400)
        self.assertEqual(observation.temperature_slope, 0)
        self.assertEqual(observation.history.samples, before)
        self.assertEqual(observation.completed_cycles, 0)

    def test_predictive_slope_ignores_evidence_outside_45_minutes(self):
        observation = ThermalObservation()
        observation.report_temperature(35, 0)
        for timestamp in (900, 1800, 2700, 3600, 4500):
            observation.report_temperature(20 - timestamp / 3600 * 0.2, timestamp)
        # Hand-calculated regression of held grid values: 19.9 x3, 19.85 x3, 19.8 x3, 19.75.
        self.assertAlmostEqual(observation.temperature_slope, -0.19636363636)

    def test_report_cadence_does_not_switch_predictive_regression_modes(self):
        observation = ThermalObservation()
        for timestamp in (0, 300, 600, 900, 1200, 1500):
            observation.report_temperature(20 + timestamp / 3600, timestamp)
        self.assertAlmostEqual(observation.temperature_slope, 1)
        observation.advance_time(1800)
        before_late_report = observation.temperature_slope
        observation.report_temperature(20.5, 1801)
        self.assertGreater(observation.temperature_slope, before_late_report)
        self.assertLessEqual(observation.temperature_slope, 1.01)
        observation.advance_time(2100)
        self.assertIsNotNone(observation.temperature_slope)
        self.assertEqual(len(observation.history.samples), 7)

    def test_predictive_grid_is_independent_of_wall_or_monotonic_epoch(self):
        first, shifted = ThermalObservation(), ThermalObservation()
        offset = 1790000123.25
        for timestamp, temperature in ((0, 20), (610, 20.1), (1500, 20.2)):
            first.report_temperature(temperature, timestamp)
            shifted.report_temperature(temperature, timestamp + offset)
        first.advance_time(2400)
        shifted.advance_time(2400 + offset)
        self.assertAlmostEqual(first.temperature_slope, shifted.temperature_slope)

    def test_held_grid_needs_ten_minutes_and_resets_on_invalid_input(self):
        observation = ThermalObservation()
        observation.report_temperature(20, 0)
        observation.advance_time(599)
        self.assertIsNone(observation.temperature_slope)
        observation.advance_time(600)
        self.assertEqual(observation.temperature_slope, 0)
        observation.report_temperature(None, 2500)
        observation.report_temperature(20, 3600)
        self.assertIsNone(observation.temperature_slope)
        observation.advance_time(4200)
        self.assertEqual(observation.temperature_slope, 0)

    def test_display_slope_holds_last_value_but_learning_slope_stays_unavailable(self):
        observation = ThermalObservation()
        for timestamp in (0, 600, 1200):
            observation.report_temperature(20 + timestamp / 3600, timestamp)
        self.assertAlmostEqual(observation.display_temperature_slope, 1)
        observation.history.clear()
        observation.report_temperature(20.4, 1200.1)
        self.assertIsNone(observation.temperature_slope)
        self.assertAlmostEqual(observation.display_temperature_slope, 1)
        observation.report_temperature(None, 1202)
        self.assertIsNone(observation.temperature_slope)
        self.assertAlmostEqual(observation.display_temperature_slope, 1)

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
        self.assertEqual(observation.last_cycle.heating_duration_minutes, 30)
        self.assertEqual(observation.last_cycle.response_curve[0], (0.0, 0.0))
        self.assertAlmostEqual(observation.last_cycle.response_curve[-1][1], 0.4)
        self.assertEqual(observation.last_cycle.coast_curve[0], (0.0, 0.0))
        self.assertEqual(observation.last_cycle.coast_curve[-1], (20.0, 1.0))

    def test_curvature_uses_disjoint_windows_and_resets_on_sensor_loss(self):
        observation = ThermalObservation()
        observation.seed_heater(False)
        observation.observe_heater(True, 0, 20)
        for minute in range(10, 100, 10):
            rise = (minute / 60) ** 2 * 0.4
            observation.report_temperature(20 + rise, minute * 60)
        self.assertGreater(observation.temperature_curvature, 0)
        self.assertIsNotNone(observation.heating_profile(90 * 60, 15 * 60))
        observation.report_temperature(None, 91 * 60)
        self.assertIsNone(observation.temperature_curvature)
        self.assertIsNone(observation.heating_profile(91 * 60, 15 * 60))

    def test_peak_is_last_plateau_report_and_requires_real_decline(self):
        observation = ThermalObservation()
        observation.seed_heater(False)
        observation.observe_heater(True, 0, 20)
        for timestamp, temperature in ((600, 20.1), (1200, 20.2), (1800, 20.3)):
            observation.report_temperature(temperature, timestamp)
        observation.observe_heater(False, 1800, 20.3)
        observation.report_temperature(20.6, 2100)
        observation.report_temperature(20.6, 2700)
        observation.report_temperature(20.6, 3300)
        self.assertEqual(observation.completed_cycles, 0)
        observation.report_temperature(20.3, 3600)
        self.assertEqual(observation.completed_cycles, 0)
        observation.report_temperature(20.2, 4200)
        self.assertEqual(observation.completed_cycles, 1)
        self.assertEqual(observation.last_cycle.peak_temperature, 20.6)
        self.assertEqual(observation.last_cycle.peak_delay_minutes, 25)

    # Held valid state preserves ON/coast continuity without fabricating reports; related: history.py.
    def test_report_gap_preserves_cycle_and_profile_without_extra_samples(self):
        observation = ThermalObservation(max_report_gap=900)
        observation.seed_heater(False)
        observation.observe_heater(True, 0, 20)
        observation.report_temperature(20.1, 600)
        before = observation.history.samples
        profile = observation.heating_profile(1799, 900)
        self.assertEqual(profile[0], 1799 / 60)
        self.assertAlmostEqual(profile[1], .1)
        self.assertEqual(observation.history.samples, before)
        observation.report_temperature(20.3, 1800)
        self.assertEqual(observation.heating_started, 0)
        observation.observe_heater(False, 2400, 20.4)
        self.assertEqual(observation.off_at, 2400)
        observation.report_temperature(20.7, 4800)
        self.assertEqual(observation.completed_cycles, 0)
        observation.report_temperature(20.4, 7200)
        self.assertEqual(observation.completed_cycles, 0)
        observation.report_temperature(20.3, 9600)
        self.assertEqual(observation.completed_cycles, 1)
        self.assertAlmostEqual(observation.last_cycle.residual_rise, .3)

    def test_long_report_gap_keeps_peak_timeout_and_invalid_state_gates(self):
        observation = ThermalObservation(max_report_gap=900, max_peak_wait_seconds=3600)
        observation.seed_heater(False)
        observation.observe_heater(True, 0, 20)
        observation.report_temperature(20.3, 1200)
        observation.observe_heater(False, 1800, 20.3)
        observation.report_temperature(20.6, 5401)
        self.assertIsNone(observation.off_at)
        self.assertEqual(observation.completed_cycles, 0)
        observation.observe_heater(True, 6000, 20.6)
        observation.report_temperature(None, 8000)
        self.assertIsNone(observation.heating_profile(9000, 900))
        observation.report_temperature(20.6, 9500)
        self.assertIsNone(observation.heating_started)

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

if __name__ == "__main__":
    unittest.main()
