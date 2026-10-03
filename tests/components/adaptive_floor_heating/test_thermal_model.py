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
    # Verify the compact delayed branch and preservation of schema-4 data; related: off_response.py.
    def test_delayed_cycle_retains_context_and_predicts_after_restart(self):
        from custom_components.adaptive_floor_heating.history import CompletedCycle
        context = {"start_temperature": 24, "on_delta": 0, "pre_slope": -0.1, "off_minutes": 240}
        sample = CompletedCycle(60, 0.7, 100, 0, heating_duration_minutes=30,
                                slope_at_off=0, off_context=context)
        model = ThermalLearningModel()
        for _ in range(8):
            model.add_cycle(sample)
        restored = ThermalLearningModel.from_snapshot(model.snapshot())
        self.assertAlmostEqual(restored.predict_off_response(30, 0, context=context).rise, 0.7)
        self.assertIsNone(restored.predict_off_response(15, 0, context=context))
        self.assertIsNone(restored.predict_off_response(30, 0))
        isolated = model.snapshot()
        isolated["off_response_profiles"][0]["context"]["start_temperature"] = 20
        self.assertEqual(model.off_response_profiles[0]["context"]["start_temperature"], 24)
        legacy = ThermalLearningModel().snapshot()
        legacy["schema_version"] = 4
        legacy["off_response_profiles"] = [{"duration": 30, "slope": 0.8, "rise": 0.6,
                                            "points": [[0, 0], [30, 0.6]]}] * 8
        migrated = ThermalLearningModel.from_snapshot(legacy)
        self.assertEqual(len(migrated.off_response_profiles), 8)
        self.assertAlmostEqual(migrated.predict_off_response(30, 0.8).rise, 0.6)
        self.assertEqual(migrated.snapshot()["schema_version"], 5)

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

    def test_heat_loss_uses_separate_robust_model_and_confidence(self):
        model = ThermalLearningModel()
        self.assertTrue(model.add_heat_loss_rate(0.04))
        self.assertTrue(model.add_heat_loss_rate(0.05))
        self.assertTrue(model.add_heat_loss_rate(0.045))
        self.assertGreater(model.heat_loss_rate_confidence, 0)
        self.assertFalse(model.add_heat_loss_rate(0.5))
        self.assertEqual(model.heat_loss_rate["samples"], 3)
        self.assertEqual(model.heat_loss_rate["rejected"], 1)
        self.assertEqual(model.accepted_cycles, 0)
        self.assertEqual(model.metrics["heating_rate"]["samples"], 0)

    def test_legacy_learning_snapshot_migrates_with_empty_heat_loss_model(self):
        model = ThermalLearningModel()
        legacy = model.snapshot()
        legacy["schema_version"] = 1
        del legacy["heat_loss_rate"]
        del legacy["response_curves"]
        del legacy["off_response_profiles"]
        restored = ThermalLearningModel.from_snapshot(legacy)
        self.assertIsNone(restored.heat_loss_rate["mean"])
        self.assertEqual(restored.heat_loss_rate["samples"], 0)
        self.assertEqual(restored.snapshot()["schema_version"], 5)

    def test_schema_two_migrates_and_curve_matching_uses_phase_and_curvature(self):
        model = ThermalLearningModel()
        for curvature, residual, mid in ((0.5, 0.9, 0.25), (-0.5, 0.2, 0.55)):
            for _ in range(3):
                sample = cycle(response=30, rise=residual, peak=20, rate=0.5)
                sample.response_curve = ((0, 0), (30, mid / 2), (45, mid), (90, 0.8))
                sample.coast_curve = ((0, 0), (20, 1))
                sample.heating_duration_minutes = 90
                sample.slope_at_off = 0.5
                sample.curvature_at_off = curvature
                model.add_cycle(sample)
        self.assertEqual(len(model.response_curves), 6)
        positive = model.predict_residual_from_curve(90, 0.8, 0.25, 0.5, 0.5)
        negative = model.predict_residual_from_curve(90, 0.8, 0.55, 0.5, -0.5)
        self.assertGreater(positive[0], negative[0])
        self.assertGreater(positive[1], 0)
        restored = ThermalLearningModel.from_snapshot(model.snapshot())
        self.assertEqual(restored.snapshot(), model.snapshot())
        damaged = model.snapshot()
        damaged["response_curves"][0]["points"][1][1] = float("nan")
        with self.assertRaises(ValueError):
            ThermalLearningModel.from_snapshot(damaged)
        legacy = model.snapshot()
        legacy["schema_version"] = 2
        del legacy["response_curves"]
        del legacy["off_response_profiles"]
        self.assertEqual(ThermalLearningModel.from_snapshot(legacy).response_curves, [])

    def test_heat_loss_estimate_round_trips_and_rejects_corruption(self):
        model = ThermalLearningModel()
        for value in (0.03, 0.032, 0.031):
            model.add_heat_loss_rate(value)
        restored = ThermalLearningModel.from_snapshot(model.snapshot())
        self.assertEqual(restored.snapshot(), model.snapshot())
        damaged = model.snapshot()
        damaged["heat_loss_rate"]["mean"] = float("nan")
        with self.assertRaises(ValueError):
            ThermalLearningModel.from_snapshot(damaged)

    def test_empty_sample_is_ignored_and_invalid_range_is_rejected(self):
        model = ThermalLearningModel()
        self.assertFalse(model.add_cycle(cycle(None, None, None, None)))
        self.assertTrue(model.add_cycle(cycle(response=60, rise=11, peak=45)))
        self.assertEqual(model.metrics["residual_rise"]["rejected"], 1)
        self.assertEqual(model.metrics["heating_response_delay"]["samples"], 1)

    def test_compact_model_learns_zero_coast_without_an_on_curve(self):
        model = ThermalLearningModel()
        for _ in range(8):
            sample = cycle(rise=0, peak=10)
            sample.heating_duration_minutes = 15
            sample.slope_at_off = 0.8
            model.add_cycle(sample)
        self.assertEqual(model.response_curves, [])
        self.assertEqual(model.predict_off_response(15, 0.8).rise, 0)
        self.assertEqual(ThermalLearningModel.from_snapshot(model.snapshot()).snapshot(), model.snapshot())

    def test_schema_three_preserves_aggregates_and_migrates_off_data(self):
        model = ThermalLearningModel()
        model.add_cycle(cycle())
        old = model.snapshot()
        old["schema_version"] = 3
        del old["off_response_profiles"]
        restored = ThermalLearningModel.from_snapshot(old)
        self.assertEqual(restored.metrics, model.metrics)
        self.assertEqual(restored.off_response_profiles, [])
        damaged = model.snapshot()
        damaged["off_response_profiles"] = [{"duration": float("nan")}]
        with self.assertRaises(ValueError):
            ThermalLearningModel.from_snapshot(damaged)


if __name__ == "__main__":
    unittest.main()
