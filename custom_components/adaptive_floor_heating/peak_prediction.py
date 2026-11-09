"""Additive OFF-Peak forecasts and bounded feedback; related: curve_storage.py, runtime.py, storage.py, panel.js."""

from math import exp, isfinite

VERSION = "additive_v1"
HYBRID_VERSION = "hybrid_v1"
TERM_LIMIT = .3
TOTAL_LIMIT = .5


# Carry goal outcome weight through additive sample restore; related: curve_learning.py, curve_storage.py.
def sample(start, off, peak, minutes, outdoor, completed, outcome_weight=None):
    """Accept only finite completed-cycle features; caller enforces quality and confirmed Peak."""
    values = (start, off, peak, minutes, completed)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in values):
        return None
    if not (-10 <= start <= 50 and -10 <= off <= 50 and off <= peak <= off + 5
            and 10 <= minutes <= 1440 and completed >= 0):
        return None
    if outdoor is not None and (isinstance(outdoor, bool) or not isinstance(outdoor, (int, float))
                                or not isfinite(outdoor) or not -60 <= outdoor <= 60):
        outdoor = None
    row = dict(start=start, off=off, peak=peak, minutes=minutes, outdoor=outdoor, completed=completed)
    if type(outcome_weight) in (int, float) and .1 <= outcome_weight <= 1:
        row["outcome_weight"] = outcome_weight
    return row


def _weights(rows, minutes, now):
    logs = [-abs(r["minutes"] - minutes) / 30 - max(0, now-r["completed"]) / (30*86400) for r in rows]
    reference = max(logs)
    return [exp(value-reference) * row.get("outcome_weight", 1.0) for value, row in zip(logs, rows)]


def _base(rows, minutes, now):
    weights = _weights(rows, minutes, now)
    total = sum(weights)
    return sum(w*(r["peak"]-r["off"]) for w, r in zip(weights, rows))/total


# Interpolate observed five-minute duration groups without extrapolating; related: runtime.py.
def _duration_base(rows, minutes, now):
    buckets = {}
    for row in rows:
        weight = exp(-max(0, now-row["completed"]) / (30*86400)) * row.get("outcome_weight", 1.0)
        total, duration, rise = buckets.get(round(row["minutes"] / 5), (0.0, 0.0, 0.0))
        buckets[round(row["minutes"] / 5)] = (total+weight, duration+weight*row["minutes"],
                                              rise+weight*(row["peak"]-row["off"]))
    points = [(duration/weight, rise/weight) for _, (weight, duration, rise) in sorted(buckets.items()) if weight > 0]
    for (left, before), (right, after) in zip(points, points[1:]):
        if left <= minutes <= right:
            return before + (minutes-left) * (after-before) / (right-left)
    return None


def _features(rows, start, temperature, minutes, outdoor, now):
    weights = _weights(rows, minutes, now)
    expected = sum(w*(r["off"]-r["start"]) for w, r in zip(weights, rows))/sum(weights)
    measured_outdoor = [r["outdoor"] for r in rows if r["outdoor"] is not None]
    reference = sum(measured_outdoor)/len(measured_outdoor) if measured_outdoor else None
    return ((outdoor-reference)/5 if outdoor is not None and reference is not None else 0,
            (temperature-start-expected)/.5)


