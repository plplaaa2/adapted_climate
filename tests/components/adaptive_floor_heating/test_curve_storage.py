"""Verify durable idempotent standards and raw TTL; related: curve_storage.py."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from custom_components.adaptive_floor_heating.curve_learning import CurveResult
from custom_components.adaptive_floor_heating.curve_storage import CurveStore


class CurveStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        hass = SimpleNamespace(config=SimpleNamespace(path=lambda *parts: str(root.joinpath(*parts))))
        self.store = CurveStore(hass, "entry-one")
        await self.store.open()

    async def asyncTearDown(self):
        self.folder.cleanup()

    async def test_duplicate_cycle_is_applied_once_and_survives_reopen(self):
        result = CurveResult(
            "same-cycle", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "BALANCED", 1, 601, 901, 1501,
            {0: 0.0, 1: 0.2, 2: 0.0}, 0.3, None,
        )
        self.assertTrue((await self.store.save(result))[0])
        self.assertFalse((await self.store.save(result))[0])
        self.assertEqual(self.store.model.buckets["WARM_HEATING"][1][1], 1)
        reopened = CurveStore(SimpleNamespace(config=SimpleNamespace(
            path=lambda *parts: str(Path(self.folder.name).joinpath(*parts)))), "entry-one")
        await reopened.open()
        self.assertEqual(reopened.model.buckets["WARM_HEATING"][1], (0.2, 1))
        self.assertEqual(reopened.model.residual["WARM_HEATING"], (0.3, 1))
        self.assertEqual(reopened.model.peak_delay["WARM_HEATING"], (5.0, 1))
        await reopened.cleanup()
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM cycles").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM cycle_buckets").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM curve_buckets").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
