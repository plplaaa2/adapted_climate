"""Observe bounded temperature history and heater cycles; related: runtime.py and sensor.py."""

from collections import deque
from dataclasses import dataclass
from math import isfinite


HISTORY_SECONDS = 24 * 60 * 60
HISTORY_MAX_POINTS = 4096
SLOPE_WINDOW_SECONDS = 45 * 60
SLOW_REPORT_SECONDS = 5 * 60
SLOW_REPORT_POINTS = 5
MIN_SLOPE_SPAN_SECONDS = 10 * 60
RESPONSE_SLOPE_THRESHOLD = 0.1
PEAK_SETTLE_SECONDS = 15 * 60
MIN_HEAT_LOSS_DELTA = 1.5
CURVE_STEP_SECONDS = 10 * 60
CURVE_MAX_MINUTES = 24 * 60


@dataclass(frozen=True)
class TemperatureSample:
    """One valid temperature report with a monotonic timestamp."""

    timestamp: float
    temperature: float


@dataclass(frozen=True)
class CompletedCycle:
    """Observed values from one complete heating and post-heating coast."""

    response_delay_minutes: float | None
    residual_rise: float
    peak_delay_minutes: float
    heating_rate_c_per_hour: float | None = None
    # Normalized elapsed-minute/temperature-rise pairs; related: thermal_model.py.
    response_curve: tuple[tuple[float, float], ...] | None = None
    coast_curve: tuple[tuple[float, float], ...] | None = None
    heating_duration_minutes: float | None = None
    slope_at_off: float | None = None
    curvature_at_off: float | None = None
    peak_temperature: float | None = None


class TemperatureHistory:
    """Keep a bounded rolling history and calculate robust window regression."""

    def __init__(self, *, retention: float = HISTORY_SECONDS, max_points: int = HISTORY_MAX_POINTS) -> None:
        self._retention = retention
        self._max_points = max_points
        self._samples: deque[TemperatureSample] = deque()

    @property
    def samples(self) -> tuple[TemperatureSample, ...]:
        """Return an immutable snapshot for diagnostics and tests."""
        return tuple(self._samples)

    def clear(self) -> None:
        """Discard observations across a sensor interruption."""
        self._samples.clear()

    def add(self, timestamp: float, temperature: float) -> bool:
        """Append a finite in-order report, pruning by age and maximum count."""
        if (not isfinite(timestamp) or not isfinite(temperature)
                or (self._samples and timestamp < self._samples[-1].timestamp)):
            return False
        self._samples.append(TemperatureSample(timestamp, temperature))
        cutoff = timestamp - self._retention
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()
        while len(self._samples) > self._max_points:
            self._samples.popleft()
        return True

    def slope(
        self, now: float, *, window: float = SLOPE_WINDOW_SECONDS,
        since: float | None = None, adaptive_reports: bool = False,
    ) -> float | None:
        """Return least-squares slope in °C/hour; require 3 points over 10 minutes."""
        # Adapt the displayed/AUTO slope to sparse reports; related: sensor.py, runtime.py.
        # Cycle-specific regressions retain their existing time windows.
        eligible = [
            sample for sample in self._samples
            if sample.timestamp <= now
            and (since is None or sample.timestamp >= since)
        ]
        slow_reports = (
            adaptive_reports and len(eligible) >= 2
            and eligible[-1].timestamp - eligible[-2].timestamp > SLOW_REPORT_SECONDS
        )
        cutoff = now - window
        points = [
            sample for sample in eligible if sample.timestamp >= cutoff
        ] if not slow_reports else eligible[-SLOW_REPORT_POINTS:]
        if len(points) < 3 or points[-1].timestamp - points[0].timestamp < MIN_SLOPE_SPAN_SECONDS:
            return None
        origin = points[0].timestamp
        xs = [sample.timestamp - origin for sample in points]
        ys = [sample.temperature for sample in points]
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        denominator = sum((x - x_mean) ** 2 for x in xs)
        if denominator == 0:
            return None
        per_second = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
        result = per_second * 3600
        return result if isfinite(result) else None


