"""MCP memory CRUD/search tool handlers."""

from __future__ import annotations

import os
from typing import Any

from mnemos.core.auth_context import UserContext

from ._runtime import _rest_delete, _rest_get, _rest_post, _tool


def _connector_namespace() -> str | None:
    """Per-connector default-namespace WRITE STAMP.

    Reads ``MNEMOS_DEFAULT_NAMESPACE`` and stamps it on
    create_memory / search_memories / list_memories /
    bulk_create_memories REST calls. The connector-gallery docs
    (claude-code.md, cursor.md, codex-cli.md, continue-dev.md,
    cline.md) explicitly retract any claim that this is an
    enforced isolation boundary — it's a default-namespace
    convenience for ergonomic per-connector write scoping.

    NOT scoped by this helper:
    * get_memory / update_memory / delete_memory — these take
      a memory_id directly and the REST seam doesn't currently
      accept a namespace constraint on the ID-based path.
    * branch_memory / log_memory / diff_memory_commits /
      checkout_memory — same, ID-based.

    A root API key with this env stamp set will WRITE into the
    configured namespace by default but can still READ /
    UPDATE / DELETE any memory by ID across all namespaces.
    For ENFORCED isolation, the docs point operators at distinct
    non-root **users** with ``users.namespace`` set; API keys
    issued for those users inherit the user's namespace via the
    server-side auth-resolution path.

    Empty / unset → no namespace override; server falls through
    to the API key's resolved namespace.
    """
    val = os.environ.get("MNEMOS_DEFAULT_NAMESPACE", "").strip()
    return val or None


async def tool_search_memories(
    query: str,
    limit: int = 10,
    category: str | None = None,
    subcategory: str | None = None,
    semantic: bool = False,
    user: UserContext | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"query": query, "limit": limit}
    if category:
        body["category"] = category
    if subcategory:
        body["subcategory"] = subcategory
    if semantic:
        body["semantic"] = True
    ns = _connector_namespace()
    if ns:
        body["namespace"] = ns
    return await _rest_post("/v1/memories/search", body)


async def tool_update_memory(
    memory_id: str,
    content: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    metadata: dict[str, Any] | None = None,
    permission_mode: int | None = None,
    user: UserContext | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for key, value in {
        "content": content,
        "category": category,
        "subcategory": subcategory,
        "metadata": metadata,
        "permission_mode": permission_mode,
    }.items():
        if value is not None:
            body[key] = value
    return await _rest_post(f"/v1/memories/{memory_id}", body, method="PATCH")


async def tool_get_memory(
    memory_id: str,
    user: UserContext | None = None,
) -> dict[str, Any]:
    return await _rest_get(f"/v1/memories/{memory_id}")


async def tool_create_memory(
    content: str,
    category: str = "facts",
    subcategory: str | None = None,
    metadata: dict[str, Any] | None = None,
    permission_mode: int | None = None,
    user: UserContext | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"content": content, "category": category}
    if subcategory:
        body["subcategory"] = subcategory
    if metadata:
        body["metadata"] = metadata
    if permission_mode is not None:
        body["permission_mode"] = permission_mode
    ns = _connector_namespace()
    if ns:
        body["namespace"] = ns
    return await _rest_post("/v1/memories", body)


async def tool_delete_memory(
    memory_id: str,
    user: UserContext | None = None,
) -> dict[str, Any]:
    status = await _rest_delete(f"/v1/memories/{memory_id}")
    return {"deleted": True, "status": status}


async def tool_list_memories(
    category: str | None = None,
    subcategory: str | None = None,
    limit: int = 20,
    offset: int = 0,
    user: UserContext | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in {
        "category": category,
        "subcategory": subcategory,
        "limit": limit,
        "offset": offset,
    }.items():
        if value is not None:
            params[key] = value
    ns = _connector_namespace()
    if ns:
        params["namespace"] = ns
    return await _rest_get("/v1/memories", params=params)


async def tool_get_stats(user: UserContext | None = None) -> dict[str, Any]:
    return await _rest_get("/stats")


async def tool_bulk_create_memories(
    memories: list[dict[str, Any]],
    user: UserContext | None = None,
) -> dict[str, Any]:
    ns = _connector_namespace()
    if ns:
        # Connector-scope env wins over per-row namespace. The
        # alternative (per-row wins) creates a footgun where a
        # bulk caller could bypass the connector's documented
        # write-stamp scope just by including ``"namespace": ...``
        # in each row. Codex round-3 audit: the env-stamp must be
        # the boundary if it's a boundary at all. Power users who
        # need cross-namespace bulk creation should hit the REST
        # API directly OR run without the env stamp.
        memories = [{**m, "namespace": ns} for m in memories]
    return await _rest_post("/v1/memories/bulk", {"memories": memories})


TOOLS: dict[str, dict[str, Any]] = {
    "search_memories": _tool(
        "Full-text search across MNEMOS memories. Returns ranked results. Filter by category and/or subcategory.",
        {
            "query": {"type": "string", "description": "Search query"},
            "limit": {"type": "integer", "default": 10},
            "category": {"type": "string", "description": "Optional category filter"},
            "subcategory": {"type": "string", "description": "Optional subcategory filter"},
            "semantic": {
                "type": "boolean",
                "default": False,
                "description": "True = pgvector cosine similarity; False = full-text search",
            },
        },
        ["query"],
        tool_search_memories,
    ),
    "update_memory": _tool(
        "Partially update an existing memory. Supply only the fields you want to change.",
        {
            "memory_id": {"type": "string"},
            "content": {"type": "string", "description": "New content (replaces existing)"},
            "category": {"type": "string", "description": "New category"},
            "subcategory": {"type": "string", "description": "New subcategory"},
            "metadata": {"type": "object", "description": "New metadata (replaces existing)"},
            "permission_mode": {"type": "integer", "description": "Unix-style octal permission digits, e.g. 600 or 644"},
        },
        ["memory_id"],
        tool_update_memory,
    ),
    "get_memory": _tool(
        "Retrieve a single memory by its ID (mem_xxxxxxxxxxxx).",
        {"memory_id": {"type": "string"}},
        ["memory_id"],
        tool_get_memory,
    ),
    "create_memory": _tool(
        "Store a new memory in MNEMOS.",
        {
            "content": {"type": "string"},
            "category": {"type": "string", "default": "facts"},
            "subcategory": {"type": "string"},
            "metadata": {"type": "object"},
            "permission_mode": {"type": "integer", "description": "Unix-style octal permission digits, e.g. 600 or 644"},
        },
        ["content"],
        tool_create_memory,
    ),
    "delete_memory": _tool(
        "Delete a memory by ID.",
        {"memory_id": {"type": "string"}},
        ["memory_id"],
        tool_delete_memory,
    ),
    "list_memories": _tool(
        "List memories with optional category/subcategory filter and pagination.",
        {
            "category": {"type": "string"},
            "subcategory": {"type": "string"},
            "limit": {"type": "integer", "default": 20},
            "offset": {"type": "integer", "default": 0},
        },
        [],
        tool_list_memories,
    ),
    "get_stats": _tool(
        "Get MNEMOS system stats: total memories, breakdown by category, compression.",
        {},
        [],
        tool_get_stats,
    ),
    "bulk_create_memories": _tool(
        "Create multiple memories in a single call.",
        {
            "memories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "category": {"type": "string", "default": "facts"},
                        "subcategory": {"type": "string"},
                        "metadata": {"type": "object"},
                        "verbatim_content": {"type": "string"},
                    },
                    "required": ["content"],
                },
            },
        },
        ["memories"],
        tool_bulk_create_memories,
    ),
}
