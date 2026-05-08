from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return False


class _FeedConn:
    def __init__(self, row):
        self.row = row
        self.queries: list[str] = []

    async def fetch(self, query: str, *args):
        self.queries.append(query)
        return [self.row]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _NoopRawTx:
    async def commit(self):
        return None

    async def rollback(self):
        return None


class _Backend:
    def __init__(self, pool):
        from mnemos.persistence.postgres import PostgresFederationRepository

        self.pool = pool
        self.federation = PostgresFederationRepository()

    @asynccontextmanager
    async def transactional(self):
        from mnemos.persistence.postgres import PostgresTransaction

        yield PostgresTransaction(self.pool.conn, _NoopRawTx())


@pytest.mark.asyncio
async def test_feed_emits_consolidation_tombstone(monkeypatch):
    import mnemos.core.lifecycle as lc
    from mnemos.api.routes import federation as handler

    consolidated_at = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    conn = _FeedConn({
        "type": "consolidation",
        "id": "duplicate",
        "content": None,
        "category": None,
        "subcategory": None,
        "metadata": None,
        "quality_rating": None,
        "verbatim_content": None,
        "owner_id": None,
        "namespace": "default",
        "permission_mode": None,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "created": consolidated_at,
        "updated": consolidated_at,
        "archived_at": None,
        "consolidated_into": "canonical",
        "consolidated_at": consolidated_at,
        "compressed_content": None,
    })
    pool = _Pool(conn)
    monkeypatch.setattr(lc, "_pool", pool)
    monkeypatch.setattr(lc, "_persistence_backend", _Backend(pool))

    response = await handler.federation_feed(
        None, None, since=None, namespace=None, category=None, limit=10
    )

    [event] = response.memories
    assert event.type == "consolidation"
    assert event.id == "duplicate"
    assert event.consolidated_into == "canonical"
    assert event.consolidated_at == consolidated_at.isoformat()
    assert "UNION ALL" in conn.queries[0]
    assert "m.consolidated_into IS NOT NULL" in conn.queries[0]


class _StoreConn:
    def __init__(self):
        self.memories = {
            "fed:peer:duplicate": {"id": "fed:peer:duplicate", "metadata": {}, "deleted_at": None},
            "fed:peer:canonical": {"id": "fed:peer:canonical", "metadata": {}, "deleted_at": None},
        }

    async def execute(self, query: str, *args):
        local_id, canonical_id, consolidated_at, remote_id, remote_canonical, peer_name = args
        row = self.memories.get(local_id)
        if row is None or canonical_id not in self.memories:
            return "UPDATE 0"
        row["consolidated_into"] = canonical_id
        row["consolidated_at"] = consolidated_at
        row["permission_mode"] = 400
        row["metadata"]["federation_consolidation"] = {
            "remote_id": remote_id,
            "remote_consolidated_into": remote_canonical,
            "peer": peer_name,
        }
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_peer_applies_consolidation_redirect_for_imported_duplicate():
    from mnemos.domain.federation import _store_memories
    from mnemos.persistence.postgres import PostgresFederationRepository, PostgresTransaction

    conn = _StoreConn()

    new_n, upd_n = await _store_memories(
        PostgresFederationRepository(),
        PostgresTransaction(conn, _NoopRawTx()),
        "peer",
        [{
            "type": "consolidation",
            "id": "duplicate",
            "consolidated_into": "canonical",
            "consolidated_at": "2026-05-02T12:00:00Z",
        }],
    )

    assert new_n == 0
    assert upd_n == 1
    duplicate = conn.memories["fed:peer:duplicate"]
    assert duplicate["consolidated_into"] == "fed:peer:canonical"
    assert duplicate["permission_mode"] == 400
    assert duplicate["metadata"]["federation_consolidation"]["remote_id"] == "duplicate"
