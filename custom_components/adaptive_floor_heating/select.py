"""Expose predictive start policy; related: runtime.py, const.py, storage.py."""

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, NAME, OFF_MODELS, PREDICTION_MODES
from .entity_naming import entity_id


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Register the OFF model setting and retire superseded selectors."""
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.domain == "select" and registered.unique_id in (
            f"{entry.entry_id}_learning_model", f"{entry.entry_id}_prediction_mode",
        ):
            registry.async_remove(registered.entity_id)
    async_add_entities([OffModelSelect(entry, entry.runtime_data)])


class OffModelSelect(SelectEntity):
    """Choose which predictive OFF algorithm controls the next cycle; related: runtime.py, panel.js."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "off_model"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(OFF_MODELS)

    def __init__(self, entry, runtime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_off_model"
        self.entity_id = entity_id(runtime.hass, entry, "select", "off_model")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title or NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.subscribe(self.async_write_ha_state))
        await self._runtime.async_start()

    @property
    def current_option(self) -> str:
        return self._runtime.off_model

    async def async_select_option(self, option: str) -> None:
        try:
            self._runtime.set_off_model(option)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err


class PredictionModeSelect(SelectEntity):
    """Choose how early AUTO starts heating while retaining predictive stop."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "prediction_mode"
    _attr_options = list(PREDICTION_MODES)

    def __init__(self, entry, runtime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_prediction_mode"
        self.entity_id = entity_id(runtime.hass, entry, "select", "prediction_mode")
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
    def current_option(self) -> str:
        return self._runtime.prediction_mode

    async def async_select_option(self, option: str) -> None:
        try:
            self._runtime.set_prediction_mode(option)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
