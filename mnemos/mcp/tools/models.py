"""MCP model recommendation tool handler."""

from __future__ import annotations

import logging
import re
from typing import Any

import mnemos.core.lifecycle as _lc
from mnemos.core.auth_context import UserContext
from mnemos.db import mcp_repo

from ._runtime import _rest_get, _safe_path_segment, _tool

logger = logging.getLogger(__name__)

_CAPABILITY_RE = re.compile(r"\A[A-Za-z0-9_:-]{1,64}\Z")
_MODEL_ALIAS_RE = re.compile(r"\A[^\x00-\x1f\x7f]{1,256}\Z")


def _validate_capabilities(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("filter_capabilities must be a list with at most 32 items")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _CAPABILITY_RE.match(item):
            raise ValueError("filter_capabilities contains an invalid capability")
        out.append(item)
    return out


def _validate_optional_tier(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _CAPABILITY_RE.match(value):
        raise ValueError("filter_tier must be a safe tier string")
    return value


def _validate_max_cost(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) < 0:
        raise ValueError("max_cost must be a non-negative number")
    return float(value)


def _validate_model_or_alias(value: str) -> str:
    if not isinstance(value, str) or not _MODEL_ALIAS_RE.match(value) or not value.strip():
        raise ValueError("model_or_alias must be a non-empty string")
    return value.strip()


async def tool_recommend_model(
    task_type: str,
    cost_budget: float = 10.0,
    quality_floor: float = 0.85,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Query model optimizer for cost-aware recommendation."""
    _safe_path_segment(task_type, label="task_type")
    if user is None and not _lc._pool:
        recommendation = await _rest_get(
            "/v1/providers/recommend",
            params={
                "task_type": task_type,
                "cost_budget": cost_budget,
                "quality_floor": quality_floor,
            },
        )
        recommended = recommendation.get("recommended") or {}
        cost = recommended.get("cost_per_mtok")
        # ``cost is None`` means the recommendation came from the
        # degraded fallback (no priced model met the budget). An
        # unknown cost CANNOT satisfy a budget — surface budget_met
        # as False (not True) so callers do not silently treat
        # "unknown" as "free". The recommendation itself is still
        # returned so the caller can decide.
        if cost is None:
            budget_met = False
        else:
            budget_met = cost <= cost_budget
        return {
            "success": True,
            "task_type": task_type,
            **recommendation,
            "budget_met": budget_met,
        }

    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        async with pool.acquire() as conn:
            model, required_caps = await mcp_repo.fetch_recommended_model(
                conn,
                task_type,
                cost_budget,
                quality_floor,
            )

            if not model:
                return {"success": False, "error": "No models available"}

            avg_cost = model["cost_per_mtok"]
            # avg_cost is None when fetch_recommended_model returns
            # a degraded-fallback row whose cost columns were NULL.
            # Comparing None <= cost_budget would TypeError; surface
            # budget_met=False so callers treat unknown cost as
            # NOT meeting the budget rather than crashing.
            if avg_cost is None:
                budget_met = False
            else:
                budget_met = avg_cost <= cost_budget
            return {
                "success": True,
                "task_type": task_type,
                "recommended": model,
                "reasoning": (
                    f"Cheapest model with {', '.join(required_caps)} capability "
                    f"above quality floor {quality_floor}"
                ),
                "budget_met": budget_met,
            }

    except Exception as e:
        logger.error(f"[MCP] recommend_model failed: {e}")
        return {"success": False, "error": str(e)}


async def tool_pantheon_list_models(
    filter_capabilities: list[str] | None = None,
    filter_tier: str | None = None,
    max_cost: float | None = None,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Return the extended PANTHEON model catalog."""
    del user
    from mnemos.domain.pantheon.catalog import models_response

    return {
        "success": True,
        **await models_response(
            filter_capabilities=_validate_capabilities(filter_capabilities),
            filter_tier=_validate_optional_tier(filter_tier),
            max_cost=_validate_max_cost(max_cost),
        ),
    }


async def tool_pantheon_route_explain(
    messages: list[dict[str, Any]],
    model_or_alias: str,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Explain PANTHEON alias and routing resolution for an MCP caller."""
    del user
    from mnemos.domain.pantheon.router import explain_route

    if not isinstance(messages, list) or len(messages) > 100:
        raise ValueError("messages must be a list with at most 100 items")
    return {
        "success": True,
        **await explain_route({
            "messages": messages,
            "model_or_alias": _validate_model_or_alias(model_or_alias),
        }),
    }


TOOLS: dict[str, dict[str, Any]] = {
    "recommend_model": _tool(
        "Query model optimizer for cost-aware recommendation.",
        {
            "task_type": {
                "type": "string",
                "description": "Task type (code_generation, reasoning, architecture_design, etc.)",
            },
            "cost_budget": {"type": "number", "description": "Max $/MTok (default: 10.0)"},
            "quality_floor": {"type": "number", "description": "Min quality score (default: 0.85)"},
        },
        ["task_type"],
        tool_recommend_model,
    ),
    "pantheon_list_models": _tool(
        "List PANTHEON models with extended capability, tier, cost, and health metadata.",
        {
            "filter_capabilities": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 32,
                "description": "Optional required capabilities such as code or reasoning.",
            },
            "filter_tier": {
                "type": "string",
                "description": "Optional usage tier filter such as budget, premium, or frontier.",
            },
            "max_cost": {
                "type": "number",
                "description": "Optional maximum USD per MTok.",
            },
        },
        [],
        tool_pantheon_list_models,
    ),
    "pantheon_route_explain": _tool(
        "Explain how PANTHEON resolves a model name or alias for supplied messages.",
        {
            "messages": {
                "type": "array",
                "items": {"type": "object"},
                "maxItems": 100,
                "description": "OpenAI-style chat messages.",
            },
            "model_or_alias": {
                "type": "string",
                "description": "Literal model name or PANTHEON alias such as auto:reasoning.",
            },
        },
        ["messages", "model_or_alias"],
        tool_pantheon_route_explain,
    ),
}
