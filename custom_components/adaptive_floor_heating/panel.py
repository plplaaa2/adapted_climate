"""Serve the shared HA sidebar and curve API; related: __init__.py, curve_api.py, frontend/panel.js."""

from pathlib import Path
from hashlib import sha256

from .const import DOMAIN

PANEL_PATH = "adaptive-floor-heating"
MODULE_URL = "/adaptive_floor_heating_static/panel.js"


async def async_setup_panel(hass) -> None:
    """Register a local module, read-only curve API and shared sidebar panel."""
    from homeassistant.components import panel_custom
    from homeassistant.components.http import StaticPathConfig
    from .curve_api import register_curve_api

    register_curve_api(hass)
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("panel_registered"):
        return
    # Version the module by content so each UI edit bypasses the browser's module cache.
    # Related: frontend/panel.js; keep the static route and integration version stable.
    module_path = Path(__file__).parent / "frontend" / "panel.js"
    module_bytes = await hass.async_add_executor_job(module_path.read_bytes)
    module_version = sha256(module_bytes).hexdigest()[:16]
    if not data.get("panel_static_registered"):
        await hass.http.async_register_static_paths([
            StaticPathConfig(
                MODULE_URL, str(module_path), False
            )
        ])
        data["panel_static_registered"] = True
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name="adaptive-floor-heating-panel",
        sidebar_title="바닥난방",
        sidebar_icon="mdi:heating-coil",
        module_url=f"{MODULE_URL}?v={module_version}",
    )
    data["panel_registered"] = True
