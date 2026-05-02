"""Regression tests for authenticated MCP transport user pass-through."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from mnemos.core.auth_context import UserContext
from tests.test_mcp_tool_registry_parity import (
    _fresh_import,
    _install_mcp_stubs,
    _mcp_request,
)


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def test_stdio_dispatch_passes_authenticated_context_user(monkeypatch):
    _install_mcp_stubs(monkeypatch)
    mcp_server = _fresh_import("mnemos.mcp.stdio")
    from mnemos.mcp.tools import reset_mcp_backend_context, set_mcp_backend_context

    seen: dict[str, UserContext | None] = {}

    async def fake_execute_tool(_name, _arguments, user=None):
        seen["user"] = user
        return {"success": True, "user_id": user.user_id if user else None}

    monkeypatch.setattr(mcp_server, "execute_tool", fake_execute_tool)
    tokens = set_mcp_backend_context(
        api_key="alice-api-key",
        user_id="alice",
        role="user",
        namespace="alice-ns",
    )
    try:
        result = asyncio.run(mcp_server.app._call_tool_handler("get_stats", {}))
    finally:
        reset_mcp_backend_context(tokens)

    payload = json.loads(result[0].text)
    assert payload == {"success": True, "user_id": "alice"}
    assert seen["user"] is not None
    assert seen["user"].user_id == "alice"


def test_http_sse_dispatch_passes_authenticated_context_user(monkeypatch):
    _install_mcp_stubs(monkeypatch)
    monkeypatch.setenv("MNEMOS_MCP_TOKENS", "alice:alice-token:alice-api-key")
    mcp_server = _fresh_import("mnemos.mcp.stdio")
    mcp_http = _fresh_import("mnemos.mcp.http")

    principal = mcp_http.TOKEN_PRINCIPALS["alice-token"]
    principal_id = mcp_http._principal_id(principal)
    mcp_http._principal_context_cache[principal_id] = mcp_http.MCPUserContext(
        user_id="alice",
        role="user",
        namespace="alice-ns",
    )
    seen: dict[str, UserContext | None] = {}

    async def fake_execute_tool(_name, _arguments, user=None):
        seen["user"] = user
        return {"success": True, "user_id": user.user_id if user else None}

    async def run_one_tool(*_args, **_kwargs):
        await mcp_http.app._call_tool_handler("get_stats", {})

    monkeypatch.setattr(mcp_server, "execute_tool", fake_execute_tool)
    monkeypatch.setattr(mcp_http.app, "run", run_one_tool)

    asyncio.run(mcp_http.handle_sse(_mcp_request(mcp_http, "alice-token")))

    assert seen["user"] is not None
    assert seen["user"].user_id == "alice"


class _PoolContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def acquire(self):
        return _PoolContext()


@pytest.mark.asyncio
async def test_dag_log_memory_uses_direct_db_path_when_user_and_pool_exist(monkeypatch):
    import mnemos.core.lifecycle as lc
    from mnemos.mcp.tools import dag as mcp_dag

    row = {
        "commit_hash": "abc123",
        "version_num": 1,
        "change_type": "create",
        "category": "note",
        "snapshot_at": datetime(2026, 5, 2, tzinfo=timezone.utc),
        "snapshot_by": "alice",
    }
    monkeypatch.setattr(lc, "_pool", _Pool())
    monkeypatch.setattr(mcp_dag, "_mcp_assert_memory_readable", AsyncMock())
    monkeypatch.setattr(mcp_dag.mcp_repo, "fetch_memory_log", AsyncMock(return_value=[row]))
    monkeypatch.setattr(mcp_dag, "_rest_get", AsyncMock())

    result = await mcp_dag.tool_log_memory(
        "mem_1234567890123_a1b2c3",
        user=_alice(),
    )

    mcp_dag._rest_get.assert_not_awaited()
    mcp_dag._mcp_assert_memory_readable.assert_awaited_once()
    mcp_dag.mcp_repo.fetch_memory_log.assert_awaited_once()
    assert result["success"] is True
    assert result["commits"][0]["hash"] == "abc123"


@pytest.mark.asyncio
async def test_kronos_anomalies_uses_direct_db_path_when_user_and_pool_exist(monkeypatch):
    import mnemos.core.lifecycle as lc
    from mnemos.core.config import _reset_settings_for_tests
    from mnemos.mcp.tools import kronos as mcp_kronos

    monkeypatch.setenv("MNEMOS_KRONOS_ENABLED", "true")
    _reset_settings_for_tests()
    monkeypatch.setattr(lc, "_pool", object())
    monkeypatch.setattr(mcp_kronos, "_rest_get", AsyncMock())
    monkeypatch.setattr(mcp_kronos, "detect_recall_anomalies", AsyncMock(return_value=[]))
    try:
        result = await mcp_kronos.tool_kronos_anomalies(
            "alice-ns",
            user=_alice(),
        )
    finally:
        monkeypatch.delenv("MNEMOS_KRONOS_ENABLED", raising=False)
        _reset_settings_for_tests()

    mcp_kronos._rest_get.assert_not_awaited()
    mcp_kronos.detect_recall_anomalies.assert_awaited_once()
    assert result == {"success": True, "namespace": "alice-ns", "count": 0, "anomalies": []}
