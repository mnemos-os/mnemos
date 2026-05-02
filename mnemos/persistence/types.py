"""Shared persistence typing primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]
Row: TypeAlias = Any

MEMORY_COLS = (
    "id, content, category, subcategory, created, updated, "
    "metadata, quality_rating, compressed_content, verbatim_content, "
    "owner_id, group_id, namespace, permission_mode, "
    "source_model, source_provider, source_session, source_agent, "
    "archived_at"
)

# Backward-compatible persistence-layer alias for modules that still use the
# historical private constant spelling.
_MEMORY_COLS = MEMORY_COLS


@dataclass(frozen=True, slots=True)
class ModelRecommendation:
    """Backend-neutral shape for model routing recommendations."""

    provider: str
    model_id: str
    display_name: str | None
    cost_per_mtok: float
    quality_score: float
    context_window: int | None
