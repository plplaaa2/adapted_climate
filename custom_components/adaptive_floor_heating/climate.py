"""Expose standalone thermostat intent; related: runtime.py and controller.py."""

from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, MAX_TARGET, MIN_TARGET, NAME
from .entity_naming import entity_id


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Create one thermostat for the standalone runtime assigned to this entry."""
    async_add_entities([AdaptiveFloorHeatingClimate(entry, entry.runtime_data)])


class AdaptiveFloorHeatingClimate(ClimateEntity):
    """A non-polling thermostat driven by validated reports and monotonic timers."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
    _attr_preset_modes = ["home", "away"]
    _attr_min_temp = MIN_TARGET
    _attr_max_temp = MAX_TARGET
    _attr_target_temperature_step = 0.1
    _attr_precision = 0.1
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.PRESET_MODE
    )

    def __init__(self, entry, runtime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_climate"
        self.entity_id = entity_id(runtime.hass, entry, "climate", "")
        # Register the software thermostat; related: const.py and config_flow.py.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.subscribe(self.async_write_ha_state))
        await self._runtime.async_start()

    async def async_will_remove_from_hass(self) -> None:
        # Entity disable/removal also stops control, even without entry unloading.
        await self._runtime.async_stop()
        await super().async_will_remove_from_hass()

    @property
    def available(self) -> bool:
        model = self._runtime.controller
        return (self._runtime.started and not model.sensor_fault
                and model.temperature is not None
                and self._runtime.actuator.observed is not None)

    @property
    def current_temperature(self) -> float | None:
        return self._runtime.controller.temperature

    @property
    def target_temperature(self) -> float:
        return self._runtime.controller.target

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode(self._runtime.controller.mode)

    @property
    def hvac_action(self) -> HVACAction | None:
        observed = self._runtime.actuator.observed
        if not self.available or (self.hvac_mode == HVACMode.OFF and observed):
            return None
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.HEATING if observed else HVACAction.IDLE

    @property
    def preset_mode(self) -> str:
        return self._runtime.preset

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        runtime = self._runtime
        faults = sorted(runtime.controller.faults)
        if runtime.controller.sensor_fault:
            faults.append("sensor_fault")
        return {
            "control_state": runtime.decision.state,
            "faults": faults,
            "heater_entity_id": runtime.heater,
            "heater_entity_ids": list(runtime.heaters),
            "temperature_sensor_entity_id": runtime.sensor,
            "heater_confirmed_on": runtime.actuator.observed,
            "heater_command_pending": runtime.actuator.pending,
            "learning_model": runtime.learning_model,
            "curve_fallback_reason": runtime.curve_fallback_reason,
            # Selected prediction and independent result comparison; related: runtime.py.
            "off_prediction": runtime.off_prediction,
            "last_off_prediction": runtime.last_off_prediction,
            "last_peak_comparison": runtime.last_peak_comparison,
            "cold_return_pending": runtime.curve_tracker.away_return_pending,
            "curve_memory": (
                {curve: memory.diagnostics() for curve, memory in runtime.curve_store.model.memory.items()}
                if runtime.curve_store is not None else {}
            ),
            "curve_learning_counts": (
                dict(runtime.curve_store.model.accepted) if runtime.curve_store is not None else {}
            ),
            "curve_rejected_counts": (
                dict(runtime.curve_store.model.rejected) if runtime.curve_store is not None else {}
            ),
            "curve_last_quality_reason": (
                runtime.curve_store.model.last_reason if runtime.curve_store is not None else None
            ),
            "curve_phase": (
                "idle" if runtime.curve_tracker.cycle is None else
                "cooling" if runtime.curve_tracker.cycle.peak_at is not None else
                "post_heat_rise" if runtime.curve_tracker.cycle.off_at is not None else "heating"
            ),
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        try:
            self._runtime.set_target(kwargs.get(ATTR_TEMPERATURE))
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        try:
            self._runtime.set_mode(hvac_mode)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        try:
            self._runtime.set_preset(preset_mode)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
