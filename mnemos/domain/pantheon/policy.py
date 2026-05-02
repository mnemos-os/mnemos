"""Rolling-window adaptive routing policy for PANTHEON v0.2."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mnemos.core.config import get_settings
from mnemos.core.numeric import safe_float

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedRoute:
    selected: dict[str, Any]
    candidates: list[str]
    rolling_window_minutes: int
    scores: dict[str, dict[str, Any]]
    selection_reason: str
    telemetry_available: bool


def _candidate_backend(candidate: dict[str, Any] | str) -> str:
    if isinstance(candidate, str):
        return candidate
    return str(candidate.get("id") or candidate.get("model_id") or "")


def _cost_sort_value(candidate: dict[str, Any] | str) -> tuple[bool, float, str]:
    if isinstance(candidate, str):
        return (True, float("inf"), candidate)
    cost = candidate.get("cost_per_mtok")
    return (cost is None, safe_float(cost) if cost is not None else float("inf"), _candidate_backend(candidate))


def _fallback_cheapest(candidates: list[dict[str, Any] | str]) -> dict[str, Any] | str:
    return sorted(candidates, key=_cost_sort_value)[0]


def _normalize_lower(value: float | None, values: list[float]) -> float:
    if value is None or not values:
        return 1.0
    low = min(values)
    high = max(values)
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def _weight_settings() -> tuple[float, float, float]:
    settings = get_settings().pantheon
    latency = float(settings.policy_latency_weight)
    error = float(settings.policy_error_weight)
    cost = float(settings.policy_cost_weight)
    total = latency + error + cost
    if total <= 0:
        return 0.40, 0.40, 0.20
    return latency / total, error / total, cost / total


async def _fetch_window(pool: Any, candidate_list: list[str], window_minutes: int) -> list[Any]:
    if pool is None or not candidate_list:
        return []
    async with pool.acquire() as conn:
        return list(
            await conn.fetch(
                """
                SELECT metadata->>'resolved_to' AS backend,
                       AVG((metadata->>'latency_ms')::FLOAT) AS avg_latency_ms,
                       SUM(CASE WHEN metadata->>'outcome' = 'error' THEN 1 ELSE 0 END)::FLOAT
                         / COUNT(*)::FLOAT AS error_rate,
                       AVG((metadata->>'cost_usd')::FLOAT) AS avg_cost
                FROM memories
                WHERE category='pantheon_routing'
                  AND created > NOW() - ($1::int * INTERVAL '1 minute')
                  AND metadata->>'resolved_to' = ANY($2::text[])
                GROUP BY backend
                """,
                int(window_minutes),
                candidate_list,
            )
        )


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _score_candidates(
    candidates: list[dict[str, Any] | str],
    rows: list[Any],
) -> dict[str, dict[str, Any]]:
    telemetry: dict[str, dict[str, float | None]] = {}
    for row in rows:
        backend = str(_row_get(row, "backend") or "")
        if not backend:
            continue
        telemetry[backend] = {
            "avg_latency_ms": _maybe_float(_row_get(row, "avg_latency_ms")),
            "error_rate": _maybe_float(_row_get(row, "error_rate")),
            "avg_cost": _maybe_float(_row_get(row, "avg_cost")),
        }

    latency_values = [
        float(item["avg_latency_ms"])
        for item in telemetry.values()
        if item.get("avg_latency_ms") is not None
    ]
    cost_values = [
        float(item["avg_cost"])
        for item in telemetry.values()
        if item.get("avg_cost") is not None
    ]
    latency_weight, error_weight, cost_weight = _weight_settings()
    scores: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        backend = _candidate_backend(candidate)
        metrics = telemetry.get(backend, {})
        avg_latency_ms = metrics.get("avg_latency_ms")
        error_rate = metrics.get("error_rate")
        avg_cost = metrics.get("avg_cost")
        latency_component = _normalize_lower(
            float(avg_latency_ms) if avg_latency_ms is not None else None,
            latency_values,
        )
        error_component = max(0.0, min(1.0, float(error_rate))) if error_rate is not None else 1.0
        cost_component = _normalize_lower(float(avg_cost) if avg_cost is not None else None, cost_values)
        weighted_score = (
            latency_weight * latency_component
            + error_weight * error_component
            + cost_weight * cost_component
        )
        scores[backend] = {
            "avg_latency_ms": avg_latency_ms,
            "error_rate": error_rate,
            "avg_cost": avg_cost,
            "weighted_score": weighted_score,
            "telemetry_counted": backend in telemetry,
        }
    return scores


async def resolve_with_policy(
    pool: Any,
    alias: str,
    candidates: list[dict[str, Any] | str],
    *,
    window_minutes: int = 15,
) -> ResolvedRoute:
    """Pick the best candidate for an ``auto:*`` alias using recent routing telemetry."""
    if not candidates:
        raise ValueError("candidates must not be empty")

    candidate_list = [_candidate_backend(candidate) for candidate in candidates]
    try:
        rows = await _fetch_window(pool, candidate_list, window_minutes)
    except Exception as exc:
        logger.debug("[PANTHEON] rolling-window policy unavailable for %s: %s", alias, exc)
        rows = []

    scores = _score_candidates(candidates, rows)
    telemetry_available = any(score.get("telemetry_counted") for score in scores.values())
    if not telemetry_available:
        selected = _fallback_cheapest(candidates)
        return ResolvedRoute(
            selected=dict(selected) if isinstance(selected, dict) else {"id": selected},
            candidates=candidate_list,
            rolling_window_minutes=window_minutes,
            scores=scores,
            selection_reason="fallback cheapest-first policy (no rolling telemetry)",
            telemetry_available=False,
        )

    selected_backend = min(
        scores,
        key=lambda backend: (
            float(scores[backend]["weighted_score"]),
            _cost_sort_value(next(candidate for candidate in candidates if _candidate_backend(candidate) == backend)),
        ),
    )
    selected = next(candidate for candidate in candidates if _candidate_backend(candidate) == selected_backend)
    return ResolvedRoute(
        selected=dict(selected) if isinstance(selected, dict) else {"id": selected},
        candidates=candidate_list,
        rolling_window_minutes=window_minutes,
        scores=scores,
        selection_reason="best weighted score (latency+error+cost)",
        telemetry_available=True,
    )


__all__ = ["ResolvedRoute", "resolve_with_policy"]
