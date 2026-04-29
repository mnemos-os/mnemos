"""Shared persistence typing primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]
Row: TypeAlias = Any


@dataclass(frozen=True, slots=True)
class ModelRecommendation:
    """Backend-neutral shape for model routing recommendations."""

    provider: str
    model_id: str
    display_name: str | None
    cost_per_mtok: float
    quality_score: float
    context_window: int | None
