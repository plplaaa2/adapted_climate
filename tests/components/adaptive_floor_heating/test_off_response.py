"""Verify supported OFF-state predictions and uncertainty; related: off_response.py."""

import unittest

from custom_components.adaptive_floor_heating.off_response import predict_off, valid_profile


def profile(rise=0.6, duration=60, slope=0.8):
    return {"duration": duration, "slope": slope, "rise": rise,
            "points": [[0, 0], [5, rise * 0.1], [15, rise * 0.8], [30, rise]]}


class OffResponseTests(unittest.TestCase):
    def test_basic_learns_equivalent_time_and_scales_current_slope(self):
        prediction = predict_off([profile() for _ in range(8)], 60, 0.96, trajectory=False)
        self.assertAlmostEqual(prediction.rise, 0.72)
        self.assertAlmostEqual(prediction.peak_minutes, 30)
        self.assertEqual(prediction.confidence, 1)

    def test_curve_returns_measured_nonlinear_coast_not_on_continuation(self):
        prediction = predict_off([profile() for _ in range(3)], 60, 0.8, trajectory=True)
        self.assertAlmostEqual(prediction.rise, 0.6)
        self.assertAlmostEqual(dict(prediction.points)[5], 0.06)
        self.assertAlmostEqual(dict(prediction.points)[15], 0.48)

    def test_missing_and_unsupported_inputs_do_not_predict(self):
        for elapsed, slope in ((60, None), (60, 0), (60, -1), (60, float("nan")),
                               (180, 0.8), (60, 2.0)):
            self.assertIsNone(predict_off([profile()] * 8, elapsed, slope, trajectory=False))
        self.assertIsNone(predict_off([profile()] * 2, 60, 0.8, trajectory=True))

    def test_zero_response_and_corrupt_profile(self):
        zero = profile(rise=0)
        self.assertTrue(valid_profile(zero))
        self.assertEqual(predict_off([zero] * 8, 60, 0.8, trajectory=True).rise, 0)
        bad = profile()
        bad["points"][1][0] = 0
        self.assertFalse(valid_profile(bad))
        self.assertIsNone(predict_off([bad] * 8, 60, 0.8, trajectory=True))

    def test_high_variance_prevents_early_cutoff(self):
        profiles = [profile(rise=0.1), profile(rise=3), profile(rise=6)]
        self.assertIsNone(predict_off(profiles, 60, 0.8, trajectory=False))
