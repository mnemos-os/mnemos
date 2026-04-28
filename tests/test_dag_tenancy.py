"""DAG handler tenancy contract tests (v3.1.2 Tier 3 follow-up).

_assert_memory_access is the single chokepoint every DAG endpoint
calls before touching a memory's commit history. These tests pin the
two-dimensional tenancy gate (owner_id AND namespace) that was
extended here to match kg.py / memories.py.
"""

from __future__ import annotations

import asyncio

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.api.routes.dag import _assert_memory_access


def _user(uid: str = "alice", ns: str = "default") -> UserContext:
    return UserContext(
        user_id=uid, group_ids=[], role="user",
        namespace=ns, authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


class _Conn:
    def __init__(self, row=None):
        self._row = row
        self.queries: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.queries.append((sql, args))
        return self._row


def test_assert_allows_matching_owner_and_namespace():
    conn = _Conn(row={"owner_id": "alice", "namespace": "default"})
    # Should not raise
    asyncio.run(_assert_memory_access(conn, "mem_1", _user("alice", "default")))


def test_assert_rejects_different_owner():
    conn = _Conn(row={"owner_id": "bob", "namespace": "default"})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_assert_memory_access(conn, "mem_1", _user("alice", "default")))
    assert exc.value.status_code == 404
    assert "Memory not found" in exc.value.detail


def test_assert_rejects_same_owner_different_namespace():
    """Two-dimensional gate: same owner but different namespace still 404.
    Prevents a namespace-A caller from poking at a namespace-B memory
    that happens to share an owner_id."""
    conn = _Conn(row={"owner_id": "alice", "namespace": "other-ns"})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_assert_memory_access(conn, "mem_1", _user("alice", "default")))
    assert exc.value.status_code == 404


def test_assert_404_when_memory_missing():
    conn = _Conn(row=None)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_assert_memory_access(conn, "mem_404", _user()))
    assert exc.value.status_code == 404


def test_assert_root_bypasses_tenancy():
    """Root sees every memory regardless of owner or namespace."""
    conn = _Conn(row={"owner_id": "anyone", "namespace": "any-ns"})
    asyncio.run(_assert_memory_access(conn, "mem_1", _root()))


def test_assert_queries_both_owner_and_namespace():
    """Regression guard: the SELECT must fetch both columns, not just
    owner_id. Otherwise the namespace comparison silently reads None
    and non-root callers lose access to their own memories."""
    conn = _Conn(row={"owner_id": "alice", "namespace": "default"})
    asyncio.run(_assert_memory_access(conn, "mem_1", _user()))
    sql = conn.queries[0][0]
    assert "owner_id" in sql
    assert "namespace" in sql
