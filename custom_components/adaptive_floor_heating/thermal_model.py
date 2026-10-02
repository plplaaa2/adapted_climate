"""Learn slow thermal estimates from complete observations; related: history.py and storage.py."""

from math import isfinite, sqrt
from typing import Any

from .off_response import MAX_OFF_PROFILES, predict_off, valid_profile


MODEL_SCHEMA_VERSION = 4
EWMA_ALPHA = 0.2
CONFIDENCE_SAMPLE_GOAL = 10
OUTLIER_SIGMAS = 3.0
MAX_RESPONSE_CURVES = 24
MAX_CURVE_POINTS = 145

# (minimum, maximum, minimum outlier distance, confidence scale)
METRIC_LIMITS = {
    "heating_response_delay": (0.0, 1440.0, 90.0, 120.0),
    "heating_rate": (0.0, 10.0, 0.5, 1.5),
    "residual_rise": (0.0, 10.0, 0.75, 1.5),
    "peak_delay": (0.0, 1440.0, 90.0, 120.0),
}


class ThermalLearningModel:
    """Track per-metric EWMA means, variance, sample count, and confidence."""

    def __init__(self) -> None:
        self.metrics = {
            name: {"mean": None, "variance": None, "samples": 0, "rejected": 0}
            for name in METRIC_LIMITS
        }
        self.accepted_cycles = 0
        self.rejected_cycles = 0
        self.heat_loss_rate = {"mean": None, "variance": None, "samples": 0, "rejected": 0}
        # Keep bounded completed step responses; related: history.py, runtime.py.
        self.response_curves: list[dict[str, Any]] = []
        self.off_response_profiles: list[dict[str, Any]] = []

    def add_cycle(self, cycle) -> bool:
        """Reject invalid/outlier measurements and update accepted EWMA metrics."""
        accepted = 0
        seen = 0
        residual_accepted = False
        for name, (minimum, maximum, minimum_deviation, _) in METRIC_LIMITS.items():
            value = getattr(cycle, {
                "heating_response_delay": "response_delay_minutes",
                "heating_rate": "heating_rate_c_per_hour",
                "residual_rise": "residual_rise",
                "peak_delay": "peak_delay_minutes",
            }[name], None)
            if value is None:
                continue
            seen += 1
            state = self.metrics[name]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not isfinite(value) or not minimum <= value <= maximum):
                state["rejected"] += 1
                continue
            mean, variance = state["mean"], state["variance"]
            if state["samples"] >= 3:
                threshold = max(minimum_deviation, OUTLIER_SIGMAS * sqrt(variance))
                if abs(value - mean) > threshold:
                    state["rejected"] += 1
                    continue
            if mean is None:
                state["mean"], state["variance"] = float(value), 0.0
            else:
                delta = float(value) - mean
                state["mean"] = mean + EWMA_ALPHA * delta
                state["variance"] = (1 - EWMA_ALPHA) * (variance + EWMA_ALPHA * delta**2)
            state["samples"] += 1
            accepted += 1
            if name == "residual_rise":
                residual_accepted = True
        if residual_accepted:
            # The compact model also learns flat/short responses without a normalized ON curve.
            # Related: history.py and off_response.py; existing aggregates remain diagnostic.
            duration = getattr(cycle, "heating_duration_minutes", None)
            slope = getattr(cycle, "slope_at_off", None)
            peak = getattr(cycle, "peak_delay_minutes", None)
            profile = {"duration": duration, "slope": slope, "rise": cycle.residual_rise,
                       "points": [[0, 0], [peak, cycle.residual_rise]]}
            if valid_profile(profile):
                self.off_response_profiles.append(profile)
                del self.off_response_profiles[:-MAX_OFF_PROFILES]
            curve = self._cycle_curve(cycle)
            if curve is not None:
                self.response_curves.append(curve)
                self.response_curves = self.response_curves[-MAX_RESPONSE_CURVES:]
        if accepted:
            self.accepted_cycles += 1
        elif seen:
            self.rejected_cycles += 1
        else:
            return False
        return True

    @staticmethod
    def _cycle_curve(cycle) -> dict[str, Any] | None:
        """Normalize one complete ON trajectory by its observed ON-to-peak rise."""
        points = getattr(cycle, "response_curve", None)
        duration = getattr(cycle, "heating_duration_minutes", None)
        residual = getattr(cycle, "residual_rise", None)
        if (not isinstance(points, (tuple, list)) or not 4 <= len(points) <= MAX_CURVE_POINTS
                or duration is None or not isfinite(duration) or not 30 <= duration <= 1440
                or residual is None or not isfinite(residual) or not 0 <= residual <= 10):
            return None
        try:
            pairs = [(float(t), float(rise)) for t, rise in points]
        except (TypeError, ValueError):
            return None
        if (any(not isfinite(t) or not isfinite(rise) or abs(rise) > 10 for t, rise in pairs)
                or pairs[0] != (0.0, 0.0)
                or any(right[0] <= left[0] for left, right in zip(pairs, pairs[1:]))
                or abs(pairs[-1][0] - duration) > 0.01):
            return None
        peak_rise = pairs[-1][1] + residual
        if not 0.3 <= peak_rise <= 10:
            return None
        normalized = [[round(t, 4), round(rise / peak_rise, 5)] for t, rise in pairs]
        if any(abs(fraction) > 4 for _, fraction in normalized):
            return None
        coast = getattr(cycle, "coast_curve", None)
        coast_points = []
        if coast is not None:
            try:
                coast_points = [[float(t), float(fraction)] for t, fraction in coast]
            except (TypeError, ValueError):
                return None
            if (not 2 <= len(coast_points) <= MAX_CURVE_POINTS
                    or coast_points[0] != [0.0, 0.0]
                    or abs(coast_points[-1][1] - 1) > 0.001
                    or any(not isfinite(t) or not isfinite(fraction)
                           or not 0 <= t <= 1440 or not 0 <= fraction <= 1.1
                           for t, fraction in coast_points)
                    or any(right[0] <= left[0]
                           for left, right in zip(coast_points, coast_points[1:]))):
                return None
        slope = getattr(cycle, "slope_at_off", None)
        curvature = getattr(cycle, "curvature_at_off", None)
        slope = float(slope) if slope is not None and isfinite(slope) and abs(slope) <= 20 else None
        curvature = (
            float(curvature) if curvature is not None and isfinite(curvature)
            and abs(curvature) <= 20 else None
        )
        return {
            "duration": float(duration), "points": normalized,
            "peak_rise": float(peak_rise), "residual": float(residual),
            "slope": slope, "curvature": curvature, "coast_points": coast_points,
        }

    @staticmethod
    def _curve_fraction(curve: dict[str, Any], minute: float) -> float:
        """Interpolate a normalized curve within its observed ON interval."""
        points = curve["points"]
        if minute <= 0:
            return points[0][1]
        for left, right in zip(points, points[1:]):
            if minute <= right[0]:
                part = (minute - left[0]) / (right[0] - left[0])
                return left[1] + part * (right[1] - left[1])
        return points[-1][1]

    def predict_residual_from_curve(
        self, elapsed: float, rise: float, half_rise: float | None,
        slope: float | None, curvature: float | None,
    ) -> tuple[float, float] | None:
        """Match phase, slope and acceleration to observed OFF/coast curves."""
        if not 30 <= elapsed <= 1440 or not self.response_curves:
            return None
        matches = []
        tolerance = max(20.0, elapsed * 0.35)
        for curve in self.response_curves:
            difference = abs(curve["duration"] - elapsed)
            if difference > tolerance:
                continue
            weight = 1 / (1 + difference / 30)
            if rise >= 0.2 and half_rise is not None:
                analog_now = self._curve_fraction(curve, min(elapsed, curve["duration"]))
                if analog_now > 0.05:
                    analog_half = self._curve_fraction(curve, min(elapsed / 2, curve["duration"]))
                    weight /= 1 + 2 * abs(half_rise / rise - analog_half / analog_now)
            analog_slope = curve["slope"]
            scale = 1.0
            if slope is not None and analog_slope is not None and analog_slope > 0.1:
                weight /= 1 + abs(slope - analog_slope) / max(0.2, analog_slope)
                if slope > 0:
                    scale = max(0.7, min(1.3, slope / analog_slope))
            analog_curvature = curve["curvature"]
            if curvature is not None and analog_curvature is not None:
                weight /= 1 + abs(curvature - analog_curvature) / max(0.2, abs(analog_curvature))
            matches.append((weight, curve["residual"] * scale))
        if len(matches) < 3:
            return None
        weight_sum = sum(weight for weight, _ in matches)
        if weight_sum / len(matches) < 0.25:
            return None
        estimate = sum(weight * residual for weight, residual in matches) / weight_sum
        confidence = min(len(matches) / 8, 1.0) * min(weight_sum / len(matches) * 1.5, 1.0)
        return estimate, confidence

    def predict_off_response(self, elapsed: float, slope: float | None):
        """Learn equivalent post-OFF time from stored complete cycles; related: runtime.py."""
        profiles = self.off_response_profiles[:]
        if profiles:
            return predict_off(profiles, elapsed, slope, trajectory=False)
        for curve in self.response_curves:
            if curve["slope"] is None or not curve["coast_points"]:
                continue
            profile = {
                "duration": curve["duration"], "slope": curve["slope"],
                "rise": curve["residual"],
                "points": [[t, fraction * curve["residual"]]
                           for t, fraction in curve["coast_points"]],
            }
            if valid_profile(profile):
                profiles.append(profile)
        return predict_off(profiles, elapsed, slope, trajectory=False)

    def add_heat_loss_rate(self, value: float) -> bool:
        """Learn one valid post-heating loss coefficient without affecting AUTO."""
        state = self.heat_loss_rate
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not isfinite(value) or not 0 < value <= 1.0):
            state["rejected"] += 1
            return False
        mean, variance = state["mean"], state["variance"]
        if state["samples"] >= 3:
            threshold = max(0.02, OUTLIER_SIGMAS * sqrt(variance))
            if abs(value - mean) > threshold:
                state["rejected"] += 1
                return False
        if mean is None:
            state["mean"], state["variance"] = float(value), 0.0
        else:
            delta = float(value) - mean
            state["mean"] = mean + EWMA_ALPHA * delta
            state["variance"] = (1 - EWMA_ALPHA) * (variance + EWMA_ALPHA * delta**2)
        state["samples"] += 1
        return True

    @property
    def heat_loss_rate_confidence(self) -> float:
        """Return a bounded confidence for the independent heat-loss estimate."""
        state = self.heat_loss_rate
        if state["mean"] is None:
            return 0.0
        sample_factor = min(state["samples"] / CONFIDENCE_SAMPLE_GOAL, 1.0)
        stability = 1 / (1 + sqrt(state["variance"]) / 0.05)
        return sample_factor * stability

    def confidence(self, name: str) -> float:
        """Combine sample count and coefficient of variation into 0..1."""
        state = self.metrics[name]
        if state["mean"] is None:
            return 0.0
        _, _, _, scale = METRIC_LIMITS[name]
        sample_factor = min(state["samples"] / CONFIDENCE_SAMPLE_GOAL, 1.0)
        stability = 1 / (1 + sqrt(state["variance"]) / scale)
        return sample_factor * stability

    @property
    def overall_confidence(self) -> float:
        """Average confidence across metrics with at least one accepted sample."""
        names = [name for name, state in self.metrics.items() if state["mean"] is not None]
        if not names:
            return 0.0
        return sum(self.confidence(name) for name in names) / len(names)

    @property
    def rejected_metric_samples(self) -> int:
        """Count independently rejected measurements, including partial cycles."""
        return sum(state["rejected"] for state in self.metrics.values())

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-compatible bounded model snapshot."""
        return {
            "schema_version": MODEL_SCHEMA_VERSION,
            "metrics": {name: values.copy() for name, values in self.metrics.items()},
            "accepted_cycles": self.accepted_cycles,
            "rejected_cycles": self.rejected_cycles,
            "heat_loss_rate": self.heat_loss_rate.copy(),
            "response_curves": [
                {**curve, "points": [point.copy() for point in curve["points"]],
                 "coast_points": [point.copy() for point in curve["coast_points"]]}
                for curve in self.response_curves
            ],
            "off_response_profiles": [
                {**profile, "points": [point.copy() for point in profile["points"]]}
                for profile in self.off_response_profiles
            ],
        }

    @classmethod
    def from_snapshot(cls, data: Any) -> "ThermalLearningModel":
        """Validate the complete saved model before restoring any parameter."""
        if (not isinstance(data, dict)
                or data.get("schema_version") not in (1, 2, 3, MODEL_SCHEMA_VERSION)
                or set(data) not in (
                    {"schema_version", "metrics", "accepted_cycles", "rejected_cycles"},
                    {
                        "schema_version", "metrics", "accepted_cycles",
                        "rejected_cycles", "heat_loss_rate",
                    },
                    {
                        "schema_version", "metrics", "accepted_cycles",
                        "rejected_cycles", "heat_loss_rate", "response_curves",
                    },
                    {
                        "schema_version", "metrics", "accepted_cycles", "rejected_cycles",
                        "heat_loss_rate", "response_curves", "off_response_profiles",
                    },
                )
                or not isinstance(data.get("metrics"), dict)
                or set(data["metrics"]) != set(METRIC_LIMITS)):
            raise ValueError("Unsupported thermal model schema")
        if (
            (data["schema_version"] == 1 and "heat_loss_rate" in data)
            or (
                data["schema_version"] in (2, 3, MODEL_SCHEMA_VERSION)
                and "heat_loss_rate" not in data
            )
            or (data["schema_version"] >= 3) != ("response_curves" in data)
            or (data["schema_version"] == MODEL_SCHEMA_VERSION) != ("off_response_profiles" in data)
        ):
            raise ValueError("Invalid heat-loss model schema")
        restored = cls()
        for name, (minimum, maximum, _, _) in METRIC_LIMITS.items():
            value = data["metrics"][name]
            if (not isinstance(value, dict)
                    or set(value) != {"mean", "variance", "samples", "rejected"}):
                raise ValueError("Invalid thermal model metric")
            mean, variance = value["mean"], value["variance"]
            samples, rejected = value["samples"], value["rejected"]
            if (isinstance(samples, bool) or not isinstance(samples, int)
                    or not 0 <= samples <= 1_000_000
                    or isinstance(rejected, bool) or not isinstance(rejected, int)
                    or not 0 <= rejected <= 1_000_000
                    or (samples == 0) != (mean is None)
                    or (samples == 0) != (variance is None)):
                raise ValueError("Invalid thermal model counts")
            if samples:
                if (isinstance(mean, bool) or not isinstance(mean, (int, float))
                        or not isfinite(mean)
                        or not minimum <= mean <= maximum
                        or isinstance(variance, bool) or not isinstance(variance, (int, float))
                        or not isfinite(variance) or variance < 0):
                    raise ValueError("Invalid thermal model estimate")
            restored.metrics[name] = {
                "mean": float(mean) if samples else None,
                "variance": float(variance) if samples else None,
                "samples": samples,
                "rejected": rejected,
            }
        for key in ("accepted_cycles", "rejected_cycles"):
            count = data[key]
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1_000_000:
                raise ValueError("Invalid thermal model cycle count")
            setattr(restored, key, count)
        if "heat_loss_rate" in data:
            value = data["heat_loss_rate"]
            if (not isinstance(value, dict)
                    or set(value) != {"mean", "variance", "samples", "rejected"}):
                raise ValueError("Invalid heat-loss model metric")
            mean, variance = value["mean"], value["variance"]
            samples, rejected = value["samples"], value["rejected"]
            if (isinstance(samples, bool) or not isinstance(samples, int)
                    or not 0 <= samples <= 1_000_000
                    or isinstance(rejected, bool) or not isinstance(rejected, int)
                    or not 0 <= rejected <= 1_000_000
                    or (samples == 0) != (mean is None)
                    or (samples == 0) != (variance is None)):
                raise ValueError("Invalid heat-loss model counts")
            if samples and (
                    isinstance(mean, bool) or not isinstance(mean, (int, float))
                    or not isfinite(mean) or not 0 < mean <= 1.0
                    or isinstance(variance, bool) or not isinstance(variance, (int, float))
                    or not isfinite(variance) or variance < 0):
                raise ValueError("Invalid heat-loss model estimate")
            restored.heat_loss_rate = {
                "mean": float(mean) if samples else None,
                "variance": float(variance) if samples else None,
                "samples": samples,
                "rejected": rejected,
            }
        if "response_curves" in data:
            curves = data["response_curves"]
            if not isinstance(curves, list) or len(curves) > MAX_RESPONSE_CURVES:
                raise ValueError("Invalid response curve collection")
            for curve in curves:
                if (not isinstance(curve, dict)
                        or set(curve) != {"duration", "points", "peak_rise", "residual", "slope", "curvature", "coast_points"}
                        or not isinstance(curve["points"], list)):
                    raise ValueError("Invalid response curve")
                points = curve["points"]
                if (not 4 <= len(points) <= MAX_CURVE_POINTS
                        or any(not isinstance(point, list) or len(point) != 2 for point in points)):
                    raise ValueError("Invalid response curve points")
                values = [curve[key] for key in ("duration", "peak_rise", "residual")]
                if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                        or not isfinite(value) for value in values)
                        or not 30 <= curve["duration"] <= 1440
                        or not 0.3 <= curve["peak_rise"] <= 10
                        or not 0 <= curve["residual"] <= 10):
                    raise ValueError("Invalid response curve values")
                for key in ("slope", "curvature"):
                    value = curve[key]
                    if value is not None and (isinstance(value, bool)
                            or not isinstance(value, (int, float)) or not isfinite(value)
                            or abs(value) > 20):
                        raise ValueError("Invalid response curve derivative")
                previous = -1.0
                for minute, fraction in points:
                    if (isinstance(minute, bool) or not isinstance(minute, (int, float))
                            or not isfinite(minute) or not previous < minute <= curve["duration"]
                            or isinstance(fraction, bool) or not isinstance(fraction, (int, float))
                            or not isfinite(fraction) or abs(fraction) > 4):
                        raise ValueError("Invalid response curve point")
                    previous = minute
                if (points[0] != [0, 0]
                        or abs(points[-1][0] - curve["duration"]) > 0.01):
                    raise ValueError("Invalid response curve endpoints")
                coast = curve["coast_points"]
                if (not isinstance(coast, list) or len(coast) > MAX_CURVE_POINTS
                        or (coast and (len(coast) < 2 or coast[0] != [0, 0]
                                       or abs(coast[-1][1] - 1) > 0.001))):
                    raise ValueError("Invalid coast curve")
                previous = -1.0
                for point in coast:
                    if (not isinstance(point, list) or len(point) != 2):
                        raise ValueError("Invalid coast curve point")
                    minute, fraction = point
                    if (isinstance(minute, bool) or not isinstance(minute, (int, float))
                            or not isfinite(minute) or not previous < minute <= 1440
                            or isinstance(fraction, bool) or not isinstance(fraction, (int, float))
                            or not isfinite(fraction) or not 0 <= fraction <= 1.1):
                        raise ValueError("Invalid coast curve point")
                    previous = minute
            restored.response_curves = [
                {**curve, "points": [point.copy() for point in curve["points"]],
                 "coast_points": [point.copy() for point in curve["coast_points"]]}
                for curve in curves
            ]
        if "off_response_profiles" in data:
            profiles = data["off_response_profiles"]
            if (not isinstance(profiles, list) or len(profiles) > MAX_OFF_PROFILES
                    or any(not valid_profile(profile) for profile in profiles)):
                raise ValueError("Invalid learned OFF profiles")
            restored.off_response_profiles = [
                {**profile, "points": [point.copy() for point in profile["points"]]}
                for profile in profiles
            ]
        return restored
