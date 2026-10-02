"""Predict measured post-OFF responses; related: thermal_model.py, curve_learning.py, runtime.py."""

from dataclasses import dataclass
from math import isfinite, sqrt

MAX_OFF_PROFILES = 24
MIN_OFF_MATCHES = 3


@dataclass(frozen=True)
class OffPrediction:
    """Relative OFF trajectory, peak rise and evidence quality; times are minutes."""

    rise: float
    peak_minutes: float
    confidence: float
    points: tuple[tuple[float, float], ...]


def valid_profile(profile: object) -> bool:
    """Validate durable observations, including zero-rise plateaus, before prediction."""
    if not isinstance(profile, dict) or set(profile) != {"duration", "slope", "rise", "points"}:
        return False
    for key in ("duration", "slope", "rise"):
        value = profile[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            return False
    if not 10 <= profile["duration"] <= 1440 or not 0 <= profile["rise"] <= 10:
        return False
    if not 0.1 < profile["slope"] <= 10:
        return False
    points = profile["points"]
    if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= 145:
        return False
    previous = -1.0
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return False
        minute, rise = point
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in point):
            return False
        if not previous < minute <= 180 or not -0.05 <= rise <= profile["rise"] + 0.001:
            return False
        previous = minute
    return (tuple(points[0]) == (0, 0) and points[-1][0] > 0
            and abs(points[-1][1] - profile["rise"]) <= 0.001)


def interpolate(points, minute: float) -> float:
    """Interpolate only observed responses; retain the peak beyond a completed trace."""
    for left, right in zip(points, points[1:]):
        if minute <= right[0]:
            part = max(0.0, (minute - left[0]) / (right[0] - left[0]))
            return left[1] + part * (right[1] - left[1])
    return points[-1][1]


def predict_off(profiles, elapsed: float, slope: float | None, *, trajectory: bool) -> OffPrediction | None:
    """Use comparable OFF states, never extrapolate an ON trace as an OFF response."""
    if (slope is None or not isfinite(slope) or not 0.1 < slope <= 10
            or not isfinite(elapsed) or not 10 <= elapsed <= 1440):
        return None
    matches = []
    for profile in profiles:
        if not valid_profile(profile):
            continue
        duration_error = abs(profile["duration"] - elapsed)
        # Refuse unsupported operating states instead of clipping arbitrary extrapolation.
        # Related: runtime.py fallback to the default model/hysteresis.
        if duration_error > max(10.0, elapsed * 0.35):
            continue
        ratio = slope / profile["slope"]
        if not 0.7 <= ratio <= 1.3:
            continue
        weight = 1 / (1 + duration_error / 30 + abs(ratio - 1) * 3)
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
    else:
        # Equivalent response time is learned from OFF rise / OFF slope (hours).
        # A linear continuation time is not assumed equal to time-to-peak.
        hours = sum(p["rise"] / p["slope"] * w for p, w, _ in matches) / total
        rise = slope * hours
        points = ((0.0, 0.0), (peak_minutes, rise))
    estimates = [p["rise"] * ratio for p, _, ratio in matches]
    mean = sum(v * w for v, (_, w, _) in zip(estimates, matches)) / total
    variance = sum(w * (v - mean) ** 2 for v, (_, w, _) in zip(estimates, matches)) / total
    confidence = min(len(matches) / 8, 1.0) / (1 + sqrt(variance) / 0.5)
    if confidence < 0.25:
        return None
    return OffPrediction(rise, peak_minutes, confidence, points)
