"""Read-only diagnostic state and storage compatibility tests; related: diagnostics.py, history.py, storage.py."""

from types import SimpleNamespace
import unittest

from custom_components.adaptive_floor_heating.diagnostics import diagnostic_snapshot, prediction_status, learning_summary
from custom_components.adaptive_floor_heating.history import ThermalObservation
from custom_components.adaptive_floor_heating.storage import decode_state


class DiagnosticTests(unittest.TestCase):
    # Summary reads never forecast, mutate evidence, or treat Cooling as another heating cycle.
    # Related: diagnostics.py, climate.py, panel.js.
    def test_learning_summary_separates_coverage_from_validation(self):
        from copy import deepcopy
        rows = [dict(outdoor=None, completed=100), dict(outdoor=-5, completed=200),
                dict(outdoor=10, completed=300)]
        model = SimpleNamespace(peak_samples=rows, accepted={'WARM_HEATING': 6, 'COOLING': 6},
                                rejected={'WARM_HEATING': 12, 'COOLING': 2}, last_reason='INCOMPLETE_PEAK')
        runtime = SimpleNamespace(curve_store=SimpleNamespace(model=model),
                                  additive_feedback={'bias': .2, 'errors': [-.2, .4]})
        before = deepcopy(model.__dict__)
        result = learning_summary(runtime)
        self.assertEqual(result['normal_heating_cycles'], 6)
        self.assertEqual(result['cooling_segments'], 6)
        self.assertEqual(result['excluded_heating_records'], 12)
        self.assertEqual(result['base_samples'], 3)
        self.assertEqual(result['outdoor_samples'], 2)
        self.assertEqual(result['outdoor_range'], [-5, 10])
        self.assertEqual(result['comparison_count'], 2)
        self.assertAlmostEqual(result['mean_absolute_error'], .3)
        self.assertEqual(result['last_normal_learning_at'], 300)
        self.assertEqual(model.__dict__, before)
        runtime.additive_feedback['errors'] = []
        self.assertIsNone(learning_summary(runtime)['mean_absolute_error'])
        runtime.curve_store = None
        self.assertIsNone(learning_summary(runtime))

    def test_delayed_pulse_phase_follows_actual_reports(self):
        observation = ThermalObservation()
        observation.seed_heater(False)
        self.assertEqual(observation.phase, "idle")
        observation.observe_heater(True, 0, 24)
        for minute in (10, 20, 30):
            observation.report_temperature(24, minute * 60)
        self.assertEqual(observation.phase, "heating_delay")
        observation.observe_heater(False, 1800, 24)
        self.assertEqual(observation.phase, "response_wait")
        observation.report_temperature(23.9, 2400)
        self.assertEqual(observation.phase, "response_wait")
        observation.report_temperature(24.2, 3000)
        self.assertEqual(observation.phase, "residual_rising")
        observation.report_temperature(24.7, 3600)
        observation.report_temperature(24.4, 4200)
        self.assertEqual(observation.phase, "peak_confirming")
        observation.report_temperature(24.3, 4800)
        self.assertEqual(observation.completed_cycles, 1)
        self.assertNotEqual(observation.phase, "peak_confirming")

    def test_runtime_model_prediction_status_and_fault(self):
        runtime = SimpleNamespace(
            controller=SimpleNamespace(faults=set(), sensor_fault=False, mode="auto", settings=SimpleNamespace(minimum_on_time=600, sensor_timeout=900)),
            actuator=SimpleNamespace(observed=True, changed_at=0),
            observation=ThermalObservation(), hass=SimpleNamespace(loop=SimpleNamespace(time=lambda: 300)),
            off_prediction=None, last_peak_comparison=None, curve_fallback_reason=None,
        )
        self.assertEqual(prediction_status(runtime), "minimum_on")
        runtime.hass.loop.time = lambda: 900
        self.assertEqual(prediction_status(runtime), "insufficient_evidence")
        runtime.off_prediction = {"model": "curve", "predicted_peak": 24.7}
        self.assertEqual(prediction_status(runtime), "ready")
        self.assertEqual(diagnostic_snapshot(runtime)["initial_learning_status"], "not_configured")
        runtime.controller.sensor_fault = True
        self.assertEqual(prediction_status(runtime), "fault")
        self.assertEqual(diagnostic_snapshot(runtime)["observation_status"], "fault")

    def test_learning_status_tracks_curve_collection_phase(self):
        from custom_components.adaptive_floor_heating.diagnostics import diagnostic_snapshot
        runtime = SimpleNamespace(
            curve_tracker=SimpleNamespace(cycle=None), curve_store=None, _curve_storage_failed=False,
            last_peak_comparison=None, off_prediction=None, actuator=SimpleNamespace(observed=False),
            controller=SimpleNamespace(faults=set(), sensor_fault=False, mode="off"),
            observation=SimpleNamespace(phase="idle", off_at=None),
        )
        self.assertEqual(diagnostic_snapshot(runtime)["learning_status"], "idle")
        runtime.curve_tracker.cycle = SimpleNamespace(off_at=None, peak_at=None)
        self.assertEqual(diagnostic_snapshot(runtime)["learning_status"], "heating")
        runtime.curve_tracker.cycle.off_at = 120
        self.assertEqual(diagnostic_snapshot(runtime)["learning_status"], "post_heat_rise")
        runtime.curve_tracker.cycle.peak_at = 180
        self.assertEqual(diagnostic_snapshot(runtime)["learning_status"], "cooling")

    def test_optional_forecast_corruption_preserves_valid_intent(self):
        intent = {"schema_version": 1, "mode": "auto", "target": 23, "faults": []}
        record = {"off_at": 1790982000, "selected_model": "curve",
                  "predictions": {"existing": 24.7}, "confidences": {"existing": 0.8}}
        migrated = decode_state({**intent, "last_off_prediction": record})["last_off_prediction"]
        self.assertIsNone(migrated["confidences"]["existing"])
        self.assertEqual(migrated["comparison_counts"], {"existing": 0})
        self.assertIsNone(decode_state(intent)["last_off_prediction"])
        unvalidated = {**record, "confidences": {"existing": None}, "comparison_counts": {"existing": 0}}
        self.assertEqual(decode_state({**intent, "last_off_prediction": unvalidated})["last_off_prediction"], unvalidated)
        for invalid in ([], {**record, "off_at": float("nan")}, {**record, "off_at": 1e99},
                        {**record, "selected_model": "other"}, {**record, "confidences": {}},
                        {**record, "confidences": {"existing": True}},
                        {**record, "confidences": {"existing": 1.1}},
                        {**record, "predictions": {"existing": float("inf")}}):
            with self.subTest(invalid=invalid), self.assertLogs(level="WARNING"):
                restored = decode_state({**intent, "last_off_prediction": invalid})
            self.assertEqual(restored["mode"], "auto")
            self.assertEqual(restored["target"], 23)
            self.assertIsNone(restored["last_off_prediction"])

    def test_legacy_comparison_and_optional_wall_time_metadata(self):
        intent = {"schema_version": 1, "mode": "heat", "target": 23, "faults": []}
        comparison = {"actual_peak": 24.7, "predictions": {"existing": 24.6}, "errors": {"existing": -0.1}}
        self.assertEqual(decode_state({**intent, "last_peak_comparison": comparison})["last_peak_comparison"], comparison)
        timed = {**comparison, "off_at": 1790982000, "peak_at": 1790988000, "completed_at": 1790988600}
        self.assertEqual(decode_state({**intent, "last_peak_comparison": timed})["last_peak_comparison"], timed)
        scored = {**timed, "confidences": {"existing": None}, "comparison_counts": {"existing": 0}}
        self.assertEqual(decode_state({**intent, "last_peak_comparison": scored})["last_peak_comparison"], scored)
        restored = decode_state({**intent, "last_peak_comparison": {**timed, "off_at": float("nan")}})
        self.assertEqual(restored["mode"], "heat")
        self.assertNotIn("off_at", restored["last_peak_comparison"])

    def test_recent_prediction_errors_are_bounded_and_legacy_state_migrates(self):
        intent = {"schema_version": 1, "mode": "auto", "target": 23, "faults": []}
        self.assertEqual(decode_state(intent)["prediction_errors"], {"curve": [], "existing": []})
        legacy = {**intent, "last_peak_comparison": {
            "actual_peak": 24.2, "predictions": {"curve": 23.72}, "errors": {"curve": -0.48},
        }}
        self.assertEqual(decode_state(legacy)["prediction_errors"]["curve"], [-0.48])
        state = decode_state({**intent, "prediction_errors": {"curve": [-0.48], "existing": [0.2]}})
        self.assertEqual(state["prediction_errors"], {"curve": [-0.48], "existing": [0.2]})
        with self.assertLogs(level="WARNING"):
            state = decode_state({**intent, "prediction_errors": {"curve": [float("nan")], "existing": []}})
        self.assertEqual(state["prediction_errors"], {"curve": [], "existing": []})
