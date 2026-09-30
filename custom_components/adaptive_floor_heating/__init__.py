"""Own thermostat runtimes and heater claims; related: runtime.py, climate.py and sensor.py."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    CONF_HEATER, CONF_MODE, CONF_ROOM_COUNT, CONF_TEMPERATURE_SENSOR, CONF_ZONES,
    DOMAIN, MODE_MULTI_ZONE, MODE_MULTI_ZONE_INTEGRATED, MODE_STANDALONE,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reserve all entry heaters before forwarding Climate and diagnostics."""
    from homeassistant.const import Platform
    from homeassistant.exceptions import ConfigEntryError

    from .controller import Settings
    from .runtime import HeatingRuntime

    if entry.data.get(CONF_MODE) == MODE_MULTI_ZONE:
        # Preserve legacy entries; their per-room sensors cannot imply one shared sensor.
        entry.runtime_data = None
        return True
    mode = entry.data.get(CONF_MODE)
    if mode not in (MODE_STANDALONE, MODE_MULTI_ZONE_INTEGRATED):
        raise ConfigEntryError("Unknown heating control mode")
    if mode == MODE_MULTI_ZONE_INTEGRATED:
        zones = entry.data.get(CONF_ZONES)
        if not isinstance(zones, list) or any(not isinstance(zone, dict) for zone in zones):
            raise ConfigEntryError("Invalid integrated multi-room configuration")
        heaters = [zone.get(CONF_HEATER) for zone in zones]
    else:
        heaters = [entry.data.get(CONF_HEATER)]
    sensor = entry.data.get(CONF_TEMPERATURE_SENSOR)
    if mode == MODE_MULTI_ZONE_INTEGRATED and (
        len(heaters) != entry.data.get(CONF_ROOM_COUNT) or len(heaters) < 2
    ):
        raise ConfigEntryError("Invalid integrated multi-room configuration")
    if (not heaters
            or any(not isinstance(heater, str) or not heater.startswith("switch.") for heater in heaters)
            or len(set(heaters)) != len(heaters)
            or not isinstance(sensor, str) or not sensor.startswith("sensor.")):
        raise ConfigEntryError("Invalid heater or temperature sensor configuration")
    owners = hass.data.setdefault(DOMAIN, {}).setdefault("owners", {})
    if any(heater in owners and owners[heater] != entry.entry_id for heater in heaters):
        raise ConfigEntryError("This heater is already owned by another thermostat")
    try:
        settings = Settings.from_options(dict(entry.options))
    except ValueError as err:
        raise ConfigEntryError(f"Invalid control setting: {err}") from err
    previous = getattr(entry, "runtime_data", None)
    if previous is not None and previous.started:
        if not await previous.async_stop():
            raise ConfigEntryError("The previous runtime has not confirmed heater OFF")
    for heater in heaters:
        owners[heater] = entry.entry_id
    runtime = entry.runtime_data = HeatingRuntime(hass, entry, settings)
    try:
        await hass.config_entries.async_forward_entry_setups(
            entry, [Platform.CLIMATE, Platform.SENSOR, Platform.SELECT]
        )
        # Keep all room prefixes current when the second entry is added.
        from .entity_naming import update_registered_ids
        update_registered_ids(hass)
    except BaseException:
        if await runtime.async_stop():
            for heater in heaters:
                owners.pop(heater, None)
        raise
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Confirm OFF before unloading entities or releasing exclusive ownership."""
    from homeassistant.const import Platform

    runtime = entry.runtime_data
    if runtime is None:
        return True
    if not await runtime.async_stop():
        return False
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, [Platform.CLIMATE, Platform.SENSOR, Platform.SELECT]
    )
    if unloaded:
        for heater in runtime.heaters:
            hass.data[DOMAIN]["owners"].pop(heater, None)
    else:
        await runtime.async_start()
    return unloaded


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply validated settings through a safe unload/reload boundary."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove persisted intent only when the user removes the config entry."""
    from .storage import RuntimeStore, ThermalLearningStore
    from .curve_storage import CurveStore

    await RuntimeStore(hass, entry.entry_id).remove()
    await ThermalLearningStore(hass, entry.entry_id).remove()
    if callable(getattr(getattr(hass, "config", None), "path", None)):
        await CurveStore(hass, entry.entry_id).remove()
