from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
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
    prefix = f"state_repo_{request.param}_{uuid.uuid4().hex[:10]}"
    if request.param == "sqlite":
        backend = SqliteBackend(tmp_path / "state.sqlite3", SimpleNamespace())
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
            await conn.execute("DELETE FROM state WHERE owner_id LIKE $1", f"{prefix}%")
        await backend.close()


@pytest.mark.asyncio
async def test_state_repository_namespace_lifecycle(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        first = await backend_case.backend.state_kv.set(
            tx,
            "alpha",
            '{"n": 1}',
            owner_id=owner,
            namespace="ns-a",
        )
        second = await backend_case.backend.state_kv.set(
            tx,
            "alpha",
            '{"n": 2}',
            owner_id=owner,
            namespace="ns-a",
        )
        row = await backend_case.backend.state_kv.get(
            tx,
            "alpha",
            owner_id=owner,
            namespace="ns-a",
        )
        listed = await backend_case.backend.state_kv.list_namespace(
            tx,
            owner_id=owner,
            namespace="ns-a",
        )
        deleted = await backend_case.backend.state_kv.delete(
            tx,
            "alpha",
            owner_id=owner,
            namespace="ns-a",
        )
        missing = await backend_case.backend.state_kv.get(
            tx,
            "alpha",
            owner_id=owner,
            namespace="ns-a",
        )

    assert first is not None
    assert second is not None
    assert int(second["version"]) >= int(first["version"])
    assert row["value"] == '{"n": 2}'
    assert [entry["key"] for entry in listed] == ["alpha"]
    assert deleted is True
    assert missing is None


@pytest.mark.asyncio
async def test_state_repository_delete_namespace_counts_rows(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        await backend_case.backend.state_kv.set(tx, "a", "1", owner_id=owner, namespace="bulk")
        await backend_case.backend.state_kv.set(tx, "b", "2", owner_id=owner, namespace="bulk")
        await backend_case.backend.state_kv.set(tx, "c", "3", owner_id=owner, namespace="other")

        deleted = await backend_case.backend.state_kv.delete_namespace(
            tx,
            owner_id=owner,
            namespace="bulk",
        )
        remaining = await backend_case.backend.state_kv.list_namespace(
            tx,
            owner_id=owner,
            namespace="other",
        )

    assert deleted == 2
    assert [entry["key"] for entry in remaining] == ["c"]
