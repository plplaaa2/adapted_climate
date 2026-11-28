"""Own events, timers, and safe lifecycle; related: climate.py, actuator.py, storage.py, thermal_model.py."""

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time as ClockTime, timedelta
from functools import partial
import logging
from math import exp, isfinite
import time
from zoneinfo import ZoneInfo

from .actuator import SwitchActuator
from .const import (
    CONF_CLIMATE_HIGH, CONF_CLIMATE_LOW, CONF_HEATER, CONF_MODE, CONF_OUTDOOR_TEMPERATURE_SENSOR,
    CONF_SUNRISE_WEATHER_ENTITY, CONF_TEMPERATURE_SENSOR, CONF_ZONES,
    MIN_PEAK_OBSERVATION_HOURS, MAX_PEAK_OBSERVATION_HOURS,
    MODE_MULTI_ZONE_INTEGRATED, DEFAULT_PREDICTION_MODE, DOMAIN,
    HEATER_RECOVERY_OVERRIDE_GRACE, HEATER_UNAVAILABLE_DELAY, PREDICTION_MODES, OFF_MODELS,
)
from .controller import Decision, Settings, ThermostatController, temperature_celsius
from .curve_learning import (
    BUCKET_SECONDS, MIN_RUNTIME_SAMPLES, SOLAR_START_DELAY_SECONDS,
    CurveCycle, CurveStandards, CurveTracker, SolarGainObservation,
)
from .curve_storage import CYCLE_RETENTION_DAYS, FORECAST_HISTORY_BACKFILL, HEATER_HISTORY_BACKFILL, CurveStore
from .history import ThermalObservation
from .storage import RuntimeStore, ThermalLearningStore
from .thermal_model import ThermalLearningModel
from .peak_prediction import HYBRID_VERSION, WEIGHTED_VERSION, heating_outdoor_bucket, weighted_residual

_LOGGER = logging.getLogger(__name__)
MIN_PREDICTIVE_ACTIVATION_SAMPLES = 10


