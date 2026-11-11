"""Persist user intent and fault latches; related: controller.py, runtime.py."""

from datetime import time
import logging
from math import isfinite
from typing import Any
from .peak_prediction import valid_forecast

from .const import (
    DEFAULT_HOME_TEMPERATURE, DEFAULT_PREDICTION_MODE, DOMAIN, LATCHED_FAULTS,
    PERSISTED_LEARNING_MODELS, MAX_TARGET, MIN_TARGET, PREDICTION_MODES,
    OFF_MODELS,
)

_LOGGER = logging.getLogger(__name__)


# Validate optional recorder metadata independently of thermostat intent; related: runtime.py, diagnostics.py.
def _valid_wall_time(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and isfinite(value) and 0 <= value <= 253402300799)


def _restore_hybrid_summary(record: dict) -> None:
    """Drop invalid optional shadow evidence without losing existing diagnostics."""
    value = record.get("hybrid_summary_peak")
    if record.get("off_model") != "hybrid" or type(value) not in (int, float) or not isfinite(value) or abs(value) > 100:
        record.pop("hybrid_summary_peak", None)


def _decode_prediction_bias(data: Any) -> dict[str, float]:
    """Restore bounded per-model Peak bias without trusting arbitrary stored values."""
    default = {model: 0.0 for model in PERSISTED_LEARNING_MODELS}
    if data is None:
        return default
    if (not isinstance(data, dict) or set(data) != set(PERSISTED_LEARNING_MODELS)
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not isfinite(value) or not -0.5 <= value <= 0.5
                   for value in data.values())):
        _LOGGER.warning("Discarding invalid Peak prediction bias")
        return default
    return {model: float(data[model]) for model in PERSISTED_LEARNING_MODELS}


def _decode_prediction_errors(data: Any) -> dict[str, list[float]]:
    """Restore bounded recent validation errors independently of saved climate intent."""
    default = {model: [] for model in PERSISTED_LEARNING_MODELS}
    if data is None:
        return default
    if (not isinstance(data, dict) or set(data) != set(PERSISTED_LEARNING_MODELS)
            or any(not isinstance(rows, list) or len(rows) > 20 or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not isfinite(value) or abs(value) > 100 for value in rows
            ) for rows in data.values())):
        _LOGGER.warning("Discarding invalid Peak prediction errors")
        return default
    return {model: [float(value) for value in data[model]] for model in PERSISTED_LEARNING_MODELS}


def _decode_off_prediction(data: Any) -> dict | None:
    """Discard corrupt historical diagnostics without resetting valid heating intent."""
    if data is None:
        return None
    if (isinstance(data, dict)
            and {"off_at", "predictions", "confidences", "selected_model"}.issubset(set(data))
            and _valid_wall_time(data["off_at"])
            and data["selected_model"] in PERSISTED_LEARNING_MODELS
            and isinstance(data["predictions"], dict) and isinstance(data["confidences"], dict)
            and set(data["predictions"]) == set(data["confidences"])
            and set(data["predictions"]).issubset(PERSISTED_LEARNING_MODELS)
            and all(not isinstance(v, bool) and isinstance(v, (int, float))
                    and isfinite(v) and abs(v) <= 100 for v in data["predictions"].values())
            and all(v is None or (not isinstance(v, bool) and isinstance(v, (int, float))
                    and isfinite(v) and 0 <= v <= 1) for v in data["confidences"].values())
            and set(data).issubset({"off_at", "predictions", "confidences", "selected_model",
                                    "comparison_counts", "runtime_sample_count", "runtime_curve_range",
                                    "runtime_extrapolated", "peak_forecast", "decision_forecast", "off_model",
                                    "hybrid_summary_peak", "off_learning_source", "sunrise_saving"})
            and ("sunrise_saving" not in data or type(data["sunrise_saving"]) is bool)
            and ("off_model" not in data or data["off_model"] in OFF_MODELS)
            and ("peak_forecast" not in data or valid_forecast(data["peak_forecast"]))
            and ("decision_forecast" not in data or (
                isinstance(data["decision_forecast"], dict)
                and set(data["decision_forecast"]) == {"cycle_id", "generated_at", "forecast"}
                and isinstance(data["decision_forecast"]["cycle_id"], str)
                and _valid_wall_time(data["decision_forecast"]["generated_at"])
                and valid_forecast(data["decision_forecast"]["forecast"])))
            and ("comparison_counts" not in data or (
                isinstance(data["comparison_counts"], dict)
                and set(data["comparison_counts"]) == set(data["predictions"])
                and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                        for v in data["comparison_counts"].values())
            ))):
        restored = dict(data)
        _restore_hybrid_summary(restored)
        if restored.get("off_learning_source") != "long_term" or restored.get("off_model") not in ("hybrid", "runtime"):
            restored.pop("off_learning_source", None)
        if "comparison_counts" not in restored:
            # Older values represented sample coverage/model confidence, not validated accuracy.
            restored["confidences"] = {model: None for model in restored["confidences"]}
            restored["comparison_counts"] = {model: 0 for model in restored["predictions"]}
        return restored
    _LOGGER.warning("Discarding invalid historical OFF prediction diagnostics")
    return None


