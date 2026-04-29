"""MCP memory CRUD/search tool handlers."""

from __future__ import annotations

from typing import Any

from mnemos.api.dependencies import UserContext

from ._runtime import _rest_delete, _rest_get, _rest_post, _tool


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
    return await _rest_post("/v1/memories/search", body)


async def tool_update_memory(
    memory_id: str,
    content: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    metadata: dict[str, Any] | None = None,
    user: UserContext | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for key, value in {
        "content": content,
        "category": category,
        "subcategory": subcategory,
        "metadata": metadata,
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
    user: UserContext | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"content": content, "category": category}
    if subcategory:
        body["subcategory"] = subcategory
    if metadata:
        body["metadata"] = metadata
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
    return await _rest_get("/v1/memories", params=params)


async def tool_get_stats(user: UserContext | None = None) -> dict[str, Any]:
    return await _rest_get("/stats")


async def tool_bulk_create_memories(
    memories: list[dict[str, Any]],
    user: UserContext | None = None,
) -> dict[str, Any]:
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