class ThermalObservation:
    """Track heater response and residual rise without feeding the controller."""

    def __init__(self, *, max_report_gap: float = 15 * 60) -> None:
        self.history = TemperatureHistory()
        self.max_report_gap = max_report_gap
        self.heater_state: bool | None = None
        self.heating_started: float | None = None
        self._response_delay_minutes: float | None = None
        self._heating_rate_c_per_hour: float | None = None
        # Keep one bounded ON trace for the learned nonlinear curve; related: thermal_model.py.
        self._on_samples: list[TemperatureSample] = []
        self._on_start_temperature: float | None = None
        self._on_curve_overflow = False
        self._off_curve: tuple[tuple[float, float], ...] | None = None
        self._off_duration_minutes: float | None = None
        self._slope_at_off: float | None = None
        self._curvature_at_off: float | None = None
        self._coast_samples: list[TemperatureSample] = []
        self._coast_overflow = False
        self.off_at: float | None = None
        self._off_temperature: float | None = None
        self._peak_temperature: float | None = None
        self._peak_at: float | None = None
        self.last_cycle: CompletedCycle | None = None
        self.completed_cycles = 0

    @property
    def temperature_slope(self) -> float | None:
        """Current rolling temperature slope in °C/hour."""
        if not self.history.samples:
            return None
        return self.history.slope(
            self.history.samples[-1].timestamp, adaptive_reports=True
        )

    @property
    def temperature_curvature(self) -> float | None:
        """Estimate acceleration from two disjoint 45-minute regressions in °C/h²."""
        if self.heating_started is None or not self.history.samples:
            return None
        now = self.history.samples[-1].timestamp
        if now - self.heating_started < 90 * 60:
            return None
        older = self.history.slope(now - 45 * 60, since=max(self.heating_started, now - 90 * 60))
        recent = self.history.slope(now, since=now - 45 * 60)
        if older is None or recent is None:
            return None
        return (recent - older) / 0.75

    def heating_profile(self, now: float, max_age: float) -> tuple[float, float, float | None] | None:
        """Return elapsed minutes, observed rise, and half-time rise for curve matching."""
        if (self.heating_started is None or self._on_start_temperature is None
                or not self._on_samples or now - self._on_samples[-1].timestamp > max_age):
            return None
        elapsed = (now - self.heating_started) / 60
        rise = self._on_samples[-1].temperature - self._on_start_temperature
        halfway = self.heating_started + (now - self.heating_started) / 2
        half_rise = None
        for left, right in zip(self._on_samples, self._on_samples[1:]):
            if left.timestamp <= halfway <= right.timestamp and right.timestamp > left.timestamp:
                fraction = (halfway - left.timestamp) / (right.timestamp - left.timestamp)
                half_rise = left.temperature + fraction * (right.temperature - left.temperature)
                half_rise -= self._on_start_temperature
                break
        return elapsed, rise, half_rise

    @property
    def off_temperature(self) -> float | None:
        """Expose this coast's OFF baseline for runtime.py residual prediction."""
        return self._off_temperature if self.off_at is not None else None

    @property
    def peak_temperature(self) -> float | None:
        """Current post-OFF peak while waiting for a stable decline."""
        return self._peak_temperature if self.off_at is not None else None

    def seed_heater(self, state: bool | None) -> None:
        """Set startup baseline without treating an interrupted cycle as complete."""
        self.heater_state = state
        self.heating_started = None
        self._response_delay_minutes = None
        self._heating_rate_c_per_hour = None
        self._clear_on_curve()
        self._clear_coast()

    def invalidate_cycle(self, state: bool | None) -> None:
        """Exclude manual heater changes from measured controlled cycles."""
        self.heater_state = state
        self._abort_active_cycle()

    def report_temperature(self, temperature: float | None, now: float) -> None:
        """Record valid reports and update response or post-OFF peak observation."""
        if temperature is None or not isfinite(temperature):
            self.history.clear()
            self._abort_active_cycle()
            return
        if (self.history.samples and now - self.history.samples[-1].timestamp > self.max_report_gap
                and (self.heating_started is not None or self.off_at is not None)):
            self._abort_active_cycle()
        if not self.history.add(now, temperature):
            self.history.clear()
            self._abort_active_cycle()
            return
        if self.heater_state is True and self.heating_started is not None:
            self._record_on_sample(now, temperature)
            if self._response_delay_minutes is None:
                slope = self.history.slope(now, since=self.heating_started)
                if slope is not None and slope >= RESPONSE_SLOPE_THRESHOLD:
                    self._response_delay_minutes = (now - self.heating_started) / 60
        elif self.heater_state is False and self.off_at is not None:
            # Confirm the last plateau report only on an actual decline; related: curve_learning.py.
            previous_coast = self._coast_samples[-1] if self._coast_samples else None
            if not self._coast_samples or now > self._coast_samples[-1].timestamp:
                if len(self._coast_samples) < HISTORY_MAX_POINTS:
                    self._coast_samples.append(TemperatureSample(now, temperature))
                else:
                    self._coast_overflow = True
            if self._peak_temperature is None or temperature >= self._peak_temperature:
                self._peak_temperature = temperature
                self._peak_at = now
            if (previous_coast is not None and temperature < previous_coast.temperature
                    and self._peak_at is not None):
                self._peak_temperature = previous_coast.temperature
                self._peak_at = previous_coast.timestamp
                self.last_cycle = CompletedCycle(
                    response_delay_minutes=self._response_delay_minutes,
                    residual_rise=max(0.0, self._peak_temperature - self._off_temperature),
                    peak_delay_minutes=(self._peak_at - self.off_at) / 60,
                    heating_rate_c_per_hour=self._heating_rate_c_per_hour,
                    response_curve=self._off_curve,
                    coast_curve=self._build_coast_curve(),
                    heating_duration_minutes=self._off_duration_minutes,
                    slope_at_off=self._slope_at_off,
                    curvature_at_off=self._curvature_at_off,
                    peak_temperature=self._peak_temperature,
                )
                self.completed_cycles += 1
                self._clear_coast()

    def observe_heater(self, state: bool | None, now: float, temperature: float | None) -> None:
        """Track confirmed switch edges; unknown states abort partial cycles."""
        previous = self.heater_state
        if state is None:
            self._abort_active_cycle()
            self._clear_coast()
            self.heater_state = None
            return
        if state == previous:
            return
        self.heater_state = state
        if state:
            self._clear_coast()
            self.heating_started = now
            self._response_delay_minutes = None
            self._heating_rate_c_per_hour = None
            self._clear_on_curve()
            if temperature is not None and isfinite(temperature):
                self._on_start_temperature = temperature
                self._on_samples.append(TemperatureSample(now, temperature))
            return
        if self.heating_started is None or temperature is None:
            self._abort_active_cycle()
            return
        self.off_at = now
        self._off_temperature = temperature
        self._peak_temperature = temperature
        self._peak_at = now
        self._heating_rate_c_per_hour = self.history.slope(now, since=self.heating_started)
        self._slope_at_off = self.temperature_slope
        self._curvature_at_off = self.temperature_curvature
        self._record_on_sample(now, temperature)
        self._off_duration_minutes = (now - self.heating_started) / 60
        self._off_curve = self._build_on_curve(now)
        self._clear_on_curve()
        self.heating_started = None
        self._coast_samples = [TemperatureSample(now, temperature)]
        self._coast_overflow = False

    def _record_on_sample(self, now: float, temperature: float) -> None:
        """Keep only actual reports and confirmed-edge temperatures in the ON trace."""
        if self._on_curve_overflow or self._on_start_temperature is None:
            return
        if self._on_samples and now == self._on_samples[-1].timestamp:
            self._on_samples[-1] = TemperatureSample(now, temperature)
        elif not self._on_samples or now > self._on_samples[-1].timestamp:
            if len(self._on_samples) >= HISTORY_MAX_POINTS:
                self._on_curve_overflow = True
                self._on_samples.clear()
            else:
                self._on_samples.append(TemperatureSample(now, temperature))

    def _build_on_curve(self, off_at: float) -> tuple[tuple[float, float], ...] | None:
        """Interpolate an observed ON trajectory onto a ten-minute elapsed-time grid."""
        points = self._on_samples
        if (self._on_curve_overflow or self._on_start_temperature is None
                or len(points) < 3 or off_at - points[0].timestamp < 30 * 60
                or off_at - points[0].timestamp > CURVE_MAX_MINUTES * 60):
            return None
        end = off_at - points[0].timestamp
        times = list(range(0, int(end), CURVE_STEP_SECONDS)) + [end]
        curve = []
        index = 0
        for elapsed in times:
            at = points[0].timestamp + elapsed
            while index + 1 < len(points) - 1 and points[index + 1].timestamp < at:
                index += 1
            left, right = points[index], points[index + 1]
            if right.timestamp == left.timestamp:
                return None
            fraction = (at - left.timestamp) / (right.timestamp - left.timestamp)
            rise = left.temperature + fraction * (right.temperature - left.temperature)
            curve.append((round(elapsed / 60, 4), round(rise - self._on_start_temperature, 4)))
        return tuple(curve)

    def _clear_on_curve(self) -> None:
        self._on_samples.clear()
        self._on_start_temperature = None
        self._on_curve_overflow = False

    def _build_coast_curve(self) -> tuple[tuple[float, float], ...] | None:
        """Normalize the observed OFF-to-peak trajectory to its total coast rise."""
        if (self.off_at is None or self._peak_at is None or self._off_temperature is None
                or self._peak_temperature is None or self._peak_temperature - self._off_temperature < 0.1
                or self._peak_at <= self.off_at or self._coast_overflow):
            return None
        samples = [point for point in self._coast_samples if point.timestamp <= self._peak_at]
        if len(samples) < 2:
            return None
        duration = self._peak_at - self.off_at
        if duration > CURVE_MAX_MINUTES * 60:
            return None
        times = list(range(0, int(duration), CURVE_STEP_SECONDS)) + [duration]
        result = []
        index = 0
        for elapsed in times:
            at = self.off_at + elapsed
            while index + 1 < len(samples) - 1 and samples[index + 1].timestamp < at:
                index += 1
            left, right = samples[index], samples[index + 1]
            if right.timestamp == left.timestamp:
                return None
            fraction = (at - left.timestamp) / (right.timestamp - left.timestamp)
            rise = left.temperature + fraction * (right.temperature - left.temperature)
            normalized = (rise - self._off_temperature) / (self._peak_temperature - self._off_temperature)
            result.append((round(elapsed / 60, 4), round(normalized, 5)))
        return tuple(result)

    def _abort_active_cycle(self) -> None:
        self.heating_started = None
        self._response_delay_minutes = None
        self._heating_rate_c_per_hour = None
        self._clear_on_curve()
        self._clear_coast()

    def _clear_coast(self) -> None:
        self.off_at = None
        self._off_temperature = None
        self._peak_temperature = None
        self._peak_at = None
        self._off_curve = None
        self._off_duration_minutes = None
        self._slope_at_off = None
        self._curvature_at_off = None
        self._coast_samples.clear()
        self._coast_overflow = False


