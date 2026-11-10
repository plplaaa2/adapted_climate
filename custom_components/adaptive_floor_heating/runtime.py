"""Own events, timers, and safe lifecycle; related: climate.py, actuator.py, storage.py, thermal_model.py."""

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
import logging
from math import exp
import time

from .actuator import SwitchActuator
from .const import (
    CONF_HEATER, CONF_MODE, CONF_OUTDOOR_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_SENSOR, CONF_ZONES,
    MIN_PEAK_OBSERVATION_HOURS, MAX_PEAK_OBSERVATION_HOURS,
    MODE_MULTI_ZONE_INTEGRATED, DEFAULT_PREDICTION_MODE,
    HEATER_UNAVAILABLE_DELAY, PREDICTION_MODES, DEFAULT_OFF_MODEL, OFF_MODELS,
)
from .controller import Decision, Settings, ThermostatController, temperature_celsius
from .curve_learning import CurveTracker, BUCKET_SECONDS
from .curve_storage import CurveStore
from .history import ThermalObservation
from .storage import RuntimeStore, ThermalLearningStore
from .thermal_model import ThermalLearningModel
from .peak_prediction import HYBRID_VERSION, forecast as additive_forecast

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
        self._heater_unavailable_started: float | None = None
        # Extended after each confirmed ON→OFF edge: configured minimum is a
        # floor, while a long floor-heating run gets twice its runtime to coast.
        self._minimum_off_until: float | None = None
        self.sensor = entry.data[CONF_TEMPERATURE_SENSOR]
        self.temperature_last_reported = None
        options = entry.options
        self.outdoor_sensor = options.get(CONF_OUTDOOR_TEMPERATURE_SENSOR)
        # Subscribe only to supported outdoor input; ignore retired pipe options.
        # Related: config_flow.py, __init__.py, history.py.
        self._optional_sensors = {
            entity_id for entity_id in (
                self.outdoor_sensor,
            ) if entity_id
        }
        self._optional_reported_at: dict[str, float | None] = {
            entity_id: None for entity_id in self._optional_sensors
        }
        self.controller = ThermostatController(settings)
        self.peak_observation_seconds = settings.peak_observation_hours * 3600
        self.observation = ThermalObservation(
            max_report_gap=settings.sensor_timeout,
            max_peak_wait_seconds=self.peak_observation_seconds,
        )
        self.store = RuntimeStore(hass, entry.entry_id)
        self.learning_store = ThermalLearningStore(hass, entry.entry_id)
        self.thermal_model = ThermalLearningModel()
        self.preset = "home"
        self.prediction_mode = DEFAULT_PREDICTION_MODE
        self.off_model = DEFAULT_OFF_MODEL
        self._cycle_off_model: str | None = None
        self.start_prediction = {"status": "insufficient_data", "predicted_minimum": None}
        self.heating_prediction_info = {"model": None}
        # Curve observation is separate from the existing thermal model and actuator.
        # Related: curve_learning.py, curve_storage.py, select.py.
        self.curve_tracker = CurveTracker(self.peak_observation_seconds)
        self.curve_store = (
            CurveStore(hass, entry.entry_id, settings.peak_observation_hours * 60)
            if callable(getattr(getattr(hass, "config", None), "path", None)) else None
        )
        self._curve_tasks: set[asyncio.Task] = set()
        self._curve_cleanup_at = 0.0
        self.curve_fallback_reason: str | None = None
        self._curve_storage_failed = False
        self._off_predictions: dict = {}
        self._off_peak_deadlines: dict = {}
        self.last_peak_comparison: dict | None = None
        self.last_cooling_prediction: dict | None = None
        # Correct repeatable model under/over-prediction without allowing an unbounded shift.
        self.prediction_bias = {"curve": 0.0, "existing": 0.0}
        # Keep recent confirmed Peak errors as the empirical prediction-quality signal.
        # Related: storage.py, climate.py, frontend/panel.js.
        self.prediction_errors = {"curve": [], "existing": []}
        # Keep the new predictor's validation separate from legacy errors; related: storage.py, peak_prediction.py.
        self.additive_feedback = {"bias": 0.0, "errors": []}
        self.hybrid_feedback = {"bias": 0.0, "errors": []}
        self._off_peak_forecast = None
        self._decision_peak_forecast = None
        # Retain confirmed OFF diagnostics after control forecasts expire; related: sensor.py, storage.py.
        self._off_prediction_confidences: dict = {}
        self.last_off_prediction: dict | None = None
        self._off_diagnostic_at: float | None = None
        self.off_prediction: dict | None = None
        # The learned cutoff has one deadline for each active heating cycle.
        self._runtime_off_started_at: float | None = None
        self._runtime_off_planned_at: float | None = None
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
        self._learning_reset_lock = asyncio.Lock()
        self._learning_reset_pending = False
        self._learning_reset_in_progress = False
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
        self.observation = ThermalObservation(
            max_report_gap=self.controller.settings.sensor_timeout,
            max_peak_wait_seconds=self.peak_observation_seconds,
        )
        self.thermal_model = ThermalLearningModel()
        try:
            learned = await self.learning_store.load()
            if learned is not None:
                self.thermal_model = ThermalLearningModel.from_snapshot(learned)
        except Exception as err:
            _LOGGER.warning("Thermal model could not be restored (%s); restarting learning", type(err).__name__)
        self.controller.target, self.controller.mode = data["target"], data["mode"]
        self.preset = data["preset"]
        self.prediction_mode = data["prediction_mode"]
        self.off_model = data.get("off_model", DEFAULT_OFF_MODEL)
        self._cycle_off_model = None
        self.curve_tracker = CurveTracker(self.peak_observation_seconds)
        self.curve_tracker.preset = self.preset
        self.last_peak_comparison = data.get("last_peak_comparison")
        self.last_cooling_prediction = data.get("last_cooling_prediction")
        self.prediction_bias = data["prediction_bias"]
        self.additive_feedback = data.get("additive_feedback", {"bias": 0.0, "errors": []})
        self.hybrid_feedback = data.get("hybrid_feedback", {"bias": 0.0, "errors": []})
        self.prediction_errors = data["prediction_errors"]
        self.last_off_prediction = data.get("last_off_prediction")
        self._off_diagnostic_at = None
        self._off_prediction_confidences = {}
        self._off_predictions = {}
        self._off_peak_deadlines = {}
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
        self._learning_reset_pending = data.get("learning_reset_pending", False)
        if self._learning_reset_pending:
            try:
                await self._finish_learning_reset()
            except Exception as err:
                _LOGGER.error("Could not resume learning reset (%s)", type(err).__name__)
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
        if observed is False:
            # Resume the minimum-OFF clock from HA's restored switch timestamps.
            # Related: actuator.py, controller.py, tests/components/adaptive_floor_heating/test_runtime.py.
            off_timestamps = []
            for entity_id in self.heaters:
                state = self.hass.states.get(entity_id)
                changed = getattr(state, "last_changed", None)
                if self._switch_state(state) is not False or changed is None:
                    off_timestamps = []
                    break
                off_timestamps.append(changed.timestamp())
            if len(off_timestamps) == len(self.heaters):
                elapsed_off = max(
                    float(self.controller.settings.minimum_off_time),
                    time.time() - max(off_timestamps),
                    0.0,
                )
                self.actuator.changed_at = now - elapsed_off
        self.observation.seed_heater(observed)
        self.controller.startup_off_seen = observed is False
        if observed is None:
            self._mark_heater_unavailable(now)
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
            if entity_id == self.outdoor_sensor:
                wall_now = time.time()
                self.curve_tracker.report_cooling_outdoor(
                    wall_now, self._temperature(state), wall_now + self.controller.settings.sensor_timeout
                )
        if entity_id == self.sensor:
            self.temperature_last_reported = getattr(state, "last_reported", None)
            temperature = self._temperature(state)
            self.curve_tracker.report_temperature(temperature, time.time())
            self._queue_curve_results()
            if temperature is None:
                self._off_predictions = {}
                self._off_peak_deadlines = {}
                self._off_diagnostic_at = None
            completed_cycles = self.observation.completed_cycles
            observed_off_at = self.observation.off_at
            self.observation.report_temperature(temperature, now)
            if self.observation.completed_cycles > completed_cycles and not self._learning_reset_pending:
                actual_peak = self.observation.last_cycle.peak_temperature
                if actual_peak is not None:
                    off_at = self._off_diagnostic_at
                    if off_at is None and observed_off_at is not None:
                        off_at = time.time() - (now - observed_off_at)
                    peak_at = (off_at + self.observation.last_cycle.peak_delay_minutes * 60
                               if off_at is not None else None)
                    self._record_peak_comparison(actual_peak, off_at, peak_at)
                self.thermal_model.add_cycle(self.observation.last_cycle)
                self.learning_store.schedule(self.thermal_model.snapshot())
        else:
            if entity_id not in self._heater_states:
                self._notify()
                return
            was_pending = self.actuator.busy
            previous = self.actuator.observed
            on_started_at = self.actuator.changed_at if previous is True else None
            requested = self.actuator.pending
            observed = self._switch_state(state)
            previous_member = self._heater_states[entity_id]
            self._heater_states[entity_id] = observed
            aggregate = self._aggregate_heater_state()
            if aggregate is None and self._heater_unavailable_started is None:
                self._mark_heater_unavailable(now)
            elif aggregate is not None:
                self._heater_unavailable_started = None
                self.controller.faults.discard("heater_unavailable")
                # Recover a transient command fault only after the source switch reports
                # a known state again; related: actuator.py, controller.py.
                if previous_member is None:
                    self.controller.faults.discard("actuation_fault")
            external = self.actuator.observe(aggregate)
            if external:
                self._clear_runtime_off_schedule()
            if aggregate is False and previous_member is None:
                # A restored OFF state after HA reconnect starts with no cooldown.
                # Related: controller.py, tests/components/adaptive_floor_heating/test_runtime.py.
                self.actuator.changed_at = now - self.controller.settings.minimum_off_time
                self._minimum_off_until = None
            if previous_member is not None and observed is not None and previous_member != observed:
                expected = self.actuator.pending
                external = external or expected is None or observed != expected
            temperature = self.controller.temperature
            transitioning = len(self.heaters) > 1 and aggregate is None and was_pending
            if transitioning:
                return
            if aggregate is not None:
                if aggregate is not previous:
                    if aggregate is False and previous is True and on_started_at is not None:
                        run_seconds = max(0.0, now - on_started_at)
                        off_seconds = max(self.controller.settings.minimum_off_time, 2 * run_seconds)
                        self._minimum_off_until = now + off_seconds
                    elif aggregate is True:
                        self._minimum_off_until = None
                        self.heating_prediction_info = {"model": None}
                        self._cycle_off_model = self.off_model
                    # Capture both models before the OFF edge clears the active ON trace.
                    # Related: off_response.py; only the selected model dispatches commands.
                    if (aggregate is False and previous is True and not external
                            and not self._learning_reset_pending):
                        try:
                            self._off_predictions = self._predict_both_off(now)
                        except Exception as err:
                            self._off_predictions = {}
                            self._off_prediction_confidences = {}
                            self._off_peak_deadlines = {}
                            self._off_peak_forecast = None
                            _LOGGER.warning("OFF comparison unavailable (%s)", type(err).__name__)
                        self._off_diagnostic_at = time.time()
                        live_forecast = self.off_prediction or {}
                        self.last_off_prediction = {
                            "off_at": self._off_diagnostic_at, "predictions": dict(self._off_predictions),
                            "confidences": dict(self._off_prediction_confidences),
                            "comparison_counts": {
                                model: self._prediction_confidence(self._feedback_model(model, self._off_peak_forecast))[1]
                                for model in self._off_predictions
                            },
                            "selected_model": "curve",
                            "runtime_sample_count": live_forecast.get("accepted_cycles"),
                            "runtime_curve_range": live_forecast.get("runtime_curve_range"),
                            "runtime_extrapolated": live_forecast.get("runtime_extrapolated"),
                        }
                        if "curve" in self._off_predictions:
                            feedback_model = self._feedback_model("curve", self._off_peak_forecast)
                            self.last_off_prediction["off_model"] = "runtime" if feedback_model == "curve" else feedback_model
                        if self._off_peak_forecast is not None:
                            self.last_off_prediction["peak_forecast"] = dict(self._off_peak_forecast)
                            if (self.decision.state == "PREDICTIVE_OFF" and self._decision_peak_forecast is not None and self.curve_tracker.cycle is not None
                                    and self._decision_peak_forecast["cycle_id"] == self.curve_tracker.cycle.id):
                                self.last_off_prediction["decision_forecast"] = dict(self._decision_peak_forecast)
                                self._decision_peak_forecast = None
                    elif aggregate is True or external:
                        self._decision_peak_forecast = None
                        self._off_predictions = {}
                        self._off_diagnostic_at = None
                    start_reason = (
                        "PREDICTIVE_START" if self.decision.state == "PREDICTIVE_ON"
                        else "THRESHOLD_START" if requested is True and not external else "UNKNOWN"
                    )
                    off_reason = (
                        "EXTERNAL_STOP" if external else
                        "PREDICTIVE_STOP" if self.decision.state == "PREDICTIVE_OFF" else
                        "TARGET_REACHED" if self.decision.state == "IDLE" else
                        "SAFETY_STOP" if self.decision.state == "FAULT" else
                        "MANUAL_STOP" if self.controller.mode == "off" else "UNKNOWN"
                    )
                    wall_now = time.time()
                    self.curve_tracker.switch(
                        aggregate, wall_now, start_reason=start_reason,
                        off_reason=off_reason, mode=self.prediction_mode.upper(),
                        # Match both models with the same measured OFF conditions; related: history.py.
                        off_context=self.observation.off_context(now) if aggregate is False else None,
                        off_slope=self.observation.temperature_slope,
                        response_observed=self.observation.response_observed if aggregate is False else None,
                        target_peak_temperature=self.controller.target + (
                            self.controller.settings.heat_hot_tolerance if self.controller.mode == "heat"
                            else self.controller.settings.hot_tolerance),
                        outdoor_temperature=(self._optional_temperature(self.outdoor_sensor, now)
                                             if aggregate is True else None),
                    )
                    # Seed with the remaining validity of the last outdoor report; related: curve_learning.py.
                    if aggregate is False and self.outdoor_sensor is not None:
                        outdoor = self._optional_temperature(self.outdoor_sensor, now)
                        reported_at = self._optional_reported_at.get(self.outdoor_sensor)
                        if outdoor is not None and reported_at is not None:
                            self.curve_tracker.report_cooling_outdoor(
                                wall_now, outdoor,
                                wall_now + self.controller.settings.sensor_timeout - (now - reported_at),
                            )
                    if external:
                        self.curve_tracker.invalidate("EXTERNAL_OVERRIDE")
                    self._queue_curve_results()
                if external and self.controller.mode != "off":
                    self.observation.invalidate_cycle(aggregate)
                else:
                    self.observation.observe_heater(aggregate, now, temperature)
            else:
                self.curve_tracker.switch(None, time.time())
                if self._heater_unavailable_started is None:
                    self._mark_heater_unavailable(now)
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

    def _minimum_off_deadline(self) -> float:
        """Return the configured OFF deadline extended by the last ON runtime."""
        configured = self.actuator.changed_at + self.controller.settings.minimum_off_time
        return max(configured, self._minimum_off_until or configured)

    def _aggregate_heater_state(self) -> bool | None:
        """Return a state only when every room switch confirms the same value."""
        values = tuple(self._heater_states.values())
        if not values or any(value is None for value in values):
            return None
        return values[0] if all(value == values[0] for value in values) else None

    def _mark_heater_unavailable(self, now: float) -> None:
        """Latch unavailable only when the aggregate switch state stays unknown for 10s."""
        if self._heater_unavailable_started is None:
            self._heater_unavailable_started = now

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
        if self._heater_unavailable_started is not None:
            if self._aggregate_heater_state() is not None:
                self._heater_unavailable_started = None
                self.controller.faults.discard("heater_unavailable")
            elif now - self._heater_unavailable_started >= HEATER_UNAVAILABLE_DELAY:
                self._heater_unavailable_started = None
                self.controller.faults.add("heater_unavailable")
        if self.curve_store is not None and now >= self._curve_cleanup_at:
            self._curve_cleanup_at = now + 24 * 3600
            task = asyncio.create_task(self._cleanup_curves())
            self._curve_tasks.add(task)
            task.add_done_callback(self._curve_tasks.discard)
        self.curve_tracker.advance(time.time())
        self._queue_curve_results()
        self._read_current_temperature(now)
        decision = self.controller.decide(
            now, self.actuator.observed, self.actuator.changed_at,
            minimum_off_deadline=self._minimum_off_deadline(),
        )
        self.off_prediction = None
        # Clear stale start diagnostics when safety, heating or residual waiting takes priority.
        # Related: climate.py, frontend/panel.js.
        self.start_prediction = {"predicted_minimum": None, "status":
            "disabled" if self.prediction_mode == "eco" else
            "inactive" if self.controller.mode != "auto" else
            "heating" if self.actuator.observed is True else "blocked"}
        # The thermostat and safety decisions take priority over the runtime schedule.
        # Related: controller.py, _runtime_cutoff_decision.
        if decision.state != "HEATING" or self.actuator.observed is not True:
            self._clear_runtime_off_schedule()
        if self.controller.mode == "auto" and not self._learning_reset_pending:
            curve_decision = self._safe_model_prediction("curve", decision, now)
            if curve_decision is None:
                self._clear_runtime_off_schedule()
                if self._curve_storage_failed:
                    self.curve_fallback_reason = "CURVE_STORAGE_ERROR"
                elif (self.actuator.observed is True and decision.state == "HEATING"
                      and self.curve_store is not None
                      and self.active_off_model == "runtime"
                      and len(self.curve_store.model.runtime_samples) < 3):
                    self.curve_fallback_reason = "INSUFFICIENT_RUNTIME_SAMPLES"
                elif self.actuator.observed is True and decision.state == "HEATING":
                    self.curve_fallback_reason = "INSUFFICIENT_OFF_MODEL_DATA"
                else:
                    self.curve_fallback_reason = "INSUFFICIENT_CURVE_DATA"
                # ON prediction remains independent. An unsupported OFF model uses the thermostat decision.
                if self.actuator.observed is False:
                    decision = self._safe_model_prediction("existing", decision, now) or decision
            else:
                self.curve_fallback_reason = None
                decision = curve_decision
        # Apply remembered shortfalls to every predictive OFF path, including a committed cutoff.
        # Related: curve_learning.py, curve_storage.py.
        cycle = self.curve_tracker.cycle
        if (decision.state == "PREDICTIVE_OFF" and self.curve_store is not None
                and cycle is not None and cycle.baseline is not None):
            minimum = self.curve_store.model.shortfall_minimum(
                cycle.baseline, self.controller.target + self.controller.settings.hot_tolerance,
                cycle.outdoor_temperature, time.time())
            if now - self.actuator.changed_at < minimum * 60:
                self._decision_peak_forecast = None
                self.curve_fallback_reason = "PREVIOUS_HEATING_SHORTFALL"
                decision = Decision(True, "HEATING", self.actuator.changed_at + minimum * 60)
        if self._learning_reset_in_progress and decision.heating:
            decision = Decision(False, "IDLE")
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
        # Refresh held-state predictive slopes at five-minute boundaries without synthetic reports.
        # Related: history.py advance_time and TemperatureHistory.slope.
        if self.controller.mode == "auto" and self.controller.temperature is not None:
            deadlines.append((int(now // 300) + 1) * 300)
        if self._heater_unavailable_started is not None:
            deadlines.append(self._heater_unavailable_started + HEATER_UNAVAILABLE_DELAY)
        if self.curve_store is not None:
            deadlines.append(self._curve_cleanup_at)
        cycle = self.curve_tracker.cycle
        if cycle is not None:
            # The curve clock uses UTC seconds; the actuator clock stays monotonic.
            wall_now = time.time()
            curve_deadlines = [
                cycle.cooling_next_tick if cycle.peak_at is not None else cycle.next_tick
            ]
            if cycle.off_at is not None and cycle.peak_at is None:
                curve_deadlines.append(cycle.off_at + self.peak_observation_seconds)
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
            minimum_off_deadline = self._minimum_off_deadline()
            if now < minimum_off_deadline:
                deadlines.append(minimum_off_deadline)
        if deadlines:
            self._timer = self.hass.loop.call_later(max(0.001, min(deadlines) - now), self.evaluate)
        if not self._learning_reset_in_progress:
            self.store.schedule(self.snapshot())
        self._notify()

    def _queue_curve_results(self) -> None:
        """Persist curve results and retain peak comparisons if basic observation expired."""
        results = self.curve_tracker.take_results()
        if self._learning_reset_pending:
            return
        comparisons = {}
        for result in results:
            if (result.curve_type != "COOLING" and result.off_at is not None
                    and result.peak_at is not None and result.peak_temperature is not None):
                self._record_peak_comparison(
                    result.peak_temperature, result.off_at, result.peak_at,
                    completed_at=result.ended_at,
                )
                # Capture before async writes or a later round replaces the live comparison; related: curve_storage.py.
                comparison = self.last_peak_comparison
                if (comparison and comparison.get("off_at") is not None
                        and abs(comparison["off_at"] - result.off_at) <= 30):
                    comparisons[result.cycle_id, result.curve_type] = deepcopy(comparison)
        if self.curve_store is None:
            return
        for result in results:
            task = asyncio.create_task(self._save_curve_result(result, comparisons.get((result.cycle_id, result.curve_type))))
            self._curve_tasks.add(task)
            task.add_done_callback(self._curve_tasks.discard)

    # Compare against matching OFF metadata from live or restored state; related: storage.py, climate.py.
    def _record_peak_comparison(self, actual_peak: float, off_at: float | None,
                                peak_at: float | None, *, completed_at: float | None = None) -> None:
        """Pair a confirmed peak with its OFF forecast, including after a restart."""
        completed_at = time.time() if completed_at is None else completed_at
        off_prediction = self.last_off_prediction or {}
        prediction_off_at = off_prediction.get("off_at")
        matches_live = (off_at is not None and self._off_diagnostic_at is not None
                        and abs(off_at - self._off_diagnostic_at) <= 30)
        matches_saved = (off_at is not None and isinstance(prediction_off_at, (int, float))
                         and abs(off_at - prediction_off_at) <= 30)
        predictions = (dict(self._off_predictions) if matches_live else
                       dict(off_prediction.get("predictions", {})) if matches_saved else {})
        if (self.last_peak_comparison is not None and off_at is not None
                and self.last_peak_comparison.get("off_at") is not None
                and abs(self.last_peak_comparison["off_at"] - off_at) <= 30):
            return
        errors = {model: value - actual_peak for model, value in predictions.items()}
        off_confidences = off_prediction.get("confidences", {}) if matches_saved else {}
        off_counts = off_prediction.get("comparison_counts", {}) if matches_saved else {}
        comparison_confidences = {model: off_confidences.get(model) for model in predictions}
        comparison_counts = {model: off_counts.get(model, 0) for model in predictions}
        # A confirmed Peak closes the feedback loop: actual minus predicted is the correction
        # for the next comparable OFF forecast. Keep it bounded for sensor/model outliers.
        for model, error in errors.items():
            if model == "curve" and matches_saved and off_prediction.get("peak_forecast") is not None:
                feedback = (self.hybrid_feedback if self._feedback_model(model, off_prediction["peak_forecast"]) == "hybrid"
                            else self.additive_feedback)
                feedback["errors"] = (feedback["errors"]+[error])[-20:]
                # Increment by remaining corrected error, rather than shrinking the existing correction twice.
                feedback["bias"] = max(-.5, min(.5, feedback["bias"]-.2*error))
                continue
            if model in self.prediction_bias:
                self.prediction_errors[model].append(error)
                self.prediction_errors[model] = self.prediction_errors[model][-20:]
                correction = actual_peak - predictions[model]
                self.prediction_bias[model] = max(-0.5, min(
                    0.5, self.prediction_bias[model] * 0.8 + correction * 0.2
                ))
        self.last_peak_comparison = {
            "actual_peak": actual_peak, "predictions": predictions,
            "errors": errors, "confidences": comparison_confidences,
            "comparison_counts": comparison_counts,
            "off_at": off_at, "peak_at": peak_at,
            "completed_at": completed_at,
        }
        if matches_saved and off_prediction.get("off_model") in OFF_MODELS and "curve" in predictions:
            self.last_peak_comparison["off_model"] = off_prediction["off_model"]
        # Capture the model as it exists at confirmation, never recalculate on UI reads.
        # Related: curve_api.py, storage.py, climate.py and frontend/panel.js.
        if peak_at is not None:
            from .curve_api import cooling_prediction
            self.last_cooling_prediction = cooling_prediction(self, peak_at, actual_peak, completed_at)
        if matches_live or matches_saved:
            self._off_predictions = {}
            self._off_peak_deadlines = {}
            self._off_diagnostic_at = None
        self.store.schedule(self.snapshot())
        self._notify()

    # Persist frozen prediction evidence alongside its measured cycle; related: curve_storage.py.
    async def _save_curve_result(self, result, comparison=None) -> None:
        try:
            if comparison is None:
                await self.curve_store.save(result)
            else:
                await self.curve_store.save(result, comparison)
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

    def _safe_model_prediction(self, model: str, decision: Decision, now: float) -> Decision | None:
        """Keep predictor failures outside actuator safety and timers; related: controller.py."""
        try:
            return (self._apply_curve_prediction(decision, now) if model == "curve"
                    else self._apply_learned_prediction(decision, now))
        except Exception as err:
            self.off_prediction = None
            _LOGGER.error("%s prediction unavailable (%s); using fallback", model, type(err).__name__)
            return None

    @staticmethod
    def _feedback_model(model: str, forecast: dict | None) -> str:
        if model != "curve" or forecast is None:
            return model
        return "hybrid" if forecast.get("kind") == HYBRID_VERSION else "additive"

    def _prediction_confidence(self, model: str) -> tuple[float | None, int]:
        """Estimate validated confidence from recent confirmed Peak errors; related: storage.py."""
        feedback = {"additive": self.additive_feedback, "hybrid": self.hybrid_feedback}.get(model)
        errors = feedback["errors"] if feedback is not None else self.prediction_errors.get(model, [])
        count = len(errors)
        if not count:
            return None, 0
        mean_absolute_error = sum(abs(value) for value in errors) / count
        # Five comparisons fill the evidence window; errors around 0.5 C halve the score.
        return min(1.0, count / 5) * exp(-mean_absolute_error / 0.5), count

    @property
    def active_off_model(self) -> str:
        """Keep a model fixed between confirmed switch edges; related: select.py, climate.py."""
        if self.actuator.observed is True and self._cycle_off_model is not None:
            return self._cycle_off_model
        return self.off_model

    def _additive_peak(self, now: float, model: str) -> dict | None:
        """Use the same sum at decision and confirmed OFF; related: peak_prediction.py, curve_storage.py."""
        cycle = self.curve_tracker.cycle
        if self.curve_store is None or cycle is None or cycle.baseline is None or cycle.invalid_reason:
            return None
        return additive_forecast(
            self.curve_store.model.peak_samples, start=cycle.baseline, temperature=self.controller.temperature,
            minutes=max(0, (time.time()-cycle.started_at)/60), outdoor=cycle.outdoor_temperature,
            now=time.time(), bias=(self.hybrid_feedback if model == "hybrid" else self.additive_feedback)["bias"],
            duration_curve=model == "hybrid",
        )

    def _apply_curve_prediction(self, decision: Decision, now: float) -> Decision | None:
        """Use learned heater-runtime response for OFF; related: curve_learning.py."""
        if self.curve_store is None or self._curve_storage_failed:
            return None
        model = self.curve_store.model
        temperature = self.controller.temperature
        if temperature is None or decision.state in ("FAULT", "OFF", "STARTUP"):
            return decision
        target = self.controller.target + self.controller.settings.hot_tolerance
        cycle = self.curve_tracker.cycle
        if cycle is not None and cycle.invalid_reason == "EXTERNAL_OVERRIDE":
            return None
        if self.actuator.observed is False:
            learned_wait = self._wait_after_off(now, "curve")
            # Keep an OFF-edge forecast authoritative through the minimum-OFF
            # boundary: a low immediate reading must not restart floor heating
            # before its delayed residual heat has been observed.
            if learned_wait is not None and decision.state in ("IDLE", "WAIT_MIN_OFF", "HEATING"):
                return learned_wait
            if decision.state == "HEATING":
                if (cycle is not None and cycle.off_at is not None and cycle.peak_at is None
                        and not model.memory[cycle.curve_type].responses
                        and not model.off_profiles[cycle.curve_type]):
                    rise = model.residual_rise(cycle.curve_type)
                    delay = model.peak_delay_minutes(cycle.curve_type)
                    if rise is not None and delay is not None and cycle.off_temperature is not None:
                        until_peak = cycle.off_at + delay * 60 - time.time()
                        if until_peak > 0 and cycle.off_temperature + rise >= target:
                            return Decision(False, "PREDICTIVE_WAIT", now + until_peak)
            return self._predictive_start(decision, now)
        if decision.state != "HEATING" or self.actuator.observed is not True:
            return decision
        # Publish forecasts during minimum ON without allowing an early stop; related: diagnostics.py, panel.js.
        minimum_on = now - self.actuator.changed_at < self.controller.settings.minimum_on_time
        if cycle is None or cycle.baseline is None:
            return None
        off_model = self.active_off_model
        if off_model in ("additive", "hybrid"):
            summed = self._additive_peak(now, off_model)
            if summed is None or summed["duration_extrapolated"] or summed["nearby_samples"] < 3:
                return None
            confidence, count = self._prediction_confidence(off_model)
            self.heating_prediction_info = {"model": off_model}
            self.off_prediction = {"model": "curve", "off_model": off_model, "predicted_peak": summed["predicted_peak"],
                                   "confidence": confidence, "comparison_count": count,
                                   "accepted_cycles": summed["sample_count"], "peak_forecast": summed,
                                   "generated_at": time.time(), "temperature": temperature}
            if not minimum_on and summed["predicted_peak"] >= target:
                self._decision_peak_forecast = {"cycle_id": cycle.id, "generated_at": time.time(),
                                                "forecast": dict(summed)}
                return Decision(False, "PREDICTIVE_OFF")
            return decision
        # Prefer the learned runtime plan and publish available forecasts independently of minimum ON.
        # Related: peak_prediction.py, storage.py, panel.js.
        elapsed_minutes = max(0.0, (now - self.actuator.changed_at) / 60)
        required_minutes = model.required_heating_minutes(
            cycle.baseline, target, cycle.outdoor_temperature
        )
        predicted_peak = model.predicted_peak(
            cycle.baseline, elapsed_minutes, cycle.outdoor_temperature
        )
        # If a scheduled model loses support, return to thermostat control for this decision.
        # Related: peak_prediction.py, test_runtime.py.
        if self._runtime_off_planned_at is not None and (required_minutes is None or predicted_peak is None):
            self._clear_runtime_off_schedule()
            return decision
        if required_minutes is not None and predicted_peak is not None:
            # The synthetic zero-minute anchor is display math, not observed shutdown support.
            # Related: curve_learning.py runtime_control_minimum and _runtime_cutoff_decision.
            samples = model._runtime_samples_for(cycle.outdoor_temperature)
            measured_minimum = min(row[0] for row in samples)
            supported_minimum = model.runtime_control_minimum(cycle.outdoor_temperature)
            failure_minimum = model.shortfall_minimum(cycle.baseline, target, cycle.outdoor_temperature, time.time())
            minimum_minutes = max(supported_minimum or 0, failure_minimum)
            required_minutes = max(required_minutes, minimum_minutes)
            bias = self.prediction_bias["curve"]
            predicted_peak = max(temperature, predicted_peak + bias)
            learned_cycles = len(model.runtime_samples)
            runtime_points = model._runtime_curve(cycle.outdoor_temperature)
            runtime_curve_range = ({
                "minimum_heating_minutes": measured_minimum,
                "maximum_heating_minutes": runtime_points[-1][0],
                "minimum_peak_rise": runtime_points[0][1],
                "maximum_peak_rise": runtime_points[-1][1],
            } if runtime_points else None)
            # Validate the reserved duration, so a late callback does not invalidate an otherwise supported stop.
            # Related: _runtime_cutoff_decision and test_runtime.py.
            has_reservation = (self._runtime_off_started_at == self.actuator.changed_at
                               and self._runtime_off_planned_at is not None)
            scheduled_minutes = self.heating_prediction_info["planned_minutes"] if has_reservation else required_minutes
            elapsed_above_range = bool(runtime_points and elapsed_minutes > runtime_points[-1][0])
            runtime_outside_support = supported_minimum is None or bool(runtime_points and (
                (elapsed_above_range and not has_reservation)
                or scheduled_minutes > runtime_points[-1][0]
                or target - cycle.baseline < runtime_points[0][1]
                or target - cycle.baseline > runtime_points[-1][1]
            ))
            self.off_prediction = {
                "model": "curve", "off_model": "runtime", "predicted_peak": predicted_peak,
                "peak_minutes": elapsed_minutes,
                "required_heating_minutes": required_minutes,
                "elapsed_heating_minutes": elapsed_minutes,
                "rise_start_delay_minutes": model._runtime_response(cycle.outdoor_temperature)[1],
                "outdoor_bucket": model.outdoor_bucket(cycle.outdoor_temperature),
                "outdoor_conditioned": len(model.outdoor_runtime_samples.get(
                    model.outdoor_bucket(cycle.outdoor_temperature), []
                )) >= 3,
                "confidence": self._prediction_confidence("curve")[0],
                "comparison_count": self._prediction_confidence("curve")[1],
                "sample_coverage": min(1.0, learned_cycles / 5),
                "runtime_curve_range": runtime_curve_range,
                "runtime_extrapolated": runtime_outside_support or elapsed_above_range or elapsed_minutes < measured_minimum,
                "runtime_control_minimum": supported_minimum,
                "shortfall_minimum": failure_minimum,
                "memory_source": model.prediction_source, "current_weight": model.current_weight,
                # Anchor display-only OFF forecasts to their own sample and evidence; related: frontend/panel.js.
                "generated_at": time.time(), "temperature": temperature,
                "accepted_cycles": learned_cycles,
            }
            if minimum_on:
                return decision
            return self._runtime_cutoff_decision(
                decision, now, required_minutes, minimum_minutes, runtime_outside_support,
            )

        return decision if minimum_on else None

    def _clear_runtime_off_schedule(self, *, completed: bool = False) -> None:
        """Discard a pending runtime cutoff after an external control change."""
        if self._runtime_off_planned_at is not None and not completed:
            self.heating_prediction_info = {"model": None}
        self._runtime_off_started_at = None
        self._runtime_off_planned_at = None

    # Reserve one stop per run and let the next cycle's learning adjust future plans.
    # Related: curve_learning.py, controller.py, frontend/panel.js.
    def _runtime_cutoff_decision(
        self, decision: Decision, now: float, required_minutes: float,
        minimum_minutes: float, runtime_extrapolated: bool,
    ) -> Decision:
        """Stop at the reserved time while its learned support remains valid."""
        if (self._runtime_off_started_at is not None
                and self._runtime_off_started_at != self.actuator.changed_at):
            self._clear_runtime_off_schedule()
        if runtime_extrapolated or (self._runtime_off_planned_at is not None
                and self._runtime_off_planned_at < self.actuator.changed_at + minimum_minutes * 60):
            self._clear_runtime_off_schedule()
            return decision

        if self._runtime_off_planned_at is None:
            self._runtime_off_started_at = self.actuator.changed_at
            self._runtime_off_planned_at = self.actuator.changed_at + required_minutes * 60
            self.heating_prediction_info = {
                "model": "runtime", "planned_minutes": required_minutes,
                "planned_off_at": time.time() + self._runtime_off_planned_at - now,
            }
        planned_off_at = self._runtime_off_planned_at
        if now < planned_off_at:
            return Decision(True, "HEATING", planned_off_at)
        self._clear_runtime_off_schedule(completed=True)
        return Decision(False, "PREDICTIVE_OFF")

    def _predict_both_off(self, now: float) -> dict:
        """Capture the learned runtime-curve peak at confirmed OFF; related: storage.py."""
        # Capture confidence alongside forecasts without changing their control contract.
        # Related: diagnostics.py; reset even when the current ON trace is unusable.
        self._off_prediction_confidences = {}
        self._off_peak_deadlines = {}
        self._off_peak_forecast = None
        temperature = self.controller.temperature
        if temperature is None:
            return {}
        predictions = {}
        cycle = self.curve_tracker.cycle
        # Freeze the model actually published for this stop, rather than replacing a runtime forecast.
        # Related: curve_storage.py, panel.js, test_runtime.py.
        live = self.off_prediction or {}
        summed = live.get("peak_forecast")
        off_model = self._cycle_off_model or self.off_model
        if (summed is None and self.controller.mode == "auto" and off_model in ("additive", "hybrid")):
            candidate = self._additive_peak(now, off_model)
            if candidate and not candidate["duration_extrapolated"] and candidate["nearby_samples"] >= 3:
                summed = candidate
        if summed is not None:
            self._off_peak_forecast = summed
            predictions["curve"] = summed["predicted_peak"]
            self._off_prediction_confidences["curve"] = self._prediction_confidence(self._feedback_model("curve", summed))[0]
            delay = self.curve_store.model.peak_delay_minutes(cycle.curve_type)
            if delay is not None:
                self._off_peak_deadlines["curve"] = now+delay*60
        if ("curve" not in predictions and live.get("model") == "curve"
                and live.get("predicted_peak") is not None):
            predictions["curve"] = live["predicted_peak"]
            self._off_prediction_confidences["curve"] = live.get("confidence")
            delay = self.curve_store.model.peak_delay_minutes(cycle.curve_type) if self.curve_store and cycle else None
            if delay is not None:
                self._off_peak_deadlines["curve"] = now + delay * 60
        if ("curve" not in predictions and off_model == "runtime"
                and self.curve_store is not None and cycle is not None
                and cycle.baseline is not None):
            duration = max(0.0, (time.time() - cycle.started_at) / 60)
            peak = self.curve_store.model.predicted_peak(
                cycle.baseline, duration, cycle.outdoor_temperature
            )
            if peak is not None:
                predictions["curve"] = max(temperature, peak + self.prediction_bias["curve"])
                self._off_prediction_confidences["curve"] = self._prediction_confidence("curve")[0]
                # Preserve learned peak timing for the existing predictive-start wait.
                delay = self.curve_store.model.peak_delay_minutes(cycle.curve_type)
                if delay is not None:
                    self._off_peak_deadlines["curve"] = now + delay * 60

        profile = self.observation.heating_profile(now, self.controller.settings.sensor_timeout)
        if profile is not None:
            existing = self.thermal_model.predict_off_response(
                profile[0], self.observation.temperature_slope,
                context=self.observation.off_context(now),
                max_peak_wait_minutes=self.controller.settings.peak_observation_hours * 60,
            )
            if existing is not None:
                predictions["existing"] = temperature + existing.rise + self.prediction_bias["existing"]
                self._off_prediction_confidences["existing"] = self._prediction_confidence("existing")[0]
                self._off_peak_deadlines["existing"] = now + existing.peak_minutes * 60
        return predictions

    def _wait_after_off(self, now: float, model: str) -> Decision | None:
        """Reuse the OFF-baseline prediction without adding already observed rise twice."""
        if self.observation.off_at is None:
            return None
        deadline = self._off_peak_deadlines.get(model)
        peak = self._off_predictions.get(model)
        # Release a forecast when measured cooling exceeds a plausible small initial dip.
        # Related: history.py sustained Peak confirmation; sensor/actuator safety still takes precedence.
        baseline, temperature = self.observation.off_temperature, self.controller.temperature
        if baseline is not None and temperature is not None and temperature < baseline - 0.5:
            return None
        target = self.controller.target + self.controller.settings.hot_tolerance
        if deadline is not None and peak is not None and now < deadline and peak >= target:
            return Decision(False, "PREDICTIVE_WAIT", deadline)
        return None

    def _read_current_temperature(self, now: float) -> None:
        """Read HA state for control without inventing history.py learning samples."""
        temperature = self._temperature(self.hass.states.get(self.sensor))
        self.controller.temperature = temperature
        self.controller.last_report = now if temperature is not None else None
        self.controller.sensor_fault = temperature is None
        self.controller.recovery_started = None
        # Current HA validity gates time advancement; related: history.py predictive held-state slope.
        if temperature is not None:
            self.observation.advance_time(now)
        if temperature is None:
            self.curve_tracker.report_temperature(None, time.time())
            self.observation.report_temperature(None, now)

    def _apply_learned_prediction(self, decision: Decision, now: float) -> Decision:
        """Apply learned early-start and curve-aware cutoff inside safety bounds."""
        temperature = self.controller.temperature
        if temperature is None:
            return decision

        # Aim residual cutoff/wait at the AUTO upper threshold; related: controller.py.
        residual_target = self.controller.target + self.controller.settings.hot_tolerance
        # A pending delayed response takes priority over predictive ON; related: off_response.py.
        if self.actuator.observed is False and decision.state in ("IDLE", "HEATING"):
            learned_wait = self._wait_after_off(now, "existing")
            if learned_wait is not None:
                return learned_wait

        # Both control paths share full-delay cooling evidence; related: curve_learning.py, switch.py.
        if self.actuator.observed is False:
            predicted = self._predictive_start(decision, now)
            if predicted.state == "PREDICTIVE_ON":
                return predicted

        if decision.state != "HEATING":
            return decision
        if self.actuator.observed is False:
            learned_wait = self._wait_after_off(now, "existing")
            if learned_wait is not None:
                return learned_wait
            # Bound residual waiting to this observed coast; related: history.py.
            off_at = self.observation.off_at
            off_temperature = self.observation.off_temperature
            peak_delay = self.thermal_model.metrics["peak_delay"]["mean"]
            estimate = self.thermal_model.metrics["residual_rise"]["mean"]
            confidence = self.thermal_model.confidence("residual_rise")
            if (off_at is not None and off_temperature is not None
                    and peak_delay is not None and now < off_at + peak_delay * 60
                    and estimate is not None and confidence >= 0.25
                    and not self.thermal_model.off_response_profiles):
                # Confidence gates use; it must not shrink the physical residual rise.
                # Related: off_response.py and the delayed-response restart guard.
                predicted_peak = off_temperature + estimate
                if predicted_peak >= residual_target:
                    return Decision(False, "PREDICTIVE_WAIT", off_at + peak_delay * 60)
            return decision
        if (self.actuator.observed is not True
                or now - self.actuator.changed_at < self.controller.settings.minimum_on_time):
            return decision
        # Predict OFF response from slope and learned equivalent time; related: off_response.py.
        profile = self.observation.heating_profile(now, self.controller.settings.sensor_timeout)
        if profile is None:
            return decision
        prediction = self.thermal_model.predict_off_response(
            profile[0], self.observation.temperature_slope,
            context=self.observation.off_context(now),
            max_peak_wait_minutes=self.controller.settings.peak_observation_hours * 60,
        )
        if prediction is None:
            return decision
        predicted_peak = temperature + prediction.rise + self.prediction_bias["existing"]
        self.off_prediction = {
            "model": "existing", "predicted_peak": predicted_peak,
            "peak_minutes": prediction.peak_minutes,
            "confidence": self._prediction_confidence("existing")[0],
            "comparison_count": self._prediction_confidence("existing")[1],
            "model_confidence": prediction.confidence,
            # Preserve the actual predictor's trajectory, never synthesize a display curve.
            # Related: off_response.py and frontend/panel.js forecast display gating.
            "trajectory": list(prediction.points), "generated_at": time.time(),
            "temperature": temperature, "accepted_cycles": self.thermal_model.accepted_cycles,
        }
        if predicted_peak < residual_target:
            return decision
        return Decision(False, "PREDICTIVE_OFF")

    def _predictive_start(self, decision: Decision, now: float) -> Decision:
        """Use continuous cooling evidence over the full response delay; related: climate.py, switch.py."""
        threshold = self.controller.target - self.controller.settings.cold_tolerance
        info = {"status": "insufficient_data", "predicted_minimum": None, "threshold": threshold}
        self.start_prediction = info
        if self.prediction_mode == "eco":
            info["status"] = "disabled"
            return decision
        if decision.state not in ("IDLE", "HEATING") or now < self._minimum_off_deadline():
            info["status"] = "blocked"
            return decision
        cycle = self.curve_tracker.cycle
        # Cooling collection may already have ended at target; retain its measured Peak anchor.
        peak_at = cycle.peak_at if cycle is not None else None
        if peak_at is None:
            peak_at = (self.last_peak_comparison or {}).get("peak_at")
        if (self.curve_store is None or self._curve_storage_failed
                or peak_at is None or self.controller.temperature is None):
            return decision
        model = self.curve_store.model
        delay = model.response_delay_minutes("PREDICTIVE_WARM_HEATING")
        if delay is None:
            return decision
        # Use a single curve across the horizon, with global fallback; related: curve_learning.py, panel.js.
        elapsed = max(0, (time.time() - peak_at) / 60)
        outdoor_group = model.outdoor_bucket(self._optional_temperature(self.outdoor_sensor, now))
        delta = (model.predict_cooling_delta(elapsed, delay, outdoor_group=outdoor_group)
                 if outdoor_group is not None else None)
        source = "outdoor" if delta is not None else "global"
        if delta is None:
            delta = model.predict_cooling_delta(elapsed, delay)
        if delta is None:
            return decision
        minimum = self.controller.temperature + delta
        info.update(predicted_minimum=minimum, response_delay_minutes=delay,
                    cooling_delta=delta, cooling_source=source, outdoor_group=outdoor_group,
                    status="start" if minimum <= threshold else "waiting")
        return Decision(True, "PREDICTIVE_ON") if minimum <= threshold else decision

    def snapshot(self) -> dict:
        return {
            "schema_version": 1, "target": self.controller.target,
            "mode": self.controller.mode, "faults": sorted(self.controller.faults),
            "preset": self.preset,
            "prediction_mode": self.prediction_mode,
            "off_model": self.off_model,
            "last_peak_comparison": self.last_peak_comparison,
            "last_cooling_prediction": self.last_cooling_prediction,
            "prediction_bias": dict(self.prediction_bias),
            "additive_feedback": {"bias": self.additive_feedback["bias"], "errors": list(self.additive_feedback["errors"])},
            "hybrid_feedback": {"bias": self.hybrid_feedback["bias"], "errors": list(self.hybrid_feedback["errors"])},
            "prediction_errors": {model: list(values) for model, values in self.prediction_errors.items()},
            "last_off_prediction": self.last_off_prediction,
            "learning_reset_pending": self._learning_reset_pending,
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

    # Serialize a per-entry learning reset with lifecycle persistence; related: button.py, storage.py, curve_storage.py.
    @property
    def can_reset_learning(self) -> bool:
        return (self.started and not self._closed and not self.controller.stopping
                and not self._learning_reset_in_progress and self.actuator.observed is False
                and self.actuator.pending is None and not self.actuator.busy)

    async def async_reset_learning(self) -> None:
        if self._learning_reset_in_progress:
            raise ValueError("A learning reset is already running")
        async with self._learning_reset_lock:
            if not self.can_reset_learning:
                raise ValueError("Wait for confirmed heater OFF and for pending commands to finish")
            if self._curve_storage_failed and self.curve_store is None:
                raise OSError("Curve storage is unavailable")
            self._learning_reset_in_progress = True
            self._learning_reset_pending = True
            self._notify()
            try:
                await self.store.save_reset_state(self.snapshot())
                await self._finish_learning_reset()
                _LOGGER.info("%s learning reset completed", self.heater)
            except Exception as err:
                _LOGGER.error("%s learning reset could not complete (%s)", self.heater, type(err).__name__)
                raise
            finally:
                self._learning_reset_in_progress = False
                self.evaluate()

    async def _finish_learning_reset(self) -> None:
        """Resume idempotently after an interrupted reset; related: storage.py."""
        if self._curve_storage_failed and self.curve_store is None:
            raise OSError("Curve storage is unavailable")
        if self._curve_tasks:
            await asyncio.gather(*tuple(self._curve_tasks), return_exceptions=True)
        if self.curve_store is not None:
            await self.curve_store.reset()
        await self.learning_store.remove()

        self.thermal_model = ThermalLearningModel()
        self.observation = ThermalObservation(max_report_gap=self.controller.settings.sensor_timeout,
                                              max_peak_wait_seconds=self.peak_observation_seconds)
        self.observation.seed_heater(self.actuator.observed)
        self.curve_tracker = CurveTracker(self.peak_observation_seconds)
        self.curve_tracker.preset = self.preset
        self.curve_tracker.seed(self.actuator.observed, self.controller.temperature, time.time())
        self._curve_storage_failed = False
        self.curve_fallback_reason = None
        self.prediction_bias = {"curve": 0.0, "existing": 0.0}
        self.prediction_errors = {"curve": [], "existing": []}
        self.additive_feedback = {"bias": 0.0, "errors": []}
        self.hybrid_feedback = {"bias": 0.0, "errors": []}
        self.last_peak_comparison = self.last_cooling_prediction = self.last_off_prediction = None
        self._off_predictions.clear()
        self._off_peak_deadlines.clear()
        self._off_prediction_confidences.clear()
        self._off_diagnostic_at = self._off_peak_forecast = self._decision_peak_forecast = None
        self.off_prediction = None
        self._clear_runtime_off_schedule()
        self.heating_prediction_info = {"model": None}

        await self.store.save_reset_state({**self.snapshot(), "learning_reset_pending": False})
        self._learning_reset_pending = False

    def set_target(self, value: float) -> None:
        # Preserve measured coast and OFF comparison across target edits; related: climate.py, curve_learning.py, history.py.
        previous = self.controller.target
        self.controller.set_target(value)
        if self.controller.target != previous:
            if self.actuator.observed is not False:
                self.curve_tracker.invalidate("MANUAL_TARGET_CHANGE")
                self.observation.invalidate_cycle(self.actuator.observed)
                self._off_predictions = {}
            self._off_peak_deadlines.clear()
            self._clear_runtime_off_schedule()
        self.evaluate()

    def set_mode(self, mode: str) -> None:
        if self.controller.stopping:
            raise ValueError("The integration is stopping; retry after reload")
        previous_mode = self.controller.mode
        if mode != previous_mode:
            if self.actuator.observed is not False:
                self.curve_tracker.invalidate("MANUAL_MODE_CHANGE")
            self._off_predictions = {}
            self._clear_runtime_off_schedule()
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
        if preset != self.preset:
            self.curve_tracker.set_preset(preset, time.time())
            self.observation.invalidate_cycle(self.actuator.observed)
            self._off_predictions = {}
        previous_target = self.controller.target
        self.preset = preset
        self.controller.set_target(
            self.controller.settings.home_temperature if preset == "home"
            else self.controller.settings.away_temperature
        )
        if self.controller.target != previous_target:
            # Recalculate the reservation for the new preset target; related: _runtime_cutoff_decision.
            self._clear_runtime_off_schedule()
        self.evaluate()

    def set_prediction_mode(self, mode: str) -> None:
        """Apply a selected predictive start strength; related: select.py, storage.py."""
        if mode not in PREDICTION_MODES:
            raise ValueError("Prediction mode must be eco, balanced or comfort")
        if self.controller.stopping:
            raise ValueError("The integration is stopping; retry after reload")
        self.prediction_mode = mode
        self.evaluate()

    def set_off_model(self, model: str) -> None:
        """Save the next cycle's OFF model without interrupting an active run; related: select.py, storage.py."""
        if model not in OFF_MODELS:
            raise ValueError("Select runtime, additive, or hybrid OFF model")
        if self.controller.stopping:
            raise ValueError("The integration is stopping; retry after reload")
        if self.actuator.observed is True and self._cycle_off_model is None:
            self._cycle_off_model = self.off_model
        self.off_model = model
        self.evaluate()

    def set_peak_observation_hours(self, hours: int) -> None:
        """Update the peak observation horizon without discarding the active cycle."""
        if (isinstance(hours, bool) or not isinstance(hours, int)
                or not MIN_PEAK_OBSERVATION_HOURS <= hours <= MAX_PEAK_OBSERVATION_HOURS):
            raise ValueError("Peak observation must be between 1 and 12 hours")
        if self.controller.stopping:
            raise ValueError("The integration is stopping; retry after reload")
        if self.controller.settings.peak_observation_hours == hours:
            return
        self.controller.settings = replace(
            self.controller.settings, peak_observation_hours=hours
        )
        self.peak_observation_seconds = hours * 3600
        self.observation.max_peak_wait_seconds = self.peak_observation_seconds
        self.curve_tracker.max_peak_wait_seconds = self.peak_observation_seconds
        if self.curve_store is not None:
            self.curve_store.max_peak_wait_minutes = hours * 60
            self.curve_store.model.max_peak_wait_minutes = hours * 60
        self.evaluate()
        self._notify()

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
        async with self._learning_reset_lock:
            stores = [(self.store, self.snapshot(), "thermostat")]
            if not self._learning_reset_pending:
                stores.append((self.learning_store, self.thermal_model.snapshot(), "thermal model"))
            for store, data, label in stores:
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
