"""Learn slow thermal estimates from complete observations; related: history.py and storage.py."""

from math import isfinite, sqrt
from typing import Any


MODEL_SCHEMA_VERSION = 2
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
        self.heat_loss_rate = {"mean": None, "variance": None, "samples": 0, "rejected": 0}

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
        }

    @classmethod
    def from_snapshot(cls, data: Any) -> "ThermalLearningModel":
        """Validate the complete saved model before restoring any parameter."""
        if (not isinstance(data, dict)
                or data.get("schema_version") not in (1, MODEL_SCHEMA_VERSION)
                or set(data) not in (
                    {"schema_version", "metrics", "accepted_cycles", "rejected_cycles"},
                    {
                        "schema_version", "metrics", "accepted_cycles",
                        "rejected_cycles", "heat_loss_rate",
                    },
                )
                or not isinstance(data.get("metrics"), dict)
                or set(data["metrics"]) != set(METRIC_LIMITS)):
            raise ValueError("Unsupported thermal model schema")
        if (
            (data["schema_version"] == 1 and "heat_loss_rate" in data)
            or (
                data["schema_version"] == MODEL_SCHEMA_VERSION
                and "heat_loss_rate" not in data
            )
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
        return restored
