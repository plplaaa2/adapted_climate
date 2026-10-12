"""Expose read-only committed curve buckets; related: panel.py, curve_memory.py, runtime.py."""

import time
import logging
import sqlite3
from math import isfinite

from .const import DOMAIN
from .curve_learning import CURVE_TYPES
from .curve_memory import blended

_LOGGER = logging.getLogger(__name__)


def heating_curve_preview(runtime, now: float | None = None) -> dict | None:
    """Anchor committed learned deltas without mutating predictors; related: climate.py, panel.js."""
    store = getattr(runtime, "curve_store", None)
    cycle = getattr(getattr(runtime, "curve_tracker", None), "cycle", None)
    temperature = runtime.controller.temperature
    if (not runtime.started or store is None or runtime.actuator.observed is not True
            or cycle is None or cycle.off_at is not None or cycle.invalid_reason
            or cycle.curve_type not in CURVE_TYPES[:3]
            or temperature is None or not isfinite(temperature)):
        return None
    now = time.time() if now is None else now
    elapsed = (now - cycle.started_at) / 60
    if not 0 <= elapsed < 1440:
        return None
    position, delta = elapsed, 0.0
    points = [[0.0, 0.0]]
    confidences = []
    sources = set()
    for index in range(int(elapsed / 5), 288):
        kind = cycle.curve_type
        memory = store.model.memory[kind]
        row = blended(memory.current.get(index), memory.long_term.get(index), now)
        if row is None and kind == "PREDICTIVE_WARM_HEATING":
            kind = "WARM_HEATING"
            memory = store.model.memory[kind]
            row = blended(memory.current.get(index), memory.long_term.get(index), now)
        if row is None or not isfinite(row[0]):
            break
        end = (index + 1) * 5
        delta += row[0] * (end - position) / 5
        points.append([end - elapsed, delta])
        position = end
        confidences.append(row[1])
        sources.add(kind)
    if len(points) < 2:
        return None
    return {"model": "curve", "generated_at": now, "temperature": temperature,
            "predicted_peak": temperature + max(point[1] for point in points),
            "trajectory": points, "confidence": min(confidences),
            "accepted_cycles": sum(store.model.accepted.get(kind, 0) for kind in sources),
            "curve_type": cycle.curve_type, "elapsed_minutes": elapsed}


def curve_snapshot(runtime, now: float | None = None) -> dict:
    """Copy in-memory learned deltas without predictions, SQLite access or updates."""
    store = getattr(runtime, "curve_store", None)
    if not getattr(runtime, "started", False) or store is None:
        return {"status": "unavailable", "curves": {}}
    now = time.time() if now is None else now
    curves = {}
    for kind in CURVE_TYPES:
        memory = store.model.memory[kind]
        diagnostics = memory.diagnostics(now)
        layers = {}
        for layer in ("current", "long_term"):
            layers[layer] = [
                {"index": index, "minutes": (index + 1) * 5, "delta": bucket.mean,
                 "samples": bucket.samples, "promotions": bucket.promotions,
                 "confidence": bucket.confidence(now, long_term=layer == "long_term"),
                 "updated_at": bucket.updated_at}
                for index, bucket in sorted(getattr(memory, layer).items())
            ]
        curves[kind] = {**layers,
                        "current_confidence": diagnostics["current_confidence"],
                        "long_term_confidence": diagnostics["long_term_confidence"],
                        "accepted": store.model.accepted.get(kind, 0),
                        "rejected": store.model.rejected.get(kind, 0)}
    return {"status": "ready", "unit": "°C", "bucket_minutes": 5, "curves": curves}


def _read_runtime(hass, connection, msg: dict):
    """Check entity read permission and registry ownership before returning its runtime."""
    from homeassistant.auth.permissions.const import POLICY_READ
    from homeassistant.helpers import entity_registry as er

    entity_id = msg["entity_id"]
    if not connection.user.is_admin and not connection.user.permissions.check_entity(entity_id, POLICY_READ):
        connection.send_error(msg["id"], "unauthorized", "Entity read permission required")
        return
    registered = er.async_get(hass).async_get(entity_id)
    if (registered is None or registered.platform != DOMAIN or registered.domain != "climate"
            or registered.disabled_by is not None or registered.hidden_by is not None):
        connection.send_error(msg["id"], "not_found", "Heating Climate not found")
        return
    entry = hass.config_entries.async_get_entry(registered.config_entry_id)
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        connection.send_result(msg["id"], {"status": "unavailable", "curves": {}, "cycles": []})
    return runtime


def get_curve_memory(hass, connection, msg: dict) -> None:
    """Return committed bucket memory after shared ownership checks; related: panel.js."""
    runtime = _read_runtime(hass, connection, msg)
    if runtime is None:
        return
    connection.send_result(msg["id"], curve_snapshot(runtime))


async def get_curve_cycles(hass, connection, msg: dict) -> None:
    """Read recent accepted/rejected evidence without running learning or control; related: curve_storage.py."""
    runtime = _read_runtime(hass, connection, msg)
    if runtime is None:
        return
    store = getattr(runtime, "curve_store", None)
    if not getattr(runtime, "started", False) or store is None:
        connection.send_result(msg["id"], {"status": "unavailable", "cycles": []})
        return
    try:
        result = await store.read_cycles(msg.get("limit", 30), msg.get("curve_type"), msg.get("accepted"))
    except ValueError:
        connection.send_error(msg["id"], "invalid_format", "Invalid cycle query")
    except (sqlite3.Error, OSError):
        _LOGGER.warning("Cycle diagnostic read unavailable")
        connection.send_error(msg["id"], "unavailable", "Cycle storage unavailable")
    else:
        connection.send_result(msg["id"], result)


def register_curve_api(hass) -> None:
    """Register an authenticated HA WebSocket command once; related: panel.py."""
    import voluptuous as vol
    from homeassistant.components import websocket_api
    from homeassistant.core import callback
    from homeassistant.helpers import config_validation as cv

    data = hass.data.setdefault(DOMAIN, {})
    if data.get("curve_api_registered"):
        return
    handler = websocket_api.websocket_command({
        vol.Required("type"): f"{DOMAIN}/curve_memory",
        vol.Required("entity_id"): cv.entity_id,
    })(callback(get_curve_memory))
    websocket_api.async_register_command(hass, handler)
    cycle_handler = websocket_api.websocket_command({
        vol.Required("type"): f"{DOMAIN}/curve_cycles",
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("limit", default=30): vol.All(int, vol.Range(min=1, max=100)),
        vol.Optional("curve_type"): vol.In(CURVE_TYPES),
        vol.Optional("accepted"): bool,
    })(websocket_api.async_response(get_curve_cycles))
    websocket_api.async_register_command(hass, cycle_handler)
    data["curve_api_registered"] = True
