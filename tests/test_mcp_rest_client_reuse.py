from __future__ import annotations

import pytest


class _Response:
    content = b"{}"
    text = "{}"
    status_code = 204

    def __init__(self, payload=None):
        self._payload = payload or {"ok": True}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


@pytest.mark.asyncio
async def test_mcp_rest_helpers_reuse_single_async_client(monkeypatch):
    from mnemos.mcp.tools import _runtime

    await _runtime._close_rest_client()
    created = []

    class Client:
        is_closed = False

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.gets = []
            self.posts = []
            created.append(self)

        async def get(self, url, params=None, headers=None):
            self.gets.append((url, params, headers))
            return _Response({"method": "get"})

        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json, headers))
            return _Response({"method": "post"})

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(_runtime.httpx, "AsyncClient", Client)

    assert await _runtime._rest_get("/stats") == {"method": "get"}
    assert await _runtime._rest_post("/v1/memories/search", {"query": "x"}) == {"method": "post"}

    assert len(created) == 1
    assert created[0].kwargs["limits"].max_connections == 100
    assert created[0].kwargs["limits"].max_keepalive_connections == 50

    await _runtime._close_rest_client()


def test_lifespan_cleanup_hook_registry_is_idempotent(monkeypatch):
    from mnemos.core import lifecycle

    monkeypatch.setattr(lifecycle, "_lifespan_cleanup_hooks", {})

    async def first():
        return None

    async def second():
        return None

    lifecycle.register_lifespan_cleanup_hook("mcp rest client", first)
    lifecycle.register_lifespan_cleanup_hook("mcp rest client", second)

    assert list(lifecycle._lifespan_cleanup_hooks) == ["mcp rest client"]
    assert lifecycle._lifespan_cleanup_hooks["mcp rest client"] is second
