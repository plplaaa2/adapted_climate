"""Expose read-only committed curve buckets; related: panel.py, curve_memory.py, runtime.py."""

import time

from .const import DOMAIN
from .curve_learning import CURVE_TYPES


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


def get_curve_memory(hass, connection, msg: dict) -> None:
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
    connection.send_result(msg["id"], curve_snapshot(runtime))


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
    data["curve_api_registered"] = True
