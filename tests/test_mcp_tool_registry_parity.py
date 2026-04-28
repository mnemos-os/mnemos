"""MCP registry parity tests.

These tests stub the external `mcp` SDK so they can validate MNEMOS'
registration contract without requiring the optional transport package in
the local test environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import sys
import types
import uuid


class _Tool:
    def __init__(self, *, name, description, inputSchema):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class _TextContent:
    def __init__(self, *, type, text):
        self.type = type
        self.text = text


class _Server:
    def __init__(self, name):
        self.name = name
        self._list_tools_handler = None
        self._call_tool_handler = None

    def list_tools(self):
        def decorator(fn):
            self._list_tools_handler = fn
            return fn
        return decorator

    def call_tool(self):
        def decorator(fn):
            self._call_tool_handler = fn
            return fn
        return decorator

    def create_initialization_options(self):
        return {}

    async def run(self, *_args, **_kwargs):
        return None


class _SseServerTransport:
    def __init__(self, *_args, **_kwargs):
        self._read_stream_writers = {}
        self._session_counter = 0
        self.last_session_id = None
        self.accepted_posts = []

    @contextlib.asynccontextmanager
    async def connect_sse(self, *_args, **_kwargs):
        self._session_counter += 1
        session_id = uuid.UUID(int=self._session_counter)
        self.last_session_id = session_id
        self._read_stream_writers[session_id] = object()
        try:
            yield (None, None)
        finally:
            self._read_stream_writers.pop(session_id, None)

    async def handle_post_message(self, scope, _receive, send):
        self.accepted_posts.append(scope.get("query_string", b""))
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b"Accepted"})


@contextlib.asynccontextmanager
async def _stdio_server():
    yield (None, None)


def _install_mcp_stubs(monkeypatch):
    # api.mcp_tools imports FastAPI handlers. Load FastAPI against real Starlette
    # before replacing only the MCP HTTP server's Starlette-facing modules below.
    importlib.import_module("fastapi")

    uvicorn = types.ModuleType("uvicorn")
    uvicorn.run = lambda *_args, **_kwargs: None

    starlette = types.ModuleType("starlette")
    starlette_applications = types.ModuleType("starlette.applications")
    starlette_middleware = types.ModuleType("starlette.middleware")
    starlette_middleware_base = types.ModuleType("starlette.middleware.base")
    starlette_responses = types.ModuleType("starlette.responses")
    starlette_routing = types.ModuleType("starlette.routing")

    class Starlette:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class Middleware:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class BaseHTTPMiddleware:
        def __init__(self, app=None, *args, **kwargs):
            self.app = app

    class JSONResponse:
        def __init__(self, content, status_code=200, headers=None):
            self.content = content
            self.status_code = status_code
            self.headers = headers or {}

        async def __call__(self, _scope, _receive, send):
            await send({"type": "http.response.start", "status": self.status_code, "headers": []})
            await send({"type": "http.response.body", "body": json.dumps(self.content).encode()})

    class PlainTextResponse:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code

        async def __call__(self, _scope, _receive, send):
            await send({"type": "http.response.start", "status": self.status_code, "headers": []})
            await send({"type": "http.response.body", "body": self.text.encode()})

    class Mount:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class Route:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    starlette_applications.Starlette = Starlette
    starlette_middleware.Middleware = Middleware
    starlette_middleware_base.BaseHTTPMiddleware = BaseHTTPMiddleware
    starlette_responses.JSONResponse = JSONResponse
    starlette_responses.PlainTextResponse = PlainTextResponse
    starlette_routing.Mount = Mount
    starlette_routing.Route = Route

    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setitem(sys.modules, "starlette", starlette)
    monkeypatch.setitem(sys.modules, "starlette.applications", starlette_applications)
    monkeypatch.setitem(sys.modules, "starlette.middleware", starlette_middleware)
    monkeypatch.setitem(sys.modules, "starlette.middleware.base", starlette_middleware_base)
    monkeypatch.setitem(sys.modules, "starlette.responses", starlette_responses)
    monkeypatch.setitem(sys.modules, "starlette.routing", starlette_routing)

    mcp_mod = types.ModuleType("mcp")
    mcp_types = types.ModuleType("mcp.types")
    mcp_types.Tool = _Tool
    mcp_types.TextContent = _TextContent

    mcp_server_mod = types.ModuleType("mcp.server")
    mcp_server_mod.Server = _Server

    mcp_stdio = types.ModuleType("mcp.server.stdio")
    mcp_stdio.stdio_server = _stdio_server

    mcp_sse = types.ModuleType("mcp.server.sse")
    mcp_sse.SseServerTransport = _SseServerTransport

    monkeypatch.setitem(sys.modules, "mcp", mcp_mod)
    monkeypatch.setitem(sys.modules, "mcp.types", mcp_types)
    monkeypatch.setitem(sys.modules, "mcp.server", mcp_server_mod)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", mcp_stdio)
    monkeypatch.setitem(sys.modules, "mcp.server.sse", mcp_sse)


def _fresh_import(module_name: str):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def _mcp_request(module, token: str):
    principal = module.TOKEN_PRINCIPALS[token]
    principal_id = module._principal_id(principal)
    return types.SimpleNamespace(
        scope={
            "type": "http",
            "method": "GET",
            "path": "/sse",
            "query_string": b"",
            "headers": [],
            "state": {
                "mnemos_mcp_principal": principal,
                "mnemos_mcp_principal_id": principal_id,
            },
        },
        receive=_empty_receive,
        _send=lambda _message: None,
        state=types.SimpleNamespace(
            mnemos_mcp_principal=principal,
            mnemos_mcp_principal_id=principal_id,
        ),
    )


async def _open_sse_session(monkeypatch, module, token: str):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_run(*_args, **_kwargs):
        started.set()
        await release.wait()

    monkeypatch.setattr(module.app, "run", blocking_run)
    task = asyncio.create_task(module.handle_sse(_mcp_request(module, token)))
    await asyncio.wait_for(started.wait(), timeout=1)
    return module.sse.last_session_id.hex, release, task


async def _post_message(
    module,
    token: str,
    session_id: str | None = None,
    *,
    path: str = "/messages/",
    query_string: str | bytes | None = None,
) -> tuple[int, str]:
    principal = module.TOKEN_PRINCIPALS[token]
    if query_string is None:
        query_string = f"session_id={session_id}" if session_id is not None else ""
    if isinstance(query_string, str):
        query_string = query_string.encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": query_string,
        "headers": [],
        "state": {
            "mnemos_mcp_principal": principal,
            "mnemos_mcp_principal_id": module._principal_id(principal),
        },
    }
    events = []

    async def send(message):
        events.append(message)

    await module.handle_post_message(scope, _empty_receive, send)
    status = next(event["status"] for event in events if event["type"] == "http.response.start")
    body = b"".join(
        event.get("body", b"")
        for event in events
        if event["type"] == "http.response.body"
    ).decode()
    return status, body


def test_stdio_tool_list_matches_canonical_registry(monkeypatch):
    _install_mcp_stubs(monkeypatch)
    from api.mcp_tools import TOOL_REGISTRY

    mcp_server = _fresh_import("mcp_server")
    tools = asyncio.run(mcp_server.app._list_tools_handler())

    assert {tool.name for tool in tools} == set(TOOL_REGISTRY)
    assert all(tool.inputSchema["type"] == "object" for tool in tools)


def test_every_registered_tool_dispatches_through_stdio(monkeypatch):
    _install_mcp_stubs(monkeypatch)
    from api.mcp_tools import TOOL_REGISTRY

    async def fake_handler(**kwargs):
        return {"success": True, "kwargs": kwargs}

    for tool in TOOL_REGISTRY.values():
        monkeypatch.setitem(tool, "handler", fake_handler)

    mcp_server = _fresh_import("mcp_server")
    for name in TOOL_REGISTRY:
        result = asyncio.run(mcp_server.app._call_tool_handler(name, {}))
        payload = json.loads(result[0].text)
        assert payload["success"] is True
        assert payload["kwargs"]["user"] is None


def test_http_registry_parity_with_stdio(monkeypatch):
    _install_mcp_stubs(monkeypatch)
    monkeypatch.setenv("MNEMOS_MCP_TOKENS", "alice:alice-api-key")

    from api.mcp_tools import TOOL_REGISTRY

    _fresh_import("mcp_server")
    mcp_http_server = _fresh_import("mcp_http_server")

    assert set(mcp_http_server.HTTP_TOOL_REGISTRY) == set(TOOL_REGISTRY)


def test_http_token_map_sets_backend_user_attribution(monkeypatch):
    _install_mcp_stubs(monkeypatch)
    monkeypatch.setenv(
        "MNEMOS_MCP_TOKENS",
        "alice:alice-api-key,bob:bob-mcp-token:bob-api-key",
    )

    from api.mcp_tools import _backend_headers, reset_mcp_backend_context, set_mcp_backend_context

    mcp_http_server = _fresh_import("mcp_http_server")

    alice = mcp_http_server.TOKEN_PRINCIPALS["alice-api-key"]
    tokens = set_mcp_backend_context(api_key=alice.api_key, user_id=alice.user_id)
    try:
        assert _backend_headers() == {
            "Authorization": "Bearer alice-api-key",
            "X-MNEMOS-User-Id": "alice",
        }
    finally:
        reset_mcp_backend_context(tokens)

    bob = mcp_http_server.TOKEN_PRINCIPALS["bob-mcp-token"]
    assert bob.user_id == "bob"
    assert bob.api_key == "bob-api-key"


def test_http_sse_message_posts_are_bound_to_session_principal(monkeypatch):
    _install_mcp_stubs(monkeypatch)
    monkeypatch.setenv(
        "MNEMOS_MCP_TOKENS",
        "alice:alice-token:alice-api-key,bob:bob-token:bob-api-key",
    )

    _fresh_import("mcp_server")
    mcp_http_server = _fresh_import("mcp_http_server")

    async def exercise():
        alice_session_id, alice_release, alice_task = await _open_sse_session(
            monkeypatch,
            mcp_http_server,
            "alice-token",
        )

        status, body = await _post_message(mcp_http_server, "bob-token", alice_session_id)
        assert status == 403
        assert body == "session does not belong to caller"

        status, body = await _post_message(mcp_http_server, "alice-token", alice_session_id)
        assert status == 202
        assert body == "Accepted"

        alice_release.set()
        await alice_task

        status, body = await _post_message(mcp_http_server, "bob-token", alice_session_id)
        assert status == 404
        assert body == "session expired or never existed"

        bob_session_id, bob_release, bob_task = await _open_sse_session(
            monkeypatch,
            mcp_http_server,
            "bob-token",
        )

        status, body = await _post_message(mcp_http_server, "bob-token", bob_session_id)
        assert status == 202
        assert body == "Accepted"

        bob_release.set()
        await bob_task

    asyncio.run(exercise())


def test_http_post_rejects_ambiguous_session_id_parameters(monkeypatch):
    _install_mcp_stubs(monkeypatch)
    monkeypatch.setenv(
        "MNEMOS_MCP_TOKENS",
        "alice:alice-token:alice-api-key,bob:bob-token:bob-api-key",
    )

    _fresh_import("mcp_server")
    mcp_http_server = _fresh_import("mcp_http_server")

    async def exercise():
        alice_session_id, alice_release, alice_task = await _open_sse_session(
            monkeypatch,
            mcp_http_server,
            "alice-token",
        )
        bob_session_id, bob_release, bob_task = await _open_sse_session(
            monkeypatch,
            mcp_http_server,
            "bob-token",
        )
        try:
            mcp_http_server.sse.accepted_posts.clear()

            status, _body = await _post_message(
                mcp_http_server,
                "bob-token",
                query_string=(
                    f"session_id={bob_session_id}&session_id={alice_session_id}"
                ),
            )
            assert status in {400, 403}
            assert mcp_http_server.sse.accepted_posts == []

            status, _body = await _post_message(
                mcp_http_server,
                "bob-token",
                query_string=(
                    f"sessionId={alice_session_id}&session_id={bob_session_id}"
                ),
            )
            assert status in {400, 403}
            assert mcp_http_server.sse.accepted_posts == []

            status, body = await _post_message(
                mcp_http_server,
                "bob-token",
                path=f"/messages/{bob_session_id}",
                query_string=f"session_id={alice_session_id}",
            )
            assert status == 400
            assert body == "ambiguous session id"
            assert mcp_http_server.sse.accepted_posts == []

            status, body = await _post_message(
                mcp_http_server,
                "bob-token",
                query_string=f"ignored=1&sessionId={bob_session_id}",
            )
            assert status == 202
            assert body == "Accepted"
            assert mcp_http_server.sse.accepted_posts == [
                f"session_id={bob_session_id}".encode()
            ]
        finally:
            alice_release.set()
            bob_release.set()
            await alice_task
            await bob_task

    asyncio.run(exercise())
