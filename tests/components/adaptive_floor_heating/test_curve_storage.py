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
from custom_components.adaptive_floor_heating.curve_storage import (
    FORECAST_HISTORY_BACKFILL, HEATER_HISTORY_BACKFILL, CurveStore,
)


class CurveStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_sunrise_runtime_retains_confirmed_shortfall_and_excludes_daylight_peak(self):
        start = datetime.now(UTC).timestamp() - 7200
        saving = CurveResult(
            "saving-runtime", "SUNRISE_HEATING", "THRESHOLD_START", "PREDICTIVE_STOP",
            "PEAK_CONFIRMED", "BALANCED", start, start + 1200, start + 1800, start + 2400,
            {0: .1, 1: .1}, None, None, start_temperature=20, off_temperature=20.1,
            peak_temperature=20.3, target_peak_temperature=20.5,
        )
        await self.store.save(saving)
        await self.store.save(replace(saving, cycle_id="daylight-runtime",
                                      sunrise_peak_excess={"sunrise_at": start + 1500}))
        await self.store.open()
        self.assertEqual(self.store.model.runtime_samples, [])
        self.assertEqual(self.store.model.runtime_average_samples, [])
        model = self.store.sunrise_runtime_model
        self.assertEqual(len(model.runtime_samples), 1)
        self.assertEqual(model.runtime_average_samples, [20])
        self.assertEqual(model.runtime_accepted_cycles, 1)
        await self.store.reset()
        await self.store.open()
        self.assertEqual(self.store.sunrise_runtime_model.runtime_samples, [])
        self.assertEqual(self.store.sunrise_runtime_model.runtime_average_samples, [])

    # Verify migration boundaries, durable learning and repeatability; related: curve_storage.py, runtime.py.
    async def test_migrate_legacy_sunrise_saving(self):
        base = datetime.now(UTC).replace(hour=5, minute=0, second=0, microsecond=0).timestamp() - 86400
        saving = CurveResult(
            "recover", "WARM_HEATING", "THRESHOLD_START", "PREDICTIVE_STOP",
            "PEAK_CONFIRMED", "BALANCED", base - 1800, base, base + 900, base + 1200,
            {0: .1, 1: .1}, .4, None, start_temperature=20, off_temperature=20.2,
            peak_temperature=20.6, target_peak_temperature=20.5, sunrise_saving=True,
        )
        comparison = {"off_at": base, "actual_peak": 20.6, "off_model": "hybrid",
                      "predictions": {"curve": 20.5}, "errors": {"curve": -.1}}
        cases = (
            saving,
            replace(saving, cycle_id="inside", started_at=base, off_at=base + 600),
            replace(saving, cycle_id="off-at-end", off_at=base + 3600,
                    peak_at=base + 3900, ended_at=base + 4200),
            replace(saving, cycle_id="incomplete", peak_at=None, peak_temperature=None,
                    end_reason="PEAK_TIMEOUT", invalid_reason="INCOMPLETE_PEAK"),
            replace(saving, cycle_id="invalid", invalid_reason="SENSOR_UNAVAILABLE"),
            replace(saving, cycle_id="away", preset="away"),
            replace(saving, cycle_id="expired"),
            replace(saving, curve_type="COOLING", end_reason="SUNRISE_COOLING_TARGET", residual_rise=None),
        )
        for result in cases:
            self.assertEqual(await self.store.save(result, comparison if result is saving else None),
                             (False, "SUNRISE_SAVING"))
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE cycles SET raw_pruned=1 WHERE id='expired'")
            conn.execute("DELETE FROM cycle_buckets WHERE cycle_id='expired'")

        def window(at):
            return base <= at < base + 3600, None

        schedule = {"start": "05:00:00", "end": "06:00:00", "time_zone": "UTC"}
        self.assertEqual(await self.store.migrate_sunrise_saving(window, schedule), 2)
        self.assertEqual(await self.store.migrate_sunrise_saving(window, schedule), 0)
        await self.store.open()
        self.assertEqual(await self.store.migrate_sunrise_saving(window, schedule), 0)
        records = await self.store.read_cycles()
        rows = {(row["id"], row["curve_type"]): row for row in records["cycles"]}
        recovered = rows["recover", "WARM_HEATING"]
        self.assertTrue(recovered["accepted"])
        self.assertEqual(recovered["quality_reason"], "ACCEPTED")
        proof = recovered["analysis"]["sunrise_saving_migration"]
        self.assertEqual(proof["schedule"], schedule)
        self.assertEqual(proof["original"]["quality_reason"], "SUNRISE_SAVING")
        self.assertEqual(rows["away", "WARM_HEATING"]["quality_reason"], "AWAY_CYCLE")
        for result in cases:
            if result.cycle_id not in ("recover", "away") or result.curve_type == "COOLING":
                self.assertEqual(rows[result.cycle_id, result.curve_type]["quality_reason"], "SUNRISE_SAVING")
        self.assertEqual(self.store.model.buckets["WARM_HEATING"], {0: (.1, 1), 1: (.1, 1)})
        self.assertEqual(self.store.model.runtime_average_samples, [30])
        self.assertEqual(len(self.store.model.peak_samples), 1)
        self.assertEqual(self.store.control_feedback, [comparison])
        self.assertEqual(records["accuracy"]["count"], 1)

        hold = {"started_at": saving.started_at, "off_at": saving.off_at, "peak_at": saving.peak_at,
                "ended_at": saving.ended_at, "completed": True, "exclusion_reason": None}
        completed = replace(saving, cycle_id="completed-cooling", curve_type="COOLING",
                            end_reason="TARGET_REACHED", buckets={0: -.1, 1: -.2},
                            residual_rise=None, heating_hold=hold)
        no_hold = replace(completed, cycle_id="missing-hold", heating_hold=None)
        await self.store.save(completed)
        await self.store.save(no_hold)
        self.assertEqual(await self.store.migrate_sunrise_saving(window, schedule, cooling=True), 1)
        await self.store.open()
        self.assertEqual(await self.store.migrate_sunrise_saving(window, schedule, cooling=True), 0)
        cooling = {row["id"]: row for row in (await self.store.read_cycles())["cycles"]
                   if row["curve_type"] == "COOLING"}
        self.assertTrue(cooling["completed-cooling"]["accepted"])
        self.assertEqual(cooling["completed-cooling"]["analysis"]["sunrise_saving_migration"]
                         ["original"]["quality_reason"], "SUNRISE_SAVING")
        self.assertEqual(cooling["missing-hold"]["quality_reason"], "SUNRISE_SAVING")
        self.assertEqual(cooling["recover"]["quality_reason"], "SUNRISE_SAVING")
        self.assertEqual(self.store.model.buckets["COOLING"], {0: (-.1, 1), 1: (-.2, 1)})
        self.assertEqual(self.store.model.runtime_average_samples, [30])

    # The window contains ten admitted records, not ten attempts; related: curve_storage.py.
    async def test_runtime_average_window_keeps_latest_ten_eligible_cycles(self):
        start = datetime.now(UTC).timestamp() - 20 * 86400
        for index, minutes in enumerate([60] * 11 + [10, 36]):
            at = start + index * 86400
            result = CurveResult(
                f"window-{index}", "WARM_HEATING", "THRESHOLD_START", "PREDICTIVE_STOP",
                "PEAK_CONFIRMED", "AUTO", at, at + minutes * 60,
                at + minutes * 60 + 300, at + minutes * 60 + 900,
                {0: .1, 1: .1}, .2, None, start_temperature=20,
                off_temperature=20.8, peak_temperature=21,
            )
            await self.store.save(result)
        self.assertEqual(self.store.model.runtime_average_samples, [36] + [60] * 9)
        self.assertEqual(self.store.model.runtime_accepted_cycles, 13)

    # Short accepted cycles still train forecasts, but cannot ratchet the durable mean down.
    # Related: curve_storage.py runtime_average and curve_learning.py runtime_shutdown_limits.
    async def test_runtime_average_excludes_short_cycles_and_survives_cleanup_reset(self):
        start = datetime.now(UTC).timestamp() - 40 * 86400
        for index, minutes in enumerate((60, 10, 36)):
            at = start + index * 86400
            result = CurveResult(
                f"average-{index}", "WARM_HEATING", "THRESHOLD_START", "PREDICTIVE_STOP",
                "PEAK_CONFIRMED", "AUTO", at, at + minutes * 60,
                at + minutes * 60 + 300, at + minutes * 60 + 900,
                {0: .1, 1: .1}, .2, None, start_temperature=20,
                off_temperature=20.8, peak_temperature=21, outdoor_temperature=5,
            )
            self.assertTrue((await self.store.save(result))[0])
            await self.store.save(result)
        self.assertEqual(self.store.model.runtime_average_samples, [36, 60])
        self.assertEqual(len(self.store.model.runtime_samples), 3)
        self.assertEqual(self.store.model.runtime_accepted_cycles, 3)
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute(
                "SELECT admission_floor,included FROM runtime_average WHERE cycle_id='average-1'"
            ).fetchone(), (36, 0))
        await self.store.cleanup(now=datetime.now(UTC))
        await self.store.open()
        self.assertEqual(self.store.model.runtime_average_samples, [36, 60])
        await self.store.reset()
        await self.store.open()
        self.assertEqual(self.store.model.runtime_average_samples, [])
        self.assertEqual(self.store.model.runtime_accepted_cycles, 0)

    async def test_hold_records_survive_curve_retention_and_reopen(self):
        now = datetime.now(UTC)
        start = (now - timedelta(days=35)).timestamp()
        tracker = CurveTracker()
        tracker.seed(False, 22, start - 1)
        tracker.switch(True, start, outdoor_temperature=5, hold_settings={"target_temperature": 23})
        tracker.report_temperature(23.2, start + 100)
        tracker.switch(False, start + 200, response_observed=True)
        tracker.report_temperature(23.6, start + 300)
        tracker.report_temperature(23.3, start + 600)
        tracker.report_temperature(23.2, start + 900)
        tracker.report_temperature(23, start + 12300, outdoor_temperature=3)
        results = tracker.take_results()
        for result in results:
            await self.store.save(result)
        await self.store.save(results[-1])
        await self.store.cleanup(now=now)
        await self.store.open()
        self.assertEqual((await self.store.read_cycles())["total"], 0)
        page = await self.store.read_heating_holds(10, 0, None)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["rows"][0]["outdoor_end"], 3)
        self.assertEqual(page["rows"][0]["hold_seconds"], 12300)

    # Export the extra evidence with the original hold without adding to the model; related: curve_learning.py.
    async def test_below_target_cooling_attaches_to_original_hold(self):
        start = datetime.now(UTC).timestamp()
        tracker = CurveTracker()
        tracker.seed(False, 22, start - 1)
        tracker.switch(True, start, hold_settings={"target_temperature": 23, "cold_tolerance": .5})
        tracker.report_temperature(23.2, start + 100)
        tracker.switch(False, start + 200, response_observed=True)
        for seconds, temperature in ((300, 23.6), (600, 23.3), (900, 23.2), (1200, 23), (1500, 22.5)):
            tracker.report_temperature(temperature, start + seconds)
        for result in tracker.take_results():
            await self.store.save(result)
        (cycle_id, record), = tracker.take_below_target_cooling()
        learned = dict(self.store.model.buckets["COOLING"])
        await self.store.save_below_target_cooling(cycle_id, record)
        await self.store.save_below_target_cooling(cycle_id, {**record, "end_reason": "NEXT_ON"})
        await self.store.open()
        page = await self.store.read_heating_holds(10, 0, None)
        hold, = page["rows"]
        self.assertEqual(hold["cycle_id"], cycle_id)
        self.assertEqual(hold["hold_seconds"], 1200)
        self.assertEqual(hold["below_target_cooling"]["end_reason"], "LOWER_REACHED")
        self.assertEqual(hold["below_target_cooling"]["samples"][-1]["temperature"], 22.5)
        self.assertEqual(self.store.model.buckets["COOLING"], learned)

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

    async def test_off_long_term_requires_distinct_matching_cycles(self):
        base = CurveResult("season", "WARM_HEATING", "THRESHOLD_START", "PREDICTIVE_STOP",
                           "PEAK_CONFIRMED", "AUTO", 100, 2200, 4000, 5000,
                           {0: 0, 1: .1}, .6, None, start_temperature=22,
                           off_temperature=22.5, peak_temperature=23.1, outdoor_temperature=-2,
                           outdoor_mean=2, outdoor_end=-2)
        for index in range(6):
            await self.store.save(replace(base, cycle_id=f"duplicate-{index}"))
        self.assertEqual(self.store.model.hybrid_residual_memory["-5"].long_term, {})
        for index in range(1, 7):
            shift = index * 86400
            await self.store.save(replace(base, cycle_id=f"independent-{index}",
                                          started_at=base.started_at+shift, off_at=base.off_at+shift,
                                          peak_at=base.peak_at+shift, ended_at=base.ended_at+shift))
        await self.store.open()
        self.assertIn(7, self.store.model.hybrid_residual_memory["-5"].long_term)
        self.assertIn(7, self.store.model.runtime_rise_memory["0"].long_term)

    async def test_sunrise_learning_survives_reopen_without_entering_control_training(self):
        tracker = CurveTracker()
        tracker.seed(False, 22.5, 0)
        tracker.switch(True, 60)
        tracker.report_temperature(22.65, 360)
        tracker.report_temperature(22.8, 660)
        tracker.switch(False, 960, off_reason="PREDICTIVE_STOP",
                       target_peak_temperature=23, sunrise_saving=True)
        tracker.report_temperature(23.05, 1260)
        tracker.report_temperature(22.9, 1560)
        tracker.report_temperature(22.75, 1860)
        original, saving = tracker.take_results()
        self.assertEqual(saving.curve_type, "SUNRISE_HEATING")
        self.assertAlmostEqual(saving.residual_rise, .25)
        self.assertEqual((await self.store.save(original))[1], "SUNRISE_SAVING")
        self.assertTrue((await self.store.save(saving))[0])
        from custom_components.adaptive_floor_heating.curve_learning import SolarGainObservation
        solar = SolarGainObservation(2000)
        for i, temp in enumerate((22, 22.2, 22.5, 22.35, 22.2) + (22.2,) * 10):
            confirmed = solar.sample(solar.starts_at + i * 300, temp, 5 + i, 1)
        self.assertTrue(confirmed)
        self.assertTrue((await self.store.save(solar.result(9800, "auto", "home")))[0])
        await self.store.open()
        self.assertEqual(self.store.model.runtime_samples, [])
        self.assertEqual(self.store.model.peak_samples, [])
        self.assertEqual(self.store.model.shortfall_samples, [])
        self.assertEqual(self.store.model.runtime_accepted_cycles, 0)
        self.assertEqual(self.store.model.residual["SUNRISE_HEATING"][1], 1)
        for kind in ("SOLAR_GAIN", "SUNRISE_HEATING"):
            self.assertEqual(self.store.model.accepted[kind], 1)
        row, = (await self.store.read_cycles(curve_type="SOLAR_GAIN"))["cycles"]
        self.assertEqual(len(row["analysis"]["solar_samples"]), 15)
        self.assertEqual(len(row["buckets"]), 2)

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
        # Later sunlight updates the stored diagnostic without changing the admitted peak.
        excess = {"threshold": .5, "predicted_peak": 21.2, "sunrise_at": 4100,
                  "first_at": 4300, "first_temperature": 21.7, "maximum_excess": .5}
        await self.store.update_sunrise_excess(ordinary.cycle_id, ordinary.curve_type, excess)
        records = await self.store.read_cycles()
        saved = next(row for row in records["cycles"] if row["curve_type"] == "WARM_HEATING")
        self.assertTrue(saved["accepted"])
        self.assertEqual(saved["analysis"]["sunrise_peak_excess"], excess)
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
                comparison["off_control"] = {"action": "early_off", "reason": None, "comparable_cycles": 3,
                                             "planned_minutes": 35, "forecast_available": True}
            await self.store.save(result, comparison)
            # Duplicate writes must not change the frozen forecast or cumulative counters.
            await self.store.save(result, {**comparison, "predictions": {"curve": 99}})
        missing = replace(base, cycle_id='missing', ended_at=40001)
        accepted, _ = await self.store.save(missing, {
            "off_at": missing.off_at, "actual_peak": 21.2, "predictions": {}, "errors": {},
            "off_control": {"action": "base_control", "reason": "INSUFFICIENT_HYBRID_EVIDENCE",
                            "comparable_cycles": 0, "planned_minutes": None, "forecast_available": False},
        })
        self.assertTrue(accepted)
        await self.store.save(replace(base, cycle_id='wrong-round', ended_at=40002),
                              {"off_at": 9999, "actual_peak": 21.2, "predictions": {"curve": 99}})
        await self.store.save(replace(base, cycle_id='cooling', curve_type='COOLING', ended_at=40003))
        await self.store.cleanup(now=datetime.fromtimestamp(40003, UTC) + timedelta(days=8))
        await self.store.open()
        response = await self.store.read_cycles(5, heating_only=True)
        self.assertEqual(len(response['cycles']), 5)
        self.assertNotIn('COOLING', [cycle['curve_type'] for cycle in response['cycles']])
        self.assertEqual(response['accuracy']['count'], 6)
        self.assertEqual(response['residual_coverage'], {'predicted': 1, 'withheld': 1})
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
        self.assertIsNone(reopened.model.peak_samples[0]["outdoor"])
        rows = await reopened.outdoor_migration_rows()
        self.assertEqual(await reopened.save_outdoor_migration([(rows[0], -4, -10)]), 1)
        self.assertEqual(await reopened.save_outdoor_migration([(rows[0], -4, -10)]), 0)
        await reopened.open()
        self.assertEqual(await reopened.outdoor_migration_rows(), [])
        self.assertEqual(reopened.model.peak_samples[0]["outdoor"], -10)
        self.assertEqual(reopened.model.runtime_sample_weather, {reopened.model.runtime_samples[0]: -4})
        cycle = (await reopened.read_cycles())["cycles"][0]
        self.assertEqual((cycle["outdoor_temperature"], cycle["outdoor_mean"], cycle["outdoor_end"]), (-10, -4, -10))

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
            self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0], '19')

    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        hass = SimpleNamespace(config=SimpleNamespace(path=lambda *parts: str(root.joinpath(*parts))))
        self.store = CurveStore(hass, "entry-one")
        await self.store.open()

    async def asyncTearDown(self):
        self.folder.cleanup()

    async def test_operation_history_import_is_idempotent_and_seeds_period(self):
        events = [("heater", 10, {"on": True}), ("heater", 20, {"on": None}),
                  ("off_prediction", 21, {"off_at": 21}), ("heater", 30, {"on": False})]
        await self.store.save_operation_history(events, migration=HEATER_HISTORY_BACKFILL)
        await self.store.save_operation_history([("heater", 22, {"on": True})], migration=HEATER_HISTORY_BACKFILL)
        await self.store.open()
        self.assertTrue(await self.store.operation_backfilled(HEATER_HISTORY_BACKFILL))
        rows = await self.store.read_operation_history(15, 25)
        self.assertEqual([(row["kind"], row["at"], row["data"]) for row in rows], events[:3])
        await self.store.reset()
        rows = await self.store.read_operation_history(15, 35)
        self.assertEqual([row["at"] for row in rows], [10, 20, 30])

    async def test_operation_migration_marks_empty_success_and_rolls_back_failure(self):
        with self.assertRaises(ValueError):
            await self.store.save_operation_history(
                [("off_prediction", 20, {"invalid": float("nan")})], migration=FORECAST_HISTORY_BACKFILL,
            )
        self.assertFalse(await self.store.operation_backfilled(FORECAST_HISTORY_BACKFILL))
        await self.store.save_operation_history([], migration=FORECAST_HISTORY_BACKFILL)
        await self.store.open()
        self.assertTrue(await self.store.operation_backfilled(FORECAST_HISTORY_BACKFILL))
        self.assertFalse(await self.store.operation_backfilled(HEATER_HISTORY_BACKFILL))

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
            memory = self.store.model.hybrid_residual_memory["GLOBAL"]
            self.assertEqual(memory.current[7].samples, 1)
            self.assertAlmostEqual(memory.current[7].mean, 3.5)
            self.assertAlmostEqual(self.store.model.hybrid_duration_memory["GLOBAL"].current[7].mean, 35)
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
            off_setpoint=25, off_stop_tolerance=0.5,
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
        self.assertEqual(accepted["analysis"]["off_setpoint"], 25)
        self.assertEqual(accepted["analysis"]["off_stop_tolerance"], 0.5)
        policy = accepted["analysis"]["peak_confirmation"]
        self.assertEqual(policy["minimum_drop_c"], .1)
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

    # Verify contiguous cooling evidence survives raw cleanup and reopening; related: curve_storage.py.
    async def test_cooling_decline_evidence_survives_raw_expiry(self):
        result = CurveResult(
            "cooling-decline", "COOLING", "THRESHOLD_START", "TARGET_REACHED",
            "NEW_HEATING", "BALANCED", 100, 1900, 3700, 7000,
            {0: 0, 1: .05, 2: 0, 3: 0, 4: -.1, 5: -.05, 6: -.05, 7: -.1,
             9: -.1, 10: -.1, 11: -.1},
            None, None, peak_temperature=24,
        )
        self.assertEqual(await self.store.save(result), (True, "ACCEPTED"))
        gap = replace(result, cycle_id="gap", buckets={0: 0, 2: -.1, 3: -.1, 4: -.1})
        self.assertEqual(await self.store.save(gap), (True, "ACCEPTED"))
        flat = replace(result, cycle_id="flat", buckets={0: 0, 1: -.02, 2: .02, 3: 0})
        self.assertEqual(await self.store.save(flat), (True, "ACCEPTED"))
        excluded = replace(result, cycle_id="excluded", invalid_reason="RELOAD_OR_SHUTDOWN")
        self.assertFalse((await self.store.save(excluded))[0])

        await self.store.cleanup(now=datetime.fromtimestamp(result.ended_at, UTC) + timedelta(days=8))
        await self.store.open()
        cycles = {row["id"]: row for row in (await self.store.read_cycles(curve_type="COOLING"))["cycles"]}
        self.assertEqual(cycles[result.cycle_id]["raw_status"], "expired")
        self.assertEqual(cycles[result.cycle_id]["analysis"]["cooling_decline"], {
            "status": "observed", "onset_minutes": 10,
            "windows": [
                {"start_minutes": 10, "end_minutes": 25, "drop_rate_c_per_hour": .4,
                 "drop_acceleration_c_per_hour2": None},
                {"start_minutes": 25, "end_minutes": 40, "drop_rate_c_per_hour": .8,
                 "drop_acceleration_c_per_hour2": 1.6},
            ],
        })
        self.assertEqual(cycles["gap"]["analysis"]["cooling_decline"], {
            "status": "insufficient_data", "onset_minutes": None, "windows": [],
        })
        self.assertEqual(cycles["flat"]["analysis"]["cooling_decline"]["status"], "no_sustained_decline")
        self.assertNotIn("cooling_decline", cycles["excluded"]["analysis"])

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
            self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0], '19')

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
            outdoor_mean=-2, outdoor_end=-2,
        )
        for index in range(8):
            shift = index * 86400
            await self.store.save(replace(base, cycle_id=f"expired-{index}",
                                          started_at=base.started_at-shift, off_at=base.off_at-shift,
                                          peak_at=base.peak_at-shift, ended_at=base.ended_at-shift), {
                "off_at": base.off_at-shift, "actual_peak": 23.1, "predictions": {"curve": 23.2},
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
            self.assertIn(7, model.runtime_rise_memory["-5"].long_term)
            self.assertIn(7, model.hybrid_residual_memory["-5"].long_term)
            self.assertAlmostEqual(model.runtime_duration_memory["-5"].long_term[7].mean, 35)
            self.assertAlmostEqual(model.hybrid_duration_memory["-5"].long_term[7].mean, 35)
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
