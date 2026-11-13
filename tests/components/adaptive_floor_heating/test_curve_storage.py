"""Verify durable idempotent standards and raw TTL; related: curve_storage.py."""

import sqlite3
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.adaptive_floor_heating.curve_learning import CurveResult, CurveTracker
from custom_components.adaptive_floor_heating.curve_storage import CurveStore


class CurveStorageTests(unittest.IsolatedAsyncioTestCase):
    # Quarantine a late peak through migration and admit it only after independent corroboration.
    async def test_control_learning_hold_survives_reopen_and_requires_repeated_late_peaks(self):
        cycle = CurveResult(
            "late", "WARM_HEATING", "THRESHOLD_START", "PREDICTIVE_STOP",
            "PEAK_CONFIRMED", "BALANCED", 100, 1700, 13640, 14000,
            {0: 0, 1: .1}, 1.1, None, start_temperature=22.5, off_temperature=22.5,
            peak_temperature=23.6, outdoor_temperature=5.4,
        )
        comparison = {"off_at": cycle.off_at, "actual_peak": 23.6, "off_model": "hybrid",
                      "predictions": {"curve": 24}, "errors": {"curve": .4}}
        self.assertTrue((await self.store.save(cycle, comparison))[0])
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE schema_meta SET value='13' WHERE key='version'")
            conn.execute("ALTER TABLE cycles DROP COLUMN control_learning_hold")
        await self.store.open()
        row = (await self.store.read_cycles())["cycles"][0]
        self.assertEqual(row["control_learning_hold"], "UNSUPPORTED_LATE_PEAK")
        self.assertEqual(row["peak_temperature"], 23.6)
        self.assertEqual(self.store.model.peak_samples, [])
        self.assertEqual(self.store.model.runtime_samples, [])
        self.assertEqual(self.store.control_feedback, [])
        for index in (1, 2):
            shift = index * 86400
            await self.store.save(replace(cycle, cycle_id=f"late-{index}", started_at=cycle.started_at + shift,
                                          off_at=cycle.off_at + shift, peak_at=cycle.peak_at + shift,
                                          ended_at=cycle.ended_at + shift))
            self.assertEqual(len(self.store.model.peak_samples), 0 if index == 1 else 3)
        await self.store.open()
        self.assertEqual(len(self.store.model.runtime_samples), 3)
        self.assertEqual(self.store.control_feedback, [comparison])

    # Exercise all persisted learning paths, including rebuilding samples on reopen; related: runtime.py.
    async def test_sunrise_round_is_recorded_without_changing_shared_learning(self):
        ordinary = CurveResult("ordinary", "WARM_HEATING", "THRESHOLD_START", "PREDICTIVE_STOP",
                               "PEAK_CONFIRMED", "BALANCED", 100, 1900, 3700, 4000,
                               {0: 0, 1: .1}, 1.0, None, start_temperature=20,
                               off_temperature=20.2, peak_temperature=21.2, target_peak_temperature=21.2)
        ordinary_cooling = replace(ordinary, curve_type="COOLING", residual_rise=None,
                                   buckets={0: -.1, 1: -.1}, cooling_outdoor_groups={0: "NORMAL"})
        self.assertTrue((await self.store.save(ordinary))[0])
        self.assertTrue((await self.store.save(ordinary_cooling))[0])
        saving = replace(ordinary, cycle_id="saving", target_peak_temperature=21.3, sunrise_saving=True,
                         sunrise_prediction={"off_model": "hybrid", "predicted_peak": 21.3})
        cooling = replace(saving, curve_type="COOLING", end_reason="SUNRISE_COOLING_TARGET",
                          residual_rise=None, buckets={0: -.4, 1: -.4}, cooling_outdoor_groups={0: "NORMAL"})
        comparison = {"off_at": saving.off_at, "actual_peak": 21.2, "off_model": "hybrid",
                      "predictions": {"curve": 21.3}, "errors": {"curve": .1}}
        model = self.store.model
        before = {kind: memory.dump() for kind, memory in model.memory.items()}
        for result in (saving, cooling):
            self.assertEqual(await self.store.save(result, comparison), (False, "SUNRISE_SAVING"))
        self.assertEqual({kind: memory.dump() for kind, memory in self.store.model.memory.items()}, before)
        self.assertEqual(len(self.store.model.peak_samples), 1)
        self.assertEqual(len(self.store.model.runtime_samples), 1)
        self.assertEqual(self.store.model.shortfall_samples, [])
        await self.store.open()
        self.assertEqual(len(self.store.model.peak_samples), 1)
        self.assertEqual(len(self.store.model.runtime_samples), 1)
        self.assertEqual(self.store.model.shortfall_samples, [])
        self.assertEqual({kind: memory.dump() for kind, memory in self.store.model.memory.items()}, before)
        records = await self.store.read_cycles()
        self.assertEqual(records["accuracy"]["count"], 0)
        rows = {row["curve_type"]: row for row in records["cycles"] if row["id"] == "saving"}
        self.assertEqual(rows["WARM_HEATING"]["analysis"]["sunrise_prediction"], saving.sunrise_prediction)
        self.assertEqual(rows["WARM_HEATING"]["heating_outcome"], "missed")
        self.assertEqual(rows["COOLING"]["end_reason"], "SUNRISE_COOLING_TARGET")
        self.assertTrue(rows["COOLING"]["analysis"]["sunrise_saving"])

    # Upgrade real schema-8 columns in place and retain the distinct incomplete failure outcome.
    async def test_schema_eight_keeps_legacy_samples_and_restores_outcomes(self):
        legacy = CurveResult('legacy-eight', 'WARM_HEATING', 'THRESHOLD_START', 'PREDICTIVE_STOP',
                             'PEAK_CONFIRMED', 'ECO', 100, 1900, 3700, 4000, {0: 0, 1: .1}, .5,
                             None, start_temperature=22.5, off_temperature=22.5, peak_temperature=23)
        await self.store.save(legacy)
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            for column in ('target_peak_temperature', 'observed_peak_temperature', 'heating_outcome', 'outcome_weight'):
                conn.execute(f"ALTER TABLE cycles DROP COLUMN {column}")
            conn.execute("UPDATE schema_meta SET value='8'")
        await self.store.open()
        row = (await self.store.read_cycles(5))['cycles'][0]
        self.assertIsNone(row['target_peak_temperature'])
        self.assertIsNone(row['outcome_weight'])
        self.assertEqual(len(self.store.model.runtime_samples), 1)
        await self.store.save(replace(legacy, cycle_id='missed', target_peak_temperature=23.5,
                                      observed_peak_temperature=23, outdoor_temperature=7))
        tracker = CurveTracker()
        tracker.seed(False, 22.5, 5000)
        tracker.switch(True, 5000, start_reason='THRESHOLD_START', outdoor_temperature=7)
        tracker.switch(False, 5960, off_reason='PREDICTIVE_STOP', target_peak_temperature=23.5)
        tracker.report_temperature(22.6, 6560)
        tracker.report_temperature(22.5, 7160)
        tracker.switch(True, 7760, start_reason='THRESHOLD_START', target_peak_temperature=23.5)
        failure, = tracker.take_results()
        await self.store.save(failure)
        await self.store.save(failure)
        await self.store.open()
        rows = {x['id']: x for x in (await self.store.read_cycles(5))['cycles']}
        self.assertEqual(rows['missed']['outcome_weight'], .5)
        self.assertEqual(rows[failure.cycle_id]['heating_outcome'], 'restarted_below_target')
        self.assertEqual(rows[failure.cycle_id]['observed_peak_temperature'], 22.6)
        self.assertIsNone(rows[failure.cycle_id]['peak_temperature'])
        self.assertEqual(len(self.store.model.shortfall_samples), 2)
        self.assertEqual(self.store.model.shortfall_minimum(22.5, 23.5, 7, 8000), 35)
        self.assertEqual(self.store.model.shortfall_minimum(22.5, 24.5, 7, 8000), 0)

    # Upgrade existing installations without inventing past accuracy; related: curve_storage.py schema 8.
    async def test_schema_seven_adds_empty_accuracy_totals_without_rewriting_cycles(self):
        result = CurveResult('legacy-seven', 'WARM_HEATING', 'THRESHOLD_START', 'TARGET_REACHED',
                             'PEAK_CONFIRMED', 'BALANCED', 100, 1900, 3700, 4000,
                             {0: 0, 1: .1}, 1.0, None)
        await self.store.save(result)
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("DROP TABLE prediction_accuracy")
            conn.execute("UPDATE schema_meta SET value='7' WHERE key='version'")
        await self.store.open()
        response = await self.store.read_cycles(5, heating_only=True)
        self.assertEqual(response['cycles'][0]['id'], 'legacy-seven')
        self.assertNotIn('prediction_comparison', response['cycles'][0]['analysis'])
        self.assertEqual(response['accuracy'], {'count': 0, 'mean_absolute_error': None})

    # Accuracy totals outlive the visible five rounds and raw TTL; related: runtime.py, curve_api.py.
    async def test_prediction_history_and_all_time_absolute_error_are_durable(self):
        base = CurveResult('forecast', 'WARM_HEATING', 'THRESHOLD_START', 'TARGET_REACHED',
                           'PEAK_CONFIRMED', 'BALANCED', 100, 1900, 3700, 4000,
                           {0: 0, 1: .1}, 1.0, None, start_temperature=20,
                           off_temperature=20.2, peak_temperature=21.2)
        for index, delta in enumerate((.2, -.4, .6, -.8, 1.0, -1.2)):
            result = replace(base, cycle_id=f'forecast-{index}', started_at=100+index*5000,
                             off_at=1900+index*5000, peak_at=3700+index*5000, ended_at=4000+index*5000)
            comparison = {"off_at": result.off_at, "actual_peak": 21.2,
                          "off_model": ("runtime", "additive", "hybrid")[index % 3],
                          "predictions": {"curve": 21.2+delta}, "errors": {"curve": delta}}
            if index == 5:
                comparison["hybrid_summary_peak"] = 21.4
            await self.store.save(result, comparison)
            # Duplicate writes must not change the frozen forecast or cumulative counters.
            await self.store.save(result, {**comparison, "predictions": {"curve": 99}})
        missing = replace(base, cycle_id='missing', ended_at=40001)
        await self.store.save(missing)
        await self.store.save(replace(base, cycle_id='wrong-round', ended_at=40002),
                              {"off_at": 9999, "actual_peak": 21.2, "predictions": {"curve": 99}})
        await self.store.save(replace(base, cycle_id='cooling', curve_type='COOLING', ended_at=40003))
        await self.store.cleanup(now=datetime.fromtimestamp(40003, UTC) + timedelta(days=8))
        await self.store.open()
        response = await self.store.read_cycles(5, heating_only=True)
        self.assertEqual(len(response['cycles']), 5)
        self.assertNotIn('COOLING', [cycle['curve_type'] for cycle in response['cycles']])
        self.assertEqual(response['accuracy']['count'], 6)
        self.assertAlmostEqual(response['accuracy']['mean_absolute_error'], .7)
        self.assertEqual({key: stats['count'] for key, stats in response['accuracy_by_model'].items()},
                         {'runtime': 2, 'additive': 2, 'hybrid': 2})
        self.assertAlmostEqual(response['accuracy_by_model']['hybrid']['mean_absolute_error'], .9)
        paired = response['hybrid_summary_accuracy']
        self.assertEqual(paired['hybrid_summary']['count'], 1)
        self.assertAlmostEqual(paired['hybrid_summary']['mean_absolute_error'], .2)
        self.assertAlmostEqual(paired['hybrid_paired']['mean_absolute_error'], 1.2)
        self.assertNotIn('prediction_comparison', response['cycles'][0]['analysis'])
        frozen = next(cycle for cycle in response['cycles'] if cycle['id'] == 'forecast-5')
        self.assertAlmostEqual(frozen['analysis']['prediction_comparison']['predictions']['curve'], 20)
        self.assertEqual(frozen['raw_status'], 'expired')
        with self.assertRaises(ValueError):
            await self.store.read_cycles(5, heating_only='true')

    async def test_heating_metrics_use_recent_comparisons_and_restore_after_reload(self):
        base = CurveResult("metrics", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
                           "PEAK_CONFIRMED", "BALANCED", 100, 1900, 3700, 4000,
                           {0: 0, 1: .1}, 1.0, None, start_temperature=20,
                           off_temperature=20.2, peak_temperature=21.2)
        for index in range(12):
            start = 100 + index * 5000
            result = replace(base, cycle_id=f"metric-{index}", started_at=start,
                             off_at=start + (30 + index) * 60, peak_at=start + 3700,
                             ended_at=start + 4000)
            comparison = None
            if index < 11:
                delta = 0.3 if index in (0, 10) else -0.5
                comparison = {"off_at": result.off_at, "actual_peak": 21.2,
                              "predictions": {"curve": 21.2 + delta}}
            await self.store.save(result, comparison)
            if index == 10:
                self.assertAlmostEqual(self.store.heating_metrics["last_peak_error"], .3)
        metrics = self.store.heating_metrics
        self.assertEqual(metrics["last_heating_duration"], 41)
        self.assertEqual(metrics["average_heating_duration"], 36.5)
        self.assertEqual(metrics["duration_count"], 10)
        self.assertIsNone(metrics["last_peak_error"])
        self.assertEqual(metrics["comparison_count"], 10)
        self.assertAlmostEqual(metrics["mean_absolute_peak_error"], .48)
        self.assertEqual(metrics["peak_prediction_accuracy"], 10)
        await self.store.open()
        self.assertEqual(self.store.heating_metrics, metrics)
        await self.store.reset()
        self.assertEqual(self.store.heating_metrics, {})

    # New additive features reuse committed metadata and ignore duplicate/rejected records; related: peak_prediction.py.
    async def test_additive_samples_survive_reopen_without_duplicate_learning(self):
        result = CurveResult('sum', 'WARM_HEATING', 'THRESHOLD_START', 'TARGET_REACHED',
                             'PEAK_CONFIRMED', 'BALANCED', 100, 1900, 3700, 4000,
                             {0: 0, 1: .1}, 1.0, None, start_temperature=20,
                             off_temperature=20.2, peak_temperature=21.2, outdoor_temperature=-10)
        self.assertTrue((await self.store.save(result))[0])
        await self.store.save(result)
        self.assertEqual(len(self.store.model.peak_samples), 1)
        reopened = CurveStore(SimpleNamespace(config=SimpleNamespace(
            path=lambda *parts: str(Path(self.folder.name).joinpath(*parts)))), 'entry-one')
        await reopened.open()
        self.assertAlmostEqual(reopened.model.peak_samples[0]['minutes'], 30, places=5)
        self.assertEqual(reopened.model.peak_samples[0]['outdoor'], -10)

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
            self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0], '14')

    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        hass = SimpleNamespace(config=SimpleNamespace(path=lambda *parts: str(root.joinpath(*parts))))
        self.store = CurveStore(hass, "entry-one")
        await self.store.open()

    async def asyncTearDown(self):
        self.folder.cleanup()

    async def test_outdoor_cooling_is_durable_idempotent_and_resettable(self):
        result = CurveResult(
            "outdoor", "COOLING", "THRESHOLD_START", "TARGET_REACHED",
            "NEXT_ON", "AUTO", 1, 601, 901, 1501,
            {0: -0.1, 1: -0.2}, None, None,
            cooling_outdoor_groups={0: "NORMAL", 1: "COLD"},
        )
        self.assertTrue((await self.store.save(result))[0])
        self.assertFalse((await self.store.save(result))[0])
        await self.store.open()
        self.assertEqual(self.store.model.outdoor_cooling, {
            ("NORMAL", 0): (-0.1, 1), ("COLD", 1): (-0.2, 1),
        })
        self.assertEqual(self.store.model.outdoor_cooling_memory["COLD"].current[1].samples, 1)
        await self.store.reset()
        await self.store.open()
        self.assertEqual(self.store.model.outdoor_cooling, {})
        self.assertEqual(self.store.model.outdoor_cooling_memory["COLD"].current, {})

    async def test_legacy_hybrid_summary_migrates_once_and_keeps_measured_residuals(self):
        result = CurveResult(
            "hybrid-legacy", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "AUTO", 1, 2101, 2401, 2701,
            {0: .1, 1: .2}, 3.5, None, start_temperature=20, off_temperature=20.5,
            peak_temperature=24, outdoor_temperature=-2,
        )
        self.assertTrue((await self.store.save(result))[0])
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE schema_meta SET value='12' WHERE key='version'")
            conn.execute("UPDATE cycles SET heating_lag_minutes=0 WHERE id='hybrid-legacy'")
            conn.execute("DELETE FROM curve_memory WHERE curve_type IN ('hybrid_residual_measured','hybrid_duration')")
        for _ in range(2):
            await self.store.open()
            memory = self.store.model.hybrid_residual_memory["COLD"]
            self.assertEqual(memory.current[7].samples, 1)
            self.assertAlmostEqual(memory.current[7].mean, 3.5)
            self.assertAlmostEqual(self.store.model.hybrid_duration_memory["COLD"].current[7].mean, 35)
            self.assertIsNone(self.store.model.runtime_samples[0][2])

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
        await reopened.cleanup(now=datetime.fromtimestamp(1501, UTC) + timedelta(days=8))
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM cycles").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM cycle_buckets").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM curve_buckets").fetchone()[0], 3)

    async def test_runtime_peak_learning_survives_reopen_even_when_five_minute_curve_is_rejected(self):
        result = CurveResult(
            "runtime-sample", "WARM_HEATING", "UNKNOWN", "MANUAL_STOP",
            "PEAK_CONFIRMED", "BALANCED", 100, 1900, 3700, 4000,
            {0: 0.2}, 0.5, None, start_temperature=20, off_temperature=21,
            peak_temperature=22.5,
        )
        self.assertEqual((await self.store.save(result)), (False, "INSUFFICIENT_BUCKETS"))
        self.assertEqual(self.store.model.runtime_samples, [(30.0, 2.5, None, 4000)])
        reopened = CurveStore(SimpleNamespace(config=SimpleNamespace(
            path=lambda *parts: str(Path(self.folder.name).joinpath(*parts)))), "entry-one")
        await reopened.open()
        self.assertAlmostEqual(reopened.model.runtime_samples[0][0], 30, places=5)
        self.assertAlmostEqual(reopened.model.runtime_samples[0][1], 2.5, places=5)
        self.assertIsNone(reopened.model.runtime_samples[0][2])
        self.assertEqual(reopened.model.runtime_samples[0][3], 4000)
        interrupted = replace(result, cycle_id="heater-unavailable", ended_at=4001,
                              invalid_reason="HEATER_UNAVAILABLE")
        self.assertEqual(await reopened.save(interrupted), (False, "HEATER_UNAVAILABLE"))
        self.assertEqual(len(reopened.model.runtime_samples), 1)
        await reopened.open()
        self.assertEqual(len(reopened.model.runtime_samples), 1)

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
        self.assertEqual(policy["maximum_wait_minutes"], 360)
        self.assertEqual(policy["observed_extra_drop_c"], .1)
        self.assertAlmostEqual(policy["observed_first_drop_c"], .2)
        self.assertEqual(accepted["analysis"]["learning"]["after"]["current"][0]["samples"], 1)
        self.assertEqual(accepted["analysis"]["learning"]["before"]["current"], [])
        saved_promotion = learning["after"]["promotion"]
        self.assertEqual(saved_promotion["first_blocker"]["reasons"], ["INSUFFICIENT_SAMPLES"])
        await self.store.cleanup(now=datetime.fromtimestamp(failed.ended_at, UTC) + timedelta(days=8))
        cycle = (await self.store.read_cycles(1, accepted=False))["cycles"][0]
        self.assertEqual(cycle["raw_status"], "expired")
        self.assertEqual(cycle["bucket_count"], 3)
        self.assertEqual(cycle["buckets"], [])
        self.assertEqual(cycle["analysis"]["checks"], response["cycles"][0]["analysis"]["checks"])
        self.assertEqual(cycle["analysis"]["learning"]["after"]["promotion"], saved_promotion)
        for arguments in ((0, None, None), (101, None, None), (True, None, None),
                          (1, "invalid", None), (1, None, 0)):
            with self.assertRaises(ValueError):
                await self.store.read_cycles(*arguments)

    async def test_cycle_pages_keep_a_stable_snapshot_while_new_cycles_arrive(self):
        base = CurveResult("page-base", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
                           "PEAK_CONFIRMED", "BALANCED", 100, 200, 300, 400,
                           {0: 0, 1: .1}, .2, None)
        for index in range(7):
            await self.store.save(replace(base, cycle_id=f"page-{index}", ended_at=400+index))
        expected = (await self.store.read_cycles(100))["cycles"]
        first = await self.store.read_cycles(3)
        self.assertEqual(first["total"], 7)
        self.assertEqual(first["offset"], 0)
        self.assertIsInstance(first["snapshot"], int)
        await self.store.save(replace(base, cycle_id="arrived-during-export", ended_at=9999))
        second = await self.store.read_cycles(3, offset=3, snapshot=first["snapshot"])
        third = await self.store.read_cycles(3, offset=6, snapshot=first["snapshot"])
        exported = first["cycles"] + second["cycles"] + third["cycles"]
        self.assertEqual([row["id"] for row in exported], [row["id"] for row in expected])
        self.assertEqual([row["id"] for row in exported].count("arrived-during-export"), 0)
        self.assertEqual((second["total"], third["total"]), (7, 7))
        self.assertEqual((second["snapshot"], third["snapshot"]), (first["snapshot"], first["snapshot"]))
        for arguments in ((1, None, None, -1, None), (1, None, None, False, None),
                          (1, None, None, 0, -1)):
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
    async def test_late_peak_profiles_survive_restore_and_predict_with_six_hour_bound(self):
        result = CurveResult("late", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
                             "PEAK_CONFIRMED", "BALANCED", 0, 1800, 15300, 15900,
                             {0: 0, 1: .1}, .6, None,
                             off_profile={"duration":30,"slope":.8,"rise":.6,"points":[[0,0],[225,.6]]})
        for index in range(8):
            self.assertTrue((await self.store.save(replace(result, cycle_id=f"late-{index}")))[0])
        await self.store.cleanup(now=datetime.fromtimestamp(15900, UTC) + timedelta(days=8))
        await self.store.open()
        prediction = self.store.model.predict_off_response("WARM_HEATING", 30, .8, now=15900)
        self.assertIsNotNone(prediction)
        self.assertAlmostEqual(prediction.peak_minutes, 225)
        self.assertTrue(self.store.model.memory["WARM_HEATING"].long_term)
        self.assertEqual(self.store.model.accepted["WARM_HEATING"], 8)
        too_late = replace(result, cycle_id="past-six-hours", peak_at=1800+361*60)
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
        await self.store.cleanup(now=datetime.fromtimestamp(3900, UTC) + timedelta(days=8))
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
            self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0], '14')

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

    async def test_cycle_retention_preserves_long_term_and_lifetime_totals(self):
        now = datetime(2026, 11, 10, tzinfo=UTC)
        boundary = (now - timedelta(days=30)).timestamp()
        base = CurveResult(
            "expired", "WARM_HEATING", "THRESHOLD_START", "TARGET_REACHED",
            "PEAK_CONFIRMED", "AUTO", boundary - 4000, boundary - 1900,
            boundary - 1200, boundary - 1, {0: .1, 1: .1}, .6, None,
            start_temperature=22, off_temperature=22.5, peak_temperature=23.1, outdoor_temperature=-2,
        )
        for index in range(8):
            await self.store.save(replace(base, cycle_id=f"expired-{index}"), {
                "off_at": base.off_at, "actual_peak": 23.1, "predictions": {"curve": 23.2},
            })
        await self.store.save(replace(base, cycle_id="boundary", ended_at=boundary))
        await self.store.cleanup(now=now)
        for _ in range(2):
            data = await self.store.read_cycles()
            self.assertEqual([row["id"] for row in data["cycles"]], ["boundary"])
            self.assertEqual(data["cycles"][0]["raw_status"], "expired")
            self.assertEqual(data["accuracy"]["count"], 8)
            model = self.store.model
            self.assertEqual(model.accepted["WARM_HEATING"], 9)
            self.assertEqual(len(model.runtime_samples), 1)
            self.assertEqual(len(model.peak_samples), 1)
            self.assertIn(7, model.runtime_rise_memory["COLD"].long_term)
            self.assertIn(7, model.hybrid_residual_memory["COLD"].long_term)
            self.assertAlmostEqual(model.runtime_duration_memory["COLD"].long_term[7].mean, 35)
            self.assertAlmostEqual(model.hybrid_duration_memory["COLD"].long_term[7].mean, 35)
            await self.store.open()
        self.assertFalse((await self.store.save(replace(base, cycle_id="expired-0")))[0])
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM cycle_buckets").fetchone()[0], 0)
        snapshot = data["snapshot"]
        await self.store.cleanup(now=now + timedelta(days=1))
        self.assertEqual((await self.store.read_cycles())["total"], 0)
        await self.store.save(replace(base, cycle_id="new", ended_at=now.timestamp()))
        self.assertEqual((await self.store.read_cycles())["total"], 1)
        self.assertEqual((await self.store.read_cycles(snapshot=snapshot))["total"], 0)

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
