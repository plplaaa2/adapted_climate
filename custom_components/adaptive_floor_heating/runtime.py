"""Own events, timers, and safe lifecycle; related: climate.py, actuator.py, storage.py, thermal_model.py."""

import asyncio
from collections.abc import Callable
import logging

from .actuator import SwitchActuator
from .const import CONF_HEATER, CONF_TEMPERATURE_SENSOR
from .controller import Decision, Settings, ThermostatController, temperature_celsius
from .history import RESPONSE_SLOPE_THRESHOLD, ThermalObservation
from .storage import RuntimeStore, ThermalLearningStore
from .thermal_model import ThermalLearningModel

_LOGGER = logging.getLogger(__name__)


class HeatingRuntime:
    """Run one standalone thermostat on HA's event loop with one actuator owner."""

    def __init__(self, hass, entry, settings: Settings) -> None:
        self.hass, self.entry = hass, entry
        self.heater = entry.data[CONF_HEATER]
        self.sensor = entry.data[CONF_TEMPERATURE_SENSOR]
        self.controller = ThermostatController(settings)
        self.observation = ThermalObservation()
        self.store = RuntimeStore(hass, entry.entry_id)
        self.learning_store = ThermalLearningStore(hass, entry.entry_id)
        self.thermal_model = ThermalLearningModel()
        self.preset = "home"
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
        self.observation = ThermalObservation()
        self.thermal_model = ThermalLearningModel()
        try:
            learned = await self.learning_store.load()
            if learned is not None:
                self.thermal_model = ThermalLearningModel.from_snapshot(learned)
        except Exception as err:
            _LOGGER.warning("Thermal model could not be restored (%s); restarting learning", type(err).__name__)
        self.controller.target, self.controller.mode = data["target"], data["mode"]
        self.preset = data["preset"]
        self.controller.faults = set(data["faults"])
        self._closed = False
        self.started = True

        @callback
        def event_filter(data):
            return data.get("entity_id") in (self.heater, self.sensor)

        @callback
        def event_received(event):
            self._state_event(event.data)

        # Both changed and unchanged reports matter; removal uses state_changed.
        for event_type in ("state_changed", "state_reported"):
            self._unsubs.append(self.hass.bus.async_listen(
                event_type, event_received, event_filter=event_filter
            ))
        self._unsubs.append(self.hass.bus.async_listen_once(
            "homeassistant_stop", self._shutdown
        ))
        state = self.hass.states.get(self.heater)
        observed = self._switch_state(state)
        self.actuator.observe(observed, initial=True)
        self.observation.seed_heater(observed)
        self.controller.startup_off_seen = observed is False
        if observed is None:
            self._fault("heater_unavailable")
        initial_temperature = self._temperature(self.hass.states.get(self.sensor))
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
        state = data.get("new_state")
        if data["entity_id"] == self.sensor:
            now = self.hass.loop.time()
            temperature = self._temperature(state)
            self.controller.report_temperature(temperature, now)
            completed_cycles = self.observation.completed_cycles
            self.observation.report_temperature(temperature, now)
            if self.observation.completed_cycles > completed_cycles:
                self.thermal_model.add_cycle(self.observation.last_cycle)
                self.learning_store.schedule(self.thermal_model.snapshot())
        else:
            previous = self.actuator.observed
            observed = self._switch_state(state)
            external = self.actuator.observe(observed)
            now = self.hass.loop.time()
            temperature = self.controller.temperature
            if (self.controller.last_report is None
                    or now - self.controller.last_report >= self.controller.settings.sensor_timeout):
                temperature = None
            if external and self.controller.mode != "off":
                self.observation.invalidate_cycle(observed)
            else:
                self.observation.observe_heater(observed, now, temperature)
            if observed is None:
                self._fault("heater_unavailable")
            elif observed is False:
                self.controller.startup_off_seen = True
                self._off_transition_pending = False
            if external and self.controller.mode != "off":
                self.controller.mode = "off"
                self._fault("external_override")
            elif external and observed is True and self._off_transition_pending:
                self._fault("external_override")
                self.actuator.request(False, retry=True, force=True)
            if previous is None and observed is not None and self.controller.mode != "off":
                self.actuator.request(False, retry=True)
        self.evaluate()

    async def _send(self, heating: bool) -> None:
        """Send only switch actions; the actuator separately confirms HA state."""
        await self.hass.services.async_call(
            "switch", "turn_on" if heating else "turn_off",
            {"entity_id": self.heater}, blocking=True,
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
        had_fresh_temperature = self.controller.last_report is not None
        decision = self.controller.decide(now, self.actuator.observed, self.actuator.changed_at)
        if had_fresh_temperature and self.controller.last_report is None:
            # An expired report breaks observational continuity; it cannot be
            # combined with a later sample to infer a thermal response.
            self.observation.report_temperature(None, now)
        if self.controller.mode == "auto":
            decision = self._apply_learned_prediction(decision, now)
        if decision.state != self.decision.state:
            _LOGGER.info("%s control state: %s", self.heater, decision.state)
        self.decision = decision
        if self.controller.mode != "off":
            self.actuator.request(decision.heating)
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        deadlines = []
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
        if self.controller.last_report is not None:
            deadlines.append(self.controller.last_report + self.controller.settings.sensor_timeout)
        if deadlines:
            self._timer = self.hass.loop.call_later(max(0.001, min(deadlines) - now), self.evaluate)
        self.store.schedule(self.snapshot())
        self._notify()

    def _apply_learned_prediction(self, decision: Decision, now: float) -> Decision:
        """Apply learned early-start and residual cutoff inside timer/safety bounds."""
        temperature = self.controller.temperature
        if temperature is None:
            return decision

        if (self.actuator.observed is False and decision.state in ("IDLE", "HEATING")
                and now - self.actuator.changed_at >= self.controller.settings.minimum_off_time):
            response_delay = self.thermal_model.metrics["heating_response_delay"]["mean"]
            response_confidence = self.thermal_model.confidence("heating_response_delay")
            cooling_slope = self.observation.temperature_slope
            if (response_delay is not None and response_confidence > 0
                    and cooling_slope is not None
                    and cooling_slope < -RESPONSE_SLOPE_THRESHOLD):
                effective_delay_hours = response_delay * response_confidence / 60
                predicted_at_response = temperature + cooling_slope * effective_delay_hours
                if predicted_at_response <= self.controller.target:
                    return Decision(True, "PREDICTIVE_ON")

        if decision.state != "HEATING":
            return decision
        if self.actuator.observed is False:
            estimate = self.thermal_model.metrics["residual_rise"]["mean"]
            confidence = self.thermal_model.confidence("residual_rise")
            if estimate is not None and confidence > 0:
                predicted_peak = temperature + estimate * confidence
                if predicted_peak >= self.controller.target:
                    return Decision(False, "PREDICTIVE_WAIT")
            return decision
        if (self.actuator.observed is not True
                or now - self.actuator.changed_at < self.controller.settings.minimum_on_time):
            return decision
        estimate = self.thermal_model.metrics["residual_rise"]["mean"]
        confidence = self.thermal_model.confidence("residual_rise")
        if estimate is None or confidence <= 0:
            return decision
        predicted_peak = temperature + estimate * confidence
        if predicted_peak < self.controller.target:
            return decision
        return Decision(False, "PREDICTIVE_OFF")

    def snapshot(self) -> dict:
        return {
            "schema_version": 1, "target": self.controller.target,
            "mode": self.controller.mode, "faults": sorted(self.controller.faults),
            "preset": self.preset,
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
        self.controller.set_target(value)
        self.evaluate()

    def set_mode(self, mode: str) -> None:
        if self.controller.stopping:
            raise ValueError("The integration is stopping; retry after reload")
        previous_mode = self.controller.mode
        self.controller.set_mode(mode, self.hass.loop.time(), self.actuator.observed, self.actuator.changed_at)
        if mode == "off":
            self._off_transition_pending = (
                self.actuator.observed is True
                or (self.actuator.busy and self.actuator.desired)
            )
            self.actuator.request(False, retry=True)
        elif (mode == "auto" or previous_mode == "off") and self.actuator.observed is not False:
            self.controller.startup_off_seen = False
            self.observation.invalidate_cycle(self.actuator.observed)
            self.actuator.request(False, retry=True)
        _LOGGER.info("%s requested HVAC mode: %s", self.heater, mode)
        self.evaluate()

    def set_preset(self, preset: str) -> None:
        """Select HOME/AWAY target using configured preset temperatures."""
        if preset not in ("home", "away"):
            raise ValueError("Preset must be home or away")
        if self.controller.stopping:
            raise ValueError("The integration is stopping; retry after reload")
        if self.preset == preset:
            return
        self.preset = preset
        self.controller.set_target(
            self.controller.settings.home_temperature if preset == "home"
            else self.controller.settings.away_temperature
        )
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
                await self._persist()
            finally:
                await self._close()
