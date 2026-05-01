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

from mnemos.mcp.tools._runtime import _safe_path_segment, _safe_path_value


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


def test_safe_path_segment_admits_federated_memory_ids():
    """Federation stores remote memories with id =
    ``fed:<peer>:<remote>``. The whitelist must admit colons so
    these IDs remain MCP-addressable. Codex round-3 caught that
    the v1 helper rejected them."""
    fed_id = "fed:alpha-prod:mem_1234567890123_a1b2c3"
    out = _safe_path_segment(fed_id, label="memory_id")
    # ``:`` is in safe= so it's not percent-encoded — keeps the
    # downstream URL parseable as a single path segment.
    assert out == fed_id


def test_safe_path_segment_admits_simple_peer_name():
    """Single-segment peer name in federated id."""
    assert (
        _safe_path_segment("fed:alpha:remote-id-1", label="memory_id")
        == "fed:alpha:remote-id-1"
    )


# ── _safe_path_value (looser helper for free-form fields) ─────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        # Plain entity name — encoded by quote(safe="").
        ("alice", "alice"),
        # Email-shaped entity.
        ("alice@acme.com", "alice%40acme.com"),
        # URL-shaped entity (https://example.com/page).
        # Slash inside is rejected; here we test a colon + dot
        # combination without slash.
        ("isbn:9780123456789", "isbn%3A9780123456789"),
        # Spaces preserved (encoded as %20).
        ("project alpha", "project%20alpha"),
        # Unicode — quote handles via UTF-8 encoding.
        ("café", "caf%C3%A9"),
    ],
)
def test_safe_path_value_admits_free_form_entities(value, expected):
    out = _safe_path_value(value, label="subject")
    assert out == expected


@pytest.mark.parametrize(
    "value",
    [
        "../../export",
        "..",
        "alice/../export",
        "alice\\..\\export",
        "subject?evil=1",
        "subject#frag",
        "alice\nlog-injection",
        "alice\x00null",
        "",
    ],
)
def test_safe_path_value_rejects_traversal_and_url_rewrite(value):
    with pytest.raises(ValueError):
        _safe_path_value(value, label="subject")


def test_safe_path_value_rejects_overlong():
    with pytest.raises(ValueError):
        _safe_path_value("a" * 1024, label="subject")


def test_safe_path_value_admits_single_dot():
    """A single ``.`` is harmless (URL paths can have it). Only the
    ``..`` traversal sequence is the threat."""
    assert _safe_path_value("v1.0", label="subject") == "v1.0"
    # quote() leaves single dots unencoded by default — that's fine.
    out = _safe_path_value("alice.bob", label="subject")
    assert out == "alice.bob"


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
async def test_knossos_t_get_drawer_rejects_traversal():
    """The Knossos MCP server (mnemos.tools.knossos_mcp) is a
    SEPARATE stdio MCP surface from mnemos.mcp.tools. Codex round-3
    caught that the round-25 hardening missed it. Knossos returns
    a {"error": ...} envelope rather than raising — pin that
    contract so callers see the rejection without the helper
    making an HTTP call."""
    from mnemos.tools.knossos_mcp import t_get_drawer

    with patch(
        "mnemos.tools.knossos_mcp._get",
        new=AsyncMock(),
    ) as mock_get:
        result = await t_get_drawer({"drawer_id": "../../metrics"})
    assert isinstance(result, dict)
    assert "error" in result
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_knossos_t_update_drawer_rejects_traversal():
    from mnemos.tools.knossos_mcp import t_update_drawer

    with patch(
        "mnemos.tools.knossos_mcp._patch",
        new=AsyncMock(),
    ) as mock_patch, patch(
        "mnemos.tools.knossos_mcp._get",
        new=AsyncMock(),
    ) as mock_get:
        result = await t_update_drawer(
            {"drawer_id": "../../metrics", "content": "x"},
        )
    assert isinstance(result, dict)
    assert "error" in result
    mock_patch.assert_not_awaited()
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_knossos_t_delete_drawer_rejects_traversal():
    from mnemos.tools.knossos_mcp import t_delete_drawer

    with patch(
        "mnemos.tools.knossos_mcp._delete",
        new=AsyncMock(),
    ) as mock_delete:
        result = await t_delete_drawer({"drawer_id": "../../metrics"})
    assert isinstance(result, dict)
    assert "error" in result
    mock_delete.assert_not_awaited()


