"""Persist per-entry curve standards and recent raw buckets; related: curve_learning.py, runtime.py."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import closing
from datetime import UTC, datetime, timedelta
import logging
import json
from copy import deepcopy
from pathlib import Path
import sqlite3
from math import isfinite

from .curve_learning import (
    BUCKET_SECONDS, CURVE_TYPES, HYBRID_BIN_MINUTES, MAX_COOLING_SECONDS, MAX_HYBRID_RUNTIME_MINUTES,
    MAX_RESIDUAL_RISE, MAX_RUNTIME_SUMMARY_RISE, NEW_CYCLE_WEIGHT, RUNTIME_SAMPLE_QUALITY,
    CurveResult, CurveStandards, heating_outcome,
)
from .curve_memory import CurveMemory
from .peak_prediction import control_hold, sample as peak_sample
from .off_response import MAX_OFF_PROFILES, valid_profile
from .history import PEAK_CONFIRM_DROP, PEAK_CONFIRM_EXTRA_DROP, PEAK_CONFIRM_REPORTS
from .const import DEFAULT_PEAK_OBSERVATION_HOURS, OFF_MODELS

_LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 14
RAW_RETENTION_DAYS = 7
CYCLE_RETENTION_DAYS = 30
OUTDOOR_COOLING_MEMORY = "outdoor_cooling"
HYBRID_RESIDUAL_MEMORY = "hybrid_residual_measured"
RUNTIME_RISE_MEMORY = "runtime_rise_measured"
HYBRID_DURATION_MEMORY = "hybrid_duration"
RUNTIME_DURATION_MEMORY = "runtime_duration"
HEATING_METRIC_WINDOW = 10
PEAK_ERROR_TOLERANCE = 0.3


# Store bounded model summaries independently of cycle history; related: curve_learning.py.
def _summary_payload(groups: dict[str, CurveMemory]) -> str:
    return json.dumps({group: memory.dump() for group, memory in groups.items() if memory.current},
                      allow_nan=False)


def _restore_summary(payload: str, groups: dict[str, CurveMemory], max_index: int,
                     max_delta: float = 3.0) -> None:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Invalid summary groups")
    restored = {}
    for group in groups.keys() & data.keys():
        memory = CurveMemory.restore(data[group], max_delta=max_delta)
        if memory.responses or any(index > max_index for index in memory.current.keys() | memory.long_term.keys()):
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

    def _open_sync(self) -> tuple[list, list, list, list, list, list, list, list, list]:
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
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
                CREATE TABLE IF NOT EXISTS prediction_accuracy (
                    model TEXT PRIMARY KEY,
                    comparison_count INTEGER NOT NULL,
                    absolute_error_total REAL NOT NULL
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
            if version < 14:
                self._rebuild_control_learning(conn)
                memories = conn.execute("SELECT curve_type, payload FROM curve_memory").fetchall()
            runtime_samples, peak_samples, shortfalls = self._control_samples(conn)
            outdoor_cooling = conn.execute(
                "SELECT outdoor_group, bucket_index, mean_delta_c, sample_count FROM outdoor_cooling_buckets"
            ).fetchall()
            return (rows, counts, features, profiles, memories, runtime_samples, peak_samples,
                    shortfalls, outdoor_cooling, self._control_feedback(conn))

    # Rebuild retained OFF evidence from the same candidates on migration and save.
    # Related: peak_prediction.py, runtime.py, frontend/panel.js.
    @staticmethod
    def _control_candidates(conn):
        rows = conn.execute("""SELECT rowid,start_temperature,off_temperature,peak_temperature,
            COALESCE(heating_duration_minutes,(julianday(off_at)-julianday(started_at))*1440),
            outdoor_temperature,CAST(strftime('%s',ended_at) AS REAL),outcome_weight,
            (julianday(peak_at)-julianday(off_at))*1440, off_at
            FROM cycles WHERE preset!='away' AND curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')
            AND quality_reason IN (?,?,?) AND peak_at>=off_at ORDER BY ended_at,rowid""",
            RUNTIME_SAMPLE_QUALITY)
        candidates = {}
        for rowid, *features, delay, off_at in rows:
            item = peak_sample(*features)
            if item is not None and delay is not None:
                candidates[rowid] = {**item, "delay": delay, "off_at": off_at}
        return candidates

    def _rebuild_control_learning(self, conn):
        candidates = self._control_candidates(conn)
        conn.executemany("UPDATE cycles SET control_learning_hold=? WHERE rowid=?",
                         [(control_hold(item, candidates.values()), rowid) for rowid, item in candidates.items()])
        runtime, peaks, shortfalls = self._control_samples(conn)
        summary = CurveStandards(self.max_peak_wait_minutes)
        for item in peaks:
            summary.learn_runtime_rise(item)
            summary.learn_hybrid_residual(item)
        conn.executemany("INSERT OR REPLACE INTO curve_memory VALUES (?,?)", self._off_summary_payloads(summary))
        return runtime, peaks, shortfalls, summary

    @staticmethod
    def _control_feedback(conn):
        rows = conn.execute("""SELECT analysis_json FROM cycles
            WHERE control_learning_hold IS NULL AND accepted=1 AND curve_type!='COOLING'
            AND json_extract(analysis_json,'$.control_feedback')=1
            ORDER BY ended_at DESC,rowid DESC LIMIT 200""")
        return [json.loads(row[0])["prediction_comparison"] for row in reversed(rows.fetchall())]

    @staticmethod
    def _control_samples(conn):
            runtime_samples = conn.execute(
                """SELECT COALESCE(heating_duration_minutes,(julianday(off_at)-julianday(started_at))*1440),
                          peak_temperature-start_temperature, heating_lag_minutes,
                          outdoor_temperature, CAST(strftime('%s', ended_at) AS REAL), outcome_weight
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
                COALESCE(heating_duration_minutes,(julianday(off_at)-julianday(started_at))*1440), outdoor_temperature,
                CAST(strftime('%s', ended_at) AS REAL), outcome_weight FROM cycles
                WHERE accepted=1 AND preset!='away' AND peak_at IS NOT NULL AND control_learning_hold IS NULL
                AND curve_type IN ('WARM_HEATING','PREDICTIVE_WARM_HEATING')
                ORDER BY ended_at,rowid""")
            peak_samples = deque(maxlen=200)
            for row in peak_rows:
                item = peak_sample(*row)
                if item is None:
                    continue
                peak_samples.append(item)
            shortfalls = conn.execute("""SELECT heating_duration_minutes, start_temperature,
                target_peak_temperature, outdoor_temperature, CAST(strftime('%s', ended_at) AS REAL)
                FROM cycles WHERE off_reason='PREDICTIVE_STOP'
                AND quality_reason!='SUNRISE_SAVING' AND control_learning_hold IS NULL
                AND heating_outcome IN ('missed','restarted_below_target')
                AND heating_duration_minutes > 0 AND start_temperature IS NOT NULL
                AND target_peak_temperature IS NOT NULL ORDER BY ended_at DESC LIMIT 200""").fetchall()
            return runtime_samples, list(peak_samples), shortfalls

    async def open(self) -> None:
        async with self.lock:
            (rows, counts, features, profiles, memories, runtime_samples, peak_samples,
             shortfalls, outdoor_cooling, self.control_feedback) = await asyncio.to_thread(self._open_sync)
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
                OUTDOOR_COOLING_MEMORY: (self.model.outdoor_cooling_memory, MAX_COOLING_SECONDS // int(BUCKET_SECONDS) - 1, 3),
                HYBRID_RESIDUAL_MEMORY: (self.model.hybrid_residual_memory, max_index, MAX_RESIDUAL_RISE),
                RUNTIME_RISE_MEMORY: (self.model.runtime_rise_memory, max_index, MAX_RUNTIME_SUMMARY_RISE),
                HYBRID_DURATION_MEMORY: (self.model.hybrid_duration_memory, max_index, MAX_HYBRID_RUNTIME_MINUTES),
                RUNTIME_DURATION_MEMORY: (self.model.runtime_duration_memory, max_index, MAX_HYBRID_RUNTIME_MINUTES),
            }
            for curve, payload in memories:
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
            conn.execute("""INSERT INTO cycle_counts VALUES (?,?,1)
                ON CONFLICT(curve_type,accepted) DO UPDATE SET total=total+1""",
                (result.curve_type, int(accepted)))
            outcome, outcome_weight = heating_outcome(result)
            conn.execute("""UPDATE cycles SET start_temperature=?,outdoor_temperature=?,off_temperature=?,peak_temperature=?,
                slope_at_off=?,residual_rise=?,peak_delay_minutes=?,heating_duration_minutes=?,heating_lag_minutes=?,
                bucket_count=?,analysis_json=?,target_peak_temperature=?,observed_peak_temperature=?,
                heating_outcome=?,outcome_weight=? WHERE id=? AND curve_type=?""", (
                _safe_evidence(result.start_temperature), _safe_evidence(result.outdoor_temperature), _safe_evidence(result.off_temperature),
                _safe_evidence(result.peak_temperature), _safe_evidence(result.slope_at_off),
                _safe_evidence(result.residual_rise),
                (result.peak_at-result.off_at)/60 if result.peak_at is not None and result.off_at is not None else None,
                (result.off_at-result.started_at)/60 if result.off_at is not None else None,
                self.model._runtime_lag(result),
                len(result.buckets), json.dumps(_safe_evidence(evidence), allow_nan=False),
                _safe_evidence(result.target_peak_temperature), _safe_evidence(result.observed_peak_temperature),
                outcome, outcome_weight,
                result.cycle_id, result.curve_type,
            ))
            # Accumulate absolute error once in the same cycle transaction; related: panel.js, curve_api.py.
            comparison = evidence.get("prediction_comparison") or {}
            predicted = comparison.get("predictions", {}).get("curve")
            actual = comparison.get("actual_peak")
            if (not result.sunrise_saving and type(predicted) in (int, float) and type(actual) in (int, float)
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
                conn.execute("INSERT OR REPLACE INTO curve_memory VALUES (?,?)",
                             (result.curve_type, memory_payload))
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
                now = _stamp(result.ended_at)
                conn.executemany("""
                    INSERT INTO curve_buckets VALUES (?,?,?,?,?)
                    ON CONFLICT(curve_type,bucket_index) DO UPDATE SET
                      mean_delta_c=excluded.mean_delta_c,
                      sample_count=excluded.sample_count,
                      updated_at=excluded.updated_at
                """, [(result.curve_type, index, mean, count, now)
                       for index, (mean, count) in updated.items()])
                if residual is not None:
                    conn.execute("""
                        INSERT INTO curve_features VALUES (?,?,?,?,?)
                        ON CONFLICT(curve_type) DO UPDATE SET
                          residual_mean=excluded.residual_mean,
                          residual_count=excluded.residual_count,
                          peak_delay_mean=excluded.peak_delay_mean,
                          updated_at=excluded.updated_at
                    """, (result.curve_type, residual[0], residual[1], residual[2], now))
            control = self._rebuild_control_learning(conn) if result.curve_type != "COOLING" else None
            metrics = self._read_heating_metrics(conn) if control is not None else None
            return True, (metrics, control, self._control_feedback(conn) if control is not None else None)

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
                                              "maximum_wait_minutes": self.max_peak_wait_minutes},
                        "learning": {"applied": accepted, "before": before, "after": after,
                                     "promoted": any(count > old_promotions.get(key, 0)
                                                     for key, count in new_promotions.items())}}
            # Preserve only the same heating round's original prediction; never reconstruct legacy forecasts.
            if result.sunrise_saving:
                evidence["sunrise_saving"] = True
                if result.sunrise_prediction is not None:
                    evidence["sunrise_prediction"] = result.sunrise_prediction
            # Related: runtime.py _queue_curve_results and panel.js prediction history.
            if (result.curve_type != "COOLING" and isinstance(comparison, dict)
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
                metrics, control, feedback = saved
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
            clauses, parameters = ["curve_type IN (?,?,?)"], list(CURVE_TYPES)
            if heating_only:
                clauses.append("curve_type != 'COOLING'")
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
            return {"status": "ready", "unit": "°C", "cycles": cycles,
                    "accuracy": {"count": accuracy[0] if accuracy else 0,
                                 "mean_absolute_error": accuracy[1]/accuracy[0] if accuracy and accuracy[0] else None},
                    "accuracy_by_model": model_accuracy,
                    "hybrid_summary_accuracy": paired_accuracy,
                    "total": total, "limit": limit, "offset": offset,
                    "snapshot": snapshot, "raw_retention_days": RAW_RETENTION_DAYS,
                    "cycle_retention_days": CYCLE_RETENTION_DAYS}

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
            return cutoff, self._profile_rows(conn), self._control_feedback(conn)

    async def cleanup(self, *, now: datetime | None = None) -> None:
        """Expire detailed cycles and raw buckets independently of persistent summaries."""
        now = (datetime.now(UTC) if now is None else now.astimezone(UTC)).replace(microsecond=0)
        cutoff = (now - timedelta(days=RAW_RETENTION_DAYS)).isoformat()
        cycle_cutoff = (now - timedelta(days=CYCLE_RETENTION_DAYS)).isoformat()
        async with self.lock:
            persisted, profiles, self.control_feedback = await asyncio.to_thread(self._cleanup_sync, cutoff, cycle_cutoff)
            self.model.expire_cycle_samples(datetime.fromisoformat(persisted).timestamp())
            self._load_profiles(profiles)
            self.heating_metrics = await asyncio.to_thread(self._read_heating_metrics)

    # Reset one entry transactionally without touching the schema or any other entry; related: runtime.py.
    def _reset_sync(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM cycle_buckets")
            conn.execute("DELETE FROM cycles")
            conn.execute("DELETE FROM cycle_counts")
            conn.execute("DELETE FROM curve_buckets")
            conn.execute("DELETE FROM outdoor_cooling_buckets")
            conn.execute("DELETE FROM curve_features")
            conn.execute("DELETE FROM curve_memory")
            conn.execute("DELETE FROM off_response_profiles")
            conn.execute("DELETE FROM prediction_accuracy")

    async def reset(self) -> None:
        """Clear learned data and prediction history, then publish the empty model."""
        async with self.lock:
            await asyncio.to_thread(self._reset_sync)
            self.model = CurveStandards(self.max_peak_wait_minutes)
            self.heating_metrics = {}
            self.control_feedback = []

    async def remove(self) -> None:
        """Delete this entry's private database only after config entry removal."""
        async with self.lock:
            for suffix in ("", "-wal", "-shm"):
                await asyncio.to_thread(Path(str(self.path) + suffix).unlink, missing_ok=True)
