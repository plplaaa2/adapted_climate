"""Read-only outdoor prediction experiment; related: sensor.py, runtime.py, thermal_model.py."""

from math import isfinite


EXPERIMENT_KEYS = (
    "experimental_measured_slope", "experimental_outdoor_slope",
    "experimental_blended_slope", "experimental_outdoor_weight",
    "experimental_baseline_temperature", "experimental_predicted_temperature",
    "experimental_status",
)


def prediction_snapshot(runtime) -> dict:
    """Compute diagnostics without changing control, learning, timers or stored state."""
    result = dict.fromkeys(EXPERIMENT_KEYS)

    def stop(reason):
        result["experimental_status"] = reason
        return result

    if not runtime.started:
        return stop("inactive")
    now = runtime.hass.loop.time()
    temperature = runtime._temperature(runtime.hass.states.get(runtime.sensor))
    if temperature is None:
        return stop("invalid_indoor")
    samples = runtime.observation.history.samples
    if len(samples) < 3:
        return stop("insufficient_reports")
    # Allow normal sparse reports but never extrapolate an indefinitely old slope.
    interval = samples[-1].timestamp - samples[-2].timestamp
    freshness = min(3600, max(900, 2 * interval))
    if now - samples[-1].timestamp > freshness:
        return stop("stale_indoor")
    slope = runtime.observation.temperature_slope
    if slope is None or not isfinite(slope):
        return stop("insufficient_reports")
    result["experimental_measured_slope"] = slope
    if runtime.actuator.observed is not False:
        return stop("heater_not_off")
    if runtime.observation.off_at is not None:
        return stop("observing_residual")
    if slope >= -0.1:
        return stop("not_cooling")
    model = runtime.thermal_model
    delay = model.metrics["heating_response_delay"]["mean"]
    if delay is None or model.confidence("heating_response_delay") <= 0:
        return stop("missing_response_model")
    hours = delay * model.confidence("heating_response_delay") / 60
    result["experimental_baseline_temperature"] = temperature + slope * hours
    outdoor = runtime._optional_temperature(runtime.outdoor_sensor, now)
    if outdoor is None:
        return stop("missing_outdoor")
    if temperature - outdoor <= 1.5:
        return stop("insufficient_temperature_difference")
    rate = model.heat_loss_rate["mean"]
    confidence = model.heat_loss_rate_confidence
    if rate is None or confidence <= 0:
        return stop("missing_heat_loss_model")
    outdoor_slope = -rate * (temperature - outdoor)
    # Agreement is a heuristic blending weight, not validated prediction accuracy.
    agreement = min(abs(slope), abs(outdoor_slope)) / max(abs(slope), abs(outdoor_slope))
    weight = 0.5 * confidence * agreement
    blended = (1 - weight) * slope + weight * outdoor_slope
    result.update({
        "experimental_outdoor_slope": outdoor_slope,
        "experimental_blended_slope": blended,
        "experimental_outdoor_weight": weight * 100,
        "experimental_predicted_temperature": temperature + blended * hours,
    })
    return stop("ready")