def forecast(rows, *, start, temperature, minutes, outdoor, now, bias=0, duration_curve=False):
    """One final Peak for control and OFF comparison; train only on earlier completed normal cycles.

    Ridge shrinkage and caps are conservative initial parameters, not validated winter physics.
    Each coefficient models residual error after the basic coast prediction; no outdoor banding is added twice.
    """
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v)
           for v in (start, temperature, minutes, now, bias)) or not 10 <= minutes <= 1440:
        return None
    if outdoor is not None and (isinstance(outdoor, bool) or not isinstance(outdoor, (int, float)) or not isfinite(outdoor)):
        outdoor = None
    rows = [r for r in rows if r["completed"] < now][-60:]
    if len(rows) < 3:
        return None
    nearby = sum(abs(r["minutes"]-minutes) <= max(10, minutes*.35) for r in rows)
    if not nearby:
        return None
    base_fn = _duration_base if duration_curve else _base
    base = base_fn(rows, minutes, now)
    if base is None:
        return None
    features = _features(rows, start, temperature, minutes, outdoor, now)
    outdoor_count = sum(r["outdoor"] is not None for r in rows)
    pairs = []
    if len(rows) >= 4:
        for index, row in enumerate(rows):
            donors = rows[:index]+rows[index+1:]
            x = _features(donors, row["start"], row["off"], row["minutes"], row["outdoor"], now)
            expected = base_fn(donors, row["minutes"], now)
            if expected is None:
                continue
            residual = row["peak"]-row["off"]-expected
            pairs.append((x, residual, row.get("outcome_weight", 1.0)))
    # Center residuals: the independently validated feedback term owns the persistent offset.
    mean = sum(weight * y for _, y, weight in pairs) / sum(weight for _, _, weight in pairs) if pairs else 0
    a = 10 + sum(weight * x[0]**2 for x, _, weight in pairs)
    b = sum(weight * x[0]*x[1] for x, _, weight in pairs)
    d = 10 + sum(weight * x[1]**2 for x, _, weight in pairs)
    u = sum(weight * x[0]*(y-mean) for x, y, weight in pairs)
    v = sum(weight * x[1]*(y-mean) for x, y, weight in pairs)
    determinant = a*d-b*b
    weather_beta, reaction_beta = (u*d-b*v)/determinant, (a*v-b*u)/determinant
    calibrated = not duration_curve or len(pairs) >= 4
    weather = max(-TERM_LIMIT, min(TERM_LIMIT, features[0]*weather_beta)) if outdoor_count >= 4 and calibrated else 0
    reaction = max(-TERM_LIMIT, min(TERM_LIMIT, features[1]*reaction_beta)) if calibrated else 0
    error = max(-.5, min(.5, bias))
    correction = max(-TOTAL_LIMIT, min(TOTAL_LIMIT, weather+reaction+error))
    peak = temperature + max(0, min(5, base+correction))
    outside_outdoor = (outdoor is not None and outdoor_count > 0 and
                       not min(r["outdoor"] for r in rows if r["outdoor"] is not None) <= outdoor
                       <= max(r["outdoor"] for r in rows if r["outdoor"] is not None))
    return {"kind": HYBRID_VERSION if duration_curve else VERSION, "temperature": temperature, "base_residual": base,
            "outdoor_correction": weather, "reaction_correction": reaction, "error_correction": error,
            "limit_correction": peak-(temperature+base+weather+reaction+error),
            "predicted_peak": peak, "sample_count": len(rows), "nearby_samples": nearby,
            "outdoor_samples": outdoor_count, "calibration_samples": len(pairs),
            "outdoor_status": ("missing" if outdoor is None else "untrained" if outdoor_count < 4 else
                               "untrained" if not calibrated else "extrapolated" if outside_outdoor else "learned"),
            "duration_extrapolated": not min(r["minutes"] for r in rows) <= minutes <= max(r["minutes"] for r in rows)}


def valid_forecast(value):
    """Validate optional durable breakdown without discarding older diagnostics; related: storage.py."""
    if not isinstance(value, dict) or value.get("kind") not in (VERSION, HYBRID_VERSION):
        return False
    numeric = ("temperature", "base_residual", "outdoor_correction", "reaction_correction",
               "error_correction", "limit_correction", "predicted_peak")
    counts = ("sample_count", "nearby_samples", "outdoor_samples", "calibration_samples")
    return (set(value) == {"kind", *numeric, *counts, "outdoor_status", "duration_extrapolated"}
            and all(not isinstance(value[k], bool) and isinstance(value[k], (int, float))
                    and isfinite(value[k]) and abs(value[k]) <= 100 for k in numeric)
            and all(type(value[k]) is int and 0 <= value[k] <= 60 for k in counts)
            and value["outdoor_status"] in ("missing", "untrained", "extrapolated", "learned")
            and type(value["duration_extrapolated"]) is bool
            and abs(sum(value[k] for k in numeric if k != "predicted_peak")-value["predicted_peak"]) < 1e-6)
