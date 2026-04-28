"""App-layer owner + namespace filtering on search / rehydrate (v3.1.2 Tier 3).

The v3.1.1 search handler passed `request.namespace` verbatim to the
SQL helpers and never passed owner_id at all. A non-root user could
search any namespace and see every owner's rows (unless RLS was
enabled). These tests pin the defense-in-depth app-layer filter:
non-root callers get `owner_id = user.user_id` AND
`namespace = user.namespace` forced regardless of the request body;
cross-namespace requests from non-root callers get 403.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import MemorySearchRequest, RehydrationRequest


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


class _Conn:
    def __init__(self):
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return []

    async def fetchrow(self, *a, **kw):
        return None

    async def execute(self, *a, **kw):
        return "OK"

    def transaction(self):
        class _NullCtx:
            async def __aenter__(self_): return self_
            async def __aexit__(self_, *a): return False
        return _NullCtx()


class _PoolCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _install(monkeypatch, conn):
    import mnemos.core.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)
    monkeypatch.setattr(lc, "_rls_enabled", False)
    monkeypatch.setattr(lc, "_cache", None)
    monkeypatch.setattr(
        memories_handler, "_row_to_memory",
        lambda r, **kw: {"id": r.get("id", "x")},
    )


# ---- /memories/search owner + namespace pinning ---------------------------


def test_search_pins_owner_and_namespace_for_non_root(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)

    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    asyncio.run(memories_handler.search_memories(req, user=_alice("alice-ns")))

    assert conn.fetch_calls, "expected a search fetch"
    sql, args = conn.fetch_calls[-1]
    # Both owner_id and namespace in the WHERE clause
    assert "owner_id=$" in sql
    assert "namespace=$" in sql
    # And their values are the caller's, not caller-controlled
    assert "alice" in args
    assert "alice-ns" in args


def test_search_rejects_cross_namespace_for_non_root(monkeypatch):
    """A non-root caller requesting a namespace other than their own
    gets 403 — we don't silently narrow their request."""
    conn = _Conn()
    _install(monkeypatch, conn)

    req = MemorySearchRequest(
        query="hello", limit=10, semantic=False,
        namespace="bob-ns",  # not alice's namespace
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(memories_handler.search_memories(req, user=_alice("alice-ns")))
    assert exc.value.status_code == 403


def test_search_root_may_search_any_namespace(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)

    req = MemorySearchRequest(
        query="hello", limit=10, semantic=False,
        namespace="other-ns",
    )
    asyncio.run(memories_handler.search_memories(req, user=_root()))

    sql, args = conn.fetch_calls[-1]
    # Root passes request.namespace through — other-ns appears in args
    assert "other-ns" in args
    # Root does NOT get owner_id filter
    assert "owner_id=$" not in sql


def test_search_root_without_namespace_has_no_ns_or_owner_filter(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)

    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    asyncio.run(memories_handler.search_memories(req, user=_root()))

    sql, _ = conn.fetch_calls[-1]
    assert "owner_id=$" not in sql
    assert "namespace=$" not in sql


# ---- /memories/rehydrate owner + namespace pinning ------------------------


def test_rehydrate_pins_owner_and_namespace_for_non_root(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)

    req = RehydrationRequest(query="hello", limit=5)
    asyncio.run(memories_handler.rehydrate_memories(req, user=_alice("alice-ns")))

    sql, args = conn.fetch_calls[-1]
    assert "owner_id=$" in sql
    assert "namespace=$" in sql
    assert "alice" in args
    assert "alice-ns" in args


def test_rehydrate_root_has_no_filter(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)

    req = RehydrationRequest(query="hello", limit=5)
    asyncio.run(memories_handler.rehydrate_memories(req, user=_root()))

    sql, _ = conn.fetch_calls[-1]
    assert "owner_id=$" not in sql
    assert "namespace=$" not in sql
