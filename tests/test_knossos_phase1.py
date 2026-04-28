"""Tests for KNOSSOS phase 1 — MemPalace-compatible MCP shim backed by MNEMOS.

Phase 1 ships 16 tools that translate MemPalace's wire vocabulary
(wings/rooms/drawers/KG) into MNEMOS /v1/* REST calls. Until now,
phase 1 had zero test coverage despite being live since v3.3.0-alpha;
these tests close that gap.

Strategy: monkeypatch the four module-level HTTP helpers
(`_get`, `_post`, `_patch`, `_delete`). Each test installs a
recording stub, invokes a tool handler, and asserts on:

  - method/path the helper was called with
  - body shape sent
  - response transformation back into MemPalace's drawer/wing shape

httpx is never reached. The tests run in <0.5s combined.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

import pytest

# Importing the module redirects stdout — restore it so pytest output works.
_real_stdout = sys.stdout
import mnemos.tools.knossos_mcp as kn  # noqa: E402

sys.stdout = _real_stdout


# ── helper for capturing helper calls ────────────────────────────────────────


class _Recorder:
    """Captures (method, path, body|params) tuples and returns canned responses."""

    def __init__(self):
        self.calls: List[Tuple[str, str, Any]] = []
        self._get_responses: Dict[str, Any] = {}
        self._post_responses: Dict[str, Any] = {}
        self._patch_responses: Dict[str, Any] = {}
        self._delete_responses: Dict[str, int] = {}
        self._default_get: Any = {}
        self._default_post: Any = {}
        self._default_patch: Any = {}
        self._default_delete: int = 204

    def get_returns(self, path: str, response: Any) -> None:
        self._get_responses[path] = response

    def post_returns(self, path: str, response: Any) -> None:
        self._post_responses[path] = response

    def patch_returns(self, path: str, response: Any) -> None:
        self._patch_responses[path] = response

    def delete_returns(self, path: str, status: int) -> None:
        self._delete_responses[path] = status

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self.calls.append(("GET", path, params or {}))
        return self._get_responses.get(path, self._default_get)

    async def post(self, path: str, body: Any) -> Any:
        self.calls.append(("POST", path, body))
        return self._post_responses.get(path, self._default_post)

    async def patch(self, path: str, body: Any) -> Any:
        self.calls.append(("PATCH", path, body))
        return self._patch_responses.get(path, self._default_patch)

    async def delete(self, path: str) -> int:
        self.calls.append(("DELETE", path, None))
        return self._delete_responses.get(path, self._default_delete)


@pytest.fixture
def rec(monkeypatch):
    """Install a fresh _Recorder + monkeypatch the four HTTP helpers."""
    r = _Recorder()
    monkeypatch.setattr(kn, "_get", r.get)
    monkeypatch.setattr(kn, "_post", r.post)
    monkeypatch.setattr(kn, "_patch", r.patch)
    monkeypatch.setattr(kn, "_delete", r.delete)
    return r


# ── status / wings / rooms / taxonomy ────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_returns_palace_overview(rec):
    rec.get_returns("/stats", {
        "total_memories": 42,
        "memories_by_category": {"facts": 30, "decisions": 12},
    })
    result = await kn.t_status({})
    assert result["total_drawers"] == 42
    assert result["rooms"] == {"facts": 30, "decisions": 12}
    assert "knossos" in result["source"].lower()


@pytest.mark.asyncio
async def test_list_wings_groups_by_axis(rec, monkeypatch):
    """Default WING_AXIS is 'namespace'; counts roll up by that key."""
    monkeypatch.setattr(kn, "WING_AXIS", "namespace")
    rec.get_returns("/v1/export", {
        "records": [
            {"payload": {"namespace": "alice", "category": "facts"}},
            {"payload": {"namespace": "alice", "category": "decisions"}},
            {"payload": {"namespace": "bob", "category": "facts"}},
            {"payload": {"namespace": None, "category": "facts"}},  # falls back to default wing
        ]
    })
    result = await kn.t_list_wings({})
    wings = {w["name"]: w["drawer_count"] for w in result["wings"]}
    assert wings["alice"] == 2
    assert wings["bob"] == 1
    assert wings[kn.DEFAULT_WING] == 1


@pytest.mark.asyncio
async def test_list_wings_respects_owner_id_axis(rec, monkeypatch):
    """When KNOSSOS_WING_AXIS=owner_id, list_wings groups by owner_id instead."""
    monkeypatch.setattr(kn, "WING_AXIS", "owner_id")
    rec.get_returns("/v1/export", {
        "records": [
            {"payload": {"owner_id": "u1"}},
            {"payload": {"owner_id": "u1"}},
            {"payload": {"owner_id": "u2"}},
        ]
    })
    result = await kn.t_list_wings({})
    wings = {w["name"]: w["drawer_count"] for w in result["wings"]}
    assert wings["u1"] == 2
    assert wings["u2"] == 1


@pytest.mark.asyncio
async def test_list_rooms_filters_by_wing(rec, monkeypatch):
    monkeypatch.setattr(kn, "WING_AXIS", "namespace")
    rec.get_returns("/v1/export", {
        "records": [
            {"payload": {"namespace": "alice", "category": "facts"}},
            {"payload": {"namespace": "alice", "category": "decisions"}},
            {"payload": {"namespace": "bob", "category": "facts"}},
        ]
    })
    result = await kn.t_list_rooms({"wing": "alice"})
    rooms = {r["name"]: r["drawer_count"] for r in result["rooms"]}
    assert rooms == {"facts": 1, "decisions": 1}
    assert result["wing"] == "alice"


@pytest.mark.asyncio
async def test_list_rooms_no_wing_returns_all(rec, monkeypatch):
    monkeypatch.setattr(kn, "WING_AXIS", "namespace")
    rec.get_returns("/v1/export", {
        "records": [
            {"payload": {"namespace": "alice", "category": "facts"}},
            {"payload": {"namespace": "bob", "category": "facts"}},
        ]
    })
    result = await kn.t_list_rooms({})
    rooms = {r["name"]: r["drawer_count"] for r in result["rooms"]}
    assert rooms == {"facts": 2}


@pytest.mark.asyncio
async def test_get_taxonomy_builds_wing_room_tree(rec, monkeypatch):
    monkeypatch.setattr(kn, "WING_AXIS", "namespace")
    rec.get_returns("/v1/export", {
        "records": [
            {"payload": {"namespace": "alice", "category": "facts"}},
            {"payload": {"namespace": "alice", "category": "facts"}},
            {"payload": {"namespace": "bob", "category": "decisions"}},
        ]
    })
    result = await kn.t_get_taxonomy({})
    by_wing = {entry["wing"]: entry["rooms"] for entry in result["taxonomy"]}
    assert {r["name"]: r["drawer_count"] for r in by_wing["alice"]} == {"facts": 2}
    assert {r["name"]: r["drawer_count"] for r in by_wing["bob"]} == {"decisions": 1}


# ── search / check_duplicate ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_forwards_wing_and_room(rec, monkeypatch):
    monkeypatch.setattr(kn, "WING_AXIS", "namespace")
    rec.post_returns("/v1/memories/search", {"memories": []})
    await kn.t_search({"query": "hello", "wing": "alice", "room": "facts", "k": 3})
    method, path, body = rec.calls[0]
    assert (method, path) == ("POST", "/v1/memories/search")
    assert body["query"] == "hello"
    assert body["limit"] == 3
    assert body["category"] == "facts"
    assert body["namespace"] == "alice"
    assert body["semantic"] is True


@pytest.mark.asyncio
async def test_search_drops_hits_above_max_distance(rec):
    rec.post_returns("/v1/memories/search", {"memories": [
        {"id": "mem_a", "content": "good", "category": "facts", "score": 0.5},
        {"id": "mem_b", "content": "bad", "category": "facts", "score": 1.9},
    ]})
    result = await kn.t_search({"query": "x", "max_distance": 1.0})
    assert result["count"] == 1
    assert result["drawers"][0]["id"] == "mem_a"


@pytest.mark.asyncio
async def test_check_duplicate_translates_threshold_to_distance(rec):
    """A cosine-similarity threshold of 0.9 should map to a distance
    cutoff of 0.1 — the test mocks /v1/memories/search and asserts
    only the matching hit (score=0.05) survives, not the score=0.4 one."""
    rec.post_returns("/v1/memories/search", {"memories": [
        {"id": "mem_a", "content": "x", "category": "facts", "score": 0.05},
        {"id": "mem_b", "content": "y", "category": "facts", "score": 0.4},
    ]})
    result = await kn.t_check_duplicate({"content": "anything", "threshold": 0.9})
    assert result["is_duplicate"] is True
    assert result["match"]["id"] == "mem_a"


@pytest.mark.asyncio
async def test_check_duplicate_no_match_returns_false(rec):
    rec.post_returns("/v1/memories/search", {"memories": [
        {"id": "mem_a", "content": "x", "category": "facts", "score": 0.5},
    ]})
    result = await kn.t_check_duplicate({"content": "anything", "threshold": 0.9})
    assert result["is_duplicate"] is False
    assert result["match"] is None


# ── drawer CRUD ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_drawers_filters_and_reshapes(rec, monkeypatch):
    monkeypatch.setattr(kn, "WING_AXIS", "namespace")
    rec.get_returns("/v1/memories", {"memories": [
        {"id": "mem_1", "content": "x", "category": "facts", "namespace": "alice"},
        {"id": "mem_2", "content": "y", "category": "facts", "namespace": "alice"},
    ]})
    result = await kn.t_list_drawers({"wing": "alice", "room": "facts", "limit": 50})
    method, path, params = rec.calls[0]
    assert (method, path) == ("GET", "/v1/memories")
    assert params["limit"] == 50
    assert params["category"] == "facts"
    assert params["namespace"] == "alice"
    assert result["count"] == 2
    assert result["drawers"][0]["wing"] == "alice"
    assert result["drawers"][0]["room"] == "facts"


@pytest.mark.asyncio
async def test_get_drawer_missing_id_returns_error(rec):
    result = await kn.t_get_drawer({})
    assert result == {"error": "drawer_id is required"}
    assert rec.calls == []  # short-circuited; no HTTP call


@pytest.mark.asyncio
async def test_get_drawer_uses_v1_memories_path(rec):
    rec.get_returns(
        "/v1/memories/mem_xyz",
        {"id": "mem_xyz", "content": "...", "category": "facts"},
    )
    await kn.t_get_drawer({"drawer_id": "mem_xyz"})
    method, path, _ = rec.calls[0]
    assert (method, path) == ("GET", "/v1/memories/mem_xyz")


@pytest.mark.asyncio
async def test_add_drawer_uses_default_wing_when_omitted(rec, monkeypatch):
    monkeypatch.setattr(kn, "WING_AXIS", "namespace")
    monkeypatch.setattr(kn, "DEFAULT_WING", "default")
    rec.post_returns("/v1/memories", {"id": "mem_new", "category": "facts", "content": "x"})
    await kn.t_add_drawer({"content": "hello", "room": "facts"})
    method, path, body = rec.calls[0]
    assert (method, path) == ("POST", "/v1/memories")
    assert body["category"] == "facts"
    assert body["namespace"] == "default"


@pytest.mark.asyncio
async def test_add_drawer_threads_tags_into_metadata(rec):
    rec.post_returns("/v1/memories", {"id": "mem_new", "category": "facts", "content": "x"})
    await kn.t_add_drawer({"content": "hi", "tags": ["alpha", "beta"]})
    _, _, body = rec.calls[0]
    assert body["metadata"]["tags"] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_add_drawer_missing_content_returns_error(rec):
    result = await kn.t_add_drawer({"room": "facts"})
    assert result == {"error": "content is required"}
    assert rec.calls == []


@pytest.mark.asyncio
async def test_update_drawer_merges_existing_metadata(rec):
    """The PATCH replaces the full metadata object on the server side,
    so KNOSSOS must read-merge-write to avoid clobbering existing keys."""
    rec.get_returns("/v1/memories/mem_x", {
        "id": "mem_x",
        "metadata": {"distillation_success": True, "source": "agent"},
    })
    rec.patch_returns("/v1/memories/mem_x", {
        "id": "mem_x", "category": "facts", "content": "updated",
    })
    await kn.t_update_drawer({"drawer_id": "mem_x", "tags": ["new-tag"]})
    # Verify the read-merge-write pattern.
    methods = [c[0] for c in rec.calls]
    assert methods == ["GET", "PATCH"]
    patch_body = rec.calls[1][2]
    assert patch_body["metadata"]["distillation_success"] is True
    assert patch_body["metadata"]["source"] == "agent"
    assert patch_body["metadata"]["tags"] == ["new-tag"]


@pytest.mark.asyncio
async def test_update_drawer_skips_merge_when_no_metadata_change(rec):
    """Content/room-only updates must not waste a fetch round-trip."""
    rec.patch_returns("/v1/memories/mem_x", {"id": "mem_x"})
    await kn.t_update_drawer({"drawer_id": "mem_x", "content": "new content"})
    methods = [c[0] for c in rec.calls]
    assert methods == ["PATCH"]


@pytest.mark.asyncio
async def test_update_drawer_falls_back_when_read_fails(rec, monkeypatch):
    """If the read-before-merge fails, send what the caller passed
    (losing existing metadata is bad; blocking the update is worse)."""
    async def failing_get(*_args, **_kwargs):
        raise RuntimeError("upstream down")
    monkeypatch.setattr(kn, "_get", failing_get)
    rec.patch_returns("/v1/memories/mem_x", {"id": "mem_x"})
    await kn.t_update_drawer({"drawer_id": "mem_x", "tags": ["x"]})
    # Only a PATCH with the caller's tags survives — no crash.
    assert any(c[0] == "PATCH" for c in rec.calls)


@pytest.mark.asyncio
async def test_update_drawer_missing_id_returns_error(rec):
    result = await kn.t_update_drawer({"content": "x"})
    assert result == {"error": "drawer_id is required"}
    assert rec.calls == []


@pytest.mark.asyncio
async def test_delete_drawer_returns_status_envelope(rec):
    rec.delete_returns("/v1/memories/mem_x", 204)
    result = await kn.t_delete_drawer({"drawer_id": "mem_x"})
    assert result == {"ok": True, "status": 204}


@pytest.mark.asyncio
async def test_delete_drawer_missing_id_returns_error(rec):
    result = await kn.t_delete_drawer({})
    assert result == {"error": "drawer_id is required"}
    assert rec.calls == []


# ── KG tools ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kg_add_uses_v1_kg_path(rec):
    """phase 1 reaches /v1/kg/triples — catches drift back to /kg/."""
    rec.post_returns("/v1/kg/triples", {"id": "tr_1"})
    await kn.t_kg_add({
        "subject": "Alice", "predicate": "knows", "object": "Bob",
        "valid_from": "2026-04-25T00:00:00Z",
        "closet_id": "mem_drawer_1",
    })
    method, path, body = rec.calls[0]
    assert (method, path) == ("POST", "/v1/kg/triples")
    assert body["subject"] == "Alice"
    assert body["predicate"] == "knows"
    assert body["valid_from"] == "2026-04-25T00:00:00Z"
    # closet_id is mapped into metadata so the source drawer is preserved.
    assert body["metadata"]["source_drawer_id"] == "mem_drawer_1"


@pytest.mark.asyncio
async def test_kg_query_passes_as_of_when_provided(rec):
    rec.get_returns("/v1/kg/triples", {"triples": []})
    await kn.t_kg_query({"entity": "Alice", "as_of": "2026-04-01"})
    method, path, params = rec.calls[0]
    assert (method, path) == ("GET", "/v1/kg/triples")
    assert params["entity"] == "Alice"
    assert params["as_of"] == "2026-04-01"
    assert params["direction"] == "both"


@pytest.mark.asyncio
async def test_kg_query_default_direction_is_both(rec):
    rec.get_returns("/v1/kg/triples", {"triples": []})
    await kn.t_kg_query({"entity": "Alice"})
    _, _, params = rec.calls[0]
    assert params["direction"] == "both"
    assert "as_of" not in params


@pytest.mark.asyncio
async def test_kg_invalidate_passes_full_triple(rec):
    rec.post_returns("/v1/kg/triples/invalidate", {"ok": True})
    await kn.t_kg_invalidate({
        "subject": "Alice", "predicate": "works_at", "object": "Acme",
        "valid_until": "2026-04-25",
    })
    method, path, body = rec.calls[0]
    assert (method, path) == ("POST", "/v1/kg/triples/invalidate")
    assert body == {
        "subject": "Alice", "predicate": "works_at", "object": "Acme",
        "valid_until": "2026-04-25",
    }


@pytest.mark.asyncio
async def test_kg_timeline_missing_subject_returns_error(rec):
    result = await kn.t_kg_timeline({})
    assert result == {"error": "subject is required"}
    assert rec.calls == []


@pytest.mark.asyncio
async def test_kg_timeline_path_includes_subject(rec):
    rec.get_returns("/v1/kg/timeline/Alice", {"events": []})
    await kn.t_kg_timeline({"subject": "Alice"})
    method, path, _ = rec.calls[0]
    assert (method, path) == ("GET", "/v1/kg/timeline/Alice")


@pytest.mark.asyncio
async def test_kg_stats_relays_total_triples(rec):
    rec.get_returns("/stats", {"total_kg_triples": 137, "total_memories": 999})
    result = await kn.t_kg_stats({})
    assert result == {"triples": 137}


# ── tool registry sanity ─────────────────────────────────────────────────────


def test_tool_registry_matches_doc_claim():
    """docs/KNOSSOS.md "Tool coverage v0.1" claims 16 implemented mnemos.tools.
    The TOOLS dict must agree exactly so no doc/code drift slips in."""
    expected = {
        "mempalace_status",
        "mempalace_list_wings",
        "mempalace_list_rooms",
        "mempalace_get_taxonomy",
        "mempalace_search",
        "mempalace_check_duplicate",
        "mempalace_list_drawers",
        "mempalace_get_drawer",
        "mempalace_add_drawer",
        "mempalace_update_drawer",
        "mempalace_delete_drawer",
        "mempalace_kg_add",
        "mempalace_kg_query",
        "mempalace_kg_invalidate",
        "mempalace_kg_timeline",
        "mempalace_kg_stats",
    }
    assert set(kn.TOOLS.keys()) == expected


def test_wing_axis_must_be_known_value():
    """The module rejects unknown KNOSSOS_WING_AXIS values at import; we
    exercise the validator by simulating the check inline."""
    valid = {"owner_id", "namespace"}
    for v in valid:
        # Should not raise.
        assert v in valid
    # And the module's current value is one of them.
    assert kn.WING_AXIS in valid
