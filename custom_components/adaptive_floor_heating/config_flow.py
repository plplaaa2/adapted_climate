"""Collect thermostat inputs; related: const.py, __init__.py and translations/*.json."""

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
    CONF_AWAY_TEMPERATURE,
    CONF_COLD_TOLERANCE,
    CONF_HEAT_HOT_TOLERANCE,
    CONF_HOME_TEMPERATURE,
    CONF_HOT_TOLERANCE,
    CONF_HEATER,
    CONF_MODE,
    CONF_MIN_OFF,
    CONF_MIN_ON,
    CONF_OUTDOOR_TEMPERATURE_SENSOR,
    CONF_RETURN_TEMPERATURE_SENSOR,
    CONF_SENSOR_TIMEOUT,
    CONF_SUPPLY_TEMPERATURE_SENSOR,
    CONF_ROOM_COUNT,
    CONF_TEMPERATURE_SENSOR,
    CONF_ZONE_ID,
    CONF_ZONES,
    CONTEXT_HEATERS,
    DOMAIN,
    MAX_ROOMS,
    MODE_MULTI_ZONE_INTEGRATED,
    MIN_INTEGRATED_ROOMS,
    MODE_STANDALONE,
    NAME,
)
from .controller import Settings


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure one thermostat and its optional learning inputs."""

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
        self._shared_sensor: str | None = None
        self._entry_data: dict[str, Any] = {}
        self._entry_title = ""
        self._options_draft = vars(Settings())

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Choose the topology before collecting its entities."""
        return self.async_show_menu(
            step_id="user", menu_options=[MODE_STANDALONE, MODE_MULTI_ZONE_INTEGRATED]
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
                self._entry_title = NAME
                self._entry_data = {CONF_MODE: MODE_STANDALONE, **pair}
                return await self.async_step_heating_settings()
        return self.async_show_form(
            step_id=MODE_STANDALONE,
            data_schema=self._pair_schema(user_input),
            errors=errors,
        )

    async def async_step_multi_zone_integrated(
        self, user_input: dict[str, Any] | None = None
    ):
        """Choose a room count for one shared climate and temperature sensor."""
        errors = {}
        if user_input is not None:
            count = user_input.get(CONF_ROOM_COUNT)
            if (isinstance(count, bool) or not isinstance(count, (int, float))
                    or not isfinite(count) or count != int(count)
                    or not MIN_INTEGRATED_ROOMS <= count <= MAX_ROOMS):
                errors[CONF_ROOM_COUNT] = "invalid_integrated_room_count"
            else:
                self._room_count = int(count)
                self._zones = []
                self._shared_sensor = None
                self.context[CONTEXT_HEATERS] = []
                return await self.async_step_integrated_sensor()
        return self.async_show_form(
            step_id=MODE_MULTI_ZONE_INTEGRATED,
            data_schema=vol.Schema({
                vol.Required(CONF_ROOM_COUNT, default=2): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_INTEGRATED_ROOMS, max=MAX_ROOMS, step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
            }),
            errors=errors,
            description_placeholders={
                "min_rooms": str(MIN_INTEGRATED_ROOMS),
                "max_rooms": str(MAX_ROOMS),
            },
        )

    async def async_step_integrated_sensor(
        self, user_input: dict[str, Any] | None = None
    ):
        """Select one shared indoor sensor before collecting the room switches."""
        if not self._room_count:
            return self.async_abort(reason="invalid_step")
        errors = {}
        if user_input is not None:
            errors = self._validate_temperature_sensor(user_input.get(CONF_TEMPERATURE_SENSOR))
            if not errors:
                self._shared_sensor = user_input[CONF_TEMPERATURE_SENSOR]
                return await self.async_step_integrated_room()
        return self.async_show_form(
            step_id="integrated_sensor",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_TEMPERATURE_SENSOR,
                    description={
                        "suggested_value": (user_input or {}).get(CONF_TEMPERATURE_SENSOR)
                    },
                ): selector.EntitySelector(selector.EntitySelectorConfig(
                    domain="sensor", device_class="temperature"
                ))
            }),
            errors=errors,
        )

    async def async_step_integrated_room(
        self, user_input: dict[str, Any] | None = None
    ):
        """Collect one switch per room and save a single shared sensor."""
        if not self._room_count or self._shared_sensor is None:
            return self.async_abort(reason="invalid_step")
        errors = {}
        if user_input is not None:
            heater = user_input.get(CONF_HEATER)
            errors = self._validate_heater(heater)
            if any(zone[CONF_HEATER] == heater for zone in self._zones):
                errors[CONF_HEATER] = "duplicate_heater"
            if not errors:
                self._zones.append({CONF_ZONE_ID: str(len(self._zones) + 1), CONF_HEATER: heater})
                if len(self._zones) == self._room_count:
                    invalid_sensor = self._validate_temperature_sensor(self._shared_sensor)
                    invalid_heater = any(
                        self._validate_heater(zone[CONF_HEATER]) for zone in self._zones
                    )
                    if invalid_sensor or invalid_heater:
                        return self.async_abort(reason="entities_changed")
                    heaters = [zone[CONF_HEATER] for zone in self._zones]
                    self.context[CONTEXT_HEATERS] = heaters
                    self._entry_title = f"{NAME} ({self._room_count} rooms)"
                    self._entry_data = {
                        CONF_MODE: MODE_MULTI_ZONE_INTEGRATED, CONF_ROOM_COUNT: self._room_count,
                        CONF_ZONES: self._zones, CONF_TEMPERATURE_SENSOR: self._shared_sensor,
                    }
                    return await self.async_step_heating_settings()
                self.context[CONTEXT_HEATERS] = [zone[CONF_HEATER] for zone in self._zones]
                user_input = None
        return self.async_show_form(
            step_id="integrated_room",
            data_schema=vol.Schema({vol.Required(
                CONF_HEATER,
                description={"suggested_value": (user_input or {}).get(CONF_HEATER)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="switch"))}),
            errors=errors,
            description_placeholders={
                "room_number": str(len(self._zones) + 1),
                "room_count": str(self._room_count),
            },
        )

    async def async_step_heating_settings(self, user_input=None):
        """Collect offsets and control timers before preset and sensor settings."""
        errors = {}
        if user_input is not None:
            candidate = {**self._options_draft, **user_input}
            try:
                settings = Settings.from_options(candidate)
            except ValueError as err:
                errors[str(err)] = "invalid_setting"
            else:
                self._options_draft = {**candidate, **vars(settings)}
                return await self.async_step_preset_temperatures()
        return self.async_show_form(
            step_id="heating_settings",
            data_schema=_settings_schema(self._options_draft, _HEATING_SETTING_FIELDS),
            errors=errors,
        )

    async def async_step_preset_temperatures(self, user_input=None):
        """Collect occupancy temperatures after the heating control settings."""
        errors = {}
        if user_input is not None:
            candidate = {**self._options_draft, **user_input}
            try:
                settings = Settings.from_options(candidate)
            except ValueError as err:
                errors[str(err)] = "invalid_setting"
            else:
                self._options_draft = {**candidate, **vars(settings)}
                return await self.async_step_optional_sensors()
        return self.async_show_form(
            step_id="preset_temperatures",
            data_schema=_settings_schema(self._options_draft, _PRESET_FIELDS),
            errors=errors,
        )

    async def async_step_optional_sensors(self, user_input=None):
        """Collect optional outdoor and water temperature sensors, then save."""
        candidate = {**self._options_draft, **(user_input or {})}
        errors = (
            _validate_optional_temperature_sensors(self.hass, candidate)
            if user_input is not None else {}
        )
        if user_input is not None and not errors:
            self._options_draft.update({
                key: candidate.get(key) or None for key in _OPTIONAL_SENSOR_FIELDS
            })
            return await self.async_step_confirm()
        return self.async_show_form(
            step_id="optional_sensors",
            data_schema=_optional_sensor_schema(self._options_draft),
            errors=errors,
        )

    async def async_step_confirm(self, user_input=None):
        """Review all setup selections before creating the config entry."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._entry_title,
                data=self._entry_data,
                options=self._options_draft,
            )
        return self.async_show_form(
            step_id="confirm",
            description_placeholders=_summary_placeholders(self._options_draft),
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

    def _validate_heater(self, entity_id: Any) -> dict[str, str]:
        errors = {}
        if not isinstance(entity_id, str) or not entity_id.startswith("switch."):
            return {CONF_HEATER: "invalid_entity"}
        state = self.hass.states.get(entity_id)
        if state is None:
            return {CONF_HEATER: "invalid_entity"}
        if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return {CONF_HEATER: "entity_unavailable"}
        if state.state not in (STATE_ON, STATE_OFF):
            return {CONF_HEATER: "invalid_switch"}
        if entity_id in self._used_heaters():
            return {CONF_HEATER: "duplicate_heater"}
        return errors

    def _validate_temperature_sensor(self, entity_id: Any) -> dict[str, str]:
        return _validate_temperature_entity(
            self.hass, entity_id, CONF_TEMPERATURE_SENSOR
        )

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
    """Edit control, preset, and optional temperature sensors in ordered steps."""

    def __init__(self) -> None:
        self._options_draft: dict[str, Any] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Start the options flow at heating control settings."""
        if self._options_draft is None:
            self._options_draft = {
                **vars(Settings()), **self.config_entry.options,
            }
            for key in _OPTIONAL_SENSOR_FIELDS:
                self._options_draft.setdefault(key, None)
        return await self.async_step_heating_settings(user_input)

    async def async_step_heating_settings(self, user_input=None):
        errors = {}
        if user_input is not None:
            candidate = {**self._options_draft, **user_input}
            try:
                settings = Settings.from_options(candidate)
            except ValueError as err:
                errors[str(err)] = "invalid_setting"
            else:
                self._options_draft = {**candidate, **vars(settings)}
                return await self.async_step_preset_temperatures()
        return self.async_show_form(
            step_id="heating_settings",
            data_schema=_settings_schema(self._options_draft, _HEATING_SETTING_FIELDS),
            errors=errors,
        )

    async def async_step_preset_temperatures(self, user_input=None):
        errors = {}
        if user_input is not None:
            candidate = {**self._options_draft, **user_input}
            try:
                settings = Settings.from_options(candidate)
            except ValueError as err:
                errors[str(err)] = "invalid_setting"
            else:
                self._options_draft = {**candidate, **vars(settings)}
                return await self.async_step_optional_sensors()
        return self.async_show_form(
            step_id="preset_temperatures",
            data_schema=_settings_schema(self._options_draft, _PRESET_FIELDS),
            errors=errors,
        )

    async def async_step_optional_sensors(self, user_input=None):
        candidate = {**self._options_draft, **(user_input or {})}
        errors = (
            _validate_optional_temperature_sensors(self.hass, candidate)
            if user_input is not None else {}
        )
        if user_input is not None and not errors:
            self._options_draft.update({
                key: candidate.get(key) or None for key in _OPTIONAL_SENSOR_FIELDS
            })
            return await self.async_step_confirm()
        return self.async_show_form(
            step_id="optional_sensors",
            data_schema=_optional_sensor_schema(self._options_draft),
            errors=errors,
        )

    async def async_step_confirm(self, user_input=None):
        """Review all option changes before saving and reloading the entry."""
        if user_input is not None:
            return self.async_create_entry(title="", data=self._options_draft)
        return self.async_show_form(
            step_id="confirm",
            description_placeholders=_summary_placeholders(self._options_draft),
        )


