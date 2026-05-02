"""Canonical MCP tool registry and dispatcher for MNEMOS."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from mnemos.core.auth_context import UserContext

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
    current_mcp_backend_api_key,
    current_mcp_backend_namespace,
    current_mcp_backend_role,
    current_mcp_backend_user_id,
    reset_mcp_backend_context,
    set_mcp_backend_context,
)
from ._security import (
    _mcp_consult_rate_limit,
    _mcp_log_root_bypass,
    _mcp_log_tool_audit,
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

_WRITE_TOOLS = {
    "update_memory",
    "create_memory",
    "delete_memory",
    "kg_create_triple",
    "update_triple",
    "delete_triple",
    "bulk_create_memories",
    "branch_memory",
}

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
    audit_parameters = parameters if isinstance(parameters, dict) else {}
    context_caller_id = current_mcp_backend_user_id()
    caller_id = user.user_id if user is not None else context_caller_id
    caller_role = user.role if user is not None else current_mcp_backend_role()
    if tool_name not in TOOL_REGISTRY:
        _mcp_log_tool_audit(
            caller_id=caller_id,
            role=caller_role,
            tool_name=tool_name,
            parameters=audit_parameters,
            outcome="error",
            error_class="UnknownTool",
        )
        return {"success": False, "error": f"Unknown tool: {tool_name}"}

    tool_info = TOOL_REGISTRY[tool_name]
    handler = tool_info["handler"]

    call_parameters = dict(audit_parameters)
    call_parameters["user"] = user

    context_tokens = None
    if user is not None:
        context_role = current_mcp_backend_role()
        context_namespace = current_mcp_backend_namespace()
        if context_caller_id is not None and context_caller_id != user.user_id:
            _mcp_log_tool_audit(
                caller_id=context_caller_id,
                role=caller_role,
                tool_name=tool_name,
                parameters=audit_parameters,
                outcome="error",
                error_class="ContextMismatch",
            )
            return {"success": False, "error": "MCP caller context mismatch"}
        if context_role is not None and context_role != user.role:
            _mcp_log_tool_audit(
                caller_id=caller_id,
                role=context_role,
                tool_name=tool_name,
                parameters=audit_parameters,
                outcome="error",
                error_class="ContextMismatch",
            )
            return {"success": False, "error": "MCP caller context mismatch"}
        if context_namespace is not None and context_namespace != user.namespace:
            _mcp_log_tool_audit(
                caller_id=caller_id,
                role=caller_role,
                tool_name=tool_name,
                parameters=audit_parameters,
                outcome="error",
                error_class="ContextMismatch",
            )
            return {"success": False, "error": "MCP caller context mismatch"}
        if context_caller_id is None:
            context_tokens = set_mcp_backend_context(
                api_key=current_mcp_backend_api_key(),
                user_id=user.user_id,
                role=user.role,
                namespace=user.namespace,
            )
            caller_id = user.user_id
            caller_role = user.role

    if caller_role == "root":
        _mcp_log_root_bypass(
            caller_id=caller_id,
            tool_name=tool_name,
            parameters=audit_parameters,
        )

    rate_kind = "write" if tool_name in _WRITE_TOOLS else "read"
    try:
        await _mcp_consult_rate_limit(
            tool_name=tool_name,
            user_id=caller_id,
            kind=rate_kind,
        )
        result = await handler(**call_parameters)
        if isinstance(result, dict) and result.get("success") is False:
            _mcp_log_tool_audit(
                caller_id=caller_id,
                role=caller_role,
                tool_name=tool_name,
                parameters=audit_parameters,
                outcome="error",
                error_class="ToolError",
            )
        else:
            _mcp_log_tool_audit(
                caller_id=caller_id,
                role=caller_role,
                tool_name=tool_name,
                parameters=audit_parameters,
                outcome="success",
            )
        logger.info(
            "[MCP] Tool %s executed for caller=%s",
            tool_name,
            caller_id or "unknown",
        )
        return result
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        if status_code in (403, 404):
            logger.info(
                "[MCP] Tool %s returned invisible resource for caller=%s",
                tool_name,
                caller_id or "unknown",
            )
            _mcp_log_tool_audit(
                caller_id=caller_id,
                role=caller_role,
                tool_name=tool_name,
                parameters=audit_parameters,
                outcome="error",
                error_class="HTTPStatusError",
            )
            return {"success": False, "error": "Resource not found"}
        if status_code == 422:
            _mcp_log_tool_audit(
                caller_id=caller_id,
                role=caller_role,
                tool_name=tool_name,
                parameters=audit_parameters,
                outcome="error",
                error_class="HTTPStatusError",
            )
            return {"success": False, "error": "Invalid tool input"}
        logger.error(
            "[MCP] Tool %s failed with HTTP %s for caller=%s",
            tool_name,
            status_code,
            caller_id or "unknown",
        )
        _mcp_log_tool_audit(
            caller_id=caller_id,
            role=caller_role,
            tool_name=tool_name,
            parameters=audit_parameters,
            outcome="error",
            error_class="HTTPStatusError",
        )
        return {"success": False, "error": "Tool execution failed"}
    except ValueError:
        _mcp_log_tool_audit(
            caller_id=caller_id,
            role=caller_role,
            tool_name=tool_name,
            parameters=audit_parameters,
            outcome="error",
            error_class="ValueError",
        )
        return {"success": False, "error": "Invalid tool input"}
    except PermissionError as e:
        error = "Rate limit exceeded" if "rate limit" in str(e) else "Resource not found"
        _mcp_log_tool_audit(
            caller_id=caller_id,
            role=caller_role,
            tool_name=tool_name,
            parameters=audit_parameters,
            outcome="error",
            error_class=type(e).__name__,
        )
        return {"success": False, "error": error}
    except Exception as e:
        logger.error(
            "[MCP] Tool %s failed with %s for caller=%s",
            tool_name,
            type(e).__name__,
            caller_id or "unknown",
        )
        _mcp_log_tool_audit(
            caller_id=caller_id,
            role=caller_role,
            tool_name=tool_name,
            parameters=audit_parameters,
            outcome="error",
            error_class=type(e).__name__,
        )
        return {"success": False, "error": "Tool execution failed"}
    finally:
        if context_tokens is not None:
            reset_mcp_backend_context(context_tokens)


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
    "current_mcp_backend_api_key",
    "current_mcp_backend_namespace",
    "current_mcp_backend_role",
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
