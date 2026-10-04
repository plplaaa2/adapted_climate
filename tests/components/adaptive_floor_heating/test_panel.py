"""Check shared registration and recoverable setup failures; related: panel.py."""

from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch

from custom_components.adaptive_floor_heating.panel import async_setup_panel, MODULE_URL
from custom_components.adaptive_floor_heating.const import DOMAIN


class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_is_shared_and_static_file_exists(self):
        await self.check_registration(fail_first=False)

    async def test_failed_panel_registration_reuses_registered_static_path(self):
        await self.check_registration(fail_first=True)

    async def check_registration(self, fail_first):
        register = AsyncMock(side_effect=[ValueError("temporary failure"), None] if fail_first else None)
        components = ModuleType("homeassistant.components")
        components.panel_custom = SimpleNamespace(async_register_panel=register)
        http = ModuleType("homeassistant.components.http")
        http.StaticPathConfig = lambda url, path, cache: SimpleNamespace(url=url, path=path, cache=cache)
        hass = SimpleNamespace(data={}, http=SimpleNamespace(async_register_static_paths=AsyncMock()))
        with patch.dict(sys.modules, {"homeassistant.components": components, "homeassistant.components.http": http}):
            if fail_first:
                with self.assertRaises(ValueError):
                    await async_setup_panel(hass)
                self.assertFalse(hass.data[DOMAIN].get("panel_registered", False))
            await async_setup_panel(hass)
            await async_setup_panel(hass)
        hass.http.async_register_static_paths.assert_awaited_once()
        config = hass.http.async_register_static_paths.call_args.args[0][0]
        self.assertEqual(config.url, MODULE_URL)
        self.assertTrue(Path(config.path).is_file())
        self.assertFalse(config.cache)
        self.assertEqual(register.await_count, 2 if fail_first else 1)
        self.assertEqual(register.call_args.kwargs["webcomponent_name"], "adaptive-floor-heating-panel")
        self.assertEqual(register.call_args.kwargs["sidebar_title"], "바닥난방")
