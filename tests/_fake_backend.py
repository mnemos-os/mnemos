"""Fake ``PersistenceBackend`` for handler-level tests.

Captures calls made to ``backend.memories.*`` / ``backend.webhooks.*`` /
``backend.compression.*`` so handler tests can assert that the right
``VisibilityFilter`` and arguments are passed without depending on the
SQL dialect. The fake is NOT functional storage — methods return
configured stubs.

Slice 1d migrated handler dispatch from ``_lc._pool.acquire()`` to
``_lc._persistence_backend.transactional()``. Tests that previously
mocked ``_lc._pool`` with an asyncpg-shaped fake now mock the backend
through this helper.

Use ``install_fake_backend(monkeypatch)`` to wire the backend into
lifecycle for the duration of a test.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock


class _FakeMemoryRepo:
    """Captures calls made by handlers + returns configured stubs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._returns: dict[str, Any] = {}
        self._raises: dict[str, BaseException] = {}

    def configure_return(self, method: str, value: Any) -> None:
        self._returns[method] = value

    def configure_raise(self, method: str, exc: BaseException) -> None:
        self._raises[method] = exc

    def _resolve(self, method: str, default: Any) -> Any:
        if method in self._raises:
            raise self._raises[method]
        return self._returns.get(method, default)

    async def list_memories(
        self, tx, *, visibility, category=None, subcategory=None,
        limit=20, offset=0,
    ):
        self.calls.append((
            "list_memories",
            {
                "visibility": visibility,
                "category": category,
                "subcategory": subcategory,
                "limit": limit,
                "offset": offset,
            },
        ))
        return self._resolve("list_memories", ([], 0))

    async def get_memory(self, tx, memory_id, *, visibility):
        self.calls.append((
            "get_memory",
            {"memory_id": memory_id, "visibility": visibility},
        ))
        return self._resolve("get_memory", None)

    async def update_memory(self, tx, memory_id, *, visibility, fields):
        self.calls.append((
            "update_memory",
            {
                "memory_id": memory_id,
                "visibility": visibility,
                "fields": fields,
            },
        ))
        return self._resolve("update_memory", None)

    async def delete_memory(self, tx, memory_id, *, visibility):
        self.calls.append((
            "delete_memory",
            {"memory_id": memory_id, "visibility": visibility},
        ))
        return self._resolve(
            "delete_memory",
            {
                "id": memory_id,
                "content": "remember this",
                "category": "facts",
                "subcategory": None,
                "owner_id": "alice",
                "namespace": "alice-ns",
            },
        )

    async def semantic_search(
        self, tx, *, embedding, limit, visibility,
        category=None, subcategory=None,
        source_provider=None, source_model=None, source_agent=None,
    ):
        self.calls.append((
            "semantic_search",
            {
                "embedding": list(embedding),
                "limit": limit,
                "visibility": visibility,
                "category": category,
                "subcategory": subcategory,
                "source_provider": source_provider,
                "source_model": source_model,
                "source_agent": source_agent,
            },
        ))
        return self._resolve("semantic_search", [])

    async def fts_search(
        self, tx, *, query, limit, visibility,
        category=None, subcategory=None,
        source_provider=None, source_model=None, source_agent=None,
    ):
        self.calls.append((
            "fts_search",
            {
                "query": query,
                "limit": limit,
                "visibility": visibility,
                "category": category,
                "subcategory": subcategory,
                "source_provider": source_provider,
                "source_model": source_model,
                "source_agent": source_agent,
            },
        ))
        return self._resolve("fts_search", [])

    async def insert_memory(self, tx, **kwargs):
        self.calls.append(("insert_memory", kwargs))
        return self._resolve("insert_memory", kwargs.get("memory_id"))

    async def gather_stats(self, tx):
        from mnemos.persistence.base import MemoryStatsRow
        return self._resolve(
            "gather_stats",
            MemoryStatsRow(
                total_memories=0,
                native_memories=0,
                federated_memories=0,
            ),
        )

    def __getattr__(self, name: str) -> AsyncMock:
        # Catch-all for legacy abstract methods (fetch_memory_log, etc.)
        # that handler-level tests don't exercise. AsyncMock returns a
        # Mock-shaped value so downstream chaining doesn't crash.
        return AsyncMock()


