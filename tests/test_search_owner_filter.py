"""App-layer owner + namespace filtering on search (v3.1.2 Tier 3, v4.1).

The v3.1.1 search handler passed ``request.namespace`` verbatim to the
SQL helpers and never passed ``owner_id`` at all — a non-root user
could search any namespace. v3.1.2 pinned both fields; v4.1 moved the
enforcement under the repository surface via ``VisibilityFilter``.

Slice 1d migrated the handler from raw asyncpg to backend dispatch.
SQL-shape assertions moved to ``tests/test_persistence_parity.py``;
these tests now assert the ``VisibilityFilter`` shape the handler
constructs and passes to ``backend.memories.semantic_search`` /
``fts_search``.

The ``rehydrate`` tests are dropped — that endpoint is deferred to
v4.2 (still 503 on edge in v4.1) and covered separately when its
conversion lands.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import MemorySearchRequest
from mnemos.persistence.visibility import VisibilityScope

from tests._fake_backend import install_fake_backend


def _alice(namespace: str = "alice-ns") -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace=namespace, authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


def _last_search_call(backend) -> dict:
    for name, kw in reversed(backend.memories.calls):
        if name in ("fts_search", "semantic_search"):
            return kw
    raise AssertionError(f"no search call captured: {backend.memories.calls}")


def test_search_pins_owner_and_namespace_for_non_root(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    asyncio.run(memories_handler.search_memories(req, user=_alice("alice-ns")))

    call = _last_search_call(backend)
    vis = call["visibility"]
    assert vis.scope == VisibilityScope.READABLE
    assert vis.user_id == "alice"
    assert vis.namespace == "alice-ns"


def test_search_rejects_cross_namespace_for_non_root(monkeypatch):
    install_fake_backend(monkeypatch)
    req = MemorySearchRequest(
        query="hello", limit=10, semantic=False, namespace="bob-ns",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(memories_handler.search_memories(req, user=_alice("alice-ns")))
    assert exc.value.status_code == 403


def test_search_root_may_search_any_namespace(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    req = MemorySearchRequest(
        query="hello", limit=10, semantic=False, namespace="other-ns",
    )
    asyncio.run(memories_handler.search_memories(req, user=_root()))

    call = _last_search_call(backend)
    vis = call["visibility"]
    assert vis.scope == VisibilityScope.ROOT_BYPASS
    assert vis.namespace == "other-ns"
    assert vis.user_id is None


def test_search_root_without_namespace_has_no_ns_or_owner_filter(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    asyncio.run(memories_handler.search_memories(req, user=_root()))

    call = _last_search_call(backend)
    vis = call["visibility"]
    assert vis.scope == VisibilityScope.ROOT_BYPASS
    assert vis.namespace is None
    assert vis.user_id is None
