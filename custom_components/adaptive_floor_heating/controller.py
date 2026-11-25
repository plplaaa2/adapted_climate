"""Pure thermostat decisions and input recovery; related: runtime.py, const.py."""

from dataclasses import dataclass
from math import isfinite
from typing import Any

from .const import (
    CONF_COLD_TOLERANCE, CONF_HOT_TOLERANCE, CONF_MIN_OFF, CONF_MIN_ON,
    CONF_HEAT_HOT_TOLERANCE, DEFAULT_HEAT_HOT_TOLERANCE,
    CONF_SENSOR_TIMEOUT, DEFAULT_MIN_OFF, DEFAULT_MIN_ON, DEFAULT_SENSOR_TIMEOUT,
    CONF_HEATER_UNAVAILABLE_GRACE, DEFAULT_HEATER_UNAVAILABLE_GRACE, MAX_HEATER_UNAVAILABLE_GRACE,
    CONF_INITIAL_MAX_ON_MINUTES, MIN_INITIAL_MAX_ON_MINUTES, MAX_INITIAL_MAX_ON_MINUTES,
    CONF_PEAK_OBSERVATION_HOURS, DEFAULT_PEAK_OBSERVATION_HOURS,
    MIN_PEAK_OBSERVATION_HOURS, MAX_PEAK_OBSERVATION_HOURS,
    DEFAULT_AWAY_TEMPERATURE, DEFAULT_HOME_TEMPERATURE, DEFAULT_TARGET, DEFAULT_TOLERANCE, MAX_TARGET, MIN_TARGET,
    CONF_HOME_TEMPERATURE, CONF_AWAY_TEMPERATURE,
    OVERHEAT_RESET, OVERHEAT_TEMPERATURE, RECOVERY_INTERVAL,
)


@dataclass(frozen=True)
class Settings:
    """Validated, immutable control parameters in Celsius and seconds."""

    cold_tolerance: float = DEFAULT_TOLERANCE
    hot_tolerance: float = DEFAULT_TOLERANCE
    heat_hot_tolerance: float = DEFAULT_HEAT_HOT_TOLERANCE
    minimum_on_time: int = DEFAULT_MIN_ON
    minimum_off_time: int = DEFAULT_MIN_OFF
    sensor_timeout: int = DEFAULT_SENSOR_TIMEOUT
    peak_observation_hours: int = DEFAULT_PEAK_OBSERVATION_HOURS
    home_temperature: float = DEFAULT_HOME_TEMPERATURE
    away_temperature: float = DEFAULT_AWAY_TEMPERATURE
    heater_unavailable_grace: int = DEFAULT_HEATER_UNAVAILABLE_GRACE
    initial_maximum_on_minutes: int = MIN_INITIAL_MAX_ON_MINUTES

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> "Settings":
        """Reject invalid settings before starting any actuator."""
        values = {}
        for key, lower, upper, integer in (
            (CONF_COLD_TOLERANCE, 0.1, 2.0, False),
            (CONF_HOT_TOLERANCE, 0.1, 2.0, False),
            (CONF_HEAT_HOT_TOLERANCE, 0.0, 2.0, False),
            (CONF_MIN_ON, 0, 3600, True),
            (CONF_INITIAL_MAX_ON_MINUTES, MIN_INITIAL_MAX_ON_MINUTES, MAX_INITIAL_MAX_ON_MINUTES, True),
            (CONF_MIN_OFF, 0, 3600, True),
            (CONF_SENSOR_TIMEOUT, 60, 3600, True),
            (CONF_HEATER_UNAVAILABLE_GRACE, 0, MAX_HEATER_UNAVAILABLE_GRACE, True),
            (CONF_PEAK_OBSERVATION_HOURS, MIN_PEAK_OBSERVATION_HOURS,
             MAX_PEAK_OBSERVATION_HOURS, True),
            (CONF_HOME_TEMPERATURE, MIN_TARGET, MAX_TARGET, False),
            (CONF_AWAY_TEMPERATURE, MIN_TARGET, MAX_TARGET, False),
        ):
            value = options.get(key, getattr(cls(), key))
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not isfinite(value) or not lower <= value <= upper
                    or (integer and value != int(value))):
                raise ValueError(key)
            values[key] = int(value) if integer else float(value)
            if key in (CONF_HOME_TEMPERATURE, CONF_AWAY_TEMPERATURE):
                values[key] = round(values[key], 1)
        return cls(**values)


@dataclass(frozen=True)
class Decision:
    """Desired heater state, explanation, and optional monotonic deadline."""

    heating: bool
    state: str
    deadline: float | None = None


def temperature_celsius(value: Any, unit: str | None, device_class: str | None) -> float | None:
    """Convert only finite temperature measurements; never use a cached fallback."""
    if device_class != "temperature" or unit not in ("°C", "°F") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not isfinite(result):
        return None
    result = (result - 32) * 5 / 9 if unit == "°F" else result
    return result if isfinite(result) else None


