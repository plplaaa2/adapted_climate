"""Expose per-entry learning reset; related: runtime.py, curve_storage.py."""

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, NAME
from .entity_naming import entity_id


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Register the reset button alongside the entry's climate entity."""
    async_add_entities([LearningResetButton(entry, entry.runtime_data)])


class LearningResetButton(ButtonEntity):
    """Clear learned data only when the heater is confirmed off."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "learning_reset"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restart"

    def __init__(self, entry, runtime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_learning_reset"
        self.entity_id = entity_id(runtime.hass, entry, "button", "learning_reset")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title or NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.subscribe(self.async_write_ha_state))
        await self._runtime.async_start()

    @property
    def available(self) -> bool:
        return self._runtime.can_reset_learning

    async def async_press(self) -> None:
        try:
            await self._runtime.async_reset_learning()
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except Exception as err:
            raise HomeAssistantError("Could not complete learning reset. Check the integration log and retry.") from err
