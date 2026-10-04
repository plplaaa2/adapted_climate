"""Serve the shared HA sidebar and curve API; related: __init__.py, curve_api.py, frontend/panel.js."""

from pathlib import Path

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
    if not data.get("panel_static_registered"):
        await hass.http.async_register_static_paths([
            StaticPathConfig(
                MODULE_URL, str(Path(__file__).parent / "frontend" / "panel.js"), False
            )
        ])
        data["panel_static_registered"] = True
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name="adaptive-floor-heating-panel",
        sidebar_title="바닥난방",
        sidebar_icon="mdi:heating-coil",
        module_url=MODULE_URL,
    )
    data["panel_registered"] = True
