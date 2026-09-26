"""Learn slow thermal estimates from complete observations; related: history.py and storage.py."""

from math import isfinite, sqrt
from typing import Any


MODEL_SCHEMA_VERSION = 1
EWMA_ALPHA = 0.2
CONFIDENCE_SAMPLE_GOAL = 10
OUTLIER_SIGMAS = 3.0

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

    def add_cycle(self, cycle) -> bool:
        """Reject invalid/outlier measurements and update accepted EWMA metrics."""
        accepted = 0
        seen = 0
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
        if accepted:
            self.accepted_cycles += 1
        elif seen:
            self.rejected_cycles += 1
        else:
            return False
        return True

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
        }

    @classmethod
    def from_snapshot(cls, data: Any) -> "ThermalLearningModel":
        """Validate the complete saved model before restoring any parameter."""
        if (not isinstance(data, dict) or data.get("schema_version") != MODEL_SCHEMA_VERSION
                or set(data) != {"schema_version", "metrics", "accepted_cycles", "rejected_cycles"}
                or not isinstance(data.get("metrics"), dict)
                or set(data["metrics"]) != set(METRIC_LIMITS)):
            raise ValueError("Unsupported thermal model schema")
        restored = cls()
        for name, (minimum, maximum, _, _) in METRIC_LIMITS.items():
            value = data["metrics"][name]
            if (not isinstance(value, dict)
                    or set(value) != {"mean", "variance", "samples", "rejected"}):
                raise ValueError("Invalid thermal model metric")
            mean, variance = value["mean"], value["variance"]
            samples, rejected = value["samples"], value["rejected"]
            if (isinstance(samples, bool) or not isinstance(samples, int) or not 0 <= samples <= 1_000_000
                    or isinstance(rejected, bool) or not isinstance(rejected, int) or not 0 <= rejected <= 1_000_000
                    or (samples == 0) != (mean is None)
                    or (samples == 0) != (variance is None)):
                raise ValueError("Invalid thermal model counts")
            if samples:
                if (isinstance(mean, bool) or not isinstance(mean, (int, float)) or not isfinite(mean)
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
        return restored