def _decode_cooling_prediction(data: Any) -> dict | None:
    """Validate optional frozen forecasts without losing thermostat intent; related: curve_api.py, runtime.py."""
    if not isinstance(data, dict) or data.get("model") != "cooling":
        return None
    if (not _valid_wall_time(data.get("peak_at")) or not _valid_wall_time(data.get("generated_at"))
            or data["generated_at"] < data["peak_at"]
            or isinstance(data.get("temperature"), bool) or not isinstance(data.get("temperature"), (int, float))
            or not isfinite(data["temperature"]) or abs(data["temperature"]) > 100
            or data.get("status") not in ("ready", "insufficient_data")):
        return None
    points = data.get("trajectory")
    if not isinstance(points, list) or not 1 <= len(points) <= 301:
        return None
    for index, point in enumerate(points):
        if (not isinstance(point, list) or len(point) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in point)
                or point[0] != index * 5 or abs(point[1]) > 100):
            return None
    if points[0] != [0, 0] or (data["status"] == "ready") != (len(points) > 1):
        return None
    return {key: data[key] for key in ("model", "peak_at", "generated_at", "temperature", "status", "trajectory")}


# Keep each residual algorithm's feedback independent, including older snapshots without a model selection.
def _decode_residual_feedback(data: Any) -> dict:
    if (not isinstance(data, dict) or set(data) != {"bias", "errors"}
            or type(data["bias"]) not in (int, float) or not isfinite(data["bias"]) or abs(data["bias"]) > .5
            or not isinstance(data["errors"], list) or len(data["errors"]) > 20
            or any(type(value) not in (int, float) or not isfinite(value) or abs(value) > 100
                   for value in data["errors"])):
        return {"bias": 0.0, "errors": []}
    return {"bias": float(data["bias"]), "errors": list(data["errors"])}


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
    prediction_mode = data.get("prediction_mode", DEFAULT_PREDICTION_MODE)
    comparison = data.get("last_peak_comparison")
    if comparison is not None:
        if (not isinstance(comparison, dict)
                or not {"actual_peak", "predictions", "errors"}.issubset(comparison)
                or not set(comparison).issubset({"actual_peak", "predictions", "errors", "confidences",
                                                 "comparison_counts", "off_at", "peak_at", "completed_at", "off_model",
                                                 "hybrid_summary_peak", "off_learning_source", "sunrise_saving"})
                or ("off_model" in comparison and comparison["off_model"] not in OFF_MODELS)
                or ("sunrise_saving" in comparison and type(comparison["sunrise_saving"]) is not bool)
                or not isinstance(comparison["predictions"], dict)
                or not isinstance(comparison["errors"], dict)
                or set(comparison["predictions"]) != set(comparison["errors"])
                or ("confidences" in comparison and (
                    not isinstance(comparison["confidences"], dict)
                    or set(comparison["confidences"]) != set(comparison["predictions"])
                    or any(value is not None and (isinstance(value, bool)
                        or not isinstance(value, (int, float)) or not isfinite(value)
                        or not 0 <= value <= 1) for value in comparison["confidences"].values())
                ))
                or ("comparison_counts" in comparison and (
                    not isinstance(comparison["comparison_counts"], dict)
                    or set(comparison["comparison_counts"]) != set(comparison["predictions"])
                    or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                           for value in comparison["comparison_counts"].values())
                ))
                or not set(comparison["predictions"]).issubset(PERSISTED_LEARNING_MODELS)):
            raise ValueError("Invalid Peak comparison")
        values = [comparison["actual_peak"], *comparison["predictions"].values(), *comparison["errors"].values()]
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not isfinite(value) or abs(value) > 100 for value in values):
            raise ValueError("Invalid Peak comparison values")
        # Additive wall-clock metadata keeps legacy comparison records valid.
        # Related: diagnostics.py; malformed optional timestamps cannot discard saved intent.
        comparison = dict(comparison)
        _restore_hybrid_summary(comparison)
        if comparison.get("off_learning_source") != "long_term" or comparison.get("off_model") not in ("hybrid", "runtime"):
            comparison.pop("off_learning_source", None)
        for key in ("off_at", "peak_at", "completed_at"):
            if key in comparison and comparison[key] is not None and not _valid_wall_time(comparison[key]):
                comparison.pop(key)
    preset_temperature = data.get("preset_temperature")
    if (isinstance(target, bool) or not isinstance(target, (int, float))
            or not isfinite(target) or not MIN_TARGET <= target <= MAX_TARGET
            or mode not in ("off", "heat", "auto")
            or not isinstance(faults, list)
            or preset not in ("home", "away")
            or prediction_mode not in PREDICTION_MODES
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
    sunrise_start = _decode_sunrise_time(data.get("sunrise_start"))
    sunrise_end = _decode_sunrise_time(data.get("sunrise_end"))
    prediction_errors = _decode_prediction_errors(data.get("prediction_errors"))
    if data.get("prediction_errors") is None and comparison is not None and comparison.get("sunrise_saving") is not True:
        # Use the one durable legacy outcome immediately; older stores retained no error history.
        for model, error in comparison["errors"].items():
            prediction_errors[model].append(float(error))
    return {
        "schema_version": 1, "target": float(target), "mode": mode,
        "additive_feedback": _decode_residual_feedback(data.get("additive_feedback")),
        "hybrid_feedback": _decode_residual_feedback(data.get("hybrid_feedback")),
        "preset": preset, "preset_temperature": configured_preset_temperature,
        "prediction_mode": prediction_mode,
        "sunrise_enabled": data.get("sunrise_enabled") is True and sunrise_start is not None and sunrise_end is not None,
        "sunrise_start": sunrise_start or "00:00:00",
        "sunrise_end": sunrise_end or "00:00:00",
        "last_peak_comparison": comparison,
        "last_cooling_prediction": _decode_cooling_prediction(data.get("last_cooling_prediction")),
        "prediction_bias": _decode_prediction_bias(data.get("prediction_bias")),
        "prediction_errors": prediction_errors,
        "last_off_prediction": _decode_off_prediction(data.get("last_off_prediction")),
        "learning_reset_pending": data.get("learning_reset_pending") is True,
        "faults": faults[:],
    }


# Restore schedule options independently of thermostat intent; related: runtime.py, time.py.
def _decode_sunrise_time(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            parsed = time.fromisoformat(value)
            if parsed.tzinfo is None:
                return parsed.isoformat()
        except ValueError:
            pass
    return None


class RuntimeStore:
    """Keep thermostat intent separate from learned thermal data."""

    def __init__(self, hass, entry_id: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, 1, f"{DOMAIN}.{entry_id}.runtime", atomic_writes=True)
        self._last: dict[str, Any] | None = None

    async def load(
        self, *, default_target: float = DEFAULT_HOME_TEMPERATURE,
        home_temperature: float = DEFAULT_HOME_TEMPERATURE, away_temperature: float = 18.0,
    ) -> dict[str, Any]:
        """Start OFF when no valid persisted state is available."""
        default = {
            "schema_version": 1, "target": default_target, "mode": "off",
            "preset": "home", "preset_temperature": home_temperature, "faults": [],
            "prediction_mode": DEFAULT_PREDICTION_MODE,
            "sunrise_enabled": False, "sunrise_start": "00:00:00", "sunrise_end": "00:00:00",
            "prediction_bias": {model: 0.0 for model in PERSISTED_LEARNING_MODELS},
            "prediction_errors": {model: [] for model in PERSISTED_LEARNING_MODELS},
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

    async def save_reset_state(self, data: dict[str, Any]) -> None:
        """Verify the reset marker reached storage before advancing; related: runtime.py."""
        await self._store.async_save(data)
        restored = await self._store.async_load()
        if not isinstance(restored, dict) or restored.get("learning_reset_pending") is not data["learning_reset_pending"]:
            raise OSError("Learning reset state could not be saved")
        self._last = data


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
        """Cancel delayed writes and delete this entry's learned model."""
        await self._store.async_remove()
        self._last = None
