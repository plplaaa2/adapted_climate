"""Expose heating diagnostics and completed-cycle metrics; related: curve_storage.py and diagnostics.py."""

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN, NAME
from .curve_storage import HEATING_METRIC_WINDOW, PEAK_ERROR_TOLERANCE
from .diagnostics import (
    HEATING_DIAGNOSTIC_STATUSES, diagnostic_attributes, diagnostic_snapshot,
    heating_diagnostic_status,
)
from .entity_naming import entity_id


@dataclass(frozen=True, kw_only=True)
class StateDescription(SensorEntityDescription):
    """Describe a phase state; numerical observations are not sensor entities."""

    sensor_role: str


# Keep the registered prediction identity; related: diagnostics.py and frontend/panel.js.
STATE_SENSORS = (
    StateDescription(
        key="prediction_status", translation_key="prediction_status", name=None,
        sensor_role="prediction", icon="mdi:heart-pulse",
        entity_category=EntityCategory.DIAGNOSTIC, device_class=SensorDeviceClass.ENUM,
        options=list(HEATING_DIAGNOSTIC_STATUSES),
    ),
)

# Publish recorded durations and errors as numeric measurements; related: curve_storage.py.
METRIC_SENSORS = tuple(
    SensorEntityDescription(
        key=key, translation_key=key, name=None, icon=icon,
        entity_category=EntityCategory.DIAGNOSTIC, device_class=device_class,
        native_unit_of_measurement=unit, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=precision,
    )
    for key, icon, unit, device_class, precision in (
        ("peak_prediction_accuracy", "mdi:bullseye-arrow", "%", None, 0),
        ("last_heating_duration", "mdi:timer-outline", "min", SensorDeviceClass.DURATION, 1),
        ("average_heating_duration", "mdi:timer-sand", "min", SensorDeviceClass.DURATION, 1),
        ("last_peak_error", "mdi:thermometer-chevron-up", "°C", None, 2),
        ("mean_absolute_peak_error", "mdi:chart-line", "°C", None, 2),
    )
)


# Retire placeholder initial learning and numeric diagnostics; related: diagnostics.py, frontend/panel.js.
REMOVED_SENSOR_KEYS = (
    "learning_status", "observation_status",
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
    """Register the diagnostic sensors and remove retired registry entries."""
    registry = er.async_get(hass)
    removed_ids = {f"{entry.entry_id}_{key}" for key in REMOVED_SENSOR_KEYS}
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.domain == "sensor" and registered.unique_id in removed_ids:
            registry.async_remove(registered.entity_id)
    async_add_entities([
        LearningStateSensor(entry, entry.runtime_data, description)
        for description in STATE_SENSORS
    ] + [
        HeatingMetricSensor(entry, entry.runtime_data, description)
        for description in METRIC_SENSORS
    ])


class HeatingDiagnosticSensor(SensorEntity):
    """Share entity registration and runtime updates across diagnostics."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, runtime, description: SensorEntityDescription) -> None:
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


class LearningStateSensor(HeatingDiagnosticSensor):
    """Read the current phase without calculating predictions."""

    @property
    def native_value(self) -> Any:
        return heating_diagnostic_status(diagnostic_snapshot(self._runtime))

    @property
    def extra_state_attributes(self) -> dict:
        key = self.entity_description.key
        return {
            "sensor_role": self.entity_description.sensor_role,
            **diagnostic_snapshot(self._runtime),
            **diagnostic_attributes(self._runtime, key),
            "learning_reason": diagnostic_attributes(self._runtime, "learning_status")["reason"],
        }


class HeatingMetricSensor(HeatingDiagnosticSensor):
    """Read the latest committed metrics without querying SQLite on state updates."""

    @property
    def _metrics(self) -> dict:
        store = self._runtime.curve_store
        return getattr(store, "heating_metrics", {})

    @property
    def native_value(self) -> float | None:
        return self._metrics.get(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict:
        key = self.entity_description.key
        if key == "average_heating_duration":
            return {"sample_count": self._metrics.get("duration_count", 0), "window_size": HEATING_METRIC_WINDOW}
        if key in ("mean_absolute_peak_error", "peak_prediction_accuracy"):
            attrs = {"comparison_count": self._metrics.get("comparison_count", 0),
                     "window_size": HEATING_METRIC_WINDOW}
            if key == "peak_prediction_accuracy":
                attrs["tolerance_celsius"] = PEAK_ERROR_TOLERANCE
                attrs["mean_absolute_error_celsius"] = self._metrics.get("mean_absolute_peak_error")
            return attrs
        return {}
