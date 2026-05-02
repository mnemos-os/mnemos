"""Small NumPy helpers for KRONOS v0.1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np


def z_score(value: float, mean: float, std: float) -> float:
    """Return a finite z-like score, preserving direction when variance is zero."""
    diff = float(value) - float(mean)
    std = float(std)
    if abs(std) <= 1e-12:
        return 0.0 if abs(diff) <= 1e-12 else diff
    return diff / std


def ewma(values: np.ndarray, alpha: float = 0.3) -> np.ndarray:
    """Exponentially weighted moving average, oldest to newest."""
    if not 0.0 < alpha <= 1.0:
        raise ValueError("alpha must be in the range (0, 1]")
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return arr.astype(float)
    result = np.empty_like(arr, dtype=float)
    result[0] = arr[0]
    for idx in range(1, arr.size):
        result[idx] = alpha * arr[idx] + (1.0 - alpha) * result[idx - 1]
    return result


def hourly_buckets(timestamps: list[datetime], window_hours: int) -> np.ndarray:
    """Count timestamps into hour buckets, oldest first.

    The window ends at the latest timestamp's hour. This keeps the helper pure
    while making fixed-timestamp tests deterministic.
    """
    if window_hours <= 0:
        raise ValueError("window_hours must be positive")
    buckets = np.zeros(window_hours, dtype=float)
    if not timestamps:
        return buckets

    normalized = [_as_utc(ts) for ts in timestamps]
    end_hour = _floor_hour(max(normalized))
    start_hour = end_hour - timedelta(hours=window_hours - 1)
    for ts in normalized:
        bucket_hour = _floor_hour(ts)
        idx = int((bucket_hour - start_hour).total_seconds() // 3600)
        if 0 <= idx < window_hours:
            buckets[idx] += 1.0
    return buckets


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)
