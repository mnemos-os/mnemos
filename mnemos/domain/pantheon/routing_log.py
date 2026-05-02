"""Best-effort MNEMOS routing-log writes for PANTHEON."""

from __future__ import annotations

import json
import logging
from typing import Any

import mnemos.core.lifecycle as _lc
from mnemos.core.ids import new_memory_id
from mnemos.core.numeric import safe_float
from mnemos.domain.pantheon.router import RouteDecision

logger = logging.getLogger(__name__)


def _usage_value(response: dict[str, Any] | None, key: str) -> int | None:
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict) or usage.get(key) is None:
        return None
    try:
        return int(usage[key])
    except (TypeError, ValueError):
        return None


def _model_cost(decision: RouteDecision, key: str) -> float | None:
    model = decision.model or {}
    raw = model.get(key)
    if raw is None:
        raw = model.get("cost_per_mtok")
    if raw is None:
        return None
    return safe_float(raw)


def _response_cost_usd(decision: RouteDecision, response: dict[str, Any] | None) -> float | None:
    if isinstance(response, dict):
        for key in ("cost_usd", "cost"):
            raw = response.get(key)
            if raw is not None:
                return safe_float(raw)
    tokens_in = _usage_value(response, "prompt_tokens")
    tokens_out = _usage_value(response, "completion_tokens")
    if tokens_in is None and tokens_out is None:
        return None
    input_cost = _model_cost(decision, "input_cost_per_mtok")
    output_cost = _model_cost(decision, "output_cost_per_mtok")
    cost_usd = 0.0
    if tokens_in is not None and input_cost is not None:
        cost_usd += (tokens_in / 1_000_000.0) * input_cost
    if tokens_out is not None and output_cost is not None:
        cost_usd += (tokens_out / 1_000_000.0) * output_cost
    return cost_usd


def _usage_tier(decision: RouteDecision) -> str | None:
    model = decision.model or {}
    raw = model.get("usage_tier")
    return str(raw) if raw is not None else None


def routing_payload(
    *,
    request_id: str,
    tenant_user_id: str,
    session_id: str,
    decision: RouteDecision,
    outcome: str,
    latency_ms: float,
    response: dict[str, Any] | None = None,
    error_class: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "request_id": request_id,
        "tenant_user_id": tenant_user_id,
        "alias_or_model": decision.alias,
        "resolved_to": decision.model_id or decision.alias,
        "outcome": outcome,
        "latency_ms": latency_ms,
        "tokens_in": _usage_value(response, "prompt_tokens"),
        "tokens_out": _usage_value(response, "completion_tokens"),
        "cost_usd": _response_cost_usd(decision, response),
        "error_class": error_class,
    }
    metadata = {
        "pantheon_version": "0.2",
        "session_id": session_id,
        "usage_tier": _usage_tier(decision),
        **payload,
    }
    return payload, metadata


async def write_routing_memory(payload: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Write one routing decision as a memory, swallowing all failures."""
    try:
        content = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        metadata_json = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        backend = _lc._persistence_backend
        memory_id = new_memory_id()
        if backend is not None:
            async with backend.transactional() as tx:
                await backend.memories.insert_memory(
                    tx,
                    memory_id=memory_id,
                    content=content,
                    category="pantheon_routing",
                    subcategory=None,
                    metadata_json=metadata_json,
                    quality_rating=75,
                    owner_id="system:pantheon",
                    namespace="pantheon",
                    permission_mode=600,
                    source_model=str(payload.get("resolved_to") or "") or None,
                    source_provider=None,
                    source_session=str(metadata.get("session_id") or "") or None,
                    source_agent="pantheon",
                    verbatim_content=content,
                    created=None,
                    updated=None,
                )
            return

        pool = _lc._pool
        if pool is None:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memories
                (id, content, category, subcategory, metadata, quality_rating, verbatim_content,
                 owner_id, namespace, permission_mode, source_model, source_provider,
                 source_session, source_agent)
                VALUES ($1, $2, $3, $4, $5::jsonb, 75, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                memory_id,
                content,
                "pantheon_routing",
                None,
                metadata_json,
                content,
                "system:pantheon",
                "pantheon",
                600,
                str(payload.get("resolved_to") or "") or None,
                None,
                str(metadata.get("session_id") or "") or None,
                "pantheon",
            )
    except Exception as exc:
        logger.debug("[PANTHEON] routing-log write failed: %s", exc)


def schedule_routing_memory(payload: dict[str, Any], metadata: dict[str, Any]) -> None:
    try:
        _lc._schedule_background(write_routing_memory(payload, metadata))
    except RuntimeError as exc:
        logger.debug("[PANTHEON] routing-log scheduling failed: %s", exc)


__all__ = ["routing_payload", "schedule_routing_memory", "write_routing_memory"]
