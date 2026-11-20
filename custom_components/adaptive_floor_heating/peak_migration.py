"""Recover timed-out Peaks from real Recorder states; related: curve_storage.py, runtime.py."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
import logging
from math import isfinite

from .controller import temperature_celsius
from .curve_learning import CurveResult, CurveTracker, heating_outdoor_mean
from .peak_prediction import heating_outdoor_bucket

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
    return _replay_peak(row, states, _epoch(row["ended_at"]))


# Replay only measured temperatures inside the proven window; related: curve_storage.py.
def _replay_peak(row, states, end):
    analysis = row.get("analysis") or {}
    solar = analysis.get("sunrise_peak_excess") or {}
    if (analysis.get("sunrise_saving") or solar.get("first_at") is not None
            or analysis.get("peak_timeout_migration") or analysis.get("heater_outage_migration")
            or analysis.get("sunrise_saving_migration")):
        return None
    start, off = (_epoch(row[name]) for name in ("started_at", "off_at"))
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


def replay_heater_outage(row, temperatures, heater_states, classify, grace, horizon):
    """Prove a complete ON/OFF trace and a recoverable outage during coast."""
    if (row.get("accepted") or row.get("preset") != "home"
            or row.get("end_reason") not in ("HEATER_UNAVAILABLE", "PEAK_TIMEOUT", "PEAK_CONFIRMED")
            or row.get("off_reason") in ("MANUAL_STOP", "EXTERNAL_STOP", "SAFETY_STOP")
            or row.get("quality_reason") != "HEATER_UNAVAILABLE"):
        return None
    start, off, closed = (_epoch(row[name]) for name in ("started_at", "off_at", "ended_at"))
    end = min(off + horizon, datetime.now(UTC).timestamp())
    if row["end_reason"] != "HEATER_UNAVAILABLE":
        end = min(end, closed)
    samples = sorted(
        ((state.last_updated.timestamp(), classify(state), state.state) for state in heater_states
         if state.last_updated is not None and state.last_updated.timestamp() <= end),
        key=lambda sample: sample[0],
    )
    edges = []
    for sample in samples:
        if not edges or sample[1] is not edges[-1][1]:
            edges.append(sample)
    # Recorder writes the state just before the runtime timestamps the confirmed edge.
    # Related: runtime.py state events and curve_storage.py persisted cycle boundaries.
    for index, (on_at, heating, _) in enumerate(edges[1:], 1):
        if heating is True and edges[index - 1][1] is False and abs(on_at - start) <= 2:
            break
    else:
        return None
    remaining = edges[index + 1:]
    if not remaining or remaining[0][1] is not False or abs(remaining[0][0] - off) > 2:
        return None
    if len(remaining) < 3:
        return None
    lost_at, lost_state, raw = remaining[1]
    restored_at, restored_state, _ = remaining[2]
    if (lost_state is not None or raw not in ("unavailable", "unknown")
            or lost_at > closed + 2 or restored_state is not False
            or not 0 < restored_at - lost_at < grace):
        return None
    # Stop at the next unverified heater transition, including a second outage.
    if len(remaining) > 3:
        end = min(end, remaining[3][0] - 0.000001)
    outcome = _replay_peak(row, temperatures, end)
    if outcome is None or outcome[0].ended_at < lost_at:
        return None
    result, proof = outcome
    proof.update(replay_end=end, heater_outage={"lost_at": lost_at, "restored_at": restored_at,
                                               "restored_heating": False, "grace_seconds": grace})
    return result, proof


async def migrate_heater_outages(hass, store, sensor, heaters, classify, grace, horizon, report):
    """Recover verified outages under the existing manual learning lock."""
    if grace == 0:
        report["status"] = "grace_disabled"
        return 0
    if len(heaters) != 1:
        report["status"] = "multiple_heaters"
        return 0
    heater = heaters[0]
    candidates = await store.read_heater_outages()
    if not candidates:
        return 0
    try:
        from homeassistant.components.recorder import get_instance, history
        recorder = get_instance(hass)
    except (ImportError, KeyError, RuntimeError):
        report["status"] = "recorder_unavailable"
        return 0
    recovered = 0
    for row in candidates:
        try:
            start = datetime.fromisoformat(row["started_at"]) - timedelta(days=1)
            end = min(datetime.fromisoformat(row["off_at"]) + timedelta(seconds=horizon),
                      datetime.now(UTC))
            states = await recorder.async_add_executor_job(partial(
                history.get_significant_states, hass, start, end, [sensor, heater],
                include_start_time_state=False, significant_changes_only=False,
                minimal_response=False, no_attributes=False,
            ))
            outcome = replay_heater_outage(
                row, states.get(sensor, []), states.get(heater, []), classify, grace, horizon,
            )
            if outcome is not None:
                result, proof = outcome
                proof["heater_outage"]["entity_id"] = heater
                recovered += int(await store.recover_heater_outage(row, result, proof, sensor))
        except Exception as err:
            report["status"] = "partial"
            _LOGGER.warning("Heater outage migration deferred (%s)", type(err).__name__)
    report["skipped"] = len(candidates) - recovered
    return recovered


# Replay the ON interval using valid Recorder states; related: curve_learning.py, curve_storage.py.
def _outdoor_from_history(states, start, off, timeout):
    reports = sorted(
        ((state.last_updated.timestamp(), _temperature(state)) for state in states
         if state.last_updated is not None and state.last_updated.timestamp() <= off),
        key=lambda report: report[0],
    )
    if not reports:
        return None, None
    at, end = reports[-1]
    if off - at >= timeout or heating_outdoor_bucket(end) is None:
        end = None
    mean = heating_outdoor_mean(
        [(at, value, at + timeout) for at, value in reports], start, off
    )
    return mean, end


async def migrate_outdoor_weather(hass, store, sensor, timeout):
    rows = await store.outdoor_migration_rows()
    report = {"status": "complete", "recovered": 0, "skipped": 0}
    if not rows:
        return report
    if sensor is None:
        return {**report, "status": "outdoor_sensor_missing", "skipped": len(rows)}
    try:
        from homeassistant.components.recorder import get_instance, history
        recorder = get_instance(hass)
    except (ImportError, KeyError, RuntimeError):
        return {**report, "status": "recorder_unavailable", "skipped": len(rows)}

    updates = []
    for row in rows:
        started, stopped = (datetime.fromisoformat(stamp) for stamp in row[2:])
        try:
            states = await recorder.async_add_executor_job(partial(
                history.get_significant_states, hass,
                started - timedelta(seconds=timeout), stopped + timedelta(microseconds=1), [sensor],
                include_start_time_state=False, significant_changes_only=False,
                minimal_response=False, no_attributes=False,
            ))
            mean, end = _outdoor_from_history(
                states.get(sensor, []), started.timestamp(), stopped.timestamp(), timeout
            )
        except Exception as err:
            report["status"] = "partial"
            report["skipped"] += 1
            _LOGGER.warning("Outdoor history could not be read (%s)", type(err).__name__)
            continue
        if mean is None and end is None:
            report["skipped"] += 1
        else:
            updates.append((row, mean, end))
    if updates:
        report["recovered"] = await store.save_outdoor_migration(updates)
        report["skipped"] += len(updates) - report["recovered"]
    _LOGGER.info("Outdoor weather migration updated %s of %s cycles", report["recovered"], len(rows))
    return report


async def migrate_timeouts(hass, store, sensor: str, *, report: dict | None = None) -> int:
    """Read retained Recorder history in its executor and report deferred recovery.

    Related: runtime.py manual recovery and curve_storage.py atomic recovery.
    """
    candidates = await store.read_peak_timeouts()
    if not candidates:
        return 0
    try:
        from homeassistant.components.recorder import get_instance, history
        recorder = get_instance(hass)
    except (ImportError, KeyError, RuntimeError):
        if report is not None:
            report["status"] = "recorder_unavailable"
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
            if report is not None:
                report["status"] = "partial"
            # A history outage must not disable heating or discard the original cycle.
            _LOGGER.warning("Peak timeout migration deferred (%s)", type(err).__name__)
    _LOGGER.info("Peak timeout migration recovered %s of %s candidates", recovered, len(candidates))
    return recovered
