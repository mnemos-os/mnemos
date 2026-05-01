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
_MCP_BACKEND_API_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mnemos_mcp_backend_api_key",
    default=None,
)
_MCP_BACKEND_USER_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mnemos_mcp_backend_user_id",
    default=None,
)


def set_mcp_backend_context(
    *,
    api_key: str | None = None,
    user_id: str | None = None,
) -> tuple[contextvars.Token[str | None], contextvars.Token[str | None]]:
    """Attach per-client backend attribution for the current MCP session."""
    api_key_token = _MCP_BACKEND_API_KEY.set(api_key)
    user_id_token = _MCP_BACKEND_USER_ID.set(user_id)
    return api_key_token, user_id_token


def reset_mcp_backend_context(
    tokens: tuple[contextvars.Token[str | None], contextvars.Token[str | None]],
) -> None:
    """Reset context set by set_mcp_backend_context()."""
    api_key_token, user_id_token = tokens
    _MCP_BACKEND_API_KEY.reset(api_key_token)
    _MCP_BACKEND_USER_ID.reset(user_id_token)


def current_mcp_backend_user_id() -> str | None:
    return _MCP_BACKEND_USER_ID.get()


def _mnemos_base() -> str:
    return get_settings().server.base.rstrip("/")


def _backend_headers() -> dict[str, str]:
    api_key = _MCP_BACKEND_API_KEY.get() or get_settings().server.api_key
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    user_id = _MCP_BACKEND_USER_ID.get()
    if user_id:
        headers["X-MNEMOS-User-Id"] = user_id
    return headers


_PATH_SEGMENT_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


def _safe_path_segment(value: object, *, label: str = "id") -> str:
    """Validate + URL-encode an identifier before splicing it into a
    REST path.

    MCP tools interpolate caller-controlled values such as
    ``memory_id`` and ``commit_hash`` into ``/v1/memories/{id}`` /
    ``/v1/memories/{id}/commits/{hash}`` URLs. Without validation, a
    value like ``../../admin`` lets ``httpx`` normalise dot segments
    out of the path entirely, so the request escapes the
    ``/v1/memories`` prefix and reaches other same-origin endpoints.
    With the new ``_rest_get_text`` helper returning raw response
    bodies, that path-traversal is a real exfiltration vector for
    any text endpoint (e.g. ``/metrics``).

    Defense in depth:
      1. Type-check + length-bound + character whitelist
         ``[A-Za-z0-9_-]`` — rejects ``/``, ``\\``, ``.``, ``?``,
         ``#`` and every other character that could split or rewrite
         the URL on the server side.
      2. Belt-and-braces URL-encode with ``safe=""`` so even if a
         future ID format adds new characters, they're guaranteed
         to land inside the path segment they were spliced into.
    """
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string, got {type(value).__name__}")
    if not _PATH_SEGMENT_PATTERN.match(value):
        raise ValueError(
            f"{label} must match {_PATH_SEGMENT_PATTERN.pattern} — got {value!r}"
        )
    return urllib.parse.quote(value, safe="")


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
