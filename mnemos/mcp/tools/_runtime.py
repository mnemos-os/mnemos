"""Shared runtime plumbing for MCP tools."""

from __future__ import annotations

import contextvars
import re
import urllib.parse
from typing import Any

import httpx

from mnemos.core.auth_context import UserContext
from mnemos.core.config import get_settings
from mnemos.db.mcp_repo import assert_memory_readable

HTTP_TIMEOUT = 30.0
MCP_BULK_CREATE_MAX_ITEMS = 100
MCP_DEFAULT_LIMIT_MAX = 500
MCP_TIMELINE_LIMIT_MAX = 1000
MCP_OFFSET_MAX = 100_000
_MCP_BACKEND_API_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mnemos_mcp_backend_api_key",
    default=None,
)
_MCP_BACKEND_USER_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mnemos_mcp_backend_user_id",
    default=None,
)
_MCP_BACKEND_ROLE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mnemos_mcp_backend_role",
    default=None,
)
_MCP_BACKEND_NAMESPACE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mnemos_mcp_backend_namespace",
    default=None,
)


def set_mcp_backend_context(
    *,
    api_key: str | None = None,
    user_id: str | None = None,
    role: str | None = None,
    namespace: str | None = None,
) -> tuple[
    contextvars.Token[str | None],
    contextvars.Token[str | None],
    contextvars.Token[str | None],
    contextvars.Token[str | None],
]:
    """Attach per-client backend attribution for the current MCP session."""
    api_key_token = _MCP_BACKEND_API_KEY.set(api_key)
    user_id_token = _MCP_BACKEND_USER_ID.set(user_id)
    role_token = _MCP_BACKEND_ROLE.set(role)
    namespace_token = _MCP_BACKEND_NAMESPACE.set(namespace)
    return api_key_token, user_id_token, role_token, namespace_token


def reset_mcp_backend_context(
    tokens: tuple[
        contextvars.Token[str | None],
        contextvars.Token[str | None],
        contextvars.Token[str | None],
        contextvars.Token[str | None],
    ],
) -> None:
    """Reset context set by set_mcp_backend_context()."""
    api_key_token, user_id_token, role_token, namespace_token = tokens
    _MCP_BACKEND_API_KEY.reset(api_key_token)
    _MCP_BACKEND_USER_ID.reset(user_id_token)
    _MCP_BACKEND_ROLE.reset(role_token)
    _MCP_BACKEND_NAMESPACE.reset(namespace_token)


def current_mcp_backend_user_id() -> str | None:
    return _MCP_BACKEND_USER_ID.get()


def current_mcp_backend_api_key() -> str | None:
    return _MCP_BACKEND_API_KEY.get()


def current_mcp_backend_role() -> str | None:
    return _MCP_BACKEND_ROLE.get()


def current_mcp_backend_namespace() -> str | None:
    return _MCP_BACKEND_NAMESPACE.get()


def _mnemos_base() -> str:
    return get_settings().server.base.rstrip("/")


def _backend_headers() -> dict[str, str]:
    api_key = _MCP_BACKEND_API_KEY.get() or get_settings().server.api_key
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    user_id = _MCP_BACKEND_USER_ID.get()
    if user_id:
        headers["X-MNEMOS-User-Id"] = user_id
    return headers


# Whitelist tightened around the actual ID grammar mnemos uses:
#   * canonical memory IDs: ``mem_<digits>_<hex>`` (alphanum + _)
#   * federated memory IDs: ``fed:<peer_name>:<remote_id>`` — the
#     ``:`` separator is part of the documented format in
#     ``mnemos.domain.federation.FEDERATION_ID_PREFIX``
#   * commit hashes / branch names: alphanum + ``_``, ``-``
# Dots are deliberately omitted — they're the path-traversal signal
# (``..``) and no current MNEMOS ID format requires them. If a
# future format does, add ``.`` and a separate ``..`` reject.
_PATH_SEGMENT_PATTERN = re.compile(r"\A[A-Za-z0-9_:-]{1,128}\Z")


_LOOSE_PATH_REJECT_PATTERN = re.compile(
    r"(?:\.\.)|[\/\\\x00-\x1f\x7f?#]"
)


def _safe_path_value(value: object, *, label: str = "value", max_length: int = 512) -> str:
    """Validate and encode free-form values before path/filter use."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string, got {type(value).__name__}")
    if not value:
        raise ValueError(f"{label} must be non-empty")
    if len(value) > max_length:
        raise ValueError(f"{label} must be at most {max_length} characters")
    if _LOOSE_PATH_REJECT_PATTERN.search(value):
        raise ValueError(
            f"{label} contains a path-rewrite character or traversal sequence"
        )
    return urllib.parse.quote(value, safe="")


def _safe_path_segment(value: object, *, label: str = "id") -> str:
    """Validate and encode one path segment before URL interpolation."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string, got {type(value).__name__}")
    if not _PATH_SEGMENT_PATTERN.match(value):
        raise ValueError(f"{label} must be a safe path segment")
    return urllib.parse.quote(value, safe=":")


def _bounded_int(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer, got {type(value).__name__}")
    if value < minimum or value > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _bounded_list(
    value: object,
    *,
    label: str,
    max_items: int,
) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list, got {type(value).__name__}")
    if len(value) > max_items:
        raise ValueError(f"{label} must contain at most {max_items} items")
    return value


async def _rest_get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.get(
            f"{_mnemos_base()}{path}",
            params=params,
            headers=_backend_headers(),
        )
        response.raise_for_status()
        return response.json() if response.content else {}


async def _rest_get_text(
    path: str, *, accept: str, params: dict[str, Any] | None = None,
) -> str:
    """GET that requests an alternate ``Accept`` representation
    and returns the raw body text.

    Used by MCP tools that proxy the HTTP API's content-negotiation
    paths — notably ``get_memory`` with ``format=prose|dense`` which
    surfaces the same prose/dense narrate output as the HTTP
    ``Accept: text/plain`` / ``Accept: application/x-apollo-dense``
    branches without parsing through JSON.
    """
    headers = _backend_headers()
    headers["Accept"] = accept
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.get(
            f"{_mnemos_base()}{path}", params=params, headers=headers,
        )
        response.raise_for_status()
        return response.text


async def _rest_post(path: str, body: dict[str, Any], method: str = "POST") -> Any:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        if method == "PATCH":
            response = await client.patch(
                f"{_mnemos_base()}{path}",
                json=body,
                headers=_backend_headers(),
            )
        else:
            response = await client.post(
                f"{_mnemos_base()}{path}",
                json=body,
                headers=_backend_headers(),
            )
        response.raise_for_status()
        return response.json() if response.content else {}


async def _rest_delete(path: str) -> int:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.delete(f"{_mnemos_base()}{path}", headers=_backend_headers())
        response.raise_for_status()
        return response.status_code


def _mcp_user_required(user: UserContext | None) -> UserContext:
    """Require the authenticated user context used by HTTP-backed MCP."""
    if user is None or not user.authenticated:
        raise PermissionError("authenticated user required for version tools")
    return user


def _mcp_is_root(user: UserContext) -> bool:
    return user.role == "root"


async def _mcp_assert_memory_readable(conn: Any, memory_id: str, user: UserContext) -> None:
    await assert_memory_readable(conn, memory_id, user)


def _tool(
    description: str,
    parameters: dict[str, Any],
    required: list[str] | None,
    handler: Any,
) -> dict[str, Any]:
    return {
        "description": description,
        "parameters": parameters,
        "required": required or [],
        "handler": handler,
    }
