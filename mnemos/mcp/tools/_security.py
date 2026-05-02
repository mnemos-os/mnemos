"""MCP tool audit and quota helpers."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from typing import Any

from mnemos.core.auth_context import UserContext

MCP_WRITE_RATE_LIMIT_PER_MINUTE = 60
MCP_READ_RATE_LIMIT_PER_MINUTE = 600
_TOOL_RATE_BUCKETS: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_MCP_AUDIT_LOGGER = logging.getLogger("mnemos.mcp.audit")


def _mcp_rate_limit_enabled() -> bool:
    from mnemos.core import rate_limit as core_rate_limit

    return bool(core_rate_limit.RATE_LIMIT_ENABLED)


def _mcp_default_limit_per_minute() -> int:
    from mnemos.core import rate_limit as core_rate_limit

    raw = str(core_rate_limit.RATE_LIMIT_DEFAULT or "").strip().lower()
    match = re.match(r"\A(\d+)\s*/\s*(second|minute|hour|day)s?\Z", raw)
    if not match:
        return 300
    count = int(match.group(1))
    unit = match.group(2)
    if unit == "second":
        return max(1, count * 60)
    if unit == "hour":
        return max(1, count // 60)
    if unit == "day":
        return max(1, count // (24 * 60))
    return max(1, count)


def _mcp_touch_bucket(
    *,
    key: tuple[str, str],
    limit: int,
    window_seconds: float,
) -> None:
    now = time.monotonic()
    cutoff = now - window_seconds
    bucket = _TOOL_RATE_BUCKETS[key]
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        raise PermissionError("rate limit exceeded")
    bucket.append(now)


async def _mcp_consult_rate_limit(
    *,
    tool_name: str,
    user_id: str | None,
    kind: str,
) -> None:
    """Consult the core rate-limit settings and touch the MCP budget bucket."""
    if not _mcp_rate_limit_enabled():
        return

    default_per_minute = _mcp_default_limit_per_minute()
    if kind == "write":
        limit = min(MCP_WRITE_RATE_LIMIT_PER_MINUTE, default_per_minute)
    elif kind == "read":
        limit = max(MCP_READ_RATE_LIMIT_PER_MINUTE, default_per_minute)
    else:
        raise ValueError("unknown MCP rate-limit bucket")

    _mcp_touch_bucket(
        key=(kind, user_id or "anonymous"),
        limit=limit,
        window_seconds=60.0,
    )


async def _mcp_enforce_write_rate_limit(
    *,
    tool_name: str,
    user: UserContext,
    limit: int,
    window_seconds: float = 60.0,
) -> None:
    """Per-tool guard for direct database write paths."""
    if not user.authenticated:
        raise PermissionError("authenticated user required for write tool")
    if not _mcp_rate_limit_enabled():
        return
    try:
        _mcp_touch_bucket(
            key=(tool_name, user.user_id),
            limit=limit,
            window_seconds=window_seconds,
        )
    except PermissionError as exc:
        raise PermissionError(f"rate limit exceeded for {tool_name}") from exc


def _mcp_parameter_shape(parameters: dict[str, Any]) -> dict[str, Any]:
    def shape(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            return {"type": "str", "length": len(value)}
        if isinstance(value, bool):
            return {"type": "bool"}
        if isinstance(value, int):
            return {"type": "int"}
        if isinstance(value, float):
            return {"type": "float"}
        if isinstance(value, list):
            item_types = sorted({type(item).__name__ for item in value[:10]})
            return {"type": "list", "count": len(value), "item_types": item_types}
        if isinstance(value, dict):
            return {"type": "dict", "count": len(value)}
        if value is None:
            return {"type": "none"}
        return {"type": type(value).__name__}

    return {key: shape(value) for key, value in sorted(parameters.items())}


def _mcp_log_tool_audit(
    *,
    caller_id: str | None,
    role: str | None,
    tool_name: str,
    parameters: dict[str, Any],
    outcome: str,
    error_class: str | None = None,
) -> None:
    payload = {
        "caller_user_id": caller_id or "unknown",
        "role": role or "unknown",
        "tool": tool_name,
        "parameter_shape": _mcp_parameter_shape(parameters),
        "outcome": outcome,
    }
    if error_class:
        payload["error_class"] = error_class
    _MCP_AUDIT_LOGGER.info("mcp_tool_invocation %s", payload)


def _mcp_log_root_bypass(
    *,
    caller_id: str | None,
    tool_name: str,
    parameters: dict[str, Any],
) -> None:
    _MCP_AUDIT_LOGGER.warning(
        "mcp_root_bypass %s",
        {
            "caller_user_id": caller_id or "unknown",
            "role": "root",
            "tool": tool_name,
            "parameter_shape": _mcp_parameter_shape(parameters),
        },
    )
