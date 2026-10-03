"""Verify independent stable memory, seasonal fallback and corruption; related: curve_memory.py, curve_storage.py."""

from copy import deepcopy
import unittest

from custom_components.adaptive_floor_heating.curve_memory import (
    Bucket, CurveMemory, CURRENT_MAX_AGE, LONG_TERM_ALPHA, blended,
)


def profile(rise=0.6):
    return {"duration": 60, "slope": 0.8, "rise": rise,
            "points": [[0, 0], [5, rise * 0.1], [15, rise * 0.8], [30, rise]]}


class CurveMemoryTests(unittest.TestCase):
    def test_observed_zero_coast_at_off_survives_memory_and_prediction(self):
        zero = {"duration": 30, "slope": 0.8, "rise": 0, "points": [[0, 0]]}
        memory = CurveMemory()
        for i in range(8):
            memory.learn_response(zero, 100 + i)
        restored = CurveMemory.restore(memory.dump())
        prediction = restored.predict_response(30, 0.8, 108)
        self.assertEqual(prediction.rise, 0)
        self.assertEqual(prediction.peak_minutes, 0)
        self.assertEqual(prediction.points, ((0.0, 0.0),))

    # Delayed memory must remain usable after a season, with thermal state preserved; related: curve_storage.py.
    def test_delayed_long_term_preserves_dip_and_context_after_reopen_and_season(self):
        context = {"start_temperature": 24, "on_delta": 0, "pre_slope": -0.1, "off_minutes": 240}
        delayed = {"duration": 30, "slope": 0, "rise": 0.7,
                   "points": [[0, 0], [10, -0.1], [30, 0], [100, 0.7]], "context": context}
        memory = CurveMemory()
        for i in range(8):
            memory.learn_response(delayed, 100 + i)
        memory = CurveMemory.restore(memory.dump())
        prediction = memory.predict_response(30, 0, 108 + 2 * CURRENT_MAX_AGE, context=context)
        self.assertIsNotNone(prediction)
        self.assertEqual(memory.last_source, "long_term")
        self.assertAlmostEqual(prediction.rise, 0.7)
        self.assertAlmostEqual(dict(prediction.points)[10], -0.1)
        self.assertIsNone(memory.predict_response(15, 0, 108, context=context))
        self.assertIsNone(memory.predict_response(30, 0, 108, context={**context, "on_delta": 1}))
        self.assertIsNone(memory.predict_response(30, 0, 108))
        next(iter(memory.responses.values()))["current"] = {}
        restored = CurveMemory.restore(memory.dump())
        self.assertAlmostEqual(restored.predict_response(30, 0, 108, context=context).rise, 0.7)
        self.assertEqual(restored.last_source, "long_term")

    def test_current_is_eighty_twenty_and_variance_is_measured(self):
        memory = CurveMemory()
        memory.learn_buckets({0: 0, 1: 0.2}, 100)
        memory.learn_buckets({0: 0, 1: 0.3}, 200)
        self.assertAlmostEqual(memory.current[1].mean, 0.22)
        self.assertGreater(memory.current[1].variance, 0)
        self.assertEqual(memory.long_term, {})

    def test_only_stable_covered_current_promotes(self):
        memory = CurveMemory()
        for i in range(5):
            memory.learn_buckets({0: 0, 1: 0.1, 2: 0.2}, 100 + i)
        self.assertEqual(memory.long_term, {})
        memory.learn_buckets({0: 0, 1: 0.1, 2: 0.2}, 106)
        self.assertEqual(set(memory.long_term), {0, 1, 2})
        self.assertEqual(memory.long_term[1].promotions, 1)
        self.assertEqual(memory.long_term[1].samples, 6)
        old = memory.long_term[1].mean
        memory.learn_buckets({0: 0, 1: 0.11, 2: 0.2}, 107)
        self.assertAlmostEqual(memory.long_term[1].mean,
                               old * (1 - LONG_TERM_ALPHA) + memory.current[1].mean * LONG_TERM_ALPHA)
        self.assertEqual(memory.long_term[1].samples, 7)

    def test_gap_or_high_variance_blocks_long_term(self):
        for values in ({0: 0, 2: 0.1}, {0: 0, 1: None}):
            memory = CurveMemory()
            for i in range(12):
                sample = dict(values)
                if sample.get(1, 0) is None:
                    sample[1] = 0.8 if i % 2 else -0.8
                memory.learn_buckets(sample, 100 + i)
            self.assertEqual(memory.long_term, {})

    def test_current_age_decays_but_long_term_survives_non_heating_season(self):
        memory = CurveMemory()
        for i in range(8):
            memory.learn_buckets({0: 0, 1: -0.1}, 100 + i)
        future = 107 + CURRENT_MAX_AGE * 4
        prediction = memory.estimate(1, future)
        self.assertAlmostEqual(prediction[0], -0.1)
        self.assertEqual(prediction[2], "long_term")
        self.assertEqual(prediction[3], 0)
        memory.current.clear()
        self.assertEqual(memory.estimate(1, future)[2], "long_term")

    def test_confidence_blend_and_unsupported_missing_bucket(self):
        current = Bucket(0.2, 0, 4, 100)
        long_term = Bucket(0.1, 0, 8, 10, 1)
        estimate = blended(current, long_term, 100)
        self.assertAlmostEqual(estimate[0], 0.15)
        self.assertEqual(estimate[2:], ("blended", 0.5))
        memory = CurveMemory()
        self.assertIsNone(memory.estimate(0, 100))

    def test_migrated_unknown_variance_never_promotes_or_claims_confidence(self):
        bucket = Bucket(0.2)
        self.assertEqual(bucket.confidence(100), 0)
        bucket.update(0.3, 100)
        self.assertAlmostEqual(bucket.mean, 0.22)
        self.assertEqual(bucket.samples, 1)
        self.assertFalse(bucket.stable(100))

    def test_off_long_term_uses_stable_current_and_retains_nonlinear_trajectory(self):
        memory = CurveMemory()
        for i in range(6):
            memory.learn_response(profile(), 100 + i)
        group = next(iter(memory.responses.values()))
        self.assertEqual(group["long_term"]["20"].promotions, 1)
        initial = group["long_term"]["20"].mean
        memory.learn_response(profile(0.65), 107)
        self.assertAlmostEqual(group["long_term"]["20"].mean,
                               initial * 0.98 + group["current"]["20"].mean * 0.02)
        future = 107 + CURRENT_MAX_AGE * 4
        prediction = memory.predict_response(60, 0.8, future)
        self.assertIsNotNone(prediction)
        self.assertEqual(memory.last_source, "long_term")
        self.assertAlmostEqual(prediction.rise, group["long_term"]["20"].mean)
        self.assertLess(prediction.points[3][1], prediction.rise * 0.3)
        group["current"].clear()
        restored = CurveMemory.restore(memory.dump())
        self.assertIsNotNone(restored.predict_response(60, 0.8, future))
        self.assertEqual(restored.last_source, "long_term")

    def test_off_operating_conditions_and_curve_types_do_not_mix(self):
        memory = CurveMemory()
        for i in range(8):
            memory.learn_response(profile(), 100 + i)
        self.assertIsNone(memory.predict_response(180, 0.8, 108))
        self.assertIsNone(memory.predict_response(60, 2, 108))
        other = CurveMemory()
        self.assertIsNone(other.predict_response(60, 0.8, 108))

    def test_round_trip_and_invalid_long_term_are_detected(self):
        memory = CurveMemory()
        for i in range(8):
            memory.learn_buckets({0: 0, 1: 0.1}, 100 + i)
            memory.learn_response(profile(), 100 + i)
        data = memory.dump()
        self.assertEqual(CurveMemory.restore(data).dump(), data)
        damaged = deepcopy(data)
        damaged["long_term"]["0"][1] = float("nan")
        with self.assertRaises(ValueError):
            CurveMemory.restore(damaged)
        damaged = deepcopy(data)
        next(iter(damaged["responses"].values()))["long_term"]["20"][0] = 100
        with self.assertRaises(ValueError):
            CurveMemory.restore(damaged)

    def test_short_cycles_do_not_repromote_untouched_tail(self):
        memory = CurveMemory()
        for i in range(6):
            memory.learn_buckets({0: 0, 1: 0.1, 2: 0.2}, 100 + i)
        tail = memory.long_term[2].dump()
        memory.learn_buckets({0: 0, 1: 0.1}, 107)
        self.assertEqual(memory.long_term[2].dump(), tail)

    def test_off_blend_uses_one_weight_to_avoid_an_artificial_peak(self):
        memory = CurveMemory()
        for i in range(8):
            memory.learn_response(profile(), 100 + i)
        group = next(iter(memory.responses.values()))
        for i in range(1, 21):
            group["current"][str(i)].mean = 0.6
            group["long_term"][str(i)].mean = i * 0.03
        group["current"]["20"].variance = 0.25
        prediction = memory.predict_response(60, 0.8, 108)
        self.assertEqual(memory.last_source, "blended")
        self.assertAlmostEqual(memory.last_weight, 0.5, places=5)
        self.assertTrue(all(right[1] >= left[1] for left, right in zip(prediction.points, prediction.points[1:])))

    def test_mixed_confidence_diagnostics_separate_off_memory(self):
        memory = CurveMemory()
        for i in range(8):
            memory.learn_response(profile(), 100 + i)
        diagnostics = memory.diagnostics(107 + 4 * CURRENT_MAX_AGE)
        self.assertEqual(diagnostics["off_current_confidence"], 0)
        self.assertAlmostEqual(diagnostics["off_long_term_confidence"], 1)
        self.assertEqual(diagnostics["long_term_response_groups"], 1)
        self.assertEqual(diagnostics["last_long_term_update"], 107)
