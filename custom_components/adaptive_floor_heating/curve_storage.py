"""Persist per-entry curve standards and recent raw buckets; related: curve_learning.py, runtime.py."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import closing
from datetime import UTC, datetime, timedelta
from dataclasses import replace
import logging
import json
from copy import deepcopy
from pathlib import Path
import sqlite3
from math import isfinite

from .curve_learning import (
    BUCKET_SECONDS, CURVE_TYPES, HYBRID_BIN_MINUTES, MAX_HYBRID_RUNTIME_MINUTES,
    MAX_RESIDUAL_RISE, MAX_RUNTIME_SUMMARY_RISE, NEW_CYCLE_WEIGHT, RUNTIME_SAMPLE_QUALITY,
    CurveResult, CurveStandards, cooling_decline_profile, heating_outcome,
)
from .curve_memory import CurveMemory
from .peak_prediction import control_hold, heating_outdoor_bucket, long_term_control_supported
from .peak_prediction import sample as peak_sample
from .off_response import MAX_OFF_PROFILES, valid_profile
from .history import PEAK_CONFIRM_DROP, PEAK_CONFIRM_EXTRA_DROP, PEAK_CONFIRM_REPORTS
from .const import DEFAULT_PEAK_OBSERVATION_HOURS, OFF_MODELS

_LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 19
RAW_RETENTION_DAYS = 7
CYCLE_RETENTION_DAYS = 30
OUTDOOR_COOLING_MEMORY = "outdoor_cooling"
HYBRID_RESIDUAL_MEMORY = "hybrid_residual_measured"
RUNTIME_RISE_MEMORY = "runtime_rise_measured"
HYBRID_DURATION_MEMORY = "hybrid_duration"
RUNTIME_DURATION_MEMORY = "runtime_duration"
HEATING_METRIC_WINDOW = 10
PEAK_ERROR_TOLERANCE = 0.3
_SUNRISE_RUNTIME_FILTER = """curve_type='SUNRISE_HEATING' AND off_at IS NOT NULL
    AND peak_at>=off_at AND peak_temperature>start_temperature
    AND json_extract(analysis_json,'$.sunrise_peak_excess.first_at') IS NULL
    AND (json_extract(analysis_json,'$.sunrise_peak_excess.sunrise_at') IS NULL
         OR unixepoch(peak_at)<json_extract(analysis_json,'$.sunrise_peak_excess.sunrise_at'))"""


# Store bounded model summaries independently of cycle history; related: curve_learning.py.
def _summary_payload(groups: dict[str, CurveMemory]) -> str:
    return json.dumps({group: memory.dump() for group, memory in groups.items() if memory.current},
                      allow_nan=False)


def _restore_summary(payload: str, groups: dict[str, CurveMemory], max_index: int | None,
                     max_delta: float = 3.0) -> None:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Invalid summary groups")
    restored = {}
    for group in groups.keys() & data.keys():
        memory = CurveMemory.restore(data[group], max_delta=max_delta)
        if memory.responses or (max_index is not None and
                                any(index > max_index for index in memory.current.keys() | memory.long_term.keys())):
            raise ValueError("Invalid summary bins")
        restored[group] = memory
    groups.update(restored)


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


# Derive the ON-to-rise delay from preserved five-minute reports; related: curve_learning.py.
# Store diagnostic evidence independently of raw TTL; related: curve_learning.py, curve_api.py.
def _safe_evidence(value):
    if isinstance(value, float) and not isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _safe_evidence(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_evidence(item) for item in value]
    return value


# Accumulate only original forecast comparisons; related: peak_migration.py and runtime.py.
def _comparison_errors(comparison: dict) -> list[tuple[str, float]]:
    predicted = comparison.get("predictions", {}).get("curve")
    actual = comparison.get("actual_peak")
    if not all(type(value) in (int, float) and isfinite(value) for value in (predicted, actual)):
        return []
    model = comparison.get("off_model")
    labels = ("curve", model) if model in OFF_MODELS else ("curve",)
    errors = [(label, abs(predicted - actual)) for label in labels]
    summary = comparison.get("hybrid_summary_peak")
    if model == "hybrid" and type(summary) in (int, float) and isfinite(summary):
        errors.extend((("hybrid_paired", abs(predicted - actual)), ("hybrid_summary", abs(summary - actual))))
    return errors


def _learning_state(memory, result: CurveResult) -> dict:
    """Freeze promotion gates with the cycle, independent of raw TTL; related: curve_memory.py, panel.js."""
    return {"diagnostics": memory.diagnostics(result.ended_at),
            "promotion": memory.promotion_diagnostics(result.ended_at),
            "current": [{"index": index, "delta": memory.current[index].mean,
                         "samples": memory.current[index].samples}
                        for index in sorted(result.buckets) if index in memory.current]}


class CurveStore:
    """Serialize SQLite work in a worker thread so HA's event loop stays free."""

    def __init__(self, hass, entry_id: str,
                 max_peak_wait_minutes: float = DEFAULT_PEAK_OBSERVATION_HOURS * 60) -> None:
        # HA config.path keeps the database in its private .storage directory.
        self.path = Path(hass.config.path(".storage", f"adaptive_floor_heating_{entry_id}_curves.sqlite3"))
        self.lock = asyncio.Lock()
        self.max_peak_wait_minutes = max_peak_wait_minutes
        self.model = CurveStandards(max_peak_wait_minutes)
        self.sunrise_runtime_model = CurveStandards(max_peak_wait_minutes)
        self.heating_metrics: dict = {}
        self.control_feedback: list = []

    # Summarize committed heating history; related: sensor.py and runtime.py.
    def _read_heating_metrics(self, conn: sqlite3.Connection | None = None) -> dict:
        if conn is None:
            with closing(self._connect()) as connection:
                return self._read_heating_metrics(connection)
        rows = conn.execute("""
            SELECT COALESCE(heating_duration_minutes,(julianday(off_at)-julianday(started_at))*1440),
                   quality_reason, analysis_json
            FROM cycles
            WHERE curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING') AND off_at IS NOT NULL
            ORDER BY off_at DESC, rowid DESC
        """)
        latest_duration = latest_error = None
        durations, errors = [], []
        for duration, reason, payload in rows:
            if type(duration) not in (int, float) or not isfinite(duration) or duration <= 0:
                continue
            try:
                analysis = json.loads(payload or "{}") or {}
                comparison = analysis.get("prediction_comparison") or {}
                predicted = comparison.get("predictions", {}).get("curve")
                actual = comparison.get("actual_peak")
                error = predicted - actual if all(
                    type(value) in (int, float) and isfinite(value) for value in (predicted, actual)
                ) else None
            except (ValueError, TypeError, AttributeError):
                analysis, error = {}, None
            if error is not None and not isfinite(error):
                error = None
            if latest_duration is None:
                latest_duration, latest_error = duration, error
            if len(durations) < HEATING_METRIC_WINDOW and reason in RUNTIME_SAMPLE_QUALITY and not analysis.get("sunrise_saving"):
                durations.append(duration)
            if len(errors) < HEATING_METRIC_WINDOW and error is not None:
                errors.append(abs(error))
            if len(durations) == len(errors) == HEATING_METRIC_WINDOW:
                break
        return {
            "last_heating_duration": latest_duration,
            "average_heating_duration": sum(durations) / len(durations) if durations else None,
            "last_peak_error": latest_error,
            "mean_absolute_peak_error": sum(errors) / len(errors) if errors else None,
            "peak_prediction_accuracy": (
                100 * sum(error <= PEAK_ERROR_TOLERANCE + 1e-9 for error in errors) / len(errors)
                if errors else None
            ),
            "duration_count": len(durations),
            "comparison_count": len(errors),
        }

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # Reassess legacy saving exclusions once using the configured window; related: runtime.py, curve_learning.py.
    async def migrate_sunrise_saving(self, window, schedule: dict, *, cooling: bool = False) -> int:
        async with self.lock:
            count = await asyncio.to_thread(self._migrate_sunrise_saving_sync, window, schedule, cooling)
            if count:
                await self._reload_locked()
        return count

    def _migrate_sunrise_saving_sync(self, window, schedule: dict, cooling: bool) -> int:
        with closing(self._connect()) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            marker = "sunrise_cooling_migration_v1" if cooling else "sunrise_saving_migration_v1"
            if conn.execute("SELECT 1 FROM schema_meta WHERE key=?", (marker,)).fetchone():
                return 0
            rows = conn.execute("""SELECT * FROM cycles WHERE accepted=0
                AND quality_reason='SUNRISE_SAVING' AND end_reason=?
                AND off_reason='PREDICTIVE_STOP' AND raw_pruned=0
                AND curve_type IN (?,?) ORDER BY ended_at,rowid""",
                ("TARGET_REACHED", "COOLING", "COOLING") if cooling else
                ("PEAK_CONFIRMED", "WARM_HEATING", "PREDICTIVE_WARM_HEATING")).fetchall()
            model = deepcopy(self.model)
            count = 0
            for source in rows:
                result, evidence = self._legacy_sunrise_result(conn, source, window)
                if result is None:
                    continue
                checks = model.quality_details(result)
                memory = model.memory[result.curve_type]
                # Older observations must not rewind the freshness of already learned buckets.
                learned_at = max([result.ended_at, *(layer[index].updated_at
                    for layer in (memory.current, memory.long_term)
                    for index in result.buckets.keys() & layer.keys())])
                accepted, reason = model.apply(replace(result, ended_at=learned_at))
                evidence["sunrise_saving_migration"] = {
                    "version": 1, "schedule": schedule, "original": dict(source),
                }
                evidence.pop("sunrise_saving", None)
                evidence.pop("sunrise_prediction", None)
                evidence["checks"] = checks
                evidence["learning"] = {
                    "applied": accepted,
                    "scope": "retained_cooling_observations" if cooling else "retained_heating_observations",
                }
                comparison = evidence.get("prediction_comparison")
                errors = (_comparison_errors(comparison) if not cooling and isinstance(comparison, dict)
                          and isinstance(comparison.get("off_at"), (int, float))
                          and abs(comparison["off_at"] - result.off_at) <= 30
                          and comparison.get("actual_peak") == result.peak_temperature
                          and isinstance(comparison.get("predictions"), dict) else [])
                if accepted and errors and comparison.get("off_model") in ("runtime", "hybrid"):
                    evidence["control_feedback"] = 1
                outcome, weight = heating_outcome(result)
                key = (result.cycle_id, result.curve_type)
                conn.execute("""UPDATE cycles SET accepted=?,quality_reason=?,analysis_json=?,
                    heating_outcome=?,outcome_weight=? WHERE id=? AND curve_type=?""", (
                    int(accepted), reason, json.dumps(_safe_evidence(evidence), allow_nan=False),
                    outcome, weight, *key))
                if accepted:
                    conn.execute("UPDATE cycle_counts SET total=max(0,total-1) WHERE curve_type=? AND accepted=0",
                                 (result.curve_type,))
                    conn.execute("""INSERT INTO cycle_counts VALUES (?,1,1) ON CONFLICT(curve_type,accepted)
                        DO UPDATE SET total=total+1""", (result.curve_type,))
                    kind = result.curve_type
                    residual = None
                    if not cooling:
                        mean, samples = model.residual[kind]
                        residual = (mean, samples, model.peak_delay[kind][0])
                    self._write_curve_standard(
                        conn, result, {index: model.buckets[kind][index] for index in result.buckets},
                        residual,
                        json.dumps(model.memory[kind].dump(), allow_nan=False))
                conn.executemany("""INSERT INTO prediction_accuracy VALUES (?,1,?) ON CONFLICT(model)
                    DO UPDATE SET comparison_count=comparison_count+1,
                    absolute_error_total=absolute_error_total+excluded.absolute_error_total""", errors)
                count += 1
            if count and not cooling:
                self._rebuild_control_learning(conn)
                self._runtime_average_state(conn)
            conn.execute("INSERT INTO schema_meta VALUES (?,?)", (marker, json.dumps(schedule)))
            return count

    @staticmethod
    def _legacy_sunrise_result(conn, source, window):
        try:
            started, off, peak, ended = (
                datetime.fromisoformat(source[field]).timestamp()
                for field in ("started_at", "off_at", "peak_at", "ended_at")
            )
            evidence = json.loads(source["analysis_json"])
            observation_valid = any(check.get("code") == "OBSERVATION_VALID" and check.get("passed") is True
                                    for check in evidence["checks"])
            excess = evidence.get("sunrise_peak_excess")
            measured = (source["start_temperature"], source["off_temperature"],
                        source["peak_temperature"])
            hold = None
            if source["curve_type"] == "COOLING":
                saved_hold = conn.execute("SELECT payload FROM heating_holds WHERE cycle_id=?",
                                          (source["id"],)).fetchone()
                if saved_hold is None:
                    return None, None
                hold = json.loads(saved_hold[0])
                if (hold.get("completed") is not True or hold.get("exclusion_reason") is not None
                        or hold.get("ended_at") != source["ended_at"]):
                    return None, None
            else:
                measured += (source["residual_rise"],)
            if (not evidence.get("sunrise_saving") or not observation_valid
                    or not started < off <= peak <= ended or window(started)[0] or not window(off)[0]
                    or not all(type(value) in (int, float) and isfinite(value) for value in measured)
                    or (excess or {}).get("first_at") is not None):
                return None, None
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return None, None
        buckets = dict(conn.execute("""SELECT bucket_index,delta_c FROM cycle_buckets
            WHERE cycle_id=? AND curve_type=? ORDER BY bucket_index""", (source["id"], source["curve_type"])))
        if (len(buckets) < 2 or len(buckets) != source["bucket_count"]
                or any(type(index) is not int or index < 0 or type(delta) not in (int, float)
                       for index, delta in buckets.items())):
            return None, None
        result = CurveResult(
            source["id"], source["curve_type"], source["start_reason"], source["off_reason"],
            source["end_reason"], source["mode"], started, off, peak, ended,
            buckets, None if hold is not None else source["residual_rise"], None, source["preset"],
            start_temperature=source["start_temperature"], off_temperature=source["off_temperature"],
            peak_temperature=source["peak_temperature"], outdoor_temperature=source["outdoor_temperature"],
            target_peak_temperature=source["target_peak_temperature"], sunrise_peak_excess=excess,
            heating_hold=hold,
        )
        return result, evidence

    # Select only unchanged timeout exclusions; related: peak_migration.py, runtime.py.
    async def read_peak_timeouts(self) -> list[dict]:
        return await self._read_peak_recovery_candidates("PEAK_TIMEOUT", "INCOMPLETE_PEAK")

    async def read_heater_outages(self) -> list[dict]:
        return await self._read_peak_recovery_candidates(None, "HEATER_UNAVAILABLE")

    async def _read_peak_recovery_candidates(self, end_reason: str | None, quality_reason: str) -> list[dict]:
        def read():
            with closing(self._connect()) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""SELECT * FROM cycles WHERE accepted=0
                    AND end_reason=COALESCE(?,end_reason) AND quality_reason=?
                    AND curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')
                    AND preset='home' AND off_at IS NOT NULL ORDER BY ended_at,rowid""",
                    (end_reason, quality_reason)).fetchall()
                result = []
                for source in rows:
                    row = dict(source)
                    try:
                        row["analysis"] = json.loads(row["analysis_json"] or "{}")
                    except (TypeError, ValueError):
                        continue
                    if isinstance(row["analysis"], dict):
                        result.append(row)
                return result
        async with self.lock:
            return await asyncio.to_thread(read)

    # Replace an excluded cycle exactly once, preserving its original row and buckets as audit evidence.
    # Related: peak_migration.py; rebuild chronological control models without rewinding Current curve memory.
    async def recover_peak_timeout(self, original: dict, result: CurveResult, proof: dict, sensor: str) -> bool:
        return await self._recover_peak(original, result, proof, sensor, "peak_timeout_migration",
                                        "PEAK_TIMEOUT", "INCOMPLETE_PEAK")

    async def recover_heater_outage(self, original: dict, result: CurveResult, proof: dict, sensor: str) -> bool:
        return await self._recover_peak(original, result, proof, sensor, "heater_outage_migration",
                                        original["end_reason"], "HEATER_UNAVAILABLE")

    async def _recover_peak(self, original, result, proof, sensor, marker, end_reason, quality_reason):
        if (result.cycle_id != original["id"] or result.curve_type != original["curve_type"]
                or result.end_reason != "PEAK_CONFIRMED" or result.invalid_reason is not None
                or result.peak_at is None or result.off_at is None):
            return False
        async with self.lock:
            accepted, reason = self.model.assess(result)
            checks = self.model.quality_details(result)
            applied = await asyncio.to_thread(
                self._recover_peak_sync, original, result, proof, sensor, accepted, reason, checks,
                marker, end_reason, quality_reason,
            )
            if applied:
                await self._reload_locked()
        return applied

    def _recover_peak_sync(self, original, result, proof, sensor, accepted, reason, checks,
                           marker, end_reason, quality_reason):
        with closing(self._connect()) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute("SELECT * FROM cycles WHERE id=? AND curve_type=?",
                                  (result.cycle_id, result.curve_type)).fetchone()
            if (source is None or source["accepted"] or source["end_reason"] != end_reason
                    or source["quality_reason"] != quality_reason
                    or source["ended_at"] != original["ended_at"]
                    or source["analysis_json"] != original.get("analysis_json")):
                return False
            evidence = json.loads(source["analysis_json"] or "{}")
            # A prior comparison cannot be silently replaced or double-counted.
            if (evidence.get("peak_timeout_migration") or evidence.get("heater_outage_migration")
                    or evidence.get("sunrise_saving_migration") or evidence.get("sunrise_saving")
                    or (evidence.get("sunrise_peak_excess") or {}).get("first_at") is not None
                    or (evidence.get("prediction_comparison") or {}).get("actual_peak") is not None):
                return False
            raw = [dict(row) for row in conn.execute(
                "SELECT bucket_index,delta_c FROM cycle_buckets WHERE cycle_id=? AND curve_type=? ORDER BY bucket_index",
                (result.cycle_id, result.curve_type))]
            evidence[marker] = {
                "version": 1, "source": "recorder_sensor_states", "sensor": sensor,
                "migrated_at": _stamp(datetime.now(UTC).timestamp()),
                "report_count": proof["report_count"], "original": dict(source), "original_buckets": raw,
                "source_reports": proof["source_reports"], "temperature_unit": "°C",
                "learning_scope": "runtime_and_hybrid", "curve_memory_replayed": False,
            }
            if marker == "heater_outage_migration":
                evidence[marker]["heater_outage"] = proof["heater_outage"]
            evidence["checks"] = checks
            evidence["peak_confirmation"] = {
                "minimum_drop_c": PEAK_CONFIRM_DROP, "minimum_extra_drop_c": PEAK_CONFIRM_EXTRA_DROP,
                "minimum_reports": PEAK_CONFIRM_REPORTS, "observed_drop_c": result.peak_confirmation_drop,
                "observed_extra_drop_c": result.peak_confirmation_extra_drop,
                "observed_reports": result.peak_confirmation_reports,
                "maximum_wait_minutes": (proof.get("replay_end", datetime.fromisoformat(source["ended_at"]).timestamp())
                                         - result.off_at) / 60,
            }
            evidence["learning"] = {"applied": accepted, "scope": "runtime_and_hybrid"}
            comparison = proof.get("comparison")
            if comparison:
                evidence["prediction_comparison"] = comparison
                if accepted and comparison.get("off_model") in ("runtime", "hybrid"):
                    evidence["control_feedback"] = 1
            outcome, weight = heating_outcome(result)
            conn.execute("""UPDATE cycles SET end_reason='PEAK_CONFIRMED',peak_at=?,ended_at=?,accepted=?,
                quality_reason=?,peak_temperature=?,observed_peak_temperature=?,residual_rise=?,peak_delay_minutes=?,
                heating_lag_minutes=COALESCE(heating_lag_minutes,?),bucket_count=?,analysis_json=?,
                heating_outcome=?,outcome_weight=?,raw_pruned=0 WHERE id=? AND curve_type=?""", (
                _stamp(result.peak_at), _stamp(result.ended_at), int(accepted), reason,
                result.peak_temperature, result.peak_temperature, result.residual_rise,
                (result.peak_at - result.off_at) / 60, self.model._runtime_lag(result), len(result.buckets),
                json.dumps(_safe_evidence(evidence), allow_nan=False), outcome, weight,
                result.cycle_id, result.curve_type))
            conn.execute("DELETE FROM cycle_buckets WHERE cycle_id=? AND curve_type=?", (result.cycle_id, result.curve_type))
            conn.executemany("INSERT INTO cycle_buckets VALUES (?,?,?,?)", [
                (result.cycle_id, result.curve_type, index, value) for index, value in result.buckets.items()])
            if accepted:
                conn.execute("UPDATE cycle_counts SET total=max(0,total-1) WHERE curve_type=? AND accepted=0",
                             (result.curve_type,))
                conn.execute("""INSERT INTO cycle_counts VALUES (?,1,1) ON CONFLICT(curve_type,accepted)
                    DO UPDATE SET total=total+1""", (result.curve_type,))
            conn.executemany("""INSERT INTO prediction_accuracy VALUES (?,1,?) ON CONFLICT(model)
                DO UPDATE SET comparison_count=comparison_count+1,
                absolute_error_total=absolute_error_total+excluded.absolute_error_total""",
                _comparison_errors(comparison or {}))
            self._rebuild_control_learning(conn)
            self._runtime_average_state(conn)
            return True

    def _open_sync(self) -> tuple:
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_history (
                    kind TEXT NOT NULL, recorded_at REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(kind, recorded_at)
                );
                CREATE INDEX IF NOT EXISTS operation_history_time ON operation_history(recorded_at);
                CREATE TABLE IF NOT EXISTS cycles (
                    id TEXT NOT NULL, curve_type TEXT NOT NULL,
                    start_reason TEXT NOT NULL, off_reason TEXT NOT NULL,
                    end_reason TEXT NOT NULL, mode TEXT NOT NULL,
                    started_at TEXT NOT NULL, off_at TEXT, peak_at TEXT,
                    ended_at TEXT NOT NULL, accepted INTEGER NOT NULL,
                    quality_reason TEXT NOT NULL,
                    PRIMARY KEY (id, curve_type)
                );
                CREATE TABLE IF NOT EXISTS cycle_buckets (
                    cycle_id TEXT NOT NULL, curve_type TEXT NOT NULL,
                    bucket_index INTEGER NOT NULL, delta_c REAL NOT NULL,
                    PRIMARY KEY (cycle_id, curve_type, bucket_index),
                    FOREIGN KEY (cycle_id, curve_type) REFERENCES cycles(id, curve_type)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS curve_buckets (
                    curve_type TEXT NOT NULL, bucket_index INTEGER NOT NULL,
                    mean_delta_c REAL NOT NULL, sample_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (curve_type, bucket_index)
                );
                CREATE TABLE IF NOT EXISTS curve_features (
                    curve_type TEXT PRIMARY KEY, residual_mean REAL NOT NULL,
                    residual_count INTEGER NOT NULL, peak_delay_mean REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outdoor_cooling_buckets (
                    outdoor_group TEXT NOT NULL, bucket_index INTEGER NOT NULL,
                    mean_delta_c REAL NOT NULL, sample_count INTEGER NOT NULL,
                    PRIMARY KEY (outdoor_group, bucket_index)
                );
                CREATE INDEX IF NOT EXISTS idx_cycles_ended ON cycles(ended_at);
                CREATE TABLE IF NOT EXISTS off_response_profiles (
                    cycle_id TEXT NOT NULL, curve_type TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (cycle_id, curve_type)
                );
                CREATE TABLE IF NOT EXISTS curve_memory (
                    curve_type TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_average (
                    cycle_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
                    minutes REAL NOT NULL, admission_floor REAL NOT NULL, included INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sunrise_runtime_average (
                    cycle_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
                    minutes REAL NOT NULL, admission_floor REAL NOT NULL, included INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prediction_accuracy (
                    model TEXT PRIMARY KEY,
                    comparison_count INTEGER NOT NULL,
                    absolute_error_total REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS heating_holds (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cycle_counts (
                    curve_type TEXT NOT NULL, accepted INTEGER NOT NULL, total INTEGER NOT NULL,
                    PRIMARY KEY (curve_type, accepted)
                );
            """)
            existing = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if existing is not None and int(existing[0]) not in range(1, SCHEMA_VERSION + 1):
                raise ValueError("Unsupported curve database version")
            version = int(existing[0]) if existing is not None else 0
            # Schema 4 permits observed dips and optional thermal context; existing payloads stay intact.
            # Related: curve_learning.py and model_operation.md.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cycles)")}
            if "control_learning_hold" not in columns:
                conn.execute("ALTER TABLE cycles ADD COLUMN control_learning_hold TEXT")
            if "preset" not in columns:
                conn.execute("ALTER TABLE cycles ADD COLUMN preset TEXT NOT NULL DEFAULT 'unknown'")
            # Nullable evidence keeps legacy omissions honest; related: curve_api.py, panel.js.
            for name, sql_type in {
                "start_temperature": "REAL", "outdoor_temperature": "REAL", "off_temperature": "REAL",
                "outdoor_mean": "REAL", "outdoor_end": "REAL",
                "peak_temperature": "REAL", "slope_at_off": "REAL", "residual_rise": "REAL",
                "peak_delay_minutes": "REAL", "heating_duration_minutes": "REAL",
                "heating_lag_minutes": "REAL",
                "bucket_count": "INTEGER", "analysis_json": "TEXT",
                # Preserve legacy rows with unknown goals; related: curve_learning.py, runtime.py.
                "target_peak_temperature": "REAL", "observed_peak_temperature": "REAL",
                "heating_outcome": "TEXT", "outcome_weight": "REAL",
                "raw_pruned": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE cycles ADD COLUMN {name} {sql_type}")
            if version < 13:
                # Restore measured lag from retained raw bins and keep old unknown zeros null.
                # Related: curve_learning.py and runtime.py predictive ON.
                conn.execute("UPDATE cycles SET heating_lag_minutes=NULL WHERE heating_lag_minutes=0")
                pending_lags = conn.execute(
                    """SELECT id,curve_type FROM cycles WHERE heating_lag_minutes IS NULL
                       AND curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')"""
                ).fetchall()
                for cycle_id, curve_type in pending_lags:
                    buckets = conn.execute(
                        """SELECT bucket_index,delta_c FROM cycle_buckets
                           WHERE cycle_id=? AND curve_type=? ORDER BY bucket_index""",
                        (cycle_id, curve_type),
                    )
                    lag = CurveStandards.runtime_lag_from_buckets(buckets)
                    if lag is not None:
                        conn.execute(
                            "UPDATE cycles SET heating_lag_minutes=? WHERE id=? AND curve_type=?",
                            (lag, cycle_id, curve_type),
                        )
            conn.execute("""UPDATE cycles SET bucket_count=(
                SELECT count(*) FROM cycle_buckets b WHERE b.cycle_id=cycles.id
                AND b.curve_type=cycles.curve_type) WHERE bucket_count IS NULL AND EXISTS (
                SELECT 1 FROM cycle_buckets b WHERE b.cycle_id=cycles.id AND b.curve_type=cycles.curve_type)
            """)
            if version < 12:
                # Preserve lifetime totals when their detail rows expire.
                # Related: curve_learning.py and diagnostics.py.
                conn.execute("""INSERT OR IGNORE INTO cycle_counts
                    SELECT curve_type, accepted, count(*) FROM cycles GROUP BY curve_type, accepted""")
            conn.execute("INSERT OR REPLACE INTO schema_meta VALUES ('version', ?)", (str(SCHEMA_VERSION),))
            rows = conn.execute(
                "SELECT curve_type, bucket_index, mean_delta_c, sample_count FROM curve_buckets"
            ).fetchall()
            counts = conn.execute(
                "SELECT curve_type, accepted, total FROM cycle_counts"
            ).fetchall()
            features = conn.execute(
                "SELECT curve_type, residual_mean, residual_count, peak_delay_mean FROM curve_features"
            ).fetchall()
            profiles = self._profile_rows(conn)
            memories = conn.execute("SELECT curve_type, payload FROM curve_memory").fetchall()
            if version < 15:
                self._rebuild_control_learning(conn)
                memories = conn.execute("SELECT curve_type, payload FROM curve_memory").fetchall()
            runtime_samples, peak_samples, shortfalls = self._control_samples(conn)
            average = self._runtime_average_state(conn)
            memories.append(("runtime_average_state", json.dumps(average)))
            outdoor_cooling = conn.execute(
                "SELECT outdoor_group, bucket_index, mean_delta_c, sample_count FROM outdoor_cooling_buckets"
            ).fetchall()
            return (rows, counts, features, profiles, memories, runtime_samples, peak_samples,
                    shortfalls, outdoor_cooling, self._control_feedback(conn), self._sunrise_runtime(conn))

    # Rebuild retained OFF evidence from the same candidates on migration and save.
    # Related: peak_prediction.py, runtime.py, frontend/panel.js.
    @staticmethod
    def _control_candidates(conn):
        rows = conn.execute("""SELECT rowid,start_temperature,off_temperature,peak_temperature,
            COALESCE(heating_duration_minutes,(julianday(off_at)-julianday(started_at))*1440),
            outdoor_temperature,CAST(strftime('%s',ended_at) AS REAL),outcome_weight,
            (julianday(peak_at)-julianday(off_at))*1440, off_at, accepted, outdoor_mean, outdoor_end
            FROM cycles WHERE preset!='away' AND curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')
            AND quality_reason IN (?,?,?) AND peak_at>=off_at ORDER BY ended_at,rowid""",
            RUNTIME_SAMPLE_QUALITY)
        candidates = {}
        for rowid, *features, delay, off_at, accepted, mean, end in rows:
            item = peak_sample(*features)
            if item is not None and delay is not None:
                candidates[rowid] = {**item, "delay": delay, "off_at": off_at,
                                     "accepted": bool(accepted), "outdoor_mean": mean, "outdoor_end": end}
        return candidates

    def _rebuild_control_learning(self, conn, *, preserve_global=False):
        candidates = self._control_candidates(conn)
        holds = {rowid: control_hold(item, candidates.values()) for rowid, item in candidates.items()}
        conn.executemany("UPDATE cycles SET control_learning_hold=? WHERE rowid=?",
                         [(hold, rowid) for rowid, hold in holds.items()])
        runtime, peaks, shortfalls = self._control_samples(conn)
        summary = CurveStandards(self.max_peak_wait_minutes)
        for item in peaks:
            summary.learn_runtime_rise({**item, "outdoor": item["outdoor_mean"]})
            summary.learn_hybrid_residual(item)
        # Build the seasonal layer separately so recent-only samples cannot influence promotions.
        # Related: peak_prediction.py, curve_learning.py and curve_memory.py.
        admitted = list({item["off_at"]: item for rowid, item in candidates.items()
                         if holds[rowid] is None and item["accepted"]}.values())
        admitted.sort(key=lambda item: item["completed"])
        seasonal = CurveStandards(self.max_peak_wait_minutes)
        for item in admitted:
            if long_term_control_supported(item, admitted, HYBRID_BIN_MINUTES):
                seasonal.learn_runtime_rise(item, groups=("GLOBAL",))
                seasonal.learn_hybrid_residual(item, groups=("GLOBAL",))
            for field, learn in (("outdoor_mean", seasonal.learn_runtime_rise),
                                 ("outdoor_end", seasonal.learn_hybrid_residual)):
                if long_term_control_supported(item, admitted, HYBRID_BIN_MINUTES, weather_field=field):
                    learn(item, groups=(heating_outdoor_bucket(item[field]),))
        for key in ("runtime_rise_memory", "runtime_duration_memory",
                    "hybrid_residual_memory", "hybrid_duration_memory"):
            recent, qualified = getattr(summary, key), getattr(seasonal, key)
            for group, memory in recent.items():
                memory.long_term = qualified[group].long_term
            for group, memory in qualified.items():
                recent.setdefault(group, memory)
        payloads = self._off_summary_payloads(summary)
        if preserve_global:
            # Keep common summaries whose historical source cycles have expired; related: cleanup().
            conn.executemany("""INSERT INTO curve_memory VALUES (?,?)
                ON CONFLICT(curve_type) DO UPDATE SET payload=
                    CASE WHEN json_valid(curve_memory.payload) THEN
                        CASE WHEN json_type(curve_memory.payload,'$.GLOBAL')='object'
                            THEN json_set(excluded.payload,'$.GLOBAL',json_extract(curve_memory.payload,'$.GLOBAL'))
                            ELSE excluded.payload END
                        ELSE excluded.payload END""", payloads)
        else:
            conn.executemany("INSERT OR REPLACE INTO curve_memory VALUES (?,?)", payloads)
        return runtime, peaks, shortfalls, summary

    # Freeze admission against completed evidence available before ON; preserve across raw/cycle TTL.
    # Related: curve_learning.py runtime_shutdown_limits, runtime.py and learning reset.
    @staticmethod
    def _runtime_average_state(conn, *, sunrise=False):
        table = "sunrise_runtime_average" if sunrise else "runtime_average"
        candidates = conn.execute("""SELECT id, started_at, ended_at, heating_duration_minutes
            FROM cycles WHERE accepted=1 AND preset!='away' AND control_learning_hold IS NULL
            AND peak_at IS NOT NULL AND heating_duration_minutes>0 AND """ + (
                _SUNRISE_RUNTIME_FILTER if sunrise else "curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')"
            ) + " ORDER BY ended_at,rowid").fetchall()
        # Revoke retained evidence quarantined by a later control-quality rebuild.
        if sunrise:
            conn.execute("""DELETE FROM sunrise_runtime_average WHERE cycle_id IN (
                SELECT id FROM cycles WHERE curve_type='SUNRISE_HEATING'
                AND (control_learning_hold IS NOT NULL OR NOT COALESCE(("""
                + _SUNRISE_RUNTIME_FILTER + "), 0)))")
        else:
            conn.execute("""DELETE FROM runtime_average WHERE cycle_id IN
                (SELECT id FROM cycles WHERE control_learning_hold IS NOT NULL)""")
        for cycle_id, started, ended, minutes in candidates:
            if conn.execute(f"SELECT 1 FROM {table} WHERE cycle_id=?", (cycle_id,)).fetchone():
                continue
            prior = conn.execute(f"""SELECT minutes FROM {table}
                WHERE included=1 AND ended_at<? ORDER BY ended_at DESC,cycle_id DESC LIMIT 10""",
                (started,)).fetchall()
            floor = .6 * sum(row[0] for row in prior) / len(prior) if prior else 0.0
            conn.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?)",
                         (cycle_id, started, ended, minutes, floor, int(minutes + 1e-9 >= floor)))
        samples = conn.execute(f"""SELECT minutes FROM {table} WHERE included=1
            ORDER BY ended_at DESC,cycle_id DESC LIMIT 10""").fetchall()
        count = conn.execute("SELECT count(*) FROM sunrise_runtime_average" if sunrise else """
            SELECT COALESCE(sum(total),0) FROM cycle_counts
            WHERE accepted=1 AND curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')""").fetchone()[0]
        return {"samples": [row[0] for row in samples], "accepted_cycles": count}

    # Restore confirmed saving cycles without mixing them into regular runtime learning.
    # Related: curve_learning.py SUNRISE_HEATING and runtime.py predictive OFF.
    def _sunrise_runtime(self, conn):
        rows = conn.execute("""SELECT heating_duration_minutes, peak_temperature-start_temperature,
                   heating_lag_minutes, outdoor_mean, CAST(strftime('%s',ended_at) AS REAL), outcome_weight
            FROM cycles WHERE accepted=1 AND preset!='away' AND control_learning_hold IS NULL
            AND heating_duration_minutes>0 AND """
            + _SUNRISE_RUNTIME_FILTER + " ORDER BY ended_at,rowid").fetchall()
        state = self._runtime_average_state(conn, sunrise=True)
        model = CurveStandards(self.max_peak_wait_minutes)
        model.load_runtime_samples(rows)
        model.runtime_average_samples = state["samples"]
        model.runtime_accepted_cycles = state["accepted_cycles"]
        return model

    @staticmethod
    def _control_feedback(conn):
        rows = conn.execute("""SELECT analysis_json FROM cycles
            WHERE control_learning_hold IS NULL AND accepted=1
            AND curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')
            AND json_extract(analysis_json,'$.control_feedback')=1
            ORDER BY ended_at DESC,rowid DESC LIMIT 200""")
        return [json.loads(row[0])["prediction_comparison"] for row in reversed(rows.fetchall())]

    @staticmethod
    def _control_samples(conn):
            runtime_samples = conn.execute(
                """SELECT COALESCE(heating_duration_minutes,(julianday(off_at)-julianday(started_at))*1440),
                          peak_temperature-start_temperature, heating_lag_minutes,
                          outdoor_mean, CAST(strftime('%s', ended_at) AS REAL), outcome_weight
                   FROM cycles
                   WHERE curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING') AND preset!='away'
                     AND off_at IS NOT NULL AND start_temperature IS NOT NULL
                     AND peak_temperature IS NOT NULL AND peak_at >= off_at
                     AND quality_reason IN (?,?,?) AND control_learning_hold IS NULL
                   ORDER BY ended_at, rowid""", RUNTIME_SAMPLE_QUALITY
            ).fetchall()
            # Restore only accepted confirmed outcomes; legacy missing outdoor remains unknown.
            # Related: peak_prediction.py, curve_learning.py.
            peak_rows = conn.execute("""SELECT start_temperature, off_temperature, peak_temperature,
COALESCE(heating_duration_minutes,(julianday(off_at)-julianday(started_at))*1440), outdoor_end,
                CAST(strftime('%s', ended_at) AS REAL), outcome_weight, outdoor_mean FROM cycles
                WHERE accepted=1 AND preset!='away' AND peak_at IS NOT NULL AND control_learning_hold IS NULL
                AND curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')
                ORDER BY ended_at,rowid""")
            peak_samples = deque(maxlen=200)
            for row in peak_rows:
                item = peak_sample(*row[:-1])
                if item is None:
                    continue
                peak_samples.append({**item, "outdoor_mean": row[-1]})
            shortfalls = conn.execute("""SELECT heating_duration_minutes, start_temperature,
                target_peak_temperature, outdoor_mean, CAST(strftime('%s', ended_at) AS REAL)
                FROM cycles WHERE off_reason='PREDICTIVE_STOP'
                AND curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')
                AND quality_reason NOT IN ('SUNRISE_SAVING','SUNRISE_PEAK_EXCESS')
                AND control_learning_hold IS NULL
                AND heating_outcome IN ('missed','restarted_below_target')
                AND heating_duration_minutes > 0 AND start_temperature IS NOT NULL
                AND target_peak_temperature IS NOT NULL ORDER BY ended_at DESC LIMIT 200""").fetchall()
            return runtime_samples, list(peak_samples), shortfalls

    # Reclassify only retained cycles with Recorder evidence; related: peak_migration.py, runtime.py.
    def _outdoor_migration_rows(self) -> list:
        with closing(self._connect()) as conn:
            return conn.execute("""SELECT id, curve_type, started_at, off_at FROM cycles
                WHERE curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')
                AND off_at > started_at AND (outdoor_mean IS NULL OR outdoor_end IS NULL)
                AND preset!='away' AND quality_reason IN (?,?,?) ORDER BY started_at""",
                RUNTIME_SAMPLE_QUALITY).fetchall()

    async def outdoor_migration_rows(self) -> list:
        async with self.lock:
            return await asyncio.to_thread(self._outdoor_migration_rows)

    def _save_outdoor_migration(self, updates: list) -> int:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.executemany("""UPDATE cycles
                SET outdoor_mean=COALESCE(outdoor_mean,?), outdoor_end=COALESCE(outdoor_end,?)
                WHERE id=? AND curve_type=? AND started_at=? AND off_at=?
                AND ((outdoor_mean IS NULL AND ? IS NOT NULL) OR (outdoor_end IS NULL AND ? IS NOT NULL))""",
                [(mean, end, *row, mean, end) for row, mean, end in updates]).rowcount
            if count:
                self._rebuild_control_learning(conn, preserve_global=True)
            return count

    async def save_outdoor_migration(self, updates: list) -> int:
        async with self.lock:
            count = await asyncio.to_thread(self._save_outdoor_migration, updates)
            if count:
                await self._reload_locked()
            return count

    async def open(self) -> None:
        async with self.lock:
            await self._reload_locked()

    # Refresh the model before releasing a committed migration; related: runtime.py and save().
    async def _reload_locked(self) -> None:
        (rows, counts, features, profiles, memories, runtime_samples, peak_samples,
         shortfalls, outdoor_cooling, self.control_feedback,
         self.sunrise_runtime_model) = await asyncio.to_thread(self._open_sync)
        self.heating_metrics = await asyncio.to_thread(self._read_heating_metrics)
        self.model = CurveStandards(self.max_peak_wait_minutes)
        self.model.load(rows)
        self.model.outdoor_cooling = {
            (group, index): (mean, count) for group, index, mean, count in outdoor_cooling
            if isinstance(index, int) and index >= 0 and isfinite(mean) and count > 0
        }
        self.model.load_runtime_samples(runtime_samples)
        self.model.peak_samples = peak_samples
        self.model.shortfall_samples = list(reversed(shortfalls))
        max_index = MAX_HYBRID_RUNTIME_MINUTES // HYBRID_BIN_MINUTES
        summaries = {
            OUTDOOR_COOLING_MEMORY: (self.model.outdoor_cooling_memory, None, 3),
            HYBRID_RESIDUAL_MEMORY: (self.model.hybrid_residual_memory, max_index, MAX_RESIDUAL_RISE),
            RUNTIME_RISE_MEMORY: (self.model.runtime_rise_memory, max_index, MAX_RUNTIME_SUMMARY_RISE),
            HYBRID_DURATION_MEMORY: (self.model.hybrid_duration_memory, max_index, MAX_HYBRID_RUNTIME_MINUTES),
            RUNTIME_DURATION_MEMORY: (self.model.runtime_duration_memory, max_index, MAX_HYBRID_RUNTIME_MINUTES),
        }
        for curve, payload in memories:
            if curve == "runtime_average_state":
                state = json.loads(payload)
                self.model.runtime_average_samples = state["samples"]
                self.model.runtime_accepted_cycles = state["accepted_cycles"]
                continue
            if curve in summaries:
                try:
                    _restore_summary(payload, *summaries[curve])
                except (ValueError, TypeError, AttributeError):
                    _LOGGER.warning("Stored conditioned summary excluded: %s", curve)
                continue
            if curve not in CURVE_TYPES:
                continue
            try:
                data = json.loads(payload)
                self.model.memory[curve] = CurveMemory.restore(data)
            except (ValueError, TypeError, AttributeError):
                # A damaged Long-term layer must not discard valid Current data.
                # Related: curve_memory.py and runtime.py safety fallback.
                _LOGGER.warning("Curve memory invalid; attempting Current-only restore: %s", curve)
                try:
                    data = json.loads(payload)
                    data["long_term"] = {}
                    for group in data["responses"].values():
                        group["long_term"] = {}
                    self.model.memory[curve] = CurveMemory.restore(data)
                except (ValueError, TypeError, AttributeError, KeyError):
                    _LOGGER.warning("Stored curve memory excluded: %s", curve)
        self._load_profiles(profiles)
        for curve, mean, count, delay in features:
            if curve in CURVE_TYPES and count > 0:
                self.model.residual[curve] = (mean, count)
                self.model.peak_delay[curve] = (delay, count)
        for curve, accepted, count in counts:
            if curve in CURVE_TYPES:
                target = self.model.accepted if accepted else self.model.rejected
                target[curve] = count

    @staticmethod
    def _off_summary_payloads(model: CurveStandards) -> list[tuple[str, str]]:
        return [
            (HYBRID_RESIDUAL_MEMORY, _summary_payload(model.hybrid_residual_memory)),
            (RUNTIME_RISE_MEMORY, _summary_payload(model.runtime_rise_memory)),
            (HYBRID_DURATION_MEMORY, _summary_payload(model.hybrid_duration_memory)),
            (RUNTIME_DURATION_MEMORY, _summary_payload(model.runtime_duration_memory)),
        ]

    @staticmethod
    def _profile_rows(conn):
        return conn.execute("""SELECT p.curve_type, p.payload, c.ended_at
            FROM off_response_profiles p LEFT JOIN cycles c ON c.id=p.cycle_id AND c.curve_type=p.curve_type
            ORDER BY p.rowid""").fetchall()

    def _load_profiles(self, rows) -> None:
        self.model.off_profiles = {curve: [] for curve in CURVE_TYPES}
        self.model.off_profile_updated.clear()
        for curve, payload, ended_at in rows:
            try:
                profile = json.loads(payload)
                if curve not in CURVE_TYPES or not valid_profile(profile):
                    raise ValueError("Invalid OFF profile")
                self.model.off_profiles[curve].append(profile)
                del self.model.off_profiles[curve][:-MAX_OFF_PROFILES]
                if ended_at is not None:
                    self.model.off_profile_updated[curve] = datetime.fromisoformat(ended_at).timestamp()
            except (ValueError, TypeError):
                _LOGGER.warning("Invalid stored OFF profile excluded: %s", curve)

    def _save_sync(self, result: CurveResult, accepted: bool, reason: str,
                   updated: dict[int, tuple[float, int]],
                   residual: tuple[float, int, float] | None, memory_payload: str | None,
                   evidence: dict, outdoor_cooling: list[tuple[str, int, float, int]],
                   summaries: list[tuple[str, str]]) -> tuple[bool, dict | None]:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            ended_at = _stamp(result.ended_at)
            cutoff = conn.execute("SELECT value FROM schema_meta WHERE key='cycle_cutoff'").fetchone()
            if cutoff is not None and ended_at < cutoff[0]:
                return False, None
            cursor = conn.execute("""
                INSERT OR IGNORE INTO cycles
                (rowid,id,curve_type,start_reason,off_reason,end_reason,mode,
                 started_at,off_at,peak_at,ended_at,accepted,quality_reason,preset)
                VALUES (max(COALESCE((SELECT max(rowid) FROM cycles),0),
                            COALESCE((SELECT CAST(value AS INTEGER) FROM schema_meta
                                      WHERE key='cycle_rowid'),0))+1,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (result.cycle_id, result.curve_type, result.start_reason,
                  result.off_reason, result.end_reason, result.mode,
                  _stamp(result.started_at), _stamp(result.off_at) if result.off_at is not None else None,
                  _stamp(result.peak_at) if result.peak_at is not None else None,
                  ended_at, int(accepted), reason, result.preset))
            if cursor.rowcount == 0:
                return False, None
            # Persist with the owning curve transaction; related: curve_learning.py, curve_api.py.
            if result.heating_hold is not None:
                hold = dict(result.heating_hold)
                for field in ("started_at", "off_at", "ended_at", "peak_at"):
                    if hold[field] is not None:
                        hold[field] = _stamp(hold[field])
                conn.execute("INSERT OR IGNORE INTO heating_holds (cycle_id,payload) VALUES (?,?)",
                             (result.cycle_id, json.dumps(_safe_evidence(hold), allow_nan=False)))
            conn.execute("""INSERT INTO cycle_counts VALUES (?,?,1)
                ON CONFLICT(curve_type,accepted) DO UPDATE SET total=total+1""",
                (result.curve_type, int(accepted)))
            outcome, outcome_weight = heating_outcome(result)
            conn.execute("""UPDATE cycles SET start_temperature=?,outdoor_temperature=?,off_temperature=?,peak_temperature=?,
                slope_at_off=?,residual_rise=?,peak_delay_minutes=?,heating_duration_minutes=?,heating_lag_minutes=?,
                bucket_count=?,analysis_json=?,target_peak_temperature=?,observed_peak_temperature=?,
                heating_outcome=?,outcome_weight=?,outdoor_mean=?,outdoor_end=? WHERE id=? AND curve_type=?""", (
                _safe_evidence(result.start_temperature), _safe_evidence(result.outdoor_temperature), _safe_evidence(result.off_temperature),
                _safe_evidence(result.peak_temperature), _safe_evidence(result.slope_at_off),
                _safe_evidence(result.residual_rise),
                (result.peak_at-result.off_at)/60 if result.peak_at is not None and result.off_at is not None else None,
                (result.off_at-result.started_at)/60 if result.off_at is not None else None,
                self.model._runtime_lag(result),
                len(result.buckets), json.dumps(_safe_evidence(evidence), allow_nan=False),
                _safe_evidence(result.target_peak_temperature), _safe_evidence(result.observed_peak_temperature),
                outcome, outcome_weight, _safe_evidence(result.outdoor_mean), _safe_evidence(result.outdoor_end),
                result.cycle_id, result.curve_type,
            ))
            # Accumulate absolute error once in the same cycle transaction; related: panel.js, curve_api.py.
            comparison = evidence.get("prediction_comparison") or {}
            predicted = comparison.get("predictions", {}).get("curve")
            actual = comparison.get("actual_peak")
            if (not result.sunrise_saving and result.invalid_reason != "SUNRISE_PEAK_EXCESS"
                    and type(predicted) in (int, float) and type(actual) in (int, float)
                    and isfinite(predicted) and isfinite(actual)):
                model = comparison.get("off_model")
                labels = ("curve", model) if model in OFF_MODELS else ("curve",)
                errors = [(label, abs(predicted - actual)) for label in labels]
                summary_peak = comparison.get("hybrid_summary_peak")
                if model == "hybrid" and type(summary_peak) in (int, float) and isfinite(summary_peak):
                    errors.extend((("hybrid_paired", abs(predicted - actual)),
                                   ("hybrid_summary", abs(summary_peak - actual))))
                conn.executemany("""INSERT INTO prediction_accuracy VALUES (?,1,?)
                    ON CONFLICT(model) DO UPDATE SET
                    comparison_count=comparison_count+1,
                    absolute_error_total=absolute_error_total+excluded.absolute_error_total""",
                    errors)
            conn.executemany(
                "INSERT INTO cycle_buckets VALUES (?,?,?,?)",
                [(result.cycle_id, result.curve_type, index, value)
                 for index, value in result.buckets.items()],
            )
            if accepted:
                # Commit the outdoor aggregate with the unique cooling cycle; related: curve_learning.py.
                conn.executemany("""
                    INSERT INTO outdoor_cooling_buckets VALUES (?,?,?,?)
                    ON CONFLICT(outdoor_group,bucket_index) DO UPDATE SET
                      mean_delta_c=excluded.mean_delta_c, sample_count=excluded.sample_count
                """, outdoor_cooling)
                conn.executemany("INSERT OR REPLACE INTO curve_memory VALUES (?,?)", summaries)
                if valid_profile(result.off_profile):
                    conn.execute("INSERT INTO off_response_profiles (cycle_id,curve_type,payload) VALUES (?,?,?)", (
                        result.cycle_id, result.curve_type,
                        json.dumps(result.off_profile, allow_nan=False),
                    ))
                    conn.execute("""
                        DELETE FROM off_response_profiles WHERE curve_type=? AND rowid NOT IN
                        (SELECT rowid FROM off_response_profiles WHERE curve_type=?
                         ORDER BY rowid DESC LIMIT ?)
                    """, (result.curve_type, result.curve_type, MAX_OFF_PROFILES))
                self._write_curve_standard(conn, result, updated, residual, memory_payload)
            control = (self._rebuild_control_learning(conn)
                       if result.curve_type in ("WARM_HEATING", "PREDICTIVE_WARM_HEATING") else None)
            if control is not None:
                state = self._runtime_average_state(conn)
                control[3].runtime_average_samples = state["samples"]
                control[3].runtime_accepted_cycles = state["accepted_cycles"]
            metrics = self._read_heating_metrics(conn) if control is not None else None
            sunrise = self._sunrise_runtime(conn) if result.curve_type == "SUNRISE_HEATING" else None
            return True, (metrics, control, self._control_feedback(conn) if control is not None else None, sunrise)

    # Persist aggregate changes for live and recovered cycles; related: curve_learning.py.
    @staticmethod
    def _write_curve_standard(conn, result, updated, residual, memory_payload):
        kind = result.curve_type
        ended_at = _stamp(result.ended_at)
        conn.execute("INSERT OR REPLACE INTO curve_memory VALUES (?,?)", (kind, memory_payload))
        conn.executemany("""
            INSERT INTO curve_buckets VALUES (?,?,?,?,?)
            ON CONFLICT(curve_type,bucket_index) DO UPDATE SET
              mean_delta_c=excluded.mean_delta_c, sample_count=excluded.sample_count,
              updated_at=max(curve_buckets.updated_at,excluded.updated_at)
        """, [(kind, index, mean, count, ended_at) for index, (mean, count) in updated.items()])
        if residual is not None:
            conn.execute("""
                INSERT INTO curve_features VALUES (?,?,?,?,?)
                ON CONFLICT(curve_type) DO UPDATE SET
                  residual_mean=excluded.residual_mean, residual_count=excluded.residual_count,
                  peak_delay_mean=excluded.peak_delay_mean,
                  updated_at=max(curve_features.updated_at,excluded.updated_at)
            """, (kind, *residual, ended_at))

    # Freeze the matching outcome with existing cycle evidence; related: runtime.py, curve_api.py, panel.js.
    async def save(self, result: CurveResult, comparison: dict | None = None) -> tuple[bool, str]:
        """Commit cycle, raw buckets and aggregate together; duplicate IDs do nothing."""
        if result.curve_type not in CURVE_TYPES:
            raise ValueError("Invalid curve type")
        async with self.lock:
            accepted, reason = self.model.assess(result)
            standard = self.model.buckets[result.curve_type]
            updated = {}
            residual = None
            summaries = []
            if accepted:
                candidate = deepcopy(self.model)
                candidate.apply(result)
                if result.curve_type == "COOLING" and result.cooling_outdoor_groups:
                    summaries.append((OUTDOOR_COOLING_MEMORY, _summary_payload(candidate.outdoor_cooling_memory)))
                memory_payload = json.dumps(candidate.memory[result.curve_type].dump(), allow_nan=False)
                for index, value in result.buckets.items():
                    previous = standard.get(index)
                    mean = value if previous is None else (
                        previous[0] * (1 - NEW_CYCLE_WEIGHT) + value * NEW_CYCLE_WEIGHT
                    )
                    updated[index] = (mean, 1 if previous is None else previous[1] + 1)
                if result.residual_rise is not None:
                    previous = self.model.residual.get(result.curve_type)
                    mean = result.residual_rise if previous is None else (
                        previous[0] * (1 - NEW_CYCLE_WEIGHT)
                        + result.residual_rise * NEW_CYCLE_WEIGHT
                    )
                    delay = (result.peak_at - result.off_at) / 60
                    old_delay = self.model.peak_delay.get(result.curve_type)
                    delay_mean = delay if old_delay is None else (
                        old_delay[0] * (1 - NEW_CYCLE_WEIGHT)
                        + delay * NEW_CYCLE_WEIGHT
                    )
                    residual = (mean, 1 if previous is None else previous[1] + 1, delay_mean)
            else:
                candidate = None
                memory_payload = None
            before_memory = self.model.memory[result.curve_type]
            after_memory = candidate.memory[result.curve_type] if candidate else before_memory
            before = _learning_state(before_memory, result)
            after = _learning_state(after_memory, result)
            # Detect promotions per bucket/group, not the maximum promotion counter; related: curve_memory.py.
            def promotions(memory):
                return {**{f"bucket:{index}": bucket.promotions for index, bucket in memory.long_term.items()},
                        **{f"response:{group}:{name}": bucket.promotions
                           for group, rows in memory.responses.items() for name, bucket in rows["long_term"].items()}}
            old_promotions, new_promotions = promotions(before_memory), promotions(after_memory)
            evidence = {"version": 1, "checks": self.model.quality_details(result),
                        "peak_confirmation": {"minimum_drop_c": PEAK_CONFIRM_DROP,
                                              "minimum_reports": PEAK_CONFIRM_REPORTS,
                                              "minimum_extra_drop_c": PEAK_CONFIRM_EXTRA_DROP,
                                              "observed_drop_c": result.peak_confirmation_drop,
                                              "observed_extra_drop_c": result.peak_confirmation_extra_drop,
                                              "observed_first_drop_c": (result.peak_confirmation_drop-result.peak_confirmation_extra_drop
                                                                        if result.peak_confirmation_drop is not None
                                                                        and result.peak_confirmation_extra_drop is not None else None),
                                              "observed_reports": result.peak_confirmation_reports,
                                              "maximum_wait_minutes": (None if
                                                  (result.sunrise_peak_excess or {}).get("ends_observation")
                                                  else self.max_peak_wait_minutes)},
                        "learning": {"applied": accepted, "before": before, "after": after,
                                     "promoted": any(count > old_promotions.get(key, 0)
                                                     for key, count in new_promotions.items())}}
            # Expose the recorded OFF threshold inputs to frontend/panel.js; related: curve_learning.py.
            if result.off_setpoint is not None:
                evidence["off_setpoint"] = result.off_setpoint
            if result.off_stop_tolerance is not None:
                evidence["off_stop_tolerance"] = result.off_stop_tolerance
            if result.solar_samples is not None:
                evidence["solar_samples"] = result.solar_samples
            # Keep compact cooling diagnostics after raw bucket retention; related: curve_learning.py.
            if accepted and result.curve_type == "COOLING" and result.peak_at is not None:
                evidence["cooling_decline"] = cooling_decline_profile(result.buckets)
            # Preserve only the same heating round's original prediction; never reconstruct legacy forecasts.
            if result.sunrise_peak_excess is not None:
                evidence["sunrise_peak_excess"] = result.sunrise_peak_excess
            if result.sunrise_saving:
                evidence["sunrise_saving"] = True
                if result.sunrise_prediction is not None:
                    evidence["sunrise_prediction"] = result.sunrise_prediction
            # Related: runtime.py _queue_curve_results and panel.js prediction history.
            if (result.curve_type in ("WARM_HEATING", "PREDICTIVE_WARM_HEATING") and isinstance(comparison, dict)
                    and result.off_at is not None and isinstance(comparison.get("off_at"), (int, float))
                    and abs(comparison["off_at"] - result.off_at) <= 30):
                evidence["prediction_comparison"] = deepcopy(comparison)
                if accepted and not result.sunrise_saving and comparison.get("off_model") in ("hybrid", "runtime"):
                    evidence["control_feedback"] = 1
            outdoor_cooling = [
                (group, index, *candidate.outdoor_cooling[group, index])
                for index, group in result.cooling_outdoor_groups.items()
                if candidate is not None and result.curve_type == "COOLING" and index in result.buckets
                and (group, index) in candidate.outdoor_cooling
            ]
            inserted, saved = await asyncio.to_thread(
                self._save_sync, result, accepted, reason, updated, residual, memory_payload,
                evidence, outdoor_cooling, summaries
            )
            if inserted:
                metrics, control, feedback, sunrise = saved
                if sunrise is not None:
                    self.sunrise_runtime_model = sunrise
                if metrics is not None:
                    self.heating_metrics = metrics
                if accepted:
                    self.model = candidate
                else:
                    self.model.rejected[result.curve_type] += 1
                if control is not None:
                    self.control_feedback = feedback
                    runtime, peaks, shortfalls, summary = control
                    self.model.load_runtime_samples(runtime)
                    self.model.runtime_average_samples = summary.runtime_average_samples
                    self.model.runtime_accepted_cycles = summary.runtime_accepted_cycles
                    self.model.peak_samples = peaks
                    self.model.shortfall_samples = list(reversed(shortfalls))
                    self.model.hybrid_residual_memory = summary.hybrid_residual_memory
                    self.model.hybrid_duration_memory = summary.hybrid_duration_memory
                    self.model.runtime_rise_memory = summary.runtime_rise_memory
                    self.model.runtime_duration_memory = summary.runtime_duration_memory
                self.model.last_reason = reason
                if accepted:
                    _LOGGER.info("Curve segment learned: %s", result.curve_type)
                elif result.sunrise_saving:
                    _LOGGER.info("Sunrise saving segment recorded: %s", result.curve_type)
                else:
                    _LOGGER.warning("Curve segment excluded: %s (%s)", result.curve_type, reason)
            return accepted and inserted, reason

    # Read an immutable SQLite snapshot without migrations, PRAGMAs or writes; related: curve_api.py.
    # Update diagnostic evidence without changing admitted learning; related: runtime.py, panel.js.
    def _update_sunrise_excess_sync(self, cycle_id: str, curve_type: str, excess: dict) -> CurveStandards | None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """UPDATE cycles SET analysis_json=json_set(
                       COALESCE(analysis_json, '{}'), '$.sunrise_peak_excess', json(?))
                   WHERE id=? AND curve_type=?""",
                (json.dumps(excess, allow_nan=False), cycle_id, curve_type),
            )
            return self._sunrise_runtime(conn) if curve_type == "SUNRISE_HEATING" else None

    async def update_sunrise_excess(self, cycle_id: str, curve_type: str, excess: dict) -> None:
        async with self.lock:
            model = await asyncio.to_thread(self._update_sunrise_excess_sync, cycle_id, curve_type, excess)
            if model is not None:
                self.sunrise_runtime_model = model

    # Attach the measured OFF interval to the existing hold; related: curve_learning.py, runtime.py.
    def _save_below_target_cooling_sync(self, cycle_id: str, observation: dict) -> None:
        record = {**observation, "started_at": _stamp(observation["started_at"]),
                  "ended_at": _stamp(observation["ended_at"])}
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """UPDATE heating_holds SET payload=json_set(payload, '$.below_target_cooling', json(?))
                   WHERE cycle_id=? AND json_type(payload, '$.below_target_cooling') IS NULL""",
                (json.dumps(_safe_evidence(record), allow_nan=False), cycle_id),
            )

    async def save_below_target_cooling(self, cycle_id: str, observation: dict) -> None:
        async with self.lock:
            await asyncio.to_thread(self._save_below_target_cooling_sync, cycle_id, observation)

    def _read_cycles_sync(self, limit: int, curve_type: str | None, accepted: bool | None,
                          offset: int, snapshot: int | None, heating_only: bool = False) -> dict:
        uri = self.path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            latest_row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM cycles").fetchone()[0]
            snapshot = latest_row if snapshot is None else snapshot
            # Hide retired curve types from every analysis page and JSON export.
            # Legacy rows remain in SQLite for compatibility but are never active data.
            placeholders = ",".join("?" for _ in CURVE_TYPES)
            clauses, parameters = [f"curve_type IN ({placeholders})"], list(CURVE_TYPES)
            if heating_only:
                clauses.append("curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')")
            if curve_type is not None:
                clauses.append("curve_type=?")
                parameters.append(curve_type)
            if accepted is not None:
                clauses.append("accepted=?")
                parameters.append(int(accepted))
            clauses.append("rowid<=?")
            parameters.append(snapshot)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            total = conn.execute("SELECT count(*) FROM cycles" + where, parameters).fetchone()[0]
            rows = conn.execute("SELECT * FROM cycles" + where +
                                " ORDER BY ended_at DESC, rowid DESC LIMIT ? OFFSET ?",
                                [*parameters, limit, offset]).fetchall()
            cycles = []
            for row in rows:
                cycle = dict(row)
                cycle["accepted"] = bool(cycle["accepted"])
                raw = conn.execute("SELECT bucket_index,delta_c FROM cycle_buckets "
                                   "WHERE cycle_id=? AND curve_type=? ORDER BY bucket_index",
                                   (cycle["id"], cycle["curve_type"])).fetchall()
                cycle["buckets"] = [{"index": bucket[0], "minutes": (bucket[0]+1)*5,
                                     "delta": _safe_evidence(bucket[1])} for bucket in raw]
                cycle["raw_status"] = ("available" if raw else "expired" if cycle.pop("raw_pruned")
                                       else "empty" if cycle["bucket_count"] == 0 else "unavailable")
                cycle.pop("raw_pruned", None)
                try:
                    analysis = json.loads(cycle.pop("analysis_json") or "null")
                    cycle["analysis"] = analysis if isinstance(analysis, dict) else None
                except (ValueError, TypeError):
                    cycle["analysis"] = None
                # Timestamps are exact stored UTC values; legacy durations need no temperature reconstruction.
                for field, end, start in (("heating_duration_minutes", "off_at", "started_at"),
                                          ("peak_delay_minutes", "peak_at", "off_at")):
                    if cycle[field] is None and cycle[end] and cycle[start]:
                        try:
                            cycle[field] = (datetime.fromisoformat(cycle[end])-datetime.fromisoformat(cycle[start])).total_seconds()/60
                        except (ValueError, TypeError):
                            cycle[field] = None
                cycles.append(cycle)
            # Aggregate covers every committed comparison, independently of list limits and raw TTL.
            accuracy = conn.execute("SELECT comparison_count,absolute_error_total FROM prediction_accuracy WHERE model='curve'").fetchone()
            model_accuracy = {model: {"count": count, "mean_absolute_error": total_error/count if count else None}
                              for model, count, total_error in conn.execute(
                                  "SELECT model,comparison_count,absolute_error_total FROM prediction_accuracy WHERE model IN (?,?,?)", OFF_MODELS)}
            paired_accuracy = {model: {"count": count, "mean_absolute_error": total_error / count}
                               for model, count, total_error in conn.execute("""
                                   SELECT model,comparison_count,absolute_error_total FROM prediction_accuracy
                                   WHERE model IN ('hybrid_paired','hybrid_summary') AND comparison_count > 0
                               """)}
            # Count confirmed coverage even when residual control abstained; related: runtime.py and panel.js.
            coverage = {available: count for available, count in conn.execute(
                """SELECT json_extract(analysis_json, '$.prediction_comparison.off_control.forecast_available'),
                          count(*) FROM cycles""" + where + """
                   AND curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')
                   AND json_valid(analysis_json) AND peak_at IS NOT NULL
                   GROUP BY 1""", parameters) if available in (0, 1)}
            return {"status": "ready", "unit": "°C", "cycles": cycles,
                    "accuracy": {"count": accuracy[0] if accuracy else 0,
                                 "mean_absolute_error": accuracy[1]/accuracy[0] if accuracy and accuracy[0] else None},
                    "accuracy_by_model": model_accuracy,
                    "hybrid_summary_accuracy": paired_accuracy,
                    "residual_coverage": {"predicted": coverage.get(1, 0), "withheld": coverage.get(0, 0)},
                    "total": total, "limit": limit, "offset": offset,
                    "snapshot": snapshot, "raw_retention_days": RAW_RETENTION_DAYS,
                    "cycle_retention_days": CYCLE_RETENTION_DAYS}

    # Store independent, idempotent observations. Related: runtime.py and curve_api.py.
    def _write_operation_history(self, events, backfill=False):
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT OR IGNORE INTO operation_history VALUES (?,?,?)",
                [(kind, stamp, json.dumps(payload, allow_nan=False)) for kind, stamp, payload in events],
            )
            if backfill:
                conn.execute("INSERT OR IGNORE INTO schema_meta VALUES ('operation_history_backfill', '1')")

    async def save_operation_history(self, events, *, backfill=False):
        async with self.lock:
            await asyncio.to_thread(self._write_operation_history, events, backfill)

    def _operation_backfilled(self):
        with closing(self._connect()) as conn:
            return conn.execute("SELECT 1 FROM schema_meta WHERE key='operation_history_backfill'").fetchone() is not None

    async def operation_backfilled(self):
        return await asyncio.to_thread(self._operation_backfilled)

    def _read_operation_history(self, start, end):
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT kind, recorded_at, payload FROM operation_history
                   WHERE recorded_at BETWEEN ? AND ?
                      OR (kind, recorded_at) IN (
                          SELECT kind, max(recorded_at) FROM operation_history
                          WHERE recorded_at < ? GROUP BY kind)
                   ORDER BY recorded_at, kind""", (start, end, start),
            ).fetchall()
        return [{"kind": kind, "at": stamp, "data": json.loads(payload)} for kind, stamp, payload in rows]

    async def read_operation_history(self, start, end):
        return await asyncio.to_thread(self._read_operation_history, start, end)

    async def read_cycles(self, limit: int = 30, curve_type: str | None = None,
                          accepted: bool | None = None, offset: int = 0,
                          snapshot: int | None = None, heating_only: bool = False) -> dict:
        """Serialize a bounded permission-scoped read with committed writes; related: curve_api.py."""
        if (type(heating_only) is not bool or type(limit) is not int or not 1 <= limit <= 100
                or type(offset) is not int or offset < 0
                or snapshot is not None and (type(snapshot) is not int or snapshot < 0)
                or curve_type is not None and curve_type not in CURVE_TYPES
                or accepted is not None and type(accepted) is not bool):
            raise ValueError("Invalid cycle query")
        async with self.lock:
            return await asyncio.to_thread(self._read_cycles_sync, limit, curve_type, accepted, offset, snapshot, heating_only)

    # Page hold records independently of expired curve metadata; related: curve_api.py, panel.js.
    def _read_heating_holds_sync(self, limit: int, offset: int, snapshot: int | None) -> dict:
        with closing(self._connect()) as conn:
            if snapshot is None:
                snapshot = conn.execute("SELECT COALESCE(MAX(sequence),0) FROM heating_holds").fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM heating_holds WHERE sequence <= ?", (snapshot,)).fetchone()[0]
            rows = conn.execute(
                "SELECT payload FROM heating_holds WHERE sequence <= ? ORDER BY sequence DESC LIMIT ? OFFSET ?",
                (snapshot, limit, offset),
            )
            return {"status": "ready", "snapshot": snapshot, "total": total,
                    "rows": [json.loads(row[0]) for row in rows]}

    async def read_heating_holds(self, limit: int, offset: int, snapshot: int | None) -> dict:
        async with self.lock:
            return await asyncio.to_thread(self._read_heating_holds_sync, limit, offset, snapshot)

    def _cleanup_sync(self, cutoff: str, cycle_cutoff: str) -> tuple[str, list]:
        with closing(self._connect()) as conn, conn:
            conn.execute("""UPDATE cycles SET raw_pruned=1 WHERE ended_at < ? AND EXISTS (
                SELECT 1 FROM cycle_buckets b WHERE b.cycle_id=cycles.id AND b.curve_type=cycles.curve_type)
            """, (cutoff,))
            conn.execute("""
                DELETE FROM cycle_buckets WHERE (cycle_id, curve_type) IN
                (SELECT id, curve_type FROM cycles WHERE ended_at < ?)
            """, (cutoff,))
            conn.execute("""INSERT INTO schema_meta VALUES ('cycle_cutoff',?)
                ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)""", (cycle_cutoff,))
            cutoff = conn.execute("SELECT value FROM schema_meta WHERE key='cycle_cutoff'").fetchone()[0]
            # Keep pagination snapshots valid even if every retained cycle is removed; related: curve_api.py.
            conn.execute("""INSERT INTO schema_meta VALUES ('cycle_rowid',(SELECT COALESCE(max(rowid),0) FROM cycles))
                ON CONFLICT(key) DO UPDATE SET value=max(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER))""")
            conn.execute("""DELETE FROM off_response_profiles WHERE (cycle_id,curve_type) IN
                (SELECT id,curve_type FROM cycles WHERE ended_at < ?)""", (cutoff,))
            conn.execute("DELETE FROM cycles WHERE ended_at < ?", (cutoff,))
            conn.execute("DELETE FROM operation_history WHERE recorded_at < ?",
                         (datetime.fromisoformat(cutoff).timestamp(),))
            return cutoff, self._profile_rows(conn), self._control_feedback(conn)

    async def cleanup(self, *, now: datetime | None = None) -> None:
        """Expire detailed cycles and raw buckets independently of persistent summaries."""
        now = (datetime.now(UTC) if now is None else now.astimezone(UTC)).replace(microsecond=0)
        cutoff = (now - timedelta(days=RAW_RETENTION_DAYS)).isoformat()
        cycle_cutoff = (now - timedelta(days=CYCLE_RETENTION_DAYS)).isoformat()
        async with self.lock:
            persisted, profiles, self.control_feedback = await asyncio.to_thread(self._cleanup_sync, cutoff, cycle_cutoff)
            self.model.expire_cycle_samples(datetime.fromisoformat(persisted).timestamp())
            self.sunrise_runtime_model.expire_cycle_samples(datetime.fromisoformat(persisted).timestamp())
            self._load_profiles(profiles)
            self.heating_metrics = await asyncio.to_thread(self._read_heating_metrics)

    # Reset one entry transactionally without touching the schema or any other entry; related: runtime.py.
    def _reset_sync(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM cycle_buckets")
            conn.execute("DELETE FROM cycles")
            conn.execute("DELETE FROM cycle_counts")
            conn.execute("DELETE FROM heating_holds")
            conn.execute("DELETE FROM curve_buckets")
            conn.execute("DELETE FROM outdoor_cooling_buckets")
            conn.execute("DELETE FROM curve_features")
            conn.execute("DELETE FROM curve_memory")
            conn.execute("DELETE FROM runtime_average")
            conn.execute("DELETE FROM sunrise_runtime_average")
            conn.execute("DELETE FROM off_response_profiles")
            conn.execute("DELETE FROM prediction_accuracy")
            conn.execute("DELETE FROM operation_history WHERE kind != 'heater'")

    async def reset(self) -> None:
        """Clear learned data and prediction history, then publish the empty model."""
        async with self.lock:
            await asyncio.to_thread(self._reset_sync)
            self.model = CurveStandards(self.max_peak_wait_minutes)
            self.sunrise_runtime_model = CurveStandards(self.max_peak_wait_minutes)
            self.heating_metrics = {}
            self.control_feedback = []

    async def remove(self) -> None:
        """Delete this entry's private database only after config entry removal."""
        async with self.lock:
            for suffix in ("", "-wal", "-shm"):
                await asyncio.to_thread(Path(str(self.path) + suffix).unlink, missing_ok=True)
