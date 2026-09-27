"""Independent pipe diagnostics, never control inputs; related: runtime.py, sensor.py."""

from .history import TemperatureHistory, ThermalObservation

WATER_KEYS = (
    "water_supply", "water_return", "water_difference", "water_supply_room",
    "water_return_room", "water_supply_slope", "water_return_slope",
    "water_off_supply", "water_off_return", "water_residual_rise",
    "water_peak_delay", "water_status",
)


class WaterObservation:
    """Collect actual reports and publish matched results from completed cycles."""

    def __init__(self, *, supply=False, return_sensor=False, timeout=900):
        self.configured = {"room": True, "supply": supply, "return": return_sensor}
        self.timeout = timeout
        self.histories = {key: TemperatureHistory() for key in self.configured}
        self.cycle = ThermalObservation()
        self.pending = None
        self.completed = dict.fromkeys(WATER_KEYS[7:11])

    def value(self, key, now):
        samples = self.histories[key].samples
        if not samples or now - samples[-1].timestamp >= self.timeout:
            return None
        return samples[-1].temperature

    def invalidate(self):
        state = self.cycle.heater_state
        self.cycle = ThermalObservation()
        self.cycle.seed_heater(state)
        self.pending = None

    def report(self, key, value, now):
        """Break interrupted sequences; cached reads never enter these histories."""
        history = self.histories[key]
        if value is None:
            history.clear()
            self.invalidate()
            return
        if history.samples and now - history.samples[-1].timestamp >= self.timeout:
            history.clear()
            self.invalidate()
        if not history.add(now, value):
            return
        if key == "room":
            if any(self.configured[k] and self.value(k, now) is None
                   for k in ("supply", "return")):
                self.invalidate()
            previous = self.cycle.completed_cycles
            self.cycle.report_temperature(value, now)
            if self.cycle.completed_cycles > previous and self.pending is not None:
                cycle = self.cycle.last_cycle
                self.completed = {
                    "water_off_supply": self.pending[0],
                    "water_off_return": self.pending[1],
                    "water_residual_rise": cycle.residual_rise,
                    "water_peak_delay": cycle.peak_delay_minutes,
                }
                self.pending = None

    def heater(self, state, now):
        """Only confirmed complete transitions may start or finish a pipe cycle."""
        previous = self.cycle.heater_state
        if state is None:
            self.invalidate()
            self.cycle.seed_heater(None)
            return
        if previous is None:
            self.cycle.seed_heater(state)
            return
        room = self.value("room", now)
        if state is False and previous is True:
            if room is None or any(self.configured[k] and self.value(k, now) is None
                                   for k in ("supply", "return")):
                self.invalidate()
                self.cycle.seed_heater(False)
                return
            self.pending = (self.value("supply", now), self.value("return", now))
        elif state is True and previous is False:
            self.pending = None
        self.cycle.observe_heater(state, now, room)

    def snapshot(self, now):
        """Return values only, with no command or model updates; related: sensor.py."""
        values = {key: self.value(key, now) for key in self.histories}
        result = dict.fromkeys(WATER_KEYS)
        result.update(self.completed)
        result.update(water_supply=values["supply"], water_return=values["return"])
        for key in ("supply", "return"):
            history = self.histories[key]
            if values[key] is not None:
                result[f"water_{key}_slope"] = history.slope(now, adaptive_reports=True)
        for name, left, right in (
            ("water_difference", "supply", "return"),
            ("water_supply_room", "supply", "room"),
            ("water_return_room", "return", "room"),
        ):
            if values[left] is not None and values[right] is not None:
                skew = abs(self.histories[left].samples[-1].timestamp
                           - self.histories[right].samples[-1].timestamp)
                if skew <= min(300, self.timeout):
                    result[name] = values[left] - values[right]
        missing = any(enabled and values[key] is None for key, enabled in self.configured.items())
        result["water_status"] = (
            "not_configured" if not (self.configured["supply"] or self.configured["return"])
            else "missing_input" if missing else "observing_residual" if self.cycle.off_at is not None
            else "heating" if self.cycle.heater_state is True
            else "completed" if self.completed["water_residual_rise"] is not None else "waiting"
        )
        return result
