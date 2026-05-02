"""Recall-pattern anomaly detection for KRONOS v0.1."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import numpy as np

from mnemos.domain.kronos.scoring import z_score

AnomalyType = Literal["spike", "drop"]
SuggestedAction = Literal["trending", "eligible_for_persephone", "investigate"]


@dataclass(frozen=True)
class RecallAnomaly:
    memory_id: str
    namespace: str
    anomaly_type: AnomalyType
    z_score: float
    observed_count: int
    expected_count: float
    suggested_action: SuggestedAction


@dataclass(frozen=True)
class NamespaceDrift:
    namespace: str
    recent_days: int
    baseline_days: int
    recent_total_recalls: int
    baseline_total_recalls: int
    total_recall_delta: float
    recent_unique_memory_ratio: float
    baseline_unique_memory_ratio: float
    unique_memory_ratio_delta: float
    recent_average_recall_count: float
    baseline_average_recall_count: float
    average_recall_count_delta: float


_DAILY_RECALL_LOG_SQL = """
SELECT memory_id,
       date_trunc('day', recalled_at AT TIME ZONE 'UTC') AS bucket_day,
       COUNT(*)::int AS recall_count
  FROM memory_recall_log
 WHERE namespace = $1
   AND recalled_at >= NOW() - ($2::int * INTERVAL '1 hour')
 GROUP BY memory_id, bucket_day
 ORDER BY memory_id, bucket_day
"""

_DAILY_MEMORIES_FALLBACK_SQL = """
SELECT id AS memory_id,
       date_trunc('day', last_recalled_at AT TIME ZONE 'UTC') AS bucket_day,
       recall_count::int AS recall_count
  FROM memories
 WHERE namespace = $1
   AND last_recalled_at IS NOT NULL
   AND last_recalled_at >= NOW() - ($2::int * INTERVAL '1 hour')
   AND deleted_at IS NULL
 ORDER BY id
"""

_RECALL_LOG_EVENTS_SQL = """
SELECT memory_id, recalled_at
  FROM memory_recall_log
 WHERE namespace = $1
   AND recalled_at >= NOW() - (($2::int + 7) * INTERVAL '1 day')
 ORDER BY recalled_at
"""

_MEMORY_RECALL_ROWS_SQL = """
SELECT id, recall_count, last_recalled_at
  FROM memories
 WHERE namespace = $1
   AND deleted_at IS NULL