class _FakeWebhookRepo:
    """Captures dispatch_event calls so create/update/delete handler
    tests can assert outbox semantics without a real DB."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._delivery_ids: list[str] = []
        self._raise: BaseException | None = None

    def configure_delivery_ids(self, ids: list[str]) -> None:
        self._delivery_ids = list(ids)

    def configure_raise(self, exc: BaseException) -> None:
        self._raise = exc

    async def dispatch_event(
        self, tx, event_type, payload, *, owner_id=None, namespace=None,
    ):
        self.calls.append((
            "dispatch_event",
            {
                "event_type": event_type,
                "payload": payload,
                "owner_id": owner_id,
                "namespace": namespace,
            },
        ))
        if self._raise is not None:
            raise self._raise
        return list(self._delivery_ids)

    def __getattr__(self, name: str) -> AsyncMock:
        return AsyncMock()


class _FakeCompressionRepo:
    def __init__(self) -> None:
        self._stats = None

    def configure_stats(self, value: Any) -> None:
        self._stats = value

    async def gather_stats(self, tx):
        from mnemos.persistence.base import CompressionStatsRow
        return self._stats or CompressionStatsRow(
            total_compressions=0,
            average_compression_ratio=None,
            unreviewed_compressions=0,
        )

    def __getattr__(self, name: str) -> AsyncMock:
        return AsyncMock()


class _FakeRepo:
    """Catch-all for repos handlers don't currently exercise."""

    def __getattr__(self, name: str) -> AsyncMock:
        return AsyncMock()


class FakeBackend:
    """Backend-shaped stub for handler-level tests.

    Not a full ``PersistenceBackend`` instance — does not subclass the
    ABC because tests don't need ABC enforcement and the ABC has many
    abstract methods irrelevant here. Handlers only use
    ``backend.transactional()``, ``backend.memories.*``,
    ``backend.compression.*``, and ``backend.webhooks.*``; everything
    else short-circuits to ``AsyncMock`` via ``__getattr__``.

    Tracks ``commits`` and ``rollbacks`` so atomicity tests (e.g.
    webhook outbox) can verify that an exception inside the
    ``transactional()`` block tripped a rollback rather than letting
    a partial write commit.
    """

    def __init__(self) -> None:
        self.memories = _FakeMemoryRepo()
        self.compression = _FakeCompressionRepo()
        self.webhooks = _FakeWebhookRepo()
        self.kg_triples = _FakeRepo()
        self.memory_versions = _FakeRepo()
        self.memory_branches = _FakeRepo()
        self.consultations_audit = _FakeRepo()
        self.federation = _FakeRepo()
        self.state_kv = _FakeRepo()
        self.commits = 0
        self.rollbacks = 0

    @asynccontextmanager
    async def transactional(self):
        tx = SimpleNamespace(_fake=True, conn=SimpleNamespace())
        try:
            yield tx
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    async def close(self) -> None:
        return None


