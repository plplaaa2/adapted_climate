"""Expose phase-only learning, observation and prediction states; related: sensor.py, runtime.py."""

DIAGNOSTIC_KEYS = (
    "initial_learning_status", "learning_status", "observation_status", "prediction_status",
)
INITIAL_LEARNING_STATUSES = (
    "not_configured", "scheduled", "collecting_20", "collecting_40", "collecting_60",
    "processing", "completed", "interrupted", "failed",
)
LEARNING_STATUSES = (
    "idle", "heating", "post_heat_rise", "cooling", "applied", "excluded", "storage_error",
)
OBSERVATION_STATUSES = (
    "idle", "heating_delay", "heating_response", "response_wait", "residual_rising",
    "peak_confirming", "cooling", "fault",
)
PREDICTION_STATUSES = (
    "idle", "inactive", "fault", "minimum_on", "insufficient_reports",
    "insufficient_evidence", "ready", "waiting_peak", "completed",
)


def prediction_status(runtime) -> str:
    """Report the current prediction phase without exposing numeric estimates as sensors."""
    if runtime.controller.faults or runtime.controller.sensor_fault:
        return "fault"
    if runtime.controller.mode != "auto":
        return "inactive"
    if runtime.actuator.observed is not True:
        if runtime.actuator.observed is False and runtime.observation.off_at is not None:
            return "waiting_peak"
        if runtime.actuator.observed is False and runtime.last_peak_comparison is not None:
            return "completed"
        return "idle"
    if runtime.off_prediction is not None:
        return "ready"
    now = runtime.hass.loop.time()
    if now - runtime.actuator.changed_at < runtime.controller.settings.minimum_on_time:
        return "minimum_on"
    return "insufficient_evidence"


def _learning_status(runtime) -> str:
    """Describe current curve collection or the latest actual learning result."""
    if getattr(runtime, "_curve_storage_failed", False):
        return "storage_error"
    tracker = getattr(runtime, "curve_tracker", None)
    cycle = getattr(tracker, "cycle", None)
    if cycle is not None:
        if getattr(cycle, "invalid_reason", None):
            return "excluded"
        if cycle.peak_at is not None:
            return "cooling"
        if cycle.off_at is not None:
            return "post_heat_rise"
        return "heating"
    store = getattr(runtime, "curve_store", None)
    reason = getattr(getattr(store, "model", None), "last_reason", None)
    if reason:
        return "applied" if reason == "ACCEPTED" else "excluded"
    return "idle"


def diagnostic_snapshot(runtime) -> dict:
    """Return only stage states; measured and predicted values belong to analysis data."""
    fault = runtime.controller.faults or runtime.controller.sensor_fault
    return {
        "initial_learning_status": "not_configured",
        "learning_status": _learning_status(runtime),
        "observation_status": "fault" if fault else runtime.observation.phase,
        "prediction_status": prediction_status(runtime),
    }


def diagnostic_attributes(runtime, key: str) -> dict:
    """Attach an explanation to a phase sensor without copying measurements into its value."""
    if key == "prediction_status":
        return {
            "control_state": getattr(getattr(runtime, "decision", None), "state", None),
            "reason": getattr(runtime, "curve_fallback_reason", None),
        }
    if key == "learning_status":
        store = getattr(runtime, "curve_store", None)
        model = getattr(store, "model", None)
        return {"reason": getattr(model, "last_reason", None)}
    return {}
