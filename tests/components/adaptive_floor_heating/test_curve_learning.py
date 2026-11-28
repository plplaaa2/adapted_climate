"""Verify five-minute curve contracts; related: curve_learning.py and curve_storage.py."""

import unittest

from custom_components.adaptive_floor_heating.curve_learning import (
    CurveCycle, CurveResult, CurveStandards, CurveTracker,
)


class CurveLearningTests(unittest.TestCase):
    def test_confirmed_runtime_and_on_intervals_gate_heating_and_linked_cooling(self):
        model = CurveStandards()
        for minutes, required in ((15, 2), (30, 5), (32, 5)):
            for elapsed, count, expected in (
                (minutes * 60 - 1, required, "HEATING_RUNTIME_BELOW_MINIMUM"),
                (minutes * 60, required - 1, "INSUFFICIENT_ON_BUCKETS"),
                (minutes * 60, required, "ACCEPTED"),
            ):
                for kind in ("WARM_HEATING", "PREDICTIVE_WARM_HEATING", "SUNRISE_HEATING", "COOLING"):
                    with self.subTest(minutes=minutes, elapsed=elapsed, count=count, kind=kind):
                        # Additional post-OFF bins never substitute for completed ON bins.
                        cycle = CurveCycle("test", 0, "THRESHOLD_START", "COMFORT", kind, 20, 20, 0, 300,
                                           off_at=elapsed, minimum_heating_seconds=minutes * 60,
                                           heating_buckets={**dict.fromkeys(range(count), .1), 20: .1})
                        checks = cycle.heating_learning_checks()
                        self.assertEqual((checks[1]["actual"], checks[1]["min"]), (count, required))
                        has_peak = kind != "SUNRISE_HEATING"
                        result = CurveResult(
                            "test", kind, "THRESHOLD_START", "TARGET_REACHED", "NEXT_ON", "COMFORT",
                            0, elapsed, elapsed + 600 if has_peak else None, elapsed + 1200,
                            {0: .1, 1: .1}, .2 if has_peak and kind != "COOLING" else None, None,
                            heating_learning_checks=checks,
                        )
                        self.assertEqual(model.assess(result), (expected == "ACCEPTED", expected))

    # Verify the stage boundary and separate, earned correction budget without forcing a stop.
    # Related: runtime.py and curve_storage.py durable average state.
    def test_shutdown_limits_keep_average_floor_and_separate_correction(self):
        from unittest.mock import patch
        model = CurveStandards()
        model.load_runtime_samples([(60, 1), (60, 1), (80, 2)])
        model.runtime_average_samples = [60] * 10
        model.runtime_accepted_cycles = 9
        self.assertAlmostEqual(model.runtime_shutdown_limits(None, 20, 21)["minimum"], 48)
        model.runtime_accepted_cycles = 10
        limits = model.runtime_shutdown_limits(None, 20, 21)
        self.assertAlmostEqual(limits["minimum"], 54)
        self.assertEqual(limits["correction_reduction"], 0)
        with patch.object(model, 'required_heating_minutes', side_effect=[60, 50]):
            limits = model.runtime_shutdown_limits(5, 20, 21, .1)
        self.assertEqual(limits["correction_reduction"], .05)
        self.assertAlmostEqual(limits["minimum"], 51)
        model.runtime_average_samples = [100] * 10
        self.assertEqual(model.runtime_shutdown_limits(None, 20, 21)["minimum"], 60)

    # A new 0.1 C confirmation dip must not arm the older 0.2 C rebound exclusion; related: history.py.
    def test_small_peak_dips_do_not_prematurely_cancel_on_rebound(self):
        tracker = CurveTracker()
        tracker.seed(False, 22.5, 0)
        tracker.switch(True, 0, hold_settings={"target_temperature": 23})
        tracker.report_temperature(22.6, 300)
        tracker.switch(False, 600, response_observed=True)
        for at, temperature in ((900, 23.5), (1200, 23.4), (1500, 23.5),
                                (1800, 23.4), (2100, 23.5), (2400, 23.6)):
            tracker.report_temperature(temperature, at)
        self.assertEqual(tracker.take_results(), [])
        self.assertIsNotNone(tracker.cycle)
        tracker.report_temperature(23.5, 2700)
        tracker.report_temperature(23.4, 3000)
        self.assertEqual(tracker.take_results()[0].end_reason, "PEAK_CONFIRMED")

    # Exercise strict solar excess and the unlimited reliable-forecast horizon; related: runtime.py.
    def test_reliable_peak_waits_past_timeout_and_ends_only_after_sunrise_excess(self):
        tracker = CurveTracker(max_peak_wait_seconds=600)
        tracker.seed(False, 20, 0)
        tracker.switch(True, 0)
        tracker.report_temperature(20.2, 300)
        tracker.switch(False, 600, response_observed=True, sunrise_peak_excess={
            "predicted_peak": 20.5, "sunrise_at": 1800, "threshold": .5,
            "ends_observation": True, "mean_absolute_error": .5,
        })
        tracker.report_temperature(21.1, 1500)
        tracker.advance(1800)
        self.assertEqual(tracker.take_results(), [])
        tracker.report_temperature(21, 1900)
        self.assertEqual(tracker.take_results(), [])
        tracker.report_temperature(21.01, 2000)
        result = tracker.take_results()[0]
        self.assertEqual(result.end_reason, "SUNRISE_PEAK_EXCESS")
        self.assertEqual(result.invalid_reason, "SUNRISE_PEAK_EXCESS")
        self.assertIsNone(result.peak_at)
        self.assertIsNone(tracker.cycle)
        self.assertFalse(CurveStandards().assess(result)[0])

    def test_reliable_peak_confirms_and_is_accepted_after_configured_horizon(self):
        tracker = CurveTracker(max_peak_wait_seconds=600)
        tracker.seed(False, 20, 0)
        tracker.switch(True, 0)
        tracker.report_temperature(20.2, 300)
        tracker.switch(False, 600, response_observed=True, sunrise_peak_excess={
            "predicted_peak": 20.5, "sunrise_at": 10000, "threshold": .5,
            "ends_observation": True,
        })
        tracker.report_temperature(20.5, 1800)
        tracker.report_temperature(20.4, 2100)
        tracker.report_temperature(20.3, 2400)
        result = tracker.take_results()[0]
        self.assertEqual(result.end_reason, "PEAK_CONFIRMED")
        self.assertEqual(result.peak_at, 1800)
        self.assertEqual(CurveStandards(max_peak_wait_minutes=10).assess(result), (True, "ACCEPTED"))

    def test_heating_hold_outlives_three_hours_and_preserves_endpoint_weather(self):
        tracker = CurveTracker()
        tracker.seed(False, 22, 0)
        tracker.switch(True, 10, outdoor_temperature=4, hold_settings={"target_temperature": 23})
        tracker.report_temperature(23.2, 100)
        tracker.switch(False, 200, response_observed=True)
        tracker.report_temperature(23.6, 300)
        tracker.report_temperature(23.3, 600)
        tracker.report_temperature(23.2, 900)
        tracker.advance(12000)
        tracker.report_temperature(23, 12300, outdoor_temperature=2)
        heating, cooling = tracker.take_results()
        self.assertEqual(heating.end_reason, "PEAK_CONFIRMED")
        self.assertEqual(cooling.end_reason, "TARGET_REACHED")
        self.assertEqual(cooling.heating_hold["hold_seconds"], 12290)
        self.assertEqual(cooling.heating_hold["boiler_on_seconds"], 190)
        self.assertEqual(cooling.heating_hold["outdoor_end"], 2)
        self.assertEqual(cooling.heating_hold["outdoor_mean"], 3)
        self.assertEqual(cooling.heating_hold["outdoor_change"], -2)
        self.assertGreater(len(cooling.buckets), 36)

    # Observe the original threshold across preset changes and confirmed ON edges; related: runtime.py.
    def _tracker_at_target(self):
        tracker = CurveTracker()
        tracker.seed(False, 22, 0)
        tracker.switch(True, 10, hold_settings={"target_temperature": 23, "cold_tolerance": .5})
        tracker.report_temperature(23.2, 100)
        tracker.switch(False, 200, response_observed=True)
        for at, temperature in ((300, 23.6), (600, 23.3), (900, 23.2), (1200, 23)):
            tracker.report_temperature(temperature, at, outdoor_temperature=4)
        _, cooling = tracker.take_results()
        self.assertEqual(cooling.end_reason, "TARGET_REACHED")
        self.assertEqual(cooling.ended_at, 1200)
        return tracker, cooling.cycle_id

    def test_below_target_cooling_reaches_original_threshold_after_preset_change(self):
        tracker, cycle_id = self._tracker_at_target()
        tracker.set_preset("away", 1250)
        tracker.report_temperature(22.9, 1300, outdoor_temperature=4)
        tracker.report_temperature(22.7, 1440, outdoor_temperature=3)
        self.assertEqual(tracker.take_below_target_cooling(), [])
        tracker.report_temperature(22.5, 1500, outdoor_temperature=2)
        (recorded_id, record), = tracker.take_below_target_cooling()
        self.assertEqual(recorded_id, cycle_id)
        self.assertEqual(record, {
            "started_at": 1200, "ended_at": 1500, "end_reason": "LOWER_REACHED",
            "lower_temperature": 22.5, "minimum_temperature": 22.5,
            "samples": [
                {"seconds": 0, "temperature": 23, "outdoor_temperature": 4},
                {"seconds": 240, "temperature": 22.7, "outdoor_temperature": 3},
                {"seconds": 300, "temperature": 22.5, "outdoor_temperature": 2},
            ],
        })

    def test_below_target_cooling_ends_on_either_confirmed_start(self):
        for start_reason in ("PREDICTIVE_START", "THRESHOLD_START"):
            with self.subTest(start_reason=start_reason):
                tracker, cycle_id = self._tracker_at_target()
                tracker.report_temperature(22.7, 1510)
                tracker.switch(True, 1550, start_reason=start_reason)
                tracker.report_temperature(22.5, 1580)
                (recorded_id, record), = tracker.take_below_target_cooling()
                self.assertEqual(recorded_id, cycle_id)
                self.assertEqual(record["end_reason"], "NEXT_ON")
                self.assertEqual(record["ended_at"], 1550)
                self.assertEqual(record["minimum_temperature"], 22.7)
                self.assertEqual(record["samples"][-1]["seconds"], 310)

    def test_below_target_cooling_replays_crossings_before_peak_confirmation(self):
        tracker = CurveTracker()
        tracker.seed(False, 22, 0)
        tracker.switch(True, 10, hold_settings={"target_temperature": 23, "cold_tolerance": .5})
        tracker.report_temperature(23.2, 100)
        tracker.switch(False, 200, response_observed=True)
        tracker.report_temperature(22.9, 600, outdoor_temperature=4)
        self.assertEqual(tracker.take_below_target_cooling(), [])
        tracker.report_temperature(22.5, 900, outdoor_temperature=3)
        _, cooling = tracker.take_results()
        (cycle_id, record), = tracker.take_below_target_cooling()
        self.assertEqual(cycle_id, cooling.cycle_id)
        self.assertEqual(record["started_at"], 600)
        self.assertEqual(record["ended_at"], 900)
        self.assertEqual(record["end_reason"], "LOWER_REACHED")
        self.assertEqual(record["samples"][-1]["outdoor_temperature"], 3)

    def test_heating_hold_excludes_cooling_after_two_rebounds(self):
        tracker = CurveTracker()
        tracker.seed(False, 22, 0)
        tracker.switch(True, 10, hold_settings={"target_temperature": 23})
        tracker.report_temperature(23.2, 100)
        tracker.switch(False, 200, response_observed=True)
        tracker.report_temperature(23.8, 300)
        tracker.report_temperature(23.5, 600)
        tracker.report_temperature(23.4, 900)
        tracker.report_temperature(23.5, 1200)
        tracker.report_temperature(23.6, 1500)
        _, cooling = tracker.take_results()
        self.assertEqual(CurveStandards().assess(cooling), (False, "REPEATED_RISE"))
        self.assertFalse(cooling.heating_hold["completed"])
        self.assertIsNone(cooling.heating_hold["outdoor_end"])

    def test_heating_hold_survives_peak_timeout_without_reopening_heating_learning(self):
        tracker = CurveTracker(max_peak_wait_seconds=600)
        tracker.seed(False, 22, 0)
        tracker.switch(True, 10, outdoor_temperature=4, hold_settings={"target_temperature": 23})
        tracker.report_temperature(23.2, 100)
        tracker.switch(False, 200, response_observed=True)
        tracker.report_temperature(23.6, 300)
        tracker.advance(800)
        heating, = tracker.take_results()
        self.assertEqual(heating.end_reason, "PEAK_TIMEOUT")
        self.assertIsNone(heating.heating_hold)
        self.assertIsNone(tracker.cycle)
        tracker.report_temperature(23.3, 900)
        tracker.report_temperature(23.2, 1200)
        tracker.report_temperature(23, 14000)
        cooling, = tracker.take_results()
        self.assertEqual(cooling.end_reason, "TARGET_REACHED")
        self.assertEqual(cooling.heating_hold["hold_seconds"], 13990)
        self.assertIsNone(cooling.heating_hold["outdoor_mean"])

    # New OFF evidence shares the predictive held-state slope; related: history.py, runtime.py.
    def test_off_slope_flattens_without_inventing_peak_reports(self):
        tracker = CurveTracker()
        tracker.report_temperature(20, 0)
        tracker.switch(False, 0)
        tracker.switch(True, 0)
        for timestamp in range(300, 1801, 300):
            tracker.report_temperature(20 + timestamp / 3600, timestamp)
        samples = tracker.cycle.on_history.samples
        tracker.switch(False, 4800)
        self.assertEqual(tracker.cycle.slope_at_off, 0)
        self.assertEqual(tracker.cycle.on_history.samples, samples)
        self.assertIsNone(tracker.cycle.peak_at)
        self.assertEqual(len(tracker.cycle.off_reports), 1)

    def test_runtime_peak_curve_generalizes_start_and_target_temperatures(self):
        model = CurveStandards()
        samples = [
            (20, 0.8), (40, 1.6), (60, 2.4),
        ]
        model.load_runtime_samples(samples)
        self.assertAlmostEqual(model.required_heating_minutes(18, 20), 50)
        self.assertAlmostEqual(model.required_heating_minutes(21, 23), 50)
        self.assertAlmostEqual(model.predicted_peak(18, 50), 20)

    def test_runtime_peak_curve_weights_outdoor_evidence_continuously(self):
        model = CurveStandards()
        # (heater minutes, peak rise, lag, outdoor temperature, completed_at)
        model.load_runtime_samples([
            (20, 0.5, 0, -7, 1), (40, 1.0, 0, -7, 2), (60, 1.5, 0, -7, 3),
            (20, 1.0, 0, 5, 4), (40, 2.0, 0, 5, 5), (60, 3.0, 0, 5, 6),
        ])
        global_peak = model.predicted_peak(18, 40)
        self.assertAlmostEqual(global_peak, 19.5)
        self.assertLess(model.predicted_peak(18, 40, -6), global_peak)
        self.assertGreater(model.predicted_peak(18, 40, 5), global_peak)
        self.assertAlmostEqual(model.predicted_peak(18, 40, 4.99), model.predicted_peak(18, 40, 5.01), places=3)

    def test_runtime_peak_curve_waits_for_three_complete_heating_cycles(self):
        model = CurveStandards()
        model.load_runtime_samples([(20, 0.8), (40, 1.6)])
        self.assertIsNone(model.required_heating_minutes(18, 20))
        model.load_runtime_samples([(20, 0.8), (40, 1.6), (60, 2.4)])
        self.assertEqual(model.required_heating_minutes(18, 20), 50)

    def test_runtime_peak_curve_accounts_for_heating_response_lag(self):
        model = CurveStandards()
        model.load_runtime_samples([(80, 2.0, 40), (100, 2.5, 50), (120, 3.0, 60)])
        self.assertAlmostEqual(model.required_heating_minutes(20, 21), 65)

    # Similar durations share evidence, and a weak short cycle cannot set the control floor.
    def test_runtime_outcome_weights_pool_conflicting_observations(self):
        model = CurveStandards()
        model.load_runtime_samples([
            (24, 1.5, 0, 7, None, .2), (26, .5, 0, 7, None, 1),
            (36, .6, 0, 7, None, 1), (50, 2, 0, 7, None, 1),
        ])
        self.assertAlmostEqual(model.predicted_peak(22, 30, 7), 22 + 1.4 / 2.2)
        self.assertEqual(model.runtime_control_minimum(7), 36)

    # Reproduce the field-like long plateau beyond the old three-hour boundary; related: history.py.
    def test_long_plateau_01_then_01_drop_confirms_both_models_before_six_hours(self):
        from custom_components.adaptive_floor_heating.history import ThermalObservation
        curve, basic = CurveTracker(), ThermalObservation()
        curve.seed(False, 23.4, 0)
        basic.seed_heater(False)
        curve.switch(True, 0)
        basic.observe_heater(True, 0, 23.4)
        on_anchors = [(0,23.4),(10,23.2),(30,23.3),(60,23.8),(90,24.6),(120,25.6),(140,26.1)]
        for minute in range(10,141,10):
            temperature = next(value for at,value in reversed(on_anchors) if at <= minute)
            curve.report_temperature(temperature, minute*60)
            basic.report_temperature(temperature, minute*60)
        curve.switch(False, 140*60)
        basic.observe_heater(False, 140*60, 26.1)
        anchors = [(0,26.1),(10,26.3),(25,26.4),(50,26.5),(91,26.6),
                   (159,26.5),(161,26.6),(165,26.5),(168,26.6),(172,26.5),
                   (175,26.6),(178,26.5),(191,26.4),(201,26.3),(211,26.2)]
        for elapsed in range(1, 192):
            temperature = next(value for minute,value in reversed(anchors) if minute <= elapsed)
            at = (140+elapsed)*60
            curve.report_temperature(temperature, at)
            basic.report_temperature(temperature, at)
            if elapsed < 191:
                self.assertEqual(curve.take_results(), [])
                self.assertEqual(basic.completed_cycles, 0)
        result = curve.take_results()[0]
        self.assertEqual(result.end_reason, "PEAK_CONFIRMED")
        self.assertEqual(result.ended_at, (140+191)*60)
        self.assertEqual(result.peak_at, (140+177)*60)
        self.assertAlmostEqual(result.peak_confirmation_drop, .2)
        self.assertAlmostEqual(result.peak_confirmation_extra_drop, .1)
        self.assertEqual(result.peak_confirmation_reports, 2)
        self.assertEqual(basic.completed_cycles, 1)
        self.assertEqual(basic.last_cycle.peak_delay_minutes, 177)
        self.assertEqual(CurveStandards().assess(result), (True, "ACCEPTED"))

    def test_slow_peak_keeps_buckets_after_six_hours_on_to_peak(self):
        tracker = CurveTracker()
        tracker.seed(False, 20, 0)
        tracker.switch(True, 0)
        tracker.report_temperature(21, 3*3600)
        tracker.switch(False, 4*3600, response_observed=True)
        tracker.report_temperature(21.5, 7*3600)
        tracker.report_temperature(21.2, 7*3600+300)
        tracker.report_temperature(21.1, 7*3600+600)
        result = tracker.take_results()[0]
        self.assertEqual(len(result.buckets), 84)
        self.assertTrue(CurveStandards().assess(result)[0])
        self.assertEqual(max(result.buckets), 83)
        self.assertEqual(result.peak_temperature, 21.5)

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
        tracker.report_temperature(24.4, 160 * 60)
        result = tracker.take_results()[0]
        tracker.report_temperature(24.3, 170 * 60)
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
        self.assertAlmostEqual(sum(tracker.cycle.cooling_buckets.values()), -0.3)

    def test_sunrise_peak_excess_records_first_threshold_and_maximum(self):
        tracker = CurveTracker()
        tracker.seed(False, 22.5, 0)
        tracker.switch(True, 0)
        tracker.switch(False, 1800, sunrise_peak_excess={
            "predicted_peak": 23.6, "sunrise_at": 3600, "threshold": 0.5,
        })
        for at, temperature in ((3300, 24.0), (3600, 24.09), (3900, 24.1), (4200, 24.8)):
            tracker.report_temperature(temperature, at)
        tracker.advance(1800 + 21600)
        result, = tracker.take_results()
        evidence = result.sunrise_peak_excess
        self.assertEqual(evidence["first_at"], 3900)
        self.assertEqual(evidence["first_temperature"], 24.1)
        self.assertAlmostEqual(evidence["maximum_excess"], 1.2)
        self.assertEqual(result.end_reason, "PEAK_TIMEOUT")
        self.assertEqual(result.invalid_reason, "SUNRISE_PEAK_EXCESS")

    def test_two_rises_after_first_decline_exclude_heating_curve(self):
        for reports, expected in (
            ((23.4, 23.5, 23.5, 23.6), "REBOUND_BEFORE_PEAK_CONFIRMATION"),
            ((23.4, 23.5, 23.4, 23.3), "PEAK_CONFIRMED"),
        ):
            with self.subTest(reports=reports):
                tracker = CurveTracker()
                tracker.seed(False, 22.5, 0)
                tracker.switch(True, 0)
                tracker.switch(False, 1800)
                tracker.report_temperature(23.6, 3600)
                for index, temperature in enumerate(reports, start=1):
                    tracker.report_temperature(temperature, 3600 + index * 600)
                result, = tracker.take_results()
                self.assertEqual(result.end_reason, expected)
                if expected == "REBOUND_BEFORE_PEAK_CONFIRMATION":
                    self.assertEqual(result.invalid_reason, expected)
                    self.assertIsNone(tracker.cycle)
                    self.assertFalse(CurveStandards().assess(result)[0])
                else:
                    self.assertEqual(result.peak_at, 3600)

    def test_unresponsive_pulse_times_out_without_valid_peak(self):
        tracker = CurveTracker()
        tracker.seed(False, 24, 0)
        tracker.switch(True, 0)
        tracker.switch(False, 1800)
        for minute in range(40, 210, 10):
            tracker.report_temperature(24 - minute * 0.002, minute * 60)
        tracker.advance(1800 + 21600)
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

    def test_cooling_continues_past_target_and_pairs_only_valid_outdoor_intervals(self):
        tracker = CurveTracker()
        tracker.seed(False, 20.0, 0)
        tracker.switch(True, 60, start_reason="PREDICTIVE_START")
        tracker.report_temperature(20.4, 360)
        tracker.switch(False, 660, off_reason="PREDICTIVE_STOP")
        tracker.report_cooling_outdoor(660, 5.0, 1710)
        tracker.report_temperature(20.6, 960)
        tracker.report_cooling_outdoor(1110, -5.0, 1710)
        tracker.advance(1560)
        self.assertEqual(tracker.take_results(), [])
        tracker.report_temperature(20.3, 1860)
        tracker.report_temperature(20.2, 2460)
        heating = tracker.take_results()
        self.assertEqual(len(heating), 1)
        self.assertEqual(heating[0].curve_type, "PREDICTIVE_WARM_HEATING")
        self.assertAlmostEqual(heating[0].residual_rise, 0.2)
        tracker.report_temperature(20.0, 2760)
        tracker.set_preset("away", 2800)
        self.assertEqual(tracker.take_results(), [])
        tracker.switch(True, 3060)
        cooling = tracker.take_results()
        self.assertEqual(len(cooling), 1)
        self.assertEqual(cooling[0].curve_type, "COOLING")
        self.assertEqual(cooling[0].end_reason, "NEXT_ON")
        self.assertIsNone(cooling[0].invalid_reason)
        self.assertEqual(cooling[0].cooling_outdoor_groups, {0: "NORMAL", 1: "COLD"})

    # Use the saved OFF goal to bound only the saving round's cooling record; related: runtime.py.
    def test_sunrise_cooling_finishes_at_the_saved_threshold(self):
        for confirming_temperature in (20.2, 19.5):
            with self.subTest(confirming_temperature=confirming_temperature):
                tracker = CurveTracker()
                tracker.seed(False, 20.0, 0)
                tracker.switch(True, 60, start_reason="PREDICTIVE_START")
                tracker.report_temperature(20.4, 360)
                tracker.switch(False, 660, off_reason="PREDICTIVE_STOP",
                               target_peak_temperature=20.0, sunrise_saving=True)
                tracker.report_temperature(20.6, 960)
                tracker.report_temperature(20.3, 1860)
                tracker.report_temperature(confirming_temperature, 2460)
                if confirming_temperature > 19.5:
                    heating, saving = tracker.take_results()
                    self.assertTrue(heating.sunrise_saving)
                    self.assertEqual(heating.target_peak_temperature, 20.0)
                    tracker.report_temperature(19.6, 2700)
                    self.assertEqual(tracker.take_results(), [])
                    tracker.report_temperature(19.5, 2760)
                    cooling, = tracker.take_results()
                else:
                    heating, saving, cooling = tracker.take_results()
                    self.assertTrue(heating.sunrise_saving)
                self.assertEqual(saving.curve_type, "SUNRISE_HEATING")
                self.assertEqual(set(saving.buckets), {0, 1})
                self.assertIsNone(saving.residual_rise)
                self.assertEqual(cooling.end_reason, "SUNRISE_COOLING_TARGET")
                self.assertTrue(cooling.sunrise_saving)
                self.assertEqual(cooling.target_peak_temperature, 20.0)
                self.assertIsNone(tracker.cycle)

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
        tracker.report_temperature(19.9, 2160)
        tracker.report_temperature(19.8, 2760)
        result = tracker.take_results()[0]
        self.assertEqual(result.peak_at, 1860)
        self.assertTrue(all(60 + (index + 1) * 300 <= 1860 for index in result.buckets))

    def test_second_rise_above_first_rise_ends_cooling_without_timer(self):
        tracker = CurveTracker()
        tracker.seed(False, 20.0, 0)
        tracker.switch(True, 60)
        tracker.report_temperature(20.4, 360)
        tracker.switch(False, 660)
        tracker.report_temperature(20.6, 960)
        tracker.advance(1560)
        tracker.report_temperature(20.3, 1860)
        tracker.report_temperature(20.2, 2460)
        tracker.take_results()
        tracker.report_temperature(20.3, 2760)
        tracker.report_temperature(20.1, 3060)
        tracker.report_temperature(20.25, 3360)
        self.assertEqual(tracker.take_results(), [])
        tracker.report_temperature(20.2, 3660)
        self.assertEqual(tracker.take_results(), [])
        tracker.report_temperature(20.31, 3960)
        result = tracker.take_results()[0]
        self.assertEqual(result.end_reason, "REPEATED_RISE")
        self.assertEqual(result.ended_at, 3960)

    def test_predictive_start_is_predictive_warm_after_any_away_duration(self):
        for away_duration in (10799, 10800, 14400):
            with self.subTest(duration=away_duration):
                tracker = CurveTracker()
                tracker.seed(False, 20, 0)
                tracker.set_preset("away", 1)
                tracker.set_preset("away", 200)  # Repeated command must not reset the clock.
                tracker.set_preset("home", 1 + away_duration)
                tracker.switch(True, 2 + away_duration, start_reason="PREDICTIVE_START")
                self.assertEqual(tracker.cycle.curve_type, "PREDICTIVE_WARM_HEATING")

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

    def test_cooling_prediction_requires_continuous_bucket_coverage(self):
        model = CurveStandards()
        model.buckets["COOLING"] = {0: (-0.1, 3), 1: (-0.2, 3)}
        self.assertAlmostEqual(model.predict_cooling_delta(2.5, 5), -0.15)
        self.assertIsNone(model.predict_cooling_delta(2.5, 10))
        model.outdoor_cooling = {("COLD", 0): (-0.2, 3), ("COLD", 1): (-0.4, 2)}
        self.assertIsNone(model.predict_cooling_delta(2.5, 5, outdoor_group="COLD"))
        model.outdoor_cooling["COLD", 1] = (-0.4, 3)
        self.assertAlmostEqual(model.predict_cooling_delta(2.5, 5, outdoor_group="COLD"), -0.3)

    def test_conditioned_cooling_uses_long_term_without_filling_missing_bins(self):
        from custom_components.adaptive_floor_heating.curve_memory import CURRENT_MAX_AGE
        model = CurveStandards()
        memory = model.outdoor_cooling_memory["COLD"]
        for now in range(8):
            memory.learn_conditioned_bucket(2, -.2, now)
            memory.learn_conditioned_bucket(3, -.4, now)
        self.assertEqual(set(memory.long_term), {2, 3})
        weights = []
        delta, layers = model.predict_cooling(
            12.5, 5, outdoor_group="COLD", now=CURRENT_MAX_AGE + 8, curve_weights=weights
        )
        self.assertAlmostEqual(delta, -.3)
        self.assertEqual(layers, ("long_term",))
        self.assertEqual(weights, [
            {"index": index, "curve_type": "COOLING", "current_weight": 0.0} for index in (2, 3)
        ])
        self.assertIsNone(model.predict_cooling_delta(
            12.5, 10, outdoor_group="COLD", now=CURRENT_MAX_AGE + 8))

    def test_hybrid_summary_interpolates_supported_weather_or_global_bins(self):
        model = CurveStandards()
        for completed in range(8):
            for minutes, rise, outdoor in ((35, .5, -3), (40, .8, -3), (60, 1.5, -3)):
                model.learn_hybrid_residual({
                    "minutes": minutes, "peak": 22 + rise, "off": 22,
                    "outdoor": outdoor, "completed": completed,
                })
        self.assertAlmostEqual(model.predict_hybrid_summary(22, 37.5, -2, 8), 22.65)
        self.assertAlmostEqual(model.predict_hybrid_summary(22, 37.5, 5, 8), 22.65)
        self.assertIsNone(model.predict_hybrid_summary(22, 45, -2, 8))
        self.assertIsNone(model.predict_hybrid_summary(22, 25, -2, 8))

    def test_off_summaries_interpolate_measured_durations_instead_of_bin_centers(self):
        model = CurveStandards()
        for completed in range(8):
            for minutes, rise in ((37.4, 1), (42.4, 2)):
                sample = {"minutes": minutes, "peak": 22 + rise, "off": 22, "start": 22,
                          "outdoor": -2, "completed": completed}
                model.learn_hybrid_residual(sample)
                model.learn_runtime_rise(sample)
        _, (left, before), (right, after) = model.long_term_runtime_segment(1.5, -2, 8)
        self.assertAlmostEqual(left + (right-left) * (1.5-before) / (after-before), 39.9)
        self.assertAlmostEqual(model.predict_hybrid_summary(22, 39.9, -2, 8), 23.5)
        self.assertIsNone(model.predict_hybrid_summary(22, 35, -2, 8))

    def test_missing_runtime_lag_does_not_reduce_measured_response_delay(self):
        model = CurveStandards()
        model.load_runtime_samples([(35, 1.2, 20), (36, 1.2, 0), (37, 1.2, None)])
        self.assertEqual(len(model.runtime_samples), 3)
        self.assertIsNone(model.response_delay_minutes("WARM_HEATING"))
        model.load_runtime_samples([(35, 1.2, 20)] * 3 + [(36, 1.2, 0), (37, 1.2, None)])
        self.assertAlmostEqual(model.response_delay_minutes("WARM_HEATING"), 20)

    def test_heating_weather_uses_weighted_reports_and_requires_continuous_coverage(self):
        for expiry, expected in ((260, 1), (159, None)):
            tracker = CurveTracker()
            tracker.seed(False, 20, 0)
            tracker.switch(True, 60, outdoor_temperature=-5, outdoor_expires_at=expiry)
            tracker.report_cooling_outdoor(160, 7, 360)
            self.assertEqual(tracker.heating_weather(260), expected)
            tracker.switch(False, 260, outdoor_temperature=8)
            self.assertEqual(tracker.cycle.outdoor_mean, expected)
            self.assertEqual(tracker.cycle.outdoor_end, 8)

    def test_decline_is_not_backdated_into_cooling_buckets(self):
        tracker = CurveTracker()
        tracker.seed(False, 20, 0)
        tracker.switch(True, 60)
        tracker.report_temperature(20.2, 360)
        tracker.switch(False, 660)
        tracker.report_temperature(20.4, 960)
        tracker.report_temperature(20.1, 1561)
        tracker.report_temperature(20.0, 2161)
        self.assertEqual(tracker.cycle.peak_at, 960)
        self.assertEqual(tracker.cycle.cooling_buckets[0], 0)
        self.assertEqual(tracker.cycle.cooling_buckets[1], 0)
        tracker.advance(2460)
        self.assertAlmostEqual(tracker.cycle.cooling_buckets[2], -0.3)


if __name__ == "__main__":
    unittest.main()
