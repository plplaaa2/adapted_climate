"""Track four five-minute heating curves; related: runtime.py, curve_storage.py, select.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, isfinite
from uuid import uuid4
import time

from .history import TemperatureHistory, PeakTracker
from .curve_memory import Bucket, CurveMemory, CURRENT_MAX_AGE
from .off_response import MAX_OFF_PROFILES, predict_off, valid_profile
from .const import DEFAULT_PEAK_OBSERVATION_HOURS

BUCKET_SECONDS = 300.0
NEW_CYCLE_WEIGHT = 0.2
MAX_RESIDUAL_RISE = 5.0
MAX_BUCKET_DELTA = 3.0
DEVIATION_BUCKET_DELTA = 0.35
DEVIATION_TOTAL_MIN = 0.8
DEVIATION_TOTAL_PER_BUCKET = 0.12
CURVE_TYPES = (
    "WARM_HEATING", "PREDICTIVE_WARM_HEATING", "COOLING",
)


@dataclass
class CurveCycle:
    """One observed heating episode with a separate post-peak cooling segment."""

    id: str
    started_at: float
    start_reason: str
    mode: str
    curve_type: str
    baseline: float
    last_temperature: float
    last_tick: float
    next_tick: float
    off_at: float | None = None
    off_temperature: float | None = None
    off_reason: str = "UNKNOWN"
    peak_at: float | None = None
    peak_temperature: float | None = None
    candidate_peak_at: float | None = None
    candidate_peak_temperature: float | None = None
    cooling_baseline: float | None = None
    cooling_last_temperature: float | None = None
    cooling_last_tick: float | None = None
    cooling_next_tick: float | None = None
    cooling_target_temperature: float | None = None
    cooling_previous_report_temperature: float | None = None
    cooling_previous_report_at: float | None = None
    cooling_first_rise_temperature: float | None = None
    invalid_reason: str | None = None
    heating_buckets: dict[int, float] = field(default_factory=dict)
    cooling_buckets: dict[int, float] = field(default_factory=dict)
    heating_saved: bool = False
    preset: str = "home"
    on_history: TemperatureHistory = field(default_factory=TemperatureHistory)
    off_reports: list[tuple[float, float]] = field(default_factory=list)
    slope_at_off: float | None = None
    off_context: dict | None = None
    peak_tracker: PeakTracker | None = None
    outdoor_temperature: float | None = None


@dataclass(frozen=True)
class CurveResult:
    """Persistable result from one complete curve segment."""

    cycle_id: str
    curve_type: str
    start_reason: str
    off_reason: str
    end_reason: str
    mode: str
    started_at: float
    off_at: float | None
    peak_at: float | None
    ended_at: float
    buckets: dict[int, float]
    residual_rise: float | None
    invalid_reason: str | None
    preset: str = "home"
    off_profile: dict | None = None
    # Preserve measured context for accepted and rejected segments; related: curve_storage.py.
    start_temperature: float | None = None
    off_temperature: float | None = None
    peak_temperature: float | None = None
    slope_at_off: float | None = None
    peak_confirmation_drop: float | None = None
    peak_confirmation_reports: int | None = None
    peak_confirmation_extra_drop: float | None = None
    # Outdoor temperature at the confirmed ON edge. It conditions only the
    # heater-runtime/peak model; cooling-based predictive starts stay global.
    outdoor_temperature: float | None = None


class CurveStandards:
    """Keep independent durable standard deltas for each curve and bucket."""

    def __init__(self, max_peak_wait_minutes: float = DEFAULT_PEAK_OBSERVATION_HOURS * 60) -> None:
        self.max_peak_wait_minutes = max_peak_wait_minutes
        self.buckets: dict[str, dict[int, tuple[float, int]]] = {
            curve: {} for curve in CURVE_TYPES
        }
        self.accepted: dict[str, int] = {curve: 0 for curve in CURVE_TYPES}
        self.rejected: dict[str, int] = {curve: 0 for curve in CURVE_TYPES}
        self.last_reason: str | None = None
        self.residual: dict[str, tuple[float, int]] = {}
        self.peak_delay: dict[str, tuple[float, int]] = {}
        self.off_profiles: dict[str, list[dict]] = {curve: [] for curve in CURVE_TYPES}
        self.off_profile_updated: dict[str, float] = {}
        self.memory = {curve: CurveMemory() for curve in CURVE_TYPES}
        self.prediction_source: str | None = None
        self.current_weight = 0.0
        # Predict the peak from actual heater runtime; related: curve_storage.py, runtime.py.
        # (heater minutes, eventual Peak rise, response lag, completion wall time)
        self.runtime_samples: list[tuple[float, float, float, float | None]] = []
        # Conditional samples are used only for heater runtime / OFF prediction.
        # The aggregate set remains the safe fallback.
        self.outdoor_runtime_samples: dict[str, list[tuple[float, float, float, float | None]]] = {}

    @staticmethod
    def outdoor_bucket(temperature: float | None) -> str | None:
        if temperature is None or not isinstance(temperature, (int, float)) or not isfinite(temperature):
            return None
        if temperature > 10:
            return "WARM"
        if temperature >= 0:
            return "NORMAL"
        if temperature >= -5:
            return "COLD"
        return "VERY_COLD"

    def load_runtime_samples(self, samples: list[tuple[float, ...]]) -> None:
        """Restore Peak samples, accepting legacy rows without a completion timestamp."""
        restored = []
        for sample in samples:
            if len(sample) not in (2, 3, 4, 5):
                continue
            minutes, rise = sample[:2]
            lag = sample[2] if len(sample) >= 3 else 0.0
            # Four-field rows are legacy (minutes, rise, lag, completed_at).
            outdoor = sample[3] if len(sample) == 5 else None
            completed_at = sample[4] if len(sample) == 5 else (sample[3] if len(sample) == 4 else None)
            try:
                completed_at = None if completed_at is None else float(completed_at)
                outdoor = None if outdoor is None else float(outdoor)
            except (TypeError, ValueError):
                continue
            if (isfinite(minutes) and isfinite(rise) and isfinite(lag)
                    and (outdoor is None or isfinite(outdoor))
                    and (completed_at is None or isfinite(completed_at) and completed_at >= 0)
                    and minutes > 0 and lag >= 0 and rise > 0):
                row = (float(minutes), float(rise), float(lag), completed_at)
                restored.append(row)
                bucket = self.outdoor_bucket(outdoor)
                if bucket is not None:
                    self.outdoor_runtime_samples.setdefault(bucket, []).append(row)
        self.runtime_samples = restored[-200:]
        for bucket, rows in self.outdoor_runtime_samples.items():
            self.outdoor_runtime_samples[bucket] = rows[-200:]

    @staticmethod
    def _runtime_lag(result: CurveResult) -> float:
        """Find the first sustained 0.2°C rise, suppressing isolated 0.1°C reports."""
        cumulative = 0.0
        totals = []
        for index in sorted(result.buckets):
            cumulative += result.buckets[index]
            totals.append((index, cumulative))
        for position in range(max(0, len(totals) - 2)):
            if (totals[position][1] >= 0.2
                    and min(value for _, value in totals[position:position + 3]) >= 0.1):
                return (totals[position][0] + 1) * BUCKET_SECONDS / 60
        return 0.0

    def add_runtime_sample(self, result: CurveResult) -> None:
        """Learn peak rise per confirmed heater minute from a complete warm cycle."""
        if (result.curve_type not in ("WARM_HEATING", "PREDICTIVE_WARM_HEATING")
                or result.preset == "away"
                or result.off_at is None or result.peak_at is None or result.start_temperature is None
                or result.peak_temperature is None):
            return
        minutes = (result.off_at - result.started_at) / 60
        rise = result.peak_temperature - result.start_temperature
        if isfinite(minutes) and isfinite(rise) and minutes > 0 and rise > 0:
            sample = (minutes, rise, self._runtime_lag(result), result.ended_at)
            self.runtime_samples.append(sample)
            self.runtime_samples = self.runtime_samples[-200:]
            bucket = self.outdoor_bucket(result.outdoor_temperature)
            if bucket is not None:
                rows = self.outdoor_runtime_samples.setdefault(bucket, [])
                rows.append(sample)
                del rows[:-200]

    def _runtime_samples_for(self, outdoor_temperature: float | None):
        """Use a temperature band only after three complete cycles exist."""
        bucket = self.outdoor_bucket(outdoor_temperature)
        rows = self.outdoor_runtime_samples.get(bucket, []) if bucket is not None else []
        return rows if len(rows) >= 3 else self.runtime_samples

    def _runtime_weights(self, samples=None, now: float | None = None) -> list[float]:
        """Favor recent completed cycles without discarding legacy timestamp-free samples."""
        now = time.time() if now is None else now
        weights = []
        samples = self.runtime_samples if samples is None else samples
        for index, (*_, completed_at) in enumerate(samples, start=1):
            age_weight = 1.0 if completed_at is None else exp(-max(0.0, now - completed_at) / (30 * 86400))
            weights.append(index * age_weight)
        return weights

    def _runtime_response(self, outdoor_temperature: float | None = None) -> tuple[float, float] | None:
        samples = self._runtime_samples_for(outdoor_temperature)
        if len(samples) < 3:
            return None
        weights = self._runtime_weights(samples)
        total_weight = sum(weights)
        rate = sum((rise / max(1.0, minutes - lag)) * weight
                   for weight, (minutes, rise, lag, _) in zip(weights, samples)) / total_weight
        lag = sum(sample[2] * weight
                  for weight, sample in zip(weights, samples)) / total_weight
        return (rate, lag) if isfinite(rate) and rate > 0 else None

    # Use measured duration-to-peak outcomes; related: curve_storage.py, runtime.py.
    def _runtime_curve(self, outdoor_temperature: float | None = None) -> list[tuple[float, float]]:
        """Build the measured heater-runtime to eventual-peak-rise curve with an anchored origin."""
        grouped: dict[float, list[tuple[float, float]]] = {}
        samples = self._runtime_samples_for(outdoor_temperature)
        for (minutes, rise, _lag, _), weight in zip(samples, self._runtime_weights(samples)):
            if isfinite(minutes) and isfinite(rise) and minutes > 0 and rise > 0:
                grouped.setdefault(minutes, []).append((rise, weight))
        if not grouped:
            return []
        points = []
        # Anchor at (0, 0) and (lag, 0) so short heating requests interpolate smoothly
        # instead of failing or producing steep unphysical extrapolations.
        response = self._runtime_response(outdoor_temperature)
        lag = response[1] if response is not None else 0.0
        points.append((0.0, 0.0))
        if isfinite(lag) and lag > 0 and lag < min(grouped.keys()):
            points.append((float(lag), 0.0))
        highest_rise = 0.0
        for minutes, rises in sorted(grouped.items()):
            total_weight = sum(weight for _, weight in rises)
            highest_rise = max(highest_rise, sum(rise * weight for rise, weight in rises) / total_weight)
            points.append((minutes, highest_rise))
        return points

    def _runtime_segment(self, value: float, *, inverse: bool,
                         outdoor_temperature: float | None = None) -> tuple[float, float, float, float] | None:
        """Select a positive-slope measured segment, using endpoint segments outside range."""
        points = self._runtime_curve(outdoor_temperature)
        if len(points) < 2:
            return None
        for left, right in zip(points, points[1:]):
            x0, y0 = (left[1], left[0]) if inverse else left
            x1, y1 = (right[1], right[0]) if inverse else right
            if x1 > x0 and y1 > y0 and x0 <= value <= x1:
                return x0, y0, x1, y1
        segments = []
        for left, right in zip(points, points[1:]):
            x0, y0 = (left[1], left[0]) if inverse else left
            x1, y1 = (right[1], right[0]) if inverse else right
            if x1 > x0 and y1 > y0:
                segments.append((x0, y0, x1, y1))
        if not segments:
            return None
        return segments[0] if value < segments[0][0] else segments[-1]

    def required_heating_minutes(self, start_temperature: float, target_temperature: float,
                                 outdoor_temperature: float | None = None) -> float | None:
        """Invert observed runtime/peak samples instead of assuming one constant slope."""
        if (not isfinite(start_temperature) or not isfinite(target_temperature)
                or target_temperature <= start_temperature):
            return None
        if len(self.runtime_samples) < 3:
            return None
        rise = target_temperature - start_temperature
        points = self._runtime_curve(outdoor_temperature)
        if len(points) < 2:
            return None
        segment = self._runtime_segment(rise, inverse=True, outdoor_temperature=outdoor_temperature)
        if segment is None:
            return None
        x0, y0, x1, y1 = segment
        return max(0.0, y0 + (rise - x0) * (y1 - y0) / (x1 - x0))

    def predicted_peak(self, start_temperature: float, heating_minutes: float,
                       outdoor_temperature: float | None = None) -> float | None:
        """Interpolate measured eventual peaks for the requested heater runtime."""
        if (not isfinite(start_temperature) or not isfinite(heating_minutes)
                or heating_minutes <= 0):
            return None
        if len(self.runtime_samples) < 3:
            return None
        segment = self._runtime_segment(heating_minutes, inverse=False, outdoor_temperature=outdoor_temperature)
        if segment is None:
            return None
        x0, y0, x1, y1 = segment
        rise = y0 + (heating_minutes - x0) * (y1 - y0) / (x1 - x0)
        return start_temperature + max(0.0, rise)

    def load(self, rows: list[tuple[str, int, float, int]]) -> None:
        """Restore persisted aggregate rows without recreating old raw reports."""
        for curve, index, mean, count in rows:
            if (curve in self.buckets and isinstance(index, int) and index >= 0
                    and isfinite(mean) and count > 0):
                self.buckets[curve][index] = (float(mean), int(count))
                self.memory[curve].current[index] = Bucket(float(mean))

    def assess(self, result: CurveResult) -> tuple[bool, str]:
        """Reject interrupted or strongly divergent cycles before 8:2 learning."""
        if result.invalid_reason:
            return False, result.invalid_reason
        if result.preset == "away":
            return False, "AWAY_CYCLE"
        if len(result.buckets) < 2:
            return False, "INSUFFICIENT_BUCKETS"
        if (result.curve_type != "COOLING" and
                (result.residual_rise is None or not 0 <= result.residual_rise <= MAX_RESIDUAL_RISE)):
            return False, "INVALID_RESIDUAL_RISE"
        if (result.curve_type != "COOLING" and
                (result.off_at is None or result.peak_at is None
                 or not 0 <= result.peak_at - result.off_at <= self.max_peak_wait_minutes * 60)):
            return False, "INVALID_PEAK_DELAY"
        if any(not isfinite(value) or abs(value) > MAX_BUCKET_DELTA for value in result.buckets.values()):
            return False, "IMPLAUSIBLE_FIVE_MINUTE_DELTA"
        reference = self.buckets[result.curve_type]
        comparable = [
            (value, reference[index][0]) for index, value in result.buckets.items()
            if index in reference and reference[index][1] >= 3
        ]
        # Compare both shape and total displacement; a sustained open-window drop
        # should not move the permanent standard. Related: curve_storage.py.
        if len(comparable) >= 3:
            large = sum(abs(value - expected) > DEVIATION_BUCKET_DELTA for value, expected in comparable)
            drift = abs(sum(value - expected for value, expected in comparable))
            if large >= 3 or drift > max(DEVIATION_TOTAL_MIN, len(comparable) * DEVIATION_TOTAL_PER_BUCKET):
                return False, "CURVE_DEVIATION"
        return True, "ACCEPTED"

    def quality_details(self, result: CurveResult) -> list[dict]:
        """Record the actual checks without changing acceptance; related: curve_storage.py, curve_api.py."""
        checks = [
            {"code": "OBSERVATION_VALID", "actual": result.invalid_reason,
             "expected": "no_invalid_reason", "passed": not bool(result.invalid_reason)},
            {"code": "AWAY_CYCLE", "actual": result.preset,
             "expected": "not_away", "passed": result.preset != "away"},
            {"code": "INSUFFICIENT_BUCKETS", "actual": len(result.buckets),
             "min": 2, "unit": "buckets", "passed": len(result.buckets) >= 2},
        ]
        if result.curve_type != "COOLING":
            delay = ((result.peak_at - result.off_at) / 60
                     if result.peak_at is not None and result.off_at is not None else None)
            checks.extend([
                {"code": "INVALID_RESIDUAL_RISE", "actual": result.residual_rise,
                 "min": 0, "max": MAX_RESIDUAL_RISE, "unit": "°C",
                 "passed": result.residual_rise is not None and 0 <= result.residual_rise <= MAX_RESIDUAL_RISE},
                {"code": "INVALID_PEAK_DELAY", "actual": delay,
                 "min": 0, "max": self.max_peak_wait_minutes, "unit": "minutes",
                 "passed": delay is not None and 0 <= delay <= self.max_peak_wait_minutes},
            ])
        finite = all(isfinite(value) for value in result.buckets.values())
        maximum = max((abs(value) for value in result.buckets.values() if isfinite(value)), default=None)
        checks.append({"code": "IMPLAUSIBLE_FIVE_MINUTE_DELTA", "actual": maximum,
                       "min": 0, "max": MAX_BUCKET_DELTA, "unit": "°C",
                       "nonfinite": not finite,
                       "passed": finite and (maximum is None or maximum <= MAX_BUCKET_DELTA)})
        reference = self.buckets[result.curve_type]
        compared = [{"index": index, "actual": value, "reference": reference[index][0]}
                    for index, value in result.buckets.items()
                    if index in reference and reference[index][1] >= 3]
        large = sum(abs(row["actual"] - row["reference"]) > DEVIATION_BUCKET_DELTA for row in compared)
        drift = abs(sum(row["actual"] - row["reference"] for row in compared))
        drift_limit = max(DEVIATION_TOTAL_MIN, len(compared) * DEVIATION_TOTAL_PER_BUCKET)
        checks.append({"code": "CURVE_DEVIATION", "compared": compared,
                       "comparable_count": len(compared), "large_count": large,
                       "large_count_limit": 3, "bucket_difference_limit": DEVIATION_BUCKET_DELTA,
                       "actual": drift, "max": drift_limit, "unit": "°C",
                       "applicable": len(compared) >= 3,
                       "passed": len(compared) < 3 or (large < 3 and drift <= drift_limit)})
        return checks

    def add_off_profile(self, result: CurveResult) -> None:
        """Retain bounded complete OFF trajectories independently of raw retention; related: curve_storage.py."""
        if valid_profile(result.off_profile, max_peak_wait_minutes=self.max_peak_wait_minutes):
            profiles = self.off_profiles[result.curve_type]
            profiles.append(result.off_profile)
            del profiles[:-MAX_OFF_PROFILES]
            self.off_profile_updated[result.curve_type] = result.ended_at

    def predict_off_response(self, curve_type: str, elapsed: float, slope: float | None,
                             now: float | None = None, *, context: dict | None = None,
                             max_peak_wait_minutes: float | None = None):
        """Blend condition-matched Current/Long-term OFF responses; related: curve_memory.py."""
        max_peak_wait_minutes = (self.max_peak_wait_minutes if max_peak_wait_minutes is None
                                 else max_peak_wait_minutes)
        self.prediction_source = None
        self.current_weight = 0.0
        candidates = (curve_type, "WARM_HEATING") if curve_type == "PREDICTIVE_WARM_HEATING" else (curve_type,)
        for kind in candidates:
            memory = self.memory[kind]
            if memory.responses:
                prediction = memory.predict_response(
                    elapsed, slope, now, context=context,
                    max_peak_wait_minutes=max_peak_wait_minutes,
                )
                source, weight = memory.last_source, memory.last_weight
            else:
                # Compatible schema-2 profiles have observed dispersion, unlike old scalar rows.
                updated = self.off_profile_updated.get(kind)
                clock_now = time.time() if now is None else now
                prediction = (None if updated is not None and clock_now - updated >= CURRENT_MAX_AGE else
                              predict_off(self.off_profiles.get(kind, []), elapsed, slope,
                                          trajectory=True, context=context,
                                          max_peak_wait_minutes=max_peak_wait_minutes))
                source, weight = "current_legacy_profiles", 1.0
            if prediction is not None:
                self.prediction_source = f"{kind}:{source}"
                self.current_weight = weight
                return prediction
        return None

    def predict_cooling_delta(self, elapsed_minutes: float, horizon_minutes: float) -> float | None:
        """Integrate only continuously covered learned cooling buckets; related: runtime.py."""
        if (not isfinite(elapsed_minutes) or not isfinite(horizon_minutes)
                or elapsed_minutes < 0 or horizon_minutes <= 0):
            return None
        end = elapsed_minutes + horizon_minutes
        position = elapsed_minutes
        delta = 0.0
        while position < end:
            index = int(position / 5)
            row = self.estimate("COOLING", index)
            if row is None or row[1] < 3 or not isfinite(row[0]):
                return None
            next_position = min(end, (index + 1) * 5)
            delta += row[0] * (next_position - position) / 5
            position = next_position
        return delta if delta < 0 else None

    def apply(self, result: CurveResult) -> tuple[bool, str]:
        """Apply one accepted cycle once the storage transaction has accepted it."""
        accepted, reason = self.assess(result)
        if accepted:
            self.add_off_profile(result)
            standard = self.buckets[result.curve_type]
            for index, value in result.buckets.items():
                previous = standard.get(index)
                self.memory[result.curve_type].current.setdefault(index, Bucket(previous[0] if previous else value))
                mean = value if previous is None else (
                    previous[0] * (1 - NEW_CYCLE_WEIGHT) + value * NEW_CYCLE_WEIGHT
                )
                standard[index] = (mean, 1 if previous is None else previous[1] + 1)
            memory = self.memory[result.curve_type]
            memory.learn_buckets(result.buckets, result.ended_at)
            if valid_profile(result.off_profile, max_peak_wait_minutes=self.max_peak_wait_minutes):
                memory.learn_response(result.off_profile, result.ended_at)
            self.accepted[result.curve_type] += 1
            if result.residual_rise is not None:
                previous = self.residual.get(result.curve_type)
                mean = (result.residual_rise if previous is None else
                        previous[0] * (1 - NEW_CYCLE_WEIGHT)
                        + result.residual_rise * NEW_CYCLE_WEIGHT)
                self.residual[result.curve_type] = (mean, 1 if previous is None else previous[1] + 1)
                delay = (result.peak_at - result.off_at) / 60
                old_delay = self.peak_delay.get(result.curve_type)
                delay_mean = (delay if old_delay is None else
                              old_delay[0] * (1 - NEW_CYCLE_WEIGHT) + delay * NEW_CYCLE_WEIGHT)
                self.peak_delay[result.curve_type] = (
                    delay_mean, 1 if old_delay is None else old_delay[1] + 1
                )
        else:
            self.rejected[result.curve_type] += 1
        self.last_reason = reason
        return accepted, reason

    def estimate(self, curve_type: str, index: int) -> tuple[float, int] | None:
        """Return a matching bucket, with Predictive Warm falling back to Warm."""
        memory = self.memory[curve_type]
        if index in memory.current or index in memory.long_term:
            prediction = memory.estimate(index)
            row = None if prediction is None else (prediction[0], 3)
            if prediction is not None:
                self.prediction_source = f"{curve_type}:{prediction[2]}"
                self.current_weight = prediction[3]
        else:
            row = self.buckets.get(curve_type, {}).get(index)
        if (row is None or row[1] < 3) and curve_type == "PREDICTIVE_WARM_HEATING":
            row = self.estimate("WARM_HEATING", index)
        return row

    def response_delay_minutes(self, curve_type: str) -> float | None:
        """Find the first sufficiently learned positive heating bucket."""
        indices = set(self.buckets.get(curve_type, {})) | set(self.memory[curve_type].long_term)
        for index in range(max(indices, default=-1) + 1):
            row = self.estimate(curve_type, index)
            if row is None:
                break
            mean, count = row
            if count >= 3 and mean >= 0.05:
                return (index + 1) * BUCKET_SECONDS / 60
        if curve_type == "PREDICTIVE_WARM_HEATING":
            return self.response_delay_minutes("WARM_HEATING")
        return None

    def residual_rise(self, curve_type: str) -> float | None:
        """Only the measured OFF-to-peak rise can justify an early heater stop."""
        value = self.residual.get(curve_type)
        if (value is None or value[1] < 3) and curve_type == "PREDICTIVE_WARM_HEATING":
            return self.residual_rise("WARM_HEATING")
        return value[0] * min(1.0, value[1] / 12) if value is not None and value[1] >= 3 else None

    def peak_delay_minutes(self, curve_type: str) -> float | None:
        value = self.peak_delay.get(curve_type)
        if (value is None or value[1] < 3) and curve_type == "PREDICTIVE_WARM_HEATING":
            return self.peak_delay_minutes("WARM_HEATING")
        return value[0] if value is not None and value[1] >= 3 else None

    def matches_active(self, cycle: CurveCycle) -> bool:
        """Require the active five-minute shape to resemble this curve's standard."""
        def differences(curve_type: str) -> list[float]:
            comparable = []
            for index, value in cycle.heating_buckets.items():
                row = self.estimate(curve_type, index)
                if row is not None and row[1] >= 3:
                    comparable.append(abs(value - row[0]))
            return comparable
        comparable = differences(cycle.curve_type)
        if len(comparable) < 3 and cycle.curve_type == "PREDICTIVE_WARM_HEATING":
            comparable = differences("WARM_HEATING")
        return len(comparable) >= 3 and sum(comparable) / len(comparable) <= 0.2


