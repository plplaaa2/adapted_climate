"""Build short entity IDs; related: __init__.py, climate.py, sensor.py, select.py."""

import logging

from .const import DOMAIN, MODE_MULTI_ZONE_INTEGRATED, MODE_STANDALONE

_LOGGER = logging.getLogger(__name__)
_PREFIX = "adaptive_heating_climate"
_MODES = (MODE_STANDALONE, MODE_MULTI_ZONE_INTEGRATED)


def active_entries(hass):
    """Keep room numbering in config entry creation order."""
    return [
        item for item in hass.config_entries.async_entries(DOMAIN)
        if item.data.get("mode") in _MODES
    ]


def entity_id(hass, entry, platform: str, key: str) -> str:
    """Suggest a compact ID without changing the stable unique ID."""
    entries = active_entries(hass)
    prefix = _PREFIX
    if len(entries) > 1:
        room_number = next(
            (n for n, item in enumerate(entries, 1) if item.entry_id == entry.entry_id),
            len(entries) + 1,
        )
        prefix = f"{prefix}_room_{room_number}"
    return f"{platform}.{prefix}" + (f"_{key}" if key else "")


def update_registered_ids(hass) -> None:
    """Rename the first room when another config entry joins; related: __init__.py."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for entry in active_entries(hass):
        for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
            prefix = f"{entry.entry_id}_"
            if not registered.unique_id.startswith(prefix) or registered.platform != DOMAIN:
                continue
            key = registered.unique_id[len(prefix):]
            if registered.domain == "climate" and key == "climate":
                key = ""
            elif registered.domain not in ("sensor", "select"):
                continue
            desired = entity_id(hass, entry, registered.domain, key)
            if registered.entity_id == desired:
                continue
            try:
                registry.async_update_entity(registered.entity_id, new_entity_id=desired)
            except ValueError:
                _LOGGER.warning("Entity ID %s is occupied; keeping %s", desired, registered.entity_id)
