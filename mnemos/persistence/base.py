"""Backend-neutral persistence interfaces for MNEMOS."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, AsyncContextManager, Protocol, runtime_checkable

from mnemos.core.auth_context import UserContext
from mnemos.persistence.types import Row


@runtime_checkable
class Transaction(Protocol):
    """Backend-neutral transaction handle.

    Repository methods accept this protocol instead of exposing driver-specific
    connection objects. Concrete repositories are responsible for translating
    the handle into their backend's private connection/session type.
    """

    async def commit(self) -> None:
        """Commit the transaction."""
        ...

    async def rollback(self) -> None:
        """Rollback the transaction."""
        ...


class MemoryRepository(ABC):
    """Memory row, memory export, and memory DAG read operations."""

    @abstractmethod
    async def assert_memory_readable(self, tx: Transaction, memory_id: str, user: UserContext) -> None:
        ...

    @abstractmethod
    async def fetch_memory_log(
        self,
        tx: Transaction,
        memory_id: str,
        branch: str,
        limit: int,
        user: UserContext,
    ) -> list[Row]:
        ...

    @abstractmethod
    async def fetch_diff_commit_pair(
        self,
        tx: Transaction,
        memory_id: str,
        commit_a: str,
        commit_b: str,
        user: UserContext,
    ) -> tuple[Row | None, Row | None]:
        ...

    @abstractmethod
    async def fetch_checkout_commit(
        self,
        tx: Transaction,
        memory_id: str,
        commit_hash: str,
        user: UserContext,
    ) -> Row | None:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def fetch_referenced_memory_allowlist(
        self,
        tx: Transaction,
        *,
        referenced_ids: Sequence[str],
        scope_owner: str | None = None,
        scope_namespace: str | None = None,
    ) -> list[Row]:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def fetch_memory_by_id(self, tx: Transaction, memory_id: str) -> Row | None:
        ...

    @abstractmethod
    async def set_suppress_version_snapshot(self, tx: Transaction) -> None:
        ...

    @abstractmethod
    async def fetch_versioned_memory_ids(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        ...

    @abstractmethod
    async def fetch_memory_head_checks(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        ...

    @abstractmethod
    async def fetch_memory_context(
        self,
        tx: Transaction,
        query: str,
        user: Any,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        ...


class KGRepository(ABC):
    """Knowledge graph triple persistence."""

    @abstractmethod
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
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def fetch_kg_triple_by_id(self, tx: Transaction, triple_id: str) -> Row | None:
        ...


class VersionRepository(ABC):
    """Memory version persistence and topology lookups."""

    @abstractmethod
    async def fetch_memory_versions_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        hard_limit: int,
    ) -> list[Row]:
        ...

    @abstractmethod
    async def fetch_memory_versions_by_ids(self, tx: Transaction, version_ids: Sequence[str]) -> list[Row]:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def fetch_memory_version_by_id(self, tx: Transaction, version_id: str) -> Row | None:
        ...


class BranchRepository(ABC):
    """Memory branch persistence."""

    @abstractmethod
    async def create_memory_branch(
        self,
        tx: Transaction,
        memory_id: str,
        name: str,
        from_commit: str | None,
        user: UserContext,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    async def delete_memory_branches_for_memories(self, tx: Transaction, memory_ids: Sequence[str]) -> None:
        ...

    @abstractmethod
    async def fetch_memory_branch_heads(
        self,
        tx: Transaction,
        memory_ids: Sequence[str],
        *,
        authorized_version_uuids: Sequence[str] | None = None,
    ) -> list[Row]:
        ...

    @abstractmethod
    async def upsert_memory_branch_head(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        branch: str,
        head_version_id: Any,
    ) -> None:
        ...


class CompressionRepository(ABC):
    """Compressed memory variant persistence."""

    @abstractmethod
    async def fetch_compressed_variants_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        hard_limit: int,
    ) -> list[Row]:
        ...

    @abstractmethod
    async def compression_candidate_exists(
        self,
        tx: Transaction,
        *,
        candidate_id: str,
        memory_id: str,
        owner_id: str,
    ) -> bool:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def fetch_compressed_variant_by_memory_id(self, tx: Transaction, memory_id: str) -> Row | None:
        ...


class WebhookRepository(ABC):
    """Webhook persistence surface.

    Webhook SQL has not been extracted into mnemos/db repositories in D.1.
    """


class ConsultationAuditRepository(ABC):
    """OpenAI-compatible gateway and consultation audit persistence lookups."""

    @abstractmethod
    async def fetch_recommended_model(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float,
        quality_floor: float,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        ...

    @abstractmethod
    async def fetch_model_recommendation(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float = 10.0,
        quality_floor: float = 0.85,
    ) -> dict[str, Any] | None:
        ...

    @abstractmethod
    async def lookup_provider_for_model(self, tx: Transaction, model: str) -> str | None:
        ...

    @abstractmethod
    async def fetch_available_models(self, tx: Transaction) -> list[Row]:
        ...

    @abstractmethod
    async def fetch_model_provider(self, tx: Transaction, model_id: str) -> str | None:
        ...


class FederationRepository(ABC):
    """Federation persistence surface.

    Federation SQL has not been extracted into mnemos/db repositories in D.1.
    """


class StateRepository(ABC):
    """State key-value persistence surface.

    State SQL has not been extracted into mnemos/db repositories in D.1.
    """


class PersistenceBackend(ABC):
    """Top-level facade exposing backend-specific repository families."""

    @abstractmethod
    def transactional(self) -> AsyncContextManager[Transaction]:
        """Open a backend-neutral transaction context."""
        ...

    @property
    @abstractmethod
    def memories(self) -> MemoryRepository:
        ...

    @property
    @abstractmethod
    def kg_triples(self) -> KGRepository:
        ...

    @property
    @abstractmethod
    def memory_versions(self) -> VersionRepository:
        ...

    @property
    @abstractmethod
    def memory_branches(self) -> BranchRepository:
        ...

    @property
    @abstractmethod
    def compression(self) -> CompressionRepository:
        ...

    @property
    @abstractmethod
    def webhooks(self) -> WebhookRepository:
        ...

    @property
    @abstractmethod
    def consultations_audit(self) -> ConsultationAuditRepository:
        ...

    @property
    @abstractmethod
    def federation(self) -> FederationRepository:
        ...

    @property
    @abstractmethod
    def state_kv(self) -> StateRepository:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...


@asynccontextmanager
async def null_transaction(tx: Transaction) -> AsyncIterator[Transaction]:
    """Yield an existing transaction without managing its lifecycle."""
    yield tx
