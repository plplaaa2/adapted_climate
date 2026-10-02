"""Verify durable idempotent standards and raw TTL; related: curve_storage.py."""

import sqlite3
from dataclasses import replace
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

    async def test_off_profiles_survive_raw_cleanup_and_duplicate_save(self):
        result = CurveResult(
            "off-profile", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "BALANCED", 0, 1800, 3600, 3900,
            {0: 0, 1: 0.1, 2: 0.2}, 0.6, None,
            off_profile={"duration": 30, "slope": 0.8, "rise": 0.6,
                         "points": [[0, 0], [5, 0.1], [30, 0.6]]},
        )
        for index in range(3):
            await self.store.save(replace(result, cycle_id=f"off-{index}"))
        await self.store.save(replace(result, cycle_id="off-2"))
        self.assertEqual(len(self.store.model.off_profiles["WARM_HEATING"]), 3)
        await self.store.cleanup()
        await self.store.open()
        prediction = self.store.model.predict_off_response("WARM_HEATING", 30, 0.8)
        self.assertAlmostEqual(prediction.rise, 0.6)
        self.assertAlmostEqual(dict(prediction.points)[5], 0.1)
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM cycle_buckets").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM off_response_profiles").fetchone()[0], 3)

    async def test_version_one_migration_preserves_data(self):
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE schema_meta SET value='1' WHERE key='version'")
            conn.execute("ALTER TABLE cycles DROP COLUMN preset")
            conn.execute("DROP TABLE off_response_profiles")
            conn.execute("INSERT INTO curve_buckets VALUES ('WARM_HEATING',0,0.1,4,'old')")
        await self.store.open()
        self.assertEqual(self.store.model.buckets["WARM_HEATING"][0], (0.1, 4))
        self.assertIsNone(self.store.model.predict_off_response("WARM_HEATING", 30, 0.8))
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM schema_meta").fetchone()[0], '2')

    async def test_profile_limit_and_corruption_are_isolated(self):
        result = CurveResult(
            "bounded", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "BALANCED", 0, 1800, 3600, 3900,
            {0: 0, 1: 0.1}, 0.6, None,
            off_profile={"duration": 30, "slope": 0.8, "rise": 0.6,
                         "points": [[0, 0], [30, 0.6]]},
        )
        for index in range(27):
            await self.store.save(replace(result, cycle_id=f"bounded-{index}"))
        self.assertEqual(len(self.store.model.off_profiles["WARM_HEATING"]), 24)
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM off_response_profiles").fetchone()[0], 24)
            conn.execute("UPDATE off_response_profiles SET payload='{}' WHERE cycle_id='bounded-26'")
        await self.store.open()
        self.assertEqual(len(self.store.model.off_profiles["WARM_HEATING"]), 23)
        self.assertEqual(self.store.model.accepted["WARM_HEATING"], 27)


if __name__ == "__main__":
    unittest.main()