_HEATING_SETTING_FIELDS = (
    (CONF_COLD_TOLERANCE, 0.1, 2.0, 0.1, "°C"),
    (CONF_HOT_TOLERANCE, 0.1, 2.0, 0.1, "°C"),
    (CONF_HEAT_HOT_TOLERANCE, 0.0, 2.0, 0.1, "°C"),
    (CONF_MIN_ON, 0, 3600, 1, "s"),
    (CONF_MIN_OFF, 0, 3600, 1, "s"),
    (CONF_SENSOR_TIMEOUT, 60, 3600, 1, "s"),
)
_PRESET_FIELDS = (
    (CONF_HOME_TEMPERATURE, 18, 30, 0.1, "°C"),
    (CONF_AWAY_TEMPERATURE, 18, 30, 0.1, "°C"),
)
_OPTIONAL_SENSOR_FIELDS = (
    CONF_OUTDOOR_TEMPERATURE_SENSOR,
    CONF_SUPPLY_TEMPERATURE_SENSOR,
    CONF_RETURN_TEMPERATURE_SENSOR,
)


def _settings_schema(defaults, fields):
    """Build a short number form for the selected control or preset fields."""
    schema = {}
    for key, lower, upper, step, unit in fields:
        schema[vol.Required(key, default=defaults[key])] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=lower, max=upper, step=step, unit_of_measurement=unit,
                mode=selector.NumberSelectorMode.BOX,
            )
        )
    return vol.Schema(schema)


