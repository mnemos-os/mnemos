#!/usr/bin/env python3
"""MNEMOS MCP HTTP/SSE server — for ChatGPT Pro Developer Mode + any
remote MCP client that needs an HTTPS URL instead of a stdio process.

Reuses the same `Server("mnemos")` instance as mcp_server.py. Tool
definitions come from mnemos/mcp/tools's canonical registry; the only
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
import json
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote
from uuid import UUID

import uvicorn
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
try:
    from starlette.responses import StreamingResponse
except ImportError:  # pragma: no cover - exercised only by lightweight test stubs.
    StreamingResponse = None  # type: ignore[assignment]
from starlette.routing import Mount, Route

from mnemos.core.config import get_settings, mcp_nats_raw_enabled
# Reuse the exact same Server instance + tool registrations from
# the stdio entry point. Importing for the side effect of having
# tools registered against `app`.
from mnemos.mcp.stdio import app  # noqa: F401 (used by handle_sse below)
from mnemos.mcp.tools import (
    TOOL_REGISTRY,
    reset_mcp_backend_context,
    set_mcp_backend_context,
)

# stderr logging — matches mcp_server.py convention so log shipping
# from container stdout/stderr stays consistent.
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] mcp_http: %(message)s")
logger = logging.getLogger(__name__)


HTTP_TOOL_REGISTRY = TOOL_REGISTRY
# #181: removed `DEFAULT_NATS_SSE_SUBJECT = "mnemos.*.*.default"` —
# defined but never referenced. SSE subject defaults are derived
# per-principal from auth context, not a global string constant.
NATS_SSE_PATH = "/mcp/events/stream"
NATS_SSE_QUEUE_MAXSIZE = 256
NATS_SSE_MAX_CONSECUTIVE_DROPS = 1000


@dataclass(frozen=True)
class MCPClientPrincipal:
    user_id: str | None
    api_key: str | None


@dataclass(frozen=True)
class MCPUserContext:
    user_id: str | None
    role: str
    namespace: str


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
    settings = get_settings()
    raw_map = settings.mcp.tokens.strip()
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
    tok = settings.mcp.token.strip()
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
            api_key=settings.server.api_key.strip() or None,
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
        if request.url.path in {"/health", "/healthz"}:
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
    principal_context = await _resolve_mcp_user_context(request)
    context_tokens = set_mcp_backend_context(
        api_key=principal.api_key if principal else None,
        user_id=principal_context.user_id,
        role=principal_context.role,
        namespace=principal_context.namespace,
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


_principal_context_cache: dict[str, MCPUserContext] = {}


def _query_value(request, name: str, default: str) -> str:
    params = getattr(request, "query_params", {})
    value = params.get(name, default) if params is not None else default
    return value or default


def _safe_subject_token(value: str, *, label: str) -> str:
    if value == "*":
        return value
    if not value.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"invalid NATS {label}: {value}")
    return value


def _safe_namespace(namespace: str | None) -> str:
    return (namespace or "default").replace(".", "_")


def _is_operator_context(context: MCPUserContext) -> bool:
    return context.role in {"root", "operator"}


async def _resolve_mcp_user_context(request) -> MCPUserContext:
    principal = getattr(getattr(request, "state", None), "mnemos_mcp_principal", None)
    principal_id = getattr(getattr(request, "state", None), "mnemos_mcp_principal_id", None)
    if principal_id and principal_id in _principal_context_cache:
        return _principal_context_cache[principal_id]

    if principal is not None and principal.api_key:
        try:
            import httpx

            base = get_settings().server.base.rstrip("/")
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{base}/auth/oauth/me",
                    headers={"Authorization": f"Bearer {principal.api_key}"},
                )
            response.raise_for_status()
            body = response.json()
            context = MCPUserContext(
                user_id=body.get("user_id") or principal.user_id,
                role=body.get("role") or "user",
                namespace=body.get("namespace") or principal.user_id or "default",
            )
            if principal_id:
                _principal_context_cache[principal_id] = context
            return context
        except Exception as exc:
            logger.warning("MCP NATS SSE principal context lookup failed: %s", exc)

    context = MCPUserContext(
        user_id=getattr(principal, "user_id", None),
        role="user",
        namespace=getattr(principal, "user_id", None) or "default",
    )
    if principal_id:
        _principal_context_cache[principal_id] = context
    return context


def _parse_nats_sse_subjects(request, context: MCPUserContext) -> list[str]:
    if _is_operator_context(context):
        raw = request.query_params.get("subjects") if hasattr(request, "query_params") else None
        if raw:
            subjects = [part.strip() for part in raw.split(",") if part.strip()]
            invalid = [
                subject for subject in subjects
                if not subject.startswith("mnemos.") or any(ch.isspace() for ch in subject)
            ]
            if invalid:
                raise ValueError(f"invalid NATS subject filter: {invalid[0]}")
            if subjects:
                return subjects

    event_class = _safe_subject_token(_query_value(request, "event_class", "*"), label="event_class")
    event_action = _safe_subject_token(_query_value(request, "event_action", "*"), label="event_action")
    return [f"mnemos.{event_class}.{event_action}.{_safe_namespace(context.namespace)}"]


def _sse_frame(event: str, data: str) -> bytes:
    lines = [f"event: {event}"]
    for line in data.splitlines() or [""]:
        lines.append(f"data: {line}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _nats_msg_data(msg: Any) -> str:
    data = getattr(msg, "data", b"")
    if isinstance(data, str):
        return data
    return bytes(data).decode("utf-8", errors="replace")


def _nats_sse_raw_enabled() -> bool:
    return mcp_nats_raw_enabled()


def _nats_sse_data(msg: Any) -> str:
    raw = _nats_msg_data(msg)
    if _nats_sse_raw_enabled():
        return raw
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    summary = {
        "subject": getattr(msg, "subject", "mnemos.event"),
        "memory_id": payload.get("memory_id") or payload.get("id"),
        "namespace": payload.get("namespace"),
        "category": payload.get("category"),
        "source_node": payload.get("source_node"),
    }
    return json.dumps({k: v for k, v in summary.items() if v is not None})


async def _subscribe_nats_sse_subject(js: Any, subject: str) -> Any:
    """Subscribe an MCP SSE bridge to a NATS subject.

    LIVE-ONLY telemetry contract. Both branches deliberately give
    up JetStream durability/replay semantics:

      * Core-NATS path (``js._nc.subscribe``) — used when the
        underlying connection exposes the core NATS handle. Pure
        live pub/sub: messages published while no SSE client is
        connected are dropped by the broker, no replay window, no
        delivery acks.
      * JetStream path — uses ``DeliverPolicy.NEW`` +
        ``AckPolicy.NONE``: each subscriber starts at the live
        edge, does NOT replay the 30-day stream backlog, and
        does NOT ack messages back to the broker. So even though
        this branch goes through JetStream API, the SSE bridge
        is configured to behave like a live-only feed.

    Operator expectations: ``GET /sse?subjects=...`` is a
    real-time telemetry stream for SSE clients that are
    connected RIGHT NOW. It is NOT a replay-able audit log; for
    historical reads use the HTTP REST surface (``GET /v1/
    memories/...``, ``GET /v1/federation/feed``, etc.) which
    runs through the visibility-gated repository path.

    Codex round-10 of the v4.2-NATS corpus review surfaced the
    expectation gap: a docs-aware operator might assume a
    NATS-backed SSE over MNEMOS events implied JetStream
    semantics. This docstring + ``docs/NATS_OPERATIONS.md``
    "MCP event bridge" section make the live-only contract
    explicit.
    """
    nc = getattr(js, "_nc", None)
    subscribe = getattr(nc, "subscribe", None)
    if subscribe is not None:
        return await subscribe(subject)

    try:
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy  # type: ignore

        config = ConsumerConfig(
            deliver_policy=DeliverPolicy.NEW,
            ack_policy=AckPolicy.NONE,
        )
    except ImportError:
        config = None
    return await js.subscribe(subject, config=config)


def _is_nats_sse_timeout(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.TimeoutError):
        return True
    return exc.__class__.__name__ == "TimeoutError" and exc.__class__.__module__.startswith("nats")


async def _unsubscribe_nats_sse(sub: Any) -> None:
    unsubscribe = getattr(sub, "unsubscribe", None)
    if unsubscribe is None:
        return
    result = unsubscribe()
    if hasattr(result, "__await__"):
        await result


async def _nats_sse_event_source(subscriptions: list[Any]):
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=NATS_SSE_QUEUE_MAXSIZE)
    dropped_total = 0

    def enqueue(kind: str, item: Any) -> int:
        nonlocal dropped_total
        try:
            queue.put_nowait((kind, item))
            return dropped_total
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            dropped_total += 1
            queue.put_nowait((kind, item))
            return dropped_total

    async def pump(sub: Any) -> None:
        while True:
            try:
                msg = await sub.next_msg(timeout=1)
                dropped = enqueue("message", msg)
                if dropped:
                    enqueue("dropped", dropped)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _is_nats_sse_timeout(exc):
                    continue
                enqueue("error", exc)
                return

    tasks = [asyncio.create_task(pump(sub)) for sub in subscriptions]
    consecutive_drops = 0
    try:
        while True:
            kind, item = await queue.get()
            if kind == "error":
                logger.warning("NATS SSE stream closing after subscription error: %s", item)
                yield _sse_frame(
                    "error",
                    json.dumps({"error": "nats_connection_lost", "detail": str(item)}),
                )
                return
            if kind == "dropped":
                consecutive_drops += 1
                yield _sse_frame("dropped", json.dumps({"count": item}))
                if consecutive_drops >= NATS_SSE_MAX_CONSECUTIVE_DROPS:
                    yield _sse_frame("error", json.dumps({"reason": "lagging"}))
                    return
                continue
            consecutive_drops = 0
            yield _sse_frame(getattr(item, "subject", "mnemos.event"), _nats_sse_data(item))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            *(_unsubscribe_nats_sse(sub) for sub in subscriptions),
            return_exceptions=True,
        )


async def handle_nats_event_stream(request):
    """Expose MNEMOS bus events as a thin NATS-to-SSE bridge.

    This is intentionally separate from `/sse`, which is the MCP SDK's
    bidirectional protocol transport.
    """
    if StreamingResponse is None:
        return PlainTextResponse("streaming responses unavailable", status_code=503)

    try:
        context = await _resolve_mcp_user_context(request)
        subjects = _parse_nats_sse_subjects(request, context)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)

    from mnemos.nats.client import get_jetstream

    js = get_jetstream()
    if js is None:
        return PlainTextResponse("NATS unavailable", status_code=503)

    subscriptions: list[Any] = []
    try:
        for subject in subjects:
            subscriptions.append(await _subscribe_nats_sse_subject(js, subject))
    except Exception as exc:
        await asyncio.gather(
            *(_unsubscribe_nats_sse(sub) for sub in subscriptions),
            return_exceptions=True,
        )
        logger.warning("NATS SSE subscription failed for subjects=%s: %s", subjects, exc)
        return PlainTextResponse("NATS unavailable", status_code=503)

    return StreamingResponse(
        _nats_sse_event_source(subscriptions),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def healthz(_request):
    """Readiness probe. Skips bearer auth so deployment infra
    (cloudflared, k8s) can confirm the process is up without
    needing to share the token."""
    return PlainTextResponse("ok")


async def _drain_audit_tasks_on_shutdown() -> None:
    """Round-3 residual #2 of #146 (#149): drain in-flight MCP audit
    persist tasks before the loop closes.

    Without this, the SSE bridge can deliver tool results and the
    Starlette teardown can cancel outstanding fire-and-forget audit
    tasks before the HTTP POST completes.
    """
    from mnemos.mcp.tools._security import drain_pending_audit_tasks

    try:
        drained = await drain_pending_audit_tasks(timeout=5.0)
        if drained:
            logger.info(
                "drained %d pending mcp_audit_log persist task(s) on shutdown",
                drained,
            )
    except Exception:
        # Drain failures must NOT propagate through Starlette
        # shutdown; the underlying logger entry is the always-on
        # surface and the dropped row is recoverable from logs.
        logger.exception("mcp_audit drain on http shutdown failed")


@asynccontextmanager
async def _mcp_http_lifespan(_app: Starlette):
    """Lifespan context manager wrapping the audit-drain on
    shutdown. Replaces the pre-Starlette-1.0 `on_shutdown=[...]`
    kwarg, which was removed in Starlette 1.0.0
    (deprecated in 0.x). Caught by the PROTEUS fresh-install
    barrage on 2026-05-08; a fresh install on Python 3.13 + the
    current Starlette pin would 9-fail in test_mcp_nats_sse +
    test_mcp_http_health + test_connector_smoke without this.
    """
    yield
    await _drain_audit_tasks_on_shutdown()


starlette_app = Starlette(
    routes=[
        Route("/health", endpoint=healthz),
        Route("/healthz", endpoint=healthz),
        Route("/sse", endpoint=handle_sse),
        Route(NATS_SSE_PATH, endpoint=handle_nats_event_stream),
        Mount("/messages/", app=handle_post_message),
    ],
    middleware=[Middleware(BearerAuthMiddleware)],
    lifespan=_mcp_http_lifespan,
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
    logger.info("MNEMOS backend: %s", get_settings().server.base)
    uvicorn.run(starlette_app, host=args.host, port=args.port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
