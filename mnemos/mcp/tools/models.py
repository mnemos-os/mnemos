"""MCP model recommendation tool handler."""

from __future__ import annotations

import logging
from typing import Any

import mnemos.core.lifecycle as _lc
from mnemos.core.auth_context import UserContext
from mnemos.db import mcp_repo

from ._runtime import _rest_get, _tool

logger = logging.getLogger(__name__)


async def tool_recommend_model(
    task_type: str,
    cost_budget: float = 10.0,
    quality_floor: float = 0.85,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Query model optimizer for cost-aware recommendation."""
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
        return {
            "success": True,
            "task_type": task_type,
            **recommendation,
            "budget_met": cost is None or cost <= cost_budget,
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
            return {
                "success": True,
                "task_type": task_type,
                "recommended": model,
                "reasoning": (
                    f"Cheapest model with {', '.join(required_caps)} capability "
                    f"above quality floor {quality_floor}"
                ),
                "budget_met": avg_cost <= cost_budget,
            }

    except Exception as e:
        logger.error(f"[MCP] recommend_model failed: {e}")
        return {"success": False, "error": str(e)}


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
}
