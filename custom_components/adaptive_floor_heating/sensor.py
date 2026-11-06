"""Expose phase-only state sensors; related: diagnostics.py, runtime.py and panel.js."""

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, NAME
from .diagnostics import (
    DIAGNOSTIC_KEYS, LEARNING_STATUSES,
    OBSERVATION_STATUSES, PREDICTION_STATUSES, diagnostic_attributes, diagnostic_snapshot,
)
from .entity_naming import entity_id


@dataclass(frozen=True, kw_only=True)
class StateDescription(SensorEntityDescription):
    """Describe a phase state; numerical observations are not sensor entities."""

    value_key: str
    sensor_role: str


STATE_SENSORS = tuple(
    StateDescription(
        key=key, translation_key=key, name=None, value_key=key, sensor_role=role,
        icon=icon, entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM, options=list(options),
    ) for key, role, icon, options in (
        ("learning_status", "analysis", "mdi:chart-bell-curve", LEARNING_STATUSES),
        ("observation_status", "observation", "mdi:thermometer-lines", OBSERVATION_STATUSES),
        ("prediction_status", "prediction", "mdi:crystal-ball", PREDICTION_STATUSES),
    )
)


# Retire placeholder initial learning and numeric diagnostics; related: diagnostics.py, frontend/panel.js.
REMOVED_SENSOR_KEYS = (
    "initial_learning_status",
    "temperature_last_reported", "observed_cycles", "temperature_slope",
    "heating_response_delay", "residual_rise", "peak_delay",
    "learned_heating_response_delay", "learned_heating_rate", "learned_residual_rise",
    "learned_peak_delay", "learning_confidence", "learned_heat_loss_rate",
    "accepted_learning_cycles", "rejected_learning_cycles", "rejected_metric_samples",
    "off_predicted_peak_existing", "off_predicted_peak_curve", "actual_peak_temperature",
    "off_prediction_error_existing", "off_prediction_error_curve", "off_prediction_confidence",
    "observation_phase", "off_prediction_status", "curve_learning_status",
    # Retain experimental IDs for legacy cleanup; related: runtime.py, thermal_model.py.
    "experimental_measured_slope", "experimental_outdoor_slope",
    "experimental_blended_slope", "experimental_outdoor_weight",
    "experimental_baseline_temperature", "experimental_predicted_temperature",
    "experimental_status",
    # Keep retired pipe IDs for registry cleanup; related: runtime.py, config_flow.py.
    "water_supply", "water_return", "water_difference", "water_supply_room",
    "water_return_room", "water_supply_slope", "water_return_slope",
    "water_off_supply", "water_off_return", "water_residual_rise",
    "water_peak_delay", "water_status",
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Create phase states and remove numeric/legacy diagnostic sensor entities."""
    registry = er.async_get(hass)
    removed_ids = {f"{entry.entry_id}_{key}" for key in REMOVED_SENSOR_KEYS}
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.domain == "sensor" and registered.unique_id in removed_ids:
            registry.async_remove(registered.entity_id)
    async_add_entities([
        LearningStateSensor(entry, entry.runtime_data, description)
        for description in STATE_SENSORS
    ])


class LearningStateSensor(SensorEntity):
    """Read one phase state from the runtime without calculating predictions."""

    _attr_has_entity_name = True

    def __init__(self, entry, runtime, description: StateDescription) -> None:
        self.entity_description = description
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self.entity_id = entity_id(runtime.hass, entry, "sensor", description.key)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.subscribe(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return self._runtime.started

    @property
    def native_value(self) -> Any:
        return diagnostic_snapshot(self._runtime)[self.entity_description.value_key]

    @property
    def extra_state_attributes(self) -> dict:
        key = self.entity_description.value_key
        return {"sensor_role": self.entity_description.sensor_role,
                **(diagnostic_attributes(self._runtime, key) if key in DIAGNOSTIC_KEYS else {})}
