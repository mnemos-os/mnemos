"""Timestamp parsing and formatting helpers for MPF."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def _iso(value) -> Optional[str]:
    """Render a DB timestamp value as an RFC 3339 / ISO 8601 string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Best-effort parse for MPF timestamp fields."""
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _parse_iso_naive(value: Optional[str]) -> Optional[datetime]:
    """Parse a timestamp and return a UTC-aware value for DB writes.

    The helper name is retained for import-call compatibility from the
    pre-v5.0.3 TIMESTAMP schema. Postgres lifecycle columns are now
    TIMESTAMPTZ and asyncpg expects aware datetime values.
    """
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
