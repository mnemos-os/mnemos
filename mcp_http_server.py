#!/usr/bin/env python3
"""MNEMOS MCP HTTP/SSE server — for ChatGPT Pro Developer Mode + any
remote MCP client that needs an HTTPS URL instead of a stdio process.

Reuses the same `Server("mnemos")` instance as mcp_server.py. Tool
definitions come from api/mcp_tools.py's canonical registry; the only
difference is the transport: stdio framing vs SSE-over-HTTP framing.
Tool surface is identical, so a memory written from Claude Desktop
(stdio) is queryable from ChatGPT Pro (SSE) and vice versa.

Auth: bearer token. The connector caller MUST send
  Authorization: Bearer <token>
on the SSE handshake. Prefer per-user token issuance with
MNEMOS_MCP_TOKENS=user:api_key[,user:api_key]. Legacy
MNEMOS_MCP_TOKEN remains supported for single-user deployments but
logs a warning because every client shares the same backend identity.

Transport security: TLS terminated upstream (Cloudflare Tunnel,
Tailscale Funnel, Caddy/nginx). This process listens on a local
HTTP port; the public URL is opaque to it.

Run:
  MNEMOS_MCP_TOKENS=user1:<mnemos-api-key-1>,user2:<mnemos-api-key-2>  \
  MNEMOS_BASE=http://localhost:5002  \
  python3 mcp_http_server.py --host 127.0.0.1 --port 5004
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, quote
from uuid import UUID

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from mcp.server.sse import SseServerTransport

from api.mcp_tools import (
    TOOL_REGISTRY,
    reset_mcp_backend_context,
    set_mcp_backend_context,
)

# Reuse the exact same Server instance + tool registrations from
# the stdio entry point. Importing for the side effect of having
# tools registered against `app`.
from mcp_server import app  # noqa: F401 (used by handle_sse below)

# stderr logging — matches mcp_server.py convention so log shipping
# from container stdout/stderr stays consistent.
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] mcp_http: %(message)s")
logger = logging.getLogger(__name__)


HTTP_TOOL_REGISTRY = TOOL_REGISTRY


@dataclass(frozen=True)
class MCPClientPrincipal:
    user_id: str | None
    api_key: str | None


def _fatal_auth_config(message: str) -> None:
    sys.stderr.write(message)
    sys.exit(2)


def _load_token_principals() -> dict[str, MCPClientPrincipal]:
    """Load bearer-token principals for the HTTP MCP edge.

    Preferred format:
      MNEMOS_MCP_TOKENS=user1:api_key1,user2:api_key2

    If the MCP bearer token must differ from the backend API key:
      MNEMOS_MCP_TOKENS=user1:mcp_token1:api_key1
    """
    raw_map = os.getenv("MNEMOS_MCP_TOKENS", "").strip()
    if raw_map:
        principals: dict[str, MCPClientPrincipal] = {}
        for item in raw_map.split(","):
            item = item.strip()
            if not item:
                continue
            parts = [part.strip() for part in item.split(":", 2)]
            if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
                _fatal_auth_config(
                    "FATAL: MNEMOS_MCP_TOKENS entries must be "
                    "user_id:token or user_id:mcp_token:api_key.\n"
                )
            user_id = parts[0]
            token = parts[1]
            api_key = parts[2] if len(parts) == 3 and parts[2] else token
            if token in principals:
                _fatal_auth_config(
                    "FATAL: duplicate bearer token in MNEMOS_MCP_TOKENS. "
                    "Each MCP client token must be unique.\n"
                )
            principals[token] = MCPClientPrincipal(user_id=user_id, api_key=api_key)
        if not principals:
            _fatal_auth_config("FATAL: MNEMOS_MCP_TOKENS was set but empty.\n")
        logger.info("Configured %d per-user MCP HTTP bearer token(s)", len(principals))
        return principals

    # Required bearer token. We refuse to start without one because
    # this edge exposes full memory write access.
    tok = os.getenv("MNEMOS_MCP_TOKEN", "").strip()
    if not tok:
        _fatal_auth_config(
            "FATAL: MNEMOS_MCP_TOKEN must be set. Refusing to expose the\n"
            "MCP server without bearer auth. Generate a token (e.g. via\n"
            "`openssl rand -hex 32`), set it in the environment, and\n"
            "configure the same token in the connector caller. For\n"
            "multi-tenant HTTP MCP, prefer MNEMOS_MCP_TOKENS.\n"
        )
    logger.warning(
        "WARNING: MCP HTTP/SSE is using one shared MNEMOS_MCP_TOKEN. "
        "All accepted clients will share the backend MNEMOS_API_KEY "
        "identity. Set MNEMOS_MCP_TOKENS=user:api_key,... for per-user "
        "token issuance and backend tenancy."
    )
    return {
        tok: MCPClientPrincipal(
            user_id=None,
            api_key=os.getenv("MNEMOS_API_KEY", "").strip() or None,
        )
    }


TOKEN_PRINCIPALS = _load_token_principals()


def _principal_id(principal: MCPClientPrincipal) -> str:
    """Return the stable caller identity used to bind SSE sessions."""
    api_key_fingerprint = hashlib.sha256((principal.api_key or "").encode()).hexdigest()
    if principal.user_id:
        return f"user:{principal.user_id}:api:{api_key_fingerprint}"
    return f"legacy-token:api:{api_key_fingerprint}"


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate `Authorization: Bearer <token>` on every request before
    the SSE handshake or the POST-message endpoint sees it. Reject
    everything else with 401 + a `WWW-Authenticate` header so the
    client knows what scheme to use."""

    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing or malformed Authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mnemos-mcp"'},
            )
        presented = auth.split(" ", 1)[1].strip()
        principal = TOKEN_PRINCIPALS.get(presented)
        if principal is None:
            return JSONResponse(
                {"error": "invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mnemos-mcp"'},
            )
        request.state.mnemos_mcp_principal = principal
        request.state.mnemos_mcp_principal_id = _principal_id(principal)
        return await call_next(request)


sse = SseServerTransport("/messages/")
_sse_session_principals: dict[str, str] = {}
_sse_session_bind_lock = asyncio.Lock()


def _session_id_key(session_id) -> str:
    if isinstance(session_id, UUID):
        return session_id.hex
    session_id_text = str(session_id)
    try:
        return UUID(session_id_text).hex
    except ValueError:
        return session_id_text


class AmbiguousSessionIdError(ValueError):
    """Raised when a request supplies conflicting session identifiers."""


def _extract_query_session_id(scope) -> str | None:
    query_string = scope.get("query_string", b"")
    if isinstance(query_string, str):
        query_string = query_string.encode()
    query_params = parse_qs(query_string.decode("latin-1"), keep_blank_values=True)
    has_session_id = "session_id" in query_params
    has_session_id_camel = "sessionId" in query_params
    if has_session_id and has_session_id_camel:
        raise AmbiguousSessionIdError("ambiguous session id")
    if has_session_id:
        values = query_params["session_id"]
    elif has_session_id_camel:
        values = query_params["sessionId"]
    else:
        return None
    if len(values) > 1:
        raise AmbiguousSessionIdError("ambiguous session id")
    raw_session_id = values[0]
    if raw_session_id:
        return _session_id_key(raw_session_id)
    return None


def _extract_path_session_id(scope) -> str | None:
    path = scope.get("path", "")
    root_path = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        path = path[len(root_path):]
    messages_prefix = "/messages/"
    if path.startswith(messages_prefix) and len(path) > len(messages_prefix):
        return _session_id_key(path[len(messages_prefix):].strip("/"))
    if root_path.rstrip("/") == "/messages" and path.strip("/"):
        return _session_id_key(path.strip("/"))
    return None


def _extract_session_id_from_scope(scope) -> str | None:
    query_session_id = _extract_query_session_id(scope)
    path_session_id = _extract_path_session_id(scope)
    if query_session_id and path_session_id and query_session_id != path_session_id:
        raise AmbiguousSessionIdError("ambiguous session id")
    return query_session_id or path_session_id


def _scope_state(scope) -> dict:
    state = scope.get("state")
    if isinstance(state, dict):
        return state
    return {}


def _authenticated_principal_id_from_scope(scope) -> str | None:
    state = _scope_state(scope)
    principal_id = state.get("mnemos_mcp_principal_id")
    if principal_id:
        return principal_id
    principal = state.get("mnemos_mcp_principal")
    if principal is not None:
        return _principal_id(principal)
    return None


@asynccontextmanager
async def _bound_sse_connection(request, principal_id: str):
    session_key = None
    transport_context = sse.connect_sse(
        request.scope, request.receive, request._send,
    )
    async with _sse_session_bind_lock:
        before_sessions = set(getattr(sse, "_read_stream_writers", {}))
        streams = await transport_context.__aenter__()
        after_sessions = set(getattr(sse, "_read_stream_writers", {}))
        new_sessions = after_sessions - before_sessions
        if len(new_sessions) != 1:
            await transport_context.__aexit__(None, None, None)
            raise RuntimeError(
                "Could not identify newly-created MCP SSE session for principal binding"
            )
        session_key = _session_id_key(new_sessions.pop())
        _sse_session_principals[session_key] = principal_id

    try:
        yield streams
    except BaseException as exc:
        _sse_session_principals.pop(session_key, None)
        await transport_context.__aexit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        _sse_session_principals.pop(session_key, None)
        await transport_context.__aexit__(None, None, None)


async def handle_post_message(scope, receive, send) -> None:
    try:
        session_id = _extract_session_id_from_scope(scope)
    except AmbiguousSessionIdError:
        response = PlainTextResponse("ambiguous session id", status_code=400)
        return await response(scope, receive, send)
    if session_id is None:
        response = PlainTextResponse("session_id is required", status_code=400)
        return await response(scope, receive, send)

    owner_principal_id = _sse_session_principals.get(session_id)
    if owner_principal_id is None:
        response = PlainTextResponse("session expired or never existed", status_code=404)
        return await response(scope, receive, send)

    caller_principal_id = _authenticated_principal_id_from_scope(scope)
    if caller_principal_id != owner_principal_id:
        response = PlainTextResponse("session does not belong to caller", status_code=403)
        return await response(scope, receive, send)

    forwarded_scope = dict(scope)
    forwarded_scope["query_string"] = f"session_id={quote(session_id, safe='')}".encode()

    return await sse.handle_post_message(forwarded_scope, receive, send)


async def handle_sse(request):
    """Open an SSE stream and pump MCP frames over it. The transport
    object owns the bidirectional plumbing; we just hand it the
    stream pair the ASGI runtime gave us."""
    # Starlette exposes the underlying ASGI send via a private attr on
    # request; the SDK examples accept this trade for now.
    principal = getattr(request.state, "mnemos_mcp_principal", None)
    principal_id = getattr(request.state, "mnemos_mcp_principal_id", None)
    if principal_id is None and principal is not None:
        principal_id = _principal_id(principal)
    context_tokens = set_mcp_backend_context(
        api_key=principal.api_key if principal else None,
        user_id=principal.user_id if principal else None,
    )
    try:
        async with _bound_sse_connection(request, principal_id or "unknown") as streams:
            read_stream, write_stream = streams
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
    finally:
        reset_mcp_backend_context(context_tokens)


async def healthz(_request):
    """Readiness probe. Skips bearer auth so deployment infra
    (cloudflared, k8s) can confirm the process is up without
    needing to share the token."""
    return PlainTextResponse("ok")


starlette_app = Starlette(
    routes=[
        Route("/healthz", endpoint=healthz),
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=handle_post_message),
    ],
    middleware=[Middleware(BearerAuthMiddleware)],
)


def main() -> None:
    p = argparse.ArgumentParser(description="MNEMOS MCP HTTP/SSE server")
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address (default: 127.0.0.1; use 0.0.0.0 if "
                        "running behind a tunnel/proxy that shares the box)")
    p.add_argument("--port", type=int, default=5004,
                   help="Listen port (default: 5004 — alongside MNEMOS API "
                        "on 5002, GRAEAE on 5002, federation on 5002)")
    args = p.parse_args()

    logger.info("MNEMOS MCP HTTP/SSE listening on %s:%d", args.host, args.port)
    logger.info("Bearer principals configured (count=%d)", len(TOKEN_PRINCIPALS))
    logger.info("MNEMOS backend: %s", os.getenv("MNEMOS_BASE",
                                                 "http://localhost:5002"))
    uvicorn.run(starlette_app, host=args.host, port=args.port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
