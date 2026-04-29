"""Canonical MCP tool registry and dispatcher for MNEMOS."""

from __future__ import annotations

import logging
from typing import Any

from mnemos.api.dependencies import UserContext

from ._runtime import (
    _backend_headers,
    _mcp_assert_memory_readable,
    _mcp_is_root,
    _mcp_user_required,
    _mnemos_base,
    _rest_delete,
    _rest_get,
    _rest_post,
    _tool,
    current_mcp_backend_user_id,
    reset_mcp_backend_context,
    set_mcp_backend_context,
)
from .dag import (
    TOOLS as DAG_TOOLS,
    tool_branch_memory,
    tool_checkout_memory,
    tool_diff_memory_commits,
    tool_log_memory,
)
from .kg import (
    TOOLS as KG_TOOLS,
    tool_delete_triple,
    tool_kg_create_triple,
    tool_kg_search,
    tool_kg_timeline,
    tool_update_triple,
)
from .memory import (
    TOOLS as MEMORY_TOOLS,
    tool_bulk_create_memories,
    tool_create_memory,
    tool_delete_memory,
    tool_get_memory,
    tool_get_stats,
    tool_list_memories,
    tool_search_memories,
    tool_update_memory,
)
from .models import TOOLS as MODEL_TOOLS, tool_recommend_model

logger = logging.getLogger(__name__)

_DOMAIN_TOOLS: dict[str, dict[str, Any]] = {}
for _domain_tools in (MEMORY_TOOLS, KG_TOOLS, DAG_TOOLS, MODEL_TOOLS):
    _DOMAIN_TOOLS.update(_domain_tools)

_TOOL_ORDER = [
    "search_memories",
    "update_memory",
    "get_memory",
    "create_memory",
    "delete_memory",
    "list_memories",
    "get_stats",
    "kg_create_triple",
    "kg_search",
    "kg_timeline",
    "update_triple",
    "delete_triple",
    "bulk_create_memories",
    "log_memory",
    "branch_memory",
    "diff_memory_commits",
    "checkout_memory",
    "recommend_model",
]

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    name: _DOMAIN_TOOLS[name]
    for name in _TOOL_ORDER
}

TOOLS = TOOL_REGISTRY


def tool_input_schema(tool_info: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": tool_info["parameters"],
    }
    if tool_info.get("required"):
        schema["required"] = tool_info["required"]
    return schema


async def execute_tool(
    tool_name: str,
    parameters: dict[str, Any],
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Execute an MCP tool."""
    if tool_name not in TOOL_REGISTRY:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}

    tool_info = TOOL_REGISTRY[tool_name]
    handler = tool_info["handler"]

    call_parameters = dict(parameters)
    call_parameters["user"] = user

    try:
        result = await handler(**call_parameters)
        logger.info(f"[MCP] Tool {tool_name} executed successfully")
        return result
    except Exception as e:
        logger.error(f"[MCP] Tool {tool_name} failed: {e}")
        return {"success": False, "error": str(e)}


__all__ = [
    "TOOL_REGISTRY",
    "TOOLS",
    "_backend_headers",
    "_mcp_assert_memory_readable",
    "_mcp_is_root",
    "_mcp_user_required",
    "_mnemos_base",
    "_rest_delete",
    "_rest_get",
    "_rest_post",
    "_tool",
    "current_mcp_backend_user_id",
    "execute_tool",
    "reset_mcp_backend_context",
    "set_mcp_backend_context",
    "tool_branch_memory",
    "tool_bulk_create_memories",
    "tool_checkout_memory",
    "tool_create_memory",
    "tool_delete_memory",
    "tool_delete_triple",
    "tool_diff_memory_commits",
    "tool_get_memory",
    "tool_get_stats",
    "tool_input_schema",
    "tool_kg_create_triple",
    "tool_kg_search",
    "tool_kg_timeline",
    "tool_list_memories",
    "tool_log_memory",
    "tool_recommend_model",
    "tool_search_memories",
    "tool_update_memory",
    "tool_update_triple",
]
