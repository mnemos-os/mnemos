"""Regression tests for update_memory trigger error translation."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import asyncpg
import pytest
from fastapi import HTTPException

from api.auth import UserContext
from api.handlers import memories as memories_handler
from api.handlers import versions as versions_handler
from api.models import MemoryUpdateRequest


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


class _DeleteConn:
    def __init__(self):
        self.execute_calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        raise _mn001_error()


class _RevertConn:
    def __init__(self):
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        compact = " ".join(sql.split())
        if compact.startswith("SELECT 1 FROM memory_versions"):
            return {"exists": 1}
        if compact.startswith("SELECT id, memory_id, version_num"):
            return {
                "id": "version-1",
                "memory_id": "memory-1",
                "version_num": 1,
                "content": "old content",
                "category": "solutions",
                "subcategory": None,
                "metadata": {"source": "test"},
                "verbatim_content": "old content",
                "owner_id": "alice",
                "namespace": "alice-ns",
                "permission_mode": 600,
                "source_model": None,
                "source_provider": None,
                "source_session": None,
                "source_agent": None,
                "snapshot_at": None,
                "snapshot_by": None,
                "change_type": "update",
            }
        if compact.startswith("SELECT id, content, category"):
            return {
                "id": "memory-1",
                "content": "current content",
                "category": "solutions",
                "subcategory": None,
                "created": None,
                "updated": None,
                "metadata": {"source": "test"},
                "quality_rating": 75,
                "compressed_content": None,
                "verbatim_content": "current content",
                "owner_id": "alice",
                "group_id": None,
                "namespace": "alice-ns",
                "permission_mode": 600,
                "source_model": None,
                "source_provider": None,
                "source_session": None,
                "source_agent": None,
            }
        if compact.startswith("SELECT mv.content"):
            return {
                "content": "current content",
                "category": "solutions",
                "subcategory": None,
                "metadata": {"source": "test"},
                "verbatim_content": "current content",
                "owner_id": "alice",
                "namespace": "alice-ns",
                "permission_mode": 600,
                "commit_hash": "a" * 64,
            }
        if compact.startswith("UPDATE memories SET"):
            raise _mn001_error()
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

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


def test_delete_memory_translates_mn001_trigger_error_to_conflict(monkeypatch):
    conn = _DeleteConn()
    _install(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            memories_handler.delete_memory(
                "memory-1",
                user=_alice(),
            )
        )

    assert exc_info.value.status_code == 409
    assert "Reconcile memory_branches and memory_versions" in exc_info.value.detail
    assert conn.execute_calls
    sql, args = conn.execute_calls[-1]
    assert sql.startswith("DELETE FROM memories")
    assert "owner_id = $2" in sql
    assert "namespace = $3" in sql
    assert args == ("memory-1", "alice", "alice-ns")


def test_revert_memory_translates_mn001_main_update_to_conflict(monkeypatch):
    conn = _RevertConn()
    _install(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            versions_handler.revert_memory(
                "memory-1",
                1,
                branch="main",
                user=_root(),
            )
        )

    assert exc_info.value.status_code == 409
    assert "Reconcile memory_branches and memory_versions" in exc_info.value.detail
    assert any(
        sql.startswith("SELECT set_config('mnemos.current_branch', 'main', true)")
        for sql, _args in conn.execute_calls
    )
    assert conn.fetchrow_calls[-1][0].startswith("UPDATE memories SET")