"""


async def detect_recall_anomalies(
    pool: Any,
    namespace: str,
    lookback_hours: int = 168,
    sensitivity: float = 2.5,
) -> list[RecallAnomaly]:
    """Detect per-memory recall spikes and drops over daily buckets."""
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    if sensitivity <= 0:
        raise ValueError("sensitivity must be positive")

    rows = await _fetch_daily_recall_counts(pool, namespace, lookback_hours)
    current_day = datetime.now(timezone.utc).date()
    baseline_days = max(1, int(np.ceil(lookback_hours / 24.0)))
    baseline_range = [
        current_day - timedelta(days=offset)
        for offset in range(baseline_days, 0, -1)
    ]

    by_memory: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        memory_id = str(_row_get(row, "memory_id", ""))
        bucket = _coerce_date(_row_get(row, "bucket_day"))
        count = int(_row_get(row, "recall_count", 0) or 0)
        if memory_id and bucket is not None:
            by_memory[memory_id][bucket] += count

    anomalies: list[RecallAnomaly] = []
    for memory_id, buckets in by_memory.items():
        baseline_counts = np.asarray(
            [buckets.get(day, 0) for day in baseline_range],
            dtype=float,
        )
        if baseline_counts.size == 0:
            continue
        mean = float(np.mean(baseline_counts))
        stddev = float(np.std(baseline_counts))
        observed = int(buckets.get(current_day, 0))
        upper = mean + sensitivity * stddev
        lower = mean - sensitivity * stddev

        score = z_score(observed, mean, stddev)
        if observed > upper:
            anomalies.append(
                RecallAnomaly(
                    memory_id=memory_id,
                    namespace=namespace,
                    anomaly_type="spike",
                    z_score=score,
                    observed_count=observed,
                    expected_count=mean,
                    suggested_action="trending",
                )
            )
        elif lower > 0 and observed < lower:
            anomalies.append(
                RecallAnomaly(
                    memory_id=memory_id,
                    namespace=namespace,
                    anomaly_type="drop",
                    z_score=score,
                    observed_count=observed,
                    expected_count=mean,
                    suggested_action="eligible_for_persephone",
                )
            )

    return sorted(anomalies, key=lambda item: abs(item.z_score), reverse=True)


async def detect_namespace_drift(
    pool: Any,
    namespace: str,
    baseline_days: int = 30,
) -> NamespaceDrift:
    """Compare the last 7 days with the prior baseline window."""
    if baseline_days <= 0:
        raise ValueError("baseline_days must be positive")

    now = datetime.now(timezone.utc)
    recent_start = now - timedelta(days=7)
    baseline_start = recent_start - timedelta(days=baseline_days)
    events = await _fetch_recall_events(pool, namespace, baseline_days)
    memory_rows = await _fetch_memory_recall_rows(pool, namespace)

    recent_total = 0
    baseline_total = 0
    recent_unique: set[str] = set()
    baseline_unique: set[str] = set()
    for row in events:
        memory_id = str(_row_get(row, "memory_id", ""))
        recalled_at = _as_utc(_row_get(row, "recalled_at"))
        if not memory_id or recalled_at is None:
            continue
        if recalled_at >= recent_start:
            recent_total += 1
            recent_unique.add(memory_id)
        elif baseline_start <= recalled_at < recent_start:
            baseline_total += 1
            baseline_unique.add(memory_id)

    recent_counts: list[float] = []
    baseline_counts: list[float] = []
    for row in memory_rows:
        recalled_at = _as_utc(_row_get(row, "last_recalled_at"))
        recall_count = float(_row_get(row, "recall_count", 0) or 0)
        if recalled_at is None:
            continue
        if recalled_at >= recent_start:
            recent_counts.append(recall_count)
        elif baseline_start <= recalled_at < recent_start:
            baseline_counts.append(recall_count)

    recent_unique_ratio = _ratio(len(recent_unique), recent_total)
    baseline_unique_ratio = _ratio(len(baseline_unique), baseline_total)
    recent_avg = float(np.mean(recent_counts)) if recent_counts else 0.0
    baseline_avg = float(np.mean(baseline_counts)) if baseline_counts else 0.0

    return NamespaceDrift(
        namespace=namespace,
        recent_days=7,
        baseline_days=baseline_days,
        recent_total_recalls=recent_total,
        baseline_total_recalls=baseline_total,
        total_recall_delta=_relative_delta(recent_total, baseline_total),
        recent_unique_memory_ratio=recent_unique_ratio,
        baseline_unique_memory_ratio=baseline_unique_ratio,
        unique_memory_ratio_delta=recent_unique_ratio - baseline_unique_ratio,
        recent_average_recall_count=recent_avg,
        baseline_average_recall_count=baseline_avg,
        average_recall_count_delta=recent_avg - baseline_avg,
    )


async def _fetch_daily_recall_counts(pool: Any, namespace: str, lookback_hours: int) -> list[Any]:
    async with pool.acquire() as conn:
        try:
            return list(await conn.fetch(_DAILY_RECALL_LOG_SQL, namespace, lookback_hours))
        except Exception:
            return list(await conn.fetch(_DAILY_MEMORIES_FALLBACK_SQL, namespace, lookback_hours))


async def _fetch_recall_events(pool: Any, namespace: str, baseline_days: int) -> list[Any]:
    async with pool.acquire() as conn:
        try:
            return list(await conn.fetch(_RECALL_LOG_EVENTS_SQL, namespace, baseline_days))
        except Exception:
            return []


async def _fetch_memory_recall_rows(pool: Any, namespace: str) -> list[Any]:
    async with pool.acquire() as conn:
        return list(await conn.fetch(_MEMORY_RECALL_ROWS_SQL, namespace))


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value).date()
    if isinstance(value, date):
        return value
    return None


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)


def _relative_delta(recent: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0 if recent == 0 else 1.0
    return (float(recent) - float(baseline)) / float(baseline)
