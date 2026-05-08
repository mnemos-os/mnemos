from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mnemos.api.dependencies import configure_auth
from mnemos.api.routes import memories as memories_handler
import mnemos.core.lifecycle as lifecycle
from mnemos.core.auth_context import UserContext
from mnemos.persistence import (
    BranchRepository,
    CompressionRepository,
    ConsultationAuditRepository,
    FederationRepository,
    KGRepository,
    MemoryRepository,
    PersistenceBackend,
    PostgresBackend,
    SqliteBackend,
    SqliteTransaction,
    StateRepository,
    VersionRepository,
    WebhookRepository,
)
from mnemos.persistence import sqlite as sqlite_persistence
from mnemos.webhooks import lease as webhook_lease
from mnemos.webhooks import types as webhook_types


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
async def backend_case(request, tmp_path, monkeypatch):
    prefix = f"parity_{request.param}_{uuid.uuid4().hex[:10]}"
    old_pool = lifecycle._pool
    if request.param == "sqlite":
        backend = SqliteBackend(tmp_path / "mnemos.sqlite3", SimpleNamespace())
        await backend.open()
        yield BackendCase("sqlite", backend, prefix)
        await backend.close()
        return

    pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=2)
    backend = PostgresBackend(pool, SimpleNamespace())
    monkeypatch.setattr(lifecycle, "_pool", pool)
    try:
        yield BackendCase("postgres", backend, prefix)
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM webhook_deliveries d
                USING webhook_subscriptions s
                WHERE d.subscription_id = s.id AND s.owner_id LIKE $1
                """,
                f"{prefix}%",
            )
            await conn.execute("DELETE FROM webhook_subscriptions WHERE owner_id LIKE $1", f"{prefix}%")
            await conn.execute("DELETE FROM memory_compressed_variants WHERE owner_id LIKE $1", f"{prefix}%")
            await conn.execute("DELETE FROM memory_compression_candidates WHERE owner_id LIKE $1", f"{prefix}%")
            await conn.execute("DELETE FROM kg_triples WHERE owner_id LIKE $1", f"{prefix}%")
            # Memory deletion order: branches deleted first leaves dangling
            # versions referencing missing branch rows. The DELETE trigger
            # on memories then fires and raises "branch main missing" when
            # it looks up the branch HEAD it expects to update. Suppress
            # the version-snapshot trigger for THIS cleanup transaction
            # via SET LOCAL (custom GUC, no superuser required) so the
            # delete-cascade can proceed without re-materialising rows
            # we are deleting.
            async with conn.transaction():
                await conn.execute("SET LOCAL mnemos.suppress_version_snapshot = '1'")
                await conn.execute("DELETE FROM memory_branches WHERE memory_id LIKE $1", f"{prefix}%")
                await conn.execute("DELETE FROM memory_versions WHERE owner_id LIKE $1", f"{prefix}%")
                await conn.execute("DELETE FROM memories WHERE owner_id LIKE $1", f"{prefix}%")
            await conn.execute("DELETE FROM model_registry WHERE provider LIKE $1", f"{prefix}%")
            await conn.execute("DELETE FROM users WHERE id LIKE $1", f"{prefix}%")
        monkeypatch.setattr(lifecycle, "_pool", old_pool)
        await backend.close()


def _root() -> UserContext:
    return UserContext(user_id="root", group_ids=[], role="root", namespace="default", authenticated=True)


def _user(user_id: str, namespace: str = "default", groups: list[str] | None = None) -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=groups or [],
        role="user",
        namespace=namespace,
        authenticated=True,
    )


def _dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def test_visibility_filter_requires_namespace_for_non_root_read():
    from mnemos.persistence.visibility import VisibilityFilter

    with pytest.raises(ValueError, match="requires a namespace"):
        VisibilityFilter.for_read(_user("alice"), namespace=None)


def test_visibility_filter_requires_namespace_for_non_root_mutation():
    from mnemos.persistence.visibility import VisibilityFilter

    with pytest.raises(ValueError, match="requires a namespace"):
        VisibilityFilter.for_mutation(_user("alice"), namespace=None)


async def _raw_execute(case: BackendCase, tx: Any, pg_sql: str, *pg_args: Any, sqlite_sql: str | None = None) -> None:
    if case.name == "sqlite":
        await sqlite_persistence._execute(tx.conn, sqlite_sql or pg_sql, pg_args)
    else:
        await tx.conn.execute(pg_sql, *pg_args)


async def _raw_fetchval(case: BackendCase, tx: Any, pg_sql: str, *pg_args: Any, sqlite_sql: str | None = None) -> Any:
    if case.name == "sqlite":
        return await sqlite_persistence._fetch_val(tx.conn, sqlite_sql or pg_sql, pg_args)
    return await tx.conn.fetchval(pg_sql, *pg_args)


async def _raw_fetch(case: BackendCase, tx: Any, pg_sql: str, *pg_args: Any, sqlite_sql: str | None = None) -> list[Any]:
    if case.name == "sqlite":
        return await sqlite_persistence._fetch_all(tx.conn, sqlite_sql or pg_sql, pg_args)
    return await tx.conn.fetch(pg_sql, *pg_args)


@pytest.mark.asyncio
async def test_schema_capabilities_include_morpheus_and_pantheon_parity(backend_case: BackendCase):
    async with backend_case.backend.transactional() as tx:
        if backend_case.name == "sqlite":
            memories_cols = {
                row["name"]
                for row in await _raw_fetch(
                    backend_case,
                    tx,
                    "",
                    sqlite_sql="PRAGMA table_info(memories)",
                )
            }
            kg_cols = {
                row["name"]
                for row in await _raw_fetch(
                    backend_case,
                    tx,
                    "",
                    sqlite_sql="PRAGMA table_info(kg_triples)",
                )
            }
            run_cols = {
                row["name"]
                for row in await _raw_fetch(
                    backend_case,
                    tx,
                    "",
                    sqlite_sql="PRAGMA table_info(morpheus_runs)",
                )
            }
            tables = {
                row["name"]
                for row in await _raw_fetch(
                    backend_case,
                    tx,
                    "",
                    sqlite_sql="SELECT name FROM sqlite_master WHERE type='table'",
                )
            }
        else:
            memories_cols = {
                row["column_name"]
                for row in await _raw_fetch(
                    backend_case,
                    tx,
                    "SELECT column_name FROM information_schema.columns WHERE table_name='memories'",
                )
            }
            kg_cols = {
                row["column_name"]
                for row in await _raw_fetch(
                    backend_case,
                    tx,
                    "SELECT column_name FROM information_schema.columns WHERE table_name='kg_triples'",
                )
            }
            run_cols = {
                row["column_name"]
                for row in await _raw_fetch(
                    backend_case,
                    tx,
                    "SELECT column_name FROM information_schema.columns WHERE table_name='morpheus_runs'",
                )
            }
            tables = {
                row["table_name"]
                for row in await _raw_fetch(
                    backend_case,
                    tx,
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
                )
            }

    assert {"consolidated_into", "consolidated_at", "triples_extracted_at"} <= memories_cols
    assert "extracted_by_run_id" in kg_cols
    assert {
        "memories_consolidated",
        "clusters_consolidated",
        "triples_extracted",
        "memories_processed_for_extraction",
    } <= run_cols
    assert "pantheon_routing_audit" in tables
    assert "morpheus_extract_run_memories" in tables


async def _ensure_user(case: BackendCase, tx: Any, user_id: str, namespace: str = "default") -> None:
    if case.name == "sqlite":
        await _raw_execute(
            case,
            tx,
            "",
            user_id,
            user_id,
            namespace,
            sqlite_sql=(
                "INSERT OR IGNORE INTO users (id, username, role, namespace) "
                "VALUES (?, ?, 'user', ?)"
            ),
        )
    else:
        await _raw_execute(
            case,
            tx,
            "INSERT INTO users (id, username, role, namespace) VALUES ($1, $2, 'user', $3) "
            "ON CONFLICT (id) DO NOTHING",
            user_id,
            user_id,
            namespace,
        )


async def _insert_memory(
    case: BackendCase,
    tx: Any,
    *,
    suffix: str = "mem",
    content: str = "alpha persistence memory",
    category: str = "solutions",
    owner_id: str | None = None,
    namespace: str = "default",
    permission_mode: int = 600,
) -> str:
    memory_id = f"{case.prefix}-{suffix}-{uuid.uuid4().hex[:8]}"
    await case.backend.memories.insert_memory(
        tx,
        memory_id=memory_id,
        content=content,
        category=category,
        subcategory=None,
        metadata_json='{"source":"parity"}',
        quality_rating=75,
        owner_id=owner_id or f"{case.prefix}-owner",
        namespace=namespace,
        permission_mode=permission_mode,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=content,
        created=None,
        updated=None,
    )
    return memory_id


async def _insert_version(
    case: BackendCase,
    tx: Any,
    memory_id: str,
    *,
    version_num: int,
    content: str,
    branch: str = "main",
    parent_version_id: str | None = None,
    merge_parents: list[str] | None = None,
    owner_id: str | None = None,
    namespace: str = "default",
    permission_mode: int = 600,
) -> tuple[str, str]:
    version_id = str(uuid.uuid4())
    commit_hash = f"{case.prefix}-{branch}-{version_num}-{uuid.uuid4().hex[:8]}"
    await case.backend.memory_versions.insert_memory_version(
        tx,
        version_id=version_id,
        memory_id=memory_id,
        version_num=version_num,
        content=content,
        category="solutions",
        subcategory=None,
        metadata_json='{"source":"parity"}',
        verbatim_content=content,
        owner_id=owner_id or f"{case.prefix}-owner",
        namespace=namespace,
        permission_mode=permission_mode,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        snapshot_at=None,
        snapshot_by="tester",
        change_type="update" if version_num > 1 else "create",
        commit_hash=commit_hash,
        parent_version_id=parent_version_id,
        branch=branch,
        merge_parents=merge_parents or [],
    )
    return version_id, commit_hash


async def _seed_versioned_memory(case: BackendCase, tx: Any) -> tuple[str, str, str]:
    # The PG memory INSERT trigger (mnemos_version_snapshot) already
    # creates a v1 row + memory_branches main HEAD on every memory
    # INSERT (db/migrations_v3_5_trigger_same_memory_parent.sql:64-85).
    # The test wants to seed v1 with a known version_id, so isolate
    # the suppress in a SAVEPOINT scope so it does not leak to the
    # rest of the test transaction. Without this, a later
    # _insert_memory in the same test would also skip the trigger
    # and mask real failures the parity suite exists to catch.
    if case.name == "postgres":
        await tx.conn.execute("SAVEPOINT mnemos_seed_suppress")
        try:
            await case.backend.memories.set_suppress_version_snapshot(tx)
            memory_id = await _insert_memory(case, tx)
        except BaseException:
            await tx.conn.execute("ROLLBACK TO SAVEPOINT mnemos_seed_suppress")
            raise
        # Commit the savepoint, but reset the GUC explicitly so the
        # outer transaction's snapshot trigger fires on subsequent
        # inserts. RESET drops the SET LOCAL value back to the session
        # default (unset/empty), which re-arms the trigger.
        await tx.conn.execute("RELEASE SAVEPOINT mnemos_seed_suppress")
        await tx.conn.execute("RESET mnemos.suppress_version_snapshot")
    else:
        # SQLite has no version trigger; suppression is a no-op here
        # and there is nothing to scope.
        memory_id = await _insert_memory(case, tx)

    version_id, commit_hash = await _insert_version(case, tx, memory_id, version_num=1, content="v1 content")
    await case.backend.memory_branches.upsert_memory_branch_head(
        tx,
        memory_id=memory_id,
        branch="main",
        head_version_id=version_id,
    )
    return memory_id, version_id, commit_hash


@pytest.mark.asyncio
async def test_backend_exposes_all_repository_properties(backend_case: BackendCase):
    backend = backend_case.backend
    assert isinstance(backend.memories, MemoryRepository)
    assert isinstance(backend.kg_triples, KGRepository)
    assert isinstance(backend.memory_versions, VersionRepository)
    assert isinstance(backend.memory_branches, BranchRepository)
    assert isinstance(backend.compression, CompressionRepository)
    assert isinstance(backend.webhooks, WebhookRepository)
    assert isinstance(backend.consultations_audit, ConsultationAuditRepository)
    assert isinstance(backend.federation, FederationRepository)
    assert isinstance(backend.state_kv, StateRepository)


@pytest.mark.asyncio
async def test_postgres_transactional_uses_pool_acquire_context_manager():
    class TxStub:
        def __init__(self) -> None:
            self.started = False
            self.committed = False
            self.rolled_back = False

        async def start(self) -> None:
            self.started = True

        async def commit(self) -> None:
            self.committed = True

        async def rollback(self) -> None:
            self.rolled_back = True

    class ConnStub:
        def __init__(self, raw_tx: TxStub) -> None:
            self.raw_tx = raw_tx

        def transaction(self) -> TxStub:
            return self.raw_tx

    class AcquireContextStub:
        def __init__(self, conn: ConnStub) -> None:
            self.conn = conn
            self.entered = False
            self.exited = False

        async def __aenter__(self) -> ConnStub:
            self.entered = True
            return self.conn

        async def __aexit__(self, exc_type, exc, tb) -> None:
            self.exited = True

        def __await__(self):
            async def _await_conn() -> ConnStub:
                return self.conn

            return _await_conn().__await__()

    raw_tx = TxStub()
    conn = ConnStub(raw_tx)
    acquire_ctx = AcquireContextStub(conn)
    pool = Mock()
    pool.acquire.return_value = acquire_ctx
    backend = PostgresBackend(pool, SimpleNamespace())

    async with backend.transactional() as tx:
        assert tx.conn is conn
        assert raw_tx.started is True
        assert acquire_ctx.entered is True

    pool.acquire.assert_called_once_with()
    assert acquire_ctx.exited is True
    assert raw_tx.committed is True
    assert raw_tx.rolled_back is False


def test_sqlite_backend_feature_flags(tmp_path):
    backend = SqliteBackend(tmp_path / "flags.sqlite3", SimpleNamespace())
    assert backend.supports_listen_notify is False
    assert backend.supports_advisory_locks is False
    assert backend.supports_row_level_security is False
    assert backend.supports_pgvector is False
    assert backend.uses_sqlite_vec is True
    assert backend.uses_fts5 is True


def test_edge_sqlite_list_memories_serializes_timestamp_text(tmp_path, monkeypatch):
    backend = SqliteBackend(tmp_path / "edge-handler.sqlite3", SimpleNamespace())
    memory_id = f"edge-{uuid.uuid4().hex[:8]}"

    class FakeCache:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

        async def scan_iter(self, *, match: str, count: int):
            if False:
                yield match

    fake_cache = FakeCache()

    @asynccontextmanager
    async def lifespan(app):
        configure_auth(
            {
                "enabled": False,
                "default_namespace": "default",
                "personal_user_id": "edge-owner",
            },
        )
        await backend.open()
        monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
        monkeypatch.setattr(lifecycle, "_cache", fake_cache)
        app.state.persistence_backend = backend
        async with backend.transactional() as tx:
            await backend.memories.insert_memory(
                tx,
                memory_id=memory_id,
                content="edge sqlite timestamp content",
                category="solutions",
                subcategory=None,
                metadata_json='{"source":"edge-test"}',
                quality_rating=75,
                owner_id="edge-owner",
                namespace="default",
                permission_mode=600,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content="edge sqlite timestamp content",
                created="2026-04-29 12:34:56",
                updated="2026-04-29 12:34:56",
            )
        try:
            yield
        finally:
            await backend.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(memories_handler.router)

    with TestClient(app) as client:
        response = client.get("/v1/memories")
        create_response = client.post(
            "/v1/memories",
            json={
                "content": "created memory defaults verbatim",
                "category": "solutions",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["memories"][0]["id"] == memory_id
    assert body["memories"][0]["created"] == "2026-04-29T12:34:56"
    assert body["memories"][0]["verbatim_content"] == "edge sqlite timestamp content"
    assert create_response.status_code == 201
    assert create_response.json()["verbatim_content"] == "created memory defaults verbatim"
    assert "stats:global:v2" in fake_cache.deleted
    assert "stats:global" not in fake_cache.deleted


def test_edge_sqlite_search_memories_serializes_timestamp_text(tmp_path, monkeypatch):
    backend = SqliteBackend(tmp_path / "edge-search-handler.sqlite3", SimpleNamespace())
    memory_id = f"edge-search-{uuid.uuid4().hex[:8]}"

    class FakeCache:
        async def get(self, key: str):
            return None

        async def setex(self, key: str, ttl: int, value: str) -> None:
            return None

    async def _noop_bump_recall_counters(memory_ids: list[str]) -> None:
        return None

    @asynccontextmanager
    async def lifespan(app):
        configure_auth(
            {
                "enabled": False,
                "default_namespace": "default",
                "personal_user_id": "edge-owner",
            },
        )
        await backend.open()
        monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
        monkeypatch.setattr(lifecycle, "_cache", FakeCache())
        monkeypatch.setattr(
            memories_handler,
            "_bump_recall_counters",
            _noop_bump_recall_counters,
        )
        app.state.persistence_backend = backend
        async with backend.transactional() as tx:
            await backend.memories.insert_memory(
                tx,
                memory_id=memory_id,
                content="edge sqlite search timestamp content",
                category="solutions",
                subcategory=None,
                metadata_json='{"source":"edge-search-test"}',
                quality_rating=75,
                owner_id="edge-owner",
                namespace="default",
                permission_mode=600,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content="edge sqlite search timestamp content",
                created="2026-04-29 12:34:56",
                updated="2026-04-29 12:34:56",
            )
        try:
            yield
        finally:
            await backend.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(memories_handler.router)

    with TestClient(app) as client:
        response = client.post(
            "/v1/memories/search",
            json={"query": "sqlite search timestamp", "semantic": False},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["memories"][0]["id"] == memory_id
    assert body["memories"][0]["created"] == "2026-04-29T12:34:56"
    assert body["memories"][0]["verbatim_content"] == "edge sqlite search timestamp content"


@pytest.mark.asyncio
async def test_memory_commit_roundtrip(backend_case: BackendCase):
    async with backend_case.backend.transactional() as tx:
        memory_id = await _insert_memory(backend_case, tx, content="commit roundtrip")
    async with backend_case.backend.transactional() as tx:
        row = await backend_case.backend.memories.fetch_memory_by_id(tx, memory_id)
    assert row["content"] == "commit roundtrip"


@pytest.mark.asyncio
async def test_delete_memory_returns_deleted_row_and_none_for_missing(backend_case: BackendCase):
    from mnemos.persistence.visibility import VisibilityFilter

    owner = f"{backend_case.prefix}-delete-owner"
    async with backend_case.backend.transactional() as tx:
        memory_id = await _insert_memory(
            backend_case,
            tx,
            content="delete returns metadata",
            category="facts",
            owner_id=owner,
            namespace="delete-ns",
        )
        row = await backend_case.backend.memories.delete_memory(
            tx,
            memory_id,
            visibility=VisibilityFilter.for_mutation(
                _user(owner, "delete-ns"),
                namespace="delete-ns",
            ),
        )
        missing = await backend_case.backend.memories.delete_memory(
            tx,
            memory_id,
            visibility=VisibilityFilter.for_mutation(
                _user(owner, "delete-ns"),
                namespace="delete-ns",
            ),
        )

    assert row is not None
    assert row["id"] == memory_id
    assert row["owner_id"] == owner
    assert row["namespace"] == "delete-ns"
    assert row["content"] == "delete returns metadata"
    assert row["category"] == "facts"
    assert row["subcategory"] is None
    assert missing is None


@pytest.mark.asyncio
async def test_transaction_exception_rolls_back_memory(backend_case: BackendCase):
    memory_id = f"{backend_case.prefix}-rollback-{uuid.uuid4().hex[:8]}"
    with pytest.raises(RuntimeError):
        async with backend_case.backend.transactional() as tx:
            await backend_case.backend.memories.insert_memory(
                tx,
                memory_id=memory_id,
                content="rollback",
                category="solutions",
                subcategory=None,
                metadata_json="{}",
                quality_rating=75,
                owner_id=f"{backend_case.prefix}-owner",
                namespace="default",
                permission_mode=600,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content="rollback",
                created=None,
                updated=None,
            )
            raise RuntimeError("boom")
    async with backend_case.backend.transactional() as tx:
        assert await backend_case.backend.memories.fetch_memory_by_id(tx, memory_id) is None


@pytest.mark.asyncio
async def test_explicit_transaction_rollback_discards_memory(backend_case: BackendCase):
    async with backend_case.backend.transactional() as tx:
        memory_id = await _insert_memory(backend_case, tx, content="explicit rollback")
        await tx.rollback()
    async with backend_case.backend.transactional() as tx:
        assert await backend_case.backend.memories.fetch_memory_by_id(tx, memory_id) is None


@pytest.mark.asyncio
async def test_memory_export_filters_category_owner_namespace(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        keep = await _insert_memory(backend_case, tx, suffix="keep", category="solutions", owner_id=owner)
        await _insert_memory(backend_case, tx, suffix="drop-cat", category="notes", owner_id=owner)
        await _insert_memory(backend_case, tx, suffix="drop-owner", category="solutions", owner_id=f"{owner}-other")
        rows = await backend_case.backend.memories.fetch_memory_export(
            tx,
            effective_owner=owner,
            effective_ns="default",
            category="solutions",
            limit=10,
            offset=0,
        )
    assert [row["id"] for row in rows] == [keep]


@pytest.mark.asyncio
async def test_referenced_memory_allowlist_empty_and_scoped(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        memory_id = await _insert_memory(backend_case, tx, owner_id=owner)
        assert await backend_case.backend.memories.fetch_referenced_memory_allowlist(tx, referenced_ids=[]) == []
        rows = await backend_case.backend.memories.fetch_referenced_memory_allowlist(
            tx,
            referenced_ids=[memory_id],
            scope_owner=owner,
            scope_namespace="default",
        )
    assert rows[0]["id"] == memory_id


@pytest.mark.asyncio
async def test_group_read_visibility_uses_unix_group_bits(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    reader = f"{backend_case.prefix}-reader"
    async with backend_case.backend.transactional() as tx:
        memory_id = await _insert_memory(backend_case, tx, owner_id=owner, permission_mode=640)
        if backend_case.name == "sqlite":
            await _raw_execute(
                backend_case,
                tx,
                "",
                "group-a",
                memory_id,
                sqlite_sql="UPDATE memories SET group_id = ? WHERE id = ?",
            )
        else:
            await _raw_execute(
                backend_case,
                tx,
                "UPDATE memories SET group_id = $1 WHERE id = $2",
                "group-a",
                memory_id,
            )
        await backend_case.backend.memories.assert_memory_readable(tx, memory_id, _user(reader, groups=["group-a"]))
        with pytest.raises(PermissionError):
            await backend_case.backend.memories.assert_memory_readable(tx, memory_id, _user(reader))


@pytest.mark.asyncio
async def test_assert_memory_readable_owner_root_and_world(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        private_id, _version_id, _commit = await _seed_versioned_memory(backend_case, tx)
        await backend_case.backend.memories.assert_memory_readable(tx, private_id, _root())
        await backend_case.backend.memories.assert_memory_readable(tx, private_id, _user(owner))
        with pytest.raises(PermissionError):
            await backend_case.backend.memories.assert_memory_readable(tx, private_id, _user(f"{owner}-other"))
        public_id = await _insert_memory(
            backend_case,
            tx,
            suffix="public",
            owner_id=owner,
            permission_mode=604,
        )
        await backend_case.backend.memories.assert_memory_readable(tx, public_id, _user(f"{owner}-other"))


@pytest.mark.asyncio
async def test_version_fetch_by_id_and_ids(backend_case: BackendCase):
    async with backend_case.backend.transactional() as tx:
        memory_id, version_id, _commit = await _seed_versioned_memory(backend_case, tx)
        row = await backend_case.backend.memory_versions.fetch_memory_version_by_id(tx, version_id)
        rows = await backend_case.backend.memory_versions.fetch_memory_versions_by_ids(tx, [version_id])
    assert row["memory_id"] == memory_id
    assert _dicts(rows) == [
        {"id": version_id, "memory_id": memory_id, "owner_id": f"{backend_case.prefix}-owner", "namespace": "default"}
    ]


@pytest.mark.asyncio
async def test_version_export_ordering(backend_case: BackendCase):
    async with backend_case.backend.transactional() as tx:
        memory_id, version_id, _commit = await _seed_versioned_memory(backend_case, tx)
        v2, _commit2 = await _insert_version(
            backend_case,
            tx,
            memory_id,
            version_num=2,
            content="v2 content",
            parent_version_id=version_id,
        )
        rows = await backend_case.backend.memory_versions.fetch_memory_versions_for_export(
            tx,
            memory_ids=[memory_id],
            effective_owner=f"{backend_case.prefix}-owner",
            effective_ns="default",
            hard_limit=10,
        )
    assert [row["id"] for row in rows] == [version_id, v2]


@pytest.mark.asyncio
async def test_branch_create_idempotent_and_missing_commit(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        memory_id, _version_id, commit_hash = await _seed_versioned_memory(backend_case, tx)
        created = await backend_case.backend.memory_branches.create_memory_branch(
            tx,
            memory_id,
            "exp",
            commit_hash,
            _user(owner),
        )
        again = await backend_case.backend.memory_branches.create_memory_branch(
            tx,
            memory_id,
            "exp",
            commit_hash,
            _user(owner),
        )
        missing = await backend_case.backend.memory_branches.create_memory_branch(
            tx,
            memory_id,
            "missing",
            "does-not-exist",
            _user(owner),
        )
    assert created["success"] is True
    assert again["success"] is True
    assert missing["success"] is False


@pytest.mark.asyncio
async def test_delete_memory_branches_for_memories(backend_case: BackendCase):
    async with backend_case.backend.transactional() as tx:
        memory_id, _version_id, _commit = await _seed_versioned_memory(backend_case, tx)
        await backend_case.backend.memory_branches.delete_memory_branches_for_memories(tx, [memory_id])
        count = await _raw_fetchval(
            backend_case,
            tx,
            "SELECT COUNT(*) FROM memory_branches WHERE memory_id = $1",
            memory_id,
            sqlite_sql="SELECT COUNT(*) FROM memory_branches WHERE memory_id = ?",
        )
    assert count == 0


@pytest.mark.asyncio
async def test_dag_log_diff_checkout_branch_heads_and_head_checks(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        memory_id, v1, c1 = await _seed_versioned_memory(backend_case, tx)
        v2, c2 = await _insert_version(
            backend_case,
            tx,
            memory_id,
            version_num=2,
            content="v2 content",
            parent_version_id=v1,
        )
        await backend_case.backend.memory_branches.upsert_memory_branch_head(
            tx,
            memory_id=memory_id,
            branch="main",
            head_version_id=v2,
        )
        v3, _c3 = await _insert_version(
            backend_case,
            tx,
            memory_id,
            version_num=3,
            content="branch content",
            branch="exp",
            parent_version_id=v1,
        )
        await backend_case.backend.memory_branches.upsert_memory_branch_head(
            tx,
            memory_id=memory_id,
            branch="exp",
            head_version_id=v3,
        )
        v4, c4 = await _insert_version(
            backend_case,
            tx,
            memory_id,
            version_num=4,
            content="merged content",
            parent_version_id=v2,
            merge_parents=[v3],
        )
        await backend_case.backend.memory_branches.upsert_memory_branch_head(
            tx,
            memory_id=memory_id,
            branch="main",
            head_version_id=v4,
        )
        log_rows = await backend_case.backend.memories.fetch_memory_log(tx, memory_id, "main", 10, _user(owner))
        diff_a, diff_b = await backend_case.backend.memories.fetch_diff_commit_pair(tx, memory_id, c1, c2, _user(owner))
        checkout = await backend_case.backend.memories.fetch_checkout_commit(tx, memory_id, c4, _user(owner))
        heads = await backend_case.backend.memory_branches.fetch_memory_branch_heads(tx, [memory_id])
        authorized = await backend_case.backend.memory_branches.fetch_memory_branch_heads(
            tx,
            [memory_id],
            authorized_version_uuids=[v3],
        )
        checks = await backend_case.backend.memories.fetch_memory_head_checks(tx, [memory_id])
        versioned = await backend_case.backend.memories.fetch_versioned_memory_ids(tx, [memory_id])
    assert [row["commit_hash"] for row in log_rows] == [c4, c2, c1]
    assert diff_a["content"] == "v1 content"
    assert diff_b["content"] == "v2 content"
    assert checkout["content"] == "merged content"
    assert {row["branch"]: row["head_version_id"] for row in heads} == {"main": v4, "exp": v3}
    assert _dicts(authorized) == [{"memory_id": memory_id, "branch": "exp", "head_version_id": v3}]
    assert checks[0]["head_content"] == "merged content"
    assert _dicts(versioned) == [{"memory_id": memory_id}]


@pytest.mark.asyncio
async def test_kg_insert_fetch_export_and_timeline_search(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        memory_id = await _insert_memory(backend_case, tx, owner_id=owner)
        triple_id = str(uuid.uuid4())
        await backend_case.backend.kg_triples.insert_kg_triple(
            tx,
            triple_id=triple_id,
            subject="Athena",
            predicate="guides",
            obj="Odysseus",
            subject_type="person",
            object_type="person",
            valid_from=None,
            valid_until=None,
            memory_id=memory_id,
            confidence=0.9,
            created=None,
            owner_id=owner,
            namespace="default",
        )
        row = await backend_case.backend.kg_triples.fetch_kg_triple_by_id(tx, triple_id)
        attached = await backend_case.backend.kg_triples.fetch_kg_triples_for_export(
            tx,
            memory_ids=[memory_id],
            effective_owner=owner,
            effective_ns="default",
            include_unattached=False,
            hard_limit=10,
        )
        unattached_id = str(uuid.uuid4())
        await backend_case.backend.kg_triples.insert_kg_triple(
            tx,
            triple_id=unattached_id,
            subject="Hermes",
            predicate="visits",
            obj="Ithaca",
            subject_type=None,
            object_type=None,
            valid_from=None,
            valid_until=None,
            memory_id=None,
            confidence=None,
            created=None,
            owner_id=owner,
            namespace="default",
        )
        with_unattached = await backend_case.backend.kg_triples.fetch_kg_triples_for_export(
            tx,
            memory_ids=[],
            effective_owner=owner,
            effective_ns="default",
            include_unattached=True,
            hard_limit=10,
        )
        if hasattr(backend_case.backend.kg_triples, "search_triples"):
            timeline = await backend_case.backend.kg_triples.search_triples(
                tx,
                "Athena",
                owner_id=owner,
                namespace="default",
            )
        else:
            timeline = attached
    assert row["subject"] == "Athena"
    assert [item["id"] for item in attached] == [triple_id]
    assert [item["id"] for item in with_unattached] == [unattached_id]
    assert timeline[0]["predicate"] == "guides"


@pytest.mark.asyncio
async def test_compression_candidate_variant_and_export(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        memory_id = await _insert_memory(backend_case, tx, owner_id=owner)
        candidate_id = str(uuid.uuid4())
        assert not await backend_case.backend.compression.compression_candidate_exists(
            tx,
            candidate_id=candidate_id,
            memory_id=memory_id,
            owner_id=owner,
        )
        if backend_case.name == "sqlite":
            await _raw_execute(
                backend_case,
                tx,
                "",
                candidate_id,
                memory_id,
                owner,
                "contest",
                "engine",
                sqlite_sql=(
                    "INSERT INTO memory_compression_candidates "
                    "(id, memory_id, owner_id, contest_id, engine_id, "
                    "is_winner, reject_reason) "
                    "VALUES (?, ?, ?, ?, ?, 0, 'inferior')"
                ),
            )
        else:
            # PG schema enforces ``mcc_loser_has_reason``: a row with
            # is_winner=false MUST have a reject_reason. Supply a
            # placeholder for parity-test seeding (the test body is
            # exercising candidate_exists / insert_variant flows, not
            # the loser-reason invariant).
            await _raw_execute(
                backend_case,
                tx,
                "INSERT INTO memory_compression_candidates "
                "(id, memory_id, owner_id, contest_id, engine_id, "
                "is_winner, reject_reason) "
                "VALUES ($1::uuid, $2, $3, $4::uuid, $5, false, 'inferior')",
                candidate_id,
                memory_id,
                owner,
                str(uuid.uuid4()),
                "engine",
            )
        assert await backend_case.backend.compression.compression_candidate_exists(
            tx,
            candidate_id=candidate_id,
            memory_id=memory_id,
            owner_id=owner,
        )
        await backend_case.backend.compression.insert_compressed_variant(
            tx,
            memory_id=memory_id,
            owner_id=owner,
            winner_candidate_id=candidate_id,
            engine_id="engine",
            engine_version="1",
            compressed_content="short",
            compressed_tokens=1,
            compression_ratio=0.5,
            quality_score=0.9,
            composite_score=0.8,
            scoring_profile="balanced",
            judge_model="judge",
            selected_at=None,
        )
        row = await backend_case.backend.compression.fetch_compressed_variant_by_memory_id(tx, memory_id)
        exported = await backend_case.backend.compression.fetch_compressed_variants_for_export(
            tx,
            memory_ids=[memory_id],
            effective_owner=owner,
            hard_limit=10,
        )
    assert row["compressed_content"] == "short"
    assert exported[0]["memory_id"] == memory_id


@pytest.mark.asyncio
async def test_compressed_variant_insert_is_idempotent(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        memory_id = await _insert_memory(backend_case, tx, owner_id=owner)
        for content in ("first", "second"):
            await backend_case.backend.compression.insert_compressed_variant(
                tx,
                memory_id=memory_id,
                owner_id=owner,
                winner_candidate_id=None,
                engine_id="engine",
                engine_version="1",
                compressed_content=content,
                compressed_tokens=1,
                compression_ratio=0.5,
                quality_score=0.9,
                composite_score=0.8,
                scoring_profile="balanced",
                judge_model="judge",
                selected_at=None,
            )
        row = await backend_case.backend.compression.fetch_compressed_variant_by_memory_id(tx, memory_id)
    assert row["compressed_content"] == "first"


@pytest.mark.asyncio
async def test_model_recommendation_lookup_and_available_models(backend_case: BackendCase):
    provider = f"{backend_case.prefix}_provider"
    async with backend_case.backend.transactional() as tx:
        if backend_case.name == "sqlite":
            await _raw_execute(
                backend_case,
                tx,
                "",
                provider,
                "reasoner",
                "Reasoner",
                '["reasoning","logic"]',
                1.0,
                1.0,
                0.95,
                128000,
                sqlite_sql=(
                    "INSERT INTO model_registry "
                    "(provider, model_id, display_name, capabilities, input_cost_per_mtok, "
                    "output_cost_per_mtok, graeae_weight, context_window) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
            )
        else:
            await _raw_execute(
                backend_case,
                tx,
                "INSERT INTO model_registry "
                "(provider, model_id, display_name, capabilities, input_cost_per_mtok, "
                "output_cost_per_mtok, graeae_weight, context_window, available, deprecated) "
                "VALUES ($1, $2, $3, $4::text[], $5, $6, $7, $8, TRUE, FALSE) "
                "ON CONFLICT (provider, model_id) DO UPDATE SET available = TRUE, deprecated = FALSE",
                provider,
                "reasoner",
                "Reasoner",
                ["reasoning", "logic"],
                1.0,
                1.0,
                0.95,
                128000,
            )
    async with backend_case.backend.transactional() as tx:
        recommended, required = await backend_case.backend.consultations_audit.fetch_recommended_model(
            tx,
            "reasoning",
            cost_budget=10.0,
            quality_floor=0.8,
        )
        fallback = await backend_case.backend.consultations_audit.fetch_model_recommendation(
            tx,
            "web_search",
            cost_budget=0.01,
            quality_floor=1.0,
        )
        provider_lookup = await backend_case.backend.consultations_audit.lookup_provider_for_model(tx, "reasoner")
        available = await backend_case.backend.consultations_audit.fetch_available_models(tx)
        model_provider = await backend_case.backend.consultations_audit.fetch_model_provider(tx, "reasoner")
    assert required == ["reasoning", "logic"]
    assert recommended["model_id"] == "reasoner"
    assert fallback["model_id"] == "reasoner"
    assert provider_lookup == provider
    assert any(row["model_id"] == "reasoner" for row in available)
    assert model_provider == provider


@pytest.mark.asyncio
async def test_model_discovery_omits_deprecated_and_unavailable(backend_case: BackendCase):
    provider = f"{backend_case.prefix}_provider"
    async with backend_case.backend.transactional() as tx:
        if backend_case.name == "sqlite":
            await _raw_execute(
                backend_case,
                tx,
                "",
                provider,
                "available",
                "Available",
                '["reasoning"]',
                provider,
                "deprecated",
                "Deprecated",
                '["reasoning"]',
                provider,
                "unavailable",
                "Unavailable",
                '["reasoning"]',
                sqlite_sql=(
                    "INSERT INTO model_registry "
                    "(provider, model_id, display_name, capabilities, deprecated, available) "
                    "VALUES (?, ?, ?, ?, 0, 1), (?, ?, ?, ?, 1, 1), (?, ?, ?, ?, 0, 0)"
                ),
            )
        else:
            await _raw_execute(
                backend_case,
                tx,
                "INSERT INTO model_registry "
                "(provider, model_id, display_name, capabilities, deprecated, available) "
                "VALUES ($1, $2, $3, $4::text[], FALSE, TRUE), "
                "($5, $6, $7, $8::text[], TRUE, TRUE), "
                "($9, $10, $11, $12::text[], FALSE, FALSE) "
                "ON CONFLICT (provider, model_id) DO NOTHING",
                provider,
                "available",
                "Available",
                ["reasoning"],
                provider,
                "deprecated",
                "Deprecated",
                ["reasoning"],
                provider,
                "unavailable",
                "Unavailable",
                ["reasoning"],
            )
    async with backend_case.backend.transactional() as tx:
        available = await backend_case.backend.consultations_audit.fetch_available_models(tx)
    ids = {row["model_id"] for row in available if row["provider"] == provider}
    assert ids == {"available"}


@pytest.mark.asyncio
async def test_memory_context_respects_visibility(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    other = f"{backend_case.prefix}-other"
    async with backend_case.backend.transactional() as tx:
        own_id = await _insert_memory(backend_case, tx, content="context needle own", owner_id=owner)
        await _insert_memory(backend_case, tx, content="context needle private", owner_id=other)
    async with backend_case.backend.transactional() as tx:
        root_rows = await backend_case.backend.memories.fetch_memory_context(tx, "needle", _root(), limit=10)
        user_rows = await backend_case.backend.memories.fetch_memory_context(tx, "needle", _user(owner), limit=10)
    assert own_id in {row["id"] for row in root_rows}
    assert {row["id"] for row in user_rows} == {own_id}


@pytest.mark.asyncio
async def test_webhook_outbox_commits_with_memory(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        await _ensure_user(backend_case, tx, owner)
        subscription_id = str(uuid.uuid4())
        await backend_case.backend.webhooks.insert_subscription(
            tx,
            subscription_id=subscription_id,
            url="https://example.com/webhook",
            events=["memory.created"],
            secret="secret",
            owner_id=owner,
            namespace="default",
        )
        memory_id = await _insert_memory(backend_case, tx, owner_id=owner)
        delivery_ids = await backend_case.backend.webhooks.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": memory_id},
            owner_id=owner,
            namespace="default",
        )
    async with backend_case.backend.transactional() as tx:
        row = await backend_case.backend.memories.fetch_memory_by_id(tx, memory_id)
        deliveries = await backend_case.backend.webhooks.fetch_deliveries(tx, subscription_id)
    assert row is not None
    assert len(delivery_ids) == 1
    assert len(deliveries) == 1


@pytest.mark.asyncio
async def test_webhook_dispatch_event_writes_claimable_writer_revision(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-claim-owner"
    subscription_id = str(uuid.uuid4())
    async with backend_case.backend.transactional() as tx:
        await _ensure_user(backend_case, tx, owner)
        await backend_case.backend.webhooks.insert_subscription(
            tx,
            subscription_id=subscription_id,
            url="https://example.com/webhook",
            events=["memory.created"],
            secret="secret",
            owner_id=owner,
            namespace="default",
        )
        delivery_ids = await backend_case.backend.webhooks.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "claimable"},
            owner_id=owner,
            namespace="default",
        )

    assert len(delivery_ids) == 1
    async with backend_case.backend.transactional() as tx:
        deliveries = await backend_case.backend.webhooks.fetch_deliveries(tx, subscription_id)
    assert len(deliveries) == 1
    assert deliveries[0]["writer_revision"] == webhook_types.NEW_CODE_WRITER_REVISION

    if backend_case.name == "postgres":
        claimed = await webhook_lease._claim_delivery(
            lifecycle._pool,
            delivery_ids[0],
            lease_token=str(uuid.uuid4()),
        )
        assert claimed is not None


@pytest.mark.asyncio
async def test_webhook_event_filter_does_not_enqueue_unmatched_event(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    subscription_id = str(uuid.uuid4())
    async with backend_case.backend.transactional() as tx:
        await _ensure_user(backend_case, tx, owner)
        await backend_case.backend.webhooks.insert_subscription(
            tx,
            subscription_id=subscription_id,
            url="https://example.com/webhook",
            events=["consultation.completed"],
            secret="secret",
            owner_id=owner,
            namespace="default",
        )
        delivery_ids = await backend_case.backend.webhooks.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "nope"},
            owner_id=owner,
            namespace="default",
        )
    assert delivery_ids == []


@pytest.mark.asyncio
async def test_webhook_dispatch_namespace_filter_applies_without_owner(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    sub_ns_a = str(uuid.uuid4())
    sub_ns_b = str(uuid.uuid4())
    async with backend_case.backend.transactional() as tx:
        await _ensure_user(backend_case, tx, owner)
        await backend_case.backend.webhooks.insert_subscription(
            tx,
            subscription_id=sub_ns_a,
            url="https://example.com/webhook-a",
            events=["memory.created"],
            secret="secret",
            owner_id=owner,
            namespace="ns-a",
        )
        await backend_case.backend.webhooks.insert_subscription(
            tx,
            subscription_id=sub_ns_b,
            url="https://example.com/webhook-b",
            events=["memory.created"],
            secret="secret",
            owner_id=owner,
            namespace="ns-b",
        )
        delivery_ids = await backend_case.backend.webhooks.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "namespace-only"},
            owner_id=None,
            namespace="ns-a",
        )
    async with backend_case.backend.transactional() as tx:
        deliveries_ns_a = await backend_case.backend.webhooks.fetch_deliveries(tx, sub_ns_a)
        deliveries_ns_b = await backend_case.backend.webhooks.fetch_deliveries(tx, sub_ns_b)
    assert len(delivery_ids) == 1
    assert len(deliveries_ns_a) == 1
    assert deliveries_ns_b == []


@pytest.mark.asyncio
async def test_webhook_outbox_rolls_back_with_memory(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    memory_id = f"{backend_case.prefix}-rollback-webhook"
    subscription_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError):
        async with backend_case.backend.transactional() as tx:
            await _ensure_user(backend_case, tx, owner)
            await backend_case.backend.webhooks.insert_subscription(
                tx,
                subscription_id=subscription_id,
                url="https://example.com/webhook",
                events=["memory.created"],
                secret="secret",
                owner_id=owner,
                namespace="default",
            )
            await backend_case.backend.memories.insert_memory(
                tx,
                memory_id=memory_id,
                content="rollback webhook",
                category="solutions",
                subcategory=None,
                metadata_json="{}",
                quality_rating=75,
                owner_id=owner,
                namespace="default",
                permission_mode=600,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content="rollback webhook",
                created=None,
                updated=None,
            )
            await backend_case.backend.webhooks.dispatch_event(
                tx,
                "memory.created",
                {"memory_id": memory_id},
                owner_id=owner,
                namespace="default",
            )
            raise RuntimeError("rollback")
    async with backend_case.backend.transactional() as tx:
        assert await backend_case.backend.memories.fetch_memory_by_id(tx, memory_id) is None
        assert await backend_case.backend.webhooks.fetch_deliveries(tx, subscription_id) == []


@pytest.mark.asyncio
async def test_federation_compound_cursor_pages(backend_case: BackendCase):
    async with backend_case.backend.transactional() as tx:
        first = await _insert_memory(backend_case, tx, suffix="fed-a", content="fed a")
        second = await _insert_memory(backend_case, tx, suffix="fed-b", content="fed b")
        page1 = await backend_case.backend.federation.fetch_memory_page(tx, limit=1)
        page2 = await backend_case.backend.federation.fetch_memory_page(
            tx,
            updated_after=page1[-1]["updated"],
            id_after=page1[-1]["id"],
            limit=10,
        )
    assert page1[0]["id"] in {first, second}
    assert {row["id"] for row in [*page1, *page2]} >= {first, second}


@pytest.mark.asyncio
async def test_federation_upsert_peer(backend_case: BackendCase):
    peer_id = str(uuid.uuid4())
    peer_name = f"peer-{uuid.uuid4().hex[:8]}"
    async with backend_case.backend.transactional() as tx:
        await backend_case.backend.federation.upsert_peer(
            tx,
            peer_id=peer_id,
            base_url=f"https://{peer_name}.example.com",
            name=peer_name,
            enabled=True,
        )
        count = await _raw_fetchval(
            backend_case,
            tx,
            "SELECT COUNT(*) FROM federation_peers WHERE id = $1::uuid AND enabled = TRUE",
            peer_id,
            sqlite_sql="SELECT COUNT(*) FROM federation_peers WHERE id = ? AND enabled = 1",
        )
    assert count == 1


@pytest.mark.asyncio
async def test_state_kv_roundtrip_and_delete(backend_case: BackendCase):
    async with backend_case.backend.transactional() as tx:
        await backend_case.backend.state_kv.set(
            tx,
            "answer",
            "42",
            owner_id=f"{backend_case.prefix}-owner",
            namespace="default",
        )
        row = await backend_case.backend.state_kv.get(
            tx,
            "answer",
            owner_id=f"{backend_case.prefix}-owner",
            namespace="default",
        )
        await backend_case.backend.state_kv.delete(
            tx,
            "answer",
            owner_id=f"{backend_case.prefix}-owner",
            namespace="default",
        )
        missing = await backend_case.backend.state_kv.get(
            tx,
            "answer",
            owner_id=f"{backend_case.prefix}-owner",
            namespace="default",
        )
    assert row["value"] == "42"
    assert missing is None


@pytest.mark.asyncio
async def test_state_kv_is_namespace_scoped(backend_case: BackendCase):
    owner = f"{backend_case.prefix}-owner"
    async with backend_case.backend.transactional() as tx:
        await backend_case.backend.state_kv.set(tx, "shared", "a", owner_id=owner, namespace="a")
        await backend_case.backend.state_kv.set(tx, "shared", "b", owner_id=owner, namespace="b")
        row_a = await backend_case.backend.state_kv.get(tx, "shared", owner_id=owner, namespace="a")
        row_b = await backend_case.backend.state_kv.get(tx, "shared", owner_id=owner, namespace="b")
    assert row_a["value"] == "a"
    assert row_b["value"] == "b"


@pytest.mark.asyncio
async def test_sqlite_vector_semantic_search(tmp_path):
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
    # The runtime dim guard (added in the embed-dim slice) requires
    # _expected_embedding_dim to match the fixture vector length. Pass
    # a settings shim with embedding_dim=3 so the 3-D fixture vectors
    # don't trip ValueError in upsert_memory_embedding/semantic_search.
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    backend = SqliteBackend(tmp_path / "vector.sqlite3", settings)
    await backend.open()
    visibility = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace=None,
    )
    async with backend.transactional() as tx:
        near = await _insert_memory(BackendCase("sqlite", backend, "sqlite_vector"), tx, suffix="near", content="near")
        far = await _insert_memory(BackendCase("sqlite", backend, "sqlite_vector"), tx, suffix="far", content="far")
        assert isinstance(tx, SqliteTransaction)
        await backend.memories.upsert_memory_embedding(tx, near, [1.0, 0.0, 0.0])
        await backend.memories.upsert_memory_embedding(tx, far, [0.0, 1.0, 0.0])
        rows = await backend.memories.semantic_search(
            tx, embedding=[0.9, 0.1, 0.0], limit=2, visibility=visibility,
        )
    await backend.close()
    assert [row["id"] for row in rows] == [near, far]
    assert rows[0]["similarity"] > rows[1]["similarity"]


@pytest.mark.asyncio
async def test_sqlite_fts5_relevance_ordering(tmp_path):
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
    backend = SqliteBackend(tmp_path / "fts.sqlite3", SimpleNamespace())
    await backend.open()
    visibility = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace=None,
    )
    case = BackendCase("sqlite", backend, "sqlite_fts")
    async with backend.transactional() as tx:
        best = await _insert_memory(case, tx, suffix="best", content="apollo apollo apollo sqlite")
        other = await _insert_memory(case, tx, suffix="other", content="apollo persistence")
        rows = await backend.memories.fts_search(
            tx, query="apollo", limit=2, visibility=visibility,
        )
    await backend.close()
    assert [row["id"] for row in rows] == [best, other]


def test_sqlite_migration_chain_mirrors_postgres_chain():
    repo_root = os.path.dirname(os.path.dirname(__file__))
    sqlite_dir = os.path.join(repo_root, "db", "migrations_sqlite")
    sqlite_files = sorted(name for name in os.listdir(sqlite_dir) if name.endswith(".sql"))
    assert len(sqlite_files) == len(sqlite_persistence.SQLITE_MIGRATION_FILES)
    assert set(sqlite_files) == set(sqlite_persistence.SQLITE_MIGRATION_FILES)


def test_sqlite_migration_files_are_nonempty():
    repo_root = os.path.dirname(os.path.dirname(__file__))
    sqlite_dir = os.path.join(repo_root, "db", "migrations_sqlite")
    for name in sqlite_persistence.SQLITE_MIGRATION_FILES:
        path = os.path.join(sqlite_dir, name)
        assert os.path.getsize(path) > 0
