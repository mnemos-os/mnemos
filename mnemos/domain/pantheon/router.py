"""Simple PANTHEON v0.1 routing policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mnemos.core.config import get_settings
from mnemos.domain.pantheon import catalog
from mnemos.domain.pantheon.aliases import PantheonRoutingError, resolve_alias


@dataclass(frozen=True)
class RouteDecision:
    alias: str
    provider: str
    model_id: str | None
    route_type: str
    reason: str
    model: dict[str, Any] | None = None
    task_type: str | None = None

    def explain(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "resolved_model": self.model_id,
            "provider": self.provider,
            "route_type": self.route_type,
            "reason": self.reason,
            "task_type": self.task_type,
            "resolution_chain": [
                {"step": "input", "value": self.alias},
                {
                    "step": "alias_resolution",
                    "type": self.route_type,
                    "resolved_model": self.model_id,
                    "provider": self.provider,
                },
                {"step": "policy", "reason": self.reason},
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
    return RouteDecision(
        alias=resolved["alias"],
        provider=resolved["provider"],
        model_id=resolved["resolved_model"],
        route_type=resolved["type"],
        reason=resolved["reason"],
        model=resolved["model"],
        task_type=resolved.get("task_type"),
    )


async def explain_route(body: dict[str, Any]) -> dict[str, Any]:
    model = str(body.get("model") or body.get("model_or_alias") or "auto:cheap")
    decision = await route_model(model, body)
    return decision.explain()


__all__ = ["PantheonRoutingError", "RouteDecision", "explain_route", "route_model"]
