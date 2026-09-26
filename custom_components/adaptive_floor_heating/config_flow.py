"""Collect heater/sensor pairs; related: const.py, __init__.py, translations/*.json."""

from math import isfinite
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.helpers import selector

from .const import (
    CONF_COLD_TOLERANCE,
    CONF_AWAY_TEMPERATURE,
    CONF_HOME_TEMPERATURE,
    CONF_HOT_TOLERANCE,
    CONF_HEAT_HOT_TOLERANCE,
    CONF_MIN_OFF,
    CONF_MIN_ON,
    CONF_SENSOR_TIMEOUT,
    CONF_HEATER,
    CONF_MODE,
    CONF_ROOM_COUNT,
    CONF_TEMPERATURE_SENSOR,
    CONF_ZONE_ID,
    CONF_ZONES,
    CONTEXT_HEATERS,
    DOMAIN,
    MAX_ROOMS,
    MODE_MULTI_ZONE,
    MODE_STANDALONE,
    NAME,
)
from .controller import Settings


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a standalone heater or an ordered collection of room pairs."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Edit control settings; related: controller.Settings and runtime reload."""
        return ControlOptionsFlow()

    def __init__(self) -> None:
        """Keep incomplete room selections inside this flow until completion."""
        self._room_count = 0
        self._zones: list[dict[str, str]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Choose the topology before collecting its entities."""
        return self.async_show_menu(
            step_id="user", menu_options=[MODE_STANDALONE, MODE_MULTI_ZONE]
        )

    async def async_step_standalone(
        self, user_input: dict[str, Any] | None = None
    ):
        """Collect a single heater and its room temperature sensor."""
        errors = {}
        if user_input is not None:
            errors = self._validate_pair(user_input)
            if not errors:
                pair = self._pair_data(user_input)
                self.context[CONTEXT_HEATERS] = [pair[CONF_HEATER]]
                return self.async_create_entry(
                    title=f"{NAME} ({pair[CONF_HEATER]})",
                    data={CONF_MODE: MODE_STANDALONE, **pair},
                )
        return self.async_show_form(
            step_id=MODE_STANDALONE,
            data_schema=self._pair_schema(user_input),
            errors=errors,
        )

    async def async_step_multi_zone(
        self, user_input: dict[str, Any] | None = None
    ):
        """Collect a positive whole number of rooms before showing room forms."""
        errors = {}
        if user_input is not None:
            count = user_input.get(CONF_ROOM_COUNT)
            if (
                isinstance(count, bool)
                or not isinstance(count, (int, float))
                or not isfinite(count)
                or not 1 <= count <= MAX_ROOMS
                or count != int(count)
            ):
                errors[CONF_ROOM_COUNT] = "invalid_room_count"
            else:
                self._room_count = int(count)
                self._zones = []
                self.context[CONTEXT_HEATERS] = []
                return await self.async_step_room()
        return self.async_show_form(
            step_id=MODE_MULTI_ZONE,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ROOM_COUNT, default=1): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=MAX_ROOMS, step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={"max_rooms": str(MAX_ROOMS)},
        )

    async def async_step_room(self, user_input: dict[str, Any] | None = None):
        """Collect exactly one matched pair per room, preserving room order."""
        if not self._room_count:
            return self.async_abort(reason="invalid_step")
        errors = {}
        if user_input is not None:
            errors = self._validate_pair(user_input)
            if any(
                zone[CONF_HEATER] == user_input.get(CONF_HEATER)
                for zone in self._zones
            ):
                errors[CONF_HEATER] = "duplicate_heater"
            if not errors:
                pair = self._pair_data(user_input)
                candidate = [
                    *self._zones,
                    {CONF_ZONE_ID: str(len(self._zones) + 1), **pair},
                ]
                if len(candidate) == self._room_count:
                    # Recheck earlier rooms against changes during a long flow.
                    if any(self._validate_pair(zone) for zone in candidate):
                        return self.async_abort(reason="entities_changed")
                    self.context[CONTEXT_HEATERS] = [
                        zone[CONF_HEATER] for zone in candidate
                    ]
                    return self.async_create_entry(
                        title=f"{NAME} ({self._room_count})",
                        data={
                            CONF_MODE: MODE_MULTI_ZONE,
                            CONF_ROOM_COUNT: self._room_count,
                            CONF_ZONES: candidate,
                        },
                    )
                self._zones = candidate
                self.context[CONTEXT_HEATERS] = [
                    zone[CONF_HEATER] for zone in self._zones
                ]
                user_input = None
        return self.async_show_form(
            step_id="room",
            data_schema=self._pair_schema(user_input),
            errors=errors,
            description_placeholders={
                "room_number": str(len(self._zones) + 1),
                "room_count": str(self._room_count),
            },
        )

    def _used_heaters(self) -> set[str]:
        """Include completed entries and reservations held by other live flows."""
        used: set[str] = set()
        for entry in self._async_current_entries():
            if heater := entry.data.get(CONF_HEATER):
                used.add(heater)
            used.update(
                zone[CONF_HEATER] for zone in entry.data.get(CONF_ZONES, [])
            )
        for flow in self._async_in_progress():
            if flow["flow_id"] != self.flow_id:
                used.update(flow["context"].get(CONTEXT_HEATERS, []))
        return used

    def _validate_pair(self, data: dict[str, Any]) -> dict[str, str]:
        """Validate live states without sending any switch service calls."""
        errors = {}
        for field, domain in (
            (CONF_HEATER, "switch"), (CONF_TEMPERATURE_SENSOR, "sensor")
        ):
            entity_id = data.get(field)
            if not isinstance(entity_id, str) or not entity_id.startswith(f"{domain}."):
                errors[field] = "invalid_entity"
                continue
            state = self.hass.states.get(entity_id)
            if state is None:
                errors[field] = "invalid_entity"
                continue
            if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                errors[field] = "entity_unavailable"
                continue
            if field == CONF_HEATER:
                if state.state not in (STATE_ON, STATE_OFF):
                    errors[field] = "invalid_switch"
                elif entity_id in self._used_heaters():
                    errors[field] = "duplicate_heater"
            elif state.attributes.get(ATTR_DEVICE_CLASS) != "temperature":
                errors[field] = "invalid_temperature"
            elif state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) not in (
                UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
            ):
                errors[field] = "invalid_unit"
            else:
                try:
                    valid = isfinite(float(state.state))
                except (ValueError, TypeError, OverflowError):
                    valid = False
                if not valid:
                    errors[field] = "invalid_temperature"
        return errors

    @staticmethod
    def _pair_data(data: dict[str, Any]) -> dict[str, str]:
        """Save only the two entity references accepted by the form."""
        return {key: data[key] for key in (CONF_HEATER, CONF_TEMPERATURE_SENSOR)}

    @staticmethod
    def _pair_schema(data: dict[str, Any] | None) -> vol.Schema:
        """Provide filtered entity pickers and preserve inputs after errors."""
        data = data or {}
        return vol.Schema(
            {
                vol.Required(
                    CONF_HEATER, description={"suggested_value": data.get(CONF_HEATER)}
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="switch")),
                vol.Required(
                    CONF_TEMPERATURE_SENSOR,
                    description={"suggested_value": data.get(CONF_TEMPERATURE_SENSOR)},
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
            }
        )


class ControlOptionsFlow(config_entries.OptionsFlow):
    """Validate tolerances and timers before the entry reloads its controller."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            try:
                settings = Settings.from_options(user_input)
            except ValueError as err:
                errors[str(err)] = "invalid_setting"
            else:
                return self.async_create_entry(title="", data=vars(settings))
        defaults = {**vars(Settings()), **self.config_entry.options, **(user_input or {})}
        fields = {}
        for key, lower, upper, step, unit in (
            (CONF_HOME_TEMPERATURE, 18, 30, 0.1, "°C"),
            (CONF_AWAY_TEMPERATURE, 18, 30, 0.1, "°C"),
            (CONF_COLD_TOLERANCE, 0.1, 2.0, 0.1, "°C"),
            (CONF_HOT_TOLERANCE, 0.1, 2.0, 0.1, "°C"),
            (CONF_HEAT_HOT_TOLERANCE, 0.0, 2.0, 0.1, "°C"),
            (CONF_MIN_ON, 0, 3600, 1, "s"),
            (CONF_MIN_OFF, 0, 3600, 1, "s"),
            (CONF_SENSOR_TIMEOUT, 60, 3600, 1, "s"),
        ):
            fields[vol.Required(key, default=defaults[key])] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=lower, max=upper, step=step, unit_of_measurement=unit,
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields), errors=errors)