@dataclass(frozen=True)
class PredictiveStop:
    """Keep the original OFF decision through delayed actuator confirmation."""

    cycle_id: str
    target: float
    forecast: dict
    sunrise_saving: bool
    sunrise_response_minutes: float | None = None


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
        # Climate setpoints are stored in °C. See config_flow.py and actuator.py.
        self.climate_targets = (
            {False: entry.options[CONF_CLIMATE_LOW], True: entry.options[CONF_CLIMATE_HIGH]}
            if self.heater.startswith("climate.") else None
        )
        self._heater_states: dict[str, bool | None] = {
            entity_id: None for entity_id in self.heaters
        }
        self._heater_unavailable_started: float | None = None
        # Hold learning reports until heater continuity is verified; related: curve_learning.py, history.py.
        self._learning_outage_deadline: float | None = None
        self._learning_outage_reports: list[Callable[[], None]] = []
        self._confirmed_on_since: float | None = None
        self._maximum_on_deadline: float | None = None
        self._runtime_limit_stop_pending = False
        # Extended after each confirmed ON→OFF edge: configured minimum is a
        # floor, while a long floor-heating run gets twice its runtime to coast.
        self._minimum_off_until: float | None = None
        self._reload_off_until = 0.0
        self.sensor = entry.data[CONF_TEMPERATURE_SENSOR]
        self.temperature_last_reported = None
        options = entry.options
        self.outdoor_sensor = options.get(CONF_OUTDOOR_TEMPERATURE_SENSOR)
        self.sunrise_weather_entity = options.get(CONF_SUNRISE_WEATHER_ENTITY)
        self._solar_observation: SolarGainObservation | None = None
        self._solar_sunrise: float | None = None
        self._solar_finished: float | None = None
        # Share five-minute snapshots across control and learning; related: controller.py, history.py.
        self._optional_sensors = {
            entity_id for entity_id in (
                self.outdoor_sensor,
            ) if entity_id
        }
        self._sensor_samples: dict[str, object | None] = {}
        self._sensor_misses: dict[str, int] = {}
        self._next_sensor_check = 0.0
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
        self.sunrise_enabled = False
        self.sunrise_start = self.sunrise_end = ClockTime()
        self._sunrise_active = False
        self._sunrise_daylight = False
        self._sunrise_diagnostic = None
        self._predictive_stop: PredictiveStop | None = None
        self._sunrise_coast = False
        self.start_prediction = {"status": "insufficient_data", "predicted_minimum": None}
        self.heating_prediction_info = {"model": None}
        # Curve observation is separate from the existing thermal model and actuator.
        # Related: curve_learning.py, curve_storage.py, select.py.
        self.curve_tracker = CurveTracker(
            self.peak_observation_seconds, minimum_heating_seconds=self.controller.settings.minimum_on_time,
        )
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
        # Retain confirmed OFF diagnostics after control forecasts expire; related: sensor.py, storage.py.
        self._off_prediction_confidences: dict = {}
        self.last_off_prediction: dict | None = None
        self._off_diagnostic_at: float | None = None
        self.off_prediction: dict | None = None
        # The learned cutoff has one deadline for each active heating cycle.
        self._runtime_off_started_at: float | None = None
        self._runtime_off_planned_at: float | None = None
        self.runtime_off_plan: dict | None = None
        self._off_transition_pending = False
        self._heater_recovery_deadlines: dict[str, float | None] = {}
        self._recovery_overrides: dict[str, tuple[bool, bool]] = {}
        self.actuator = SwitchActuator(
            self._send, self._queue_evaluate, self._fault, hass.loop.time,
            confirmed=self._climate_confirmed if self.climate_targets is not None else None,
            confirmation_delay=settings.climate_command_wait if self.climate_targets is not None else 0,
            command_confirmed=self._climate_command_confirmed if self.climate_targets is not None else None,
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

    async def async_start(self, *, clear_idle_reload_wait: bool = True) -> None:
        """Serialize entity-triggered starts so shared state initializes once."""
        async with self._start_lock:
            await self._async_start_locked(clear_idle_reload_wait=clear_idle_reload_wait)

    async def _async_start_locked(self, *, clear_idle_reload_wait: bool) -> None:
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
        self.sunrise_enabled = data["sunrise_enabled"]
        self.sunrise_start = ClockTime.fromisoformat(data["sunrise_start"])
        self.sunrise_end = ClockTime.fromisoformat(data["sunrise_end"])
        self._predictive_stop = None
        self._sunrise_coast = False
        self.curve_tracker = CurveTracker(
            self.peak_observation_seconds, minimum_heating_seconds=self.controller.settings.minimum_on_time,
        )
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
                self._restore_control_feedback()
            except Exception as err:
                _LOGGER.error("Curve storage could not be opened (%s)", type(err).__name__)
                self._curve_storage_failed = True
                self.curve_store = None
        self.controller.faults = set(data["faults"])
        self.controller.auto_external_override = data.get("auto_external_override", False)
        self._heater_recovery_deadlines.clear()
        self._recovery_overrides.clear()
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
        self._sensor_samples.clear()
        self._sensor_misses.clear()
        self._next_sensor_check = now
        self._read_current_temperature(now, learn=False)

        @callback
        def event_filter(data):
            if data.get("entity_id") == "sun.sun":
                return getattr(data.get("new_state"), "state", None) != getattr(data.get("old_state"), "state", None)
            return data.get("entity_id") in self.heaters

        @callback
        def event_received(event):
            if event.data.get("entity_id") == "sun.sun":
                self.evaluate()
            else:
                self._state_event(event.data)

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
            self._heater_states[entity_id] = self._heater_state(
                self.hass.states.get(entity_id)
            )
        observed = self._aggregate_heater_state()
        self.actuator.observe(observed, initial=True)
        self._record_confirmed_run(observed, self.actuator.changed_at)
        if isinstance(self.curve_store, CurveStore):
            stamp = time.time()
            self._queue_operation("heater", stamp, {"on": observed})
            if not self._learning_reset_pending:
                self._track_operation(self._backfill_forecast_history(
                    self.curve_store, deepcopy(self.last_off_prediction), deepcopy(self.last_cooling_prediction),
                ))
                self._track_operation(self._backfill_heater_history(self.curve_store, stamp))
        # Carry a new stop through reload or reinstallation; related: __init__.py, controller.py.
        reload_off = self.hass.data.get(DOMAIN, {}).get("reload_off", {})
        reset_off_wait = clear_idle_reload_wait and any(heater in reload_off for heater in self.heaters)
        stopped_at = []
        self._reload_off_until = 0.0
        for heater in self.heaters:
            stop = reload_off.get(heater)
            if stop is not None:
                off_at, deadline = stop
                deadline = max(deadline, off_at + self.controller.settings.minimum_off_time)
                if deadline > now:
                    stopped_at.append(off_at)
                    self._reload_off_until = max(self._reload_off_until, deadline)
                    continue
            reload_off.pop(heater, None)
        if reset_off_wait:
            self._minimum_off_until = None
        confirmed_off_at = None
        if observed is False and stopped_at:
            self.actuator.changed_at = max(stopped_at)
        elif observed is False and self.climate_targets is None:
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
                confirmed_off_at = max(off_timestamps)
                elapsed_off = max(
                    float(self.controller.settings.minimum_off_time),
                    time.time() - confirmed_off_at,
                    0.0,
                )
                self.actuator.changed_at = now - elapsed_off
        elif observed is False and (self.store.is_new or reset_off_wait):
            # An already idle Climate starts without a new wait. See storage.py and controller.py.
            self.actuator.changed_at = now - self.controller.settings.minimum_off_time
        self.observation.seed_heater(observed)
        self.controller.startup_off_seen = observed is False
        if observed is None:
            self._mark_heater_unavailable(now)
        if len(self.heaters) > 1 and observed is not False:
            self.actuator.request(False, retry=True, force=True)
        if self.climate_targets is not None:
            self.actuator.request(False, retry=True)
        initial_temperature = self.controller.temperature
        self.curve_tracker.seed(observed, initial_temperature, time.time(), confirmed_off_at=confirmed_off_at)
        self.evaluate()

    @staticmethod
    def _switch_state(state) -> bool | None:
        if state is None or state.state not in ("on", "off"):
            return None
        return state.state == "on"

    def _heater_state(self, state) -> bool | None:
        """Read actual heat, with setpoint fallback for Climate entities without action."""
        if self.climate_targets is None:
            return self._switch_state(state)
        if state is not None and state.state == "heat" and "hvac_action" in state.attributes:
            action = state.attributes["hvac_action"]
            if action == "heating":
                return True
            if action in ("idle", "off"):
                return False
            return None
        return self._reported_climate_request(state)

    def _reported_climate_request(self, state) -> bool | None:
        """Read the requested heat state independently of action. Related: actuator.py."""
        if state is None:
            return None
        if state.state == "off":
            return False
        if state.state != "heat":
            return None
        reported = state.attributes.get("temperature")
        if type(reported) not in (int, float):
            return None
        precision = 0.5 if self.hass.config.units.temperature_unit == "°F" else 0.05
        low_error = abs(reported - self._climate_target(False))
        high_error = abs(reported - self._climate_target(True))
        if min(low_error, high_error) <= precision + 1e-6 and low_error != high_error:
            return high_error < low_error
        return None

    def _climate_target(self, heating: bool) -> float:
        target = self.climate_targets[heating]
        if self.hass.config.units.temperature_unit == "°F":
            return target * 9 / 5 + 32
        return target

    def _climate_confirmed(self, heating: bool) -> bool:
        """Check the reported setpoint in HA's configured unit; related: actuator.py."""
        return self._reported_climate_request(self.hass.states.get(self.heater)) is heating

    def _climate_command_confirmed(self, heating: bool) -> None:
        """Release the latest physical state to learning. Related: actuator.py, curve_learning.py."""
        state = self.hass.states.get(self.heater)
        self._state_event({
            "entity_id": self.heater, "new_state": state,
            "climate_confirmed": True, "confirmed_request": heating,
        })

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
        if entity_id in self._heater_states:
            was_pending = self.actuator.busy
            previous = self.actuator.observed
            on_started_at = self._confirmed_on_since
            requested = data.get("confirmed_request", self.actuator.pending)
            climate_confirmed = data.get("climate_confirmed", False)
            observed = self._heater_state(state)
            source_mode = getattr(state, "state", None)
            previous_source_mode = getattr(data.get("old_state"), "state", None)
            if source_mode in (None, "unknown", "unavailable"):
                self._heater_recovery_deadlines[entity_id] = None
            elif "old_state" in data and previous_source_mode in (None, "unknown", "unavailable"):
                self._heater_recovery_deadlines.setdefault(entity_id, None)
            if (observed is not None and entity_id in self._heater_recovery_deadlines
                    and self._heater_recovery_deadlines[entity_id] is None):
                self._heater_recovery_deadlines[entity_id] = now + HEATER_RECOVERY_OVERRIDE_GRACE
            recovery_deadline = self._heater_recovery_deadlines.get(entity_id)
            previous_member = self._heater_states[entity_id]
            if (self.climate_targets is not None and self.actuator.pending is not None
                    and source_mode == "heat" and "hvac_action" not in state.attributes):
                # Without action, hold the last observation until the setpoint is confirmed.
                observed = previous_member
            self._heater_states[entity_id] = observed
            aggregate = self._aggregate_heater_state()
            limited_stop = self._runtime_limit_stop_pending
            if aggregate is not False and self._solar_observation is not None:
                self._solar_finished = self._solar_observation.sunrise_at
                self._solar_observation = None
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
            if aggregate is not previous and not (len(self.heaters) > 1 and aggregate is None and was_pending):
                self._queue_operation("heater", time.time(), {"on": aggregate})
            self._record_confirmed_run(aggregate, self.actuator.changed_at)
            if external:
                self._clear_runtime_off_schedule()
            if aggregate is False and previous_member is None and now >= self._reload_off_until:
                # A restored OFF state after HA reconnect starts with no cooldown.
                # Related: controller.py, tests/components/adaptive_floor_heating/test_runtime.py.
                self.actuator.changed_at = now - self.controller.settings.minimum_off_time
                self._minimum_off_until = None
            if previous_member is not None and observed is not None and previous_member != observed:
                expected = self.actuator.pending
                external = external or expected is None or observed != expected
            if self.climate_targets is not None:
                old_request = self._reported_climate_request(data.get("old_state"))
                new_request = self._reported_climate_request(state)
                # A Climate may cycle its action while retaining the commanded setpoint.
                external = not was_pending and old_request is not None and new_request is not None and (
                    old_request != new_request and new_request != self.actuator.desired
                )
            # Preserve explicit thermostat OFF protection; related: climate.py, actuator.py.
            explicit_climate_off = (
                self.climate_targets is not None and self.controller.mode != "off"
                and previous_source_mode == "heat" and source_mode == "off" and requested is not False
            )
            if explicit_climate_off:
                external = True
            # A return from a transient mismatch can itself look like an external edge.
            # Keep the original observation until it changes; related: actuator.py, const.py.
            candidate = self._recovery_overrides.get(entity_id)
            if candidate is not None:
                candidate_running, candidate_climate_off = candidate
                if observed is not candidate_running or (candidate_climate_off and source_mode != "off"):
                    self._recovery_overrides.pop(entity_id)
                    if not explicit_climate_off and not (candidate_climate_off and observed is True):
                        external = False
            temperature = self.controller.temperature
            transitioning = len(self.heaters) > 1 and aggregate is None and was_pending
            if transitioning:
                return
            if aggregate is False and previous is True and on_started_at is not None:
                run_seconds = max(0.0, now - on_started_at)
                off_seconds = max(self.controller.settings.minimum_off_time, 2 * run_seconds)
                # Wait for the learned response from the ON edge. Related: controller.py, curve_learning.py.
                stop = self._predictive_stop
                if not external and requested is False and stop and stop.sunrise_response_minutes is not None:
                    off_seconds = max(off_seconds, stop.sunrise_response_minutes * 60 - run_seconds)
                self._minimum_off_until = now + off_seconds
            elif aggregate is True and previous is not True:
                self._minimum_off_until = None
                self.heating_prediction_info = {"model": None}
            # Keep safety observations live while deferring Climate learning. Related: actuator.py.
            learning_held = self.climate_targets is not None and (
                (was_pending and not climate_confirmed) or "actuation_fault" in self.controller.faults
            )
            if aggregate is not None and not learning_held:
                previous_learning = self.curve_tracker.heater if self.climate_targets is not None else previous
                learning_now = self.actuator.changed_at if climate_confirmed and aggregate is not previous_learning else now
                resumed_learning = self._resolve_learning_outage(now, aggregate)
                if aggregate is not previous_learning and not resumed_learning:
                    wall_now = time.time() - (now - learning_now)
                    cycle = self.curve_tracker.cycle
                    predictive_stop = self._predictive_stop
                    self._predictive_stop = None
                    if (aggregate is not False or previous_learning is not True or external
                            or cycle is None or (predictive_stop and predictive_stop.cycle_id != cycle.id)):
                        predictive_stop = None
                    sunrise_stop = predictive_stop if predictive_stop and predictive_stop.sunrise_saving else None
                    self._sunrise_coast = sunrise_stop is not None
                    # Capture both models before the OFF edge clears the active ON trace.
                    # Related: off_response.py; preserve the forecast used by AUTO.
                    live_forecast = predictive_stop.forecast if predictive_stop else self.off_prediction or {}
                    if (self.controller.mode == "auto" and predictive_stop is None
                            and live_forecast.get("off_control") is not None):
                        live_forecast = {}
                    if (aggregate is False and previous_learning is True and not external
                            and not self._learning_reset_pending):
                        try:
                            self._off_predictions = self._predict_both_off(now, live_forecast=live_forecast)
                        except Exception as err:
                            self._off_predictions = {}
                            self._off_prediction_confidences = {}
                            self._off_peak_deadlines = {}
                            self._off_peak_forecast = None
                            _LOGGER.warning("OFF comparison unavailable (%s)", type(err).__name__)
                        self._off_diagnostic_at = wall_now
                        self.last_off_prediction = {
                            "off_at": self._off_diagnostic_at, "predictions": dict(self._off_predictions),
                            "confidences": dict(self._off_prediction_confidences),
                            "comparison_counts": {
                                model: (0 if model == "curve" and live_forecast.get("off_learning_source") == "long_term"
                                        else self._prediction_confidence(self._feedback_model(model, self._off_peak_forecast))[1])
                                for model in self._off_predictions
                            },
                            "selected_model": "curve",
                            "runtime_sample_count": live_forecast.get("accepted_cycles"),
                            "runtime_curve_range": live_forecast.get("runtime_curve_range"),
                            "runtime_extrapolated": live_forecast.get("runtime_extrapolated"),
                        }
                        if self.controller.mode == "auto":
                            control = live_forecast.get("off_control")
                            if control is None:
                                forecast, comparable = self._hybrid_peak(now)
                                control = {
                                    "action": "base_control", "reason": (
                                        "BASE_CONTROL" if forecast else "INSUFFICIENT_HYBRID_EVIDENCE"),
                                    "comparable_cycles": comparable, "planned_minutes": None,
                                    "forecast_available": forecast is not None,
                                }
                            self.last_off_prediction["off_control"] = dict(control)
                            if "residual_preview" in live_forecast:
                                self.last_off_prediction["residual_preview"] = dict(live_forecast["residual_preview"])
                        if sunrise_stop is not None:
                            self.last_off_prediction["sunrise_saving"] = True
                        if predictive_stop:
                            self.last_off_prediction["decision_target"] = predictive_stop.target
                            advance = live_forecast.get("advance_minutes")
                            if advance is not None:
                                self.last_off_prediction["advance_minutes"] = advance
                        if "outdoor_conditioned" in live_forecast:
                            self.last_off_prediction["outdoor_conditioned"] = live_forecast["outdoor_conditioned"]
                        if "curve" in self._off_predictions:
                            feedback_model = self._feedback_model("curve", self._off_peak_forecast)
                            self.last_off_prediction["off_model"] = (
                                "runtime" if feedback_model == "curve" else
                                "hybrid" if feedback_model == "weighted_residual" else feedback_model)
                            if live_forecast.get("off_learning_source") == "long_term":
                                self.last_off_prediction["off_model"] = live_forecast["off_model"]
                                self.last_off_prediction["off_learning_source"] = "long_term"
                        if self._off_peak_forecast is not None:
                            self.last_off_prediction["peak_forecast"] = dict(self._off_peak_forecast)
                            if predictive_stop:
                                self.last_off_prediction["decision_forecast"] = {
                                    "cycle_id": predictive_stop.cycle_id,
                                    "generated_at": live_forecast["generated_at"],
                                    "forecast": dict(self._off_peak_forecast),
                                }
                        # Freeze the shadow forecast at the same confirmed OFF; related: storage.py, curve_storage.py.
                        cycle = self.curve_tracker.cycle
                        if (self.last_off_prediction.get("off_model") == "hybrid"
                                and (self._off_peak_forecast or {}).get("kind") == HYBRID_VERSION
                                and self.last_off_prediction.get("off_learning_source") != "long_term"
                                and self.curve_store is not None and cycle is not None
                                and temperature is not None and not cycle.invalid_reason):
                            wall_now = self._off_diagnostic_at
                            try:
                                summary_peak = self.curve_store.model.predict_hybrid_summary(
                                    temperature, (wall_now - cycle.started_at) / 60,
                                    self._optional_temperature(self.outdoor_sensor, now), wall_now,
                                )
                                if summary_peak is not None:
                                    self.last_off_prediction["hybrid_summary_peak"] = summary_peak
                            except Exception as err:
                                _LOGGER.warning("Hybrid summary comparison unavailable (%s)", type(err).__name__)
                        self._queue_operation("off_prediction", self._off_diagnostic_at, self.last_off_prediction)
                    elif aggregate is True or external:
                        self._off_predictions = {}
                        self._off_diagnostic_at = None
                    # Attribute confirmed heat to the original ON command; related: actuator.py, curve_learning.py.
                    start_reason = (
                        "PREDICTIVE_START" if aggregate is True and not external
                        and self.actuator.predictive_start_requested
                        else "THRESHOLD_START" if not external and (
                            requested is True or (self.climate_targets is not None and self.actuator.desired)
                        ) else "UNKNOWN"
                    )
                    if aggregate is True:
                        self.actuator.predictive_start_requested = False
                    off_reason = (
                        "EXTERNAL_STOP" if external else
                        "MAXIMUM_RUNTIME_LIMIT" if limited_stop and aggregate is False else
                        "PREDICTIVE_STOP" if predictive_stop or self.decision.state == "PREDICTIVE_OFF" else
                        "TARGET_REACHED" if self.decision.state == "IDLE" else
                        "SAFETY_STOP" if self.decision.state == "FAULT" else
                        "MANUAL_STOP" if self.controller.mode == "off" else "UNKNOWN"
                    )
                    # Share the configured stop tolerance with curve_learning.py and curve_storage.py.
                    stop_tolerance = (self.controller.settings.heat_hot_tolerance
                                      if self.controller.mode == "heat" else self.controller.settings.hot_tolerance)
                    self.curve_tracker.switch(
                        aggregate, wall_now, start_reason=start_reason,
                        off_reason=off_reason, mode=self.prediction_mode.upper(),
                        # Match both models with the same measured OFF conditions; related: history.py.
                        off_context=self.observation.off_context(learning_now) if aggregate is False else None,
                        off_slope=self.observation.temperature_slope,
                        response_observed=self.observation.response_observed if aggregate is False else None,
                        sunrise_saving=self._sunrise_coast,
                        sunrise_peak_excess=(
                            self._sunrise_peak_baseline(wall_now) if aggregate is False
                            and self._off_diagnostic_at == wall_now else None
                        ),
                        sunrise_prediction=(
                            {"off_model": live_forecast.get("off_model"),
                             "predicted_peak": live_forecast.get("predicted_peak")}
                            if sunrise_stop else None
                        ),
                        off_setpoint=self.controller.target if aggregate is False else None,
                        off_stop_tolerance=stop_tolerance if aggregate is False else None,
                        target_peak_temperature=(predictive_stop.target if predictive_stop
                                                 else self.controller.target + stop_tolerance),
                        outdoor_temperature=self._optional_temperature(self.outdoor_sensor, now),
                        outdoor_expires_at=wall_now + max(0.0, self._next_sensor_check - now) + BUCKET_SECONDS,
                        hold_settings={
                            "target_temperature": self.controller.target,
                            "hot_tolerance": stop_tolerance,
                            "cold_tolerance": self.controller.settings.cold_tolerance,
                        } if aggregate is True else None,
                    )
                    tracked = self.curve_tracker.cycle
                    self._sunrise_diagnostic = (
                        (tracked.id, tracked.curve_type, tracked.sunrise_peak_excess)
                        if aggregate is False and tracked and tracked.sunrise_peak_excess else None
                    )
                    # Seed with the remaining validity of the last outdoor report; related: curve_learning.py.
                    if aggregate is False and self.outdoor_sensor is not None:
                        outdoor = self._optional_temperature(self.outdoor_sensor, now)
                        if outdoor is not None:
                            self.curve_tracker.report_cooling_outdoor(
                                wall_now, outdoor,
                                wall_now + max(0.0, self._next_sensor_check - now) + BUCKET_SECONDS,
                            )
                    if external:
                        self.curve_tracker.invalidate("EXTERNAL_OVERRIDE")
                    self._queue_curve_results()
                if external and (self.controller.mode != "off" or self.climate_targets is not None):
                    self.observation.invalidate_cycle(aggregate)
                else:
                    # Apply the frozen OFF policy to both observers; related: history.py, curve_learning.py.
                    if aggregate is False:
                        tracked = self.curve_tracker.cycle
                        sunrise_end = bool(tracked and (tracked.sunrise_peak_excess or {}).get("ends_observation"))
                        self.observation.max_peak_wait_seconds = (
                            float("inf") if sunrise_end else self.peak_observation_seconds
                        )
                    self.observation.observe_heater(aggregate, learning_now, temperature)
            elif aggregate is None:
                if self._learning_outage_deadline is None and self.curve_tracker.heater is not None:
                    self._learning_outage_deadline = now + self.controller.settings.heater_unavailable_grace
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
            if external and (self.controller.mode != "off" or self.climate_targets is not None):
                if (observed is not None and recovery_deadline is not None and now < recovery_deadline
                        and not self._off_transition_pending):
                    self._recovery_overrides[entity_id] = (observed, explicit_climate_off)
                    if observed:
                        self.actuator.request(False, retry=True, force=True)
                else:
                    self._latch_external_override()
            elif aggregate is True and self._off_transition_pending:
                self._fault("external_override")
                self.actuator.request(False, retry=True, force=True)
            if (previous is None and aggregate is not None and not was_pending
                    and self.controller.mode != "off"):
                self.actuator.request(False, retry=True)
        if not data.get("climate_confirmed"):
            self.evaluate()

    # Queue real observations in order; related: curve_learning.py, history.py.
    def _defer_learning_report(self, callback, *args) -> None:
        # Preserve the physical edge timestamp when applying a confirmed Climate transition.
        if self.climate_targets is not None and self.actuator.pending is not None:
            return
        if self._learning_outage_deadline is None:
            callback(*args)
        else:
            self._learning_outage_reports.append(partial(callback, *args))

    def _resolve_learning_outage(self, now: float, heater: bool | None) -> bool:
        deadline = self._learning_outage_deadline
        if deadline is None or (heater is None and now < deadline):
            return False
        self._learning_outage_deadline = None
        reports, self._learning_outage_reports = self._learning_outage_reports, []
        if now < deadline and heater is self.curve_tracker.heater:
            for report in reports:
                report()
            return True
        self.curve_tracker.switch(None, time.time())
        self.observation.invalidate_cycle(None)
        return False

    def _report_learning_temperature(self, temperature: float | None, now: float,
                                     wall_now: float, outdoor: float | None) -> None:
        curve_cycle = self.curve_tracker.cycle
        # Capture outdoor temperature at the same actual report; related: curve_learning.py.
        self.curve_tracker.report_temperature(
            temperature, wall_now, outdoor_temperature=outdoor
        )
        # Exclude the same rebound from measured peak learning; related: curve_learning.py, history.py.
        if curve_cycle is not None and (curve_cycle.peak_rebound_reports >= 2 or
                (curve_cycle.sunrise_peak_excess or {}).get("first_at") == wall_now):
            self.observation.invalidate_cycle(self.curve_tracker.heater)
        self._queue_curve_results()
        self._update_sunrise_diagnostic(temperature, wall_now)
        if temperature is None:
            self._off_predictions = {}
            self._off_peak_deadlines = {}
            self._off_diagnostic_at = None
        completed_cycles = self.observation.completed_cycles
        observed_off_at = self.observation.off_at
        self.observation.report_temperature(temperature, now)
        if (self.observation.completed_cycles > completed_cycles
                and not self._learning_reset_pending and not self._sunrise_coast
                and (curve_cycle is None or all(check["passed"] for check in curve_cycle.heating_learning_checks()))):
            actual_peak = self.observation.last_cycle.peak_temperature
            if actual_peak is not None:
                off_at = self._off_diagnostic_at
                if off_at is None and observed_off_at is not None:
                    off_at = wall_now - (now - observed_off_at)
                peak_at = (off_at + self.observation.last_cycle.peak_delay_minutes * 60
                           if off_at is not None else None)
                self._record_peak_comparison(actual_peak, off_at, peak_at)
            self.thermal_model.add_cycle(self.observation.last_cycle)
            self.learning_store.schedule(self.thermal_model.snapshot())

    # Start the cap at confirmed ON and keep its clock through unknown reports; related: actuator.py.
    # Keep evidence writes independent from actuation. Related: curve_storage.py, peak_migration.py.
    def _track_operation(self, operation):
        async def persist():
            try:
                await operation
            except Exception as err:
                _LOGGER.warning("Operation history unavailable (%s)", type(err).__name__)

        task = asyncio.create_task(persist())
        self._curve_tasks.add(task)
        task.add_done_callback(self._curve_tasks.discard)

    def _queue_operation(self, kind, stamp, payload):
        if isinstance(self.curve_store, CurveStore) and not self._learning_reset_pending:
            self._track_operation(self.curve_store.save_operation_history([(kind, stamp, deepcopy(payload))]))

    # Migrate restored forecasts independently of Recorder availability. Related: curve_storage.py.
    async def _backfill_forecast_history(self, store, last_off, last_cooling):
        if await store.operation_backfilled(FORECAST_HISTORY_BACKFILL):
            return
        events = [
            (kind, value[key], value)
            for kind, value, key in (
                ("off_prediction", last_off, "off_at"),
                ("cooling_prediction", last_cooling, "generated_at"),
            )
            if value and value.get(key) is not None
        ]
        await store.save_operation_history(events, migration=FORECAST_HISTORY_BACKFILL)

    async def _backfill_heater_history(self, store, until):
        if await store.operation_backfilled(HEATER_HISTORY_BACKFILL):
            return
        try:
            from homeassistant.components.recorder import get_instance, history
            recorder = get_instance(self.hass)
        except (ImportError, KeyError, RuntimeError):
            return
        end = datetime.fromtimestamp(until, UTC)
        rows = await recorder.async_add_executor_job(partial(
            history.get_significant_states, self.hass, end - timedelta(days=CYCLE_RETENTION_DAYS),
            end, list(self.heaters), include_start_time_state=True,
            significant_changes_only=False, minimal_response=False, no_attributes=False,
        ))
        edges = sorted(
            ((row.last_updated.timestamp(), entity_id, self._heater_state(row))
             for entity_id in self.heaters for row in rows.get(entity_id, ())
             if row.last_updated.timestamp() < until),
            key=lambda edge: edge[0],
        )
        members = dict.fromkeys(self.heaters)
        events = []
        previous = None
        for stamp, entity_id, value in edges:
            members[entity_id] = value
            aggregate = next(iter(members.values()))
            if any(member is not aggregate for member in members.values()):
                aggregate = None
            if aggregate is not previous:
                events.append(("heater", stamp, {"on": aggregate}))
                previous = aggregate
        await store.save_operation_history(events, migration=HEATER_HISTORY_BACKFILL)

    def _record_confirmed_run(self, heater: bool | None, now: float) -> None:
        if heater is False:
            self._confirmed_on_since = self._maximum_on_deadline = None
            self._runtime_limit_stop_pending = False
        elif heater is True and self._confirmed_on_since is None:
            self._confirmed_on_since = now
            self._maximum_on_deadline = now + 60 * self.controller.settings.initial_maximum_on_minutes

    def _optional_temperature(self, entity_id: str | None, _now: float) -> float | None:
        """Return the sampled value of a configured temperature sensor."""
        return self._temperature(self._sensor_samples.get(entity_id))

    def _minimum_off_deadline(self) -> float:
        """Return the configured OFF deadline extended by the last ON runtime."""
        configured = self.actuator.changed_at + self.controller.settings.minimum_off_time
        return max(configured, self._minimum_off_until or configured, self._reload_off_until)

    # Resolve daily schedule boundaries on the HA local clock; related: time.py, storage.py.
    # Freeze the OFF forecast and astronomical sunrise; related: curve_learning.py.
    def _sunrise_peak_baseline(self, off_at: float) -> dict | None:
        # Use the dashboard's committed recent MAE, frozen at OFF; related: curve_storage.py.
        metrics = self.curve_store.heating_metrics if isinstance(self.curve_store, CurveStore) else {}
        mae = metrics.get("mean_absolute_peak_error")
        if type(mae) not in (int, float) or not isfinite(mae) or not 0 <= mae <= 0.5:
            return None
        peak = self._off_predictions.get("curve")
        sun = self.hass.states.get("sun.sun")
        rising = sun.attributes.get("next_rising") if sun is not None else None
        if peak is None or not isfinite(peak) or not isinstance(rising, str):
            return None
        try:
            sunrise = datetime.fromisoformat(rising)
        except ValueError:
            return None
        if sunrise.tzinfo is None or sunrise.timestamp() < off_at:
            return None
        if sun.state == "above_horizon":
            from homeassistant.helpers.sun import get_astral_event_date

            today = get_astral_event_date(self.hass, "sunrise", datetime.fromtimestamp(off_at, UTC))
            cycle = self.curve_tracker.cycle
            if today is not None and (self._sunrise_active or
                    (cycle is not None and cycle.started_at <= today.timestamp())):
                sunrise = today
        return {"predicted_peak": peak, "sunrise_at": sunrise.timestamp(), "threshold": 0.5,
                "ends_observation": True, "mean_absolute_error": mae}

    # Extend the diagnostic past peak completion; related: curve_storage.py, frontend/panel.js.
    def _update_sunrise_diagnostic(self, temperature: float | None, now: float) -> None:
        if (self._sunrise_diagnostic is None or temperature is None or self._learning_reset_pending
                or not isinstance(self.curve_store, CurveStore)):
            return
        cycle_id, curve_type, excess = self._sunrise_diagnostic
        if now < excess["sunrise_at"]:
            return
        difference = temperature - excess["predicted_peak"]
        maximum = excess.get("maximum_excess")
        if maximum is not None and difference <= maximum:
            return
        excess["maximum_excess"] = difference
        if difference > excess["threshold"] + 1e-9 and "first_at" not in excess:
            excess.update(first_at=now, first_temperature=temperature)
        task = asyncio.create_task(self._save_sunrise_diagnostic(self.curve_store, cycle_id, curve_type, dict(excess)))
        self._curve_tasks.add(task)
        task.add_done_callback(self._curve_tasks.discard)

    async def _save_sunrise_diagnostic(self, store: CurveStore, cycle_id: str,
                                       curve_type: str, excess: dict) -> None:
        try:
            await store.update_sunrise_excess(cycle_id, curve_type, excess)
            self._notify()
        except Exception as err:
            _LOGGER.warning("Sunrise diagnostic could not be saved (%s)", type(err).__name__)

    # Schedule passive samples one hour after sunrise; related: curve_learning.py, curve_storage.py.
    def _observe_solar_gain(self, now: float, wall_now: float) -> float | None:
        if self.curve_store is None or self._learning_reset_pending:
            self._solar_observation = None
            return None
        sun = self.hass.states.get("sun.sun")
        if sun is None or sun.state not in ("above_horizon", "below_horizon"):
            if self._solar_observation is not None:
                self._solar_finished = self._solar_observation.sunrise_at
                self._solar_observation = None
            return None
        rising = sun.attributes.get("next_rising")
        try:
            event = datetime.fromisoformat(rising) if isinstance(rising, str) else None
        except ValueError:
            event = None
        upcoming = event.timestamp() if event is not None and event.tzinfo is not None else None
        if self._solar_sunrise is None or (
            self._solar_finished == self._solar_sunrise and upcoming is not None and upcoming > wall_now
        ):
            self._solar_sunrise = upcoming
        sunrise = self._solar_sunrise
        if sunrise is None or sunrise == self._solar_finished:
            return None
        starts_at = sunrise + SOLAR_START_DELAY_SECONDS
        if wall_now < starts_at:
            return now + starts_at - wall_now

        observation = self._solar_observation
        pending_peak = self.curve_tracker.cycle
        if (self.actuator.observed is not False or self._aggregate_heater_state() is not False
                or self.actuator.pending is not None or self._learning_outage_deadline is not None
                or self.preset == "away"
                or (pending_peak is not None and pending_peak.peak_at is None)
                or wall_now - starts_at >= self.peak_observation_seconds):
            self._solar_finished = sunrise
            self._solar_observation = None
            return None
        if observation is None:
            observation = self._solar_observation = SolarGainObservation(sunrise)
        expected = starts_at + len(observation.samples) * BUCKET_SECONDS
        if wall_now < expected:
            return now + expected - wall_now
        temperature = self.controller.temperature
        elevation = sun.attributes.get("elevation")
        if (wall_now - expected > 30 or temperature is None
                or type(elevation) not in (float, int) or not isfinite(elevation)):
            self._solar_finished = sunrise
            self._solar_observation = None
            return None

        weather = self._sensor_samples.get(self.sunrise_weather_entity)
        sunny = (int(weather.state == "sunny") if weather is not None
                 and weather.state not in ("unknown", "unavailable") else None)
        if observation.sample(wall_now, temperature, elevation, sunny):
            self._solar_finished = sunrise
            self._solar_observation = None
            result = observation.result(wall_now, self.controller.mode, self.preset)
            task = asyncio.create_task(self._save_curve_result(result))
            self._curve_tasks.add(task)
            task.add_done_callback(self._curve_tasks.discard)
            return None
        return now + expected + BUCKET_SECONDS - wall_now

    def _sunrise_window(self, timestamp: float) -> tuple[bool, float | None]:
        start, end = self.sunrise_start, self.sunrise_end
        if not self.sunrise_enabled or start == end:
            return False, None
        zone_name = getattr(getattr(self.hass, "config", None), "time_zone", None)
        zone = UTC if zone_name in (None, "UTC") else ZoneInfo(zone_name)
        local = datetime.fromtimestamp(timestamp, zone)
        clock = local.time()
        active = start <= clock < end if start < end else clock >= start or clock < end
        # Check both folds so a repeated local time can trigger a fresh evaluation.
        boundaries = (
            datetime.combine(local.date() + timedelta(days=day), boundary, zone)
            .replace(fold=fold).timestamp()
            for day in (0, 1) for boundary in (start, end) for fold in (0, 1)
        )
        return active, min(value - timestamp for value in boundaries if value > timestamp)

    # Use the same learning gate for control and diagnostics. Related: curve_learning.py, binary_sensor.py.
    @property
    def predictive_activation_sample_count(self) -> int:
        """Count usable runtime samples for both feature switches; related: switch.py."""
        if self._learning_reset_pending or self._curve_storage_failed:
            return 0
        model = getattr(self.curve_store, "model", None)
        return len(model.runtime_samples) if model is not None else 0

    def _sunrise_ready(self, now: float) -> bool:
        if self._learning_reset_pending or self._curve_storage_failed or self.curve_store is None:
            return False
        model = getattr(self.curve_store, "model", None)
        if model is None or self.predictive_activation_sample_count < MIN_PREDICTIVE_ACTIVATION_SAMPLES:
            return False
        sunrise = getattr(self.curve_store, "sunrise_runtime_model", None)
        return bool(model.off_ready_models(now) or (sunrise and "runtime" in sunrise.off_ready_models(now)))

    def _sunrise_response_minutes(self) -> float | None:
        """Use the nearest-rank P75 of the last ten usable regular response delays."""
        delays = [row[2] for row in self.curve_store.model.runtime_samples
                  if row[2] is not None and isfinite(row[2]) and row[2] > 0]
        return sorted(delays[-10:])[7] if len(delays) >= 10 else None

    # Apply the reduced OFF target during daylight and the scheduled window; related: controller.py.
    def _predictive_off_target(self) -> float:
        if self._sunrise_active or self._sunrise_daylight:
            return self.controller.target
        return self.controller.target + self.controller.settings.hot_tolerance

    @property
    def sunrise_saving_active(self) -> bool:
        """Expose the active AUTO saving target; related: climate.py, frontend/panel.js."""
        return self.controller.mode == "auto" and (self._sunrise_active or self._sunrise_daylight)

    def set_sunrise_schedule(self, *, enabled: bool | None = None,
                             start: ClockTime | None = None, end: ClockTime | None = None) -> None:
        """Update the schedule and re-evaluate AUTO; related: switch.py, time.py."""
        if self.controller.stopping:
            raise ValueError("The integration is stopping; retry after reload")
        if enabled is not None:
            self.sunrise_enabled = enabled
        if start is not None:
            self.sunrise_start = start.replace(tzinfo=None)
        if end is not None:
            self.sunrise_end = end.replace(tzinfo=None)
        self.evaluate()

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
        """Dispatch the heater command; related: actuator.py and config_flow.py."""
        if self.climate_targets is not None:
            data = {"entity_id": self.heater, "temperature": self._climate_target(heating)}
            if heating:
                data["hvac_mode"] = "heat"
            await self.hass.services.async_call("climate", "set_temperature", data, blocking=True)
            return
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

    # Keep reconnect and immediate overrides on the same fault path; related: controller.py, storage.py.
    def _latch_external_override(self) -> None:
        if self.controller.mode == "auto":
            self.controller.auto_external_override = True
        self.controller.mode = "off"
        self._recovery_overrides.clear()
        self._fault("external_override")
        if len(self.heaters) > 1 or self.climate_targets is not None:
            self.actuator.request(False, retry=True, force=True)

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
        # Confirm a lasting mismatch even if no new reports arrive; related: _state_event, const.py.
        for heater, deadline in tuple(self._heater_recovery_deadlines.items()):
            if deadline is not None and now >= deadline:
                del self._heater_recovery_deadlines[heater]
                if heater in self._recovery_overrides:
                    self._latch_external_override()
        sunrise_active, sunrise_delay = self._sunrise_window(time.time())
        sun = self.hass.states.get("sun.sun")
        sunrise_daylight = self.sunrise_enabled and getattr(sun, "state", None) == "above_horizon"
        sunrise_ready = (sunrise_active or sunrise_daylight) and self._sunrise_ready(time.time())
        sunrise_active = sunrise_active and sunrise_ready
        sunrise_daylight = sunrise_daylight and sunrise_ready
        if sunrise_active != self._sunrise_active or sunrise_daylight != self._sunrise_daylight:
            self._sunrise_active = sunrise_active
            self._sunrise_daylight = sunrise_daylight
            self._clear_runtime_off_schedule()
        waiting_for_climate_off = (
            self.climate_targets is not None and not self.actuator.desired
            and self.actuator.observed is True
        )
        if self.actuator.pending is not False and not waiting_for_climate_off:
            self._predictive_stop = None
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
        self._resolve_learning_outage(now, None)
        self._read_current_temperature(now)
        if (self._learning_outage_deadline is None
                and (self.climate_targets is None or self.actuator.pending is None)):
            self.curve_tracker.advance(time.time())
        self._queue_curve_results()
        solar_deadline = self._observe_solar_gain(now, time.time())
        decision = self.controller.decide(
            now, self.actuator.observed, self.actuator.changed_at,
            minimum_off_deadline=self._minimum_off_deadline(),
            heating_stop_target=self.controller.target if self.controller.mode == "auto"
            and self._sunrise_daylight else None,
        )
        self.off_prediction = None
        self.hybrid_control_support = None
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
                      and len(self.curve_store.model.runtime_samples) < MIN_RUNTIME_SAMPLES):
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
        if (decision.state == "PREDICTIVE_OFF" and self.curve_store is not None and not self.sunrise_saving_active
                and cycle is not None and cycle.baseline is not None):
            minimum = self.curve_store.model.shortfall_minimum(
                cycle.baseline, self._predictive_off_target(),
                self.curve_tracker.heating_weather(time.time()), time.time())
            if now - self.actuator.changed_at < minimum * 60:
                self.curve_fallback_reason = "PREVIOUS_HEATING_SHORTFALL"
                decision = Decision(True, "HEATING", self.actuator.changed_at + minimum * 60)
        if self._learning_reset_in_progress and decision.heating:
            decision = Decision(False, "IDLE")
        if waiting_for_climate_off and decision.heating:
            decision = Decision(False, "IDLE")
        if self._recovery_overrides and decision.heating:
            decision = Decision(False, "IDLE")
        maximum_on_deadline = self._maximum_on_deadline
        if (decision.heating and self.actuator.observed is True
                and maximum_on_deadline is not None and now >= maximum_on_deadline):
            self._runtime_limit_stop_pending = True
            decision = Decision(False, "MAXIMUM_RUNTIME_LIMIT")
        if decision.state != self.decision.state:
            _LOGGER.info("%s control state: %s", self.heater, decision.state)
        self.decision = decision
        if self.controller.mode != "off":
            # Freeze the exact forecast sent to the actuator; related: actuator.py, curve_storage.py.
            if (decision.state == "PREDICTIVE_OFF" and self.off_prediction
                    and cycle is not None and self._predictive_stop is None
                    and self.actuator.pending is not False and not self.actuator.off_exhausted):
                # Classify saving for learning using the cycle's ON edge; related: curve_learning.py, curve_storage.py.
                sunrise_learning_exclusion = self._sunrise_active and self._sunrise_window(cycle.started_at)[0]
                self._predictive_stop = PredictiveStop(
                    cycle.id, self._predictive_off_target(), deepcopy(self.off_prediction), sunrise_learning_exclusion,
                    self._sunrise_response_minutes() if self.sunrise_saving_active else None,
                )
            self.actuator.request(decision.heating, predictive_start=decision.state == "PREDICTIVE_ON")
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        deadlines = [self._next_sensor_check]
        if solar_deadline is not None:
            deadlines.append(solar_deadline)
        if sunrise_delay is not None:
            deadlines.append(now + sunrise_delay)
        # Refresh held-state predictive slopes at five-minute boundaries without synthetic reports.
        # Related: history.py advance_time and TemperatureHistory.slope.
        if self.controller.mode == "auto" and self.controller.temperature is not None:
            deadlines.append((int(now // 300) + 1) * 300)
        if self._heater_unavailable_started is not None:
            deadlines.append(self._heater_unavailable_started + HEATER_UNAVAILABLE_DELAY)
        deadlines.extend(deadline for deadline in self._heater_recovery_deadlines.values() if deadline is not None)
        if self.curve_store is not None:
            deadlines.append(self._curve_cleanup_at)
        if self._learning_outage_deadline is not None:
            deadlines.append(self._learning_outage_deadline)
        cycle = self.curve_tracker.cycle
        if cycle is not None and self._learning_outage_deadline is None:
            # The curve clock uses UTC seconds; the actuator clock stays monotonic.
            wall_now = time.time()
            curve_deadlines = [
                cycle.cooling_next_tick if cycle.peak_at is not None else cycle.next_tick
            ]
            if (cycle.off_at is not None and cycle.peak_at is None
                    and not (cycle.sunrise_peak_excess or {}).get("ends_observation")):
                curve_deadlines.append(cycle.off_at + self.peak_observation_seconds)
            deadlines.extend(
                now + max(0.001, value - wall_now)
                for value in curve_deadlines if value is not None and value > wall_now
            )
        if decision.deadline is not None:
            deadlines.append(decision.deadline)
        if decision.heating and maximum_on_deadline is not None and now < maximum_on_deadline:
            deadlines.append(maximum_on_deadline)
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
        below_target = self.curve_tracker.take_below_target_cooling()
        if self._learning_reset_pending:
            return
        comparisons = {}
        for result in results:
            if (result.curve_type in ("WARM_HEATING", "PREDICTIVE_WARM_HEATING") and result.off_at is not None
                    and result.peak_at is not None and result.peak_temperature is not None
                    and all(check["passed"] for check in result.heating_learning_checks)
                    and not result.invalid_reason):
                self._record_peak_comparison(
                    result.peak_temperature, result.off_at, result.peak_at,
                    completed_at=result.ended_at, sunrise_saving=result.sunrise_saving,
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
        # Enqueue after the original hold so its row exists before the update; related: curve_storage.py.
        for cycle_id, observation in below_target:
            task = asyncio.create_task(self._save_below_target_cooling(cycle_id, observation))
            self._curve_tasks.add(task)
            task.add_done_callback(self._curve_tasks.discard)

    async def _save_below_target_cooling(self, cycle_id: str, observation: dict) -> None:
        try:
            await self.curve_store.save_below_target_cooling(cycle_id, observation)
        except Exception as err:
            _LOGGER.error("Below-target cooling observation could not be saved (%s)", type(err).__name__)

    # Compare against matching OFF metadata from live or restored state; related: storage.py, climate.py.
    def _record_peak_comparison(self, actual_peak: float, off_at: float | None,
                                peak_at: float | None, *, completed_at: float | None = None,
                                sunrise_saving: bool = False) -> None:
        """Pair a confirmed peak with its OFF forecast, including after a restart."""
        completed_at = time.time() if completed_at is None else completed_at
        off_prediction = self.last_off_prediction or {}
        prediction_off_at = off_prediction.get("off_at")
        matches_live = (off_at is not None and self._off_diagnostic_at is not None
                        and abs(off_at - self._off_diagnostic_at) <= 30)
        matches_saved = (off_at is not None and isinstance(prediction_off_at, (int, float))
                         and abs(off_at - prediction_off_at) <= 30)
        sunrise_saving = sunrise_saving or (matches_saved and off_prediction.get("sunrise_saving") is True)
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
        for model, error in (() if sunrise_saving else errors.items()):
            # CurveStore learns only from committed, admitted samples; related: curve_storage.py.
            if model == "curve" and (isinstance(self.curve_store, CurveStore)
                    or self._feedback_model(model, off_prediction.get("peak_forecast")) == "weighted_residual"):
                continue
            if model == "curve" and matches_saved and off_prediction.get("off_learning_source") == "long_term":
                continue
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
                previous_weight = 1.0 if model == "curve" else 0.8
                self.prediction_bias[model] = max(-0.5, min(
                    0.5, self.prediction_bias[model] * previous_weight + correction * 0.2
                ))
        self.last_peak_comparison = {
            "actual_peak": actual_peak, "predictions": predictions,
            "errors": errors, "confidences": comparison_confidences,
            "comparison_counts": comparison_counts,
            "off_at": off_at, "peak_at": peak_at,
            "completed_at": completed_at,
        }
        if sunrise_saving:
            self.last_peak_comparison["sunrise_saving"] = True
        if matches_saved and "off_control" in off_prediction:
            self.last_peak_comparison["off_control"] = dict(off_prediction["off_control"])
        if matches_saved and off_prediction.get("peak_forecast", {}).get("kind") == WEIGHTED_VERSION:
            self.last_peak_comparison["forecast_kind"] = WEIGHTED_VERSION
        if matches_saved and off_prediction.get("off_model") in OFF_MODELS and "curve" in predictions:
            self.last_peak_comparison["off_model"] = off_prediction["off_model"]
            if off_prediction.get("off_learning_source") == "long_term":
                self.last_peak_comparison["off_learning_source"] = "long_term"
            if off_prediction["off_model"] == "hybrid" and "hybrid_summary_peak" in off_prediction:
                self.last_peak_comparison["hybrid_summary_peak"] = off_prediction["hybrid_summary_peak"]
        # Capture the model as it exists at confirmation, never recalculate on UI reads.
        # Related: curve_api.py, storage.py, climate.py and frontend/panel.js.
        if peak_at is not None and not sunrise_saving:
            from .curve_api import cooling_prediction
            self.last_cooling_prediction = cooling_prediction(self, peak_at, actual_peak, completed_at)
            if self.last_cooling_prediction:
                self._queue_operation("cooling_prediction", completed_at, self.last_cooling_prediction)
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
            if isinstance(self.curve_store, CurveStore) and getattr(result, "curve_type", None) in (
                "WARM_HEATING", "PREDICTIVE_WARM_HEATING",
            ):
                self._restore_control_feedback()
                self.store.schedule(self.snapshot())
            self._notify()
        except Exception as err:
            self._curve_storage_failed = True
            _LOGGER.error("Curve segment could not be saved (%s)", type(err).__name__)
            self._queue_evaluate()

    # Replay admitted errors so a newly quarantined cycle cannot retain a bias contribution.
    # Related: curve_storage.py and storage.py.
    def _restore_control_feedback(self) -> None:
        self.prediction_bias["curve"] = 0.0
        self.prediction_errors["curve"] = []
        self.hybrid_feedback = {"bias": 0.0, "errors": []}
        for comparison in self.curve_store.control_feedback:
            error = comparison.get("errors", {}).get("curve")
            if type(error) not in (int, float) or not isfinite(error) or comparison.get("off_learning_source") == "long_term":
                continue
            if comparison.get("forecast_kind") == WEIGHTED_VERSION:
                continue
            if comparison.get("off_model") == "hybrid":
                feedback = self.hybrid_feedback
            else:
                feedback = {"bias": self.prediction_bias["curve"], "errors": self.prediction_errors["curve"]}
            feedback["errors"] = (feedback["errors"] + [error])[-20:]
            feedback["bias"] = max(-.5, min(.5, feedback["bias"] - .2 * error))
            if comparison.get("off_model") == "runtime":
                self.prediction_bias["curve"] = feedback["bias"]
                self.prediction_errors["curve"] = feedback["errors"]

    async def _cleanup_curves(self) -> None:
        try:
            await self.curve_store.cleanup()
            if isinstance(self.curve_store, CurveStore):
                self._restore_control_feedback()
            self._notify()
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
        if forecast.get("kind") == WEIGHTED_VERSION:
            return "weighted_residual"
        return "hybrid" if forecast.get("kind") == HYBRID_VERSION else "additive"

    def _prediction_confidence(self, model: str) -> tuple[float | None, int]:
        """Estimate validated confidence from recent confirmed Peak errors; related: storage.py."""
        feedback = {"additive": self.additive_feedback, "hybrid": self.hybrid_feedback}.get(model)
        if model == "weighted_residual" and isinstance(self.curve_store, CurveStore):
            errors = [row["errors"]["curve"] for row in self.curve_store.control_feedback
                      if row.get("forecast_kind") == WEIGHTED_VERSION and "curve" in row.get("errors", {})][-20:]
        else:
            errors = feedback["errors"] if feedback is not None else self.prediction_errors.get(model, [])
        count = len(errors)
        if not count:
            return None, 0
        mean_absolute_error = sum(abs(value) for value in errors) / count
        # Five comparisons fill the evidence window; errors around 0.5 C halve the score.
        return min(1.0, count / 5) * exp(-mean_absolute_error / 0.5), count

    @property
    def active_off_model(self) -> str | None:
        """Report the live OFF forecast's model; related: climate.py, diagnostics.py."""
        return (self.off_prediction or {}).get("off_model")

    def _hybrid_peak(self, now: float) -> tuple[dict | None, int]:
        """Estimate weighted residual heat from completed observations; related: peak_prediction.py."""
        cycle = self.curve_tracker.cycle
        if self.curve_store is None or cycle is None or cycle.invalid_reason not in (None, "AWAY_CYCLE"):
            return None, 0
        wall_now = time.time()
        try:
            return weighted_residual(
                self.curve_store.model.peak_samples, temperature=self.controller.temperature,
                minutes=max(0, (wall_now - cycle.started_at) / 60),
                outdoor=self._optional_temperature(self.outdoor_sensor, now), now=wall_now,
            )
        except Exception as err:
            _LOGGER.warning("Residual forecast unavailable (%s); retaining the base OFF decision", type(err).__name__)
            return None, 0

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
        target = self._predictive_off_target()
        if cycle.invalid_reason not in (None, "AWAY_CYCLE"):
            return None
        original_plan = self.runtime_off_plan
        runtime_decision = self._apply_runtime_off(decision, now, minimum_on, target, temperature, cycle, model)
        plan = self.runtime_off_plan
        if runtime_decision is not None and runtime_decision.state == "PREDICTIVE_OFF":
            plan = original_plan or self.heating_prediction_info

        # Keep the runtime cutoff authoritative; related: peak_prediction.py, storage.py and frontend/panel.js.
        forecast, comparable = self._hybrid_peak(now)
        planned_minutes = plan.get("planned_minutes") if plan else None
        at_cutoff = runtime_decision is not None and runtime_decision.state == "PREDICTIVE_OFF"
        control = {
            "action": "plan_off" if at_cutoff else "keep_plan" if plan else "base_control",
            "reason": None,
            "comparable_cycles": comparable,
            "planned_minutes": planned_minutes,
            "forecast_available": forecast is not None,
        }
        self.hybrid_control_support = control
        if forecast is None:
            control["reason"] = "INSUFFICIENT_HYBRID_EVIDENCE"
        elif plan is None:
            control["reason"] = "NO_RUNTIME_PLAN"
        elif not at_cutoff and (minimum_on or (now - self.actuator.changed_at) / 60 < max(
            (self.off_prediction or {}).get("runtime_control_minimum") or 0,
            (self.off_prediction or {}).get("shortfall_minimum") or 0,
        )):
            control["reason"] = "BELOW_MEASURED_RUNTIME"
        elif not at_cutoff and forecast["predicted_peak"] < target:
            control["reason"] = "BELOW_TARGET"
        elif not at_cutoff:
            control["action"] = "early_off"
            confidence, count = self._prediction_confidence("weighted_residual")
            self.off_prediction = {
                **(self.off_prediction or {}), "model": "curve", "off_model": "hybrid",
                "predicted_peak": forecast["predicted_peak"], "confidence": confidence,
                "comparison_count": count, "peak_forecast": forecast, "generated_at": time.time(),
                "runtime_extrapolated": False,
                "advance_minutes": (self._runtime_off_planned_at - now) / 60,
            }
            self.off_prediction.pop("off_learning_source", None)
            self.heating_prediction_info = {"model": "hybrid"}
            runtime_decision = Decision(False, "PREDICTIVE_OFF")

        if self.off_prediction is not None:
            if limits := self.off_prediction.get("runtime_shutdown_limits"):
                control["runtime_shutdown_limits"] = limits
            if plan and "runtime_shutdown_limits" in plan:
                control["reserved_runtime_shutdown_limits"] = plan["runtime_shutdown_limits"]
            self.off_prediction["off_control"] = control
            if forecast is not None and control["action"] != "early_off":
                self.off_prediction["residual_preview"] = forecast
        return runtime_decision

    # Use saving-only runtime support or a demand-scaled provisional floor; related: curve_storage.py.
    def _off_runtime_limits(self, model, outdoor, start, target):
        if not self.sunrise_saving_active:
            return {**model.runtime_shutdown_limits(outdoor, start, target), "mode": "regular",
                    "source": "regular"}
        sunrise = getattr(self.curve_store, "sunrise_runtime_model", None)
        if sunrise is not None and len(sunrise.runtime_samples) >= MIN_RUNTIME_SAMPLES:
            limits = sunrise.runtime_shutdown_limits(outdoor, start, target)
            if limits["minimum"] is not None:
                return {**limits, "mode": "sunrise_saving", "source": "sunrise_saving"}
        # Bootstrap only from the regular average, scaled by the lower temperature demand.
        regular_target = self.controller.target + self.controller.settings.hot_tolerance
        rise = regular_target - start
        ratio = min(1.0, max(0.0, (target - start) / rise)) if rise > 0 else 0.0
        samples = model.runtime_average_samples
        average = sum(samples) / len(samples) if samples else None
        provisional = .6 * average * ratio if average is not None else 0.0
        return {"mode": "sunrise_saving", "source": "scaled_regular_average" if samples else "configured_only",
                "minimum": provisional, "average_floor": provisional, "measured_minimum": None,
                "regular_average_minutes": average, "target_rise_ratio": ratio,
                "sample_count": len(sunrise.runtime_samples) if sunrise is not None else 0}

    def _runtime_limit_evidence(self, limits, model_minutes, shortfall, *, bracket=None):
        """Record which candidate determined the cutoff; related: curve_storage.py and panel.js."""
        configured = self.controller.settings.minimum_on_time / 60
        learned = limits["minimum"] if bracket is None or limits["mode"] == "sunrise_saving" else limits["average_floor"]
        candidates = {"configured_minimum": configured, "learned_minimum": learned or 0,
                      "shortfall": shortfall}
        if bracket is not None:
            candidates["long_term_bracket"] = bracket
        binding = max(candidates, key=candidates.get)
        minimum = candidates[binding]
        if binding == "learned_minimum":
            binding = ("average_floor" if learned == limits["average_floor"] else "measured_support")
        return {**limits, "model_minutes": model_minutes, "configured_minimum": configured,
                "shortfall_minimum": shortfall, "long_term_bracket_minimum": bracket,
                "applied_minimum": minimum, "binding_minimum": binding,
                "planned_by": binding if minimum > model_minutes else "model"}

    def _apply_runtime_off(self, decision: Decision, now: float, minimum_on: bool, target: float,
                           temperature: float, cycle: CurveCycle, model: CurveStandards) -> Decision | None:
        # Reserve the runtime cutoff even while minimum ON prevents an early stop.
        # Related: peak_prediction.py, storage.py, panel.js.
        elapsed_minutes = max(0.0, (now - self.actuator.changed_at) / 60)
        if self._runtime_off_started_at is not None and self._runtime_off_started_at != self.actuator.changed_at:
            self._clear_runtime_off_schedule()
        if (self._runtime_off_planned_at is not None
                and self.runtime_off_plan["off_learning_source"] == "long_term"):
            fallback = self._apply_long_term_runtime(decision, now, elapsed_minutes, minimum_on)
            if fallback is not None:
                return fallback
            self._clear_runtime_off_schedule()
        # Freeze available outdoor evidence with the plan; related: curve_learning.py and panel.js.
        reserved = (self.runtime_off_plan["planned_minutes"]
                    if self._runtime_off_started_at == self.actuator.changed_at
                    and self.runtime_off_plan is not None else None)
        outdoor = (self.runtime_off_plan.get("outdoor_temperature") if reserved is not None
                   else self._optional_temperature(self.outdoor_sensor, now))
        failure_minimum = 0.0 if self.sunrise_saving_active else model.shortfall_minimum(
            cycle.baseline, target, self.curve_tracker.heating_weather(time.time()), time.time())
        limits = self._off_runtime_limits(model, outdoor, cycle.baseline, target)
        limits["prediction_source"] = "regular_runtime"
        if limits["source"] == "sunrise_saving":
            sunrise_model = self.curve_store.sunrise_runtime_model
            sunrise_points = sunrise_model._runtime_curve(outdoor)
            if sunrise_points and 0 < target - cycle.baseline <= sunrise_points[-1][1]:
                model = sunrise_model
                limits["prediction_source"] = "sunrise_runtime"
        runtime_points = model._runtime_curve(outdoor) if len(model.runtime_samples) >= MIN_RUNTIME_SAMPLES else []
        required_minutes = model.required_heating_minutes(
            cycle.baseline, target, outdoor, points=runtime_points,
        )
        predicted_peak = model.predicted_peak(
            cycle.baseline, elapsed_minutes, outdoor, points=runtime_points,
        )
        if elapsed_minutes == 0 and runtime_points:
            predicted_peak = cycle.baseline
        # If a scheduled model loses support, return to thermostat control for this decision.
        # Related: peak_prediction.py, test_runtime.py.
        if self._runtime_off_planned_at is not None and (required_minutes is None or predicted_peak is None):
            fallback = self._apply_long_term_runtime(decision, now, elapsed_minutes, minimum_on)
            if fallback is None:
                self._clear_runtime_off_schedule()
            return fallback
        if required_minutes is not None and predicted_peak is not None:
            # The synthetic zero-minute anchor is display math, not observed shutdown support.
            # Related: curve_learning.py runtime_control_minimum and _runtime_cutoff_decision.
            samples = model.runtime_samples
            measured_minimum = min(row[0] for row in samples)
            # Keep the base allowance and earned correction separate from the average floor.
            # Related: curve_learning.py, curve_storage.py and frontend/panel.js.
            supported_minimum = limits["minimum"]
            limits = self._runtime_limit_evidence(limits, required_minutes, failure_minimum)
            minimum_minutes = limits["applied_minimum"]
            required_minutes = max(required_minutes, minimum_minutes)
            predicted_peak = max(temperature, predicted_peak)
            learned_cycles = len(model.runtime_samples)
            runtime_curve_range = ({
                "minimum_heating_minutes": measured_minimum,
                "maximum_heating_minutes": runtime_points[-1][0],
                "minimum_peak_rise": runtime_points[0][1],
                "maximum_peak_rise": runtime_points[-1][1],
            } if runtime_points else None)
            # Validate the reserved duration, so a late callback does not invalidate an otherwise supported stop.
            # Related: _runtime_cutoff_decision and test_runtime.py.
            has_reservation = reserved is not None
            scheduled_minutes = reserved if has_reservation else required_minutes
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
                "rise_start_delay_minutes": response[1] if (response := model._runtime_response(outdoor)) else None,
                "outdoor_bucket": heating_outdoor_bucket(outdoor),
                "outdoor_conditioned": heating_outdoor_bucket(outdoor) is not None and bool(model.runtime_sample_weather),
                "confidence": self._prediction_confidence("curve")[0],
                "comparison_count": self._prediction_confidence("curve")[1],
                "sample_coverage": min(1.0, learned_cycles / 5),
                "runtime_curve_range": runtime_curve_range,
                "runtime_extrapolated": runtime_outside_support or elapsed_above_range or elapsed_minutes < measured_minimum,
                "runtime_control_minimum": supported_minimum,
                "runtime_shutdown_limits": limits,
                "shortfall_minimum": failure_minimum,
                "memory_source": model.prediction_source, "current_weight": model.current_weight,
                # Anchor display-only OFF forecasts to their own sample and evidence; related: frontend/panel.js.
                "generated_at": time.time(), "temperature": temperature,
                "accepted_cycles": learned_cycles,
            }
            if runtime_outside_support:
                fallback = self._apply_long_term_runtime(decision, now, elapsed_minutes, minimum_on)
                if fallback is not None:
                    return fallback
            return self._runtime_cutoff_decision(
                decision, now, required_minutes, minimum_minutes, runtime_outside_support,
                source="global", outdoor_temperature=outdoor,
            )

        fallback = self._apply_long_term_runtime(decision, now, elapsed_minutes, minimum_on)
        return (decision if minimum_on else None) if fallback is None else fallback

    # Use only an observed Long-term bracket and the existing single cutoff; related: curve_learning.py.
    def _apply_long_term_runtime(self, decision: Decision, now: float,
                                 elapsed_minutes: float, minimum_on: bool) -> Decision | None:
        cycle = self.curve_tracker.cycle
        temperature = self.controller.temperature
        if (cycle is None or cycle.invalid_reason not in (None, "AWAY_CYCLE")
                or cycle.baseline is None or temperature is None):
            return None
        model = self.curve_store.model
        target = self._predictive_off_target()
        wall_now = time.time()
        rise = target - cycle.baseline
        outdoor = self.curve_tracker.heating_weather(wall_now)
        segment = model.long_term_runtime_segment(rise, outdoor, wall_now)
        if segment is None:
            return None
        group, (left, before), (right, after) = segment
        model_minutes = left + (right - left) * (rise - before) / (after - before)
        failure_minimum = 0.0 if self.sunrise_saving_active else model.shortfall_minimum(
            cycle.baseline, target, outdoor, wall_now)
        limits = self._off_runtime_limits(model, outdoor, cycle.baseline, target)
        limits["prediction_source"] = "long_term_runtime"
        limits = self._runtime_limit_evidence(limits, model_minutes, failure_minimum, bracket=left)
        required = max(model_minutes, limits["applied_minimum"])
        has_reservation = (self._runtime_off_started_at == self.actuator.changed_at
                           and self._runtime_off_planned_at is not None)
        scheduled = self.runtime_off_plan["planned_minutes"] if has_reservation else required
        if not left <= scheduled <= right or required > right or (elapsed_minutes > right and not has_reservation):
            return None
        if has_reservation and scheduled < required:
            return None
        # Show a live OFF prediction only at a supported runtime; retain the reserved value for late callbacks.
        estimate_minutes = scheduled if elapsed_minutes > right and has_reservation else elapsed_minutes
        predicted_peak = None
        if left <= estimate_minutes <= right:
            predicted_peak = cycle.baseline + before + (after - before) * (estimate_minutes - left) / (right - left)
            predicted_peak = max(temperature, predicted_peak)
        self.off_prediction = {
            "model": "curve", "off_model": "runtime", "off_learning_source": "long_term",
            "predicted_peak": predicted_peak,
            "required_heating_minutes": scheduled, "elapsed_heating_minutes": elapsed_minutes,
            "runtime_control_minimum": limits["applied_minimum"], "shortfall_minimum": failure_minimum,
            "runtime_shutdown_limits": limits,
            "runtime_curve_range": {"minimum_heating_minutes": left, "maximum_heating_minutes": right},
            "outdoor_bucket": heating_outdoor_bucket(outdoor),
            "outdoor_conditioned": group != "GLOBAL", "generated_at": wall_now, "temperature": temperature,
        }
        return self._runtime_cutoff_decision(
            decision, now, required, limits["applied_minimum"], False, source="long_term",
        )

    def _clear_runtime_off_schedule(self, *, completed: bool = False) -> None:
        """Discard a pending runtime cutoff after an external control change."""
        if self._runtime_off_planned_at is not None and not completed:
            self.heating_prediction_info = {"model": None}
        self._runtime_off_started_at = None
        self._runtime_off_planned_at = None
        self.runtime_off_plan = None

    # Reserve one stop per run and let the next cycle's learning adjust future plans.
    # Related: curve_learning.py, controller.py, frontend/panel.js.
    def _runtime_cutoff_decision(
        self, decision: Decision, now: float, required_minutes: float,
        minimum_minutes: float, runtime_extrapolated: bool, *, source: str | None = None,
        outdoor_temperature: float | None = None,
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
            self.runtime_off_plan = {
                "model": "runtime", "planned_minutes": required_minutes,
                "planned_off_at": time.time() + self._runtime_off_planned_at - now,
                "off_learning_source": source,
                "outdoor_temperature": outdoor_temperature,
            }
            if limits := (self.off_prediction or {}).get("runtime_shutdown_limits"):
                self.runtime_off_plan["runtime_shutdown_limits"] = limits
        if source is not None:
            self.runtime_off_plan["off_learning_source"] = source
        self.heating_prediction_info = dict(self.runtime_off_plan)
        planned_off_at = self._runtime_off_planned_at
        if now < planned_off_at:
            return Decision(True, "HEATING", planned_off_at)
        self._clear_runtime_off_schedule(completed=True)
        return Decision(False, "PREDICTIVE_OFF")

    def _predict_both_off(self, now: float, *, live_forecast: dict | None = None) -> dict:
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
        live = (self.off_prediction or {}) if live_forecast is None else live_forecast
        summed = live.get("peak_forecast")
        if summed is not None:
            self._off_peak_forecast = summed
            predictions["curve"] = summed["predicted_peak"]
            self._off_prediction_confidences["curve"] = self._prediction_confidence(self._feedback_model("curve", summed))[0]
            delay = self.curve_store.model.peak_delay_minutes(cycle.curve_type)
            if delay is not None:
                self._off_peak_deadlines["curve"] = now+delay*60
        # Only an actual reservation supplies a runtime comparison in AUTO; display estimates never delay ON.
        # Related: runtime.py _wait_after_off and storage.py.
        control = live.get("off_control")
        if ("curve" not in predictions and live.get("model") == "curve"
                and live.get("predicted_peak") is not None
                and (control is None or control["action"] == "plan_off")):
            predictions["curve"] = live["predicted_peak"]
            self._off_prediction_confidences["curve"] = live.get("confidence")
            delay = self.curve_store.model.peak_delay_minutes(cycle.curve_type) if self.curve_store and cycle else None
            if delay is not None and control is None:
                self._off_peak_deadlines["curve"] = now + delay * 60
        if ("curve" not in predictions
                and self.controller.mode != "auto"
                and self.curve_store is not None and cycle is not None
                and cycle.baseline is not None and cycle.invalid_reason in (None, "AWAY_CYCLE")):
            duration = max(0.0, (time.time() - cycle.started_at) / 60)
            peak = self.curve_store.model.predicted_peak(
                cycle.baseline, duration, self.curve_tracker.heating_weather(time.time())
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

    def _read_current_temperature(self, now: float, *, learn: bool = True) -> None:
        """Update the shared sensor snapshot only when its five-minute check is due."""
        if now < self._next_sensor_check:
            if self.controller.temperature is not None:
                self.controller.last_report = now
                if (self._learning_outage_deadline is None
                        and (self.climate_targets is None or self.actuator.pending is None)):
                    self.observation.advance_time(now)
            return
        self._next_sensor_check = now + BUCKET_SECONDS
        for entity_id in dict.fromkeys((self.sensor, *self._optional_sensors, self.sunrise_weather_entity)):
            if entity_id is None:
                continue
            state = self.hass.states.get(entity_id)
            valid = state is not None and state.state not in ("unavailable", "unknown") and (
                entity_id == self.sunrise_weather_entity or self._temperature(state) is not None
            )
            if valid:
                self._sensor_misses[entity_id] = 0
                self._sensor_samples[entity_id] = state
            else:
                misses = self._sensor_misses.get(entity_id, 0) + 1
                self._sensor_misses[entity_id] = min(misses, 2)
                if misses > 1:
                    self._sensor_samples[entity_id] = None
        indoor = self._sensor_samples.get(self.sensor)
        self.temperature_last_reported = getattr(indoor, "last_reported", None)
        temperature = self._temperature(indoor)
        self.controller.temperature = temperature
        self.controller.last_report = now if temperature is not None else None
        self.controller.sensor_fault = temperature is None
        self.controller.recovery_started = None
        if learn:
            wall_now = time.time()
            outdoor = self._optional_temperature(self.outdoor_sensor, now)
            if self.outdoor_sensor is not None:
                # The next sample supersedes this value, including on the second miss.
                # Allow one missed interval without gaps from timer dispatch jitter.
                self._defer_learning_report(
                    self.curve_tracker.report_cooling_outdoor, wall_now, outdoor, wall_now + 2 * BUCKET_SECONDS
                )
            if temperature is None:
                self.curve_tracker.invalidate("SENSOR_UNAVAILABLE")
                self._report_learning_temperature(None, now, wall_now, outdoor)
            else:
                self._defer_learning_report(self._report_learning_temperature, temperature, now, wall_now, outdoor)

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

    # Publish the actual ON evidence without changing the decision; related: curve_learning.py, climate.py, frontend/panel.js.
    def _predictive_start(self, decision: Decision, now: float) -> Decision:
        """Use continuous cooling evidence over the full response delay; related: climate.py, switch.py."""
        threshold = self.controller.target - self.controller.settings.cold_tolerance
        info = {"status": "insufficient_data", "predicted_minimum": None, "threshold": threshold}
        self.start_prediction = info
        if self.prediction_mode == "eco":
            info["status"] = "disabled"
            return decision
        # Basic threshold starts own their classification; predictive ON only advances an idle start.
        # Related: controller.py decide(), actuator.py command attribution and climate.py presets.
        if decision.state != "IDLE" or now < self._minimum_off_deadline():
            info["status"] = "blocked"
            return decision
        if self.curve_store is None or self._curve_storage_failed:
            info["reason"] = "learning_unavailable"
            return decision
        # Share the sample threshold with the ready sensor. Related: binary_sensor.py.
        model = self.curve_store.model
        count = self.predictive_activation_sample_count
        info.update(sample_count=count, required_runtime_samples=MIN_PREDICTIVE_ACTIVATION_SAMPLES)
        if count < MIN_PREDICTIVE_ACTIVATION_SAMPLES:
            info["reason"] = "insufficient_regular_samples"
            return decision
        if self.controller.temperature is None:
            info["reason"] = "temperature_unavailable"
            return decision
        if self.controller.temperature <= threshold:
            info["status"] = "blocked"
            return decision
        # Use only the confirmed Peak of this continuous OFF period; related: curve_learning.py.
        info["reason"] = "peak_unconfirmed"
        cycle = self.curve_tracker.cycle
        if cycle is not None:
            if cycle.invalid_reason:
                info["reason"] = "observation_invalid"
                return decision
            off_at, peak_at = cycle.off_at, cycle.peak_at
        else:
            off_at = self.curve_tracker.last_off_at
            comparison = self.last_peak_comparison or {}
            recorded_off = comparison.get("off_at")
            if off_at is None or recorded_off is None or abs(off_at - recorded_off) > 2:
                return decision
            peak_at = comparison.get("peak_at")
        wall_now = time.time()
        if off_at is None or peak_at is None or not off_at <= peak_at <= wall_now:
            return decision
        delay = model.response_delay_minutes("PREDICTIVE_WARM_HEATING")
        if delay is None:
            info["reason"] = "response_delay_missing"
            return decision
        # Use the lowest point of one fully covered curve; related: curve_learning.py, frontend/panel.js.
        elapsed = (wall_now - peak_at) / 60
        outdoor_group = model.outdoor_bucket(self._optional_temperature(self.outdoor_sensor, now))
        info.update(response_delay_minutes=delay, outdoor_group=outdoor_group)
        curve_weights = []
        prediction = (model.predict_cooling(
            elapsed, delay, outdoor_group=outdoor_group, lowest=True, curve_weights=curve_weights
        ) if outdoor_group is not None else None)
        source = "outdoor" if prediction is not None else "global"
        if prediction is None:
            prediction = model.predict_cooling(elapsed, delay, lowest=True, curve_weights=curve_weights)
        if prediction is None:
            info["reason"] = "cooling_coverage_missing"
            return decision
        delta, layers = prediction
        minimum = self.controller.temperature + delta
        start = delta < 0 and minimum <= threshold
        info.pop("reason")
        info.update(predicted_minimum=minimum, cooling_delta=delta, cooling_source=source,
                    cooling_layers=list(layers), curve_weights=curve_weights,
                    status="no_cooling" if delta >= 0 else "start" if start else "waiting")
        return Decision(True, "PREDICTIVE_ON") if start else decision

    def prediction_readiness(self, key: str) -> tuple[bool, dict]:
        """Report learned support independently of live control gates; related: binary_sensor.py."""
        attributes = {"available_models": []}
        if key == "predictive_on_ready":
            attributes.update(sample_count=0, required_runtime_samples=MIN_PREDICTIVE_ACTIVATION_SAMPLES)
        if self._learning_reset_pending or self.curve_store is None or self._curve_storage_failed:
            attributes["reason"] = "learning_unavailable"
            return False, attributes

        now = time.time()
        model = self.curve_store.model
        if key == "predictive_on_ready":
            attributes["sample_count"] = len(model.runtime_samples)
            if len(model.runtime_samples) < MIN_PREDICTIVE_ACTIVATION_SAMPLES:
                attributes["reason"] = "insufficient_regular_samples"
                return False, attributes
            delay = model.response_delay_minutes("PREDICTIVE_WARM_HEATING")
            models = model.cooling_ready_models(delay, now) if delay is not None else []
            attributes["response_delay_minutes"] = delay
            reason = "response_delay_missing" if delay is None else "cooling_coverage_missing"
        else:
            # Saving can bootstrap from regular forecasts until its own runtime model is ready.
            # Related: _off_runtime_limits() and binary_sensor.py.
            models = model.off_ready_models(now)
            confidence, comparisons = self._prediction_confidence("curve")
            attributes.update(
                sample_count=len(model.runtime_samples),
                required_runtime_samples=MIN_RUNTIME_SAMPLES,
                confidence=confidence, comparison_count=comparisons,
            )
            if key == "sunrise_saving_ready":
                attributes["required_runtime_samples"] = MIN_PREDICTIVE_ACTIVATION_SAMPLES
                sunrise = getattr(self.curve_store, "sunrise_runtime_model", None)
                sunrise_models = sunrise.off_ready_models(now) if sunrise is not None else []
                attributes["sunrise_sample_count"] = len(sunrise.runtime_samples) if sunrise is not None else 0
                if "runtime" in sunrise_models:
                    models = ["sunrise_runtime", *models]
                if not self._sunrise_ready(now):
                    attributes.update(available_models=models, reason=(
                        "insufficient_regular_samples"
                        if len(model.runtime_samples) < MIN_PREDICTIVE_ACTIVATION_SAMPLES
                        else "off_model_support_missing"
                    ))
                    return False, attributes
            reason = "off_model_support_missing"
        attributes.update(available_models=models, reason=None if models else reason)
        return bool(models), attributes

    def snapshot(self) -> dict:
        return {
            "schema_version": 1, "target": self.controller.target,
            "mode": self.controller.mode, "faults": sorted(self.controller.faults),
            "auto_external_override": self.controller.auto_external_override,
            "preset": self.preset,
            "prediction_mode": self.prediction_mode,
            "sunrise_enabled": self.sunrise_enabled,
            "sunrise_start": self.sunrise_start.isoformat(),
            "sunrise_end": self.sunrise_end.isoformat(),
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
                and self.actuator.pending is None and not self.actuator.busy
                and (self.climate_targets is None or self._climate_confirmed(False)))

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

    # Run historical recovery on demand; related: curve_api.py, peak_migration.py, curve_storage.py.
    async def async_migrate_learning(self) -> dict:
        from .peak_migration import migrate_heater_outages, migrate_outdoor_weather, migrate_timeouts

        if self._learning_reset_lock.locked():
            return {"status": "busy"}
        async with self._learning_reset_lock:
            store = self.curve_store
            if (not self.started or self._closed or self.controller.stopping
                    or self._learning_reset_pending or not isinstance(store, CurveStore)):
                return {"status": "unavailable"}

            result = {"status": "complete"}
            peak = {"status": "complete", "recovered": 0}
            result["peak"] = peak
            try:
                peak["recovered"] = await migrate_timeouts(self.hass, store, self.sensor, report=peak)
            except Exception:
                peak["status"] = "failed"
                _LOGGER.exception("Peak timeout migration failed")

            # Recover a disjoint set of source rows under the same lock; related: peak_migration.py.
            outage = {"status": "complete", "recovered": 0, "skipped": 0}
            result["heater_outage"] = outage
            try:
                outage["recovered"] = await migrate_heater_outages(
                    self.hass, store, self.sensor, self.heaters, self._heater_state,
                    self.controller.settings.heater_unavailable_grace, self.peak_observation_seconds, outage,
                )
            except Exception:
                outage["status"] = "failed"
                _LOGGER.exception("Heater outage migration failed")

            schedule = {
                "start": self.sunrise_start.isoformat(),
                "end": self.sunrise_end.isoformat(),
                "time_zone": getattr(getattr(self.hass, "config", None), "time_zone", None) or "UTC",
            }
            for key, cooling in (("sunrise_heating", False), ("sunrise_cooling", True)):
                if not self.sunrise_enabled or self.sunrise_start == self.sunrise_end:
                    result[key] = {"status": "sunrise_disabled", "recovered": 0}
                    continue
                try:
                    count = await store.migrate_sunrise_saving(self._sunrise_window, schedule, cooling=cooling)
                    result[key] = {"status": "complete", "recovered": count}
                    _LOGGER.info("Learning migration %s recovered %s cycles", key, count)
                except Exception:
                    result[key] = {"status": "failed", "recovered": 0}
                    _LOGGER.exception("Learning migration %s failed", key)
            try:
                result["outdoor_weather"] = await migrate_outdoor_weather(
                    self.hass, store, self.outdoor_sensor, self.controller.settings.sensor_timeout,
                )
            except Exception:
                result["outdoor_weather"] = {"status": "failed", "recovered": 0}
                _LOGGER.exception("Outdoor weather migration failed")
            self._restore_control_feedback()
            self._notify()
            return result

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
        self._learning_outage_deadline = None
        self._learning_outage_reports.clear()
        self.observation = ThermalObservation(max_report_gap=self.controller.settings.sensor_timeout,
                                              max_peak_wait_seconds=self.peak_observation_seconds)
        self.observation.seed_heater(self.actuator.observed)
        self.curve_tracker = CurveTracker(
            self.peak_observation_seconds, minimum_heating_seconds=self.controller.settings.minimum_on_time,
        )
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
        self._off_diagnostic_at = self._off_peak_forecast = None
        self._predictive_stop = None
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
            # A changed threshold invalidates the measured hold; related: curve_learning.py.
            if self.actuator.observed is not False:
                self.curve_tracker.invalidate_hold("MANUAL_TARGET_CHANGE")
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
        if mode != previous_mode:
            self._recovery_overrides.clear()
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
            if self.actuator.observed is not False:
                self.curve_tracker.invalidate_hold("MANUAL_TARGET_CHANGE")
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
        cycle = self.curve_tracker.cycle
        self.observation.max_peak_wait_seconds = (
            float("inf") if cycle and (cycle.sunrise_peak_excess or {}).get("ends_observation")
            else self.peak_observation_seconds
        )
        self.curve_tracker.max_peak_wait_seconds = self.peak_observation_seconds
        if self.curve_store is not None:
            self.curve_store.max_peak_wait_minutes = hours * 60
            self.curve_store.model.max_peak_wait_minutes = hours * 60
        self.evaluate()
        self._notify()

    # Preserve only the cooldown created during unload; related: __init__.py, actuator.py.
    async def async_stop_for_reload(self) -> bool:
        """Pass an unload-triggered stop to the next owner of the same heater."""
        if not self.started or self._closed:
            return True
        already_off = self.actuator.observed is False and self.actuator.pending is not True
        if not await self.async_stop():
            return False
        reload_off = self.hass.data.setdefault(DOMAIN, {}).setdefault("reload_off", {})
        for heater in self.heaters:
            if already_off:
                reload_off.setdefault(heater, None)
            else:
                reload_off[heater] = (self.actuator.changed_at, self._minimum_off_deadline())
        return True

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
        if (self.actuator.observed is not False or self.actuator.busy
                or (self.climate_targets is not None and not self._climate_confirmed(False))):
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
