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


class _RecordingTxn:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.events.append("begin")
        self.conn.in_transaction = True
        return None

    async def __aexit__(self, exc_type, *_args):
        self.conn.events.append("rollback" if exc_type else "commit")
        self.conn.in_transaction = False
        return False


class _BranchCreateRaceConn:
    """HTTP create_branch sees preflight ownership, then a locked-row result."""

    def __init__(self, *, locked_owner_matches: bool, duplicate: bool = False):
        self.now = datetime(2026, 1, 1, 12, 0, 0)
        self.locked_owner_matches = locked_owner_matches
        self.duplicate = duplicate
        self.events: list[str] = []
        self.in_transaction = False
        self.insert_attempts = 0
        self.insert_sql: str | None = None
        self.start_sql: str | None = None
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    def transaction(self):
        return _RecordingTxn(self)

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        compact = " ".join(sql.split())

        if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id = $1"):
            assert self.in_transaction
            self.events.append("preflight")
            return {"owner_id": "alice", "namespace": "alice-ns"}

        if (
            compact.startswith("SELECT 1 FROM memories WHERE id = $1")
            and "FOR SHARE" in compact
        ):
            assert self.in_transaction
            self.events.append("lock")
            if self.locked_owner_matches:
                return {"ok": 1}
            return None

        if "FROM memory_versions mv" in compact and "mb.name = 'main'" in compact:
            assert self.in_transaction
            self.events.append("start")
            self.start_sql = sql
            return {
                "id": "main-version-id",
                "commit_hash": "main-hash",
                "created_at": self.now,
            }

        if compact.startswith("SELECT id FROM memory_branches WHERE memory_id = $1 AND name = $2"):
            raise AssertionError("create_branch must not use stale duplicate pre-check")

        if compact.startswith("INSERT INTO memory_branches"):
            assert self.in_transaction
            self.events.append("insert")
            self.insert_attempts += 1
            self.insert_sql = sql
            if self.duplicate:
                return None
            return {"id": "branch-id"}

        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql: str, *args):
        raise AssertionError(f"unexpected fetchval SQL: {sql}")


class _MergeHiddenTargetConn:
    """Alice owns the live memory, but target branch HEAD is Bob-private."""

    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, 0)
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.target_gate_sql: str | None = None
        self.insert_attempts = 0
        self.memory = {
            "id": "mem-1",
            "owner_id": "alice",
            "namespace": "alice-ns",
            "permission_mode": 600,
            "content": "live main content",
            "category": "solutions",
            "subcategory": None,
            "metadata": {"live": True},
            "verbatim_content": "live main content",
        }
        self.source_head = {
            "id": "source-head-id",
            "commit_hash": "source-hash",
            "content": "source content",
            "version_num": 2,
            "category": "solutions",
            "subcategory": None,
            "metadata": {"from": "source"},
            "verbatim_content": "source content",
            "source_model": None,
            "source_provider": None,
            "source_session": None,
            "source_agent": None,
        }
        self.hidden_target = {
            "id": "hidden-target-id",
            "version_num": 2,
            "commit_hash": "hidden-target-hash",
            "content": "hidden target content",
            "category": "solutions",
            "subcategory": None,
            "metadata": {"hidden": True},
            "verbatim_content": "hidden target content",
            "owner_id": "bob",
            "namespace": "alice-ns",
            "permission_mode": 600,
        }

    def transaction(self):
        return _Txn()

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        compact = " ".join(sql.split())

        if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id = $1"):
            return {"owner_id": "alice", "namespace": "alice-ns"}

        if "FROM memories WHERE id = $1 AND owner_id = $2 AND namespace = $3 FOR UPDATE" in compact:
            if args[1] == "alice" and args[2] == "alice-ns":
                return self.memory
            return None

        if "FROM memory_versions mv" in compact and "mb.name = $2" in compact:
            if args[1] == "source":
                return self.source_head
            if args[1] == "hidden_branch":
                return self.hidden_target

        if compact.startswith("SELECT head_version_id FROM memory_branches WHERE memory_id = $1 AND name = $2 FOR UPDATE"):
            if args[1] == "hidden_branch":
                return {"head_version_id": self.hidden_target["id"]}
            return None

        if compact.startswith("SELECT 1 FROM memory_versions WHERE id = $1"):
            self.target_gate_sql = sql
            if args[0] != self.hidden_target["id"]:
                return None
            if "permission_mode % 10" in compact:
                caller = args[1]
                namespace = args[-1]
                world_readable = self.hidden_target["permission_mode"] % 10 >= 4
                if (
                    self.hidden_target["namespace"] != namespace
                    or (
                        self.hidden_target["owner_id"] != caller
                        and not world_readable
                    )
                ):
                    return None
            return {"ok": 1}

        if compact.startswith("SELECT id, version_num, commit_hash, content"):
            if args[0] == self.hidden_target["id"] and args[1] == "mem-1":
                return self.hidden_target
            return None

        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql: str, *args):
        self.fetchval_calls.append((sql, args))
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO memory_versions"):
            self.insert_attempts += 1
            return "merge-version-id"
        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        compact = " ".join(sql.split())
        if compact.startswith("SELECT pg_advisory_xact_lock"):
            return "SELECT 1"
        if compact.startswith("UPDATE memory_branches SET head_version_id = $1"):
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")


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


