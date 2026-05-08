"""Alias-prefix resolution for PANTHEON v0.1."""

from __future__ import annotations

from typing import Any

from mnemos.domain.pantheon.catalog import find_model


class PantheonRoutingError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _cost_sort_value(model: dict[str, Any]) -> tuple[bool, float, str]:
    cost = model.get("cost_per_mtok")
    return (cost is None, float(cost) if cost is not None else float("inf"), model["id"])


def _latency_sort_value(model: dict[str, Any]) -> tuple[bool, float, str]:
    latency = model.get("p50_latency_ms")
    return (latency is None, float(latency) if latency is not None else float("inf"), model["id"])


def _available(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        model for model in models
        if model.get("available", True) and not model.get("deprecated", False)
    ]


def _quality_ok(model: dict[str, Any], quality_floor: float) -> bool:
    return float(model.get("quality_score") or 0.0) >= quality_floor


def _cost_ok(model: dict[str, Any], max_cost: float | None) -> bool:
    cost = model.get("cost_per_mtok")
    return max_cost is None or (cost is not None and float(cost) <= max_cost)


# #183: removed `_select_cheapest` helper — defined but never
# called. Selection is done by the route handler with a different
# scoring shape that doesn't go through this helper.


def _candidate_pool(
    models: list[dict[str, Any]],
    *,
    required_capability: str | None = None,
    quality_floor: float = 0.0,
    max_cost: float | None = None,
) -> list[dict[str, Any]]:
    candidates = _available(models)
    if required_capability:
        candidates = [
            model for model in candidates
            if required_capability in set(model.get("capabilities") or [])
        ]
    return [
        model for model in candidates
        if _quality_ok(model, quality_floor) and _cost_ok(model, max_cost)
    ]


def resolve_alias(
    model_or_alias: str,
    models: list[dict[str, Any]],
    *,
    quality_floor: float,
    max_cost: float | None,
) -> dict[str, Any]:
    """Resolve a model string into a catalog model or virtual consensus route."""
    requested = (model_or_alias or "").strip()
    if not requested:
        raise PantheonRoutingError(400, "model is required")

    if requested.startswith("consensus:"):
        task_type = requested.split(":", 1)[1].strip() or "reasoning"
        return {
            "alias": requested,
            "type": "consensus",
            "task_type": task_type,
            "provider": "graeae",
            "resolved_model": None,
            "model": None,
            "reason": f"routes through GRAEAE consultation for task_type={task_type}",
        }

    if requested == "auto:reasoning":
        candidates = _candidate_pool(
            models,
            required_capability="reasoning",
            quality_floor=quality_floor,
            max_cost=max_cost,
        )
        model = sorted(candidates, key=_cost_sort_value)[0] if candidates else None
        if model is None:
            raise PantheonRoutingError(404, "no reasoning-capable model meets the routing floor")
        return {
            "alias": requested,
            "type": "auto",
            "required_capability": "reasoning",
            "candidates": candidates,
            "provider": model["provider"],
            "resolved_model": model["id"],
            "model": model,
            "reason": (
                "cheapest non-deprecated reasoning model "
                f"with quality_score >= {quality_floor}"
            ),
        }

    if requested == "auto:cheap":
        candidates = _candidate_pool(models, quality_floor=0.0, max_cost=max_cost)
        model = sorted(candidates, key=_cost_sort_value)[0] if candidates else None
        if model is None:
            raise PantheonRoutingError(404, "no available model meets the cost ceiling")
        return {
            "alias": requested,
            "type": "auto",
            "candidates": candidates,
            "provider": model["provider"],
            "resolved_model": model["id"],
            "model": model,
            "reason": "cheapest available non-deprecated model",
        }

    if requested == "auto:fast":
        candidates = _candidate_pool(models, quality_floor=0.0, max_cost=max_cost)
        if not candidates:
            raise PantheonRoutingError(404, "no available model meets the cost ceiling")
        model = sorted(candidates, key=_latency_sort_value)[0]
        return {
            "alias": requested,
            "type": "auto",
            "candidates": candidates,
            "provider": model["provider"],
            "resolved_model": model["id"],
            "model": model,
            "reason": "lowest p50_latency_ms among available non-deprecated models",
        }

    if requested.startswith("auto:"):
        raise PantheonRoutingError(400, f"unknown PANTHEON alias {requested!r}")

    model = find_model(models, requested)
    if model is None:
        raise PantheonRoutingError(404, f"model {requested!r} not found in PANTHEON catalog")
    return {
        "alias": requested,
        "type": "literal",
        "candidates": [model],
        "provider": model["provider"],
        "resolved_model": model["id"],
        "model": model,
        "reason": "literal model name; no alias policy applied",
    }
