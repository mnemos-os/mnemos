"""MCP HTTP health endpoint tests."""

from __future__ import annotations

import importlib
import sys

import pytest
from httpx import ASGITransport, AsyncClient


def _fresh_http(monkeypatch: pytest.MonkeyPatch):
    from mnemos.core import config

    monkeypatch.setenv("MNEMOS_MCP_TOKENS", "health:health-token")
    config._reset_settings_for_tests()
    sys.modules.pop("mnemos.mcp.http", None)
    return importlib.import_module("mnemos.mcp.http")


@pytest.mark.asyncio
async def test_mcp_http_health_routes_are_unauthenticated(monkeypatch: pytest.MonkeyPatch):
    http = _fresh_http(monkeypatch)

    async with AsyncClient(
        transport=ASGITransport(app=http.starlette_app),
        base_url="http://testserver",
    ) as client:
        for path in ("/health", "/healthz"):
            response = await client.get(path)

            assert response.status_code == 200
            assert response.content == b"ok"
            assert response.headers["content-type"].startswith("text/plain")
