"""Regression tests for update_memory trigger error translation."""

from __future__ import annotations

import asyncio
from pathlib import Path
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


def _mn001_error(message: str = "cross-memory branch head") -> asyncpg.PostgresError:
    exc = asyncpg.PostgresError(message)
    exc.sqlstate = "MN001"
    return exc


class _Conn:
    def __init__(self, message: str = "cross-memory branch head"):
        self.message = message
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        raise _mn001_error(self.message)

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


def _assert_update_memory_conflict(monkeypatch, message: str):
    conn = _Conn(message)
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
    return exc_info.value


def _trigger_sql() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return (repo_root / "db" / "migrations_v3_5_trigger_same_memory_parent.sql").read_text()


def _extract_update_branch(sql: str) -> str:
    try:
        return sql.split("ELSIF TG_OP = 'UPDATE' THEN", 1)[1].split(
            "ELSIF TG_OP = 'DELETE' THEN", 1,
        )[0]
    except IndexError as exc:
        raise AssertionError("could not isolate mnemos_version_snapshot UPDATE branch") from exc


def _extract_delete_branch(sql: str) -> str:
    try:
        return sql.split("ELSIF TG_OP = 'DELETE' THEN", 1)[1].split(
            "\n    IF TG_OP = 'DELETE' THEN", 1,
        )[0]
    except IndexError as exc:
        raise AssertionError("could not isolate mnemos_version_snapshot DELETE branch") from exc


def test_trigger_update_delete_reject_missing_null_and_foreign_heads_before_insert():
    sql = _trigger_sql()

    for branch_sql, row_id in (
        (_extract_update_branch(sql), "NEW.id"),
        (_extract_delete_branch(sql), "OLD.id"),
    ):
        compact = " ".join(branch_sql.split())
        insert_pos = compact.index("INSERT INTO memory_versions")

        locked_bare_head_check = (
            "SELECT mb.head_version_id INTO _bare_head FROM memory_branches mb "
            f"WHERE mb.memory_id = {row_id} AND mb.name = _branch FOR UPDATE OF mb"
        )

        assert locked_bare_head_check in compact
        assert "_branch_exists := FOUND" in compact
        assert "IF NOT _branch_exists THEN RAISE EXCEPTION" in compact
        assert "has NULL head_version_id" in compact
        assert "AND mv.memory_id = mb.memory_id" in compact
        assert (
            f"WHERE mb.memory_id = {row_id} AND mb.name = _branch FOR UPDATE OF mb"
            in compact
        )
        assert "points outside this memory" in compact
        assert compact.index("_branch_exists := FOUND") < insert_pos
        assert compact.index("IF NOT _branch_exists") < insert_pos
        assert compact.index("IF _bare_head IS NULL") < insert_pos
        assert compact.index("IF _parent_version IS NULL") < insert_pos


def test_update_memory_translates_missing_branch_row_mn001_to_conflict(monkeypatch):
    exc = _assert_update_memory_conflict(
        monkeypatch,
        "mnemos: branch main for memory memory-1 is missing",
    )

    assert "branch row is missing" in exc.detail


def test_update_memory_translates_null_branch_head_mn001_to_conflict(monkeypatch):
    exc = _assert_update_memory_conflict(
        monkeypatch,
        "mnemos: branch main for memory memory-1 has NULL head_version_id",
    )

    assert "NULL head_version_id" in exc.detail


def test_update_memory_translates_mn001_trigger_error_to_conflict(monkeypatch):
    _assert_update_memory_conflict(monkeypatch, "cross-memory branch head")


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
