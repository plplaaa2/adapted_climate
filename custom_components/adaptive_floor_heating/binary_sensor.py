"""Expose prediction learning readiness; related: runtime.py and curve_learning.py."""

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, NAME
from .entity_naming import entity_id


READY_KEYS = ("predictive_off_ready", "predictive_on_ready", "sunrise_saving_ready")
# Add pre_sunrise_heating_ready when its prediction model is implemented (#31).


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Register prediction learning diagnostics for this entry."""
    async_add_entities([PredictionReadySensor(entry, key) for key in READY_KEYS])


class PredictionReadySensor(BinarySensorEntity):
    """Report learned model support independently of active operation."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, key: str) -> None:
        self._runtime = entry.runtime_data
        self.entity_description = BinarySensorEntityDescription(
            key=key, translation_key=key, entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self.entity_id = entity_id(self._runtime.hass, entry, "binary_sensor", key)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title or NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.subscribe(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return self._runtime.started

    @property
    def is_on(self) -> bool:
        return self._runtime.prediction_readiness(self.entity_description.key)[0]

    @property
    def extra_state_attributes(self) -> dict:
        return self._runtime.prediction_readiness(self.entity_description.key)[1]
