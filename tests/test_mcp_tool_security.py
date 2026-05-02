"""Security regressions for the canonical MCP tool surface.

The 20 user-callable tools share mnemos.mcp.tools.execute_tool as their
dispatcher. These tests pin the cross-tenant and input-hardening seams
that are easy to bypass when handlers call REST for one transport and
direct database helpers for another.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from mnemos.core.auth_context import UserContext


EXPECTED_USER_TOOLS = [
    "search_memories",
    "update_memory",
    "get_memory",
    "create_memory",
    "delete_memory",
    "list_memories",
    "get_stats",
    "kg_create_triple",
    "kg_search",
    "kg_timeline",
    "update_triple",
    "delete_triple",
    "bulk_create_memories",
    "log_memory",
    "branch_memory",
    "diff_memory_commits",
    "checkout_memory",
    "recommend_model",
    "pantheon_list_models",
    "pantheon_route_explain",
]


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://mnemos.local/v1/memories/mem_1")
    response = httpx.Response(
        status_code,
        request=request,
        json={"detail": "forbidden" if status_code == 403 else "not found"},
    )
    return httpx.HTTPStatusError(
        f"{status_code} error",
        request=request,
        response=response,
    )


def test_mcp_registry_user_callable_surface_is_exact():
    from mnemos.mcp.tools import TOOL_REGISTRY

    assert list(TOOL_REGISTRY) == EXPECTED_USER_TOOLS


def test_array_typed_tool_parameters_have_explicit_caps():
    from mnemos.mcp.tools import TOOL_REGISTRY

    array_params = []
    for tool_name, tool_info in TOOL_REGISTRY.items():
        for param_name, schema in tool_info["parameters"].items():
            if schema.get("type") == "array":
                array_params.append((tool_name, param_name, schema))

    assert array_params, "expected at least one array parameter on the MCP surface"
    for tool_name, param_name, schema in array_params:
        assert "maxItems" in schema, f"{tool_name}.{param_name} lacks maxItems"
        assert schema["maxItems"] <= 100


@pytest.mark.asyncio
async def test_execute_tool_binds_user_context_to_backend_headers():
    from mnemos.mcp.tools import (
        _backend_headers,
        current_mcp_backend_user_id,
        execute_tool,
    )
    from mnemos.mcp.tools import memory as mcp_memory

    async def fake_get(_path, params=None):
        return {
            "headers": _backend_headers(),
            "current_user_id": current_mcp_backend_user_id(),
        }

    with patch.object(mcp_memory, "_rest_get", new=AsyncMock(side_effect=fake_get)):
        result = await execute_tool("get_stats", {}, user=_alice())

    assert result["headers"]["X-MNEMOS-User-Id"] == "alice"
    assert result["current_user_id"] == "alice"
    assert current_mcp_backend_user_id() is None


@pytest.mark.asyncio
async def test_execute_tool_audit_log_uses_parameter_shape_not_raw_values(
    caplog,
    monkeypatch,
):
    import mnemos.mcp.tools as mcp_tools

    async def fake_handler(**_kwargs):
        return {"success": True}

    monkeypatch.setitem(
        mcp_tools.TOOL_REGISTRY["create_memory"],
        "handler",
        fake_handler,
    )
    caplog.set_level(logging.INFO, logger="mnemos.mcp.audit")

    await mcp_tools.execute_tool(
        "create_memory",
        {"content": "private raw memory", "metadata": {"secret": "raw"}},
        user=_alice(),
    )

    audit_text = caplog.text
    assert "mcp_tool_invocation" in audit_text
    assert "parameter_shape" in audit_text
    assert "private raw memory" not in audit_text
    assert "secret" not in audit_text


@pytest.mark.asyncio
async def test_execute_tool_logs_root_bypass(caplog, monkeypatch):
    import mnemos.mcp.tools as mcp_tools

    async def fake_handler(**_kwargs):
        return {"success": True}

    monkeypatch.setitem(
        mcp_tools.TOOL_REGISTRY["get_stats"],
        "handler",
        fake_handler,
    )
    caplog.set_level(logging.WARNING, logger="mnemos.mcp.audit")

    result = await mcp_tools.execute_tool("get_stats", {}, user=_root())

    assert result == {"success": True}
    assert "mcp_root_bypass" in caplog.text
    assert "admin" in caplog.text


@pytest.mark.asyncio
async def test_execute_tool_touches_read_and_write_rate_buckets(monkeypatch):
    import mnemos.mcp.tools as mcp_tools

    calls = []

    async def fake_rate_limit(*, tool_name, user_id, kind):
        calls.append((tool_name, user_id, kind))

    async def fake_handler(**_kwargs):
        return {"success": True}

    monkeypatch.setattr(mcp_tools, "_mcp_consult_rate_limit", fake_rate_limit)
    monkeypatch.setitem(
        mcp_tools.TOOL_REGISTRY["get_memory"],
        "handler",
        fake_handler,
    )
    monkeypatch.setitem(
        mcp_tools.TOOL_REGISTRY["create_memory"],
        "handler",
        fake_handler,
    )

    await mcp_tools.execute_tool("get_memory", {"memory_id": "mem_1"}, user=_alice())
    await mcp_tools.execute_tool("create_memory", {"content": "x"}, user=_alice())

    assert calls == [
        ("get_memory", "alice", "read"),
        ("create_memory", "alice", "write"),
    ]


@pytest.mark.asyncio
async def test_execute_tool_rejects_mismatched_existing_backend_context():
    from mnemos.mcp.tools import (
        execute_tool,
        reset_mcp_backend_context,
        set_mcp_backend_context,
    )
    from mnemos.mcp.tools import memory as mcp_memory

    tokens = set_mcp_backend_context(api_key="bob-api-key", user_id="bob")
    try:
        with patch.object(mcp_memory, "_rest_get", new=AsyncMock()) as mock_get:
            result = await execute_tool("get_stats", {}, user=_alice())
    finally:
        reset_mcp_backend_context(tokens)

    assert result == {"success": False, "error": "MCP caller context mismatch"}
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 404])
async def test_execute_tool_normalizes_forbidden_and_missing_error_shape(status_code):
    from mnemos.mcp.tools import execute_tool
    from mnemos.mcp.tools import memory as mcp_memory

    with patch.object(
        mcp_memory,
        "_rest_get",
        new=AsyncMock(side_effect=_http_status_error(status_code)),
    ):
        result = await execute_tool(
            "get_memory",
            {"memory_id": "mem_1234567890123_a1b2c3"},
            user=_alice(),
        )

    assert result == {"success": False, "error": "Resource not found"}


@pytest.mark.asyncio
async def test_rest_delete_raises_for_missing_or_forbidden_resources(monkeypatch):
    from mnemos.mcp.tools import _runtime

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def delete(self, url, headers=None):
            request = httpx.Request("DELETE", url, headers=headers)
            return httpx.Response(404, request=request, json={"detail": "not found"})

    monkeypatch.setattr(_runtime.httpx, "AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(httpx.HTTPStatusError):
        await _runtime._rest_delete("/v1/memories/mem_missing")


@pytest.mark.asyncio
async def test_bulk_create_memories_rejects_oversized_batches():
    from mnemos.mcp.tools._runtime import MCP_BULK_CREATE_MAX_ITEMS
    from mnemos.mcp.tools.memory import tool_bulk_create_memories

    oversized = [{"content": "x"} for _ in range(MCP_BULK_CREATE_MAX_ITEMS + 1)]
    with patch("mnemos.mcp.tools.memory._rest_post", new=AsyncMock()) as mock_post:
        with pytest.raises(ValueError, match="at most"):
            await tool_bulk_create_memories(oversized)
    mock_post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "func,args,patch_target",
    [
        ("tool_search_memories", {"query": "x", "limit": 501}, "mnemos.mcp.tools.memory._rest_post"),
        ("tool_list_memories", {"limit": 501}, "mnemos.mcp.tools.memory._rest_get"),
        ("tool_kg_search", {"limit": 501}, "mnemos.mcp.tools.kg._rest_get"),
        ("tool_kg_timeline", {"subject": "alice", "limit": 1001}, "mnemos.mcp.tools.kg._rest_get"),
        (
            "tool_log_memory",
            {"memory_id": "mem_1234567890123_a1b2c3", "limit": 501},
            "mnemos.mcp.tools.dag._rest_get",
        ),
    ],
)
async def test_result_limit_arguments_are_bounded(func, args, patch_target):
    from mnemos.mcp.tools import dag as mcp_dag
    from mnemos.mcp.tools import kg as mcp_kg
    from mnemos.mcp.tools import memory as mcp_memory

    handlers = {
        "tool_search_memories": mcp_memory.tool_search_memories,
        "tool_list_memories": mcp_memory.tool_list_memories,
        "tool_kg_search": mcp_kg.tool_kg_search,
        "tool_kg_timeline": mcp_kg.tool_kg_timeline,
        "tool_log_memory": mcp_dag.tool_log_memory,
    }

    with patch(patch_target, new=AsyncMock()) as mock_call:
        with pytest.raises(ValueError, match="between"):
            await handlers[func](**args)
    mock_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_dag_path_rejects_traversal_before_pool_access(monkeypatch):
    from mnemos.core import lifecycle as lc
    from mnemos.mcp.tools.dag import tool_checkout_memory

    monkeypatch.setattr(lc, "_pool", object())

    with pytest.raises(ValueError):
        await tool_checkout_memory("../../metrics", "abc123", user=_alice())


@pytest.mark.asyncio
async def test_branch_memory_direct_db_path_applies_per_tool_rate_guard(monkeypatch):
    from mnemos.core import lifecycle as lc
    from mnemos.mcp.tools import dag as mcp_dag

    monkeypatch.setattr(lc, "_pool", object())
    with patch.object(
        mcp_dag,
        "_mcp_enforce_write_rate_limit",
        new=AsyncMock(side_effect=PermissionError("rate limit exceeded for branch_memory")),
    ) as mock_guard:
        result = await mcp_dag.tool_branch_memory(
            "mem_1234567890123_a1b2c3",
            "feature",
            user=_alice(),
        )

    mock_guard.assert_awaited_once()
    assert result == {"success": False, "error": "rate limit exceeded for branch_memory"}


@pytest.mark.asyncio
async def test_fetch_memory_log_query_is_identity_scoped_for_non_root():
    from mnemos.db import mcp_repo

    class Conn:
        def __init__(self):
            self.calls = []

        async def fetch(self, sql, *args):
            self.calls.append((sql, args))
            return []

    conn = Conn()
    await mcp_repo.fetch_memory_log(
        conn,
        "mem_1234567890123_a1b2c3",
        "main",
        10,
        _alice(),
    )

    sql, args = conn.calls[0]
    assert "mv.owner_id=$4" in sql
    assert "mv.namespace = $5" in sql
    assert args == (
        "mem_1234567890123_a1b2c3",
        "main",
        10,
        "alice",
        "alice-ns",
    )
