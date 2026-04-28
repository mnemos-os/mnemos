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
        pass

    @contextlib.asynccontextmanager
    async def connect_sse(self, *_args, **_kwargs):
        yield (None, None)

    async def handle_post_message(self, _scope, _receive, _send):
        return None


@contextlib.asynccontextmanager
async def _stdio_server():
    yield (None, None)


def _install_mcp_stubs(monkeypatch):
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

    class PlainTextResponse:
        def __init__(self, text):
            self.text = text

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
