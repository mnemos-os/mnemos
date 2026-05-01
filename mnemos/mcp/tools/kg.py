"""MCP knowledge graph tool handlers."""

from __future__ import annotations

from typing import Any

from mnemos.core.auth_context import UserContext

from ._runtime import (
    _rest_delete,
    _rest_get,
    _rest_post,
    _safe_path_segment,
    _safe_path_value,
    _tool,
)


async def tool_kg_create_triple(
    subject: str,
    predicate: str,
    object: str,
    subject_type: str | None = None,
    object_type: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    memory_id: str | None = None,
    confidence: float = 1.0,
    user: UserContext | None = None,
) -> dict[str, Any]:
    body = {
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "subject_type": subject_type,
        "object_type": object_type,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "memory_id": memory_id,
        "confidence": confidence,
    }
    return await _rest_post("/v1/kg/triples", {k: v for k, v in body.items() if v is not None})


async def tool_kg_search(
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    subject_type: str | None = None,
    object_type: str | None = None,
    limit: int = 50,
    user: UserContext | None = None,
) -> dict[str, Any]:
    params = {
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "subject_type": subject_type,
        "object_type": object_type,
        "limit": limit,
    }
    return await _rest_get("/v1/kg/triples", params={k: v for k, v in params.items() if v is not None})


async def tool_kg_timeline(
    subject: str,
    limit: int = 100,
    user: UserContext | None = None,
) -> dict[str, Any]:
    safe_subject = _safe_path_value(subject, label="subject")
    return await _rest_get(
        f"/v1/kg/timeline/{safe_subject}", params={"limit": limit},
    )


async def tool_update_triple(
    triple_id: str,
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    subject_type: str | None = None,
    object_type: str | None = None,
    valid_until: str | None = None,
    confidence: float | None = None,
    user: UserContext | None = None,
) -> dict[str, Any]:
    body = {
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "subject_type": subject_type,
        "object_type": object_type,
        "valid_until": valid_until,
        "confidence": confidence,
    }
    safe_triple_id = _safe_path_segment(triple_id, label="triple_id")
    return await _rest_post(
        f"/v1/kg/triples/{safe_triple_id}",
        {k: v for k, v in body.items() if v is not None},
        method="PATCH",
    )


async def tool_delete_triple(
    triple_id: str,
    user: UserContext | None = None,
) -> dict[str, Any]:
    safe_triple_id = _safe_path_segment(triple_id, label="triple_id")
    status = await _rest_delete(f"/v1/kg/triples/{safe_triple_id}")
    return {"deleted": True, "status": status}


TOOLS: dict[str, dict[str, Any]] = {
    "kg_create_triple": _tool(
        "Add a knowledge graph triple (subject -> predicate -> object).",
        {
            "subject": {"type": "string"},
            "predicate": {"type": "string"},
            "object": {"type": "string"},
            "subject_type": {"type": "string"},
            "object_type": {"type": "string"},
            "valid_from": {"type": "string", "description": "ISO8601 datetime"},
            "valid_until": {"type": "string", "description": "ISO8601 datetime"},
            "memory_id": {"type": "string", "description": "Link to source memory"},
            "confidence": {"type": "number", "default": 1.0, "minimum": 0.0, "maximum": 1.0},
        },
        ["subject", "predicate", "object"],
        tool_kg_create_triple,
    ),
    "kg_search": _tool(
        "Search knowledge graph triples.",
        {
            "subject": {"type": "string"},
            "predicate": {"type": "string"},
            "object": {"type": "string"},
            "subject_type": {"type": "string"},
            "object_type": {"type": "string"},
            "limit": {"type": "integer", "default": 50},
        },
        [],
        tool_kg_search,
    ),
    "kg_timeline": _tool(
        "Get the chronological history of an entity.",
        {"subject": {"type": "string"}, "limit": {"type": "integer", "default": 100}},
        ["subject"],
        tool_kg_timeline,
    ),
    "update_triple": _tool(
        "Partially update a KG triple by ID. Supply only the fields to change.",
        {
            "triple_id": {"type": "string"},
            "subject": {"type": "string"},
            "predicate": {"type": "string"},
            "object": {"type": "string"},
            "subject_type": {"type": "string"},
            "object_type": {"type": "string"},
            "valid_until": {"type": "string", "description": "ISO8601"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        ["triple_id"],
        tool_update_triple,
    ),
    "delete_triple": _tool(
        "Delete a KG triple by ID.",
        {"triple_id": {"type": "string"}},
        ["triple_id"],
        tool_delete_triple,
    ),
}
