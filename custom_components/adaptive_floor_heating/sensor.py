"""Expose non-controlling observation diagnostics; related: runtime.py and history.py."""

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, NAME
from .experimental import EXPERIMENT_KEYS, prediction_snapshot
from .water_observation import WATER_KEYS


@dataclass(frozen=True, kw_only=True)
class ObservationDescription(SensorEntityDescription):
    """Describe one read-only thermal observation entity."""

    value_key: str


OBSERVATIONS = (
    ObservationDescription(
        key="temperature_last_reported", translation_key="temperature_last_reported", name=None,
        value_key="temperature_last_reported", icon="mdi:clock-check-outline",
        device_class=SensorDeviceClass.TIMESTAMP, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="temperature_slope", translation_key="temperature_slope", name=None,
        value_key="temperature_slope", icon="mdi:chart-line",
        native_unit_of_measurement="°C/h", suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="heating_response_delay", translation_key="heating_response_delay", name=None,
        value_key="heating_response_delay", icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="min",
        suggested_display_precision=1, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="residual_rise", translation_key="residual_rise", name=None,
        value_key="residual_rise", icon="mdi:thermometer-plus",
        device_class=SensorDeviceClass.TEMPERATURE_DELTA,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS, suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="peak_delay", translation_key="peak_delay", name=None,
        value_key="peak_delay", icon="mdi:timer-sand",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="min",
        suggested_display_precision=1, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="observed_cycles", translation_key="observed_cycles", name=None,
        value_key="observed_cycles", icon="mdi:counter",
        native_unit_of_measurement="cycles", suggested_display_precision=0,
        state_class=SensorStateClass.TOTAL_INCREASING, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="learned_heating_response_delay", translation_key="learned_heating_response_delay",
        name=None, value_key="learned_heating_response_delay", icon="mdi:timer-check-outline",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="min",
        suggested_display_precision=1, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="learned_heating_rate", translation_key="learned_heating_rate", name=None,
        value_key="learned_heating_rate", icon="mdi:chart-line",
        native_unit_of_measurement="°C/h", suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="learned_residual_rise", translation_key="learned_residual_rise", name=None,
        value_key="learned_residual_rise", icon="mdi:thermometer-plus",
        device_class=SensorDeviceClass.TEMPERATURE_DELTA,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS, suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="learned_peak_delay", translation_key="learned_peak_delay", name=None,
        value_key="learned_peak_delay", icon="mdi:timer-sand",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="min",
        suggested_display_precision=1, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="learning_confidence", translation_key="learning_confidence", name=None,
        value_key="learning_confidence", icon="mdi:chart-bell-curve",
        native_unit_of_measurement="%", suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="learned_heat_loss_rate", translation_key="learned_heat_loss_rate", name=None,
        value_key="learned_heat_loss_rate", icon="mdi:home-thermometer-outline",
        native_unit_of_measurement="1/h", suggested_display_precision=4,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="accepted_learning_cycles", translation_key="accepted_learning_cycles", name=None,
        value_key="accepted_learning_cycles", icon="mdi:check-circle-outline",
        native_unit_of_measurement="cycles", suggested_display_precision=0,
        state_class=SensorStateClass.TOTAL_INCREASING, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="rejected_learning_cycles", translation_key="rejected_learning_cycles", name=None,
        value_key="rejected_learning_cycles", icon="mdi:close-circle-outline",
        native_unit_of_measurement="cycles", suggested_display_precision=0,
        state_class=SensorStateClass.TOTAL_INCREASING, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ObservationDescription(
        key="rejected_metric_samples", translation_key="rejected_metric_samples", name=None,
        value_key="rejected_metric_samples", icon="mdi:filter-remove-outline",
        native_unit_of_measurement="samples", suggested_display_precision=0,
        state_class=SensorStateClass.TOTAL_INCREASING, entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


# Default-disabled individual experiment entities; related: experimental.py, translations.
EXPERIMENTS = tuple(
    ObservationDescription(
        key=key, translation_key=key, name=None, value_key=key,
        icon="mdi:flask-outline", entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        native_unit_of_measurement=(
            "°C/h" if key.endswith("slope") else "%" if key.endswith("weight")
            else "°C" if key.endswith("temperature") else None
        ),
        device_class=SensorDeviceClass.TEMPERATURE if key.endswith("temperature") else None,
        state_class=SensorStateClass.MEASUREMENT if key != "experimental_status" else None,
        suggested_display_precision=2 if key != "experimental_status" else None,
    ) for key in EXPERIMENT_KEYS
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Create diagnostic sensors linked to the software thermostat device."""
    async_add_entities([
        ThermalObservationSensor(entry, entry.runtime_data, description)
        for description in (*OBSERVATIONS, *EXPERIMENTS, *WATER_EXPERIMENTS)
    ])


class ThermalObservationSensor(SensorEntity):
    """Read one metric collected by the observation-only runtime tracker."""

    _attr_has_entity_name = True

    def __init__(self, entry, runtime, description: ObservationDescription) -> None:
        self.entity_description = description
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Refresh this diagnostic whenever the runtime publishes new values."""
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.subscribe(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        """Diagnostics only run while the config entry runtime is active."""
        return self._runtime.started

    @property
    def native_value(self) -> Any:
        """Return the requested observation or None until it can be measured."""
        if self.entity_description.value_key in WATER_KEYS:
            return self._runtime.water_observation.snapshot(
                self._runtime.hass.loop.time()
            )[self.entity_description.value_key]
        if self.entity_description.value_key in EXPERIMENT_KEYS:
            return prediction_snapshot(self._runtime)[self.entity_description.value_key]
        observation = self._runtime.observation
        cycle = observation.last_cycle
        model = self._runtime.thermal_model
        metrics = model.metrics
        return {
            "temperature_last_reported": self._runtime.temperature_last_reported,
            "temperature_slope": observation.temperature_slope,
            "heating_response_delay": cycle.response_delay_minutes if cycle else None,
            "residual_rise": cycle.residual_rise if cycle else None,
            "peak_delay": cycle.peak_delay_minutes if cycle else None,
            "observed_cycles": observation.completed_cycles,
            "learned_heating_response_delay": metrics["heating_response_delay"]["mean"],
            "learned_heating_rate": metrics["heating_rate"]["mean"],
            "learned_residual_rise": metrics["residual_rise"]["mean"],
            "learned_peak_delay": metrics["peak_delay"]["mean"],
            "learning_confidence": model.overall_confidence * 100,
            "learned_heat_loss_rate": model.heat_loss_rate["mean"],
            "accepted_learning_cycles": model.accepted_cycles,
            "rejected_learning_cycles": model.rejected_cycles,
            "rejected_metric_samples": model.rejected_metric_samples,
        }[self.entity_description.value_key]


# Separate recorder-friendly pipe diagnostics; related: water_observation.py, translations.
WATER_EXPERIMENTS = tuple(
    ObservationDescription(
        key=key, translation_key=key, name=None, value_key=key,
        icon="mdi:pipe", entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        native_unit_of_measurement=(
            None if key == "water_status" else "min" if key == "water_peak_delay"
            else "°C/h" if key.endswith("slope") else "°C"
        ),
        device_class=(
            SensorDeviceClass.DURATION if key == "water_peak_delay"
            else SensorDeviceClass.TEMPERATURE_DELTA if key in (
                "water_difference", "water_supply_room", "water_return_room", "water_residual_rise"
            ) else SensorDeviceClass.TEMPERATURE if key in (
                "water_supply", "water_return", "water_off_supply", "water_off_return"
            ) else None
        ),
        state_class=SensorStateClass.MEASUREMENT if key != "water_status" else None,
        suggested_display_precision=2 if key != "water_status" else None,
    ) for key in WATER_KEYS
)
