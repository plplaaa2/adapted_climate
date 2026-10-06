"""Read-only diagnostic state and storage compatibility tests; related: diagnostics.py, history.py, storage.py."""

from types import SimpleNamespace
import unittest

from custom_components.adaptive_floor_heating.diagnostics import diagnostic_snapshot, prediction_status
from custom_components.adaptive_floor_heating.history import ThermalObservation
from custom_components.adaptive_floor_heating.storage import decode_state


class DiagnosticTests(unittest.TestCase):
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

    def test_current_fallback_and_historical_confidence_are_distinct(self):
        runtime = SimpleNamespace(
            controller=SimpleNamespace(faults=set(), sensor_fault=False, mode="auto", settings=SimpleNamespace(minimum_on_time=600, sensor_timeout=900)),
            actuator=SimpleNamespace(observed=True, changed_at=0),
            observation=ThermalObservation(), hass=SimpleNamespace(loop=SimpleNamespace(time=lambda: 300)),
            off_prediction=None, learning_model="curve", last_peak_comparison=None,
            last_off_prediction={"selected_model": "curve", "confidences": {"existing": 0.8}, "predictions": {"existing": 24.7}},
        )
        self.assertEqual(prediction_status(runtime), "minimum_on")
        runtime.hass.loop.time = lambda: 900
        self.assertEqual(prediction_status(runtime), "insufficient_reports")
        runtime.off_prediction = {"model": "existing", "confidence": 0.5}
        self.assertEqual(prediction_status(runtime), "basic_fallback")
        self.assertEqual(diagnostic_snapshot(runtime)["off_prediction_confidence"], 50)
        runtime.actuator.observed = False
        self.assertEqual(diagnostic_snapshot(runtime)["off_prediction_confidence"], 80)
        runtime.controller.sensor_fault = True
        self.assertEqual(prediction_status(runtime), "fault")
        self.assertEqual(diagnostic_snapshot(runtime)["observation_phase"], "fault")

    def test_optional_forecast_corruption_preserves_valid_intent(self):
        intent = {"schema_version": 1, "mode": "auto", "target": 23, "faults": []}
        record = {"off_at": 1790982000, "selected_model": "curve",
                  "predictions": {"existing": 24.7}, "confidences": {"existing": 0.8}}
        self.assertEqual(decode_state({**intent, "last_off_prediction": record})["last_off_prediction"], record)
        self.assertIsNone(decode_state(intent)["last_off_prediction"])
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
        restored = decode_state({**intent, "last_peak_comparison": {**timed, "off_at": float("nan")}})
        self.assertEqual(restored["mode"], "heat")
        self.assertNotIn("off_at", restored["last_peak_comparison"])
