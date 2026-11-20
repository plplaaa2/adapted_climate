"""Track four five-minute heating curves; related: runtime.py, curve_storage.py, select.py."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import exp, isfinite
from uuid import uuid4
import time

from .history import TemperatureHistory, PeakTracker
from .curve_memory import Bucket, CurveMemory, CURRENT_MAX_AGE, blended
from .off_response import MAX_OFF_PROFILES, predict_off, valid_profile
from .const import DEFAULT_PEAK_OBSERVATION_HOURS
from .peak_prediction import HEATING_OUTDOOR_GROUPS, heating_outdoor_bucket

BUCKET_SECONDS = 300.0
# Integrate a fully covered ON interval; related: runtime.py, peak_migration.py.
def heating_outdoor_mean(reports, start: float, end: float) -> float | None:
    covered = start
    weighted = 0.0
    for index, (at, value, expiry) in enumerate(reports):
        next_at = reports[index + 1][0] if index + 1 < len(reports) else end
        left, right = max(at, start), min(next_at, expiry, end)
        if right <= left:
            continue
        if left > covered or heating_outdoor_bucket(value) is None:
            return None
        weighted += (right - left) * value
        covered = right
        if covered == end:
            break
    return weighted / (end - start) if end > start and covered == end else None


NEW_CYCLE_WEIGHT = 0.2
MAX_RESIDUAL_RISE = 5.0
MAX_BUCKET_DELTA = 3.0
MAX_COOLING_SECONDS = 3 * 3600
# Preserve rebound cancellation independently of Peak confirmation sensitivity; related: history.py.
PEAK_REBOUND_DROP = 0.2
HYBRID_BIN_MINUTES = 5
MAX_HYBRID_RUNTIME_MINUTES = 1440
MAX_HYBRID_INTERPOLATION_MINUTES = 10
MAX_RUNTIME_SUMMARY_RISE = 20.0
OUTDOOR_GROUPS = ("WARM", "NORMAL", "COLD", "VERY_COLD")
RUNTIME_SAMPLE_QUALITY = ("ACCEPTED", "INSUFFICIENT_BUCKETS", "CURVE_DEVIATION")
DEVIATION_BUCKET_DELTA = 0.35
DEVIATION_TOTAL_MIN = 0.8
DEVIATION_TOTAL_PER_BUCKET = 0.12
CURVE_TYPES = (
    "WARM_HEATING", "PREDICTIVE_WARM_HEATING", "COOLING", "SOLAR_GAIN", "SUNRISE_HEATING",
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
    target_peak_temperature: float | None = None
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
    peak_decline_seen: bool = False
    peak_rebound_reports: int = 0
    sunrise_peak_excess: dict | None = None
    outdoor_temperature: float | None = None
    heating_outdoor_reports: list[tuple[float, float | None, float]] = field(default_factory=list)
    outdoor_mean: float | None = None
    outdoor_end: float | None = None
    cooling_outdoor_reports: list[tuple[float, float | None, float]] = field(default_factory=list)
    sunrise_saving: bool = False
    sunrise_prediction: dict | None = None
    hold_settings: dict | None = None
    hold_crossing: tuple[float, float | None] | None = None
    hold_maximum: tuple[float, float] | None = None
    hold_exclusion: str | None = None


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
    sunrise_peak_excess: dict | None = None
    peak_confirmation_extra_drop: float | None = None
    # Outdoor temperature at confirmed ON for the runtime/peak model.
    outdoor_temperature: float | None = None
    target_peak_temperature: float | None = None
    observed_peak_temperature: float | None = None
    automatic_restart: bool = False
    cooling_outdoor_groups: dict[int, str] = field(default_factory=dict)
    sunrise_saving: bool = False
    sunrise_prediction: dict | None = None
    heating_hold: dict | None = None
    outdoor_mean: float | None = None
    outdoor_end: float | None = None
    solar_samples: list | None = None


# Grade confirmed outcomes separately from incomplete observations; related: curve_storage.py, runtime.py.
def heating_outcome(result: CurveResult) -> tuple[str | None, float | None]:
    target = result.target_peak_temperature
    if (result.curve_type == "COOLING" or result.invalid_reason not in (None, "INCOMPLETE_PEAK")
            or target is None or not isfinite(target) or result.off_at is None):
        return None, None
    if result.peak_at is not None and result.peak_temperature is not None:
        deficit = target - result.peak_temperature
        if deficit <= 1e-6:
            return "reached", 1.0
        return "missed", max(0.1, 1.0 - deficit)
    if (result.off_reason == "PREDICTIVE_STOP" and result.automatic_restart
            and result.observed_peak_temperature is not None
            and result.observed_peak_temperature < target - 1e-6):
        return "restarted_below_target", None
    return "partial", None


# Collect one continuously covered morning; related: history.py, runtime.py, curve_storage.py.
@dataclass
class SolarGainObservation:
    sunrise_at: float
    samples: list = field(default_factory=list)
    tracker: PeakTracker | None = None

    def sample(self, now, temperature, elevation, sunny):
        if self.tracker is None:
            self.tracker = PeakTracker(self.sunrise_at, now, temperature, temperature)
        self.samples.append({"at": now, "temperature": temperature, "elevation": elevation, "sunny": sunny})
        return self.tracker.report(temperature, now)

    def result(self, now, mode, preset):
        tracker = self.tracker
        buckets = {
            index: right["temperature"] - left["temperature"]
            for index, (left, right) in enumerate(zip(self.samples, self.samples[1:]))
            if right["at"] <= tracker.peak_at
        }
        return CurveResult(
            f"solar-{int(self.sunrise_at)}", "SOLAR_GAIN", "SUNRISE", "NOT_APPLICABLE",
            "PEAK_CONFIRMED", mode, self.sunrise_at, None, tracker.peak_at, now,
            buckets, None, None, preset, start_temperature=self.samples[0]["temperature"],
            peak_temperature=tracker.peak_temperature, solar_samples=self.samples,
            peak_confirmation_reports=tracker.decline_reports,
            peak_confirmation_drop=tracker.peak_temperature - tracker.previous_temperature,
            peak_confirmation_extra_drop=tracker.confirmation_extra_drop,
        )


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
        self.runtime_samples: list[tuple[float, float, float | None, float | None]] = []
        self.runtime_outcome_weights: dict[tuple, float] = {}
        # Keep average admission independent of prediction samples; related: curve_storage.py, runtime.py.
        self.runtime_average_samples: list[float] = []
        self.runtime_accepted_cycles = 0
        self.shortfall_samples: list[tuple[float, float, float, float | None, float]] = []
        # Completed normal-cycle additive features; related: peak_prediction.py, curve_storage.py.
        self.peak_samples: list[dict] = []
        # Conditional samples are used only for heater runtime / OFF prediction.
        # The aggregate set remains the safe fallback.
        self.outdoor_runtime_samples: dict[str, list[tuple[float, float, float | None, float | None]]] = {}
        self.outdoor_cooling: dict[tuple[str, int], tuple[float, int]] = {}
        self.outdoor_cooling_memory = {group: CurveMemory() for group in OUTDOOR_GROUPS}
        self.hybrid_residual_memory = {group: CurveMemory() for group in ("GLOBAL", *HEATING_OUTDOOR_GROUPS)}
        self.runtime_rise_memory = {group: CurveMemory() for group in ("GLOBAL", *HEATING_OUTDOOR_GROUPS)}
        self.hybrid_duration_memory = {group: CurveMemory() for group in self.hybrid_residual_memory}
        self.runtime_duration_memory = {group: CurveMemory() for group in self.runtime_rise_memory}

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

    # Keep bounded OFF residual summaries by runtime and weather; related: curve_storage.py, runtime.py.
    def learn_hybrid_residual(self, sample: dict, *, groups=None) -> None:
        self._learn_off_summary(self.hybrid_residual_memory, self.hybrid_duration_memory,
                                sample, sample["peak"] - sample["off"], groups)

    def learn_runtime_rise(self, sample: dict, *, groups=None) -> None:
        rise = sample["peak"] - sample["start"]
        if 0 < rise <= MAX_RUNTIME_SUMMARY_RISE:
            self._learn_off_summary(self.runtime_rise_memory, self.runtime_duration_memory, sample, rise, groups)

    def _learn_off_summary(self, memories, durations, sample: dict, rise: float, groups) -> None:
        minutes, now = sample["minutes"], sample["completed"]
        index = round(minutes / HYBRID_BIN_MINUTES)
        for group in groups if groups is not None else ("GLOBAL", heating_outdoor_bucket(sample["outdoor"])):
            if group is None:
                continue
            duration = durations[group]
            current = duration.current.setdefault(index, Bucket(minutes))
            current.update(minutes, now)
            if memories[group].learn_conditioned_bucket(index, rise, now):
                duration.long_term.setdefault(index, Bucket(current.mean)).promote(current, now)

    def _summary_segments(self, memories, durations, outdoor, now, *, require_long_term):
        for group in (heating_outdoor_bucket(outdoor), "GLOBAL"):
            if group is None:
                continue
            memory = memories[group]
            duration = durations[group]
            points = []
            for index in sorted(memory.current.keys() | memory.long_term.keys()):
                # Keep OFF seasonal predictions independent of unpromoted evidence; related: curve_storage.py.
                if require_long_term:
                    estimate = blended(None, memory.long_term.get(index), now)
                else:
                    estimate = memory.conditioned_estimate(index, now)
                if estimate is None:
                    continue
                weight = estimate[3]
                current, long_term = duration.current.get(index), duration.long_term.get(index)
                if (weight > 0 and current is None) or (weight < 1 and long_term is None):
                    continue
                minutes = weight * (current.mean if current else 0) + (1 - weight) * (long_term.mean if long_term else 0)
                if abs(minutes - index * HYBRID_BIN_MINUTES) <= HYBRID_BIN_MINUTES / 2 + 1e-6:
                    points.append((minutes, estimate[0]))
            yield group, [
                (left, right) for left, right in zip(points, points[1:])
                if 0 < right[0] - left[0] <= MAX_HYBRID_INTERPOLATION_MINUTES + 1e-6
            ]

    def predict_hybrid_summary(self, temperature: float, minutes: float,
                               outdoor: float | None, now: float, *,
                               require_long_term: bool = False) -> float | None:
        """Interpolate only within two qualified neighboring duration bins."""
        if not isfinite(temperature) or not isfinite(minutes) or not 10 <= minutes <= MAX_HYBRID_RUNTIME_MINUTES:
            return None
        for _, segments in self._summary_segments(
            self.hybrid_residual_memory, self.hybrid_duration_memory, outdoor, now,
            require_long_term=require_long_term,
        ):
            for (left, before), (right, after) in segments:
                if left <= minutes <= right:
                    return temperature + before + (minutes - left) * (after - before) / (right - left)
        return None

    # Invert stable observed rise only within a qualified runtime segment; related: runtime.py.
    def long_term_runtime_segment(self, rise: float, outdoor: float | None, now: float):
        for group, segments in self._summary_segments(
            self.runtime_rise_memory, self.runtime_duration_memory, outdoor, now, require_long_term=True,
        ):
            for (left, before), (right, after) in segments:
                if 0 < before <= rise <= after and before < after:
                    return group, (left, before), (right, after)
        return None

    # Expire individual evidence without touching learned aggregates; related: curve_storage.py.
    def expire_cycle_samples(self, cutoff: float) -> None:
        self.peak_samples = [row for row in self.peak_samples if row["completed"] >= cutoff]
        self.runtime_samples = [row for row in self.runtime_samples if row[3] is None or row[3] >= cutoff]
        for group, rows in self.outdoor_runtime_samples.items():
            self.outdoor_runtime_samples[group] = [row for row in rows if row[3] is None or row[3] >= cutoff]
        self.shortfall_samples = [row for row in self.shortfall_samples if row[4] >= cutoff]
        self._prune_runtime_weights()

    def load_runtime_samples(self, samples: list[tuple[float, ...]]) -> None:
        """Restore Peak samples, accepting legacy rows without a completion timestamp."""
        restored = []
        self.runtime_outcome_weights.clear()
        self.outdoor_runtime_samples.clear()
        for sample in samples:
            if len(sample) not in (2, 3, 4, 5, 6):
                continue
            minutes, rise = sample[:2]
            lag = sample[2] if len(sample) >= 3 and sample[2] != 0 else None
            # Four-field rows are legacy (minutes, rise, lag, completed_at).
            outdoor = sample[3] if len(sample) >= 5 else None
            completed_at = sample[4] if len(sample) >= 5 else (sample[3] if len(sample) == 4 else None)
            outcome_weight = sample[5] if len(sample) == 6 else None
            try:
                completed_at = None if completed_at is None else float(completed_at)
                outdoor = None if outdoor is None else float(outdoor)
            except (TypeError, ValueError):
                continue
            if (isfinite(minutes) and isfinite(rise) and (lag is None or isfinite(lag) and lag > 0)
                    and (outdoor is None or isfinite(outdoor))
                    and (completed_at is None or isfinite(completed_at) and completed_at >= 0)
                    and minutes > 0 and rise > 0):
                row = (float(minutes), float(rise), lag, completed_at)
                restored.append(row)
                if type(outcome_weight) in (float, int) and 0.1 <= outcome_weight <= 1:
                    self.runtime_outcome_weights[row] = outcome_weight
                bucket = heating_outdoor_bucket(outdoor)
                if bucket is not None:
                    self.outdoor_runtime_samples.setdefault(bucket, []).append(row)
        self.runtime_samples = restored[-200:]
        for bucket, rows in self.outdoor_runtime_samples.items():
            self.outdoor_runtime_samples[bucket] = rows[-200:]
        self._prune_runtime_weights()

    def _prune_runtime_weights(self) -> None:
        """Retain weights only for live samples; related: curve_storage.py."""
        live = set(self.runtime_samples)
        for rows in self.outdoor_runtime_samples.values():
            live.update(rows)
        self.runtime_outcome_weights = {row: weight for row, weight in self.runtime_outcome_weights.items() if row in live}

    @staticmethod
    def runtime_lag_from_buckets(buckets) -> float | None:
        """Find the first sustained measured rise; unknown is kept distinct from zero."""
        cumulative = 0.0
        totals = []
        for index, delta in sorted(buckets):
            cumulative += delta
            totals.append((index, cumulative))
        for position in range(max(0, len(totals) - 2)):
            if (totals[position + 2][0] == totals[position][0] + 2
                    and totals[position][1] >= 0.2
                    and min(value for _, value in totals[position:position + 3]) >= 0.1):
                return (totals[position][0] + 1) * BUCKET_SECONDS / 60
        return None

    @staticmethod
    def _runtime_lag(result: CurveResult) -> float | None:
        return CurveStandards.runtime_lag_from_buckets(result.buckets.items())

    def add_runtime_sample(self, result: CurveResult, reason: str) -> None:
        """Learn peak rise per confirmed heater minute from a complete warm cycle."""
        if (result.sunrise_saving or result.invalid_reason or reason not in RUNTIME_SAMPLE_QUALITY
                or result.curve_type not in ("WARM_HEATING", "PREDICTIVE_WARM_HEATING")
                or result.preset == "away"
                or result.off_at is None or result.peak_at is None or result.start_temperature is None
                or result.peak_temperature is None or result.peak_at < result.off_at):
            return
        minutes = (result.off_at - result.started_at) / 60
        rise = result.peak_temperature - result.start_temperature
        if isfinite(minutes) and isfinite(rise) and minutes > 0 and rise > 0:
            sample = (minutes, rise, self._runtime_lag(result), result.ended_at)
            self.runtime_samples.append(sample)
            self.runtime_samples = self.runtime_samples[-200:]
            _, outcome_weight = heating_outcome(result)
            if outcome_weight is not None:
                self.runtime_outcome_weights[sample] = outcome_weight
            bucket = heating_outdoor_bucket(result.outdoor_mean)
            if bucket is not None:
                rows = self.outdoor_runtime_samples.setdefault(bucket, [])
                rows.append(sample)
                del rows[:-200]
            self._prune_runtime_weights()

    def _runtime_samples_for(self, outdoor_temperature: float | None):
        """Use a temperature band only after three complete cycles exist."""
        bucket = heating_outdoor_bucket(outdoor_temperature)
        rows = self.outdoor_runtime_samples.get(bucket, []) if bucket is not None else []
        return rows if len(rows) >= 3 else self.runtime_samples

    def _runtime_weights(self, samples=None, now: float | None = None) -> list[float]:
        """Favor recent completed cycles without discarding legacy timestamp-free samples."""
        now = time.time() if now is None else now
        weights = []
        samples = self.runtime_samples if samples is None else samples
        for sample in samples:
            completed_at = sample[3]
            age_weight = 1.0 if completed_at is None else exp(-max(0.0, now - completed_at) / (30 * 86400))
            weights.append(max(1e-6, age_weight) * self.runtime_outcome_weights.get(sample, 1.0))
        return weights

    def _runtime_response(self, outdoor_temperature: float | None = None) -> tuple[float, float | None] | None:
        samples = self._runtime_samples_for(outdoor_temperature)
        if len(samples) < 3:
            return None
        weights = self._runtime_weights(samples)
        total_weight = sum(weights)
        rate = sum((rise / max(1.0, minutes - (lag or 0))) * weight
                   for weight, (minutes, rise, lag, _) in zip(weights, samples)) / total_weight
        measured = [(sample[2], weight) for sample, weight in zip(samples, weights)
                    if sample[2] is not None]
        lag = (sum(value * weight for value, weight in measured) / sum(weight for _, weight in measured)
               if len(measured) >= 3 else None)
        return (rate, lag) if isfinite(rate) and rate > 0 else None

    # Use measured duration-to-peak outcomes; related: curve_storage.py, runtime.py.
    def _runtime_curve(self, outdoor_temperature: float | None = None) -> list[tuple[float, float]]:
        """Build the measured heater-runtime to eventual-peak-rise curve with an anchored origin."""
        grouped: dict[int, list[tuple[float, float, float]]] = {}
        samples = self._runtime_samples_for(outdoor_temperature)
        for (minutes, rise, _lag, _), weight in zip(samples, self._runtime_weights(samples)):
            if isfinite(minutes) and isfinite(rise) and minutes > 0 and rise > 0:
                grouped.setdefault(round(minutes / 5), []).append((minutes, rise, weight))
        if not grouped:
            return []
        points = []
        # Anchor at (0, 0) and (lag, 0) so short heating requests interpolate smoothly
        # instead of failing or producing steep unphysical extrapolations.
        response = self._runtime_response(outdoor_temperature)
        lag = response[1] if response is not None else None
        points.append((0.0, 0.0))
        if lag is not None and lag > 0 and lag < min(item[0] for group in grouped.values() for item in group):
            points.append((float(lag), 0.0))
        # Pool adjacent conflicting observations instead of propagating a single high rise forward.
        # Related: test_curve_learning.py and runtime.py cutoff support.
        blocks = []
        for _, rises in sorted(grouped.items()):
            total_weight = sum(weight for _, _, weight in rises)
            minutes = sum(duration * weight for duration, _, weight in rises) / total_weight
            blocks.append(([minutes], sum(rise * weight for _, rise, weight in rises), total_weight))
            while len(blocks) > 1:
                left, right = blocks[-2:]
                if left[1] / left[2] <= right[1] / right[2]:
                    break
                blocks[-2:] = [(left[0] + right[0], left[1] + right[1], left[2] + right[2])]
        for durations, weighted_rise, weight in blocks:
            points.extend((minutes, weighted_rise / weight) for minutes in durations)
        return points

    # Require more than one useful observation before trusting short runtime predictions; related: runtime.py.
    def runtime_control_minimum(self, outdoor: float | None) -> float | None:
        samples = sorted(self._runtime_samples_for(outdoor), key=lambda row: row[0])
        support = 0.0
        for sample in samples:
            support += self.runtime_outcome_weights.get(sample, 1.0)
            if support >= 2.0:
                return sample[0]
        return None

    # Relax measured support without multiplying the model's requested runtime; related: runtime.py.
    def runtime_shutdown_limits(self, outdoor: float | None, start: float,
                                target: float, bias: float = 0.0) -> dict:
        measured = self.runtime_control_minimum(outdoor)
        average = (sum(self.runtime_average_samples) / len(self.runtime_average_samples)
                   if self.runtime_average_samples else None)
        count = self.runtime_accepted_cycles
        base = .2 if count < 10 else .1
        # Weather and forecast bias share a separate five-percent budget. Only an actual
        # reduction in calculated demand unlocks it; missing evidence grants no bonus.
        global_required = (self.required_heating_minutes(start, target)
                           if average is not None and count >= 10 else None)
        conditioned = (self.required_heating_minutes(start, target - bias, outdoor)
                       if global_required is not None else None)
        correction = (min(.05, max(0.0, (global_required - conditioned) / measured))
                      if measured and global_required is not None and conditioned is not None else 0.0)
        minimum = (max(measured * (1 - base - correction), (average or 0) * .6)
                   if measured is not None else None)
        if average is None:
            minimum = measured
        return {"minimum": minimum, "measured_minimum": measured,
                "average_minutes": average, "average_samples": len(self.runtime_average_samples),
                "average_floor": (average * .6 if average is not None else None),
                "accepted_cycles": count, "base_reduction": base, "correction_reduction": correction}

    def shortfall_minimum(self, start: float, target: float, outdoor: float | None, now: float) -> float:
        """Bound carryover to recent failures under comparable heating demand and weather."""
        bucket = heating_outdoor_bucket(outdoor)
        return max((minutes + 5 for minutes, baseline, goal, weather, ended in self.shortfall_samples
                    if 0 <= now - ended <= 14 * 86400 and abs(start - baseline) <= .5
                    and abs(target - goal) <= .25 and heating_outdoor_bucket(weather) in (None, bucket)),
                   default=0.0)

    def _runtime_segment(self, value: float, *, inverse: bool,
                         points: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
        """Select a positive-slope measured segment, using endpoint segments outside range."""
        if len(points) < 2:
            return None
        for left, right in zip(points, points[1:]):
            x0, y0 = (left[1], left[0]) if inverse else left
            x1, y1 = (right[1], right[0]) if inverse else right
            if x1 > x0 and (not inverse or y1 > y0) and x0 <= value <= x1:
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
                                 outdoor_temperature: float | None = None, *,
                                 points: list[tuple[float, float]] | None = None) -> float | None:
        """Invert observed runtime/peak samples instead of assuming one constant slope."""
        if (not isfinite(start_temperature) or not isfinite(target_temperature)
                or target_temperature <= start_temperature):
            return None
        if len(self.runtime_samples) < 3:
            return None
        rise = target_temperature - start_temperature
        if points is None:
            points = self._runtime_curve(outdoor_temperature)
        segment = self._runtime_segment(rise, inverse=True, points=points)
        if segment is None:
            return None
        x0, y0, x1, y1 = segment
        return max(0.0, y0 + (rise - x0) * (y1 - y0) / (x1 - x0))

    def predicted_peak(self, start_temperature: float, heating_minutes: float,
                       outdoor_temperature: float | None = None, *,
                       points: list[tuple[float, float]] | None = None) -> float | None:
        """Interpolate measured eventual peaks for the requested heater runtime."""
        if (not isfinite(start_temperature) or not isfinite(heating_minutes)
                or heating_minutes <= 0):
            return None
        if len(self.runtime_samples) < 3:
            return None
        if points is None:
            points = self._runtime_curve(outdoor_temperature)
        segment = self._runtime_segment(heating_minutes, inverse=False, points=points)
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
        if result.sunrise_saving:
            return False, "SUNRISE_SAVING"
        if result.invalid_reason:
            return False, result.invalid_reason
        if result.curve_type == "COOLING":
            if result.end_reason == "REPEATED_RISE":
                return False, "REPEATED_RISE"
            if result.heating_hold is not None and not result.heating_hold["completed"]:
                return False, result.heating_hold["exclusion_reason"]
        if result.preset == "away":
            return False, "AWAY_CYCLE"
        if result.curve_type in ("SOLAR_GAIN", "SUNRISE_HEATING"):
            if (len(result.buckets) < 2
                    or any(not isfinite(value) or abs(value) > MAX_BUCKET_DELTA
                           for value in result.buckets.values())):
                return False, "INSUFFICIENT_OR_INVALID_SUNRISE_BUCKETS"
            return True, "ACCEPTED"
        if len(result.buckets) < 2:
            return False, "INSUFFICIENT_BUCKETS"
        if (result.curve_type != "COOLING" and
                (result.residual_rise is None or not 0 <= result.residual_rise <= MAX_RESIDUAL_RISE)):
            return False, "INVALID_RESIDUAL_RISE"
        if (result.curve_type != "COOLING" and
                (result.off_at is None or result.peak_at is None
                 or result.peak_at < result.off_at
                 or (not (result.sunrise_peak_excess or {}).get("ends_observation")
                     and result.peak_at - result.off_at > self.max_peak_wait_minutes * 60))):
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
        if result.curve_type in ("WARM_HEATING", "PREDICTIVE_WARM_HEATING"):
            delay = ((result.peak_at - result.off_at) / 60
                     if result.peak_at is not None and result.off_at is not None else None)
            checks.extend([
                {"code": "INVALID_RESIDUAL_RISE", "actual": result.residual_rise,
                 "min": 0, "max": MAX_RESIDUAL_RISE, "unit": "°C",
                 "passed": result.residual_rise is not None and 0 <= result.residual_rise <= MAX_RESIDUAL_RISE},
                {"code": "INVALID_PEAK_DELAY", "actual": delay,
                 "min": 0, "max": (None if (result.sunrise_peak_excess or {}).get("ends_observation")
                                    else self.max_peak_wait_minutes), "unit": "minutes",
                 "passed": delay is not None and delay >= 0 and (
                     bool((result.sunrise_peak_excess or {}).get("ends_observation"))
                     or delay <= self.max_peak_wait_minutes)},
            ])
        finite = all(isfinite(value) for value in result.buckets.values())
        maximum = max((abs(value) for value in result.buckets.values() if isfinite(value)), default=None)
        checks.append({"code": "IMPLAUSIBLE_FIVE_MINUTE_DELTA", "actual": maximum,
                       "min": 0, "max": MAX_BUCKET_DELTA, "unit": "°C",
                       "nonfinite": not finite,
                       "passed": finite and (maximum is None or maximum <= MAX_BUCKET_DELTA)})
        if result.curve_type in ("SOLAR_GAIN", "SUNRISE_HEATING"):
            return checks
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

    # Integrate one continuously covered curve; related: runtime.py, curve_storage.py.
    def predict_cooling_delta(self, elapsed_minutes: float, horizon_minutes: float, *,
                              outdoor_group: str | None = None,
                              now: float | None = None) -> float | None:
        """Integrate learned cooling over the entire response horizon."""
        prediction = self.predict_cooling(elapsed_minutes, horizon_minutes, outdoor_group=outdoor_group, now=now)
        return prediction[0] if prediction is not None else None

    # Track the lowest cumulative change for early starts; related: runtime.py, frontend/panel.js.
    def predict_cooling(self, elapsed_minutes: float, horizon_minutes: float, *,
                        outdoor_group: str | None = None,
                        now: float | None = None,
                        lowest: bool = False, curve_weights: list | None = None,
                        ) -> tuple[float, tuple[str, ...]] | None:
        """Return the net or lowest cumulative delta with full-horizon support."""
        if (not isfinite(elapsed_minutes) or not isfinite(horizon_minutes)
                or elapsed_minutes < 0 or horizon_minutes <= 0):
            return None
        if outdoor_group is not None:
            memory = self.outdoor_cooling_memory.get(outdoor_group)
            if memory is None:
                return None
            now = time.time() if now is None else now
            # Use either summary or legacy data for the entire horizon, preserving upgrades.
            prediction = self._integrate_cooling(
                elapsed_minutes, horizon_minutes,
                lambda index: ((row[0], row[2], row[3]) if (row := memory.conditioned_estimate(index, now)) else None),
                lowest=lowest, curve_weights=curve_weights,
            )
            if prediction is not None:
                return prediction

        def supported_delta(index):
            source, weight = "legacy", None
            if outdoor_group is not None:
                row = self.outdoor_cooling.get((outdoor_group, index))
            else:
                row = self.estimate("COOLING", index)
                memory = self.memory["COOLING"]
                if row is not None and (index in memory.current or index in memory.long_term):
                    source, weight = memory.last_source, memory.last_weight
            return (row[0], source, weight) if row is not None and row[1] >= 3 else None

        return self._integrate_cooling(
            elapsed_minutes, horizon_minutes, supported_delta, lowest=lowest, curve_weights=curve_weights
        )

    @staticmethod
    def _integrate_cooling(elapsed_minutes, horizon_minutes, lookup, *,
                           lowest: bool = False, curve_weights: list | None = None,
                           ) -> tuple[float, tuple[str, ...]] | None:
        end = elapsed_minutes + horizon_minutes
        position = elapsed_minutes
        delta = 0.0
        minimum_delta = 0.0
        layers = set()
        weights = []
        while position < end:
            index = int(position / 5)
            row = lookup(index)
            if row is None or not isfinite(row[0]):
                return None
            change, layer, weight = row
            layers.add(layer)
            if curve_weights is not None:
                weights.append({"index": index, "curve_type": "COOLING", "current_weight": weight})
            next_position = min(end, (index + 1) * 5)
            delta += change * (next_position - position) / 5
            minimum_delta = min(minimum_delta, delta)
            position = next_position
        # Publish only a fully covered prediction; related: runtime.py, panel.js.
        if curve_weights is not None:
            curve_weights.extend(weights)
        return (minimum_delta if lowest else delta), tuple(sorted(layers))

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
                group = result.cooling_outdoor_groups.get(index) if result.curve_type == "COOLING" else None
                if group in self.outdoor_cooling_memory and index >= 0:
                    key = (group, index)
                    self.outdoor_cooling_memory[group].learn_conditioned_bucket(index, value, result.ended_at)
                    outdoor_mean, count = self.outdoor_cooling.get(key, (value, 0))
                    self.outdoor_cooling[key] = (
                        outdoor_mean + (value - outdoor_mean) * NEW_CYCLE_WEIGHT, count + 1
                    )
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
        """Prefer measured ON-to-rise lag, then covered heating buckets; related: runtime.py."""
        response = self._runtime_response()
        if response is not None and response[1] is not None:
            return response[1]
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
        self._cooling_tail: CurveTracker | None = None
        self.preset = "home"

    def set_preset(self, preset: str, now: float) -> None:
        """Invalidate an active curve when its target-temperature preset changes."""
        if preset == self.preset:
            return
        self.invalidate_hold("MANUAL_PRESET_CHANGE")
        if self.heater is not False:
            self.invalidate("MANUAL_PRESET_CHANGE")
        self.preset = preset

    def seed(self, heater: bool | None, temperature: float | None, now: float, *,
             confirmed_off_at: float | None = None) -> None:
        """A restart establishes state but never invents an earlier ON boundary."""
        self.heater = heater
        self.temperature = (
            temperature if temperature is not None and isfinite(temperature)
            and -10 <= temperature <= 50 else None
        )
        self.last_off_at = (confirmed_off_at if heater is False and confirmed_off_at is not None
                            and confirmed_off_at <= now else None)

    def invalidate(self, reason: str) -> None:
        if self.cycle is not None:
            self.cycle.invalid_reason = reason

    # Preserve the existing peak observer across target edits; related: runtime.py.
    def invalidate_hold(self, reason: str) -> None:
        cycle = self.cycle
        if cycle is not None and cycle.hold_settings is not None and cycle.hold_exclusion is None:
            cycle.hold_exclusion = reason
        if self._cooling_tail is not None:
            self._cooling_tail.invalidate_hold(reason)

    # Capture the crossing from actual reports; related: runtime.py, curve_storage.py.
    def report_temperature(self, temperature: float | None, now: float, *,
                           outdoor_temperature: float | None = None) -> None:
        """Advance held state to event time, then apply the actual new sensor state."""
        self.advance(now, confirm_peak=False)
        if self._cooling_tail is not None:
            self._cooling_tail.report_temperature(temperature, now, outdoor_temperature=outdoor_temperature)
            self._drain_cooling_tail()
        self.temperature = (
            temperature if temperature is not None and isfinite(temperature)
            and -10 <= temperature <= 50 else None
        )
        cycle = self.cycle
        if cycle is None:
            return
        if self.temperature is None:
            self.last_off_at = None
            cycle.invalid_reason = "SENSOR_UNAVAILABLE"
            if cycle.peak_at is not None:
                self._finish_cooling(now, "SENSOR_UNAVAILABLE")
                self.cycle = None
            elif cycle.heating_saved:
                self._finish_late_hold(now, "SENSOR_UNAVAILABLE")
            return
        if cycle.off_at is None:
            cycle.on_history.add(now, self.temperature)
        if cycle.hold_maximum is not None and self.temperature > cycle.hold_maximum[1]:
            cycle.hold_maximum = (now, self.temperature)
        if cycle.off_at is not None and cycle.hold_settings is not None:
            previous_at, previous_value = (
                (cycle.cooling_previous_report_at, cycle.cooling_previous_report_temperature)
                if cycle.peak_at is not None else
                cycle.off_reports[-1] if cycle.off_reports else (None, None)
            )
            target = cycle.hold_settings["target_temperature"]
            if (previous_at is not None and now > previous_at and previous_value is not None
                    and previous_value > target + 1e-6 and self.temperature <= target + 1e-6
                    and cycle.hold_crossing is None):
                cycle.hold_crossing = (now, outdoor_temperature)
            if cycle.peak_at is not None and self._finish_target_cooling(cycle):
                return
        if cycle.off_at is not None and cycle.peak_at is None:
            previous = cycle.off_reports[-1] if cycle.off_reports else None
            # Measure excess independently of peak confirmation; related: runtime.py, curve_storage.py.
            excess = cycle.sunrise_peak_excess
            if (excess is not None and now >= excess["sunrise_at"]
                    and (previous is None or now > previous[0])):
                difference = self.temperature - excess["predicted_peak"]
                maximum = excess.get("maximum_excess")
                excess["maximum_excess"] = difference if maximum is None else max(maximum, difference)
                if difference >= excess.get("threshold", 1.0) - 1e-6 and "first_at" not in excess:
                    # Reliable OFF forecasts end observation only on strict excess; related: runtime.py.
                    if not excess.get("ends_observation") or difference > excess.get("threshold", 0.5) + 1e-9:
                        excess.update(first_at=now, first_temperature=self.temperature)
                if excess.get("ends_observation") and "first_at" in excess:
                    cycle.invalid_reason = "SUNRISE_PEAK_EXCESS"
                    self._finish_heating(now, "SUNRISE_PEAK_EXCESS")
                    self.cycle = None
                    return
            if len(cycle.off_reports) < 4096:
                if previous is None or now > previous[0]:
                    cycle.off_reports.append((now, self.temperature))
            else:
                cycle.invalid_reason = "OBSERVATION_OVERFLOW"
            # Shared confirmation avoids ending learning on an initial dip or one noisy decline.
            # Related: history.py PeakTracker and runtime.py measured Peak comparisons.
            confirmed = cycle.peak_tracker is not None and cycle.peak_tracker.report(self.temperature, now)
            if (cycle.hold_crossing is not None and cycle.peak_tracker is not None
                    and cycle.peak_tracker.peak_at > cycle.hold_crossing[0]):
                cycle.hold_crossing = None
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
                end = cycle.hold_crossing[0] if cycle.hold_crossing else now
                if cycle.hold_settings is None:
                    end = min(end, cycle.peak_at + MAX_COOLING_SECONDS)
                while cycle.cooling_next_tick <= end:
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
                if self._finish_target_cooling(cycle):
                    return
            if cycle.peak_tracker is not None:
                cycle.candidate_peak_at = cycle.peak_tracker.peak_at
                cycle.candidate_peak_temperature = cycle.peak_tracker.peak_temperature
                # Reject two rises after an initial decline; related: history.py, frontend/panel.js.
                if not confirmed and previous is not None and now > previous[0]:
                    change = self.temperature - previous[1]
                    if cycle.peak_decline_seen and change > 1e-9:
                        cycle.peak_rebound_reports += 1
                    elif change < -1e-9:
                        cycle.peak_rebound_reports = 0
                    cycle.peak_decline_seen |= (
                        cycle.peak_tracker.decline_reports == 1
                        and cycle.peak_tracker.peak_temperature - self.temperature >= PEAK_REBOUND_DROP - 1e-9
                    )
                    if cycle.peak_rebound_reports >= 2:
                        reason = "REBOUND_BEFORE_PEAK_CONFIRMATION"
                        cycle.invalid_reason = cycle.invalid_reason or reason
                        if cycle.heating_saved:
                            self._finish_late_hold(now, reason)
                        else:
                            self._finish_heating(now, reason)
                        self.cycle = None
                        return
            if cycle.hold_settings is None and self._finish_sunrise_cooling(now, cycle):
                return
            self.advance(now)
        elif cycle.peak_at is not None:
            if cycle.hold_settings is None and self._finish_sunrise_cooling(now, cycle):
                return
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

    def switch(self, heater: bool | None, now: float, *, start_reason: str = "UNKNOWN",
               off_reason: str = "UNKNOWN", mode: str = "UNKNOWN",
               off_context: dict | None = None, off_slope: float | None = None,
               response_observed: bool | None = None,
               outdoor_temperature: float | None = None, target_peak_temperature: float | None = None,
               sunrise_saving: bool = False, sunrise_prediction: dict | None = None,
               sunrise_peak_excess: dict | None = None, hold_settings: dict | None = None,
               outdoor_expires_at: float | None = None) -> None:
        """Create or end an episode only at a confirmed aggregate switch transition."""
        # Invalidate before advancing a rejected outage; related: runtime.py.
        if heater is not None:
            self.advance(now)
        previous = self.heater
        if self._cooling_tail is not None and heater is not False:
            self._cooling_tail.close_incomplete(now, "NEXT_ON" if heater else "HEATER_UNAVAILABLE")
            self._drain_cooling_tail()
        if heater is None:
            self.invalidate("HEATER_UNAVAILABLE")
            if self.cycle is not None and self.cycle.hold_settings is not None:
                self.close_incomplete(now, "HEATER_UNAVAILABLE")
            self.last_off_at = None
            self.heater = None
            return
        if heater is previous:
            return
        if heater:
            self.last_off_at = None
            if self.cycle is not None:
                if self.cycle.peak_at is not None:
                    self._finish_cooling(now, "NEXT_ON")
                else:
                    self._finish_heating(now, "NEXT_ON_BEFORE_PEAK", automatic_restart=(
                        start_reason == "THRESHOLD_START" and len(self.cycle.off_reports) > 1
                        and target_peak_temperature is not None
                        and self.cycle.target_peak_temperature == target_peak_temperature))
                self.cycle = None
            if self.temperature is not None and previous is False:
                kind = ("PREDICTIVE_WARM_HEATING" if start_reason == "PREDICTIVE_START"
                        else "WARM_HEATING")
                self.cycle = CurveCycle(
                    str(uuid4()), now, start_reason, mode, kind, self.temperature,
                    self.temperature, now, now + BUCKET_SECONDS,
                )
                self.cycle.preset = self.preset
                self.cycle.hold_settings = hold_settings
                if hold_settings is not None:
                    self.cycle.hold_maximum = (now, self.temperature)
                self.cycle.outdoor_temperature = outdoor_temperature
                if outdoor_expires_at is not None:
                    self.cycle.heating_outdoor_reports.append((now, outdoor_temperature, outdoor_expires_at))
                self.cycle.on_history.add(now, self.temperature)
                if self.preset == "away":
                    self.cycle.invalid_reason = "AWAY_CYCLE"
        else:
            self.last_off_at = now if previous is True else None
            if self.cycle is not None and previous is True:
                self.cycle.outdoor_mean = self.heating_weather(now)
                self.cycle.heating_outdoor_reports.clear()
                self.cycle.outdoor_end = outdoor_temperature
                self.cycle.off_at = now
                self.cycle.off_temperature = self.temperature
                self.cycle.off_reason = off_reason
                self.cycle.target_peak_temperature = target_peak_temperature
                self.cycle.sunrise_saving = sunrise_saving
                self.cycle.sunrise_prediction = sunrise_prediction
                self.cycle.sunrise_peak_excess = sunrise_peak_excess
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
        # Freeze the MAE-selected horizon at OFF; related: runtime.py.
        sunrise_end = bool((cycle.sunrise_peak_excess or {}).get("ends_observation"))
        bucket_limit = (float("inf") if sunrise_end else
                        cycle.off_at + self.max_peak_wait_seconds
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
            cooling_limit = cycle.peak_at + MAX_COOLING_SECONDS
            end = now if cycle.hold_settings is not None else min(now, cooling_limit)
            while cycle.cooling_next_tick <= end and cycle.off_at is not None:
                if self.temperature is not None and cycle.cooling_last_temperature is not None:
                    index = int((cycle.cooling_next_tick - cycle.peak_at) / BUCKET_SECONDS) - 1
                    cycle.cooling_buckets[index] = round(self.temperature - cycle.cooling_last_temperature, 3)
                    cycle.cooling_last_temperature = self.temperature
                else:
                    cycle.invalid_reason = "SENSOR_UNAVAILABLE"
                cycle.cooling_last_tick = cycle.cooling_next_tick
                cycle.cooling_next_tick += BUCKET_SECONDS
            if cycle.hold_settings is None and now >= cooling_limit:
                self._finish_cooling(cooling_limit, "THREE_HOUR_TIMEOUT")
                self.cycle = None
                return
        limit = (cycle.off_at + self.max_peak_wait_seconds
                 if cycle.peak_at is None and cycle.off_at is not None and not sunrise_end else None)
        if limit is not None and now >= limit and not cycle.heating_saved:
            self._finish_heating(now, "PEAK_TIMEOUT")
            if cycle.hold_settings is not None and cycle.invalid_reason is None:
                # Continue cooling evidence after the control observer expires; related: runtime.py.
                tail = CurveTracker(self.max_peak_wait_seconds)
                tail.seed(False, self.temperature, now, confirmed_off_at=cycle.off_at)
                tail.cycle = cycle
                self._cooling_tail = tail
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
            profile, max_peak_wait_minutes=(float("inf") if
                (cycle.sunrise_peak_excess or {}).get("ends_observation") else self.max_peak_wait_seconds / 60)
        ) else None

    # Keep the saving ON profile independent of the qualified residual; related: runtime.py, curve_storage.py.
    def _sunrise_heating_result(self, cycle, original):
        if not cycle.sunrise_saving or cycle.off_at is None:
            return None
        buckets = {index: value for index, value in cycle.heating_buckets.items()
                   if cycle.started_at + (index + 1) * BUCKET_SECONDS <= cycle.off_at}
        residual = original.residual_rise
        target = cycle.target_peak_temperature
        peak = cycle.peak_temperature
        if (original.invalid_reason is not None or target is None or peak is None
                or not target - .1 <= peak <= target + .5 or residual is None or residual < 0):
            residual = None
        return replace(original, curve_type="SUNRISE_HEATING", sunrise_saving=False,
                       buckets=buckets, residual_rise=residual, off_profile=None,
                       heating_hold=None, invalid_reason=cycle.invalid_reason)

    def _finish_heating(self, now: float, reason: str, *, automatic_restart: bool = False) -> None:
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
            cycle.invalid_reason or (
                "SUNRISE_PEAK_EXCESS" if (cycle.sunrise_peak_excess or {}).get("first_at") is not None
                and cycle.sunrise_peak_excess["first_at"] <= now else
                "INCOMPLETE_PEAK" if cycle.peak_at is None else None
            ),
            cycle.preset, self._off_profile(cycle),
            cycle.baseline, cycle.off_temperature, cycle.peak_temperature, cycle.slope_at_off,
            peak_confirmation_drop=(cycle.peak_temperature - cycle.peak_tracker.previous_temperature
                                    if cycle.peak_at is not None and cycle.peak_tracker is not None
                                    and cycle.peak_tracker.previous_temperature is not None else None),
            peak_confirmation_reports=cycle.peak_tracker.decline_reports if cycle.peak_tracker else None,
            sunrise_peak_excess=cycle.sunrise_peak_excess,
            peak_confirmation_extra_drop=cycle.peak_tracker.confirmation_extra_drop if cycle.peak_tracker else None,
            outdoor_temperature=cycle.outdoor_temperature,
            outdoor_mean=cycle.outdoor_mean,
            outdoor_end=cycle.outdoor_end,
            target_peak_temperature=cycle.target_peak_temperature,
            observed_peak_temperature=cycle.candidate_peak_temperature,
            automatic_restart=automatic_restart,
            sunrise_saving=cycle.sunrise_saving,
            sunrise_prediction=cycle.sunrise_prediction,
            heating_hold=(self._hold_record(cycle, reason) if cycle.peak_at is None
                          and (reason != "PEAK_TIMEOUT" or cycle.invalid_reason) else None),
        ))
        saving = self._sunrise_heating_result(cycle, self.pending[-1])
        if saving is not None:
            self.pending.append(saving)

    # Use the measured mean through the current decision time; related: runtime.py.
    def heating_weather(self, now: float) -> float | None:
        cycle = self.cycle
        if cycle is None:
            return None
        if cycle.off_at is not None:
            return cycle.outdoor_mean
        if now <= cycle.started_at:
            return cycle.outdoor_temperature
        return heating_outdoor_mean(cycle.heating_outdoor_reports, cycle.started_at, now)

    # Pair cooling bins with outdoor reports on the same timeline; related: runtime.py.
    def report_cooling_outdoor(self, now: float, value: float | None, expires_at: float) -> None:
        if self._cooling_tail is not None:
            self._cooling_tail.report_cooling_outdoor(now, value, expires_at)
        cycle = self.cycle
        if cycle is not None and cycle.off_at is not None and self.heater is False:
            cycle.cooling_outdoor_reports.append((now, value, expires_at))
        elif cycle is not None and self.heater is True:
            cycle.heating_outdoor_reports.append((now, value, expires_at))

    @staticmethod
    def _cooling_outdoor_groups(cycle: CurveCycle) -> dict[int, str]:
        reports = cycle.cooling_outdoor_reports
        if not reports or cycle.peak_at is None:
            return {}
        groups = {}
        report_index = 0
        for index in sorted(cycle.cooling_buckets):
            cursor = cycle.peak_at + index * BUCKET_SECONDS
            end = cursor + BUCKET_SECONDS
            weighted = 0.0
            while cursor < end:
                while report_index + 1 < len(reports) and reports[report_index + 1][0] <= cursor:
                    report_index += 1
                at, value, expires_at = reports[report_index]
                next_at = reports[report_index + 1][0] if report_index + 1 < len(reports) else end
                stop = min(end, next_at)
                if at > cursor or value is None or expires_at < stop:
                    break
                weighted += value * (stop - cursor)
                cursor = stop
            if cursor == end:
                group = CurveStandards.outdoor_bucket(weighted / BUCKET_SECONDS)
                if group is not None:
                    groups[index] = group
        return groups

    # Stop recording at the measured threshold tied to this OFF decision; related: runtime.py.
    def _finish_sunrise_cooling(self, now: float, cycle: CurveCycle) -> bool:
        if (cycle.sunrise_saving and cycle.peak_at is not None and self.temperature is not None
                and cycle.target_peak_temperature is not None
                and self.temperature <= cycle.target_peak_temperature - 0.5 + 1e-9):
            self._finish_cooling(now, "SUNRISE_COOLING_TARGET")
            self.cycle = None
            return True
        return False

    # Finish at the actual crossing and keep a compact record apart from the bins.
    # Related: runtime.py, curve_storage.py, curve_api.py.
    def _finish_target_cooling(self, cycle: CurveCycle) -> bool:
        if cycle.hold_crossing is None or cycle.peak_at is None or cycle.hold_crossing[0] < cycle.peak_at:
            return False
        self._finish_cooling(cycle.hold_crossing[0], "TARGET_REACHED")
        self.cycle = None
        return True

    @staticmethod
    def _hold_record(cycle: CurveCycle, reason: str) -> dict | None:
        if cycle.hold_settings is None:
            return None
        invalid_reason = cycle.hold_exclusion or cycle.invalid_reason
        completed = reason == "TARGET_REACHED" and invalid_reason is None and cycle.hold_crossing is not None
        ended_at, outdoor_end = cycle.hold_crossing if completed else (None, None)
        outdoor_start = cycle.outdoor_temperature
        paired = outdoor_start is not None and outdoor_end is not None
        return {
            **cycle.hold_settings,
            "cycle_id": cycle.id, "started_at": cycle.started_at, "off_at": cycle.off_at,
            "ended_at": ended_at,
            "hold_seconds": ended_at - cycle.started_at if completed else None,
            "boiler_on_seconds": cycle.off_at - cycle.started_at if cycle.off_at is not None else None,
            "outdoor_start": outdoor_start, "outdoor_end": outdoor_end,
            "outdoor_mean": (outdoor_start + outdoor_end) / 2 if paired else None,
            "outdoor_change": outdoor_end - outdoor_start if paired else None,
            "peak_temperature": cycle.hold_maximum[1] if cycle.hold_maximum else None,
            "peak_at": cycle.hold_maximum[0] if cycle.hold_maximum else None,
            "peak_confirmed": cycle.peak_at is not None,
            "start_reason": cycle.start_reason, "preset": cycle.preset,
            "completed": completed, "exclusion_reason": None if completed else invalid_reason or reason,
            "sunrise_saving": cycle.sunrise_saving,
            "possible_solar_gain": (cycle.sunrise_peak_excess or {}).get("first_at") is not None,
        }

    def _finish_cooling(self, now: float, reason: str, *, include_unconfirmed: bool = False) -> None:
        cycle = self.cycle
        if cycle is None or (cycle.peak_at is None and not include_unconfirmed):
            return
        self.pending.append(CurveResult(
            cycle.id, "COOLING", cycle.start_reason, cycle.off_reason, reason,
            cycle.mode, cycle.started_at, cycle.off_at, cycle.peak_at, now,
            dict(cycle.cooling_buckets), None, cycle.invalid_reason, cycle.preset,
            start_temperature=cycle.baseline, off_temperature=cycle.off_temperature,
            peak_temperature=cycle.peak_temperature, slope_at_off=cycle.slope_at_off,
            peak_confirmation_drop=(cycle.peak_temperature - cycle.peak_tracker.previous_temperature
                                    if cycle.peak_temperature is not None and cycle.peak_tracker is not None
                                    and cycle.peak_tracker.previous_temperature is not None else None),
            peak_confirmation_reports=cycle.peak_tracker.decline_reports if cycle.peak_tracker else None,
            peak_confirmation_extra_drop=cycle.peak_tracker.confirmation_extra_drop if cycle.peak_tracker else None,
            cooling_outdoor_groups=self._cooling_outdoor_groups(cycle),
            target_peak_temperature=cycle.target_peak_temperature if cycle.sunrise_saving else None,
            sunrise_saving=cycle.sunrise_saving,
            heating_hold=self._hold_record(cycle, reason),
        ))

    def take_results(self) -> list[CurveResult]:
        results, self.pending = self.pending, []
        return results

    def _drain_cooling_tail(self) -> None:
        tail = self._cooling_tail
        self.pending.extend(tail.take_results())
        if tail.cycle is None:
            self._cooling_tail = None

    def _finish_late_hold(self, now: float, reason: str) -> None:
        cycle = self.cycle
        if cycle is not None and cycle.heating_saved and cycle.peak_at is None:
            self._finish_cooling(now, reason, include_unconfirmed=True)
            self.cycle = None

    def close_incomplete(self, now: float, reason: str) -> None:
        """Keep a restart boundary out of learned standards while retaining raw bins."""
        if self._cooling_tail is not None:
            self._cooling_tail.close_incomplete(now, reason)
            self._drain_cooling_tail()
        if self.cycle is None:
            return
        self.cycle.invalid_reason = reason
        if not self.cycle.heating_saved:
            self._finish_heating(now, reason)
        elif self.cycle.peak_at is None:
            self._finish_late_hold(now, reason)
        if self.cycle is not None and self.cycle.peak_at is not None:
            self._finish_cooling(now, reason)
        self.cycle = None
