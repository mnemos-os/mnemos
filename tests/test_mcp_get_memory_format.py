"""MCP ``get_memory`` tool with optional ``format`` parameter (v3.6 §2.5 #3).

The HTTP API exposes prose / dense narrate variants via Accept-header
content negotiation on ``GET /v1/memories/{id}`` (v4.2.0a14 round-12).
This test file pins the MCP-tool surface that lets stdio / HTTP-SSE
MCP clients reach those same variants without parsing through JSON
or running a parallel HTTP call.

Default behaviour (no ``format`` argument) MUST stay byte-identical
to the legacy JSON response — connectors that don't know about the
new parameter cannot regress.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mnemos.mcp.tools.memory import tool_get_memory


@pytest.mark.asyncio
async def test_default_returns_json_unchanged():
    """Without ``format``, the tool returns whatever ``_rest_get``
    returns — the legacy JSON memory object."""
    expected = {"id": "m1", "content": "raw", "category": "facts"}
    with patch(
        "mnemos.mcp.tools.memory._rest_get",
        new=AsyncMock(return_value=expected),
    ) as mock_get:
        result = await tool_get_memory("m1")
    mock_get.assert_awaited_once_with("/v1/memories/m1")
    assert result == expected


@pytest.mark.asyncio
async def test_format_prose_uses_text_plain_accept():
    """``format='prose'`` must dispatch to ``_rest_get_text`` with
    Accept: text/plain so the HTTP API negotiates the prose path."""
    with patch(
        "mnemos.mcp.tools.memory._rest_get_text",
        new=AsyncMock(return_value="alice joined acme. Facts: signed-offer."),
    ) as mock_get_text:
        result = await tool_get_memory("m1", format="prose")
    mock_get_text.assert_awaited_once_with(
        "/v1/memories/m1", accept="text/plain",
    )
    assert result == {
        "memory_id": "m1",
        "format": "prose",
        "content": "alice joined acme. Facts: signed-offer.",
    }


@pytest.mark.asyncio
async def test_format_dense_uses_apollo_dense_accept():
    """``format='dense'`` must dispatch to ``_rest_get_text`` with
    Accept: application/x-apollo-dense."""
    with patch(
        "mnemos.mcp.tools.memory._rest_get_text",
        new=AsyncMock(return_value="AAPL:100@150.25/175.50:tech"),
    ) as mock_get_text:
        result = await tool_get_memory("m1", format="dense")
    mock_get_text.assert_awaited_once_with(
        "/v1/memories/m1", accept="application/x-apollo-dense",
    )
    assert result == {
        "memory_id": "m1",
        "format": "dense",
        "content": "AAPL:100@150.25/175.50:tech",
    }


@pytest.mark.asyncio
async def test_format_invalid_value_rejected():
    """Anything other than 'prose' or 'dense' must raise a ValueError
    rather than silently fall through to JSON or send a wrong Accept."""
    with patch(
        "mnemos.mcp.tools.memory._rest_get_text",
        new=AsyncMock(),
    ) as mock_get_text, patch(
        "mnemos.mcp.tools.memory._rest_get",
        new=AsyncMock(),
    ) as mock_get:
        with pytest.raises(ValueError, match="prose|dense"):
            await tool_get_memory("m1", format="json")
    mock_get_text.assert_not_awaited()
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_format_none_explicit_routes_to_json():
    """``format=None`` is the same as omitting the parameter — JSON path."""
    expected = {"id": "m1"}
    with patch(
        "mnemos.mcp.tools.memory._rest_get",
        new=AsyncMock(return_value=expected),
    ) as mock_get, patch(
        "mnemos.mcp.tools.memory._rest_get_text",
        new=AsyncMock(),
    ) as mock_get_text:
        result = await tool_get_memory("m1", format=None)
    mock_get.assert_awaited_once_with("/v1/memories/m1")
    mock_get_text.assert_not_awaited()
    assert result == expected


# ── Tool registry metadata ─────────────────────────────────────────────────


def test_get_memory_tool_advertises_format_parameter():
    """The MCP tool registry must expose the ``format`` parameter
    so clients calling /v1/mcp/discovery (or stdio --print-schema)
    see it. Without this, agent surfaces won't know to offer the
    prose/dense affordance."""
    from mnemos.mcp.tools.memory import TOOLS

    schema = TOOLS["get_memory"]
    parameters = schema["parameters"]
    assert "format" in parameters, (
        "get_memory tool schema must advertise the format parameter"
    )
    assert parameters["format"]["type"] == "string"
    assert set(parameters["format"].get("enum", [])) == {"prose", "dense"}


def test_get_memory_tool_format_is_optional():
    """``format`` must NOT be a required parameter — default JSON
    response is the legacy contract."""
    from mnemos.mcp.tools.memory import TOOLS

    schema = TOOLS["get_memory"]
    required = schema.get("required", [])
    assert "format" not in required
    assert "memory_id" in required