class HeatLossObservation:
    """Measure one post-heating heat-loss rate after the indoor peak settles."""

    def __init__(self, *, use_supply_sensor: bool = False, use_return_sensor: bool = False) -> None:
        self.history = TemperatureHistory()
        self.use_supply_sensor = use_supply_sensor
        self.use_return_sensor = use_return_sensor
        self.heater_state: bool | None = None
        self._heated = False
        self._off_at: float | None = None
        self._peak_temperature: float | None = None
        self._peak_at: float | None = None
        self._deltas: deque[tuple[float, float]] = deque()
        self._sampled_this_coast = False

    def seed_heater(self, state: bool | None) -> None:
        """Set startup state without treating an interrupted cycle as a new cycle."""
        self.heater_state = state
        self._heated = False
        self._reset_coast()

    def observe_heater(self, state: bool | None, now: float) -> None:
        """Start a post-heat observation only after a confirmed ON-to-OFF cycle."""
        previous = self.heater_state
        if state is None:
            self.heater_state = None
            self._heated = False
            self._reset_coast()
            return
        if state == previous:
            return
        self.heater_state = state
        if state:
            self._heated = previous is False
            self._reset_coast()
        else:
            if self._heated and previous is True:
                self._off_at = now
            else:
                self._off_at = None
            self.history.clear()
            self._peak_temperature = None
            self._peak_at = None
            self._deltas.clear()
            self._sampled_this_coast = False

    def report_temperature(
        self,
        temperature: float | None,
        outdoor_temperature: float | None,
        now: float,
        *,
        supply_temperature: float | None = None,
        return_temperature: float | None = None,
    ) -> float | None:
        """Return a normalized cooling rate once this coast has a stable decline."""
        if (self.heater_state is not False or not self._heated or self._off_at is None):
            return None
        if (temperature is None or not isfinite(temperature)
                or outdoor_temperature is None or not isfinite(outdoor_temperature)):
            self._clear_post_peak_samples()
            return None
        self.history.add(now, temperature)
        if self._peak_temperature is None or temperature > self._peak_temperature:
            self._peak_temperature = temperature
            self._peak_at = now
            self._clear_post_peak_samples()
            return None
        if self._sampled_this_coast or self._peak_at is None:
            return None
        if temperature >= self._peak_temperature:
            return None
        if outdoor_temperature >= temperature - MIN_HEAT_LOSS_DELTA:
            self._clear_post_peak_samples()
            return None
        if (self.use_supply_sensor and (
                supply_temperature is None or not isfinite(supply_temperature)
                or supply_temperature > temperature
        )):
            self._clear_post_peak_samples()
            return None
        if (self.use_return_sensor and (
                return_temperature is None or not isfinite(return_temperature)
                or return_temperature > temperature
        )):
            self._clear_post_peak_samples()
            return None
        if now - self._peak_at < PEAK_SETTLE_SECONDS:
            return None

        self._deltas.append((now, temperature - outdoor_temperature))
        cutoff = now - SLOPE_WINDOW_SECONDS
        while self._deltas and self._deltas[0][0] < cutoff:
            self._deltas.popleft()
        slope = self.history.slope(now, since=self._peak_at)
        if slope is None or slope > -RESPONSE_SLOPE_THRESHOLD or not self._deltas:
            return None
        mean_delta = sum(delta for _, delta in self._deltas) / len(self._deltas)
        rate = -slope / mean_delta
        if not isfinite(rate) or rate <= 0:
            return None
        self._sampled_this_coast = True
        return rate

    def invalidate(self) -> None:
        """Break a cooling sample sequence after invalid or stale sensor reports."""
        self._clear_post_peak_samples()

    def _clear_post_peak_samples(self) -> None:
        self.history.clear()
        self._deltas.clear()

    def _reset_coast(self) -> None:
        self._off_at = None
        self._peak_temperature = None
        self._peak_at = None
        self._sampled_this_coast = False
        self._clear_post_peak_samples()