# ── Federated IDs are admitted by every memory-id call site ────────────────


@pytest.mark.asyncio
async def test_tool_get_memory_admits_federated_id():
    """Federation IDs (fed:peer:remote) must round-trip through the
    helper — codex round-3 confirmed they're documented MNEMOS IDs
    and the helper used to reject them outright."""
    from mnemos.mcp.tools.memory import tool_get_memory

    with patch(
        "mnemos.mcp.tools.memory._rest_get",
        new=AsyncMock(return_value={"id": "fed:alpha:mem_1"}),
    ) as mock_get:
        result = await tool_get_memory("fed:alpha:mem_1")
    mock_get.assert_awaited_once()
    # The path must contain the unencoded colon — colons are valid
    # in path segments per RFC 3986; encoding them would change
    # what the server sees.
    called_path = mock_get.await_args.args[0]
    assert called_path == "/v1/memories/fed:alpha:mem_1"
    assert result == {"id": "fed:alpha:mem_1"}


@pytest.mark.asyncio
async def test_tool_kg_timeline_rejects_traversal_in_subject():
    """Codex round-4 (review-momtgp9j-e9u9zf) caught that
    ``/v1/kg/timeline/{subject}`` was unvalidated. ``../../export``
    would let httpx normalise the path back to ``/v1/export``,
    reaching the bulk export endpoint under the MCP server's
    bearer token."""
    from mnemos.mcp.tools.kg import tool_kg_timeline

    with patch(
        "mnemos.mcp.tools.kg._rest_get",
        new=AsyncMock(),
    ) as mock_get:
        with pytest.raises(ValueError):
            await tool_kg_timeline("../../export")
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_kg_timeline_admits_free_form_subject():
    """Email / URL / unicode entity names must round-trip safely."""
    from mnemos.mcp.tools.kg import tool_kg_timeline

    with patch(
        "mnemos.mcp.tools.kg._rest_get",
        new=AsyncMock(return_value={"events": []}),
    ) as mock_get:
        await tool_kg_timeline("alice@acme.com")
    mock_get.assert_awaited_once()
    called_path = mock_get.await_args.args[0]
    # `@` encoded so the URL parser doesn't treat alice as userinfo.
    assert called_path == "/v1/kg/timeline/alice%40acme.com"


@pytest.mark.asyncio
async def test_tool_update_triple_rejects_traversal():
    from mnemos.mcp.tools.kg import tool_update_triple

    with patch(
        "mnemos.mcp.tools.kg._rest_post",
        new=AsyncMock(),
    ) as mock_post:
        with pytest.raises(ValueError):
            await tool_update_triple("../../admin", subject="x")
    mock_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_delete_triple_rejects_traversal():
    from mnemos.mcp.tools.kg import tool_delete_triple

    with patch(
        "mnemos.mcp.tools.kg._rest_delete",
        new=AsyncMock(),
    ) as mock_delete:
        with pytest.raises(ValueError):
            await tool_delete_triple("../../admin")
    mock_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_knossos_t_kg_timeline_rejects_traversal():
    """Knossos has its OWN KG timeline tool (`t_kg_timeline`)
    that splices subject into the same /v1/kg/timeline path. Same
    fix shape as the canonical surface — return error envelope."""
    from mnemos.tools.knossos_mcp import t_kg_timeline

    with patch(
        "mnemos.tools.knossos_mcp._get",
        new=AsyncMock(),
    ) as mock_get:
        result = await t_kg_timeline({"subject": "../../export"})
    assert isinstance(result, dict)
    assert "error" in result
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
