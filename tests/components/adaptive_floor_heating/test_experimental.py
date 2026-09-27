"""Verify shadow predictions without heater side effects; related: experimental.py, sensor.py."""

import unittest
from types import SimpleNamespace

from custom_components.adaptive_floor_heating.experimental import prediction_snapshot
from custom_components.adaptive_floor_heating.history import ThermalObservation, CompletedCycle
from custom_components.adaptive_floor_heating.thermal_model import ThermalLearningModel


class ExperimentalTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800
        self.outdoor = 10
        self.observation = ThermalObservation()
        for t in (0, 900, 1800):
            self.observation.report_temperature(23.3 - t / 3600 * 0.6, t)
        self.model = ThermalLearningModel()
        for _ in range(10):
            self.model.add_cycle(CompletedCycle(30, 0.5, 30, 0.4))
            self.model.add_heat_loss_rate(0.04)
        self.runtime = SimpleNamespace(
            started=True, sensor="sensor.indoor", outdoor_sensor="sensor.outdoor",
            hass=SimpleNamespace(loop=SimpleNamespace(time=lambda: self.now),
                                 states=SimpleNamespace(get=lambda _: 23)),
            _temperature=lambda state: state,
            _optional_temperature=lambda entity, now: self.outdoor,
            observation=self.observation, thermal_model=self.model,
            actuator=SimpleNamespace(observed=False),
        )

    def test_blend_and_baseline_are_comparable_without_mutating_model(self):
        before = self.model.snapshot()
        result = prediction_snapshot(self.runtime)
        self.assertEqual(result['experimental_status'], 'ready')
        self.assertAlmostEqual(result['experimental_outdoor_slope'], -0.52)
        self.assertAlmostEqual(result['experimental_baseline_temperature'], 22.7)
        self.assertGreater(result['experimental_predicted_temperature'], 22.7)
        self.assertLess(result['experimental_predicted_temperature'], 22.74)
        self.assertLessEqual(result['experimental_outdoor_weight'], 50)
        self.assertEqual(before, self.model.snapshot())
        self.assertFalse(self.runtime.actuator.observed)

    def test_missing_outdoor_keeps_baseline_only(self):
        self.outdoor = None
        result = prediction_snapshot(self.runtime)
        self.assertEqual(result['experimental_status'], 'missing_outdoor')
        self.assertIsNotNone(result['experimental_baseline_temperature'])
        self.assertIsNone(result['experimental_predicted_temperature'])

    def test_sparse_reports_are_allowed_but_old_reports_are_rejected(self):
        self.now = 2700
        self.assertEqual(prediction_snapshot(self.runtime)['experimental_status'], 'ready')
        self.now = 3601
        result = prediction_snapshot(self.runtime)
        self.assertEqual(result['experimental_status'], 'stale_indoor')
        self.assertIsNone(result['experimental_predicted_temperature'])

    def test_residual_heating_and_missing_learning_block_prediction(self):
        self.observation.off_at = 1700
        self.assertEqual(prediction_snapshot(self.runtime)['experimental_status'], 'observing_residual')
        self.observation.off_at = None
        self.runtime.actuator.observed = True
        self.assertEqual(prediction_snapshot(self.runtime)['experimental_status'], 'heater_not_off')
        self.runtime.actuator.observed = False
        self.model.heat_loss_rate['mean'] = None
        self.assertEqual(prediction_snapshot(self.runtime)['experimental_status'], 'missing_heat_loss_model')
