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
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import versions as versions_handler
from mnemos.api.routes.versions import _assert_memory_readable


def _alice(ns: str = "alice-ns") -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace=ns, authenticated=True,
    )


def _bob(ns: str = "alice-ns") -> UserContext:
    return UserContext(
        user_id="bob", group_ids=[], role="user",
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


def _install_pool(monkeypatch, conn):
    import mnemos.core.lifecycle as lc

    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def _memory_row() -> dict:
    now = datetime(2026, 1, 1, 12, 0, 0)
    return {
        "id": "mem-1",
        "content": "live main content",
        "category": "solutions",
        "subcategory": None,
        "created": now,
        "updated": now,
        "metadata": {"live": True},
        "quality_rating": 80,
        "compressed_content": None,
        "verbatim_content": "live main content",
        "owner_id": "alice",
        "group_id": None,
        "namespace": "alice-ns",
        "permission_mode": 644,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
    }


class _FeatureRevertConn:
    def __init__(self):
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.memory = _memory_row()
        self.versions = {
            1: {
                "id": "source-v1",
                "memory_id": "mem-1",
                "version_num": 1,
                "content": "old public content",
                "category": "solutions",
                "subcategory": None,
                "metadata": {"from": "source"},
                "verbatim_content": "old public content",
                "owner_id": "bob",
                "namespace": "alice-ns",
                "permission_mode": 644,
                "source_model": "model-a",
                "source_provider": "provider-a",
                "source_session": "session-a",
                "source_agent": "agent-a",
                "snapshot_at": datetime(2026, 1, 1, 10, 0, 0),
                "snapshot_by": "bob",
                "change_type": "update",
            }
        }
        self.target_head = {
            "id": "target-head-id",
            "owner_id": "alice",
            "namespace": "alice-ns",
            "permission_mode": 600,
        }
        self.inserted_version: dict | None = None

    def transaction(self):
        return _Txn()

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        compact = " ".join(sql.split())

        if compact.startswith("SELECT 1 FROM memories WHERE id = $1"):
            return {"ok": 1}

        if "FROM memory_versions WHERE memory_id = $1 AND version_num = $2 AND branch = $3" in compact:
            row = self.versions.get(args[1])
            if row is None:
                return None
            if "permission_mode % 10" in compact:
                caller = args[3]
                namespace = args[-1]
                world_readable = row["permission_mode"] % 10 >= 4
                if row["namespace"] != namespace or (row["owner_id"] != caller and not world_readable):
                    return None
            return row

        if "FROM memories WHERE id=$1 AND owner_id=$2 AND namespace=$3 FOR UPDATE" in compact:
            if args[1] == self.memory["owner_id"] and args[2] == self.memory["namespace"]:
                return self.memory
            return None

        if compact.startswith("SELECT head_version_id FROM memory_branches WHERE memory_id = $1 AND name = $2 FOR UPDATE"):
            return {"head_version_id": self.target_head["id"]}

        if compact.startswith("SELECT 1 FROM memory_versions WHERE id = $1"):
            if args[0] != self.target_head["id"]:
                return None
            if "permission_mode % 10" in compact:
                caller = args[1]
                namespace = args[-1]
                world_readable = self.target_head["permission_mode"] % 10 >= 4
                if (
                    self.target_head["namespace"] != namespace
                    or (
                        self.target_head["owner_id"] != caller
                        and not world_readable
                    )
                ):
                    return None
            return {"ok": 1}

        if compact.startswith("SELECT id, owner_id, namespace, permission_mode FROM memory_versions WHERE id = $1 AND memory_id = $2"):
            return self.target_head

        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql: str, *args):
        self.fetchval_calls.append((sql, args))
        compact = " ".join(sql.split())

        if compact.startswith("SELECT COALESCE(MAX(version_num), 0) + 1 FROM memory_versions"):
            return 3

        if compact.startswith("INSERT INTO memory_versions"):
            self.inserted_version = {
                "id": "new-revert-id",
                "memory_id": args[0],
                "version_num": args[1],
                "content": args[2],
                "category": args[3],
                "subcategory": args[4],
                "metadata": args[5],
                "verbatim_content": args[6],
                "owner_id": args[7],
                "namespace": args[8],
                "permission_mode": args[9],
                "source_model": args[10],
                "source_provider": args[11],
                "source_session": args[12],
                "source_agent": args[13],
                "branch": args[14],
                "commit_hash": args[15],
                "parent_version_id": args[16],
                "snapshot_by": args[17],
                "snapshot_at": datetime(2026, 1, 1, 13, 0, 0),
                "change_type": "update",
            }
            self.versions[args[1]] = self.inserted_version
            return "new-revert-id"

        if compact.startswith("SELECT head_version_id FROM memory_branches"):
            return self.target_head["id"]

        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        compact = " ".join(sql.split())
        if compact.startswith("SELECT pg_advisory_xact_lock"):
            return "SELECT 1"
        if compact.startswith("UPDATE memory_branches SET head_version_id = $1"):
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")


def test_feature_branch_revert_preserves_target_head_tenancy(monkeypatch):
    conn = _FeatureRevertConn()
    _install_pool(monkeypatch, conn)

    asyncio.run(
        versions_handler.revert_memory(
            "mem-1", 1, branch="feature", user=_alice(),
        )
    )

    assert conn.inserted_version is not None
    assert conn.inserted_version["content"] == "old public content"
    assert conn.inserted_version["owner_id"] == "alice"
    assert conn.inserted_version["namespace"] == "alice-ns"
    assert conn.inserted_version["permission_mode"] == 600
    assert conn.inserted_version["parent_version_id"] == "target-head-id"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            versions_handler.get_version(
                "mem-1", 3, branch="feature", user=_bob(),
            )
        )
    assert exc.value.status_code == 404


def test_feature_branch_revert_rejects_invisible_target_head(monkeypatch):
    conn = _FeatureRevertConn()
    conn.target_head.update({"owner_id": "bob", "permission_mode": 600})
    _install_pool(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            versions_handler.revert_memory(
                "mem-1", 1, branch="feature", user=_alice(),
            )
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Branch 'feature' not found"
    assert conn.inserted_version is None
    assert not any(
        "INSERT INTO memory_versions" in sql
        for sql, _args in conn.fetchval_calls
    )
    target_gate_sql = next(
        sql for sql, _args in conn.fetchrow_calls
        if "SELECT 1 FROM memory_versions WHERE id = $1" in " ".join(sql.split())
    )
    assert "permission_mode % 10" in target_gate_sql
    assert "namespace = $" in target_gate_sql
