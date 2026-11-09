"""Verify additive decomposition, missing/out-of-range evidence and persistence; related: peak_prediction.py, storage.py."""

from copy import deepcopy
import unittest
from custom_components.adaptive_floor_heating.peak_prediction import forecast, sample, valid_forecast
from custom_components.adaptive_floor_heating.storage import _decode_off_prediction, decode_state


def training(outdoors=True):
    return [sample(20, 20.2, 21.2 + .02*t, 60, t if outdoors else None, 100+i)
            for i, t in enumerate((-10, -5, 0, 5, 10, 15))]


def predict(rows=None, **changes):
    args = dict(start=20, temperature=20.2, minutes=60, outdoor=0, now=1000, bias=0)
    args.update(changes)
    return forecast(training() if rows is None else rows, **args)


class AdditivePeakTests(unittest.TestCase):
    def test_hybrid_interpolates_duration_specific_rise_and_refuses_short_extrapolation(self):
        rows = [sample(22, 22, 22 + rise, minutes, None, 100)
                for minutes, rise in ((20, .6), (30, .8), (40, 1.6))]
        hybrid = predict(rows, start=22, temperature=22, minutes=25, duration_curve=True)
        self.assertEqual(hybrid["kind"], "hybrid_v1")
        self.assertAlmostEqual(hybrid["base_residual"], .7)
        self.assertTrue(valid_forecast(hybrid))
        self.assertIsNone(predict(rows, minutes=19, duration_curve=True))
        self.assertTrue(predict(rows, minutes=19)["duration_extrapolated"])

    def test_sum_and_future_label_independence(self):
        rows = training()
        before = deepcopy(rows)
        value = predict(rows)
        self.assertTrue(valid_forecast(value))
        self.assertEqual(rows, before)
        self.assertEqual(predict(rows+[sample(20, 20.2, 24, 60, -30, 2000)]), value)
        self.assertGreaterEqual(value['predicted_peak'], value['temperature'])

    def test_weather_responds_but_cold_extrapolation_is_flagged_and_bounded(self):
        cold, warm = predict(outdoor=-20), predict(outdoor=10)
        self.assertLess(cold['outdoor_correction'], warm['outdoor_correction'])
        self.assertEqual(cold['outdoor_status'], 'extrapolated')
        self.assertLessEqual(abs(cold['outdoor_correction']), .3)
        self.assertTrue(valid_forecast(predict(outdoor=-60, temperature=22, bias=.5)))

    def test_unknown_conditions_remain_unknown_and_insufficient_samples_fall_back(self):
        self.assertEqual(predict(outdoor=None)['outdoor_status'], 'missing')
        value = predict(training(False))
        self.assertEqual(value['outdoor_status'], 'untrained')
        self.assertEqual(value['outdoor_correction'], 0)
        self.assertIsNone(predict(training()[:2]))
        self.assertIsNone(predict(minutes=10))
        self.assertIsNone(predict(temperature=float('nan')))

    def test_correction_caps_and_durable_breakdown_reject_corruption(self):
        value = predict(temperature=23, outdoor=-50, bias=.5)
        self.assertTrue(valid_forecast(value))
        self.assertLessEqual(abs(value['predicted_peak']-value['temperature']-value['base_residual']), .500001)
        saved = dict(off_at=1000, predictions={'curve': value['predicted_peak']}, confidences={'curve': None},
                     comparison_counts={'curve': 0}, selected_model='curve', peak_forecast=value)
        self.assertEqual(_decode_off_prediction(saved), saved)
        corrupt = deepcopy(saved)
        corrupt['peak_forecast']['predicted_peak'] += 1
        self.assertIsNone(_decode_off_prediction(corrupt))

    def test_new_feedback_is_separate_from_legacy_bias(self):
        state = dict(schema_version=1, target=23, mode='off', faults=[], preset='home',
                     prediction_mode='balanced', additive_feedback={'bias': .2, 'errors': [-.1]})
        restored = decode_state(state)
        self.assertEqual(restored['additive_feedback'], state['additive_feedback'])
        self.assertEqual(restored['prediction_bias']['curve'], 0)
        state['additive_feedback']['bias'] = float('nan')
        self.assertEqual(decode_state(state)['additive_feedback'], {'bias': 0.0, 'errors': []})
