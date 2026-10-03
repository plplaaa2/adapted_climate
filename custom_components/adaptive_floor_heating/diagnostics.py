"""Read recorder-friendly OFF diagnostics without prediction or I/O; related: runtime.py, sensor.py, storage.py."""

from datetime import datetime, timezone


DIAGNOSTIC_KEYS = (
    "off_predicted_peak_existing", "off_predicted_peak_curve", "actual_peak_temperature",
    "off_prediction_error_existing", "off_prediction_error_curve", "off_prediction_confidence",
    "observation_phase", "off_prediction_status",
)
OBSERVATION_PHASES = (
    "idle", "heating_delay", "heating_response", "response_wait", "residual_rising",
    "peak_confirming", "cooling", "fault",
)
PREDICTION_STATUSES = (
    "idle", "inactive", "fault", "minimum_on", "insufficient_reports",
    "insufficient_evidence", "ready", "basic_fallback", "waiting_peak", "completed",
)


def _recorded_model(recorded: dict) -> str | None:
    """Prefer the selected OFF estimate, then its available basic fallback."""
    confidences = recorded.get("confidences", {})
    selected = recorded.get("selected_model")
    return selected if selected in confidences else "existing" if "existing" in confidences else None


def prediction_status(runtime) -> str:
    """Explain observed availability, without claiming an unmeasured failure cause."""
    if runtime.controller.faults or runtime.controller.sensor_fault:
        return "fault"
    if runtime.actuator.observed is False:
        if runtime.observation.off_at is not None:
            return "waiting_peak" if runtime._off_predictions else "insufficient_evidence"
        return "completed" if runtime.last_peak_comparison is not None else "idle"
    if runtime.controller.mode != "auto":
        return "inactive"
    if runtime.actuator.observed is not True:
        return "idle"
    if runtime.off_prediction is not None:
        return "basic_fallback" if (
            runtime.learning_model == "curve" and runtime.off_prediction["model"] == "existing"
        ) else "ready"
    now = runtime.hass.loop.time()
    if now - runtime.actuator.changed_at < runtime.controller.settings.minimum_on_time:
        return "minimum_on"
    if runtime.observation.heating_profile(now, runtime.controller.settings.sensor_timeout) is None:
        return "insufficient_reports"
    return "insufficient_evidence"


def diagnostic_snapshot(runtime) -> dict:
    """Retain last confirmed OFF forecasts and compare only completed cycles."""
    recorded = runtime.last_off_prediction or {}
    comparison = runtime.last_peak_comparison or {}
    live = runtime.off_prediction if runtime.actuator.observed is True else None
    confidences = recorded.get("confidences", {})
    selected = _recorded_model(recorded)
    confidence = (live["confidence"] if live is not None else confidences.get(selected))
    predictions, errors = recorded.get("predictions", {}), comparison.get("errors", {})
    return {
        "off_predicted_peak_existing": predictions.get("existing"),
        "off_predicted_peak_curve": predictions.get("curve"),
        "actual_peak_temperature": comparison.get("actual_peak"),
        "off_prediction_error_existing": errors.get("existing"),
        "off_prediction_error_curve": errors.get("curve"),
        "off_prediction_confidence": confidence * 100 if confidence is not None else None,
        "observation_phase": "fault" if runtime.controller.faults or runtime.controller.sensor_fault else runtime.observation.phase,
        "off_prediction_status": prediction_status(runtime),
    }


def diagnostic_attributes(runtime, key: str) -> dict:
    """Attach stable cycle metadata so graphs do not pair different heating cycles."""
    recorded = runtime.last_off_prediction or {}
    comparison = runtime.last_peak_comparison or {}
    is_comparison = key == "actual_peak_temperature" or key.startswith("off_prediction_error_")
    source = (comparison if is_comparison else {}
              if key in ("observation_phase", "off_prediction_status") else recorded)
    attributes = {}
    for field in ("off_at", "peak_at", "completed_at"):
        if source.get(field) is not None:
            attributes[field] = datetime.fromtimestamp(source[field], timezone.utc).isoformat()
    if key == "off_prediction_confidence":
        live = runtime.off_prediction if runtime.actuator.observed is True else None
        attributes["source"] = "live" if live is not None else "last_confirmed_off"
        attributes["model"] = live["model"] if live is not None else _recorded_model(recorded)
        attributes["selected_model"] = runtime.learning_model if live is not None else recorded.get("selected_model")
        if live is not None:
            attributes.pop("off_at", None)
    if is_comparison:
        model = key.removeprefix("off_prediction_error_")
        if model in comparison.get("predictions", {}):
            attributes["predicted_peak"] = comparison["predictions"][model]
            attributes["actual_peak"] = comparison["actual_peak"]
        attributes["error_convention"] = "predicted_minus_actual"
    if key.startswith("off_predicted_peak_"):
        model = key.removeprefix("off_predicted_peak_")
        attributes["confidence"] = recorded.get("confidences", {}).get(model)
    if key == "off_prediction_status":
        attributes["selected_model"] = runtime.learning_model
        attributes["curve_fallback_reason"] = runtime.curve_fallback_reason
        attributes["control_state"] = runtime.decision.state
    return attributes