def test_create_branch_rechecks_locked_memory_before_insert_http(monkeypatch):
    conn = _BranchCreateRaceConn(locked_owner_matches=False)
    _install(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            dag_handler.create_branch(
                "mem-1",
                dag_handler.BranchCreateRequest(name="feature"),
                user=_alice(),
            )
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Memory not found"
    assert conn.insert_attempts == 0
    assert conn.start_sql is None
    assert conn.events == ["begin", "preflight", "lock", "rollback"]


def test_create_branch_inserts_after_locked_recheck_http(monkeypatch):
    conn = _BranchCreateRaceConn(locked_owner_matches=True)
    _install(monkeypatch, conn)

    branch = asyncio.run(
        dag_handler.create_branch(
            "mem-1",
            dag_handler.BranchCreateRequest(name="feature"),
            user=_alice(),
        )
    )

    assert branch == dag_handler.BranchInfo(
        name="feature",
        head_commit_hash="main-hash",
        created_at=conn.now.isoformat(),
        created_by="alice",
    )
    assert conn.events == ["begin", "preflight", "lock", "start", "insert", "commit"]
    assert conn.insert_attempts == 1
    assert conn.insert_sql is not None
    assert "ON CONFLICT (memory_id, name) DO NOTHING" in " ".join(conn.insert_sql.split())


def test_create_branch_duplicate_uses_atomic_insert_path_http(monkeypatch):
    conn = _BranchCreateRaceConn(locked_owner_matches=True, duplicate=True)
    _install(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            dag_handler.create_branch(
                "mem-1",
                dag_handler.BranchCreateRequest(name="feature"),
                user=_alice(),
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "Branch 'feature' already exists"
    assert conn.events == ["begin", "preflight", "lock", "start", "insert", "rollback"]
    assert conn.insert_attempts == 1
    assert conn.insert_sql is not None
    assert "ON CONFLICT (memory_id, name) DO NOTHING" in " ".join(conn.insert_sql.split())


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


def test_merge_rejects_invisible_target_head(monkeypatch):
    conn = _MergeHiddenTargetConn()
    _install(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            dag_handler.merge_branch(
                "mem-1",
                dag_handler.MergeRequest(source_branch="source"),
                target_branch="hidden_branch",
                user=_alice(),
            )
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Target branch 'hidden_branch' not found"
    assert conn.insert_attempts == 0
    assert conn.target_gate_sql is not None
    assert "permission_mode % 10" in conn.target_gate_sql
    assert "namespace = $" in conn.target_gate_sql
