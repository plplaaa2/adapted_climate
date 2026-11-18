"""Recover timed-out Peaks from real Recorder states; related: curve_storage.py, runtime.py."""

from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
import logging
from math import isfinite

from .controller import temperature_celsius
from .curve_learning import CurveResult, CurveTracker

_LOGGER = logging.getLogger(__name__)


def _epoch(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Missing observation timezone")
    return parsed.timestamp()


def _temperature(state) -> float | None:
    return temperature_celsius(state.state, state.attributes.get("unit_of_measurement"),
                               state.attributes.get("device_class"))


def replay_timeout(row: dict, states: list) -> tuple[CurveResult, dict] | None:
    """Use original observation boundaries, never five-minute held bins as sensor reports.

    Related: history.py PeakTracker; outside the original interval, manual/safety boundaries are unknown.
    """
    if (row.get("end_reason") != "PEAK_TIMEOUT" or row.get("accepted")
            or row.get("quality_reason") != "INCOMPLETE_PEAK" or row.get("preset") != "home"):
        return None
    analysis = row.get("analysis") or {}
    solar = analysis.get("sunrise_peak_excess") or {}
    if (analysis.get("sunrise_saving") or solar.get("first_at") is not None
            or analysis.get("peak_timeout_migration")):
        return None
    start, off, end = (_epoch(row[name]) for name in ("started_at", "off_at", "ended_at"))
    if not start < off < end:
        return None
    if any(type(row.get(name)) not in (int, float) or not isfinite(row[name])
           for name in ("start_temperature", "off_temperature")):
        return None
    reports = sorted((state.last_updated.timestamp(), _temperature(state)) for state in states
                     if state.last_updated is not None and state.last_updated.timestamp() <= end)
    # An actual pre-ON record proves retained source coverage; do not use Recorder's synthetic start state.
    prior = [(at, value) for at, value in reports if at <= start]
    if not prior or prior[-1][1] is None or abs(prior[-1][1] - row["start_temperature"]) > .051:
        return None
    # Cover the whole ON trace even when it is longer than the original post-OFF horizon.
    tracker = CurveTracker(max_peak_wait_seconds=(end - start) + 1)
    tracker.seed(False, row["start_temperature"], start)
    tracker.set_preset(row["preset"], start)
    tracker.switch(True, start, start_reason=row["start_reason"], mode=row["mode"],
                   outdoor_temperature=row.get("outdoor_temperature"))
    previous_at = start
    for at, temperature in reports:
        if at <= start or at > off:
            continue
        if temperature is None or at <= previous_at:
            return None
        tracker.report_temperature(temperature, at)
        previous_at = at
    if tracker.temperature is None or abs(tracker.temperature - row["off_temperature"]) > .051:
        return None
    # The recorded OFF temperature is a measured boundary, not a fabricated extra temperature report.
    tracker.advance(off)
    tracker.temperature = row["off_temperature"]
    tracker.switch(False, off, off_reason=row["off_reason"],
                   target_peak_temperature=row.get("target_peak_temperature"),
                   sunrise_peak_excess=dict(solar) if solar else None,
                   response_observed=(True if row.get("heating_lag_minutes") is not None else None))
    report_count = 0
    for at, temperature in reports:
        if at <= off:
            continue
        if temperature is None or at <= previous_at:
            return None
        previous_at = at
        report_count += 1
        tracker.report_temperature(temperature, at)
        result = next((item for item in tracker.take_results() if item.curve_type != "COOLING"), None)
        if result is None:
            continue
        if result.end_reason != "PEAK_CONFIRMED" or result.invalid_reason is not None:
            return None
        result = replace(result, cycle_id=row["id"], curve_type=row["curve_type"],
                         outdoor_temperature=row.get("outdoor_temperature"))
        # Recover only the matching original forecast, not a prediction recomputed with today's model.
        original = analysis.get("prediction_comparison") or {}
        forecast = original.get("predictions", {}).get("curve", solar.get("predicted_peak"))
        comparison = None
        if type(forecast) in (int, float) and isfinite(forecast):
            comparison = {
                "off_at": off, "peak_at": result.peak_at, "completed_at": result.ended_at,
                "actual_peak": result.peak_temperature, "predictions": {"curve": forecast},
                "errors": {"curve": forecast - result.peak_temperature},
            }
            for key in ("off_model", "off_learning_source", "hybrid_summary_peak"):
                if key in original:
                    comparison[key] = original[key]
        return result, {"comparison": comparison, "report_count": report_count,
                        "source_reports": [[at, value] for at, value in reports
                                           if prior[-1][0] <= at <= result.ended_at]}
    return None


async def migrate_timeouts(hass, store, sensor: str) -> int:
    """Read Recorder in its executor before curve cleanup; retry missing history on a later startup.

    Related: runtime.py startup and curve_storage.py atomic recovery; physical devices are never controlled.
    """
    candidates = await store.read_peak_timeouts()
    if not candidates:
        return 0
    try:
        from homeassistant.components.recorder import get_instance, history
        recorder = get_instance(hass)
    except (ImportError, KeyError, RuntimeError):
        _LOGGER.info("Peak timeout migration deferred: Recorder is unavailable")
        return 0
    recovered = 0
    for row in candidates:
        try:
            start = datetime.fromisoformat(row["started_at"]) - timedelta(days=1)
            end = datetime.fromisoformat(row["ended_at"]) + timedelta(microseconds=1)
            states = await recorder.async_add_executor_job(partial(
                history.get_significant_states, hass, start, end, [sensor],
                include_start_time_state=False, significant_changes_only=False,
                minimal_response=False, no_attributes=False,
            ))
            outcome = replay_timeout(row, states.get(sensor, []))
            if outcome is None:
                continue
            result, evidence = outcome
            if await store.recover_peak_timeout(row, result, evidence, sensor):
                recovered += 1
        except Exception as err:
            # A history outage must not disable heating or discard the original cycle.
            _LOGGER.warning("Peak timeout migration deferred (%s)", type(err).__name__)
    _LOGGER.info("Peak timeout migration recovered %s of %s candidates", recovered, len(candidates))
    return recovered