def _optional_sensor_schema(defaults):
    """Build optional temperature sensor fields without overloading setup forms."""
    return vol.Schema({vol.Optional(
        key,
        description={"suggested_value": defaults.get(key)},
    ): selector.EntitySelector(selector.EntitySelectorConfig(
        domain="sensor", device_class="temperature"
    )) for key in _OPTIONAL_SENSOR_FIELDS})


def _summary_placeholders(options):
    """Format the draft so the final setup step can display a clear summary."""
    return {
        **{key: str(options.get(key, "—")) for key in _OPTIONAL_SENSOR_FIELDS},
        **{
            CONF_HOME_TEMPERATURE: str(options[CONF_HOME_TEMPERATURE]),
            CONF_AWAY_TEMPERATURE: str(options[CONF_AWAY_TEMPERATURE]),
            CONF_COLD_TOLERANCE: str(options[CONF_COLD_TOLERANCE]),
            CONF_HOT_TOLERANCE: str(options[CONF_HOT_TOLERANCE]),
            CONF_HEAT_HOT_TOLERANCE: str(options[CONF_HEAT_HOT_TOLERANCE]),
            CONF_MIN_ON: str(options[CONF_MIN_ON]),
            CONF_MIN_OFF: str(options[CONF_MIN_OFF]),
            CONF_SENSOR_TIMEOUT: str(options[CONF_SENSOR_TIMEOUT]),
        },
    }


def _validate_optional_temperature_sensors(hass, data):
    """Validate selected optional sensors using the same finite temperature contract."""
    errors = {}
    for key in _OPTIONAL_SENSOR_FIELDS:
        entity_id = data.get(key)
        if entity_id in (None, ""):
            continue
        errors.update(_validate_temperature_entity(hass, entity_id, key))
    return errors


def _validate_temperature_entity(hass, entity_id, field):
    """Validate one existing finite °C/°F sensor with temperature device class."""
    if not isinstance(entity_id, str) or not entity_id.startswith("sensor."):
        return {field: "invalid_entity"}
    state = hass.states.get(entity_id)
    if state is None:
        return {field: "invalid_entity"}
    if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return {field: "entity_unavailable"}
    if state.attributes.get(ATTR_DEVICE_CLASS) != "temperature":
        return {field: "invalid_temperature"}
    if state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) not in (
        UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
    ):
        return {field: "invalid_unit"}
    try:
        valid = isfinite(float(state.state))
    except (ValueError, TypeError, OverflowError):
        valid = False
    return {} if valid else {field: "invalid_temperature"}
