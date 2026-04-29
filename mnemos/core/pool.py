"""asyncpg pool wrapper with transaction and retry helpers."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional, TypeVar

import asyncpg

DEFAULT_ACQUIRE_TIMEOUT = float(os.getenv("MNEMOS_POOL_ACQUIRE_TIMEOUT", "10.0"))
RETRY_DELAY_SECONDS = 0.05

_TRANSIENT_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.ConnectionDoesNotExistError,
    ConnectionResetError,
)

_ISOLATION_LEVELS = {
    "read_uncommitted": "READ UNCOMMITTED",
    "read_committed": "READ COMMITTED",
    "repeatable_read": "REPEATABLE READ",
    "serializable": "SERIALIZABLE",
}

T = TypeVar("T")


class PoolManager:
    """Singleton-style wrapper around an asyncpg pool."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._acquire_timeout = DEFAULT_ACQUIRE_TIMEOUT

    @property
    def pool(self) -> asyncpg.Pool:
        """Return the wrapped asyncpg pool."""
        return self._pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection using the default timeout."""
        try:
            acquire_ctx = self._pool.acquire(timeout=self._acquire_timeout)
        except TypeError:
            # Test fakes often expose a minimal acquire() without asyncpg's
            # timeout keyword; keep the production path timed while preserving
            # the asyncpg-shaped connection contract.
            acquire_ctx = self._pool.acquire()

        async with acquire_ctx as conn:
            yield conn

    @asynccontextmanager
    async def transactional(
        self,
        *,
        isolation: str = "read_committed",
        read_only: bool = False,
    ) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection and manage BEGIN/COMMIT/ROLLBACK."""
        begin_sql = _begin_sql(isolation, read_only)
        async with self.acquire() as conn:
            await conn.execute(begin_sql)
            try:
                yield conn
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            else:
                await conn.execute("COMMIT")

    async def query(self, sql: str, *args: Any, retries: int = 1) -> list[asyncpg.Record]:
        """Fetch rows with one retry by default for transient connection loss."""

        async def _operation(conn: asyncpg.Connection) -> list[asyncpg.Record]:
            return await conn.fetch(sql, *args)

        return await self._run_with_retries(_operation, retries=retries)

    async def execute(self, sql: str, *args: Any, retries: int = 1) -> str:
        """Execute non-fetching SQL with transient-error retry."""

        async def _operation(conn: asyncpg.Connection) -> str:
            return await conn.execute(sql, *args)

        return await self._run_with_retries(_operation, retries=retries)

    async def fetchrow(
        self,
        sql: str,
        *args: Any,
        retries: int = 1,
    ) -> Optional[asyncpg.Record]:
        """Fetch a single row with transient-error retry."""

        async def _operation(conn: asyncpg.Connection) -> Optional[asyncpg.Record]:
            return await conn.fetchrow(sql, *args)

        return await self._run_with_retries(_operation, retries=retries)

    async def fetchval(self, sql: str, *args: Any, retries: int = 1) -> Any:
        """Fetch a single value with transient-error retry."""

        async def _operation(conn: asyncpg.Connection) -> Any:
            return await conn.fetchval(sql, *args)

        return await self._run_with_retries(_operation, retries=retries)

    async def _run_with_retries(
        self,
        operation: Callable[[asyncpg.Connection], Any],
        *,
        retries: int,
    ) -> T:
        max_retries = max(0, retries)
        attempt = 0
        while True:
            try:
                async with self.acquire() as conn:
                    return await operation(conn)
            except _TRANSIENT_ERRORS:
                if attempt >= max_retries:
                    raise
                attempt += 1
                await asyncio.sleep(RETRY_DELAY_SECONDS)


def _begin_sql(isolation: str, read_only: bool) -> str:
    normalized = isolation.strip().lower().replace("-", "_").replace(" ", "_")
    try:
        isolation_sql = _ISOLATION_LEVELS[normalized]
    except KeyError as exc:
        allowed = ", ".join(sorted(_ISOLATION_LEVELS))
        raise ValueError(f"Unsupported isolation level {isolation!r}; expected one of: {allowed}") from exc

    parts = ["BEGIN"]
    if normalized != "read_committed":
        parts.extend(("ISOLATION LEVEL", isolation_sql))
    if read_only:
        parts.append("READ ONLY")
    return " ".join(parts)


def get_pool_manager() -> PoolManager:
    """Return the lifecycle-owned PoolManager singleton."""
    from mnemos.core import lifecycle

    manager = lifecycle._pool_manager
    if manager is None:
        raise RuntimeError("Database pool manager not available")
    return manager
