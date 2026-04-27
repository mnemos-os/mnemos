"""Per-snapshot visibility regressions for branch creation/listing."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from api.auth import UserContext
from api.handlers import dag as dag_handler
from api import mcp_tools


def _alice() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="alice-ns", authenticated=True,
    )


class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return False


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


def _install(monkeypatch, conn):
    import api.lifecycle as lc

    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


class _HiddenStartConn:
    """Live memory is Alice-owned, but the requested/head snapshot is Bob-private."""

    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, 0)
        self.start_sql: str | None = None
        self.insert_attempts = 0
        self.fetch_calls: list[tuple[str, tuple]] = []

    def transaction(self):
        return _Txn()

    async def fetchrow(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        compact = " ".join(sql.split())

        if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id = $1"):
            return {"owner_id": "alice", "namespace": "alice-ns"}

        if compact.startswith("SELECT 1 FROM memories WHERE id = $1 AND owner_id = $2"):
            return {"ok": 1}

        if "FROM memory_versions" in compact and "commit_hash = $2" in compact:
            self.start_sql = sql
            if "permission_mode % 10" in compact and "namespace = $" in compact:
                return None
            return {"id": "hidden-version-id", "commit_hash": "hidden-hash", "created_at": self.now}

        if "FROM memory_versions mv" in compact and "mb.name = 'main'" in compact:
            self.start_sql = sql
            if "permission_mode % 10" in compact and "mv.namespace = $" in compact:
                return None
            return {"id": "hidden-version-id", "commit_hash": "hidden-hash", "created_at": self.now}

        if compact.startswith("SELECT id FROM memory_branches WHERE memory_id = $1 AND name = $2"):
            return None

        if compact.startswith("INSERT INTO memory_branches"):
            self.insert_attempts += 1
            return {"head_version_id": args[2]}

        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO memory_branches"):
            self.insert_attempts += 1
            return "branch-id"
        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        compact = " ".join(sql.split())
        if "FROM memory_branches mb" in compact and "LEFT JOIN memory_versions mv" in compact:
            self.start_sql = sql
            if "permission_mode % 10" in compact and "mv.namespace = $" in compact:
                return [{
                    "name": "main",
                    "commit_hash": None,
                    "created_at": self.now,
                    "created_by": "alice",
                }]
            return [{
                "name": "main",
                "commit_hash": "hidden-hash",
                "created_at": self.now,
                "created_by": "alice",
            }]
        raise AssertionError(f"unexpected fetch SQL: {sql}")


@pytest.mark.parametrize(
    ("from_commit", "detail"),
    [
        ("hidden-hash", "Commit hash not found"),
        (None, "main branch HEAD not found"),
    ],
    ids=["explicit-hidden-commit", "default-hidden-head"],
)
def test_create_branch_rejects_hidden_start_commit_http(monkeypatch, from_commit, detail):
    conn = _HiddenStartConn()
    _install(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            dag_handler.create_branch(
                "mem-1",
                dag_handler.BranchCreateRequest(name="feature", from_commit=from_commit),
                user=_alice(),
            )
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == detail
    assert conn.insert_attempts == 0
    assert conn.start_sql is not None
    assert "permission_mode % 10" in conn.start_sql
    assert "namespace = $" in conn.start_sql


@pytest.mark.parametrize(
    ("from_commit", "error"),
    [
        ("hidden-hash", "Commit not found"),
        (None, "main branch not found"),
    ],
    ids=["explicit-hidden-commit", "default-hidden-head"],
)
def test_mcp_branch_memory_rejects_hidden_start_commit(monkeypatch, from_commit, error):
    conn = _HiddenStartConn()
    _install(monkeypatch, conn)

    result = asyncio.run(
        mcp_tools.tool_branch_memory(
            "mem-1", "feature", from_commit=from_commit, user=_alice(),
        )
    )

    assert result == {"success": False, "error": error}
    assert conn.insert_attempts == 0
    assert conn.start_sql is not None
    assert "permission_mode % 10" in conn.start_sql
    assert "namespace = $" in conn.start_sql


def test_list_branches_suppresses_invisible_heads(monkeypatch):
    conn = _HiddenStartConn()
    _install(monkeypatch, conn)

    branches = asyncio.run(
        dag_handler.get_memory_branches("mem-1", user=_alice())
    )

    assert branches == []
    assert conn.start_sql is not None
    assert "permission_mode % 10" in conn.start_sql
    assert "mv.namespace = $" in conn.start_sql
