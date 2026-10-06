"""Verify durable idempotent standards and raw TTL; related: curve_storage.py."""

import sqlite3
import json
from dataclasses import replace
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.adaptive_floor_heating.curve_learning import CurveResult
from custom_components.adaptive_floor_heating.curve_storage import CurveStore


class CurveStorageTests(unittest.IsolatedAsyncioTestCase):
    # Schema 3 memory and schema 4 delayed profiles must coexist; related: curve_memory.py.
    async def test_delayed_profiles_survive_schema_three_migration_and_raw_cleanup(self):
        from custom_components.adaptive_floor_heating.curve_memory import CURRENT_MAX_AGE
        legacy = CurveResult(
            "legacy", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "BALANCED", 0, 1800, 3600, 3900,
            {0: 0, 1: 0.1}, 0.6, None,
            off_profile={"duration": 30, "slope": 0.8, "rise": 0.6, "points": [[0, 0], [30, 0.6]]},
        )
        await self.store.save(legacy)
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE schema_meta SET value='3' WHERE key='version'")
        await self.store.open()
        self.assertEqual(self.store.model.off_profiles["WARM_HEATING"][0], legacy.off_profile)
        context = {"start_temperature": 24, "on_delta": 0, "pre_slope": -0.1, "off_minutes": 240}
        delayed = replace(legacy, off_at=1800, peak_at=7800, ended_at=8400, residual_rise=0.7,
                          off_profile={"duration": 30, "slope": 0, "rise": 0.7,
                                       "points": [[0, 0], [10, -0.1], [30, 0], [100, 0.7]], "context": context})
        for i in range(8):
            self.assertTrue((await self.store.save(replace(delayed, cycle_id=f"delayed-{i}")))[0])
        await self.store.cleanup()
        await self.store.open()
        prediction = self.store.model.predict_off_response(
            "WARM_HEATING", 30, 0, 8401 + 2 * CURRENT_MAX_AGE, context=context
        )
        self.assertAlmostEqual(prediction.rise, 0.7)
        self.assertIn("long_term", self.store.model.prediction_source)
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0], '5')

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

    # Retain failed field evidence and separate raw expiry from missing legacy data; related: curve_api.py.
    async def test_cycle_evidence_filters_read_only_and_raw_expiry(self):
        result = CurveResult(
            "accepted", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "BALANCED", 100, 1900, 3700, 4000,
            {0: 0.0, 1: 0.2, 3: 0.1}, 0.6, None,
            start_temperature=23, off_temperature=25, peak_temperature=25.6, slope_at_off=0.8,
            peak_confirmation_drop=.3, peak_confirmation_reports=2, peak_confirmation_extra_drop=.1,
        )
        await self.store.save(result)
        failed = replace(result, cycle_id="failed", ended_at=4001, residual_rise=5.4, peak_temperature=30.4)
        self.assertEqual(await self.store.save(failed), (False, "INVALID_RESIDUAL_RISE"))
        before = self.store.model.memory["WARM_HEATING"].dump()
        with patch.object(self.store, "_connect", side_effect=AssertionError("read used write connection")):
            response = await self.store.read_cycles(1, "WARM_HEATING", False)
        self.assertEqual(response["total"], 1)
        cycle = response["cycles"][0]
        self.assertEqual(cycle["quality_reason"], "INVALID_RESIDUAL_RISE")
        self.assertEqual(cycle["bucket_count"], 3)
        self.assertEqual([row["index"] for row in cycle["buckets"]], [0, 1, 3])
        self.assertEqual(cycle["buckets"][0]["delta"], 0)
        self.assertEqual(cycle["heating_duration_minutes"], 30)
        self.assertEqual(cycle["peak_delay_minutes"], 30)
        check = next(row for row in cycle["analysis"]["checks"] if row["code"] == "INVALID_RESIDUAL_RISE")
        self.assertEqual((check["actual"], check["min"], check["max"], check["passed"]), (5.4, 0, 5, False))
        learning = cycle["analysis"]["learning"]
        self.assertFalse(learning["applied"])
        self.assertEqual(learning["before"], learning["after"])
        self.assertEqual(self.store.model.memory["WARM_HEATING"].dump(), before)
        accepted = (await self.store.read_cycles(30, accepted=True))["cycles"][0]
        policy = accepted["analysis"]["peak_confirmation"]
        self.assertEqual(policy["minimum_drop_c"], .2)
        self.assertEqual(policy["minimum_extra_drop_c"], .1)
        self.assertEqual(policy["maximum_wait_minutes"], 240)
        self.assertEqual(policy["observed_extra_drop_c"], .1)
        self.assertAlmostEqual(policy["observed_first_drop_c"], .2)
        self.assertEqual(accepted["analysis"]["learning"]["after"]["current"][0]["samples"], 1)
        self.assertEqual(accepted["analysis"]["learning"]["before"]["current"], [])
        await self.store.cleanup()
        cycle = (await self.store.read_cycles(1, accepted=False))["cycles"][0]
        self.assertEqual(cycle["raw_status"], "expired")
        self.assertEqual(cycle["bucket_count"], 3)
        self.assertEqual(cycle["buckets"], [])
        self.assertEqual(cycle["analysis"]["checks"], response["cycles"][0]["analysis"]["checks"])
        for arguments in ((0, None, None), (101, None, None), (True, None, None),
                          (1, "invalid", None), (1, None, 0)):
            with self.assertRaises(ValueError):
                await self.store.read_cycles(*arguments)

    async def test_schema_four_legacy_cycles_preserve_reasons_without_inventing_evidence(self):
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("INSERT INTO cycles (id,curve_type,start_reason,off_reason,end_reason,mode,"
                         "started_at,off_at,peak_at,ended_at,accepted,quality_reason,preset) VALUES "
                         "('legacy','WARM_HEATING','UNKNOWN','UNKNOWN','PEAK_TIMEOUT','HEAT',"
                         "'2026-10-05T20:00:00+00:00','2026-10-05T22:00:00+00:00',NULL,"
                         "'2026-10-06T01:00:00+00:00',0,'INCOMPLETE_PEAK','home')")
            conn.execute("UPDATE schema_meta SET value='4'")
            for field in ("start_temperature", "off_temperature", "peak_temperature", "slope_at_off",
                          "residual_rise", "peak_delay_minutes", "heating_duration_minutes",
                          "bucket_count", "analysis_json", "raw_pruned"):
                conn.execute(f"ALTER TABLE cycles DROP COLUMN {field}")
        await self.store.open()
        cycle = (await self.store.read_cycles())["cycles"][0]
        self.assertEqual(cycle["quality_reason"], "INCOMPLETE_PEAK")
        self.assertEqual(cycle["heating_duration_minutes"], 120)
        for field in ("analysis", "bucket_count", "peak_temperature", "peak_delay_minutes"):
            self.assertIsNone(cycle[field])
        self.assertEqual(cycle["raw_status"], "unavailable")

    async def test_learning_evidence_captures_promotions_and_duplicate_is_immutable(self):
        result = CurveResult("promotion", "COOLING", "THRESHOLD_START", "TARGET_REACHED",
                             "NEXT_ON", "HEAT", 100, 700, 1000, 2000, {0: 0, 1: -.1}, None, None)
        for index in range(8):
            await self.store.save(replace(result, cycle_id=f"promotion-{index}", ended_at=2000+index))
        rows = (await self.store.read_cycles())["cycles"]
        self.assertTrue(any(row["analysis"]["learning"]["promoted"] for row in rows))
        latest = rows[0]
        await self.store.save(replace(result, cycle_id=latest["id"], ended_at=9999))
        self.assertEqual((await self.store.read_cycles())["cycles"][0], latest)

    # A >180-minute peak must remain usable after persistence, migration and raw cleanup; related: curve_memory.py.
    async def test_late_peak_profiles_survive_restore_and_predict_with_four_hour_bound(self):
        result = CurveResult("late", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
                             "PEAK_CONFIRMED", "BALANCED", 0, 1800, 15300, 15900,
                             {0: 0, 1: .1}, .6, None,
                             off_profile={"duration":30,"slope":.8,"rise":.6,"points":[[0,0],[225,.6]]})
        for index in range(8):
            self.assertTrue((await self.store.save(replace(result, cycle_id=f"late-{index}")))[0])
        await self.store.cleanup()
        await self.store.open()
        prediction = self.store.model.predict_off_response("WARM_HEATING", 30, .8, now=15900)
        self.assertIsNotNone(prediction)
        self.assertAlmostEqual(prediction.peak_minutes, 225)
        self.assertTrue(self.store.model.memory["WARM_HEATING"].long_term)
        self.assertEqual(self.store.model.accepted["WARM_HEATING"], 8)
        too_late = replace(result, cycle_id="past-four-hours", peak_at=1800+241*60)
        self.assertEqual(await self.store.save(too_late), (False, "INVALID_PEAK_DELAY"))

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
        prediction = self.store.model.predict_off_response("WARM_HEATING", 30, 0.8, now=3900)
        self.assertAlmostEqual(prediction.rise, 0.6)
        from custom_components.adaptive_floor_heating.off_response import interpolate
        self.assertAlmostEqual(interpolate(prediction.points, 5), 0.1)
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
            self.assertEqual(conn.execute("SELECT value FROM schema_meta").fetchone()[0], '5')

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

    async def test_long_term_survives_reopen_raw_ttl_and_season(self):
        from custom_components.adaptive_floor_heating.curve_memory import CURRENT_MAX_AGE
        result = CurveResult(
            "season", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "BALANCED", 0, 1800, 3600, 3900,
            {0: 0, 1: 0.1, 2: 0.2}, 0.6, None,
            off_profile={"duration": 30, "slope": 0.8, "rise": 0.6,
                         "points": [[0, 0], [5, 0.1], [30, 0.6]]},
        )
        for i in range(8):
            await self.store.save(replace(result, cycle_id=f"season-{i}", ended_at=3900+i))
        memory = self.store.model.memory["WARM_HEATING"]
        before = memory.dump()
        self.assertTrue(memory.long_term)
        self.assertTrue(next(iter(memory.responses.values()))["long_term"])
        await self.store.save(replace(result, cycle_id="season-7", ended_at=3907))
        self.assertEqual(self.store.model.memory["WARM_HEATING"].dump(), before)
        await self.store.cleanup()
        await self.store.open()
        memory = self.store.model.memory["WARM_HEATING"]
        self.assertEqual(memory.dump(), before)
        future = 3907 + 4 * CURRENT_MAX_AGE
        self.assertEqual(memory.estimate(1, future)[2], "long_term")
        prediction = self.store.model.predict_off_response("WARM_HEATING", 30, 0.8, now=future)
        self.assertAlmostEqual(prediction.rise, 0.6)
        self.assertEqual(self.store.model.prediction_source, "WARM_HEATING:long_term")
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM cycle_buckets").fetchone()[0], 0)

    async def test_failed_transaction_does_not_update_memory_or_promotions(self):
        result = CurveResult(
            "failed-memory", "COOLING", "THRESHOLD_START", "TARGET_REACHED",
            "NEXT_ON", "BALANCED", 0, 600, 900, 2100, {0: 0, 1: -0.1}, None, None,
        )
        before = self.store.model.memory["COOLING"].dump()
        with patch.object(self.store, "_save_sync", side_effect=sqlite3.OperationalError("write failed")):
            with self.assertRaises(sqlite3.OperationalError):
                await self.store.save(result)
        self.assertEqual(self.store.model.memory["COOLING"].dump(), before)
        self.assertEqual(self.store.model.accepted["COOLING"], 0)

    async def test_corrupt_long_term_restores_valid_current(self):
        result = CurveResult(
            "corrupt", "COOLING", "THRESHOLD_START", "TARGET_REACHED",
            "NEXT_ON", "BALANCED", 0, 600, 900, 2100, {0: 0, 1: -0.1}, None, None,
        )
        for i in range(8):
            await self.store.save(replace(result, cycle_id=f"corrupt-{i}", ended_at=2100+i))
        data = self.store.model.memory["COOLING"].dump()
        data["long_term"]["1"][1] = None
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE curve_memory SET payload=? WHERE curve_type='COOLING'", (json.dumps(data),))
        await self.store.open()
        memory = self.store.model.memory["COOLING"]
        self.assertEqual(memory.long_term, {})
        self.assertEqual(memory.current[1].samples, 8)
        self.assertEqual(memory.estimate(1, 2107)[2], "current")

    async def test_schema_two_preserves_old_mean_with_unknown_variance(self):
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE schema_meta SET value='2'")
            conn.execute("DROP TABLE curve_memory")
            conn.execute("INSERT INTO curve_buckets VALUES ('COOLING',0,-0.1,50,'old')")
        await self.store.open()
        bucket = self.store.model.memory["COOLING"].current[0]
        self.assertEqual(bucket.mean, -0.1)
        self.assertIsNone(bucket.variance)
        self.assertEqual(bucket.samples, 0)
        self.assertEqual(bucket.confidence(100), 0)
        self.assertFalse(self.store.model.memory["COOLING"].long_term)

    async def test_migrated_off_profiles_do_not_claim_fresh_current_after_a_season(self):
        from custom_components.adaptive_floor_heating.curve_memory import CURRENT_MAX_AGE
        result = CurveResult(
            "legacy-off", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "BALANCED", 0, 1800, 3600, 3900,
            {0: 0, 1: 0.1}, 0.6, None,
            off_profile={"duration": 30, "slope": 0.8, "rise": 0.6,
                         "points": [[0, 0], [30, 0.6]]},
        )
        for i in range(3):
            await self.store.save(replace(result, cycle_id=f"legacy-{i}"))
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("DROP TABLE curve_memory")
            conn.execute("UPDATE schema_meta SET value='2'")
        await self.store.open()
        self.assertIsNotNone(self.store.model.predict_off_response("WARM_HEATING", 30, 0.8, now=3900))
        self.assertIsNone(self.store.model.predict_off_response(
            "WARM_HEATING", 30, 0.8, now=3900 + 4 * CURRENT_MAX_AGE))
        self.assertEqual(self.store.model.memory["WARM_HEATING"].long_term, {})


if __name__ == "__main__":
    unittest.main()
