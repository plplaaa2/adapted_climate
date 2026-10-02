"""Persist per-entry curve standards and recent raw buckets; related: curve_learning.py, runtime.py."""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
import logging
import json
from copy import deepcopy
from pathlib import Path
import sqlite3

from .curve_learning import CURVE_TYPES, NEW_CYCLE_WEIGHT, CurveResult, CurveStandards
from .curve_memory import CurveMemory
from .off_response import MAX_OFF_PROFILES, valid_profile

_LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 3
RAW_RETENTION_DAYS = 7


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


class CurveStore:
    """Serialize SQLite work in a worker thread so HA's event loop stays free."""

    def __init__(self, hass, entry_id: str) -> None:
        # HA config.path keeps the database in its private .storage directory.
        self.path = Path(hass.config.path(".storage", f"adaptive_floor_heating_{entry_id}_curves.sqlite3"))
        self.lock = asyncio.Lock()
        self.model = CurveStandards()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _open_sync(self) -> tuple[list, list, list, list, list]:
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
                CREATE INDEX IF NOT EXISTS idx_cycles_ended ON cycles(ended_at);
                CREATE TABLE IF NOT EXISTS off_response_profiles (
                    cycle_id TEXT NOT NULL, curve_type TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (cycle_id, curve_type)
                );
                CREATE TABLE IF NOT EXISTS curve_memory (
                    curve_type TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
            """)
            existing = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if existing is not None and int(existing[0]) not in (1, 2, SCHEMA_VERSION):
                raise ValueError("Unsupported curve database version")
            # Additive migration preserves old aggregates; unknown OFF trajectories stay unknown.
            # Related: curve_learning.py and model_operation.md.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cycles)")}
            if "preset" not in columns:
                conn.execute("ALTER TABLE cycles ADD COLUMN preset TEXT NOT NULL DEFAULT 'unknown'")
            conn.execute("INSERT OR REPLACE INTO schema_meta VALUES ('version', ?)", (str(SCHEMA_VERSION),))
            rows = conn.execute(
                "SELECT curve_type, bucket_index, mean_delta_c, sample_count FROM curve_buckets"
            ).fetchall()
            counts = conn.execute(
                "SELECT curve_type, accepted, count(*) FROM cycles GROUP BY curve_type, accepted"
            ).fetchall()
            features = conn.execute(
                "SELECT curve_type, residual_mean, residual_count, peak_delay_mean FROM curve_features"
            ).fetchall()
            profiles = conn.execute(
                """SELECT p.curve_type, p.payload, c.ended_at FROM off_response_profiles p
                   LEFT JOIN cycles c ON c.id=p.cycle_id AND c.curve_type=p.curve_type
                   ORDER BY p.rowid"""
            ).fetchall()
            memories = conn.execute("SELECT curve_type, payload FROM curve_memory").fetchall()
            return rows, counts, features, profiles, memories

    async def open(self) -> None:
        async with self.lock:
            rows, counts, features, profiles, memories = await asyncio.to_thread(self._open_sync)
            self.model = CurveStandards()
            self.model.load(rows)
            for curve, payload in memories:
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
            for curve, payload, ended_at in profiles:
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
            for curve, mean, count, delay in features:
                if curve in CURVE_TYPES and count > 0:
                    self.model.residual[curve] = (mean, count)
                    self.model.peak_delay[curve] = (delay, count)
            for curve, accepted, count in counts:
                if curve in CURVE_TYPES:
                    target = self.model.accepted if accepted else self.model.rejected
                    target[curve] = count

    def _save_sync(self, result: CurveResult, accepted: bool, reason: str,
                   updated: dict[int, tuple[float, int]],
                   residual: tuple[float, int, float] | None, memory_payload: str | None) -> bool:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("""
                INSERT OR IGNORE INTO cycles
                (id,curve_type,start_reason,off_reason,end_reason,mode,
                 started_at,off_at,peak_at,ended_at,accepted,quality_reason,preset)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (result.cycle_id, result.curve_type, result.start_reason,
                  result.off_reason, result.end_reason, result.mode,
                  _stamp(result.started_at), _stamp(result.off_at) if result.off_at is not None else None,
                  _stamp(result.peak_at) if result.peak_at is not None else None,
                  _stamp(result.ended_at), int(accepted), reason, result.preset))
            if cursor.rowcount == 0:
                return False
            conn.executemany(
                "INSERT INTO cycle_buckets VALUES (?,?,?,?)",
                [(result.cycle_id, result.curve_type, index, value)
                 for index, value in result.buckets.items()],
            )
            if accepted:
                conn.execute("INSERT OR REPLACE INTO curve_memory VALUES (?,?)",
                             (result.curve_type, memory_payload))
                if valid_profile(result.off_profile):
                    conn.execute("INSERT INTO off_response_profiles VALUES (?,?,?)", (
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
            return True

    async def save(self, result: CurveResult) -> tuple[bool, str]:
        """Commit cycle, raw buckets and aggregate together; duplicate IDs do nothing."""
        if result.curve_type not in CURVE_TYPES:
            raise ValueError("Invalid curve type")
        async with self.lock:
            accepted, reason = self.model.assess(result)
            standard = self.model.buckets[result.curve_type]
            updated = {}
            residual = None
            if accepted:
                candidate = deepcopy(self.model)
                candidate.apply(result)
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
            inserted = await asyncio.to_thread(self._save_sync, result, accepted, reason, updated, residual, memory_payload)
            if inserted:
                if accepted:
                    self.model = candidate
                else:
                    self.model.rejected[result.curve_type] += 1
                self.model.last_reason = reason
                if accepted:
                    _LOGGER.info("Curve segment learned: %s", result.curve_type)
                else:
                    _LOGGER.warning("Curve segment excluded: %s (%s)", result.curve_type, reason)
            return accepted and inserted, reason

    def _cleanup_sync(self, cutoff: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                DELETE FROM cycle_buckets WHERE (cycle_id, curve_type) IN
                (SELECT id, curve_type FROM cycles WHERE ended_at < ?)
            """, (cutoff,))

    async def cleanup(self) -> None:
        """Keep aggregate and cycle metadata while removing seven-day raw buckets."""
        cutoff = (datetime.now(UTC) - timedelta(days=RAW_RETENTION_DAYS)).isoformat()
        async with self.lock:
            await asyncio.to_thread(self._cleanup_sync, cutoff)

    async def remove(self) -> None:
        """Delete this entry's private database only after config entry removal."""
        async with self.lock:
            for suffix in ("", "-wal", "-shm"):
                await asyncio.to_thread(Path(str(self.path) + suffix).unlink, missing_ok=True)
