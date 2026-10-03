"""Predict measured post-OFF responses; related: thermal_model.py, curve_learning.py, runtime.py."""

from dataclasses import dataclass
from math import isfinite, sqrt

MAX_OFF_PROFILES = 24
MIN_OFF_MATCHES = 3
CONTEXT_KEYS = {"start_temperature", "on_delta", "pre_slope", "off_minutes"}


def valid_context(context: object) -> bool:
    """Validate thermal-state evidence; related: history.py, curve_memory.py."""
    if not isinstance(context, dict) or set(context) != CONTEXT_KEYS:
        return False
    for key, value in context.items():
        if value is None and key in ("pre_slope", "off_minutes"):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            return False
        limits = {"start_temperature": (-10, 50), "on_delta": (-10, 10),
                  "pre_slope": (-10, 10), "off_minutes": (0, 1440)}
        if not limits[key][0] <= value <= limits[key][1]:
            return False
    return True


def match_state(profile, elapsed, slope, context=None):
    """Match measured OFF conditions without dividing by near-zero slopes; related: thermal_model.py."""
    duration_error = abs(profile["duration"] - elapsed)
    if duration_error > max(10.0, elapsed * 0.35):
        return None
    delayed = slope <= 0.1
    if delayed != (profile["slope"] <= 0.1):
        return None
    if delayed:
        if abs(slope - profile["slope"]) > 0.2:
            return None
        scale, slope_error = 1.0, abs(slope - profile["slope"]) / 0.2
    else:
        scale = slope / profile["slope"]
        if not 0.7 <= scale <= 1.3:
            return None
        slope_error = abs(scale - 1) * 3
    weight = 1 / (1 + duration_error / 30 + slope_error)
    previous = profile.get("context")
    if delayed and (not valid_context(previous) or not valid_context(context)):
        return None
    if previous is not None and context is not None:
        if not valid_context(previous) or not valid_context(context):
            return None
        for key, tolerance in (("start_temperature", 1.0), ("on_delta", 0.3),
                               ("pre_slope", 0.3), ("off_minutes", 30.0)):
            left, right = previous[key], context[key]
            if left is None or right is None:
                if left != right:
                    return None
                weight *= 0.75
                continue
            if key == "off_minutes":
                tolerance = max(tolerance, min(left, right) * 0.5)
            error = abs(left - right)
            if error > tolerance:
                return None
            weight /= 1 + error / tolerance
    return weight, scale


@dataclass(frozen=True)
class OffPrediction:
    """Relative OFF trajectory, peak rise and evidence quality; times are minutes."""

    rise: float
    peak_minutes: float
    confidence: float
    points: tuple[tuple[float, float], ...]


def valid_profile(profile: object) -> bool:
    """Validate durable observations, including zero-rise plateaus, before prediction."""
    if not isinstance(profile, dict) or set(profile) not in (
            {"duration", "slope", "rise", "points"},
            {"duration", "slope", "rise", "points", "context"}):
        return False
    if "context" in profile and not valid_context(profile["context"]):
        return False
    for key in ("duration", "slope", "rise"):
        value = profile[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            return False
    if not 10 <= profile["duration"] <= 1440 or not 0 <= profile["rise"] <= 10:
        return False
    if not -10 <= profile["slope"] <= 10:
        return False
    points = profile["points"]
    if not isinstance(points, (list, tuple)) or not 1 <= len(points) <= 145:
        return False
    previous = -1.0
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return False
        minute, rise = point
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in point):
            return False
        if not previous < minute <= 180 or not -3 <= rise <= profile["rise"] + 0.001:
            return False
        previous = minute
    return (tuple(points[0]) == (0, 0)
            and (points[-1][0] > 0 or (len(points) == 1 and profile["rise"] == 0))
            and abs(points[-1][1] - profile["rise"]) <= 0.001)


def interpolate(points, minute: float) -> float:
    """Interpolate only observed responses; retain the peak beyond a completed trace."""
    for left, right in zip(points, points[1:]):
        if minute <= right[0]:
            part = max(0.0, (minute - left[0]) / (right[0] - left[0]))
            return left[1] + part * (right[1] - left[1])
    return points[-1][1]


def predict_off(profiles, elapsed: float, slope: float | None, *, trajectory: bool,
                context: dict | None = None) -> OffPrediction | None:
    """Use comparable OFF states, never extrapolate an ON trace as an OFF response."""
    if (slope is None or not isfinite(slope) or not -10 <= slope <= 10
            or not isfinite(elapsed) or not 10 <= elapsed <= 1440):
        return None
    matches = []
    for profile in profiles:
        if not valid_profile(profile):
            continue
        # Refuse unsupported operating states instead of clipping arbitrary extrapolation.
        # Related: runtime.py fallback to the default model/hysteresis.
        match = match_state(profile, elapsed, slope, context)
        if match is None:
            continue
        weight, ratio = match
        matches.append((profile, weight, ratio))
    if len(matches) < MIN_OFF_MATCHES:
        return None
    total = sum(weight for _, weight, _ in matches)
    peak_minutes = sum(p["points"][-1][0] * w for p, w, _ in matches) / total
    if trajectory:
        times = sorted({0.0, *(float(t) for p, _, _ in matches for t, _ in p["points"])})
        points = tuple((t, sum(interpolate(p["points"], t) * ratio * w
                               for p, w, ratio in matches) / total) for t in times)
        rise = max(value for _, value in points)
        peak_minutes = next(t for t, value in reversed(points) if abs(value - rise) < 1e-9)
    elif slope > 0.1:
        # Equivalent response time is learned from OFF rise / OFF slope (hours).
        # A linear continuation time is not assumed equal to time-to-peak.
        hours = sum(p["rise"] / p["slope"] * w for p, w, _ in matches) / total
        rise = slope * hours
        points = ((0.0, 0.0),) if peak_minutes == 0 else ((0.0, 0.0), (peak_minutes, rise))
    else:
        # Delayed response uses measured rise directly, never a zero/negative slope ratio.
        # Related: history.py thermal context and runtime.py predictive cutoff.
        rise = sum(p["rise"] * w for p, w, _ in matches) / total
        points = ((0.0, 0.0),) if peak_minutes == 0 else ((0.0, 0.0), (peak_minutes, rise))
    estimates = [p["rise"] * ratio for p, _, ratio in matches]
    mean = sum(v * w for v, (_, w, _) in zip(estimates, matches)) / total
    variance = sum(w * (v - mean) ** 2 for v, (_, w, _) in zip(estimates, matches)) / total
    confidence = min(len(matches) / 8, 1.0) / (1 + sqrt(variance) / 0.5)
    if slope <= 0.1:
        confidence *= total / len(matches)
    if confidence < 0.25:
        return None
    return OffPrediction(rise, peak_minutes, confidence, points)