class ThermostatController:
    """Keep thermostat intent separate from observed actuator state."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mode = "off"
        self.target = DEFAULT_TARGET
        self.faults: set[str] = set()
        self.auto_external_override = False
        self.temperature: float | None = None
        self.last_report: float | None = None
        self.sensor_fault = False
        self.recovery_started: float | None = None
        self.startup_off_seen = False
        self.stopping = False
        # One-shot waiver of the OFF wait right after a cleared FAULT; related: set_mode(), decide().
        self.fault_recovery = False

    def report_temperature(self, temperature: float | None, now: float) -> None:
        """Count new reports, including unchanged values, toward sensor recovery."""
        self.expire_sensor(now)
        self.temperature = temperature
        if temperature is None:
            self.sensor_fault = True
            self.recovery_started = None
            self.last_report = None
            return
        self.last_report = now
        if temperature >= OVERHEAT_TEMPERATURE:
            self.faults.add("overheat")
        if self.sensor_fault:
            if self.recovery_started is None:
                self.recovery_started = now
            elif now - self.recovery_started >= RECOVERY_INTERVAL:
                self.sensor_fault = False
                self.recovery_started = None

    def expire_sensor(self, now: float) -> None:
        """Expire a report on a timer even if no new state event arrives."""
        if (self.last_report is not None
                and now - self.last_report >= self.settings.sensor_timeout):
            self.sensor_fault = True
            self.recovery_started = None
            self.last_report = None
            self.temperature = None

    def sensor_ready(self, now: float) -> bool:
        """Require a report from this execution, with completed recovery."""
        self.expire_sensor(now)
        return self.last_report is not None and not self.sensor_fault

    def set_target(self, value: Any) -> None:
        """Validate target changes without implicitly changing HVAC mode."""
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not isfinite(value) or not MIN_TARGET <= value <= MAX_TARGET):
            raise ValueError("Target temperature must be between 18 and 30°C")
        self.target = round(float(value), 1)

    # Clear a FAULT without the minimum OFF wait; related: runtime.py, decide(), storage.py.
    def set_mode(self, mode: str, now: float, heater: bool | None, changed_at: float) -> None:
        """Clear an eligible fault when the user selects HEAT or AUTO.

        ``changed_at`` stays in the signature for the runtime.py call contract; a FAULT
        recovery no longer waits for the minimum OFF time, which used to re-lock the
        thermostat for 30 minutes after every override or sensor glitch. A heater that
        is already ON no longer blocks the recovery either: the runtime stops it for a
        confirmed OFF handover, so the user can select HEAT while the boiler runs.
        """
        if mode not in ("off", "heat", "auto"):
            raise ValueError("Only OFF, HEAT and AUTO are supported")
        if mode in ("heat", "auto") and self.faults:
            if (not self.sensor_ready(now) or heater is None
                    or ("overheat" in self.faults and self.temperature > OVERHEAT_RESET)):
                raise ValueError("Wait for a valid temperature and a known heater state")
            self.faults.clear()
            self.auto_external_override = False
            # A fault held the heater OFF, so the next decision starts heating right
            # away; ordinary cycles keep decide()'s WAIT_MIN_OFF. When the heater was
            # already ON, decide() ends this waiver on its next call.
            self.fault_recovery = True
        if mode == "off":
            self.fault_recovery = False
        self.mode = mode

    # Accept an AUTO stop target from daylight saving; related: runtime.py.
    def decide(self, now: float, heater: bool | None, changed_at: float,
               minimum_off_deadline: float | None = None, *,
               heating_stop_target: float | None = None) -> Decision:
        """Apply safety first, then hysteresis and minimum transition times.

        ``minimum_off_deadline`` may extend, but never shorten, the configured
        minimum OFF time after a measured heating run.
        """
        ready = self.sensor_ready(now)
        if heater is True or self.mode == "off":
            self.fault_recovery = False
        if self.temperature is not None and self.temperature >= OVERHEAT_TEMPERATURE:
            self.faults.add("overheat")
        if self.faults or self.sensor_fault or heater is None:
            return Decision(False, "FAULT")
        if self.stopping or self.mode == "off":
            return Decision(False, "OFF")
        if not ready or not self.startup_off_seen:
            return Decision(False, "STARTUP")
        if heater:
            hot_tolerance = (self.settings.heat_hot_tolerance if self.mode == "heat"
                             else self.settings.hot_tolerance)
            stop_target = self.target + hot_tolerance if heating_stop_target is None else heating_stop_target
            if self.temperature >= stop_target:
                end = changed_at + self.settings.minimum_on_time
                return Decision(True, "WAIT_MIN_ON", end) if now < end else Decision(False, "IDLE")
            return Decision(True, "HEATING")
        if self.temperature <= self.target - self.settings.cold_tolerance:
            if self.fault_recovery:
                # Keep starting until the heater reports ON: the fault already held it OFF,
                # and a repeated decision must not cancel the pending start.
                return Decision(True, "HEATING")
            end = changed_at + self.settings.minimum_off_time
            if minimum_off_deadline is not None and isfinite(minimum_off_deadline):
                end = max(end, minimum_off_deadline)
            return Decision(False, "WAIT_MIN_OFF", end) if now < end else Decision(True, "HEATING")
        return Decision(False, "IDLE")
