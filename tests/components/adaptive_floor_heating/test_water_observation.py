"""Pipe observation boundary tests; related: water_observation.py and runtime.py."""

import unittest
from custom_components.adaptive_floor_heating.water_observation import WaterObservation


class WaterTests(unittest.TestCase):
    def test_independent_reports_skew_and_expiry(self):
        w = WaterObservation(supply=True, return_sensor=True)
        w.report('supply', 40, 0)
        w.report('return', 35, 400)
        w.report('room', 23, 400)
        s = w.snapshot(400)
        self.assertIsNone(s['water_difference'])
        self.assertEqual(s['water_return_room'], 12)
        w.report('supply', 39, 500)
        self.assertEqual(w.snapshot(500)['water_difference'], 4)
        self.assertIsNone(w.snapshot(1400)['water_supply'])

    def test_sparse_slopes_actual_times_and_single_sensor(self):
        w = WaterObservation(supply=True, timeout=3600)
        for t in (0, 1000, 2500, 4000):
            w.report('supply', 40 - t / 3600, t)
        s = w.snapshot(4000)
        self.assertAlmostEqual(s['water_supply_slope'], -1)
        self.assertIsNone(s['water_return'])
        self.assertIsNone(s['water_difference'])

    def test_complete_cycle_publishes_matched_off_values(self):
        w = WaterObservation(supply=True, timeout=3600)
        w.heater(False, 0)
        w.report('room', 20, 0)
        w.report('supply', 40, 0)
        w.heater(True, 0)
        w.report('room', 21, 600)
        w.report('supply', 38, 600)
        w.heater(False, 600)
        self.assertIsNone(w.snapshot(600)['water_residual_rise'])
        for t, value in ((1200, 21.5), (1800, 21.4), (2400, 21.2), (3000, 21), (3600, 20.8)):
            w.report('supply', 35, t)
            w.report('room', value, t)
        s = w.snapshot(3600)
        self.assertEqual(s['water_off_supply'], 38)
        self.assertEqual(s['water_residual_rise'], 0.5)
        self.assertEqual(s['water_peak_delay'], 10)
        w.heater(True, 3700)
        self.assertEqual(w.snapshot(3700)['water_residual_rise'], 0.5)

    def test_invalid_or_unknown_transition_discards_partial_cycle(self):
        for failure in ('sensor', 'heater'):
            w = WaterObservation(supply=True)
            w.heater(False, 0)
            w.report('room', 20, 0)
            w.report('supply', 40, 0)
            w.heater(True, 0)
            w.heater(False, 100)
            if failure == 'sensor':
                w.report('supply', None, 200)
            else:
                w.heater(None, 200)
            self.assertIsNone(w.pending)
            self.assertIsNone(w.snapshot(200)['water_residual_rise'])
        self.assertEqual(WaterObservation().snapshot(0)['water_status'], 'not_configured')