class CurveTracker:
    """Use confirmed switch transitions and held HA sensor state for 5-minute bins."""

    def __init__(self, max_peak_wait_seconds: float = DEFAULT_PEAK_OBSERVATION_HOURS * 3600) -> None:
        self.max_peak_wait_seconds = max_peak_wait_seconds
        self.cycle: CurveCycle | None = None
        self.heater: bool | None = None
        self.temperature: float | None = None
        self.last_off_at: float | None = None
        self.pending: list[CurveResult] = []
        self.preset = "home"

    def set_preset(self, preset: str, now: float) -> None:
        """Invalidate an active curve when its target-temperature preset changes."""
        if preset == self.preset:
            return
        self.invalidate("MANUAL_PRESET_CHANGE")
        self.preset = preset

    def seed(self, heater: bool | None, temperature: float | None, now: float) -> None:
        """A restart establishes state but never invents an earlier ON boundary."""
        self.heater = heater
        self.temperature = (
            temperature if temperature is not None and isfinite(temperature)
            and -10 <= temperature <= 50 else None
        )
        self.last_off_at = now if heater is False else None

    def invalidate(self, reason: str) -> None:
        if self.cycle is not None:
            self.cycle.invalid_reason = reason

    def report_temperature(self, temperature: float | None, now: float) -> None:
        """Advance held state to event time, then apply the actual new sensor state."""
        self.advance(now, confirm_peak=False)
        self.temperature = (
            temperature if temperature is not None and isfinite(temperature)
            and -10 <= temperature <= 50 else None
        )
        cycle = self.cycle
        if cycle is None:
            return
        if self.temperature is None:
            cycle.invalid_reason = "SENSOR_UNAVAILABLE"
            if cycle.peak_at is not None:
                self._finish_cooling(now, "SENSOR_UNAVAILABLE")
                self.cycle = None
            return
        if cycle.off_at is None:
            cycle.on_history.add(now, self.temperature)
        if cycle.off_at is not None and cycle.peak_at is None:
            previous = cycle.off_reports[-1] if cycle.off_reports else None
            if len(cycle.off_reports) < 4096:
                if previous is None or now > previous[0]:
                    cycle.off_reports.append((now, self.temperature))
            else:
                cycle.invalid_reason = "OBSERVATION_OVERFLOW"
            # Shared confirmation avoids ending learning on an initial dip or one noisy decline.
            # Related: history.py PeakTracker and runtime.py measured Peak comparisons.
            confirmed = cycle.peak_tracker is not None and cycle.peak_tracker.report(self.temperature, now)
            if confirmed and cycle.peak_tracker.peak_at >= cycle.off_at:
                cycle.peak_at = cycle.peak_tracker.peak_at
                cycle.peak_temperature = cycle.peak_tracker.peak_temperature
                cycle.cooling_baseline = cycle.peak_temperature
                cycle.cooling_last_temperature = cycle.peak_temperature
                cycle.cooling_last_tick = cycle.peak_at
                cycle.cooling_next_tick = cycle.peak_at + BUCKET_SECONDS
                cycle.cooling_previous_report_temperature = self.temperature
                cycle.cooling_previous_report_at = now
                self._finish_heating(now, "PEAK_CONFIRMED")
                # Retroactive cooling ticks retain the old held value, not the later decline.
                # Related: curve_storage.py; actual report timestamps must not be backdated.
                # Rebuild cooling bins from held actual reports during confirmation, never backdate a drop.
                while cycle.cooling_next_tick <= now:
                    held = cycle.peak_temperature
                    for at, value in cycle.off_reports:
                        if at >= cycle.cooling_next_tick:
                            break
                        if at >= cycle.peak_at:
                            held = value
                    index = int((cycle.cooling_next_tick - cycle.peak_at) / BUCKET_SECONDS) - 1
                    cycle.cooling_buckets[index] = round(held - cycle.cooling_last_temperature, 3)
                    cycle.cooling_last_temperature = held
                    cycle.cooling_last_tick = cycle.cooling_next_tick
                    cycle.cooling_next_tick += BUCKET_SECONDS
            if cycle.peak_tracker is not None:
                cycle.candidate_peak_at = cycle.peak_tracker.peak_at
                cycle.candidate_peak_temperature = cycle.peak_tracker.peak_temperature
            self.advance(now)
        elif cycle.peak_at is not None:
            previous_report = cycle.cooling_previous_report_temperature
            if (cycle.cooling_previous_report_at is None
                    or now > cycle.cooling_previous_report_at):
                if previous_report is not None and self.temperature > previous_report + 1e-9:
                    first_rise = cycle.cooling_first_rise_temperature
                    if first_rise is not None and self.temperature > first_rise + 1e-9:
                        self._finish_cooling(now, "REPEATED_RISE")
                        self.cycle = None
                        return
                    if first_rise is None:
                        # Keep the first rebound as the reference across later dips.
                        cycle.cooling_first_rise_temperature = self.temperature
                cycle.cooling_previous_report_temperature = self.temperature
                cycle.cooling_previous_report_at = now
        if (self.cycle is cycle and cycle.peak_at is not None
                and cycle.cooling_target_temperature is not None
                and self.temperature <= cycle.cooling_target_temperature):
            self._finish_cooling(now, "TARGET_REACHED")
            self.cycle = None

    def switch(self, heater: bool | None, now: float, *, start_reason: str = "UNKNOWN",
               off_reason: str = "UNKNOWN", mode: str = "UNKNOWN",
               off_context: dict | None = None, off_slope: float | None = None,
               response_observed: bool | None = None,
               cooling_target_temperature: float | None = None,
               outdoor_temperature: float | None = None) -> None:
        """Create or end an episode only at a confirmed aggregate switch transition."""
        self.advance(now)
        previous = self.heater
        if heater is None:
            self.invalidate("HEATER_UNAVAILABLE")
            self.heater = None
            return
        if heater is previous:
            return
        if heater:
            if self.cycle is not None:
                if self.cycle.peak_at is not None:
                    self._finish_cooling(now, "NEXT_ON")
                else:
                    self._finish_heating(now, "NEXT_ON_BEFORE_PEAK")
                self.cycle = None
            if self.temperature is not None and previous is False:
                kind = ("PREDICTIVE_WARM_HEATING" if start_reason == "PREDICTIVE_START"
                        else "WARM_HEATING")
                self.cycle = CurveCycle(
                    str(uuid4()), now, start_reason, mode, kind, self.temperature,
                    self.temperature, now, now + BUCKET_SECONDS,
                )
                self.cycle.preset = self.preset
                self.cycle.outdoor_temperature = outdoor_temperature
                self.cycle.on_history.add(now, self.temperature)
                if self.preset == "away":
                    self.cycle.invalid_reason = "AWAY_CYCLE"
        else:
            self.last_off_at = now
            if self.cycle is not None and previous is True:
                self.cycle.off_at = now
                self.cycle.off_temperature = self.temperature
                self.cycle.off_reason = off_reason
                self.cycle.cooling_target_temperature = cooling_target_temperature
                # Use the shared held-state five-minute regression for new OFF evidence; related: history.py, runtime.py.
                self.cycle.slope_at_off = self.cycle.on_history.slope(now, held_states=True)
                if off_context is not None:
                    self.cycle.off_context = off_context
                    self.cycle.slope_at_off = off_slope
                if self.temperature is not None:
                    self.cycle.off_reports = [(now, self.temperature)]
                    self.cycle.peak_tracker = PeakTracker(
                        now, now, self.temperature, self.temperature,
                        (response_observed if response_observed is not None else
                         self.temperature - self.cycle.baseline >= 0.1 - 1e-9)
                    )
                self.cycle.candidate_peak_at = now
                self.cycle.candidate_peak_temperature = self.temperature
                if self.temperature is None:
                    self.cycle.invalid_reason = "SENSOR_UNAVAILABLE"
        self.heater = heater

    def advance(self, now: float, *, confirm_peak: bool = True) -> None:
        """Close elapsed five-minute bins without inventing new sensor reports."""
        cycle = self.cycle
        if cycle is None or not isfinite(now):
            return
        heating_limit = cycle.peak_at
        # Keep ON-to-Peak buckets through the configured horizon; related: history.py.
        bucket_limit = (cycle.off_at + self.max_peak_wait_seconds
                        if cycle.off_at is not None else cycle.started_at + self.max_peak_wait_seconds)
        while (cycle.next_tick <= now and cycle.next_tick <= bucket_limit
               and (heating_limit is None or cycle.next_tick <= heating_limit)):
            if self.temperature is not None:
                index = int((cycle.next_tick - cycle.started_at) / BUCKET_SECONDS) - 1
                cycle.heating_buckets[index] = round(self.temperature - cycle.last_temperature, 3)
                cycle.last_temperature = self.temperature
            else:
                cycle.invalid_reason = "SENSOR_UNAVAILABLE"
            cycle.last_tick = cycle.next_tick
            cycle.next_tick += BUCKET_SECONDS
        if cycle.peak_at is not None and cycle.cooling_next_tick is not None:
            while cycle.cooling_next_tick <= now and cycle.off_at is not None:
                if self.temperature is not None and cycle.cooling_last_temperature is not None:
                    index = int((cycle.cooling_next_tick - cycle.peak_at) / BUCKET_SECONDS) - 1
                    cycle.cooling_buckets[index] = round(self.temperature - cycle.cooling_last_temperature, 3)
                    cycle.cooling_last_temperature = self.temperature
                else:
                    cycle.invalid_reason = "SENSOR_UNAVAILABLE"
                cycle.cooling_last_tick = cycle.cooling_next_tick
                cycle.cooling_next_tick += BUCKET_SECONDS
        limit = (cycle.off_at + self.max_peak_wait_seconds
                 if cycle.peak_at is None and cycle.off_at is not None else None)
        if limit is not None and now >= limit:
            self._finish_heating(now, "PEAK_TIMEOUT")
            self.cycle = None

    def _off_profile(self, cycle: CurveCycle) -> dict | None:
        """Sample held OFF reports on a five-minute grid and retain the exact last peak report."""
        if (cycle.off_at is None or cycle.off_temperature is None or cycle.peak_at is None
                or cycle.peak_temperature is None or cycle.slope_at_off is None
                or cycle.peak_at < cycle.off_at):
            return None
        duration = cycle.peak_at - cycle.off_at
        reports = [(at, value) for at, value in cycle.off_reports if at <= cycle.peak_at]
        times = list(range(0, int(duration), int(BUCKET_SECONDS))) + [duration]
        points = []
        index = 0
        for elapsed in times:
            while index + 1 < len(reports) and reports[index + 1][0] <= cycle.off_at + elapsed:
                index += 1
            points.append([elapsed / 60, reports[index][1] - cycle.off_temperature])
        profile = {
            "duration": (cycle.off_at - cycle.started_at) / 60,
            "slope": cycle.slope_at_off,
            "rise": cycle.peak_temperature - cycle.off_temperature,
            "points": points,
        }
        if duration == 0 and cycle.peak_temperature == cycle.off_temperature:
            profile["points"] = [[0, 0]]
        if cycle.off_context is not None:
            profile["context"] = cycle.off_context
        return profile if valid_profile(
            profile, max_peak_wait_minutes=self.max_peak_wait_seconds / 60
        ) else None

    def _finish_heating(self, now: float, reason: str) -> None:
        cycle = self.cycle
        if cycle is None or cycle.heating_saved:
            return
        cycle.heating_saved = True
        heating_buckets = {
            index: value for index, value in cycle.heating_buckets.items()
            if cycle.peak_at is None
            or cycle.started_at + (index + 1) * BUCKET_SECONDS <= cycle.peak_at
        }
        self.pending.append(CurveResult(
            cycle.id, cycle.curve_type, cycle.start_reason, cycle.off_reason, reason,
            cycle.mode, cycle.started_at, cycle.off_at, cycle.peak_at, now,
            heating_buckets,
            (cycle.peak_temperature - cycle.off_temperature
             if cycle.peak_temperature is not None and cycle.off_temperature is not None else None),
            cycle.invalid_reason or ("INCOMPLETE_PEAK" if cycle.peak_at is None else None),
            cycle.preset, self._off_profile(cycle),
            cycle.baseline, cycle.off_temperature, cycle.peak_temperature, cycle.slope_at_off,
            peak_confirmation_drop=(cycle.peak_temperature - cycle.peak_tracker.previous_temperature
                                    if cycle.peak_at is not None and cycle.peak_tracker is not None
                                    and cycle.peak_tracker.previous_temperature is not None else None),
            peak_confirmation_reports=cycle.peak_tracker.decline_reports if cycle.peak_tracker else None,
            peak_confirmation_extra_drop=cycle.peak_tracker.confirmation_extra_drop if cycle.peak_tracker else None,
            outdoor_temperature=cycle.outdoor_temperature,
        ))

    def _finish_cooling(self, now: float, reason: str) -> None:
        cycle = self.cycle
        if cycle is None or cycle.peak_at is None:
            return
        self.pending.append(CurveResult(
            cycle.id, "COOLING", cycle.start_reason, cycle.off_reason, reason,
            cycle.mode, cycle.started_at, cycle.off_at, cycle.peak_at, now,
            dict(cycle.cooling_buckets), None, cycle.invalid_reason, cycle.preset,
            start_temperature=cycle.baseline, off_temperature=cycle.off_temperature,
            peak_temperature=cycle.peak_temperature, slope_at_off=cycle.slope_at_off,
            peak_confirmation_drop=(cycle.peak_temperature - cycle.peak_tracker.previous_temperature
                                    if cycle.peak_tracker is not None
                                    and cycle.peak_tracker.previous_temperature is not None else None),
            peak_confirmation_reports=cycle.peak_tracker.decline_reports if cycle.peak_tracker else None,
            peak_confirmation_extra_drop=cycle.peak_tracker.confirmation_extra_drop if cycle.peak_tracker else None,
            outdoor_temperature=cycle.outdoor_temperature,
        ))

    def take_results(self) -> list[CurveResult]:
        results, self.pending = self.pending, []
        return results

    def close_incomplete(self, now: float, reason: str) -> None:
        """Keep a restart boundary out of learned standards while retaining raw bins."""
        if self.cycle is None:
            return
        self.cycle.invalid_reason = reason
        if not self.cycle.heating_saved:
            self._finish_heating(now, reason)
        if self.cycle.peak_at is not None:
            self._finish_cooling(now, reason)
        self.cycle = None
