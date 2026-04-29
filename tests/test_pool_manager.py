from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock

import asyncpg
import pytest

from mnemos.core import lifecycle
from mnemos.core.pool import DEFAULT_ACQUIRE_TIMEOUT, PoolManager, get_pool_manager

pytestmark = pytest.mark.asyncio


class _AcquireContext:
    def __init__(self, conn: "_FakeConnection"):
        self.conn = conn

    async def __aenter__(self) -> "_FakeConnection":
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: "_FakeConnection"):
        self.conn = conn
        self.acquire_calls: list[dict] = []

    def acquire(self, **kwargs):
        self.acquire_calls.append(kwargs)
        return _AcquireContext(self.conn)


class _FakeConnection:
    def __init__(self):
        self.executes: list[tuple[str, tuple]] = []
        self.fetch_effects = deque()
        self.fetchrow_effects = deque()
        self.fetchval_effects = deque()

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        return "EXECUTE OK"

    async def fetch(self, sql: str, *args):
        effect = self.fetch_effects.popleft() if self.fetch_effects else []
        if isinstance(effect, BaseException):
            raise effect
        return effect

    async def fetchrow(self, sql: str, *args):
        effect = self.fetchrow_effects.popleft() if self.fetchrow_effects else None
        if isinstance(effect, BaseException):
            raise effect
        return effect

    async def fetchval(self, sql: str, *args):
        effect = self.fetchval_effects.popleft() if self.fetchval_effects else None
        if isinstance(effect, BaseException):
            raise effect
        return effect


async def test_acquire_returns_working_connection():
    conn = _FakeConnection()
    conn.fetchval_effects.append(1)
    pool = _FakePool(conn)
    manager = PoolManager(pool)

    async with manager.acquire() as acquired:
        value = await acquired.fetchval("SELECT 1")

    assert acquired is conn
    assert value == 1
    assert pool.acquire_calls == [{"timeout": DEFAULT_ACQUIRE_TIMEOUT}]


async def test_transactional_commits_on_success():
    conn = _FakeConnection()
    manager = PoolManager(_FakePool(conn))

    async with manager.transactional() as tx_conn:
        await tx_conn.execute("UPDATE example SET ok = true")

    assert [sql for sql, _ in conn.executes] == [
        "BEGIN",
        "UPDATE example SET ok = true",
        "COMMIT",
    ]


async def test_transactional_rolls_back_on_raise():
    conn = _FakeConnection()
    manager = PoolManager(_FakePool(conn))

    with pytest.raises(RuntimeError):
        async with manager.transactional():
            raise RuntimeError("boom")

    assert [sql for sql, _ in conn.executes] == ["BEGIN", "ROLLBACK"]


async def test_transactional_read_only_sets_begin_read_only():
    conn = _FakeConnection()
    manager = PoolManager(_FakePool(conn))

    async with manager.transactional(read_only=True):
        pass

    assert [sql for sql, _ in conn.executes] == ["BEGIN READ ONLY", "COMMIT"]


async def test_query_retries_once_on_connection_error_then_succeeds(monkeypatch):
    conn = _FakeConnection()
    conn.fetch_effects.extend(
        [
            asyncpg.PostgresConnectionError("lost connection"),
            [{"id": "row-1"}],
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr("mnemos.core.pool.asyncio.sleep", sleep)
    manager = PoolManager(_FakePool(conn))

    rows = await manager.query("SELECT id FROM example")

    assert rows == [{"id": "row-1"}]
    assert sleep.await_count == 1


async def test_query_does_not_retry_on_unique_violation(monkeypatch):
    conn = _FakeConnection()
    conn.fetch_effects.append(asyncpg.UniqueViolationError("duplicate"))
    sleep = AsyncMock()
    monkeypatch.setattr("mnemos.core.pool.asyncio.sleep", sleep)
    manager = PoolManager(_FakePool(conn))

    with pytest.raises(asyncpg.UniqueViolationError):
        await manager.query("SELECT id FROM example")

    assert sleep.await_count == 0


async def test_fetchrow_returns_none_on_no_match():
    conn = _FakeConnection()
    manager = PoolManager(_FakePool(conn))

    row = await manager.fetchrow("SELECT id FROM example WHERE id=$1", "missing")

    assert row is None


async def test_get_pool_manager_returns_lifecycle_singleton(monkeypatch):
    manager = PoolManager(_FakePool(_FakeConnection()))
    monkeypatch.setattr(lifecycle, "_pool_manager", manager)

    assert get_pool_manager() is manager
