"""Backend-neutral persistence interfaces for MNEMOS."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncContextManager, Protocol, runtime_checkable

from mnemos.core.auth_context import UserContext
from mnemos.persistence.types import Row
from mnemos.persistence.visibility import VisibilityFilter


class DuplicateMemoryError(ValueError):
    """Raised when an explicit memory id already exists."""


@dataclass(frozen=True)
class MemoryStatsRow:
    """Backend-neutral aggregate snapshot for ``GET /stats``.

    One round-trip per backend. ``avg_quality_rating`` is ``None`` when
    no scored rows exist; the handler picks the published default.
    """

    total_memories: int
    native_memories: int
    federated_memories: int
    memories_by_peer: dict[str, int] = field(default_factory=dict)
    memories_by_category: dict[str, int] = field(default_factory=dict)
    memories_by_subcategory: dict[str, dict[str, int]] = field(default_factory=dict)
    avg_quality_rating: float | None = None


@dataclass(frozen=True)
class CompressionStatsRow:
    """Backend-neutral aggregate snapshot for the compression slice of
    ``GET /stats``."""

    total_compressions: int
    average_compression_ratio: float | None
    unreviewed_compressions: int


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
        verbatim_content: str | None,
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

    # --- v4.1 handler-through-backend surface ---------------------------------

    @abstractmethod
    async def list_memories(
        self,
        tx: Transaction,
        *,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Row], int]:
        """List memories under the given visibility filter, ordered
        ``created DESC``.

        Returns ``(rows, total_count)`` where ``total_count`` is the
        ``COUNT(*)`` over the same predicate (pre-LIMIT/OFFSET) so the
        handler can populate paged response totals without a second
        round-trip.
        """
        ...

    @abstractmethod
    async def get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
    ) -> Row | None:
        """Fetch a memory by id, applying the visibility filter.

        Returns ``None`` when the memory does not exist OR when the
        filter excludes it. The 404-vs-403 distinction is intentionally
        collapsed at this layer to keep cross-tenant existence
        invisible; the handler returns 404 for both.
        """
        ...

    @abstractmethod
    async def update_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        fields: dict[str, Any],
    ) -> Row | None:
        """Apply ``fields`` patch to a memory. Returns the updated row,
        or ``None`` if the memory does not exist or the filter excludes
        it. Mutation paths use ``VisibilityScope.OWN_ONLY`` — non-owner
        callers cannot edit a row they merely have read access to via
        group/world bits.

        ``fields`` keys are validated and translated by the handler;
        the repository assumes they map cleanly to memory columns.
        """
        ...

    @abstractmethod
    async def delete_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
    ) -> Row | None:
        """Delete a memory if it exists and the filter admits.

        Returns the deleted row metadata if a row was deleted. Non-owner
        callers see ``None`` even for memories they could otherwise read.
        """
        ...

    @abstractmethod
    async def semantic_search(
        self,
        tx: Transaction,
        *,
        embedding: Sequence[float],
        limit: int,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        source_provider: str | None = None,
        source_model: str | None = None,
        source_agent: str | None = None,
    ) -> list[Row]:
        """Vector search over memory embeddings, applying visibility.

        Returns full memory rows (not the join-only shape used by the
        legacy SQLite helper), so the handler can hand them straight to
        ``row_to_memory`` without a second fetch.

        Vector ranking is backend-owned: Postgres ranks with pgvector
        ``ORDER BY embedding <=>`` and SQLite ranks in SQL via
        ``mnemos_cosine_similarity``. There is currently no Python
        post-fetch vector rerank call site for ``mnemos_hot.top_k``.
        """
        ...

    @abstractmethod
    async def fts_search(
        self,
        tx: Transaction,
        *,
        query: str,
        limit: int,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        source_provider: str | None = None,
        source_model: str | None = None,
        source_agent: str | None = None,
    ) -> list[Row]:
        """Full-text search over memory content, applying visibility."""
        ...

    @abstractmethod
    async def gather_stats(self, tx: Transaction) -> MemoryStatsRow:
        """Aggregate counters used by ``GET /stats``. System-level view
        with no visibility filter — only operators reach this path."""
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

    @abstractmethod
    async def gather_stats(self, tx: Transaction) -> CompressionStatsRow:
        """Aggregate compression counters used by ``GET /stats``."""
        ...


class WebhookRepository(ABC):
    """Webhook persistence surface.

    The v4.0 webhook outbox contract requires that every event-producing
    write commit a ``webhook_attempts`` row in the same database
    transaction as the triggering data write. ``enqueue_webhook_attempt``
    is the backend-neutral entry point for that — both backends
    implement it so handlers can preserve the transactional outbox
    property without reaching into ``mnemos.webhooks`` from inside a
    repository (forbidden by the persistence-no-upward-deps contract).
    """

    @abstractmethod
    async def dispatch_event(
        self,
        tx: Transaction,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[str]:
        """Append ``webhook_deliveries`` rows for every matching
        subscription, inside ``tx``, and return their delivery IDs.

        Both backends must atomically commit these rows alongside the
        triggering data write — that is the v4.0 outbox contract. The
        delivery worker reads the queue separately and performs the
        HTTP send; this method never dispatches over HTTP, despite the
        legacy name. The returned IDs let callers schedule the delivery
        attempt via ``mnemos.core.lifecycle._schedule_delivery_attempt``
        once the outer transaction has committed.
        """
        ...


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
