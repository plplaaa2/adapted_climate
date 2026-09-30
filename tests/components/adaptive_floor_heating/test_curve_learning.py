"""Verify five-minute curve contracts; related: curve_learning.py and curve_storage.py."""

import unittest

from custom_components.adaptive_floor_heating.curve_learning import (
    CurveResult, CurveStandards, CurveTracker,
)


class CurveLearningTests(unittest.TestCase):
    def test_held_temperature_creates_real_zero_buckets(self):
        tracker = CurveTracker()
        tracker.seed(False, 20.0, 0)
        tracker.switch(True, 3600, start_reason="THRESHOLD_START")
        tracker.advance(3900)
        self.assertEqual(tracker.cycle.curve_type, "COLD_HEATING")
        self.assertEqual(tracker.cycle.start_reason, "COLD_START")
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
        heating = tracker.take_results()
        self.assertEqual(len(heating), 1)
        self.assertEqual(heating[0].curve_type, "PREDICTIVE_WARM_HEATING")
        self.assertAlmostEqual(heating[0].residual_rise, 0.2)
        tracker.report_temperature(20.4, 1860)
        tracker.advance(660 + 10800)
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
        tracker.advance(1260)
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
        result = tracker.take_results()[0]
        self.assertEqual(result.peak_at, 960)
        self.assertTrue(all(60 + (index + 1) * 300 <= 960 for index in result.buckets))

    def test_sustained_warming_ends_cooling_before_three_hour_limit(self):
        tracker = CurveTracker()
        tracker.seed(False, 20.0, 0)
        tracker.switch(True, 60)
        tracker.report_temperature(20.4, 360)
        tracker.switch(False, 660)
        tracker.report_temperature(20.6, 960)
        tracker.advance(1560)
        tracker.take_results()
        tracker.report_temperature(20.4, 1860)
        tracker.report_temperature(20.5, 2160)
        tracker.advance(2760)
        result = tracker.take_results()[0]
        self.assertEqual(result.end_reason, "SUSTAINED_WARMING")
        self.assertEqual(result.ended_at, 2160)


if __name__ == "__main__":
    unittest.main()
