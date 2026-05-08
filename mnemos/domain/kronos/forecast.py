"""Recall-load forecasting for KRONOS v0.1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from mnemos.domain.kronos.backends.selector import get_backend


@dataclass(frozen=True)
class ForecastResult:
    namespace: str
    forecast_window_hours: int
    predicted_total_recalls: float
    predicted_p95_per_hour: float
    ci_lower: float
    ci_upper: float


_HOURLY_RECALL_LOG_SQL = """
SELECT date_trunc('hour', recalled_at AT TIME ZONE 'UTC') AS bucket_hour,
       COUNT(*)::int AS recall_count
  FROM memory_recall_log
 WHERE namespace = $1
   AND recalled_at >= NOW() - INTERVAL '7 days'
 GROUP BY bucket_hour
 ORDER BY bucket_hour
"""

_HOURLY_MEMORIES_FALLBACK_SQL = """
SELECT date_trunc('hour', last_recalled_at AT TIME ZONE 'UTC') AS bucket_hour,
       SUM(recall_count)::int AS recall_count
  FROM memories
 WHERE namespace = $1
   AND last_recalled_at IS NOT NULL
   AND last_recalled_at >= NOW() - INTERVAL '7 days'
   AND deleted_at IS NULL
 GROUP BY bucket_hour
 ORDER BY bucket_hour
"""

_PERSEPHONE_ELIGIBILITY_SQL = """
SELECT id, last_recalled_at
  FROM memories
 WHERE namespace = $1
   AND deleted_at IS NULL
   AND archived_at IS NULL
   AND consolidated_into IS NULL
   AND last_recalled_at IS NOT NULL
"""


async def forecast_recall_load(
    pool: Any,
    namespace: str,
    hours_ahead: int = 24,
) -> ForecastResult:
    """Forecast aggregate recall load with EWMA over hourly recall buckets."""
    if hours_ahead <= 0:
        raise ValueError("hours_ahead must be positive")

    rows = await _fetch_hourly_recall_counts(pool, namespace)
    history = _hourly_counts_from_rows(rows, window_hours=24 * 7)
    if history.size == 0 or float(np.sum(history)) == 0.0:
        return ForecastResult(
            namespace=namespace,
            forecast_window_hours=hours_ahead,
            predicted_total_recalls=0.0,
            predicted_p95_per_hour=0.0,
            ci_lower=0.0,
            ci_upper=0.0,
        )

    smoothed = get_backend().ewma(history, alpha=0.3)
    per_hour = max(0.0, float(smoothed[-1]))
    residuals = history - smoothed
    residual_std = float(np.std(residuals))
    if residual_std <= 1e-9:
        residual_std = float(np.sqrt(max(per_hour, 1.0)))

    predicted_total = per_hour * float(hours_ahead)
    ci_half_width = 1.96 * residual_std * float(np.sqrt(hours_ahead))
    return ForecastResult(
        namespace=namespace,
        forecast_window_hours=hours_ahead,
        predicted_total_recalls=predicted_total,
        predicted_p95_per_hour=max(0.0, per_hour + 1.645 * residual_std),
        ci_lower=max(0.0, predicted_total - ci_half_width),
        ci_upper=predicted_total + ci_half_width,
    )


async def forecast_persephone_eligibility(
    pool: Any,
    namespace: str,
    archive_after_days: int = 180,
    days_ahead: int = 30,
) -> int:
    """Count memories projected to become PERSEPHONE-eligible soon."""
    if archive_after_days <= 0:
        raise ValueError("archive_after_days must be positive")
    if days_ahead <= 0:
        raise ValueError("days_ahead must be positive")

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=days_ahead)
    rows = await _fetch_persephone_rows(pool, namespace)
    count = 0
    for row in rows:
        last_recalled_at = _as_utc(_row_get(row, "last_recalled_at"))
        if last_recalled_at is None:
            continue
        eligible_at = last_recalled_at + timedelta(days=archive_after_days)
        if now < eligible_at <= window_end:
            count += 1
    return count


async def _fetch_hourly_recall_counts(pool: Any, namespace: str) -> list[Any]:
    async with pool.acquire() as conn:
        try:
            return list(await conn.fetch(_HOURLY_RECALL_LOG_SQL, namespace))
        except Exception:
            return list(await conn.fetch(_HOURLY_MEMORIES_FALLBACK_SQL, namespace))


async def _fetch_persephone_rows(pool: Any, namespace: str) -> list[Any]:
    async with pool.acquire() as conn:
        return list(await conn.fetch(_PERSEPHONE_ELIGIBILITY_SQL, namespace))


def _hourly_counts_from_rows(rows: list[Any], window_hours: int) -> np.ndarray:
    buckets = np.zeros(window_hours, dtype=float)
    now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_hour = now_hour - timedelta(hours=window_hours - 1)
    for row in rows:
        bucket_hour = _as_utc(_row_get(row, "bucket_hour"))
        if bucket_hour is None:
            continue
        bucket_hour = bucket_hour.replace(minute=0, second=0, microsecond=0)
        idx = int((bucket_hour - start_hour).total_seconds() // 3600)
        if 0 <= idx < window_hours:
            buckets[idx] += float(_row_get(row, "recall_count", 0) or 0)
    return buckets


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
