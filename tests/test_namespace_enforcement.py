"""Namespace enforcement on memory read paths (v3.1.2 Tier 3, v4.1).

App-layer defense-in-depth for the ``namespace`` column on memories.

Slice 1d migrated handler dispatch from raw asyncpg to the
backend-neutral ``backend.memories.*`` repository. The SQL-shape
assertions that used to live here moved to repository parity tests
in ``tests/test_persistence_parity.py``. These tests now assert the
*intent* the handler sends to the backend — the ``VisibilityFilter``
shape — which is the right contract at this layer.

The properties protected:
- Non-root callers get ``READABLE`` scope pinned to ``user.namespace``.
- Cross-namespace requests from non-root callers return 403.
- Root callers can pass any ``namespace`` (or none) for cross-tenant
  audit lookups.
- ``GET /memories/{id}`` for non-root pins ``namespace = user.namespace``;
  for root, ``namespace`` is ``None`` (cross-tenant lookup).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
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


def _last_call(backend, method: str) -> dict:
    for name, kw in reversed(backend.memories.calls):
        if name == method:
            return kw
    raise AssertionError(f"no {method} call captured: {backend.memories.calls}")


def _empty_request():
    """Mock fastapi.Request with no Accept header — drives the default
    JSON negotiation path used by these tests."""
    from unittest.mock import MagicMock

    req = MagicMock()
    req.headers = {}
    return req


def _memory_row(*, namespace: str = "other-ns", owner_id: str = "other-owner") -> dict:
    return {
        "id": "memory-1",
        "content": "updated content",
        "category": "solutions",
        "subcategory": None,
        "created": "2026-04-29T12:34:56",
        "updated": "2026-04-29T12:34:56",
        "metadata": "{}",
        "quality_rating": 75,
        "verbatim_content": "updated content",
        "owner_id": owner_id,
        "group_id": None,
        "namespace": namespace,
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
    }


# ---- list_memories ---------------------------------------------------------


def test_list_memories_filters_by_namespace_for_non_root(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    asyncio.run(memories_handler.list_memories(user=_alice("alice-ns")))

    call = _last_call(backend, "list_memories")
    vis = call["visibility"]
    assert vis.scope == VisibilityScope.READABLE
    assert vis.namespace == "alice-ns"
    assert vis.user_id == "alice"


def test_list_memories_no_namespace_filter_for_root(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    asyncio.run(memories_handler.list_memories(user=_root()))

    call = _last_call(backend, "list_memories")
    vis = call["visibility"]
    # Root with no explicit namespace => ROOT_BYPASS, no namespace pin
    assert vis.scope == VisibilityScope.ROOT_BYPASS
    assert vis.user_id is None


def test_list_memories_combines_namespace_with_category(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    asyncio.run(memories_handler.list_memories(
        category="solutions", user=_alice("alice-ns"),
    ))

    call = _last_call(backend, "list_memories")
    assert call["category"] == "solutions"
    vis = call["visibility"]
    assert vis.scope == VisibilityScope.READABLE
    assert vis.namespace == "alice-ns"


def test_list_memories_combines_namespace_with_subcategory(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    asyncio.run(memories_handler.list_memories(
        subcategory="pipeline", user=_alice("alice-ns"),
    ))

    call = _last_call(backend, "list_memories")
    assert call["subcategory"] == "pipeline"
    vis = call["visibility"]
    assert vis.namespace == "alice-ns"


def test_list_memories_combines_namespace_with_category_and_subcategory(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    asyncio.run(memories_handler.list_memories(
        category="solutions", subcategory="pipeline",
        user=_alice("alice-ns"),
    ))

    call = _last_call(backend, "list_memories")
    assert call["category"] == "solutions"
    assert call["subcategory"] == "pipeline"
    vis = call["visibility"]
    assert vis.namespace == "alice-ns"


def test_list_memories_rejects_cross_namespace_for_non_root(monkeypatch):
    install_fake_backend(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(memories_handler.list_memories(
            namespace="bob-ns",
            user=_alice("alice-ns"),
        ))
    assert exc.value.status_code == 403


def test_list_memories_root_honors_explicit_namespace(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    asyncio.run(memories_handler.list_memories(
        namespace="alice-ns", user=_root(),
    ))
    call = _last_call(backend, "list_memories")
    vis = call["visibility"]
    # Root + explicit namespace => ROOT_BYPASS scoped to that namespace
    assert vis.scope == VisibilityScope.ROOT_BYPASS
    assert vis.namespace == "alice-ns"


# ---- get_memory ------------------------------------------------------------


def test_get_memory_filters_by_namespace_for_non_root(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("get_memory", {"id": "x"})
    monkeypatch.setattr(
        memories_handler, "_row_to_memory",
        lambda r, **kw: {"id": r["id"]},
    )
    asyncio.run(memories_handler.get_memory(
        "memory-1", request=_empty_request(), user=_alice("alice-ns"),
    ))

    call = _last_call(backend, "get_memory")
    assert call["memory_id"] == "memory-1"
    vis = call["visibility"]
    assert vis.scope == VisibilityScope.READABLE
    assert vis.namespace == "alice-ns"
    assert vis.user_id == "alice"


def test_get_memory_no_namespace_filter_for_root(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("get_memory", {"id": "x"})
    monkeypatch.setattr(
        memories_handler, "_row_to_memory",
        lambda r, **kw: {"id": r["id"]},
    )
    asyncio.run(memories_handler.get_memory(
        "memory-1", request=_empty_request(), user=_root(),
    ))

    call = _last_call(backend, "get_memory")
    vis = call["visibility"]
    # Root callers get ROOT_BYPASS with namespace=None for cross-tenant
    # lookups
    assert vis.scope == VisibilityScope.ROOT_BYPASS
    assert vis.namespace is None


def test_get_memory_returns_404_when_namespace_mismatch(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("get_memory", None)  # repo filtered it out
    with pytest.raises(HTTPException) as exc:
        asyncio.run(memories_handler.get_memory(
            "memory-1", request=_empty_request(), user=_alice("alice-ns"),
        ))
    assert exc.value.status_code == 404


# ---- update_memory / delete_memory ----------------------------------------


def test_update_memory_root_has_no_namespace_pin(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return(
        "update_memory",
        _memory_row(namespace="other-ns", owner_id="other-owner"),
    )

    asyncio.run(memories_handler.update_memory(
        "memory-1",
        memories_handler.MemoryUpdateRequest(content="updated content"),
        user=_root(),
    ))

    call = _last_call(backend, "update_memory")
    vis = call["visibility"]
    assert vis.scope == VisibilityScope.ROOT_BYPASS
    assert vis.namespace is None


def test_delete_memory_root_has_no_namespace_pin(monkeypatch):
    backend = install_fake_backend(monkeypatch)

    asyncio.run(memories_handler.delete_memory("memory-1", user=_root()))

    call = _last_call(backend, "delete_memory")
    vis = call["visibility"]
    assert vis.scope == VisibilityScope.ROOT_BYPASS
    assert vis.namespace is None


def test_delete_memory_root_dispatches_with_deleted_row_tenant(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return(
        "delete_memory",
        _memory_row(namespace="alice-ns", owner_id="alice-owner"),
    )

    asyncio.run(memories_handler.delete_memory("memory-1", user=_root()))

    assert len(backend.webhooks.calls) == 1
    _, call = backend.webhooks.calls[0]
    assert call["event_type"] == "memory.deleted"
    assert call["owner_id"] == "alice-owner"
    assert call["namespace"] == "alice-ns"
    assert call["payload"]["owner_id"] == "alice-owner"
    assert call["payload"]["namespace"] == "alice-ns"


def test_update_memory_non_root_is_owner_and_namespace_pinned(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return(
        "update_memory",
        _memory_row(namespace="alice-ns", owner_id="alice"),
    )

    asyncio.run(memories_handler.update_memory(
        "memory-1",
        memories_handler.MemoryUpdateRequest(content="updated content"),
        user=_alice("alice-ns"),
    ))

    call = _last_call(backend, "update_memory")
    vis = call["visibility"]
    assert vis.scope == VisibilityScope.OWN_ONLY
    assert vis.user_id == "alice"
    assert vis.namespace == "alice-ns"


def test_delete_memory_non_root_is_owner_and_namespace_pinned(monkeypatch):
    backend = install_fake_backend(monkeypatch)

    asyncio.run(memories_handler.delete_memory("memory-1", user=_alice("alice-ns")))

    call = _last_call(backend, "delete_memory")
    vis = call["visibility"]
    assert vis.scope == VisibilityScope.OWN_ONLY
    assert vis.user_id == "alice"
    assert vis.namespace == "alice-ns"
