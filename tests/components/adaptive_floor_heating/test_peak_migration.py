"""Verify Recorder-only Peak recovery and atomic exactly-once learning; related: peak_migration.py."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from custom_components.adaptive_floor_heating.curve_learning import CurveResult
from custom_components.adaptive_floor_heating.curve_storage import CurveStore
from custom_components.adaptive_floor_heating.peak_migration import (
    migrate_heater_outages, migrate_outdoor_weather, migrate_timeouts, replay_heater_outage, replay_timeout,
)


def state(at, value, unit="°C"):
    return SimpleNamespace(state=str(value), last_updated=datetime.fromtimestamp(at, UTC),
                           attributes={"unit_of_measurement": unit, "device_class": "temperature"})


def reports():
    return [state(at, value) for at, value in ((-1, 20), (300, 20.1), (600, 20.2),
            (900, 20.3), (1200, 20.4), (1500, 20.5), (1750, 20.6),
            (2400, 20.9), (3000, 21), (3600, 20.9), (4200, 20.8))]


class PeakMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.hass = SimpleNamespace(config=SimpleNamespace(
            path=lambda *parts: str(Path(self.folder.name).joinpath(*parts))))
        self.store = CurveStore(self.hass, "migration")
        await self.store.open()
        self.timeout = CurveResult(
            "timeout", "WARM_HEATING", "THRESHOLD_START", "PREDICTIVE_STOP", "PEAK_TIMEOUT",
            "BALANCED", 0, 1800, None, 7200, {0: 0, 1: .1}, None, "INCOMPLETE_PEAK",
            start_temperature=20, off_temperature=20.6, slope_at_off=.3, outdoor_temperature=5,
            target_peak_temperature=21, sunrise_peak_excess={"predicted_peak": 20.95, "sunrise_at": 8000})
        await self.store.save(self.timeout)
        self.row = (await self.store.read_peak_timeouts())[0]

    async def asyncTearDown(self):
        self.folder.cleanup()

    async def test_weather_migration_uses_on_interval_and_ignores_reports_after_off(self):
        ordinary = replace(self.timeout, cycle_id="weather", peak_at=3000, ended_at=4200,
                           end_reason="PEAK_CONFIRMED", invalid_reason=None,
                           peak_temperature=21, residual_rise=.4)
        await self.store.save(ordinary)
        history = lambda *args, **kwargs: {"sensor.outdoor": [
            state(-30, -6), state(900, 4), state(1800, 6), state(1801, 50),
        ]}
        recorder = SimpleNamespace(async_add_executor_job=AsyncMock(side_effect=lambda job: job()))
        fake = SimpleNamespace(get_instance=lambda hass: recorder,
                               history=SimpleNamespace(get_significant_states=history))
        with patch.dict(sys.modules, {"homeassistant.components.recorder": fake}):
            migrated = await migrate_outdoor_weather(self.hass, self.store, "sensor.outdoor", 960)
            repeated = await migrate_outdoor_weather(self.hass, self.store, "sensor.outdoor", 960)
        self.assertEqual(migrated, {"status": "complete", "recovered": 1, "skipped": 0})
        self.assertEqual(repeated["recovered"], 0)
        row = next(row for row in (await self.store.read_cycles())["cycles"] if row["id"] == "weather")
        self.assertEqual((row["outdoor_temperature"], row["outdoor_mean"], row["outdoor_end"]), (5, -1, 6))

    async def test_recovery_preserves_original_counts_accuracy_and_current_memory(self):
        recent = replace(self.timeout, cycle_id="recent", started_at=20000, off_at=21800,
                         peak_at=23000, ended_at=24000, end_reason="PEAK_CONFIRMED", invalid_reason=None,
                         residual_rise=.4, peak_temperature=21)
        await self.store.save(recent)
        memory = deepcopy(self.store.model.memory["WARM_HEATING"].dump())
        result, proof = replay_timeout(self.row, reports())
        self.assertEqual(result.peak_temperature, 21)
        self.assertEqual(result.ended_at, 4200)
        self.assertTrue(await self.store.recover_peak_timeout(self.row, result, proof, "sensor.room"))
        self.assertFalse(await self.store.recover_peak_timeout(self.row, result, proof, "sensor.room"))
        self.assertEqual(await self.store.read_peak_timeouts(), [])
        self.assertEqual(self.store.model.accepted["WARM_HEATING"], 2)
        self.assertEqual(self.store.model.rejected["WARM_HEATING"], 0)
        self.assertEqual(self.store.model.memory["WARM_HEATING"].dump(), memory)
        page = await self.store.read_cycles()
        row = next(row for row in page["cycles"] if row["id"] == "timeout")
        self.assertEqual(row["end_reason"], "PEAK_CONFIRMED")
        audit = row["analysis"]["peak_timeout_migration"]
        self.assertEqual(audit["original"]["end_reason"], "PEAK_TIMEOUT")
        self.assertEqual(len(audit["original_buckets"]), 2)
        self.assertEqual(audit["source_reports"][-2:], [[3600, 20.9], [4200, 20.8]])
        self.assertEqual(page["accuracy"]["count"], 1)
        self.assertAlmostEqual(page["accuracy"]["mean_absolute_error"], .05)
        await self.store.open()
        self.assertEqual((await self.store.read_cycles())["accuracy"]["count"], 1)
        self.assertTrue(any(abs(sample["peak"] - 21) < 1e-6 for sample in self.store.model.peak_samples))

    async def test_heater_recovery_is_independent_and_applied_once(self):
        # Use distinct exclusion reasons even when the legacy end reason was also PEAK_TIMEOUT.
        outage = replace(self.timeout, cycle_id="outage", invalid_reason="HEATER_UNAVAILABLE")
        await self.store.save(outage)
        self.assertEqual([row["id"] for row in await self.store.read_peak_timeouts()], ["timeout"])
        row, = await self.store.read_heater_outages()
        classify = lambda sample: {"on": True, "off": False}.get(sample.state)
        heater = [state(at, value) for at, value in (
            (-30, "off"), (0, "on"), (1800, "off"), (3100, "unavailable"), (3130, "off"),
        )]
        self.assertIsNone(replay_timeout(row, reports()))
        self.assertIsNone(replay_heater_outage(row, reports(), heater, classify, 30, 6000))
        self.assertIsNone(replay_heater_outage(
            row, reports(), [*heater[:-1], state(3130, "on")], classify, 60, 6000,
        ))
        self.assertIsNone(replay_heater_outage(
            row, reports(), [*heater, state(4000, "on")], classify, 60, 6000,
        ))
        result, proof = replay_heater_outage(row, reports(), heater, classify, 60, 6000)
        self.assertFalse(await self.store.recover_peak_timeout(row, result, proof, "sensor.room"))
        self.assertTrue(await self.store.recover_heater_outage(row, result, proof, "sensor.room"))
        self.assertFalse(await self.store.recover_heater_outage(row, result, proof, "sensor.room"))
        self.assertEqual(await self.store.read_heater_outages(), [])
        self.assertEqual([row["id"] for row in await self.store.read_peak_timeouts()], ["timeout"])
        page = await self.store.read_cycles()
        migrated = next(row for row in page["cycles"] if row["id"] == "outage")
        audit = migrated["analysis"]["heater_outage_migration"]
        self.assertEqual(audit["original"]["quality_reason"], "HEATER_UNAVAILABLE")
        self.assertNotIn("peak_timeout_migration", migrated["analysis"])
        self.assertEqual(page["accuracy"]["count"], 1)

    async def test_no_forecast_does_not_invent_comparison(self):
        row = deepcopy(self.row)
        row["analysis"].pop("sunrise_peak_excess", None)
        result, proof = replay_timeout(row, reports())
        self.assertIsNone(proof["comparison"])
        self.assertTrue(await self.store.recover_peak_timeout(self.row, result, proof, "sensor.room"))
        self.assertEqual((await self.store.read_cycles())["accuracy"]["count"], 0)

    async def test_failed_transaction_leaves_original_and_totals_unchanged(self):
        result, proof = replay_timeout(self.row, reports())
        with patch.object(self.store, "_rebuild_control_learning", side_effect=RuntimeError("rollback")):
            with self.assertRaises(RuntimeError):
                await self.store.recover_peak_timeout(self.row, result, proof, "sensor.room")
        self.assertEqual(len(await self.store.read_peak_timeouts()), 1)
        self.assertEqual((await self.store.read_cycles())["accuracy"]["count"], 0)
        self.assertEqual(self.store.model.rejected["WARM_HEATING"], 1)

    async def test_missing_invalid_or_repeated_low_reports_do_not_recover(self):
        for source in ([], reports()[1:], reports()[:-1],
                       [*reports()[:-2], state(3600, 20.9), state(4200, 20.9)],
                       [*reports()[:-2], state(3500, "unavailable"), *reports()[-2:]]):
            with self.subTest(count=len(source)):
                self.assertIsNone(replay_timeout(self.row, source))
        self.assertEqual(len(await self.store.read_peak_timeouts()), 1)

    async def test_exclusions_and_missing_on_response_remain_excluded(self):
        for field, value in (("end_reason", "MANUAL_MODE_CHANGE"), ("quality_reason", "SUNRISE_SAVING"),
                             ("preset", "away"), ("accepted", True)):
            row = {**self.row, field: value}
            self.assertIsNone(replay_timeout(row, reports()))
        row = deepcopy(self.row)
        row["analysis"]["sunrise_peak_excess"]["first_at"] = 3500
        self.assertIsNone(replay_timeout(row, reports()))
        flat = [state(-1, 20), state(1750, 20), state(2400, 19.9), state(3000, 19.8)]
        row = {**self.row, "off_temperature": 20}
        self.assertIsNone(replay_timeout(row, flat))

    async def test_recorder_executor_uses_real_states_and_skips_synthetic_start(self):
        async def execute(job):
            self.assertFalse(job.keywords["include_start_time_state"])
            self.assertFalse(job.keywords["significant_changes_only"])
            return {"sensor.room": reports()}
        recorder = SimpleNamespace(async_add_executor_job=execute)
        fake = SimpleNamespace(get_instance=lambda hass: recorder,
                               history=SimpleNamespace(get_significant_states=lambda *a, **k: None))
        with patch.dict(sys.modules, {"homeassistant.components.recorder": fake}):
            self.assertEqual(await migrate_timeouts(self.hass, self.store, "sensor.room"), 1)
            self.assertEqual(await migrate_timeouts(self.hass, self.store, "sensor.room"), 0)

    async def test_recorder_failure_does_not_modify_original(self):
        recorder = SimpleNamespace(async_add_executor_job=AsyncMock(side_effect=RuntimeError("unavailable")))
        fake = SimpleNamespace(get_instance=lambda hass: recorder,
                               history=SimpleNamespace(get_significant_states=lambda *a, **k: None))
        with patch.dict(sys.modules, {"homeassistant.components.recorder": fake}):
            self.assertEqual(await migrate_timeouts(self.hass, self.store, "sensor.room"), 0)
        self.assertEqual(len(await self.store.read_peak_timeouts()), 1)

    async def test_recorder_recovers_a_cycle_closed_at_the_outage(self):
        await self.store.save(replace(self.timeout, cycle_id="outage", ended_at=3100,
                                      end_reason="HEATER_UNAVAILABLE", invalid_reason="HEATER_UNAVAILABLE"))
        heater = [state(at, value) for at, value in (
            (-30, "off"), (0, "on"), (1800, "off"), (3100, "unavailable"), (3130, "off"),
        )]

        async def execute(job):
            self.assertEqual(job.args[-1], ["sensor.room", "climate.heater"])
            self.assertFalse(job.keywords["include_start_time_state"])
            return {"sensor.room": reports(), "climate.heater": heater}

        recorder = SimpleNamespace(async_add_executor_job=execute)
        fake = SimpleNamespace(get_instance=lambda hass: recorder,
                               history=SimpleNamespace(get_significant_states=lambda *a, **k: None))
        outcome = {"status": "complete"}
        with patch.dict(sys.modules, {"homeassistant.components.recorder": fake}):
            count = await migrate_heater_outages(
                self.hass, self.store, "sensor.room", ("climate.heater",),
                lambda sample: {"on": True, "off": False}.get(sample.state), 60, 6000, outcome,
            )
        self.assertEqual(count, 1)
        self.assertEqual(outcome, {"status": "complete", "skipped": 0})
        row = next(row for row in (await self.store.read_cycles())["cycles"] if row["id"] == "outage")
        self.assertEqual(row["analysis"]["heater_outage_migration"]["heater_outage"]["entity_id"],
                         "climate.heater")

    async def test_fahrenheit_reports_use_celsius_confirmation_and_preserve_measurements(self):
        source = [state(item.last_updated.timestamp(), float(item.state) * 9 / 5 + 32, "°F")
                  for item in reports()]
        result, proof = replay_timeout(self.row, source)
        self.assertAlmostEqual(result.peak_temperature, 21)
        self.assertEqual(result.ended_at, 4200)

    async def test_no_extension_past_original_manual_safety_unknown_boundary(self):
        source = [*reports()[:-1], state(7300, 20.8)]
        self.assertIsNone(replay_timeout(self.row, source))

    async def test_quality_rejected_peak_is_confirmed_without_control_learning(self):
        source = [*reports()[:7], state(2400, 27), state(3000, 26.9), state(3600, 26.8)]
        row = deepcopy(self.row)
        row["analysis"].pop("sunrise_peak_excess", None)
        result, proof = replay_timeout(row, source)
        self.assertTrue(await self.store.recover_peak_timeout(self.row, result, proof, "sensor.room"))
        cycle = (await self.store.read_cycles())["cycles"][0]
        self.assertEqual(cycle["end_reason"], "PEAK_CONFIRMED")
        self.assertFalse(cycle["accepted"])
        self.assertEqual(cycle["quality_reason"], "INVALID_RESIDUAL_RISE")
        self.assertEqual(len(self.store.model.peak_samples), 0)
        self.assertEqual(self.store.model.rejected["WARM_HEATING"], 1)
