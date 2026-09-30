"""Own events, timers, and safe lifecycle; related: climate.py, actuator.py, storage.py, thermal_model.py."""

import asyncio
from collections.abc import Callable
import logging
import time

from .actuator import SwitchActuator
from .const import (
    CONF_HEATER, CONF_MODE, CONF_OUTDOOR_TEMPERATURE_SENSOR,
    CONF_RETURN_TEMPERATURE_SENSOR, CONF_SUPPLY_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_SENSOR, CONF_ZONES,
    MODE_MULTI_ZONE_INTEGRATED, DEFAULT_LEARNING_MODEL, DEFAULT_PREDICTION_MODE,
    LEARNING_MODELS, PREDICTION_MODES,
)
from .controller import Decision, Settings, ThermostatController, temperature_celsius
from .curve_learning import CurveTracker, BUCKET_SECONDS, MAX_AFTER_OFF_SECONDS, PEAK_SETTLE_SECONDS
from .curve_storage import CurveStore
from .history import HeatLossObservation, RESPONSE_SLOPE_THRESHOLD, ThermalObservation
from .storage import RuntimeStore, ThermalLearningStore
from .thermal_model import ThermalLearningModel
from .water_observation import WaterObservation

_LOGGER = logging.getLogger(__name__)


class HeatingRuntime:
    """Run one standalone thermostat on HA's event loop with one actuator owner."""

    def __init__(self, hass, entry, settings: Settings) -> None:
        self.hass, self.entry = hass, entry
        self.heaters = (
            tuple(zone[CONF_HEATER] for zone in entry.data.get(CONF_ZONES, []))
            if entry.data.get(CONF_MODE) == MODE_MULTI_ZONE_INTEGRATED
            else (entry.data[CONF_HEATER],)
        )
        self.heater = self.heaters[0]
        self._heater_states: dict[str, bool | None] = {
            entity_id: None for entity_id in self.heaters
        }
        self.sensor = entry.data[CONF_TEMPERATURE_SENSOR]
        self.temperature_last_reported = None
        options = entry.options
        self.outdoor_sensor = options.get(CONF_OUTDOOR_TEMPERATURE_SENSOR)
        self.supply_sensor = options.get(CONF_SUPPLY_TEMPERATURE_SENSOR)
        self.return_sensor = options.get(CONF_RETURN_TEMPERATURE_SENSOR)
        self._optional_sensors = {
            entity_id for entity_id in (
                self.outdoor_sensor, self.supply_sensor, self.return_sensor
            ) if entity_id
        }
        self._optional_reported_at: dict[str, float | None] = {
            entity_id: None for entity_id in self._optional_sensors
        }
        # Keep pipe experiments independent of control and learning; related: water_observation.py.
        self.water_observation = WaterObservation(
            supply=bool(self.supply_sensor), return_sensor=bool(self.return_sensor),
            timeout=settings.sensor_timeout,
        )
        self.controller = ThermostatController(settings)
        self.observation = ThermalObservation(max_report_gap=settings.sensor_timeout)
        self.heat_loss_observation = HeatLossObservation(
            use_supply_sensor=bool(self.supply_sensor),
            use_return_sensor=bool(self.return_sensor),
        )
        self.store = RuntimeStore(hass, entry.entry_id)
        self.learning_store = ThermalLearningStore(hass, entry.entry_id)
        self.thermal_model = ThermalLearningModel()
        self.preset = "home"
        self.prediction_mode = DEFAULT_PREDICTION_MODE
        # Curve observation is separate from the existing thermal model and actuator.
        # Related: curve_learning.py, curve_storage.py, select.py.
        self.learning_model = DEFAULT_LEARNING_MODEL
        self.curve_tracker = CurveTracker()
        self.curve_store = (
            CurveStore(hass, entry.entry_id)
            if callable(getattr(getattr(hass, "config", None), "path", None)) else None
        )
        self._curve_tasks: set[asyncio.Task] = set()
        self._curve_cleanup_at = 0.0
        self.curve_fallback_reason: str | None = None
        self._curve_storage_failed = False
        self._off_transition_pending = False
        self.actuator = SwitchActuator(
            self._send, self._queue_evaluate, self._fault, hass.loop.time
        )
        self.decision = Decision(False, "STARTUP")
        self.started = False
        self._closed = False
        self._timer = None
        self._queued = None
        self._start_lock = asyncio.Lock()
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: set[Callable[[], None]] = set()

    async def async_start(self) -> None:
        """Serialize entity-triggered starts so shared state initializes once."""
        async with self._start_lock:
            await self._async_start_locked()

    async def _async_start_locked(self) -> None:
        """Restore intent, subscribe, and establish a new startup OFF baseline."""
        if self.started:
            return
        from homeassistant.core import callback

        data = await self.store.load(
            default_target=self.controller.settings.home_temperature,
            home_temperature=self.controller.settings.home_temperature,
            away_temperature=self.controller.settings.away_temperature,
        )
        self.controller = ThermostatController(self.controller.settings)
        self.water_observation = WaterObservation(
            supply=bool(self.supply_sensor), return_sensor=bool(self.return_sensor),
            timeout=self.controller.settings.sensor_timeout,
        )
        self.observation = ThermalObservation(
            max_report_gap=self.controller.settings.sensor_timeout
        )
        self.thermal_model = ThermalLearningModel()
        self.heat_loss_observation = HeatLossObservation(
            use_supply_sensor=bool(self.supply_sensor),
            use_return_sensor=bool(self.return_sensor),
        )
        try:
            learned = await self.learning_store.load()
            if learned is not None:
                self.thermal_model = ThermalLearningModel.from_snapshot(learned)
        except Exception as err:
            _LOGGER.warning("Thermal model could not be restored (%s); restarting learning", type(err).__name__)
        self.controller.target, self.controller.mode = data["target"], data["mode"]
        self.preset = data["preset"]
        self.prediction_mode = data["prediction_mode"]
        self.learning_model = data["learning_model"]
        self.curve_tracker = CurveTracker()
        self._curve_storage_failed = False
        if self.curve_store is not None:
            try:
                await self.curve_store.open()
                await self.curve_store.cleanup()
            except Exception as err:
                _LOGGER.error("Curve storage could not be opened (%s)", type(err).__name__)
                self._curve_storage_failed = True
                self.curve_store = None
        self.controller.faults = set(data["faults"])
        self._closed = False
        self.started = True
        now = self.hass.loop.time()
        self._curve_cleanup_at = now + 24 * 3600
        initial_sensor_state = self.hass.states.get(self.sensor)
        self.temperature_last_reported = getattr(initial_sensor_state, "last_reported", None)
        for entity_id in self._optional_sensors:
            self._optional_reported_at[entity_id] = (
                now if self._temperature(self.hass.states.get(entity_id)) is not None
                else None
            )

        @callback
        def event_filter(data):
            return data.get("entity_id") in (
                *self.heaters, self.sensor, *self._optional_sensors
            )

        @callback
        def event_received(event):
            self._state_event(event.data)

        # Unchanged temperature reports must enter history immediately; related: history.py.
        self._unsubs.append(self.hass.bus.async_listen(
            "state_changed", event_received, event_filter=event_filter
        ))
        self._unsubs.append(self.hass.bus.async_listen(
            "state_reported", event_received,
            event_filter=event_filter,
        ))
        self._unsubs.append(self.hass.bus.async_listen_once(
            "homeassistant_stop", self._shutdown
        ))
        for entity_id in self.heaters:
            self._heater_states[entity_id] = self._switch_state(
                self.hass.states.get(entity_id)
            )
        observed = self._aggregate_heater_state()
        self.actuator.observe(observed, initial=True)
        self.observation.seed_heater(observed)
        self.heat_loss_observation.seed_heater(observed)
        self.water_observation.heater(observed, now)
        self.controller.startup_off_seen = observed is False
        if observed is None:
            self._fault("heater_unavailable")
        if len(self.heaters) > 1 and observed is not False:
            self.actuator.request(False, retry=True, force=True)
        initial_temperature = self._temperature(self.hass.states.get(self.sensor))
        self.curve_tracker.seed(observed, initial_temperature, time.time())
        self.controller.report_temperature(initial_temperature, self.hass.loop.time())
        self.evaluate()

    @staticmethod
    def _switch_state(state) -> bool | None:
        if state is None or state.state not in ("on", "off"):
            return None
        return state.state == "on"

    @staticmethod
    def _temperature(state) -> float | None:
        if state is None:
            return None
        return temperature_celsius(
            state.state, state.attributes.get("unit_of_measurement"),
            state.attributes.get("device_class"),
        )

    def _state_event(self, data) -> None:
        """Consume current observations synchronously before dispatching decisions."""
        if self._closed:
            return
        entity_id = data.get("entity_id")
        state = data.get("new_state")
        now = self.hass.loop.time()
        if entity_id in self._optional_sensors:
            self._optional_reported_at[entity_id] = (
                now if self._temperature(state) is not None else None
            )
        # Feed actual reports only, including entities reused in multiple roles.
        for role, selected in (("room", self.sensor), ("supply", self.supply_sensor),
                               ("return", self.return_sensor)):
            if selected and entity_id == selected:
                self.water_observation.report(role, self._temperature(state), now)
        if entity_id == self.sensor:
            self.temperature_last_reported = getattr(state, "last_reported", None)
            temperature = self._temperature(state)
            self.curve_tracker.report_temperature(temperature, time.time())
            self._queue_curve_results()
            if temperature is None:
                self.heat_loss_observation.invalidate()
            completed_cycles = self.observation.completed_cycles
            self.observation.report_temperature(temperature, now)
            if self.observation.completed_cycles > completed_cycles:
                self.thermal_model.add_cycle(self.observation.last_cycle)
                self.learning_store.schedule(self.thermal_model.snapshot())
            heat_loss_rate = self.heat_loss_observation.report_temperature(
                temperature,
                self._optional_temperature(self.outdoor_sensor, now),
                now,
                supply_temperature=self._optional_temperature(self.supply_sensor, now),
                return_temperature=self._optional_temperature(self.return_sensor, now),
            )
            if heat_loss_rate is not None:
                self.thermal_model.add_heat_loss_rate(heat_loss_rate)
                self.learning_store.schedule(self.thermal_model.snapshot())
        else:
            if entity_id not in self._heater_states:
                self._notify()
                return
            was_pending = self.actuator.busy
            previous = self.actuator.observed
            requested = self.actuator.pending
            observed = self._switch_state(state)
            previous_member = self._heater_states[entity_id]
            self._heater_states[entity_id] = observed
            aggregate = self._aggregate_heater_state()
            external = self.actuator.observe(aggregate)
            if previous_member is not None and observed is not None and previous_member != observed:
                expected = self.actuator.pending
                external = external or expected is None or observed != expected
            temperature = self.controller.temperature
            transitioning = len(self.heaters) > 1 and aggregate is None and was_pending
            self.water_observation.heater(
                None if transitioning or (external and self.controller.mode != "off") else aggregate, now
            )
            if transitioning:
                self.heat_loss_observation.observe_heater(None, now)
                return
            if aggregate is not None:
                if aggregate is not previous:
                    start_reason = (
                        "PREDICTIVE_START" if self.decision.state == "PREDICTIVE_ON"
                        else "THRESHOLD_START" if requested is True else "UNKNOWN"
                    )
                    off_reason = (
                        "EXTERNAL_STOP" if external else
                        "PREDICTIVE_STOP" if self.decision.state == "PREDICTIVE_OFF" else
                        "TARGET_REACHED" if self.decision.state == "IDLE" else
                        "SAFETY_STOP" if self.decision.state == "FAULT" else
                        "MANUAL_STOP" if self.controller.mode == "off" else "UNKNOWN"
                    )
                    self.curve_tracker.switch(
                        aggregate, time.time(), start_reason=start_reason,
                        off_reason=off_reason, mode=self.prediction_mode.upper(),
                    )
                    if external:
                        self.curve_tracker.invalidate("EXTERNAL_OVERRIDE")
                    self._queue_curve_results()
                self.heat_loss_observation.observe_heater(aggregate, now)
                if external and self.controller.mode != "off":
                    self.observation.invalidate_cycle(aggregate)
                else:
                    self.observation.observe_heater(aggregate, now, temperature)
            else:
                self.curve_tracker.switch(None, time.time())
                self.heat_loss_observation.observe_heater(None, now)
                self._fault("heater_unavailable")
                if len(self.heaters) > 1:
                    self.actuator.request(False, retry=True, force=True)
            if aggregate is False:
                self.controller.startup_off_seen = True
                # A repeated OFF cannot confirm cancellation of an in-flight ON.
                # Related: actuator.py tracks actual observed transitions.
                if previous is True:
                    self._off_transition_pending = False
            if external and self.controller.mode != "off":
                self.controller.mode = "off"
                self._fault("external_override")
                if len(self.heaters) > 1:
                    self.actuator.request(False, retry=True, force=True)
            elif aggregate is True and self._off_transition_pending:
                self._fault("external_override")
                self.actuator.request(False, retry=True, force=True)
            if (previous is None and aggregate is not None and not was_pending
                    and self.controller.mode != "off"):
                self.actuator.request(False, retry=True)
        self.evaluate()

    def _optional_temperature(self, entity_id: str | None, now: float) -> float | None:
        """Read a selected learning sensor only after a recent valid HA report."""
        if entity_id is None:
            return None
        reported_at = self._optional_reported_at.get(entity_id)
        if (reported_at is None
                or now - reported_at >= self.controller.settings.sensor_timeout):
            return None
        return self._temperature(self.hass.states.get(entity_id))

    def _aggregate_heater_state(self) -> bool | None:
        """Return a state only when every room switch confirms the same value."""
        values = tuple(self._heater_states.values())
        if not values or any(value is None for value in values):
            return None
        return values[0] if all(value == values[0] for value in values) else None

    async def _send(self, heating: bool) -> None:
        """Send only switch actions; the actuator separately confirms HA state."""
        await self.hass.services.async_call(
            "switch", "turn_on" if heating else "turn_off",
            {"entity_id": self.heaters[0] if len(self.heaters) == 1 else list(self.heaters)},
            blocking=True,
        )

    def _fault(self, fault: str) -> None:
        if fault not in self.controller.faults:
            _LOGGER.warning("%s entered fault: %s", self.heater, fault)
        self.controller.faults.add(fault)
        self._queue_evaluate()

    def _queue_evaluate(self) -> None:
        """Coalesce actuator completions without recursively starting commands."""
        if self.started and not self._closed and self._queued is None:
            self._queued = self.hass.loop.call_soon(self._evaluate_queued)

    def _evaluate_queued(self) -> None:
        self._queued = None
        self.evaluate()

    def evaluate(self) -> None:
        """Perform an atomic decision and schedule the next relevant deadline."""
        if not self.started or self._closed:
            return
        now = self.hass.loop.time()
        if self.curve_store is not None and now >= self._curve_cleanup_at:
            self._curve_cleanup_at = now + 24 * 3600
            task = asyncio.create_task(self._cleanup_curves())
            self._curve_tasks.add(task)
            task.add_done_callback(self._curve_tasks.discard)
        self.curve_tracker.advance(time.time())
        self._queue_curve_results()
        self._read_current_temperature(now)
        decision = self.controller.decide(now, self.actuator.observed, self.actuator.changed_at)
        if self.controller.mode == "auto":
            if self.learning_model == "curve":
                curve_decision = self._apply_curve_prediction(decision, now)
                if curve_decision is None:
                    self.curve_fallback_reason = (
                        "CURVE_STORAGE_ERROR" if self._curve_storage_failed
                        else "INSUFFICIENT_CURVE_DATA"
                    )
                    decision = self._apply_learned_prediction(decision, now)
                else:
                    self.curve_fallback_reason = None
                    decision = curve_decision
            else:
                self.curve_fallback_reason = None
                decision = self._apply_learned_prediction(decision, now)
        if decision.state != self.decision.state:
            _LOGGER.info("%s control state: %s", self.heater, decision.state)
        self.decision = decision
        if self.controller.mode != "off":
            self.actuator.request(decision.heating)
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        # Re-read HA's current state before controller report aging could expire it.
        deadlines = [now + self.controller.settings.sensor_timeout]
        if self.curve_store is not None:
            deadlines.append(self._curve_cleanup_at)
        cycle = self.curve_tracker.cycle
        if cycle is not None:
            # The curve clock uses UTC seconds; the actuator clock stays monotonic.
            wall_now = time.time()
            curve_deadlines = [
                cycle.cooling_next_tick if cycle.peak_at is not None else cycle.next_tick
            ]
            if cycle.off_at is not None:
                curve_deadlines.append(cycle.off_at + MAX_AFTER_OFF_SECONDS)
                if cycle.peak_at is None and cycle.candidate_peak_at is not None:
                    curve_deadlines.append(cycle.candidate_peak_at + PEAK_SETTLE_SECONDS)
                if cycle.warming_since is not None:
                    curve_deadlines.append(cycle.warming_since + 600)
            deadlines.extend(
                now + max(0.001, value - wall_now)
                for value in curve_deadlines if value is not None and value > wall_now
            )
        if decision.deadline is not None:
            deadlines.append(decision.deadline)
        if (self.controller.mode != "off" and decision.state == "HEATING"
                and self.actuator.observed is True):
            minimum_on_deadline = self.actuator.changed_at + self.controller.settings.minimum_on_time
            if now < minimum_on_deadline:
                deadlines.append(minimum_on_deadline)
        if (self.controller.mode != "off" and self.actuator.observed is False
                and decision.state in ("IDLE", "WAIT_MIN_OFF", "HEATING")):
            minimum_off_deadline = self.actuator.changed_at + self.controller.settings.minimum_off_time
            if now < minimum_off_deadline:
                deadlines.append(minimum_off_deadline)
        if deadlines:
            self._timer = self.hass.loop.call_later(max(0.001, min(deadlines) - now), self.evaluate)
        self.store.schedule(self.snapshot())
        self._notify()

    def _queue_curve_results(self) -> None:
        """Persist completed curve segments without blocking HA state callbacks."""
        results = self.curve_tracker.take_results()
        if self.curve_store is None:
            return
        for result in results:
            task = asyncio.create_task(self._save_curve_result(result))
            self._curve_tasks.add(task)
            task.add_done_callback(self._curve_tasks.discard)

    async def _save_curve_result(self, result) -> None:
        try:
            await self.curve_store.save(result)
            self._notify()
        except Exception as err:
            self._curve_storage_failed = True
            _LOGGER.error("Curve segment could not be saved (%s)", type(err).__name__)
            self._queue_evaluate()

    async def _cleanup_curves(self) -> None:
        try:
            await self.curve_store.cleanup()
        except Exception as err:
            self._curve_storage_failed = True
            _LOGGER.error("Curve raw cleanup failed (%s)", type(err).__name__)
            self._queue_evaluate()

    def _apply_curve_prediction(self, decision: Decision, now: float) -> Decision | None:
        """Use a learned five-minute curve within existing timer and safety gates."""
        if self.curve_store is None or self._curve_storage_failed:
            return None
        model = self.curve_store.model
        temperature = self.controller.temperature
        if temperature is None or decision.state in ("FAULT", "OFF", "STARTUP"):
            return decision
        target = self.controller.target + self.controller.settings.hot_tolerance
        cycle = self.curve_tracker.cycle
        if self.actuator.observed is False:
            if decision.state == "HEATING":
                if cycle is not None and cycle.off_at is not None and cycle.peak_at is None:
                    rise = model.residual_rise(cycle.curve_type)
                    delay = model.peak_delay_minutes(cycle.curve_type)
                    if rise is not None and delay is not None and cycle.off_temperature is not None:
                        until_peak = cycle.off_at + delay * 60 - time.time()
                        if until_peak > 0 and cycle.off_temperature + rise >= target:
                            return Decision(False, "PREDICTIVE_WAIT", now + until_peak)
            if (self.prediction_mode != "eco" and decision.state in ("IDLE", "HEATING")
                    and now - self.actuator.changed_at >= self.controller.settings.minimum_off_time):
                delay = model.response_delay_minutes("PREDICTIVE_WARM_HEATING")
                slope = self.observation.temperature_slope
                if delay is None or slope is None:
                    return None
                fraction = 0.5 if self.prediction_mode == "balanced" else 1.0
                if slope < -RESPONSE_SLOPE_THRESHOLD and temperature + slope * delay * fraction / 60 <= self.controller.target:
                    return Decision(True, "PREDICTIVE_ON")
            return decision
        if decision.state != "HEATING" or self.actuator.observed is not True:
            return decision
        if now - self.actuator.changed_at < self.controller.settings.minimum_on_time:
            return decision
        if cycle is None:
            return None
        if not model.matches_active(cycle):
            return None
        residual = model.residual_rise(cycle.curve_type)
        if residual is None:
            return None
        return Decision(False, "PREDICTIVE_OFF") if temperature + residual >= target else decision

    def _read_current_temperature(self, now: float) -> None:
        """Read HA state for control without inventing history.py learning samples."""
        temperature = self._temperature(self.hass.states.get(self.sensor))
        self.controller.temperature = temperature
        self.controller.last_report = now if temperature is not None else None
        self.controller.sensor_fault = temperature is None
        self.controller.recovery_started = None
        if temperature is None:
            self.curve_tracker.report_temperature(None, time.time())
            self.observation.report_temperature(None, now)
            self.heat_loss_observation.invalidate()

    def _apply_learned_prediction(self, decision: Decision, now: float) -> Decision:
        """Apply learned early-start and curve-aware cutoff inside safety bounds."""
        temperature = self.controller.temperature
        if temperature is None:
            return decision

        # Aim residual cutoff/wait at the AUTO upper threshold; related: controller.py.
        residual_target = self.controller.target + self.controller.settings.hot_tolerance

        # All policies retain predictive stop; related: select.py and storage.py.
        if (self.prediction_mode != "eco" and self.actuator.observed is False
                and decision.state in ("IDLE", "HEATING")
                and now - self.actuator.changed_at >= self.controller.settings.minimum_off_time):
            response_delay = self.thermal_model.metrics["heating_response_delay"]["mean"]
            response_confidence = self.thermal_model.confidence("heating_response_delay")
            cooling_slope = self.observation.temperature_slope
            if (response_delay is not None and response_confidence > 0
                    and cooling_slope is not None
                    and cooling_slope < -RESPONSE_SLOPE_THRESHOLD):
                start_fraction = 0.5 if self.prediction_mode == "balanced" else 1.0
                effective_delay_hours = response_delay * response_confidence * start_fraction / 60
                predicted_at_response = temperature + cooling_slope * effective_delay_hours
                if predicted_at_response <= self.controller.target:
                    return Decision(True, "PREDICTIVE_ON")

        if decision.state != "HEATING":
            return decision
        if self.actuator.observed is False:
            # Bound residual waiting to this observed coast; related: history.py.
            off_at = self.observation.off_at
            off_temperature = self.observation.off_temperature
            peak_delay = self.thermal_model.metrics["peak_delay"]["mean"]
            estimate = self.thermal_model.metrics["residual_rise"]["mean"]
            confidence = self.thermal_model.confidence("residual_rise")
            if (off_at is not None and off_temperature is not None
                    and peak_delay is not None and now < off_at + peak_delay * 60
                    and estimate is not None and confidence > 0):
                predicted_peak = off_temperature + estimate * confidence
                if predicted_peak >= residual_target:
                    return Decision(False, "PREDICTIVE_WAIT", off_at + peak_delay * 60)
            return decision
        if (self.actuator.observed is not True
                or now - self.actuator.changed_at < self.controller.settings.minimum_on_time):
            return decision
        # Match the active ON phase against completed curves; related: history.py, thermal_model.py.
        profile = self.observation.heating_profile(now, self.controller.settings.sensor_timeout)
        curve_prediction = None
        if profile is not None:
            elapsed, rise, half_rise = profile
            curve_prediction = self.thermal_model.predict_residual_from_curve(
                elapsed, rise, half_rise, self.observation.temperature_slope,
                self.observation.temperature_curvature,
            )
        if curve_prediction is not None:
            estimate, confidence = curve_prediction
        else:
            estimate = self.thermal_model.metrics["residual_rise"]["mean"]
            confidence = self.thermal_model.confidence("residual_rise")
        if estimate is None or confidence <= 0:
            return decision
        predicted_peak = temperature + estimate * confidence
        if predicted_peak < residual_target:
            return decision
        return Decision(False, "PREDICTIVE_OFF")

    def snapshot(self) -> dict:
        return {
            "schema_version": 1, "target": self.controller.target,
            "mode": self.controller.mode, "faults": sorted(self.controller.faults),
            "preset": self.preset,
            "prediction_mode": self.prediction_mode,
            "learning_model": self.learning_model,
            "preset_temperature": (
                self.controller.settings.home_temperature if self.preset == "home"
                else self.controller.settings.away_temperature
            ),
        }

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def set_target(self, value: float) -> None:
        # Only a changed manual target invalidates this curve; related: climate.py, curve_learning.py.
        previous = self.controller.target
        self.controller.set_target(value)
        if self.controller.target != previous:
            self.curve_tracker.invalidate("MANUAL_TARGET_CHANGE")
        self.evaluate()

    def set_mode(self, mode: str) -> None:
        if self.controller.stopping:
            raise ValueError("The integration is stopping; retry after reload")
        previous_mode = self.controller.mode
        if mode != previous_mode:
            self.curve_tracker.invalidate("MANUAL_MODE_CHANGE")
        self._read_current_temperature(self.hass.loop.time())
        self.controller.set_mode(mode, self.hass.loop.time(), self.actuator.observed, self.actuator.changed_at)
        if mode == "off":
            self._off_transition_pending = (
                self._off_transition_pending or self.actuator.observed is True
                or (self.actuator.busy and self.actuator.desired)
            )
            self.actuator.request(False, retry=True)
        elif (((mode == "auto" and previous_mode != "auto") or previous_mode == "off")
                and self.actuator.observed is not False):
            self.controller.startup_off_seen = False
            self.observation.invalidate_cycle(self.actuator.observed)
            self.actuator.request(False, retry=True)
        if mode != "off":
            # Explicit control resumption ends cancellation-only OFF protection.
            self._off_transition_pending = False
        _LOGGER.info("%s requested HVAC mode: %s", self.heater, mode)
        self.evaluate()

    def set_preset(self, preset: str) -> None:
        """Select HOME/AWAY target using configured preset temperatures."""
        if preset not in ("home", "away"):
            raise ValueError("Preset must be home or away")
        if self.controller.stopping:
            raise ValueError("The integration is stopping; retry after reload")
        self.preset = preset
        self.curve_tracker.invalidate("MANUAL_PRESET_CHANGE")
        self.controller.set_target(
            self.controller.settings.home_temperature if preset == "home"
            else self.controller.settings.away_temperature
        )
        self.evaluate()

    def set_prediction_mode(self, mode: str) -> None:
        """Apply a selected predictive start strength; related: select.py, storage.py."""
        if mode not in PREDICTION_MODES:
            raise ValueError("Prediction mode must be eco, balanced or comfort")
        if self.controller.stopping:
            raise ValueError("The integration is stopping; retry after reload")
        self.prediction_mode = mode
        self.evaluate()

    def set_learning_model(self, model: str) -> None:
        """Persist the AUTO estimator choice; related: select.py, curve_learning.py."""
        if model not in LEARNING_MODELS:
            raise ValueError("Learning model must be existing or curve")
        if self.controller.stopping:
            raise ValueError("The integration is stopping; retry after reload")
        self.learning_model = model
        self.evaluate()

    async def async_stop(self) -> bool:
        """Keep fault monitoring alive if a normal unload cannot confirm OFF."""
        if not self.started or self._closed:
            return True
        self.controller.stopping = True
        self.actuator.request(False, retry=True)
        self.evaluate()
        stopped = await self.actuator.wait()
        if not stopped:
            self.controller.stopping = False
            self.evaluate()
            return False
        self.curve_tracker.close_incomplete(time.time(), "RELOAD_OR_SHUTDOWN")
        self._queue_curve_results()
        await self._persist()
        # State events may arrive while storage awaits disk I/O. Do not detach
        # monitoring if a new ON or pending OFF appeared during that await.
        if self.actuator.observed is not False or self.actuator.busy:
            self.controller.stopping = False
            self.evaluate()
            return False
        await self._close()
        return True

    async def _persist(self) -> None:
        """Flush intent and learned model independently without masking safe OFF."""
        for store, data, label in (
            (self.store, self.snapshot(), "thermostat"),
            (self.learning_store, self.thermal_model.snapshot(), "thermal model"),
        ):
            try:
                await store.save(data)
            except Exception as err:
                _LOGGER.error("Could not persist %s (%s)", label, type(err).__name__)
        if self._curve_tasks:
            await asyncio.gather(*tuple(self._curve_tasks), return_exceptions=True)

    async def _close(self) -> None:
        self._closed = True
        self.started = False
        for handle in (self._timer, self._queued):
            if handle is not None:
                handle.cancel()
        self._timer = self._queued = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.actuator.close()

    async def _shutdown(self, event) -> None:
        """Bound best-effort OFF during HA shutdown and stop all callbacks."""
        try:
            async with asyncio.timeout(15):
                await self.async_stop()
        except TimeoutError:
            _LOGGER.error("HA shutdown ended before heater OFF was confirmed")
        finally:
            try:
                self.curve_tracker.close_incomplete(time.time(), "HA_SHUTDOWN")
                self._queue_curve_results()
                await self._persist()
            finally:
                await self._close()
