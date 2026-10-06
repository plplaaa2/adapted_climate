"""Verify supported OFF-state predictions and uncertainty; related: off_response.py."""

import unittest

from custom_components.adaptive_floor_heating.off_response import predict_off, valid_profile


def profile(rise=0.6, duration=60, slope=0.8):
    return {"duration": duration, "slope": slope, "rise": rise,
            "points": [[0, 0], [5, rise * 0.1], [15, rise * 0.8], [30, rise]]}


class OffResponseTests(unittest.TestCase):
    def test_off_profile_four_hour_boundary_remains_bounded(self):
        late = {**profile(), "points": [[0,0],[240,.6]]}
        self.assertTrue(valid_profile(late))
        self.assertFalse(valid_profile({**late, "points": [[0,0],[240.01,.6]]}))
    # Exercise delayed thermal states, not just slope scaling; related: history.py, runtime.py.
    def test_delayed_response_uses_observed_rise_and_preserves_initial_dip(self):
        context = {"start_temperature": 24, "on_delta": 0, "pre_slope": -0.1, "off_minutes": 240}
        delayed = {"duration": 30, "slope": 0, "rise": 0.7,
                   "points": [[0, 0], [10, -0.1], [30, 0], [100, 0.7]], "context": context}
        self.assertTrue(valid_profile(delayed))
        for slope in (0, -0.1, 0.05):
            basic = predict_off([delayed] * 8, 30, slope, trajectory=False, context=context)
            curve = predict_off([delayed] * 8, 30, slope, trajectory=True, context=context)
            self.assertAlmostEqual(basic.rise, 0.7)
            self.assertAlmostEqual(curve.rise, 0.7)
            self.assertAlmostEqual(dict(curve.points)[10], -0.1)
            self.assertEqual(curve.peak_minutes, 100)
        self.assertIsNone(predict_off([delayed] * 2, 30, 0, trajectory=False, context=context))
        self.assertIsNone(predict_off([delayed] * 8, 15, 0, trajectory=False, context=context))
        self.assertIsNone(predict_off([delayed] * 8, 30, 0.5, trajectory=False, context=context))

    def test_delayed_matching_requires_comparable_observed_thermal_context(self):
        context = {"start_temperature": 24, "on_delta": 0, "pre_slope": -0.1, "off_minutes": 240}
        delayed = {**profile(duration=30, slope=0), "context": context}
        self.assertIsNone(predict_off([delayed] * 8, 30, 0, trajectory=False))
        for key, value in (("start_temperature", 20), ("on_delta", 0.8),
                           ("pre_slope", 0.5), ("off_minutes", 10), ("pre_slope", None)):
            current = {**context, key: value}
            self.assertIsNone(predict_off([delayed] * 8, 30, 0, trajectory=False, context=current))
        legacy = profile(duration=30, slope=0)
        self.assertIsNone(predict_off([legacy] * 8, 30, 0, trajectory=False, context=context))

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
