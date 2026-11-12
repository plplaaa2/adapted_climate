"""Expose the saving schedule's local times; related: runtime.py, switch.py, storage.py."""

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, NAME
from .entity_naming import entity_id


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([
        SunriseTime(entry, entry.runtime_data, "start"),
        SunriseTime(entry, entry.runtime_data, "end"),
    ])


class SunriseTime(TimeEntity):
    """Edit a daily schedule boundary; related: runtime.py, storage.py."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry, runtime, boundary: str) -> None:
        self._runtime = runtime
        self._boundary = boundary
        key = f"sunrise_{boundary}"
        self._attr_translation_key = key
        self._attr_icon = "mdi:clock-start" if boundary == "start" else "mdi:clock-end"
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self.entity_id = entity_id(runtime.hass, entry, "time", key)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title or NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.subscribe(self.async_write_ha_state))
        await self._runtime.async_start()

    @property
    def native_value(self) -> time:
        return getattr(self._runtime, f"sunrise_{self._boundary}")

    async def async_set_value(self, value: time) -> None:
        self._runtime.set_sunrise_schedule(**{self._boundary: value})