class _PoolBackedTx:
    def __init__(self, conn):
        self.conn = conn

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _PoolBackedMemoryRepo:
    def __init__(self, pool) -> None:
        self._pool = pool

    def _visible(self, row: dict[str, Any], visibility) -> bool:
        from mnemos.persistence.visibility import VisibilityScope

        if visibility.namespace is not None and row.get("namespace") != visibility.namespace:
            return False
        if visibility.scope == VisibilityScope.ROOT_BYPASS:
            return True
        if row.get("owner_id") == visibility.user_id:
            return True
        if visibility.scope == VisibilityScope.OWN_ONLY:
            return False
        mode = int(row.get("permission_mode") or 0)
        world_readable = mode % 10 >= 4
        group_readable = (mode // 10) % 10 >= 4 and row.get("group_id") in set(visibility.group_ids)
        return world_readable or group_readable

    def _rows(self, visibility, *, category=None, subcategory=None) -> list[dict[str, Any]]:
        rows = [
            row for row in self._pool.state["memories"].values()
            if self._visible(row, visibility)
        ]
        if category is not None:
            rows = [row for row in rows if row.get("category") == category]
        if subcategory is not None:
            rows = [row for row in rows if row.get("subcategory") == subcategory]
        return sorted(rows, key=lambda row: row.get("created"), reverse=True)

    async def list_memories(
        self, tx, *, visibility, category=None, subcategory=None, limit=20, offset=0,
    ):
        rows = self._rows(visibility, category=category, subcategory=subcategory)
        return rows[offset:offset + limit], len(rows)

    async def get_memory(self, tx, memory_id, *, visibility):
        row = self._pool.state["memories"].get(memory_id)
        if row is None or not self._visible(row, visibility):
            return None
        return row

    async def fts_search(
        self, tx, *, query, limit, visibility,
        category=None, subcategory=None, source_provider=None,
        source_model=None, source_agent=None,
    ):
        needle = query.lower()
        rows = [
            row for row in self._rows(visibility, category=category, subcategory=subcategory)
            if needle in row.get("content", "").lower()
        ]
        return rows[:limit]

    async def semantic_search(
        self, tx, *, embedding, limit, visibility,
        category=None, subcategory=None, source_provider=None,
        source_model=None, source_agent=None,
    ):
        rows = self._rows(visibility, category=category, subcategory=subcategory)
        return [{**row, "similarity": 0.99} for row in rows[:limit]]

    async def insert_memory(self, tx, **kwargs):
        import json

        memory_id = kwargs["memory_id"]
        metadata = kwargs.get("metadata_json") or "{}"
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        self._pool.state["memories"][memory_id] = {
            "id": memory_id,
            "content": kwargs["content"],
            "category": kwargs["category"],
            "subcategory": kwargs.get("subcategory"),
            "created": kwargs.get("created"),
            "updated": kwargs.get("updated"),
            "metadata": metadata,
            "quality_rating": kwargs.get("quality_rating"),
            "compressed_content": None,
            "verbatim_content": kwargs.get("verbatim_content") or kwargs["content"],
            "owner_id": kwargs.get("owner_id"),
            "group_id": None,
            "namespace": kwargs.get("namespace"),
            "permission_mode": kwargs.get("permission_mode"),
            "source_model": kwargs.get("source_model"),
            "source_provider": kwargs.get("source_provider"),
            "source_session": kwargs.get("source_session"),
            "source_agent": kwargs.get("source_agent"),
        }
        return memory_id

    async def gather_stats(self, tx):
        from mnemos.persistence.base import MemoryStatsRow

        return MemoryStatsRow(
            total_memories=len(self._pool.state["memories"]),
            native_memories=len(self._pool.state["memories"]),
            federated_memories=0,
        )

    def __getattr__(self, name: str) -> AsyncMock:
        return AsyncMock()


class FakePoolBackedBackend:
    """Postgres-shaped backend facade over the shared conftest FakePool."""

    supports_listen_notify = True
    supports_advisory_locks = True
    supports_row_level_security = True
    supports_pgvector = True

    def __new__(cls, *args, **kwargs):
        from mnemos.persistence.postgres import PostgresBackend

        if cls is FakePoolBackedBackend:
            cls = type(
                "FakePoolBackedPostgresBackend",
                (FakePoolBackedBackend, PostgresBackend),
                {},
            )
        return super(FakePoolBackedBackend, cls).__new__(cls)

    def __init__(self, pool) -> None:
        self._pool = pool
        self._memories = _PoolBackedMemoryRepo(pool)
        self._webhooks = _FakeWebhookRepo()
        self._compression = _FakeCompressionRepo()
        self._kg_triples = _FakeRepo()
        self._memory_versions = _FakeRepo()
        self._memory_branches = _FakeRepo()
        self._consultations_audit = _FakeRepo()
        self._federation = _FakeRepo()
        self._state_kv = _FakeRepo()

    @property
    def memories(self):
        return self._memories

    @property
    def kg_triples(self):
        return self._kg_triples

    @property
    def memory_versions(self):
        return self._memory_versions

    @property
    def memory_branches(self):
        return self._memory_branches

    @property
    def compression(self):
        return self._compression

    @property
    def webhooks(self):
        return self._webhooks

    @property
    def consultations_audit(self):
        return self._consultations_audit

    @property
    def federation(self):
        return self._federation

    @property
    def state_kv(self):
        return self._state_kv

    @asynccontextmanager
    async def transactional(self):
        pool = self._pool
        if pool is None:
            import mnemos.core.lifecycle as lc

            pool = lc._pool
        async with pool.acquire() as conn:
            yield _PoolBackedTx(conn)

    async def close(self) -> None:
        return None


def install_fake_backend(monkeypatch, *, rls_enabled: bool = False) -> FakeBackend:
    """Wire a ``FakeBackend`` into ``mnemos.core.lifecycle`` for the
    duration of the test.

    Sets ``_pool=None`` so the legacy code paths cannot accidentally
    fire, and ``_cache=None`` so handlers skip Redis. Tests that
    exercise cache invalidation can monkeypatch ``_cache`` themselves.
    """
    import mnemos.core.lifecycle as lc
    backend = FakeBackend()
    monkeypatch.setattr(lc, "_pool", None)
    monkeypatch.setattr(lc, "_persistence_backend", backend)
    monkeypatch.setattr(lc, "_rls_enabled", rls_enabled)
    monkeypatch.setattr(lc, "_cache", None)
    return backend
