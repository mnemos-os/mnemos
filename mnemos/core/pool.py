"""asyncpg pool wrapper with transaction and retry helpers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional, TypeVar

import asyncpg

from mnemos.core.config import get_settings

DEFAULT_ACQUIRE_TIMEOUT = get_settings().runtime.pool_acquire_timeout
RETRY_DELAY_SECONDS = 0.05

_TRANSIENT_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.ConnectionDoesNotExistError,
    ConnectionResetError,
)


# Pool exhaustion / acquire timeout / connection loss are
# infrastructure-class errors that callers must distinguish from
# content/processing failures: a contest-queue row whose
# ``acquire()`` timed out is RETRYABLE — the row's content didn't
# fail, the DB is wedged. Callers that mark rows failed on every
# Exception need to consult this predicate so they don't convert
# transient pool pressure into terminal data state.
INFRASTRUCTURE_ERRORS: tuple[type[BaseException], ...] = (
    *_TRANSIENT_ERRORS,
    asyncio.TimeoutError,
)


def is_infrastructure_error(exc: BaseException) -> bool:
    """True if the exception is an asyncpg connection-loss / pool-
    timeout class error rather than a content / processing failure.

    Use to gate broad-except handlers that would otherwise mark
    durable state (e.g., ``memory_compression_queue.failed``) — pool
    timeouts must not be treated as terminal content failures.
    """
    return isinstance(exc, INFRASTRUCTURE_ERRORS)


class TimeoutPool:
    """Thin proxy around an asyncpg pool that injects a default
    acquire timeout on every ``.acquire()`` call.

    Background. ``PoolManager.acquire()`` already applies
    ``timeout=DEFAULT_ACQUIRE_TIMEOUT`` so every code path that goes
    through ``get_pool_manager().acquire()`` is bounded under pool
    pressure. But many legacy hot paths still call
    ``_lc._pool.acquire()`` directly; those calls inherit asyncpg's
    default of ``None`` (wait forever) and pile up indefinitely
    when the pool is exhausted, snowballing latency across the
    fleet.

    Wrapping the raw pool at lifecycle creation with this proxy
    closes the gap uniformly — every ``.acquire()`` call on
    ``_lc._pool`` now inherits the configured timeout — without
    requiring a migration of the 86+ direct call sites. Callers
    that DO need a different timeout still pass it explicitly and
    the proxy honours their value.

    Everything else on the pool (release, terminate, close,
    fetchval, transactional helpers, etc.) is delegated via
    ``__getattr__``.
    """

    def __init__(self, pool: asyncpg.Pool, *, default_timeout: float | None = None):
        self._pool = pool
        self._default_timeout = (
            default_timeout if default_timeout is not None else DEFAULT_ACQUIRE_TIMEOUT
        )

    def acquire(self, *, timeout: float | None = None):
        """Acquire a connection. Applies the default timeout when
        the caller does not specify one explicitly. Returns the
        same context-manager shape as ``asyncpg.Pool.acquire``."""
        effective = timeout if timeout is not None else self._default_timeout
        return self._pool.acquire(timeout=effective)

    def __getattr__(self, name: str) -> Any:
        # Delegate every non-acquire attribute (release, close,
        # terminate, _holders, get_size, etc.) to the wrapped pool.
        return getattr(self._pool, name)


def wrap_pool_with_timeout(
    pool: asyncpg.Pool, *, default_timeout: float | None = None,
) -> "TimeoutPool":
    """Wrap a freshly-created ``asyncpg.Pool`` with the
    timeout-injecting proxy so legacy raw ``.acquire()`` call sites
    inherit the default acquire timeout."""
    return TimeoutPool(pool, default_timeout=default_timeout)

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
