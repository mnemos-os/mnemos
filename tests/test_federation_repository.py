from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

from mnemos.persistence import PersistenceBackend, PostgresBackend, SqliteBackend


PG_URL = os.environ.get("MNEMOS_TEST_DB")


@dataclass
class BackendCase:
    name: str
    backend: PersistenceBackend
    prefix: str


def _backend_params() -> list[str]:
    params = ["sqlite"]
    if PG_URL:
        params.append("postgres")
    return params


@pytest_asyncio.fixture(params=_backend_params())
async def backend_case(request, tmp_path):
    prefix = f"frepo{uuid.uuid4().hex[:10]}"
    if request.param == "sqlite":
        backend = SqliteBackend(tmp_path / "federation.sqlite3", SimpleNamespace())
        await backend.open()
        try:
            yield BackendCase("sqlite", backend, prefix)
        finally:
            await backend.close()
        return

    pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=2)
    backend = PostgresBackend(pool, SimpleNamespace())
    try:
        yield BackendCase("postgres", backend, prefix)
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM federation_peers WHERE name LIKE $1", f"{prefix}%")
            async with conn.transaction():
                await conn.execute("SET LOCAL mnemos.suppress_version_snapshot = '1'")
                await conn.execute("DELETE FROM memory_branches WHERE memory_id LIKE $1", f"{prefix}%")
                await conn.execute("DELETE FROM memory_versions WHERE owner_id LIKE $1", f"{prefix}%")
                await conn.execute("DELETE FROM memories WHERE owner_id LIKE $1", f"{prefix}%")
        await backend.close()


@pytest.mark.asyncio
async def test_federation_repository_peer_crud_and_sync_log(backend_case: BackendCase):
    peer_name = f"{backend_case.prefix}-peer"
    async with backend_case.backend.transactional() as tx:
        created = await backend_case.backend.federation.create_peer(
            tx,
            name=peer_name,
            base_url=f"https://{peer_name}.example.com",
            auth_token="token",
            namespace_filter=["default"],
            category_filter=["facts"],
            enabled=True,
            sync_interval_secs=300,
            compat_mode="strict",
        )
        peer_id = str(created["id"])
        listed = await backend_case.backend.federation.list_peers(tx)
        fetched = await backend_case.backend.federation.get_peer(tx, peer_id)
        updated = await backend_case.backend.federation.update_peer(tx, peer_id, {"enabled": False})
        log_id = await backend_case.backend.federation.create_sync_log(tx, peer_id, None)
        await backend_case.backend.federation.finish_sync_log(
            tx,
            log_id=log_id,
            memories_pulled=2,
            memories_new=1,
            memories_updated=1,
            error=None,
            cursor_after=datetime.now(timezone.utc),
        )
        logs = await backend_case.backend.federation.fetch_sync_log(tx, peer_id, 10)
        deleted = await backend_case.backend.federation.delete_peer(tx, peer_id)

    assert any(str(row["id"]) == peer_id for row in listed)
    assert fetched is not None
    assert fetched["name"] == peer_name
    assert updated is not None
    assert bool(updated["enabled"]) is False
    assert logs[0]["memories_pulled"] == 2
    assert deleted is True


@pytest.mark.asyncio
async def test_federation_repository_feed_cursor_and_sqlite_compressed_stub(backend_case: BackendCase):
    now = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
    ids = [f"{backend_case.prefix}-fed-a", f"{backend_case.prefix}-fed-b"]
    namespace = f"{backend_case.prefix}-ns"
    async with backend_case.backend.transactional() as tx:
        for idx, memory_id in enumerate(ids):
            await backend_case.backend.memories.insert_memory(
                tx,
                memory_id=memory_id,
                content=f"federated {idx}",
                category="facts",
                subcategory=None,
                metadata_json="{}",
                quality_rating=75,
                owner_id=f"{backend_case.prefix}-owner",
                namespace=namespace,
                permission_mode=644,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content=f"federated {idx}",
                created=now + timedelta(seconds=idx),
                updated=now + timedelta(seconds=idx),
            )
        page1 = await backend_case.backend.federation.feed_query(
            tx,
            since_updated=None,
            since_id=None,
            namespaces=[namespace],
            categories=[],
            limit=1,
            prefer_compressed=False,
        )
        page2 = await backend_case.backend.federation.feed_query(
            tx,
            since_updated=page1[-1]["updated"],
            since_id=page1[-1]["id"],
            namespaces=[namespace],
            categories=[],
            limit=10,
            prefer_compressed=False,
        )
        if backend_case.name == "sqlite":
            with pytest.raises(NotImplementedError):
                await backend_case.backend.federation.feed_query(
                    tx,
                    since_updated=None,
                    since_id=None,
                    namespaces=[namespace],
                    categories=[],
                    limit=10,
                    prefer_compressed=True,
                )

    assert [row["id"] for row in [*page1, *page2]] == ids
