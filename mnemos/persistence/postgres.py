"""Postgres persistence backend shell.

D.1 keeps the existing mnemos/db repository functions as the implementation
source of truth. These classes only adapt their asyncpg connection parameters
to the backend-neutral persistence interfaces.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from mnemos.core.auth_context import UserContext
from mnemos.db import mcp_repo, openai_compat_repo, portability_repo
from mnemos.persistence.base import (
    BranchRepository,
    CompressionRepository,
    ConsultationAuditRepository,
    FederationRepository,
    KGRepository,
    MemoryRepository,
    PersistenceBackend,
    StateRepository,
    Transaction,
    VersionRepository,
    WebhookRepository,
)
from mnemos.persistence.types import Row


class PostgresTransaction:
    """Transaction wrapper that keeps asyncpg private to the Postgres adapter."""

    def __init__(self, conn: asyncpg.Connection, tx: Any):
        self._conn = conn
        self._tx = tx
        self._closed = False

    @property
    def conn(self) -> asyncpg.Connection:
        return self._conn

    @property
    def closed(self) -> bool:
        return self._closed

    async def commit(self) -> None:
        if self._closed:
            return
        await self._tx.commit()
        self._closed = True

    async def rollback(self) -> None:
        if self._closed:
            return
        await self._tx.rollback()
        self._closed = True


def _postgres_tx(tx: Transaction) -> PostgresTransaction:
    if not isinstance(tx, PostgresTransaction):
        raise TypeError("Postgres repositories require a PostgresTransaction")
    return tx


class PostgresMemoryRepository(MemoryRepository):
    async def assert_memory_readable(self, tx: Transaction, memory_id: str, user: UserContext) -> None:
        await mcp_repo.assert_memory_readable(_postgres_tx(tx).conn, memory_id, user)

    async def fetch_memory_log(
        self,
        tx: Transaction,
        memory_id: str,
        branch: str,
        limit: int,
        user: UserContext,
    ) -> list[Row]:
        return await mcp_repo.fetch_memory_log(_postgres_tx(tx).conn, memory_id, branch, limit, user)

    async def fetch_diff_commit_pair(
        self,
        tx: Transaction,
        memory_id: str,
        commit_a: str,
        commit_b: str,
        user: UserContext,
    ) -> tuple[Row | None, Row | None]:
        return await mcp_repo.fetch_diff_commit_pair(_postgres_tx(tx).conn, memory_id, commit_a, commit_b, user)

    async def fetch_checkout_commit(
        self,
        tx: Transaction,
        memory_id: str,
        commit_hash: str,
        user: UserContext,
    ) -> Row | None:
        return await mcp_repo.fetch_checkout_commit(_postgres_tx(tx).conn, memory_id, commit_hash, user)

    async def fetch_memory_export(
        self,
        tx: Transaction,
        *,
        effective_owner: str | None,
        effective_ns: str | None,
        category: str | None,
        limit: int,
        offset: int,
    ) -> list[Row]:
        return await portability_repo.fetch_memory_export(
            _postgres_tx(tx).conn,
            effective_owner=effective_owner,
            effective_ns=effective_ns,
            category=category,
            limit=limit,
            offset=offset,
        )

    async def fetch_referenced_memory_allowlist(
        self,
        tx: Transaction,
        *,
        referenced_ids: Sequence[str],
        scope_owner: str | None = None,
        scope_namespace: str | None = None,
    ) -> list[Row]:
        return await portability_repo.fetch_referenced_memory_allowlist(
            _postgres_tx(tx).conn,
            referenced_ids=referenced_ids,
            scope_owner=scope_owner,
            scope_namespace=scope_namespace,
        )

    async def insert_memory(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        quality_rating: int,
        owner_id: str,
        namespace: str,
        permission_mode: int,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        created: Any,
        updated: Any,
    ) -> str:
        return await portability_repo.insert_memory(
            _postgres_tx(tx).conn,
            memory_id=memory_id,
            content=content,
            category=category,
            subcategory=subcategory,
            metadata_json=metadata_json,
            quality_rating=quality_rating,
            owner_id=owner_id,
            namespace=namespace,
            permission_mode=permission_mode,
            source_model=source_model,
            source_provider=source_provider,
            source_session=source_session,
            source_agent=source_agent,
            created=created,
            updated=updated,
        )

    async def fetch_memory_by_id(self, tx: Transaction, memory_id: str) -> Row | None:
        return await portability_repo.fetch_memory_by_id(_postgres_tx(tx).conn, memory_id)

    async def set_suppress_version_snapshot(self, tx: Transaction) -> None:
        await portability_repo.set_suppress_version_snapshot(_postgres_tx(tx).conn)

    async def fetch_versioned_memory_ids(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        return await portability_repo.fetch_versioned_memory_ids(_postgres_tx(tx).conn, memory_ids)

    async def fetch_memory_head_checks(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        return await portability_repo.fetch_memory_head_checks(_postgres_tx(tx).conn, memory_ids)

    async def fetch_memory_context(
        self,
        tx: Transaction,
        query: str,
        user: Any,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        _postgres_tx(tx)
        return await openai_compat_repo.fetch_memory_context(query, user, limit=limit)


class PostgresKGRepository(KGRepository):
    async def fetch_kg_triples_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        include_unattached: bool,
        hard_limit: int,
    ) -> list[Row]:
        return await portability_repo.fetch_kg_triples_for_export(
            _postgres_tx(tx).conn,
            memory_ids=memory_ids,
            effective_owner=effective_owner,
            effective_ns=effective_ns,
            include_unattached=include_unattached,
            hard_limit=hard_limit,
        )

    async def insert_kg_triple(
        self,
        tx: Transaction,
        *,
        triple_id: str,
        subject: str,
        predicate: str,
        obj: str,
        subject_type: str | None,
        object_type: str | None,
        valid_from: Any,
        valid_until: Any,
        memory_id: str | None,
        confidence: float | None,
        created: Any,
        owner_id: str,
        namespace: str | None,
    ) -> str:
        return await portability_repo.insert_kg_triple(
            _postgres_tx(tx).conn,
            triple_id=triple_id,
            subject=subject,
            predicate=predicate,
            obj=obj,
            subject_type=subject_type,
            object_type=object_type,
            valid_from=valid_from,
            valid_until=valid_until,
            memory_id=memory_id,
            confidence=confidence,
            created=created,
            owner_id=owner_id,
            namespace=namespace,
        )

    async def fetch_kg_triple_by_id(self, tx: Transaction, triple_id: str) -> Row | None:
        return await portability_repo.fetch_kg_triple_by_id(_postgres_tx(tx).conn, triple_id)


class PostgresVersionRepository(VersionRepository):
    async def fetch_memory_versions_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        hard_limit: int,
    ) -> list[Row]:
        return await portability_repo.fetch_memory_versions_for_export(
            _postgres_tx(tx).conn,
            memory_ids=memory_ids,
            effective_owner=effective_owner,
            effective_ns=effective_ns,
            hard_limit=hard_limit,
        )

    async def fetch_memory_versions_by_ids(self, tx: Transaction, version_ids: Sequence[str]) -> list[Row]:
        return await portability_repo.fetch_memory_versions_by_ids(_postgres_tx(tx).conn, version_ids)

    async def insert_memory_version(
        self,
        tx: Transaction,
        *,
        version_id: str,
        memory_id: str,
        version_num: int,
        content: str,
        category: str | None,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str | None,
        owner_id: str,
        namespace: str | None,
        permission_mode: int | None,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        snapshot_at: Any,
        snapshot_by: str | None,
        change_type: str | None,
        commit_hash: str | None,
        parent_version_id: str | None,
        branch: str | None,
        merge_parents: Any,
    ) -> str:
        return await portability_repo.insert_memory_version(
            _postgres_tx(tx).conn,
            version_id=version_id,
            memory_id=memory_id,
            version_num=version_num,
            content=content,
            category=category,
            subcategory=subcategory,
            metadata_json=metadata_json,
            verbatim_content=verbatim_content,
            owner_id=owner_id,
            namespace=namespace,
            permission_mode=permission_mode,
            source_model=source_model,
            source_provider=source_provider,
            source_session=source_session,
            source_agent=source_agent,
            snapshot_at=snapshot_at,
            snapshot_by=snapshot_by,
            change_type=change_type,
            commit_hash=commit_hash,
            parent_version_id=parent_version_id,
            branch=branch,
            merge_parents=merge_parents,
        )

    async def fetch_memory_version_by_id(self, tx: Transaction, version_id: str) -> Row | None:
        return await portability_repo.fetch_memory_version_by_id(_postgres_tx(tx).conn, version_id)


class PostgresBranchRepository(BranchRepository):
    async def create_memory_branch(
        self,
        tx: Transaction,
        memory_id: str,
        name: str,
        from_commit: str | None,
        user: UserContext,
    ) -> dict[str, Any]:
        return await mcp_repo.create_memory_branch(_postgres_tx(tx).conn, memory_id, name, from_commit, user)

    async def delete_memory_branches_for_memories(self, tx: Transaction, memory_ids: Sequence[str]) -> None:
        await portability_repo.delete_memory_branches_for_memories(_postgres_tx(tx).conn, memory_ids)

    async def fetch_memory_branch_heads(
        self,
        tx: Transaction,
        memory_ids: Sequence[str],
        *,
        authorized_version_uuids: Sequence[str] | None = None,
    ) -> list[Row]:
        return await portability_repo.fetch_memory_branch_heads(
            _postgres_tx(tx).conn,
            memory_ids,
            authorized_version_uuids=authorized_version_uuids,
        )

    async def upsert_memory_branch_head(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        branch: str,
        head_version_id: Any,
    ) -> None:
        await portability_repo.upsert_memory_branch_head(
            _postgres_tx(tx).conn,
            memory_id=memory_id,
            branch=branch,
            head_version_id=head_version_id,
        )


class PostgresCompressionRepository(CompressionRepository):
    async def fetch_compressed_variants_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        hard_limit: int,
    ) -> list[Row]:
        return await portability_repo.fetch_compressed_variants_for_export(
            _postgres_tx(tx).conn,
            memory_ids=memory_ids,
            effective_owner=effective_owner,
            hard_limit=hard_limit,
        )

    async def compression_candidate_exists(
        self,
        tx: Transaction,
        *,
        candidate_id: str,
        memory_id: str,
        owner_id: str,
    ) -> bool:
        return await portability_repo.compression_candidate_exists(
            _postgres_tx(tx).conn,
            candidate_id=candidate_id,
            memory_id=memory_id,
            owner_id=owner_id,
        )

    async def insert_compressed_variant(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        owner_id: str,
        winner_candidate_id: str | None,
        engine_id: str,
        engine_version: str | None,
        compressed_content: str | None,
        compressed_tokens: int | None,
        compression_ratio: float | None,
        quality_score: float | None,
        composite_score: float | None,
        scoring_profile: str | None,
        judge_model: str | None,
        selected_at: Any,
    ) -> str:
        return await portability_repo.insert_compressed_variant(
            _postgres_tx(tx).conn,
            memory_id=memory_id,
            owner_id=owner_id,
            winner_candidate_id=winner_candidate_id,
            engine_id=engine_id,
            engine_version=engine_version,
            compressed_content=compressed_content,
            compressed_tokens=compressed_tokens,
            compression_ratio=compression_ratio,
            quality_score=quality_score,
            composite_score=composite_score,
            scoring_profile=scoring_profile,
            judge_model=judge_model,
            selected_at=selected_at,
        )

    async def fetch_compressed_variant_by_memory_id(self, tx: Transaction, memory_id: str) -> Row | None:
        return await portability_repo.fetch_compressed_variant_by_memory_id(_postgres_tx(tx).conn, memory_id)


class PostgresWebhookRepository(WebhookRepository):
    pass


class PostgresConsultationAuditRepository(ConsultationAuditRepository):
    async def fetch_recommended_model(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float,
        quality_floor: float,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        return await mcp_repo.fetch_recommended_model(_postgres_tx(tx).conn, task_type, cost_budget, quality_floor)

    async def fetch_model_recommendation(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float = 10.0,
        quality_floor: float = 0.85,
    ) -> dict[str, Any] | None:
        _postgres_tx(tx)
        return await openai_compat_repo.fetch_model_recommendation(task_type, cost_budget, quality_floor)

    async def lookup_provider_for_model(self, tx: Transaction, model: str) -> str | None:
        _postgres_tx(tx)
        return await openai_compat_repo.lookup_provider_for_model(model)

    async def fetch_available_models(self, tx: Transaction) -> list[Row]:
        _postgres_tx(tx)
        return await openai_compat_repo.fetch_available_models()

    async def fetch_model_provider(self, tx: Transaction, model_id: str) -> str | None:
        _postgres_tx(tx)
        return await openai_compat_repo.fetch_model_provider(model_id)


class PostgresFederationRepository(FederationRepository):
    pass


class PostgresStateRepository(StateRepository):
    pass


class PostgresBackend(PersistenceBackend):
    """Postgres persistence facade backed by an asyncpg pool."""

    def __init__(self, pool: asyncpg.Pool, settings: Any):
        self._pool = pool
        self._settings = settings
        self._memories = PostgresMemoryRepository()
        self._kg_triples = PostgresKGRepository()
        self._memory_versions = PostgresVersionRepository()
        self._memory_branches = PostgresBranchRepository()
        self._compression = PostgresCompressionRepository()
        self._webhooks = PostgresWebhookRepository()
        self._consultations_audit = PostgresConsultationAuditRepository()
        self._federation = PostgresFederationRepository()
        self._state_kv = PostgresStateRepository()
        self._closed = False

    @property
    def settings(self) -> Any:
        return self._settings

    @asynccontextmanager
    async def transactional(self) -> AsyncIterator[Transaction]:
        acquire_ctx = self._pool.acquire()
        if inspect.isawaitable(acquire_ctx):
            acquire_ctx = await acquire_ctx
        async with acquire_ctx as conn:
            raw_tx = conn.transaction()
            await raw_tx.start()
            tx = PostgresTransaction(conn, raw_tx)
            try:
                yield tx
            except BaseException:
                if not tx.closed:
                    await tx.rollback()
                raise
            else:
                if not tx.closed:
                    await tx.commit()

    @property
    def memories(self) -> MemoryRepository:
        return self._memories

    @property
    def kg_triples(self) -> KGRepository:
        return self._kg_triples

    @property
    def memory_versions(self) -> VersionRepository:
        return self._memory_versions

    @property
    def memory_branches(self) -> BranchRepository:
        return self._memory_branches

    @property
    def compression(self) -> CompressionRepository:
        return self._compression

    @property
    def webhooks(self) -> WebhookRepository:
        return self._webhooks

    @property
    def consultations_audit(self) -> ConsultationAuditRepository:
        return self._consultations_audit

    @property
    def federation(self) -> FederationRepository:
        return self._federation

    @property
    def state_kv(self) -> StateRepository:
        return self._state_kv

    async def close(self) -> None:
        if self._closed:
            return
        close = getattr(self._pool, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        self._closed = True
