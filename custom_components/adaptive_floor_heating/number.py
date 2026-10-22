"""Expose per-entry peak observation settings; related: runtime.py and const.py."""

from math import isfinite

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import (
    CONF_PEAK_OBSERVATION_HOURS,
    DOMAIN,
    MAX_PEAK_OBSERVATION_HOURS,
    MIN_PEAK_OBSERVATION_HOURS,
    NAME,
)
from .entity_naming import entity_id


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Create the per-thermostat peak observation setting."""
    async_add_entities([PeakObservationHoursNumber(entry, entry.runtime_data)])


class PeakObservationHoursNumber(NumberEntity):
    """Choose how long learning waits for the measured post-OFF peak."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "peak_observation_hours"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = MIN_PEAK_OBSERVATION_HOURS
    _attr_native_max_value = MAX_PEAK_OBSERVATION_HOURS
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_mode = NumberMode.BOX

    def __init__(self, entry, runtime) -> None:
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_peak_observation_hours"
        self.entity_id = entity_id(runtime.hass, entry, "number", "peak_observation_hours")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.subscribe(self.async_write_ha_state))
        await self._runtime.async_start()

    @property
    def native_value(self) -> int:
        return self._runtime.controller.settings.peak_observation_hours

    async def async_set_native_value(self, value: float) -> None:
        """Apply live and persist the selected integer hour value."""
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not isfinite(value) or int(value) != value):
            raise ServiceValidationError("Peak observation must be a whole number of hours")
        hours = int(value)
        if not MIN_PEAK_OBSERVATION_HOURS <= hours <= MAX_PEAK_OBSERVATION_HOURS:
            raise ServiceValidationError("Peak observation must be between 1 and 12 hours")
        try:
            self._runtime.set_peak_observation_hours(hours)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={
                **self._entry.options,
                CONF_PEAK_OBSERVATION_HOURS: hours,
            },
        )
