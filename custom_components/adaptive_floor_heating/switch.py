"""Expose predictive ON while retaining legacy intent values; related: runtime.py, storage.py, select.py."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, NAME
from .entity_naming import entity_id


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Register the replacement predictive-start switch; related: __init__.py."""
    async_add_entities([
        PredictionOnSwitch(entry, entry.runtime_data),
        SunriseSavingSwitch(entry, entry.runtime_data),
    ])


class PredictionOnSwitch(SwitchEntity):
    """Map legacy eco to OFF and balanced/comfort to ON without rewriting history."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "예측 ON"
    _attr_icon = "mdi:crystal-ball"

    def __init__(self, entry, runtime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_prediction_on"
        self.entity_id = entity_id(runtime.hass, entry, "switch", "prediction_on")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title or NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.subscribe(self.async_write_ha_state))
        await self._runtime.async_start()

    @property
    def is_on(self) -> bool:
        return self._runtime.prediction_mode != "eco"

    async def async_turn_on(self, **kwargs) -> None:
        self._runtime.set_prediction_mode("comfort")

    async def async_turn_off(self, **kwargs) -> None:
        self._runtime.set_prediction_mode("eco")


class SunriseSavingSwitch(SwitchEntity):
    """Toggle the daily saving schedule; related: runtime.py, time.py."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "sunrise_saving"
    _attr_icon = "mdi:weather-sunset-up"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry, runtime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_sunrise_saving"
        self.entity_id = entity_id(runtime.hass, entry, "switch", "sunrise_saving")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title or NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.subscribe(self.async_write_ha_state))
        await self._runtime.async_start()

    @property
    def is_on(self) -> bool:
        return self._runtime.sunrise_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self._runtime.set_sunrise_schedule(enabled=True)

    async def async_turn_off(self, **kwargs) -> None:
        self._runtime.set_sunrise_schedule(enabled=False)
