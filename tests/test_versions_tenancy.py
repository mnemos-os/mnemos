"""Tenancy contract tests for the versions handler (slice 2 round 10).

Codex round-10 review of slice 2 caught two CRITICAL findings in
api/handlers/versions.py:

  1. list_versions / get_version / diff_versions queried
     memory_versions by memory_id+branch alone — any authenticated
     caller could read every other tenant's full history by
     guessing memory_id.
  2. revert_memory updated `memories WHERE id=$N` with no owner
     check — cross-tenant write under RLS-disabled installs.

These tests pin the tenancy chokepoint (`_assert_memory_readable`)
plus the atomic owner+namespace gate on the revert UPDATE.
"""

from __future__ import annotations

import asyncio

import pytest

from api.auth import UserContext
from api.handlers.versions import _assert_memory_readable


def _alice(ns: str = "alice-ns") -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace=ns, authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


class _Conn:
    """Records fetchrow SQL + args; returns a configured row or None."""

    def __init__(self, row=None):
        self._row = row
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        return self._row


def test_assert_readable_root_bypasses_tenancy():
    """Root sees every memory's history regardless of namespace/owner."""
    conn = _Conn(row={"existing": 1})  # _assert_memory_exists
    asyncio.run(_assert_memory_readable(conn, "mem_1", _root()))


def test_assert_readable_non_root_uses_full_visibility_predicate():
    """Non-root callers go through the shared read_visibility_predicate
    plus a namespace pin. The SQL must include all four predicate
    branches (owner / federation / world / group)."""
    conn = _Conn(row={"existing": 1})
    asyncio.run(_assert_memory_readable(conn, "mem_1", _alice()))

    sql, args = conn.fetchrow_calls[-1]
    assert "owner_id=$" in sql
    assert "federation_source IS NOT NULL" in sql
    assert "permission_mode % 10" in sql           # world-readable
    assert "(permission_mode / 10) % 10" in sql    # group-readable
    assert "group_id = ANY(" in sql
    assert "namespace = $" in sql
    assert "alice" in args
    assert "alice-ns" in args


def test_assert_readable_non_root_404_on_invisible_memory():
    """When the visibility-pinned SELECT returns no row, the helper
    raises 404 — uniform with 'memory does not exist' so existence
    of other-tenant memories isn't leaked."""
    conn = _Conn(row=None)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_assert_memory_readable(conn, "mem_other", _alice()))
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail.lower()
