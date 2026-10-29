"""Expose phase-only state sensors; related: diagnostics.py, runtime.py and panel.js."""

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, NAME
from .diagnostics import (
    DIAGNOSTIC_KEYS, INITIAL_LEARNING_STATUSES, LEARNING_STATUSES,
    OBSERVATION_STATUSES, PREDICTION_STATUSES, diagnostic_attributes, diagnostic_snapshot,
)
from .entity_naming import entity_id
from .experimental import EXPERIMENT_KEYS
from .water_observation import WATER_KEYS


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
        ("initial_learning_status", "analysis", "mdi:school-outline", INITIAL_LEARNING_STATUSES),
        ("learning_status", "analysis", "mdi:chart-bell-curve", LEARNING_STATUSES),
        ("observation_status", "observation", "mdi:thermometer-lines", OBSERVATION_STATUSES),
        ("prediction_status", "prediction", "mdi:crystal-ball", PREDICTION_STATUSES),
    )
)


# Remove retired numeric diagnostics and superseded phase IDs; related: diagnostics.py, experimental.py.
REMOVED_SENSOR_KEYS = (
    "temperature_last_reported", "observed_cycles", "temperature_slope",
    "heating_response_delay", "residual_rise", "peak_delay",
    "learned_heating_response_delay", "learned_heating_rate", "learned_residual_rise",
    "learned_peak_delay", "learning_confidence", "learned_heat_loss_rate",
    "accepted_learning_cycles", "rejected_learning_cycles", "rejected_metric_samples",
    "off_predicted_peak_existing", "off_predicted_peak_curve", "actual_peak_temperature",
    "off_prediction_error_existing", "off_prediction_error_curve", "off_prediction_confidence",
    "observation_phase", "off_prediction_status", "curve_learning_status",
    *EXPERIMENT_KEYS, *WATER_KEYS,
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
