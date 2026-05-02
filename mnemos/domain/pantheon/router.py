"""Simple PANTHEON v0.1 routing policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import mnemos.core.lifecycle as _lc
from mnemos.core.config import get_settings
from mnemos.domain.pantheon import catalog
from mnemos.domain.pantheon.aliases import PantheonRoutingError, resolve_alias
from mnemos.domain.pantheon.policy import resolve_with_policy


@dataclass(frozen=True)
class RouteDecision:
    alias: str
    provider: str
    model_id: str | None
    route_type: str
    reason: str
    model: dict[str, Any] | None = None
    task_type: str | None = None
    candidates: list[str] | None = None
    rolling_window_minutes: int | None = None
    scores: dict[str, dict[str, Any]] | None = None
    selection_reason: str | None = None

    def explain(self) -> dict[str, Any]:
        selection_reason = self.selection_reason or self.reason
        return {
            "alias": self.alias,
            "resolved_model": self.model_id,
            "provider": self.provider,
            "route_type": self.route_type,
            "reason": self.reason,
            "task_type": self.task_type,
            "candidates": self.candidates or ([] if self.route_type == "consensus" else [self.model_id]),
            "rolling_window_minutes": self.rolling_window_minutes,
            "scores": self.scores or {},
            "selected": self.model_id,
            "selection_reason": selection_reason,
            "resolution_chain": [
                {"step": "input", "value": self.alias},
                {
                    "step": "alias_resolution",
                    "type": self.route_type,
                    "resolved_model": self.model_id,
                    "provider": self.provider,
                },
                {"step": "policy", "reason": selection_reason},
            ],
            "model": self.model,
        }


def _body_quality_floor(body: dict[str, Any], default: float) -> float:
    pantheon = body.get("pantheon") if isinstance(body.get("pantheon"), dict) else {}
    raw = body.get("quality_floor", pantheon.get("quality_floor", default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _body_max_cost(body: dict[str, Any], default: float | None) -> float | None:
    pantheon = body.get("pantheon") if isinstance(body.get("pantheon"), dict) else {}
    raw = body.get("max_cost", pantheon.get("max_cost", default))
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


async def route_model(model_or_alias: str, body: dict[str, Any] | None = None) -> RouteDecision:
    settings = get_settings().pantheon
    request_body = body or {}
    quality_floor = _body_quality_floor(request_body, settings.default_quality_floor)
    max_cost = _body_max_cost(request_body, settings.default_max_cost_usd_per_mtok)
    models = await catalog.list_models()
    resolved = resolve_alias(
        model_or_alias,
        models,
        quality_floor=quality_floor,
        max_cost=max_cost,
    )
    candidates = [
        str(candidate.get("id"))
        for candidate in resolved.get("candidates", [])
        if isinstance(candidate, dict) and candidate.get("id")
    ]
    scores: dict[str, dict[str, Any]] | None = None
    selection_reason: str | None = None
    rolling_window_minutes: int | None = None
    if resolved["type"] == "auto":
        rolling_window_minutes = settings.routing_window_minutes
        policy_route = await resolve_with_policy(
            _lc._pool,
            resolved["alias"],
            list(resolved.get("candidates") or []),
            window_minutes=rolling_window_minutes,
        )
        selected = policy_route.selected
        resolved = {
            **resolved,
            "provider": selected["provider"],
            "resolved_model": selected["id"],
            "model": selected,
            "reason": policy_route.selection_reason,
        }
        candidates = policy_route.candidates
        scores = policy_route.scores
        selection_reason = policy_route.selection_reason
    return RouteDecision(
        alias=resolved["alias"],
        provider=resolved["provider"],
        model_id=resolved["resolved_model"],
        route_type=resolved["type"],
        reason=resolved["reason"],
        model=resolved["model"],
        task_type=resolved.get("task_type"),
        candidates=candidates,
        rolling_window_minutes=rolling_window_minutes,
        scores=scores,
        selection_reason=selection_reason,
    )


async def explain_route(body: dict[str, Any]) -> dict[str, Any]:
    model = str(body.get("model") or body.get("model_or_alias") or "auto:cheap")
    decision = await route_model(model, body)
    return decision.explain()


__all__ = ["PantheonRoutingError", "RouteDecision", "explain_route", "route_model"]
