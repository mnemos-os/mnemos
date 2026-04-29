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
