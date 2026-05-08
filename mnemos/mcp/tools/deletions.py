"""MCP deletion-log tool handlers."""

from __future__ import annotations

from typing import Any

from mnemos.core.auth_context import UserContext

from ._runtime import (
    MCP_OFFSET_MAX,
    _bounded_int,
    _mcp_is_root,
    _mcp_user_required,
    _rest_get,
    _safe_path_value,
    _tool,
)


def _required_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


async def tool_list_deletions(
    from_ts: str,
    to_ts: str,
    owner_id: str | None = None,
    page: int = 1,
    user: UserContext | None = None,
) -> dict[str, Any]:
    caller = _mcp_user_required(user)
    if not _mcp_is_root(caller):
        raise PermissionError("root token required for list_deletions")

    from_ts = _required_string(from_ts, label="from_ts")
    to_ts = _required_string(to_ts, label="to_ts")
    page = _bounded_int(page, label="page", minimum=1, maximum=MCP_OFFSET_MAX)
    params: dict[str, Any] = {
        "from": from_ts,
        "to": to_ts,
        "page": page,
    }
    if owner_id:
        _safe_path_value(
            owner_id,
            label="owner_id",
            max_length=256,
        )
        params["owner_id"] = owner_id
    return await _rest_get("/admin/deletion-log", params=params)


TOOLS: dict[str, dict[str, Any]] = {
    "list_deletions": _tool(
        "List GDPR deletion-log audit rows. Root token required.",
        {
            "from_ts": {
                "type": "string",
                "format": "date-time",
                "description": "Inclusive lower requested_at bound.",
            },
            "to_ts": {
                "type": "string",
                "format": "date-time",
                "description": "Inclusive upper requested_at bound.",
            },
            "owner_id": {
                "type": "string",
                "description": "Optional owner_id filter.",
            },
            "page": {
                "type": "integer",
                "minimum": 1,
                "default": 1,
            },
        },
        ["from_ts", "to_ts"],
        tool_list_deletions,
    ),
}
