from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import mnemos.core.lifecycle as lifecycle
from mnemos.persistence import (
    BranchRepository,
    CompressionRepository,
    ConsultationAuditRepository,
    FederationRepository,
    KGRepository,
    MemoryRepository,
    PersistenceBackend,
    PostgresBackend,
    PostgresBranchRepository,
    PostgresCompressionRepository,
    PostgresConsultationAuditRepository,
    PostgresFederationRepository,
    PostgresKGRepository,
    PostgresMemoryRepository,
    PostgresStateRepository,
    PostgresTransaction,
    PostgresVersionRepository,
    PostgresWebhookRepository,
    StateRepository,
    Transaction,
    VersionRepository,
    WebhookRepository,
)


class FakeAsyncpgTransaction:
    def __init__(self):
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")


class FakeConnection:
    def __init__(self, raw_tx: FakeAsyncpgTransaction):
        self.raw_tx = raw_tx

    def transaction(self) -> FakeAsyncpgTransaction:
        return self.raw_tx


class FakeAcquire:
    def __init__(self, conn: FakeConnection):
        self.conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self.conn

    async def __aexit__(self, *_exc_info) -> None:
        return None


class FakePool:
    def __init__(self, conn: FakeConnection):
        self.conn = conn
        self.close_calls = 0

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.conn)

    async def close(self) -> None:
        self.close_calls += 1


def _backend_with_raw_tx(raw_tx: FakeAsyncpgTransaction) -> PostgresBackend:
    return PostgresBackend(FakePool(FakeConnection(raw_tx)), SimpleNamespace())


def test_persistence_backend_abc_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        PersistenceBackend()


def test_postgres_backend_exposes_all_repository_properties():
    backend = _backend_with_raw_tx(FakeAsyncpgTransaction())

    assert isinstance(backend.memories, MemoryRepository)
    assert isinstance(backend.kg_triples, KGRepository)
    assert isinstance(backend.memory_versions, VersionRepository)
    assert isinstance(backend.memory_branches, BranchRepository)
    assert isinstance(backend.compression, CompressionRepository)
    assert isinstance(backend.webhooks, WebhookRepository)
    assert isinstance(backend.consultations_audit, ConsultationAuditRepository)
    assert isinstance(backend.federation, FederationRepository)
    assert isinstance(backend.state_kv, StateRepository)


def test_postgres_backend_repositories_are_postgres_adapters():
    backend = _backend_with_raw_tx(FakeAsyncpgTransaction())

    assert isinstance(backend.memories, PostgresMemoryRepository)
    assert isinstance(backend.kg_triples, PostgresKGRepository)
    assert isinstance(backend.memory_versions, PostgresVersionRepository)
    assert isinstance(backend.memory_branches, PostgresBranchRepository)
    assert isinstance(backend.compression, PostgresCompressionRepository)
    assert isinstance(backend.webhooks, PostgresWebhookRepository)
    assert isinstance(backend.consultations_audit, PostgresConsultationAuditRepository)
    assert isinstance(backend.federation, PostgresFederationRepository)
    assert isinstance(backend.state_kv, PostgresStateRepository)


@pytest.mark.asyncio
async def test_postgres_transaction_auto_commit_uses_asyncpg_transaction():
    raw_tx = FakeAsyncpgTransaction()
    backend = _backend_with_raw_tx(raw_tx)

    async with backend.transactional() as tx:
        assert isinstance(tx, Transaction)
        assert isinstance(tx, PostgresTransaction)

    assert raw_tx.calls == ["start", "commit"]


@pytest.mark.asyncio
async def test_postgres_transaction_explicit_rollback_skips_auto_commit():
    raw_tx = FakeAsyncpgTransaction()
    backend = _backend_with_raw_tx(raw_tx)

    async with backend.transactional() as tx:
        await tx.rollback()

    assert raw_tx.calls == ["start", "rollback"]


@pytest.mark.asyncio
async def test_postgres_transaction_exception_rolls_back():
    raw_tx = FakeAsyncpgTransaction()
    backend = _backend_with_raw_tx(raw_tx)

    with pytest.raises(RuntimeError):
        async with backend.transactional():
            raise RuntimeError("boom")

    assert raw_tx.calls == ["start", "rollback"]


def test_get_persistence_backend_returns_lifecycle_singleton(monkeypatch):
    backend = _backend_with_raw_tx(FakeAsyncpgTransaction())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

    assert lifecycle.get_persistence_backend() is backend


def test_get_persistence_backend_raises_when_unavailable(monkeypatch):
    monkeypatch.setattr(lifecycle, "_persistence_backend", None)

    with pytest.raises(HTTPException) as exc:
        lifecycle.get_persistence_backend()

    assert exc.value.status_code == 503
