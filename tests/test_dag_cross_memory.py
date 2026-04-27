"""Cross-memory DAG edge regressions for DAG handlers."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from api.auth import UserContext
from api.handlers import dag as dag_handler


def _alice() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="alice-ns", authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


class _Conn:
    def __init__(self):
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        compact = " ".join(sql.split())

        if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id = $1"):
            return {"owner_id": "alice", "namespace": "alice-ns"}

        if "FROM memory_versions mv" in compact and "mv.commit_hash" in compact:
            same_memory_parent = "AND mv2.memory_id = mv.memory_id" in compact
            return {
                "commit_hash": "child-hash",
                "version_num": 2,
                "parent_hash": None if same_memory_parent else "foreign-parent-hash",
                "branch": "main",
                "content": "child content",
                "category": "solutions",
                "subcategory": None,
                "snapshot_at": datetime(2026, 1, 1, 12, 0, 0),
                "snapshot_by": "alice",
                "change_type": "update",
            }

        raise AssertionError(f"unexpected fetchrow SQL: {sql}")


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


@pytest.mark.parametrize("user", [_root(), _alice()], ids=["root", "user"])
def test_get_commit_suppresses_cross_memory_parent_hash(monkeypatch, user):
    conn = _Conn()
    _install(monkeypatch, conn)

    commit = asyncio.run(
        dag_handler.get_commit("memory-a", "child-hash", user=user)
    )

    assert commit.parent_hash is None
    commit_sql = next(
        sql for sql, _args in conn.fetchrow_calls
        if "FROM memory_versions mv" in sql
    )
    assert "AND mv2.memory_id = mv.memory_id" in " ".join(commit_sql.split())
