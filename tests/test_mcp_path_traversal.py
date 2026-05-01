"""Regression tests for the path-traversal hardening on MCP tools.

Codex round-2 of the round-24 thread caught a real path-traversal
vulnerability: ``tool_get_memory`` and other MCP tools that
interpolate caller-controlled IDs (memory_id, commit_hash) into
REST paths could escape the ``/v1/memories/`` prefix when fed
values containing ``..`` segments. With ``_rest_get_text`` returning
raw response bodies, that became an exfiltration vector for any
text-returning same-origin endpoint (e.g. ``/metrics``).

The fix is ``_safe_path_segment`` — type-check + length-bound +
character whitelist + URL-encode at every memory_id / commit_hash
splice site. These tests pin both the helper itself AND a sample
of the call sites, so a future addition that splices a fresh
caller-controlled value without going through the helper trips
this regression.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mnemos.mcp.tools._runtime import _safe_path_segment


# ── Helper validation contract ─────────────────────────────────────────────


def test_safe_path_segment_admits_canonical_memory_id():
    """The standard ``mem_<digits>_<6 hex>`` shape passes
    unchanged."""
    out = _safe_path_segment("mem_1234567890123_a1b2c3", label="memory_id")
    assert out == "mem_1234567890123_a1b2c3"


def test_safe_path_segment_admits_branch_and_commit_hashes():
    """Hex-ish identifiers used by the DAG path."""
    assert _safe_path_segment("abc123def456", label="commit_hash") == "abc123def456"
    assert _safe_path_segment("main", label="branch") == "main"


@pytest.mark.parametrize(
    "value",
    [
        "../../etc/passwd",
        "..",
        "../admin",
        "mem_1/../metrics",
        "mem_1/extra",
        "mem_1?param=evil",
        "mem_1#frag",
        "mem_1 with spaces",
        "../../../../../../etc/shadow",
        "mem_1/../../v1/users",
        "mem_1\\..\\admin",   # Windows-style separator
        "mem_1%2F..%2Fmetrics",  # already-encoded slash; whitelist still bans %
        "",
        ".",
        "./mem_1",
        "//metrics",
        "mem_\x00",  # NUL byte
        "mem_\nlog-injection",
    ],
)
def test_safe_path_segment_rejects_traversal_payloads(value):
    """Every adversarial input must raise ValueError before any
    HTTP request fires."""
    with pytest.raises(ValueError):
        _safe_path_segment(value, label="memory_id")


def test_safe_path_segment_rejects_non_string():
    with pytest.raises(ValueError):
        _safe_path_segment(123, label="memory_id")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _safe_path_segment(None, label="memory_id")  # type: ignore[arg-type]


def test_safe_path_segment_enforces_length_bound():
    """A 1024-byte ID is rejected — the 128-char cap blocks
    request-smuggling tricks based on hugely long path segments."""
    with pytest.raises(ValueError):
        _safe_path_segment("a" * 1024, label="memory_id")


def test_safe_path_segment_admits_length_at_boundary():
    """At the boundary (128 chars) the helper passes."""
    boundary = "a" * 128
    out = _safe_path_segment(boundary, label="memory_id")
    assert out == boundary


# ── Call-site coverage: tool_get_memory ────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_get_memory_rejects_path_traversal_default_path():
    """The legacy JSON path (no ``format``) must reject traversal
    too — codex called this out: 'consider applying the same
    validation/encoding to the other MCP memory-id tools'."""
    from mnemos.mcp.tools.memory import tool_get_memory

    with patch(
        "mnemos.mcp.tools.memory._rest_get",
        new=AsyncMock(),
    ) as mock_get:
        with pytest.raises(ValueError):
            await tool_get_memory("../../metrics")
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_get_memory_rejects_path_traversal_text_format():
    """The new text format path is the explicit codex finding."""
    from mnemos.mcp.tools.memory import tool_get_memory

    with patch(
        "mnemos.mcp.tools.memory._rest_get_text",
        new=AsyncMock(),
    ) as mock_get_text:
        with pytest.raises(ValueError):
            await tool_get_memory("../../metrics", format="prose")
        with pytest.raises(ValueError):
            await tool_get_memory("../../metrics", format="dense")
    mock_get_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_update_memory_rejects_path_traversal():
    from mnemos.mcp.tools.memory import tool_update_memory

    with patch(
        "mnemos.mcp.tools.memory._rest_post",
        new=AsyncMock(),
    ) as mock_post:
        with pytest.raises(ValueError):
            await tool_update_memory("../../admin", content="x")
    mock_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_delete_memory_rejects_path_traversal():
    from mnemos.mcp.tools.memory import tool_delete_memory

    with patch(
        "mnemos.mcp.tools.memory._rest_delete",
        new=AsyncMock(),
    ) as mock_delete:
        with pytest.raises(ValueError):
            await tool_delete_memory("../../admin")
    mock_delete.assert_not_awaited()


# ── Call-site coverage: DAG tools (commit_hash + memory_id) ────────────────


@pytest.mark.asyncio
async def test_tool_log_memory_rejects_path_traversal():
    from mnemos.mcp.tools.dag import tool_log_memory

    with patch(
        "mnemos.mcp.tools.dag._rest_get",
        new=AsyncMock(),
    ) as mock_get:
        with pytest.raises(ValueError):
            await tool_log_memory("../../metrics", branch="main")
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_branch_memory_rejects_path_traversal():
    from mnemos.mcp.tools.dag import tool_branch_memory

    with patch(
        "mnemos.mcp.tools.dag._rest_post",
        new=AsyncMock(),
    ) as mock_post:
        with pytest.raises(ValueError):
            await tool_branch_memory("../../admin", name="x")
    mock_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_checkout_memory_rejects_traversal_in_memory_id():
    from mnemos.mcp.tools.dag import tool_checkout_memory

    with patch(
        "mnemos.mcp.tools.dag._rest_get",
        new=AsyncMock(),
    ) as mock_get:
        with pytest.raises(ValueError):
            await tool_checkout_memory("../../metrics", commit_hash="abc123")
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_checkout_memory_rejects_traversal_in_commit_hash():
    """commit_hash is also caller-controlled and spliced into the
    path — must be validated."""
    from mnemos.mcp.tools.dag import tool_checkout_memory

    with patch(
        "mnemos.mcp.tools.dag._rest_get",
        new=AsyncMock(),
    ) as mock_get:
        with pytest.raises(ValueError):
            await tool_checkout_memory("mem_1234567890123_a1b2c3", commit_hash="../../metrics")
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_diff_memory_commits_rejects_traversal_in_either_commit():
    from mnemos.mcp.tools.dag import tool_diff_memory_commits

    with patch(
        "mnemos.mcp.tools.dag._rest_get",
        new=AsyncMock(),
    ) as mock_get:
        with pytest.raises(ValueError):
            await tool_diff_memory_commits(
                "mem_1234567890123_a1b2c3",
                commit_a="../../metrics",
                commit_b="abc123",
            )
        with pytest.raises(ValueError):
            await tool_diff_memory_commits(
                "mem_1234567890123_a1b2c3",
                commit_a="abc123",
                commit_b="../../metrics",
            )
    mock_get.assert_not_awaited()
