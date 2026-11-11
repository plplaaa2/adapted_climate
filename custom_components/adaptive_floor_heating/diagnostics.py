"""Expose phase-only learning, observation and prediction states; related: sensor.py, runtime.py."""

DIAGNOSTIC_KEYS = (
    "learning_status", "observation_status", "prediction_status",
)


def learning_summary(runtime) -> dict | None:
    """Expose read-only training coverage separately from measured accuracy; related: climate.py, panel.js."""
    model = getattr(getattr(runtime, "curve_store", None), "model", None)
    if model is None:
        return None
    rows = list(getattr(model, "peak_samples", []))[-60:]
    outdoor = [row["outdoor"] for row in rows if row["outdoor"] is not None]
    # Report the active algorithm's validation, or the primary model while idle; related: runtime.py.
    active = getattr(runtime, "active_off_model", None) or "hybrid"
    if active == "runtime":
        errors = list(getattr(runtime, "prediction_errors", {}).get("curve", []))
    else:
        errors = list(getattr(runtime, "hybrid_feedback", {}).get("errors", []))
    accepted, rejected = getattr(model, "accepted", {}), getattr(model, "rejected", {})
    return {"normal_heating_cycles": sum(accepted.get(kind, 0) for kind in ("WARM_HEATING", "PREDICTIVE_WARM_HEATING")),
            "cooling_segments": accepted.get("COOLING", 0),
            "excluded_heating_records": sum(rejected.get(kind, 0) for kind in ("WARM_HEATING", "PREDICTIVE_WARM_HEATING")),
            "base_samples": len(rows), "base_minimum": 3,
            "outdoor_samples": len(outdoor), "outdoor_minimum": 4,
            "outdoor_range": [min(outdoor), max(outdoor)] if outdoor else None,
            "reaction_samples": len(rows), "reaction_minimum": 4,
            "comparison_count": len(errors),
            "mean_absolute_error": sum(abs(error) for error in errors)/len(errors) if errors else None,
            "last_normal_learning_at": max((row["completed"] for row in rows), default=None),
            "last_quality_reason": getattr(model, "last_reason", None)}
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
    now = runtime.hass.loop.time()
    if now - runtime.actuator.changed_at < runtime.controller.settings.minimum_on_time:
        return "minimum_on"
    if runtime.off_prediction is not None:
        return "ready"
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
    """Return normal-operation phases without initial-learning placeholders; related: sensor.py, panel.js."""
    fault = runtime.controller.faults or runtime.controller.sensor_fault
    return {
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
