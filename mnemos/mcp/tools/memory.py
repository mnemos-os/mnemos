"""MCP memory CRUD/search tool handlers."""

from __future__ import annotations

from typing import Any

from mnemos.core.auth_context import UserContext
from mnemos.core.config import connector_default_namespace

from ._runtime import (
    MCP_BULK_CREATE_MAX_ITEMS,
    MCP_DEFAULT_LIMIT_MAX,
    MCP_OFFSET_MAX,
    _bounded_int,
    _bounded_list,
    _rest_delete,
    _rest_get,
    _rest_get_text,
    _rest_post,
    _safe_path_segment,
    _safe_path_value,
    _tool,
)


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
    val = connector_default_namespace()
    if val:
        _safe_path_value(val, label="namespace", max_length=128)
    return val


def _validate_optional_filter(value: str | None, *, label: str) -> str | None:
    if value:
        _safe_path_value(value, label=label, max_length=128)
    return value


async def tool_search_memories(
    query: str,
    limit: int = 10,
    category: str | None = None,
    subcategory: str | None = None,
    semantic: bool = False,
    user: UserContext | None = None,
) -> dict[str, Any]:
    limit = _bounded_int(
        limit, label="limit", minimum=1, maximum=MCP_DEFAULT_LIMIT_MAX,
    )
    body: dict[str, Any] = {"query": query, "limit": limit}
    category = _validate_optional_filter(category, label="category")
    subcategory = _validate_optional_filter(subcategory, label="subcategory")
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
    category = _validate_optional_filter(category, label="category")
    subcategory = _validate_optional_filter(subcategory, label="subcategory")
    for key, value in {
        "content": content,
        "category": category,
        "subcategory": subcategory,
        "metadata": metadata,
        "permission_mode": permission_mode,
    }.items():
        if value is not None:
            body[key] = value
    safe_id = _safe_path_segment(memory_id, label="memory_id")
    return await _rest_post(f"/v1/memories/{safe_id}", body, method="PATCH")


async def tool_get_memory(
    memory_id: str,
    format: str | None = None,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Fetch a memory by id, optionally in compressed form.

    ``format`` is the v3.6 §2.5-item-3 hook for letting MCP clients
    consume the same prose / dense compressed-variant representations
    that ``GET /v1/memories/{id}`` exposes via Accept-header
    content negotiation:

      * ``"prose"``  → prose narration body (Accept: text/plain)
      * ``"dense"``  → raw winning-variant content (Accept:
                       application/x-apollo-dense)
      * ``None``     → existing JSON ``MemoryItem`` (default,
                       backwards-compatible)

    Returns either the JSON-parsed dict (default path) or a
    ``{"format": <fmt>, "content": <body>, "memory_id": <id>}``
    envelope when ``format`` is set, so MCP clients always receive
    a structured response regardless of the body type underneath.
    """
    safe_id = _safe_path_segment(memory_id, label="memory_id")
    if format is None:
        return await _rest_get(f"/v1/memories/{safe_id}")
    accept_map = {
        "prose": "text/plain",
        "dense": "application/x-apollo-dense",
    }
    if format not in accept_map:
        raise ValueError("format must be 'prose' or 'dense'")
    body = await _rest_get_text(
        f"/v1/memories/{safe_id}", accept=accept_map[format],
    )
    return {
        "memory_id": memory_id,
        "format": format,
        "content": body,
    }


async def tool_create_memory(
    content: str,
    category: str = "facts",
    subcategory: str | None = None,
    metadata: dict[str, Any] | None = None,
    permission_mode: int | None = None,
    user: UserContext | None = None,
) -> dict[str, Any]:
    _safe_path_value(category, label="category", max_length=128)
    subcategory = _validate_optional_filter(subcategory, label="subcategory")
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
    safe_id = _safe_path_segment(memory_id, label="memory_id")
    status = await _rest_delete(f"/v1/memories/{safe_id}")
    return {"deleted": True, "status": status}


async def tool_list_memories(
    category: str | None = None,
    subcategory: str | None = None,
    limit: int = 20,
    offset: int = 0,
    user: UserContext | None = None,
) -> dict[str, Any]:
    limit = _bounded_int(
        limit, label="limit", minimum=1, maximum=MCP_DEFAULT_LIMIT_MAX,
    )
    offset = _bounded_int(
        offset, label="offset", minimum=0, maximum=MCP_OFFSET_MAX,
    )
    category = _validate_optional_filter(category, label="category")
    subcategory = _validate_optional_filter(subcategory, label="subcategory")
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
    rows = _bounded_list(
        memories, label="memories", max_items=MCP_BULK_CREATE_MAX_ITEMS,
    )
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"memories[{i}] must be an object")
        if "category" in row and row["category"] is not None:
            _safe_path_value(row["category"], label=f"memories[{i}].category", max_length=128)
        if "subcategory" in row and row["subcategory"] is not None:
            _safe_path_value(
                row["subcategory"],
                label=f"memories[{i}].subcategory",
                max_length=128,
            )
        if "namespace" in row and row["namespace"] is not None:
            _safe_path_value(row["namespace"], label=f"memories[{i}].namespace", max_length=128)
    ns = _connector_namespace()
    if ns:
        # Connector-scope env wins over per-row namespace. The
        # alternative (per-row wins) creates a footgun where a
        # bulk caller could bypass the connector's documented
        # write-stamp scope just by including ``"namespace": ...``
        # in each row. Power users who need cross-namespace bulk
        # creation should hit the REST API directly or run without
        # the env stamp.
        rows = [{**m, "namespace": ns} for m in rows]
    return await _rest_post("/v1/memories/bulk", {"memories": rows})


TOOLS: dict[str, dict[str, Any]] = {
    "search_memories": _tool(
        "Full-text search across MNEMOS memories. Returns ranked results. Filter by category and/or subcategory.",
        {
            "query": {"type": "string", "description": "Search query"},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": MCP_DEFAULT_LIMIT_MAX},
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
        (
            "Retrieve a single memory by its ID (mem_xxxxxxxxxxxx). "
            "Default response is the JSON memory object. Optional "
            "``format='prose'`` returns the prose-narrated body "
            "(human-readable), ``format='dense'`` returns the raw "
            "APOLLO compressed variant — both intended for clients "
            "that want to feed the compressed form straight to a "
            "downstream LLM without round-tripping through JSON."
        ),
        {
            "memory_id": {"type": "string"},
            "format": {
                "type": "string",
                "enum": ["prose", "dense"],
                "description": (
                    "Optional. ``prose`` → text/plain narration. "
                    "``dense`` → application/x-apollo-dense raw "
                    "variant. Omit for default JSON."
                ),
            },
        },
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
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": MCP_DEFAULT_LIMIT_MAX},
            "offset": {"type": "integer", "default": 0, "minimum": 0, "maximum": MCP_OFFSET_MAX},
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
                "maxItems": MCP_BULK_CREATE_MAX_ITEMS,
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
