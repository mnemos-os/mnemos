"""Regression tests for update_memory trigger error translation."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import asyncpg
import pytest
from fastapi import HTTPException

from api.auth import UserContext
from api.handlers import memories as memories_handler
from api.models import MemoryUpdateRequest


def _alice(namespace: str = "alice-ns") -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace=namespace, authenticated=True,
    )


def _mn001_error() -> asyncpg.PostgresError:
    exc = asyncpg.PostgresError("cross-memory branch head")
    exc.sqlstate = "MN001"
    return exc


class _Conn:
    def __init__(self):
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        raise _mn001_error()

    def transaction(self):
        class _NullCtx:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False

        return _NullCtx()


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


def _install(monkeypatch, conn):
    import api.lifecycle as lc

    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)
    monkeypatch.setattr(lc, "_rls_enabled", False)
    monkeypatch.setattr(lc, "_cache", None)


def test_update_memory_translates_mn001_trigger_error_to_conflict(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            memories_handler.update_memory(
                "memory-1",
                MemoryUpdateRequest(content="new content"),
                user=_alice(),
            )
        )

    assert exc_info.value.status_code == 409
    assert "Reconcile memory_branches and memory_versions" in exc_info.value.detail
    assert conn.fetchrow_calls
    sql, args = conn.fetchrow_calls[-1]
    assert sql.startswith("UPDATE memories SET")
    assert "owner_id=$" in sql
    assert "namespace=$" in sql
    assert args[-2:] == ("alice", "alice-ns")
