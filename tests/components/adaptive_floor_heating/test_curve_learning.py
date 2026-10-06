"""Verify five-minute curve contracts; related: curve_learning.py and curve_storage.py."""

import unittest

from custom_components.adaptive_floor_heating.curve_learning import (
    CurveResult, CurveStandards, CurveTracker,
)


class CurveLearningTests(unittest.TestCase):
    # Preserve real delay and dips in a completed short pulse; related: history.py, off_response.py.
    def test_short_pulse_keeps_initial_dip_until_sustained_post_peak_decline(self):
        tracker = CurveTracker()
        tracker.seed(False, 24, 0)
        tracker.switch(True, 0)
        for minute in (10, 20, 30):
            tracker.report_temperature(24, minute * 60)
        context = {"start_temperature": 24, "on_delta": 0, "pre_slope": -0.1, "off_minutes": 240}
        tracker.switch(False, 1800, off_context=context, off_slope=0)
        for minute, temperature in ((40, 23.9), (50, 23.9), (60, 24), (70, 24.2),
                                    (80, 24.4), (90, 24.6), (100, 24.7), (110, 24.7),
                                    (120, 24.6), (130, 24.7), (140, 24.7), (150, 24.6)):
            tracker.report_temperature(temperature, minute * 60)
            self.assertEqual(tracker.take_results(), [])
        tracker.report_temperature(24.6, 160 * 60)
        result = tracker.take_results()[0]
        self.assertEqual(result.peak_at, 140 * 60)
        self.assertAlmostEqual(result.residual_rise, 0.7)
        self.assertEqual(result.start_temperature, 24)
        self.assertEqual(result.off_temperature, 24)
        self.assertAlmostEqual(result.peak_temperature, 24.7)
        self.assertEqual(result.slope_at_off, 0)
        self.assertEqual(result.off_profile["context"], context)
        self.assertTrue(any(value < 0 for _, value in result.off_profile["points"]))
        self.assertTrue(CurveStandards().assess(result)[0])
        self.assertAlmostEqual(sum(result.buckets.values()), 0.7)
        self.assertAlmostEqual(sum(tracker.cycle.cooling_buckets.values()), -0.1)

    def test_unresponsive_pulse_times_out_without_valid_peak(self):
        tracker = CurveTracker()
        tracker.seed(False, 24, 0)
        tracker.switch(True, 0)
        tracker.switch(False, 1800)
        for minute in range(40, 210, 10):
            tracker.report_temperature(24 - minute * 0.002, minute * 60)
        tracker.advance(1800 + 10800)
        result = tracker.take_results()[0]
        self.assertEqual(result.end_reason, "PEAK_TIMEOUT")
        self.assertEqual(result.invalid_reason, "INCOMPLETE_PEAK")
        self.assertEqual(result.start_temperature, 24)
        self.assertEqual(result.off_temperature, 24)
        self.assertIsNone(result.peak_temperature)
        self.assertFalse(CurveStandards().assess(result)[0])

    def test_held_temperature_creates_real_zero_buckets(self):
        tracker = CurveTracker()
        tracker.seed(False, 20.0, 0)
        tracker.switch(True, 3600, start_reason="THRESHOLD_START")
        tracker.advance(3900)
        self.assertEqual(tracker.cycle.curve_type, "WARM_HEATING")
        self.assertEqual(tracker.cycle.start_reason, "THRESHOLD_START")
        self.assertEqual(tracker.cycle.heating_buckets[0], 0.0)
        tracker.report_temperature(20.2, 3901)
        tracker.advance(4200)
        self.assertEqual(tracker.cycle.heating_buckets[1], 0.2)

    def test_peak_and_three_hour_timeout_close_separate_curves(self):
        tracker = CurveTracker()
        tracker.seed(False, 20.0, 0)
        tracker.switch(True, 60, start_reason="PREDICTIVE_START")
        tracker.report_temperature(20.4, 360)
        tracker.switch(False, 660, off_reason="PREDICTIVE_STOP")
        tracker.report_temperature(20.6, 960)
        tracker.advance(1560)
        self.assertEqual(tracker.take_results(), [])
        tracker.report_temperature(20.4, 1860)
        tracker.report_temperature(20.4, 2460)
        heating = tracker.take_results()
        self.assertEqual(len(heating), 1)
        self.assertEqual(heating[0].curve_type, "PREDICTIVE_WARM_HEATING")
        self.assertAlmostEqual(heating[0].residual_rise, 0.2)
        tracker.advance(960 + 10800)
        cooling = tracker.take_results()
        self.assertEqual(len(cooling), 1)
        self.assertEqual(cooling[0].curve_type, "COOLING")
        self.assertEqual(cooling[0].end_reason, "THREE_HOUR_TIMEOUT")

    def test_unavailable_sensor_marks_segment_invalid(self):
        tracker = CurveTracker()
        tracker.seed(False, 20.0, 0)
        tracker.switch(True, 60)
        tracker.report_temperature(None, 360)
        tracker.switch(False, 660)
        tracker.close_incomplete(1260, "SENSOR_UNAVAILABLE")
        result = tracker.take_results()[0]
        self.assertEqual(result.invalid_reason, "SENSOR_UNAVAILABLE")

    def test_accepted_bucket_keeps_zeros_and_uses_eighty_twenty(self):
        model = CurveStandards()
        def result(cycle_id, value):
            return CurveResult(
                cycle_id, "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
                "PEAK_CONFIRMED", "BALANCED", 0, 600, 900, 1500,
                {0: 0.0, 1: value, 2: 0.0}, 0.2, None,
            )
        self.assertTrue(model.apply(result("one", 0.2))[0])
        self.assertTrue(model.apply(result("two", 0.3))[0])
        self.assertEqual(model.buckets["WARM_HEATING"][0], (0.0, 2))
        self.assertAlmostEqual(model.buckets["WARM_HEATING"][1][0], 0.22)

    def test_strong_divergence_is_not_learned(self):
        model = CurveStandards()
        model.buckets["COOLING"] = {i: (-0.1, 4) for i in range(4)}
        result = CurveResult(
            "window", "COOLING", "THRESHOLD_START", "TARGET_REACHED",
            "NEXT_ON", "BALANCED", 0, 600, 900, 2100,
            {i: -0.8 for i in range(4)}, None, None,
        )
        self.assertEqual(model.assess(result), (False, "CURVE_DEVIATION"))

    def test_predictive_estimate_falls_back_to_warm_until_three_samples(self):
        model = CurveStandards()
        model.buckets["WARM_HEATING"] = {0: (0.0, 3), 1: (0.1, 3)}
        model.residual["WARM_HEATING"] = (0.6, 3)
        model.residual["PREDICTIVE_WARM_HEATING"] = (0.9, 1)
        self.assertEqual(model.estimate("PREDICTIVE_WARM_HEATING", 1), (0.1, 3))
        self.assertAlmostEqual(model.residual_rise("PREDICTIVE_WARM_HEATING"), 0.15)

    def test_peak_excludes_later_held_buckets_from_heating_standard(self):
        tracker = CurveTracker()
        tracker.seed(False, 20.0, 0)
        tracker.switch(True, 60)
        tracker.switch(False, 660)
        tracker.report_temperature(20.2, 960)
        tracker.report_temperature(20.2, 1860)
        self.assertEqual(tracker.take_results(), [])
        tracker.report_temperature(20.1, 2160)
        tracker.report_temperature(20.1, 2760)
        result = tracker.take_results()[0]
        self.assertEqual(result.peak_at, 1860)
        self.assertTrue(all(60 + (index + 1) * 300 <= 1860 for index in result.buckets))

    def test_sustained_warming_ends_cooling_before_three_hour_limit(self):
        tracker = CurveTracker()
        tracker.seed(False, 20.0, 0)
        tracker.switch(True, 60)
        tracker.report_temperature(20.4, 360)
        tracker.switch(False, 660)
        tracker.report_temperature(20.6, 960)
        tracker.advance(1560)
        tracker.report_temperature(20.4, 1860)
        tracker.report_temperature(20.4, 2460)
        tracker.take_results()
        tracker.report_temperature(20.5, 2760)
        tracker.advance(3360)
        result = tracker.take_results()[0]
        self.assertEqual(result.end_reason, "SUSTAINED_WARMING")
        self.assertEqual(result.ended_at, 2760)

    def test_cold_requires_three_hours_away_and_has_predictive_precedence(self):
        for away_duration, expected in ((10799, "PREDICTIVE_WARM_HEATING"),
                                        (10800, "COLD_HEATING"), (14400, "COLD_HEATING")):
            with self.subTest(duration=away_duration):
                tracker = CurveTracker()
                tracker.seed(False, 20, 0)
                tracker.set_preset("away", 1)
                tracker.set_preset("away", 200)  # Repeated command must not reset the clock.
                tracker.set_preset("home", 1 + away_duration)
                tracker.switch(True, 2 + away_duration, start_reason="PREDICTIVE_START")
                self.assertEqual(tracker.cycle.curve_type, expected)

    def test_long_home_off_is_warm_and_away_cycles_are_excluded(self):
        tracker = CurveTracker()
        tracker.seed(False, 20, 0)
        tracker.switch(True, 14400)
        self.assertEqual(tracker.cycle.curve_type, "WARM_HEATING")
        tracker.set_preset("away", 15000)
        tracker.switch(False, 15600)
        tracker.switch(True, 16000)
        tracker.switch(False, 16600)
        tracker.report_temperature(19.9, 16900)
        tracker.close_incomplete(17500, "TEST_END")
        result = tracker.take_results()[-1]
        self.assertEqual(result.preset, "away")
        self.assertFalse(CurveStandards().assess(result)[0])

    def test_incomplete_cold_keeps_return_flag(self):
        tracker = CurveTracker()
        tracker.seed(False, 20, 0)
        tracker.set_preset("away", 0)
        tracker.set_preset("home", 10800)
        tracker.switch(True, 10801)
        tracker.close_incomplete(12000, "RELOAD")
        self.assertTrue(tracker.away_return_pending)

    def test_cooling_prediction_requires_continuous_bucket_coverage(self):
        model = CurveStandards()
        model.buckets["COOLING"] = {0: (-0.1, 3), 1: (-0.2, 3)}
        self.assertAlmostEqual(model.predict_cooling_delta(2.5, 5), -0.15)
        self.assertIsNone(model.predict_cooling_delta(2.5, 10))

    def test_decline_is_not_backdated_into_cooling_buckets(self):
        tracker = CurveTracker()
        tracker.seed(False, 20, 0)
        tracker.switch(True, 60)
        tracker.report_temperature(20.2, 360)
        tracker.switch(False, 660)
        tracker.report_temperature(20.4, 960)
        tracker.report_temperature(20.2, 1561)
        tracker.report_temperature(20.2, 2161)
        self.assertEqual(tracker.cycle.peak_at, 960)
        self.assertEqual(tracker.cycle.cooling_buckets[0], 0)
        self.assertEqual(tracker.cycle.cooling_buckets[1], 0)
        tracker.advance(2460)
        self.assertAlmostEqual(tracker.cycle.cooling_buckets[2], -0.2)


if __name__ == "__main__":
    unittest.main()
