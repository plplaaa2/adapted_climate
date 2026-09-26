"""Persist user intent and fault latches; related: controller.py, runtime.py."""

import logging
from math import isfinite
from typing import Any

from .const import DEFAULT_HOME_TEMPERATURE, DOMAIN, LATCHED_FAULTS, MAX_TARGET, MIN_TARGET

_LOGGER = logging.getLogger(__name__)


def decode_state(
    data: Any, default_target: float = DEFAULT_HOME_TEMPERATURE, *,
    home_temperature: float = DEFAULT_HOME_TEMPERATURE, away_temperature: float = 18.0,
) -> dict[str, Any]:
    """Reject malformed saved intent as a whole rather than restoring partial HEAT."""
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported runtime storage schema")
    target = data.get("target")
    faults = data.get("faults")
    # Migrate legacy active-control switch into the new AUTO HVAC mode.
    mode = data.get("mode")
    legacy_auto_control = data.get("auto_control", False)
    if not isinstance(legacy_auto_control, bool):
        raise ValueError("Invalid legacy auto-control state")
    if mode == "heat" and legacy_auto_control:
        mode = "auto"
    preset = data.get("preset", "home")
    preset_temperature = data.get("preset_temperature")
    if (isinstance(target, bool) or not isinstance(target, (int, float))
            or not isfinite(target) or not MIN_TARGET <= target <= MAX_TARGET
            or mode not in ("off", "heat", "auto")
            or not isinstance(faults, list)
            or preset not in ("home", "away")
            or (preset_temperature is not None and (
                isinstance(preset_temperature, bool)
                or not isinstance(preset_temperature, (int, float))
                or not isfinite(preset_temperature)
                or not MIN_TARGET <= preset_temperature <= MAX_TARGET
            ))
            or any(not isinstance(f, str) or f not in LATCHED_FAULTS for f in faults)):
        raise ValueError("Invalid saved runtime state")
    configured_preset_temperature = home_temperature if preset == "home" else away_temperature
    if preset_temperature is not None and target == preset_temperature:
        target = configured_preset_temperature
    return {
        "schema_version": 1, "target": float(target), "mode": mode,
        "preset": preset, "preset_temperature": configured_preset_temperature,
        "faults": faults[:],
    }


class RuntimeStore:
    """Keep thermostat intent separate from learned thermal data."""

    def __init__(self, hass, entry_id: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, 1, f"{DOMAIN}.{entry_id}.runtime")
        self._last: dict[str, Any] | None = None

    async def load(
        self, *, default_target: float = DEFAULT_HOME_TEMPERATURE,
        home_temperature: float = DEFAULT_HOME_TEMPERATURE, away_temperature: float = 18.0,
    ) -> dict[str, Any]:
        """Start OFF when no valid persisted state is available."""
        default = {
            "schema_version": 1, "target": default_target, "mode": "off",
            "preset": "home", "preset_temperature": home_temperature, "faults": [],
        }
        try:
            data = await self._store.async_load()
            return default if data is None else decode_state(
                data, default_target, home_temperature=home_temperature,
                away_temperature=away_temperature,
            )
        except Exception as err:
            _LOGGER.warning("Runtime storage could not be restored (%s); starting OFF", type(err).__name__)
            return default

    def schedule(self, data: dict[str, Any]) -> None:
        """Write only changed intent, never sensor measurements or monotonic clocks."""
        if data != self._last:
            self._last = data
            self._store.async_delay_save(lambda: data, 0)

    async def save(self, data: dict[str, Any]) -> None:
        """Flush the latest intent at a lifecycle boundary."""
        await self._store.async_save(data)
        self._last = data

    async def remove(self) -> None:
        """Remove this entry's runtime state after entry removal."""
        await self._store.async_remove()


class ThermalLearningStore:
    """Persist learned aggregates separately from the thermostat's runtime intent."""

    def __init__(self, hass, entry_id: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, 1, f"{DOMAIN}.{entry_id}.learning")
        self._last: dict[str, Any] | None = None

    async def load(self) -> Any:
        """Load the entry's independent learned thermal model."""
        return await self._store.async_load()

    def schedule(self, data: dict[str, Any]) -> None:
        """Debounce aggregate writes after accepted or rejected completed cycles."""
        if data != self._last:
            self._last = data
            self._store.async_delay_save(lambda: data, 5)

    async def save(self, data: dict[str, Any]) -> None:
        """Flush the latest learned aggregates at an entry lifecycle boundary."""
        await self._store.async_save(data)
        self._last = data

    async def remove(self) -> None:
        """Delete the learned model when its configuration entry is removed."""
        await self._store.async_remove()
