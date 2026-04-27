"""Namespace enforcement on memory read paths (v3.1.2 Tier 3).

App-layer defense-in-depth for the `namespace` column on memories.
RLS (when enabled) scopes by owner_id / group_id but does NOT filter
by namespace — a second tenancy dimension introduced in v3.1.x. These
tests pin the app-layer filter on `list_memories` and `get_memory`
so cross-namespace reads are blocked even when RLS is off (personal-
mode default).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from api.auth import UserContext
from api.handlers import memories as memories_handler


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
    """Asyncpg-shaped mock that records fetch/fetchrow SQL + args."""

    def __init__(self, rows=None, row_for_get=None):
        self._rows = rows or []
        self._row_for_get = row_for_get
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return self._rows

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        return self._row_for_get

    async def fetchval(self, sql: str, *args):
        self.fetchval_calls.append((sql, args))
        return len(self._rows)

    async def execute(self, sql: str, *args):
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
    import api.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)
    # RLS disabled — we're testing app-layer fallback specifically.
    monkeypatch.setattr(lc, "_rls_enabled", False)
    # Avoid loading real row decoder
    monkeypatch.setattr(
        memories_handler, "_row_to_memory",
        lambda r, **kw: {"id": r.get("id", "x")},
    )


def _fetched_sql(conn) -> str:
    assert conn.fetch_calls, "expected a fetch call"
    return conn.fetch_calls[-1][0]


def _fetched_args(conn) -> tuple:
    assert conn.fetch_calls, "expected a fetch call"
    return conn.fetch_calls[-1][1]


# ---- list_memories ---------------------------------------------------------


def test_list_memories_filters_by_namespace_for_non_root(monkeypatch):
    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    asyncio.run(memories_handler.list_memories(user=_alice("alice-ns")))

    sql = _fetched_sql(conn)
    args = _fetched_args(conn)
    assert "namespace=$" in sql
    assert "alice-ns" in args
    # v3.5 audit slice 2: list_memories must also scope by owner_id
    # for non-root callers, matching search/update/delete. Without
    # this, a non-root user could list other users' rows in the
    # same namespace.
    # Full read-visibility predicate (mirrors v1_multiuser RLS policies):
    # owner / federation / world-readable / group-readable. RLS cannot
    # re-add rows that the WHERE rejected, so all four branches must
    # appear at the app layer.
    assert "owner_id=$" in sql
    assert "federation_source IS NOT NULL" in sql
    assert "permission_mode % 10" in sql  # world-readable
    assert "(permission_mode / 10) % 10" in sql  # group-readable threshold
    assert "group_id = ANY(" in sql        # group-membership branch
    assert "alice" in args


def test_list_memories_no_namespace_filter_for_root(monkeypatch):
    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    asyncio.run(memories_handler.list_memories(user=_root()))

    sql = _fetched_sql(conn)
    assert "namespace=$" not in sql
    # Root bypasses both namespace and owner_id scoping.
    assert "owner_id=$" not in sql


def test_list_memories_combines_namespace_with_category(monkeypatch):
    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    asyncio.run(memories_handler.list_memories(
        category="solutions", user=_alice("alice-ns"),
    ))

    sql = _fetched_sql(conn)
    args = _fetched_args(conn)
    assert "category=$" in sql
    assert "namespace=$" in sql
    assert "owner_id=$" in sql
    assert "solutions" in args
    assert "alice-ns" in args
    assert "alice" in args


def test_list_memories_combines_namespace_with_subcategory(monkeypatch):
    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    asyncio.run(memories_handler.list_memories(
        subcategory="pipeline", user=_alice("alice-ns"),
    ))

    sql = _fetched_sql(conn)
    args = _fetched_args(conn)
    assert "subcategory=$" in sql
    assert "namespace=$" in sql
    assert "owner_id=$" in sql
    assert "pipeline" in args
    assert "alice-ns" in args
    assert "alice" in args


def test_list_memories_combines_namespace_with_category_and_subcategory(monkeypatch):
    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    asyncio.run(memories_handler.list_memories(
        category="solutions", subcategory="pipeline",
        user=_alice("alice-ns"),
    ))

    sql = _fetched_sql(conn)
    args = _fetched_args(conn)
    assert "category=$" in sql
    assert "subcategory=$" in sql
    assert "namespace=$" in sql
    assert "owner_id=$" in sql
    assert "federation_source IS NOT NULL" in sql
    assert all(v in args for v in ("solutions", "pipeline", "alice-ns", "alice"))


def test_list_memories_rejects_cross_namespace_for_non_root(monkeypatch):
    """Non-root caller asking ?namespace=other → 403, not silent re-scope.
    Mirrors search_memories' parity contract; hides bad caller behavior
    otherwise."""
    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    import pytest as _p
    with _p.raises(HTTPException) as exc:
        asyncio.run(memories_handler.list_memories(
            namespace="other-ns", user=_alice("alice-ns"),
        ))
    assert exc.value.status_code == 403


def test_list_memories_root_honors_explicit_namespace(monkeypatch):
    """Root callers can target a specific namespace for cross-tenant
    audit lookups."""
    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    asyncio.run(memories_handler.list_memories(
        namespace="other-ns", user=_root(),
    ))

    sql = _fetched_sql(conn)
    args = _fetched_args(conn)
    assert "namespace=$" in sql
    assert "other-ns" in args
    assert "owner_id=$" not in sql  # root still bypasses owner scoping


# ---- get_memory ------------------------------------------------------------


def test_get_memory_filters_by_namespace_for_non_root(monkeypatch):
    conn = _Conn(row_for_get={"id": "mem_1"})
    _install(monkeypatch, conn)

    asyncio.run(memories_handler.get_memory("mem_1", user=_alice("alice-ns")))

    sql, args = conn.fetchrow_calls[-1]
    assert "namespace=$" in sql
    assert "alice-ns" in args
    # v3.5 audit slice 2: get_memory must also scope by owner_id —
    # otherwise any non-root caller in the same namespace could read
    # other users' rows by guessing memory_id.
    # Full read-visibility predicate (mirrors v1_multiuser RLS policies):
    # owner / federation / world-readable / group-readable. RLS cannot
    # re-add rows that the WHERE rejected, so all four branches must
    # appear at the app layer.
    assert "owner_id=$" in sql
    assert "federation_source IS NOT NULL" in sql
    assert "permission_mode % 10" in sql  # world-readable
    assert "(permission_mode / 10) % 10" in sql  # group-readable threshold
    assert "group_id = ANY(" in sql        # group-membership branch
    assert "alice" in args


def test_get_memory_no_namespace_filter_for_root(monkeypatch):
    conn = _Conn(row_for_get={"id": "mem_1"})
    _install(monkeypatch, conn)

    asyncio.run(memories_handler.get_memory("mem_1", user=_root()))

    sql, _ = conn.fetchrow_calls[-1]
    assert "namespace=$" not in sql
    assert "owner_id=$" not in sql


def test_get_memory_returns_404_when_namespace_mismatch(monkeypatch):
    """When the filtered SELECT returns no row (because the memory is
    in a different namespace), the handler raises 404 — uniform with
    "memory doesn't exist" so existence isn't leaked.
    """
    conn = _Conn(row_for_get=None)
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    import pytest as _p
    with _p.raises(HTTPException) as exc:
        asyncio.run(memories_handler.get_memory(
            "mem_in_other_ns", user=_alice("alice-ns"),
        ))
    assert exc.value.status_code == 404
