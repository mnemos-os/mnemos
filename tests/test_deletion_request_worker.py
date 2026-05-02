"""GDPR deletion-request Phase B worker tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import MemorySearchRequest
from mnemos.persistence.postgres import PostgresMemoryRepository, PostgresTransaction
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from mnemos.workers import deletion_request_worker as worker
from tests._fake_backend import install_fake_backend


class _AsyncContext:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return None


class _TxContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return None


def _pool_for(conn):
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncContext(conn))
    return pool


def _confirmed_request(namespace=None):
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "target_user_id": "alice",
        "target_namespace": namespace,
    }


def _soft_deleted_request(namespace=None):
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "target_user_id": "alice",
        "target_namespace": namespace,
    }


def _marked_request():
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "soft_deleted_at": datetime(2026, 5, 1, 23, 5, 0, tzinfo=timezone.utc),
        "restore_by": datetime(2026, 5, 31, 23, 5, 0, tzinfo=timezone.utc),
    }


def _verifying_request():
    return {"id": "00000000-0000-0000-0000-000000000001"}


def _hard_deleted_request(namespace=None):
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "target_user_id": "alice",
        "target_namespace": namespace,
        "status": "hard_deleted",
        "soft_deleted_at": datetime(2026, 5, 1, 23, 5, 0, tzinfo=timezone.utc),
        "restore_by": datetime(2026, 5, 1, 23, 10, 0, tzinfo=timezone.utc),
        "hard_deleted_at": datetime(2026, 5, 2, 0, 0, 0, tzinfo=timezone.utc),
    }


def _target_labels() -> set[str]:
    return {
        label
        for label, _table, _sql in (
            *worker._OWNER_NAMESPACE_SOFT_DELETE_SQL,
            *worker._SOFT_DELETE_SQL,
        )
    }


def _hard_target_labels() -> set[str]:
    return {label for label, _table, _sql in worker._HARD_DELETE_SQL}


class _FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.deleted: list[str] = []

    async def get(self, key: str):
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.store.pop(key, None)

    async def scan_iter(self, *, match: str, count: int):
        for key in list(self.store):
            if match == "mnemos:search:*" and key.startswith("mnemos:search:"):
                yield key


@pytest.mark.asyncio
async def test_worker_soft_deletes_confirmed_request_happy_path():
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxContext())
    conn.fetchrow = AsyncMock(
        side_effect=[_confirmed_request(), _verifying_request(), _marked_request()]
    )
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock(return_value="UPDATE 1")

    result = await worker.process_one_deletion_request(_pool_for(conn))

    assert result is not None
    assert result.request_id == "00000000-0000-0000-0000-000000000001"
    assert result.target_user_id == "alice"
    assert result.target_namespace is None
    assert result.status == "soft_deleted"
    assert result.restore_by == _marked_request()["restore_by"]
    assert result.row_counts == {label: 1 for label in _target_labels()}
    assert result.verification_attempts == 1

    dequeue_sql = conn.fetchrow.await_args_list[0].args[0]
    assert "FOR UPDATE SKIP LOCKED" in dequeue_sql
    assert "status = 'confirmed'" in dequeue_sql
    verifying_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "SET status = 'sweep_verifying'" in verifying_sql
    mark_sql = conn.fetchrow.await_args_list[2].args[0]
    assert "SET status = 'soft_deleted'" in mark_sql
    assert "restore_by = NOW() + ($2::int * INTERVAL '1 day')" in mark_sql


@pytest.mark.asyncio
async def test_worker_soft_delete_is_namespace_scoped():
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")

    counts = await worker.soft_delete_target(conn, "alice", "tenant-a")

    assert set(counts) == _target_labels()
    assert all(call.args[1] == "alice" for call in conn.execute.await_args_list)
    assert all(call.args[2] == "tenant-a" for call in conn.execute.await_args_list)
    assert all("namespace = $2::text" in call.args[0] for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_worker_idempotent_after_request_leaves_confirmed_state():
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxContext())
    conn.fetchrow = AsyncMock(
        side_effect=[
            _confirmed_request(),
            _verifying_request(),
            _marked_request(),
            None,
        ]
    )
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock(return_value="UPDATE 1")

    counts = await worker.process_deletion_requests(_pool_for(conn), batch_size=2)

    assert counts["requests"] == 1
    assert {label: counts[label] for label in _target_labels()} == {
        label: 1 for label in _target_labels()
    }
    assert conn.execute.await_count == len(_target_labels())


@pytest.mark.asyncio
async def test_restore_target_reverses_only_the_soft_delete_batch_timestamp():
    soft_deleted_at = datetime(2026, 5, 1, 23, 5, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")

    counts = await worker.restore_soft_deleted_target(
        conn,
        "alice",
        "tenant-a",
        soft_deleted_at,
    )

    assert counts == {label: 1 for label in _target_labels()}
    assert all("SET deleted_at = NULL" in call.args[0] for call in conn.execute.await_args_list)
    assert all("$3::timestamptz" in call.args[0] for call in conn.execute.await_args_list)
    assert all(call.args[3] == soft_deleted_at for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_restore_target_invalidates_search_and_stats_cache(monkeypatch):
    cache = _FakeCache()
    cache.store["mnemos:search:primed"] = '{"count":1,"memories":[]}'
    cache.store["stats:global"] = "{}"
    cache.store["stats:global:v2"] = "{}"
    monkeypatch.setattr(lifecycle, "_cache", cache)
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")

    await worker.restore_soft_deleted_target(
        conn,
        "alice",
        "tenant-a",
        datetime(2026, 5, 1, 23, 5, 0, tzinfo=timezone.utc),
    )

    assert "mnemos:search:primed" in cache.deleted
    assert "stats:global" in cache.deleted
    assert "stats:global:v2" in cache.deleted


@pytest.mark.asyncio
async def test_worker_soft_delete_evicts_primed_search_cache_before_next_search(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    cache = _FakeCache()
    monkeypatch.setattr(lifecycle, "_cache", cache)
    live = {"visible": True}
    memory_row = {
        "id": "mem-cache-race",
        "content": "cached deletion target",
        "category": "facts",
        "subcategory": None,
        "created": datetime(2026, 5, 1, 22, 0, 0, tzinfo=timezone.utc),
        "updated": datetime(2026, 5, 1, 22, 0, 0, tzinfo=timezone.utc),
        "metadata": {},
        "quality_rating": 75,
        "compressed_content": None,
        "verbatim_content": "cached deletion target",
        "owner_id": "alice",
        "group_id": None,
        "namespace": "tenant-a",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
    }

    async def fts_search(tx, *, query, limit, visibility, **kwargs):
        return [memory_row] if live["visible"] else []

    async def noop_bump_recall_counters(memory_ids: list[str]) -> None:
        return None

    monkeypatch.setattr(backend.memories, "fts_search", fts_search)
    monkeypatch.setattr(memories_handler, "_bump_recall_counters", noop_bump_recall_counters)
    user = UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="tenant-a",
        authenticated=True,
    )
    request = MemorySearchRequest(query="cached", limit=10, semantic=False)

    first = await memories_handler.search_memories(request, user=user)
    await asyncio.sleep(0)
    assert [memory.id for memory in first.memories] == ["mem-cache-race"]
    assert any(key.startswith("mnemos:search:") for key in cache.store)

    async def execute(sql, *args):
        if "UPDATE memories" in sql and "SET deleted_at = NOW()" in sql:
            live["visible"] = False
            return "UPDATE 1"
        return "UPDATE 0"

    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxContext())
    conn.fetchrow = AsyncMock(
        side_effect=[
            _confirmed_request("tenant-a"),
            _verifying_request(),
            _marked_request(),
        ]
    )
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock(side_effect=execute)

    result = await worker.process_one_deletion_request(_pool_for(conn))
    assert result is not None
    assert result.status == "soft_deleted"

    second = await memories_handler.search_memories(request, user=user)

    assert second.memories == []
    assert any(key.startswith("mnemos:search:") for key in cache.deleted)


@pytest.mark.asyncio
async def test_worker_hard_deletes_expired_soft_deleted_request_happy_path(monkeypatch):
    monkeypatch.setattr(lifecycle, "_cache", None)
    memory_exists = {"value": True}

    async def execute(sql, *args):
        if sql.startswith("SET LOCAL"):
            return "SET"
        if "DELETE FROM memories" in sql:
            memory_exists["value"] = False
            return "DELETE 1"
        return "DELETE 1"

    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxContext())
    conn.fetchrow = AsyncMock(
        side_effect=[
            _soft_deleted_request("tenant-a"),
            _hard_deleted_request("tenant-a"),
        ]
    )
    conn.execute = AsyncMock(side_effect=execute)

    result = await worker.process_one_hard_deletion_request(_pool_for(conn))

    assert result is not None
    assert result.request_id == "00000000-0000-0000-0000-000000000001"
    assert result.target_user_id == "alice"
    assert result.target_namespace == "tenant-a"
    assert result.status == "hard_deleted"
    assert result.hard_deleted_at == _hard_deleted_request("tenant-a")["hard_deleted_at"]
    assert result.row_counts == {label: 1 for label in _hard_target_labels()}
    assert memory_exists["value"] is False

    dequeue_sql = conn.fetchrow.await_args_list[0].args[0]
    assert "FOR UPDATE SKIP LOCKED" in dequeue_sql
    assert "status = 'soft_deleted'" in dequeue_sql
    assert "restore_by < NOW()" in dequeue_sql
    mark_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "SET status = 'hard_deleted'" in mark_sql
    assert "hard_deleted_at = NOW()" in mark_sql


@pytest.mark.asyncio
async def test_worker_hard_delete_preserves_deletion_request_audit_row(monkeypatch):
    monkeypatch.setattr(lifecycle, "_cache", None)
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxContext())
    conn.fetchrow = AsyncMock(
        side_effect=[
            _soft_deleted_request(),
            _hard_deleted_request(),
        ]
    )
    conn.execute = AsyncMock(return_value="DELETE 0")

    result = await worker.process_one_hard_deletion_request(_pool_for(conn))

    assert result is not None
    assert result.status == "hard_deleted"
    executed_sql = [call.args[0] for call in conn.execute.await_args_list]
    assert not any("DELETE FROM deletion_requests" in sql for sql in executed_sql)
    mark_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "UPDATE deletion_requests" in mark_sql


@pytest.mark.asyncio
async def test_worker_hard_delete_respects_status_and_restore_window_guard(monkeypatch):
    monkeypatch.setattr(lifecycle, "_cache", None)
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxContext())
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="DELETE 1")

    result = await worker.process_one_hard_deletion_request(_pool_for(conn))

    assert result is None
    conn.execute.assert_not_awaited()
    dequeue_sql = conn.fetchrow.await_args.args[0]
    assert "status = 'soft_deleted'" in dequeue_sql
    assert "restore_by < NOW()" in dequeue_sql
    assert "FOR UPDATE SKIP LOCKED" in dequeue_sql


@pytest.mark.asyncio
async def test_worker_hard_delete_invalidates_search_and_stats_cache(monkeypatch):
    cache = _FakeCache()
    cache.store["mnemos:search:primed"] = '{"count":1,"memories":[]}'
    cache.store["stats:global"] = "{}"
    cache.store["stats:global:v2"] = "{}"
    monkeypatch.setattr(lifecycle, "_cache", cache)
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxContext())
    conn.fetchrow = AsyncMock(
        side_effect=[
            _soft_deleted_request("tenant-a"),
            _hard_deleted_request("tenant-a"),
        ]
    )
    conn.execute = AsyncMock(return_value="DELETE 0")

    await worker.process_one_hard_deletion_request(_pool_for(conn))

    assert "mnemos:search:primed" in cache.deleted
    assert "stats:global" in cache.deleted
    assert "stats:global:v2" in cache.deleted


def test_hard_delete_sql_order_keeps_fk_children_before_parents():
    labels = [label for label, _table, _sql in worker._HARD_DELETE_SQL]

    assert labels[:5] == [
        "memory_versions",
        "memory_branches",
        "session_messages",
        "session_memory_injections",
        "graeae_audit_log",
    ]
    assert labels[5:9] == [
        "memory_archive",
        "memories",
        "sessions",
        "graeae_consultations",
    ]
    assert labels[9:] == ["kg_triples", "journal", "entities", "state"]
    assert len(labels) == 13


@pytest.mark.asyncio
async def test_worker_verify_pass_sweeps_insert_committed_during_initial_update(monkeypatch):
    monkeypatch.setattr(lifecycle, "_cache", None)
    first_memories_update_started = asyncio.Event()
    finish_first_memories_update = asyncio.Event()
    late_memory = {"visible": False}
    memory_update_calls = 0

    async def execute(sql, *args):
        nonlocal memory_update_calls
        if "UPDATE memories" in sql and "SET deleted_at = NOW()" in sql:
            memory_update_calls += 1
            if memory_update_calls == 1:
                first_memories_update_started.set()
                await finish_first_memories_update.wait()
                return "UPDATE 1"
            if late_memory["visible"]:
                late_memory["visible"] = False
                return "UPDATE 1"
        return "UPDATE 0"

    async def fetchval(sql, *args):
        if "FROM memories" in sql and "deleted_at IS NULL" in sql:
            return 1 if late_memory["visible"] else 0
        return 0

    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxContext())
    conn.fetchrow = AsyncMock(
        side_effect=[
            _confirmed_request("tenant-a"),
            _verifying_request(),
            _marked_request(),
        ]
    )
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock(side_effect=execute)

    task = asyncio.create_task(worker.process_one_deletion_request(_pool_for(conn)))
    await first_memories_update_started.wait()
    late_memory["visible"] = True
    finish_first_memories_update.set()
    result = await task

    assert result is not None
    assert result.status == "soft_deleted"
    assert result.row_counts["memories"] == 2
    assert result.verification_attempts == 2
    assert memory_update_calls == 2
    assert late_memory["visible"] is False


@pytest.mark.asyncio
async def test_memory_read_path_filters_soft_deleted_rows():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    tx = PostgresTransaction(conn, MagicMock())
    visibility = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )

    row = await PostgresMemoryRepository().get_memory(
        tx,
        "mem-soft-deleted",
        visibility=visibility,
    )

    assert row is None
    sql = conn.fetchrow.await_args.args[0]
    assert "FROM memories WHERE id=$1 AND deleted_at IS NULL" in sql
