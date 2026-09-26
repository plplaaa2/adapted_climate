"""Thermal EWMA and corruption handling; related: thermal_model.py and storage.py."""

from types import SimpleNamespace
import unittest

from custom_components.adaptive_floor_heating.thermal_model import ThermalLearningModel


def cycle(response=60, rise=0.5, peak=45, rate=0.8):
    return SimpleNamespace(
        response_delay_minutes=response,
        heating_rate_c_per_hour=rate,
        residual_rise=rise,
        peak_delay_minutes=peak,
    )


class ThermalLearningModelTests(unittest.TestCase):
    def test_ewma_and_confidence_rise_with_repeated_cycles(self):
        model = ThermalLearningModel()
        for _ in range(4):
            self.assertTrue(model.add_cycle(cycle()))
        self.assertEqual(model.metrics["residual_rise"]["mean"], 0.5)
        self.assertEqual(model.metrics["residual_rise"]["samples"], 4)
        self.assertAlmostEqual(model.confidence("residual_rise"), 0.4)
        model.add_cycle(cycle(response=70, rise=1.0, peak=50))
        self.assertAlmostEqual(model.metrics["heating_response_delay"]["mean"], 62.0)
        self.assertAlmostEqual(model.metrics["residual_rise"]["mean"], 0.6)

    def test_extreme_cycles_are_rejected_after_baseline_samples(self):
        model = ThermalLearningModel()
        for _ in range(3):
            model.add_cycle(cycle())
        self.assertTrue(model.add_cycle(cycle(response=500, rise=5.0, peak=900, rate=5.0)))
        self.assertEqual(model.rejected_cycles, 1)
        for name in model.metrics:
            self.assertEqual(model.metrics[name]["samples"], 3)
            self.assertEqual(model.metrics[name]["rejected"], 1)

    def test_missing_response_does_not_discard_valid_coast_measurements(self):
        model = ThermalLearningModel()
        model.add_cycle(cycle(response=None, rise=0.4, peak=35))
        self.assertEqual(model.accepted_cycles, 1)
        self.assertEqual(model.metrics["heating_response_delay"]["samples"], 0)
        self.assertEqual(model.metrics["residual_rise"]["samples"], 1)

    def test_snapshot_round_trip_and_corruption_rejection(self):
        model = ThermalLearningModel()
        for _ in range(5):
            model.add_cycle(cycle())
        restored = ThermalLearningModel.from_snapshot(model.snapshot())
        self.assertEqual(restored.snapshot(), model.snapshot())
        damaged = model.snapshot()
        damaged["metrics"]["residual_rise"]["mean"] = float("nan")
        with self.assertRaises(ValueError):
            ThermalLearningModel.from_snapshot(damaged)

    def test_empty_sample_is_ignored_and_invalid_range_is_rejected(self):
        model = ThermalLearningModel()
        self.assertFalse(model.add_cycle(cycle(None, None, None, None)))
        self.assertTrue(model.add_cycle(cycle(response=60, rise=11, peak=45)))
        self.assertEqual(model.metrics["residual_rise"]["rejected"], 1)
        self.assertEqual(model.metrics["heating_response_delay"]["samples"], 1)


if __name__ == "__main__":
    unittest.main()
