"""Regression for the per-connector MNEMOS_DEFAULT_NAMESPACE wiring.

The connector gallery (claude-code.md, cursor.md, codex-cli.md,
continue-dev.md, cline.md) documents ``MNEMOS_DEFAULT_NAMESPACE`` as
the per-MCP-server isolation knob. Two MCP entries with different
env vars should write/read distinct namespace scopes even when they
share the same backing API key.

Codex round-2 audit (2026-05-01) flagged that this was a doc-only
promise — the MCP create_memory / search_memories / list_memories /
bulk_create_memories handlers never read the env var. Two entries
with the same token would have written to the same namespace.

These tests pin that:

  * unset env → no ``namespace`` key in the request body / params
    (server falls through to the API-key-resolved default).
  * env="work" → ``namespace=work`` in every relevant call.
  * bulk path: per-row caller-supplied namespace wins over the env.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from mnemos.mcp.tools import memory as mcp_memory


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("MNEMOS_DEFAULT_NAMESPACE", raising=False)
    yield


def test_create_no_env_omits_namespace():
    fake = AsyncMock(return_value={"id": "mem_x"})
    with patch.object(mcp_memory, "_rest_post", fake):
        asyncio.run(
            mcp_memory.tool_create_memory(content="hello", category="facts")
        )
    body = fake.call_args.args[1]
    assert "namespace" not in body, (
        f"unset MNEMOS_DEFAULT_NAMESPACE must NOT inject a namespace "
        f"key — server resolves from the API key. body: {body}"
    )


def test_create_with_env_stamps_namespace(monkeypatch):
    monkeypatch.setenv("MNEMOS_DEFAULT_NAMESPACE", "work")
    fake = AsyncMock(return_value={"id": "mem_x"})
    with patch.object(mcp_memory, "_rest_post", fake):
        asyncio.run(
            mcp_memory.tool_create_memory(content="hello", category="facts")
        )
    body = fake.call_args.args[1]
    assert body.get("namespace") == "work", (
        f"MNEMOS_DEFAULT_NAMESPACE=work must stamp namespace=work on "
        f"the create body. body: {body}"
    )


def test_search_with_env_stamps_namespace(monkeypatch):
    monkeypatch.setenv("MNEMOS_DEFAULT_NAMESPACE", "personal")
    fake = AsyncMock(return_value={"results": []})
    with patch.object(mcp_memory, "_rest_post", fake):
        asyncio.run(mcp_memory.tool_search_memories(query="anything"))
    body = fake.call_args.args[1]
    assert body.get("namespace") == "personal"


def test_list_with_env_stamps_namespace(monkeypatch):
    monkeypatch.setenv("MNEMOS_DEFAULT_NAMESPACE", "sandbox")
    fake = AsyncMock(return_value={"memories": []})
    with patch.object(mcp_memory, "_rest_get", fake):
        asyncio.run(mcp_memory.tool_list_memories())
    params = fake.call_args.kwargs.get("params", {})
    assert params.get("namespace") == "sandbox"


def test_bulk_env_wins_over_per_row_namespace(monkeypatch):
    """Codex round-3 audit (2026-05-01): when MNEMOS_DEFAULT_NAMESPACE
    is set, it must override any per-row namespace the caller
    supplies. Otherwise the env-stamp boundary is bypassable just by
    including ``"namespace": ...`` in each bulk row.

    Power users who need cross-namespace bulk creation hit the REST
    API directly OR run the connector without the env stamp.
    """
    monkeypatch.setenv("MNEMOS_DEFAULT_NAMESPACE", "work")
    fake = AsyncMock(return_value={"created": 2})
    payload = [
        {"content": "row 1", "category": "facts"},
        {"content": "row 2", "category": "facts", "namespace": "tries-to-escape"},
    ]
    with patch.object(mcp_memory, "_rest_post", fake):
        asyncio.run(mcp_memory.tool_bulk_create_memories(memories=payload))
    body = fake.call_args.args[1]
    rows = body["memories"]
    assert rows[0]["namespace"] == "work"
    assert rows[1]["namespace"] == "work", (
        "per-row namespace MUST be overwritten by the env stamp; "
        f"got: {rows[1]['namespace']}"
    )


def test_bulk_no_env_preserves_per_row_namespace(monkeypatch):
    """Inverse: when env is unset, per-row namespace is preserved
    so power users can hit cross-namespace bulk creation."""
    monkeypatch.delenv("MNEMOS_DEFAULT_NAMESPACE", raising=False)
    fake = AsyncMock(return_value={"created": 2})
    payload = [
        {"content": "row 1", "category": "facts", "namespace": "alpha"},
        {"content": "row 2", "category": "facts", "namespace": "beta"},
    ]
    with patch.object(mcp_memory, "_rest_post", fake):
        asyncio.run(mcp_memory.tool_bulk_create_memories(memories=payload))
    body = fake.call_args.args[1]
    rows = body["memories"]
    assert rows[0]["namespace"] == "alpha"
    assert rows[1]["namespace"] == "beta"


def test_blank_env_treated_as_unset(monkeypatch):
    monkeypatch.setenv("MNEMOS_DEFAULT_NAMESPACE", "   ")
    fake = AsyncMock(return_value={"id": "mem_x"})
    with patch.object(mcp_memory, "_rest_post", fake):
        asyncio.run(
            mcp_memory.tool_create_memory(content="hello", category="facts")
        )
    body = fake.call_args.args[1]
    assert "namespace" not in body, (
        f"whitespace-only env var must NOT stamp; got body: {body}"
    )
