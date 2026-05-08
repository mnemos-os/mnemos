"""Postgres persistence backend.

Most legacy memory/DAG helpers still delegate to ``mnemos.db`` repository
functions. Federation and state KV SQL now live directly behind this
backend-neutral persistence interface.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import asyncpg

from mnemos.core.auth_context import UserContext
from mnemos.core.config import hot_rs_enabled
from mnemos.core.visibility import (
    read_visibility_predicate as _core_read_visibility_predicate,
)
from mnemos.db import eligibility as _eligibility
from mnemos.db import mcp_repo, openai_compat_repo, portability_repo
from mnemos.persistence.base import (
    BranchRepository,
    CompressionRepository,
    CompressionStatsRow,
    ConsultationAuditRepository,
    FederationRepository,
    KGRepository,
    MemoryRepository,
    MemoryStatsRow,
    PersistenceBackend,
    StateRepository,
    Transaction,
    VersionRepository,
    WebhookRepository,
)
from mnemos.persistence.types import MEMORY_COLS as _MEMORY_COLS, Row
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from mnemos.core import webhook_constants
from mnemos.persistence import nats_events as persistence_nats_events

logger = logging.getLogger(__name__)
_RECENCY_E_FOLD_SECONDS = 7 * 24 * 60 * 60
_FEDERATION_NATS_MEMORY_ROW_COLS = (
    "id, content, category, subcategory, created, updated, metadata, "
    "quality_rating, verbatim_content, owner_id, namespace, permission_mode, "
    "source_model, source_provider, source_session, source_agent, archived_at, "
    "federation_source, deleted_at, consolidated_into"
)


def _log_search_phase(
    trace_id: str | None,
    started_at: float | None,
    phase: str,
) -> None:
    if not trace_id or started_at is None:
        return
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    logger.info("[search:%s] %s done in %dms", trace_id, phase, elapsed_ms)


# Optional Rust hot-path accelerator. Loaded lazily so operators do not
# need the Rust wheel unless they opt in.
_HOT_RS = None
_HOT_RS_ENABLED = hot_rs_enabled()
if _HOT_RS_ENABLED:
    try:
        import mnemos_hot as _HOT_RS  # type: ignore[import-not-found]
        logger.info(
            "mnemos_hot Rust accelerator enabled (Postgres semantic rerank will use mnemos_hot %s)",
            getattr(_HOT_RS, "__version__", "?"),
        )
    except ImportError as _exc:
        logger.warning(
            "MNEMOS_HOT_RS_ENABLED=1 but mnemos_hot wheel is not importable: %s. "
            "Falling back to Python Postgres semantic rerank.",
            _exc,
        )
        _HOT_RS = None


def _vector_to_float_list(vector: Sequence[float]) -> list[float]:
    return [float(value) for value in vector]


def _parse_pgvector_text(raw: Any) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [float(value) for value in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [part for part in raw.strip("[]").split(",") if part]
        if isinstance(parsed, list):
            try:
                return [float(value) for value in parsed]
            except (TypeError, ValueError):
                return []
    return []


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


def _rerank_composite_python(
    query: Sequence[float],
    candidates: Sequence[Sequence[float]],
    recency_boost: Sequence[float],
    weight_cos: float,
    weight_recency: float,
    k: int,
) -> list[tuple[int, float]]:
    query_values = _vector_to_float_list(query)
    scores = [
        (
            idx,
            weight_cos * _cosine_similarity(query_values, _vector_to_float_list(candidate))
            + weight_recency * float(recency_boost[idx] if idx < len(recency_boost) else 0.0),
        )
        for idx, candidate in enumerate(candidates)
    ]
    scores.sort(key=lambda item: (-item[1], item[0]))
    return scores if k == 0 or k >= len(scores) else scores[:k]


def _rerank_composite(
    query: Sequence[float],
    candidates: Sequence[Sequence[float]],
    recency_boost: Sequence[float],
    weight_cos: float,
    weight_recency: float,
    k: int,
) -> list[tuple[int, float]]:
    if _HOT_RS is not None:
        try:
            result = _HOT_RS.rerank_composite(
                _vector_to_float_list(query),
                [_vector_to_float_list(candidate) for candidate in candidates],
                [float(value) for value in recency_boost],
                float(weight_cos),
                float(weight_recency),
                int(k),
            )
            return [(int(idx), float(score)) for idx, score in result]
        except Exception:
            pass
    return _rerank_composite_python(
        query, candidates, recency_boost, weight_cos, weight_recency, k,
    )


def _render_postgres_visibility(
    visibility: VisibilityFilter,
    *,
    start_idx: int = 1,
    table_alias: str = "",
) -> tuple[str, list[Any], int]:
    """Render a ``VisibilityFilter`` into a Postgres WHERE fragment.

    Returns ``(clause, params, next_idx)`` where ``clause`` is the SQL
    fragment using ``$N`` placeholders starting at ``start_idx``,
    ``params`` is the list of values to extend the caller's params
    list with (in placeholder order), and ``next_idx`` is the first
    free placeholder index after consuming ``params``.

    Returns ``("", [], start_idx)`` for ``ROOT_BYPASS`` with no
    namespace pin — the caller omits the WHERE entirely. The
    ``READABLE`` branch delegates to ``mnemos.core.visibility`` so the
    predicate stays one-to-one with the v1_multiuser RLS read policy.
    """
    p = f"{table_alias}." if table_alias else ""

    if visibility.scope == VisibilityScope.ROOT_BYPASS:
        if visibility.namespace is None:
            return "", [], start_idx
        return f"{p}namespace=${start_idx}", [visibility.namespace], start_idx + 1

    if visibility.namespace is None:
        return "1=0", [], start_idx

    if visibility.scope == VisibilityScope.OWN_ONLY:
        # Mutation path: strict owner_id + namespace match.
        return (
            f"{p}owner_id=${start_idx} AND {p}namespace=${start_idx + 1}",
            [visibility.user_id, visibility.namespace],
            start_idx + 2,
        )

    # READABLE: full v1_multiuser read predicate via core helper, plus
    # namespace pin appended after.
    clause, vis_params = _core_read_visibility_predicate(
        visibility.user_id or "",
        list(visibility.group_ids),
        start_idx,
        table_alias=table_alias,
    )
    next_idx = start_idx + len(vis_params)
    clause = f"{clause} AND {p}namespace=${next_idx}"
    vis_params = vis_params + [visibility.namespace]
    next_idx += 1
    return clause, vis_params, next_idx


class PostgresTransaction:
    """Transaction wrapper that keeps asyncpg private to the Postgres adapter."""

    def __init__(self, conn: asyncpg.Connection, tx: Any):
        self._conn = conn
        self._tx = tx
        self._closed = False
        self._after_commit: list[Callable[[], Awaitable[None] | None]] = []

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
        callbacks = self._after_commit
        self._after_commit = []
        for callback in callbacks:
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning("Postgres post-commit callback failed", exc_info=True)

    async def rollback(self) -> None:
        if self._closed:
            return
        await self._tx.rollback()
        self._closed = True
        self._after_commit = []

    def add_after_commit(self, callback: Callable[[], Awaitable[None] | None]) -> None:
        if self._closed:
            raise RuntimeError("cannot register post-commit callback on a closed transaction")
        self._after_commit.append(callback)


def _postgres_tx(tx: Transaction) -> PostgresTransaction:
    if not isinstance(tx, PostgresTransaction):
        raise TypeError("Postgres repositories require a PostgresTransaction")
    return tx


def _pg_result_count(result: str | None) -> int:
    if not result:
        return 0
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except (IndexError, ValueError):
        return 0


async def _queue_federation_nats_upsert_from_db(tx: PostgresTransaction, memory_id: str) -> None:
    if not persistence_nats_events.federation_nats_enabled():
        return
    row = await tx.conn.fetchrow(
        f"""
        SELECT {_FEDERATION_NATS_MEMORY_ROW_COLS}
        FROM memories
        WHERE id = $1
        """,
        memory_id,
    )
    _queue_federation_nats_upsert(tx, row)


def _queue_federation_nats_upsert(tx: PostgresTransaction, row: Row | None) -> None:
    if row is None or not persistence_nats_events.federation_nats_enabled():
        return
    event = persistence_nats_events.federation_memory_upsert_event(row)
    if event is None:
        return
    tx.add_after_commit(
        lambda event=event: persistence_nats_events.publish_federation_memory_upsert_event(event)
    )


class PostgresMemoryRepository(MemoryRepository):
    # Set by PostgresBackend on construction so search paths can fail
    # loudly on dim mismatches. None disables the check (e.g. tests
    # that bypass the backend). Mirrors SqliteMemoryRepository's
    # `_expected_embedding_dim` so the operator-facing error has the
    # same shape on both backends — surfaced 2026-05-08 by the
    # cross-code audit (#202).
    _expected_embedding_dim: int | None = None

    def _require_dim(self, embedding: Sequence[float], op: str) -> None:
        """Fail loudly if the embedding length doesn't match the
        configured dim. Without this guard, `embedding <=> $1::vector`
        in semantic_search rejects the cast at the asyncpg layer with
        a generic ``DataError``; the operator-facing message names
        the wrong layer (asyncpg type cast) instead of the actual
        cause (mismatched embedding model). The SQLite repository
        has the same guard for the same reason — keep both backends
        in lockstep so MNEMOS_EMBEDDING_DIM mismatches surface the
        same way regardless of profile.
        """
        expected = self._expected_embedding_dim
        if expected is None:
            return
        actual = len(embedding)
        if actual != expected:
            raise ValueError(
                f"Postgres embedding dim mismatch on {op}: got "
                f"{actual}-D vector but the configured "
                f"MNEMOS_EMBEDDING_DIM is {expected}. The embedding "
                f"endpoint may have been switched to a different "
                f"model. Verify INFERENCE_EMBED_HOST / model "
                f"selection and either restart with the matching "
                f"MNEMOS_EMBEDDING_DIM or swap the embedding "
                f"endpoint back to the model the DB was sized for."
            )

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
        verbatim_content: str | None,
        created: Any,
        updated: Any,
    ) -> str:
        pg_tx = _postgres_tx(tx)
        result = await portability_repo.insert_memory(
            pg_tx.conn,
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
            verbatim_content=verbatim_content,
            created=created,
            updated=updated,
        )
        if _pg_result_count(result) > 0:
            await _queue_federation_nats_upsert_from_db(pg_tx, memory_id)
        return result

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

    # --- v4.1 handler-through-backend impls -----------------------------------

    async def list_memories(
        self,
        tx: Transaction,
        *,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
    ) -> tuple[list[Row], int]:
        conn = _postgres_tx(tx).conn
        where_parts: list[str] = ["deleted_at IS NULL"]
        if not include_archived:
            where_parts.append("archived_at IS NULL")
        params: list[Any] = []
        if category is not None:
            params.append(category)
            where_parts.append(f"category=${len(params)}")
        if subcategory is not None:
            params.append(subcategory)
            where_parts.append(f"subcategory=${len(params)}")
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility, start_idx=len(params) + 1,
        )
        if vis_clause:
            where_parts.append(vis_clause)
            params.extend(vis_params)
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        select_sql = (
            f"SELECT {_MEMORY_COLS} FROM memories{where_sql} "
            f"ORDER BY created DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        count_sql = f"SELECT COUNT(*) FROM memories{where_sql}"
        rows = await conn.fetch(select_sql, *params, limit, offset)
        total = await conn.fetchval(count_sql, *params)
        return list(rows), int(total or 0)

    async def get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        include_archived: bool = False,
    ) -> Row | None:
        conn = _postgres_tx(tx).conn
        archived_clause = "" if include_archived else " AND archived_at IS NULL"
        if visibility.scope == VisibilityScope.ROOT_BYPASS and visibility.namespace is None:
            return await conn.fetchrow(
                f"SELECT {_MEMORY_COLS} FROM memories WHERE id=$1 AND deleted_at IS NULL{archived_clause}",
                memory_id,
            )
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility, start_idx=2,
        )
        sql = (
            f"SELECT {_MEMORY_COLS} FROM memories "
            f"WHERE id=$1 AND deleted_at IS NULL{archived_clause} AND {vis_clause}"
        )
        return await conn.fetchrow(sql, memory_id, *vis_params)

    async def update_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        fields: dict[str, Any],
    ) -> Row | None:
        if not fields:
            return None
        conn = _postgres_tx(tx).conn
        # $1 = memory_id, $2.. = field values, then visibility params,
        # so update_memory writes are atomic with their authorization
        # check (folded into the WHERE on the same UPDATE).
        keys = list(fields.keys())
        set_clauses = [f"{col}=${i + 2}" for i, col in enumerate(keys)]
        set_clauses.append("updated=NOW()")
        set_sql = ", ".join(set_clauses)
        values = [fields[k] for k in keys]
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility, start_idx=len(values) + 2,
        )
        if vis_clause:
            sql = (
                f"UPDATE memories SET {set_sql} "
                f"WHERE id=$1 AND deleted_at IS NULL AND {vis_clause} "
                f"RETURNING {_MEMORY_COLS}"
            )
            row = await conn.fetchrow(sql, memory_id, *values, *vis_params)
            if row is not None:
                await _queue_federation_nats_upsert_from_db(_postgres_tx(tx), memory_id)
            return row
        sql = (
            f"UPDATE memories SET {set_sql} WHERE id=$1 AND deleted_at IS NULL "
            f"RETURNING {_MEMORY_COLS}"
        )
        row = await conn.fetchrow(sql, memory_id, *values)
        if row is not None:
            await _queue_federation_nats_upsert_from_db(_postgres_tx(tx), memory_id)
        return row

    async def find_active_duplicate_by_content_hash(
        self,
        tx: Transaction,
        *,
        owner_id: str,
        namespace: str,
        content_hash: str,
        cross_namespace: bool = False,
    ) -> Row | None:
        conn = _postgres_tx(tx).conn
        namespace_clause = "" if cross_namespace else "AND namespace=$3"
        params: list[Any] = [owner_id, content_hash]
        if not cross_namespace:
            params.append(namespace)
        return await conn.fetchrow(
            f"""
            SELECT id, last_recalled_at
            FROM memories
            WHERE owner_id=$1
              {namespace_clause}
              AND deleted_at IS NULL
              AND archived_at IS NULL
              AND consolidated_into IS NULL
              AND content_hash=$2
            ORDER BY created ASC, id ASC
            LIMIT 1
            """,
            *params,
        )

    async def bump_recall_and_get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
    ) -> Row | None:
        conn = _postgres_tx(tx).conn
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility,
            start_idx=2,
        )
        if vis_clause:
            sql = (
                "UPDATE memories "
                "SET recall_count = recall_count + 1, last_recalled_at = NOW() "
                f"WHERE id=$1 AND deleted_at IS NULL AND archived_at IS NULL AND {vis_clause} "
                f"RETURNING {_MEMORY_COLS}"
            )
            return await conn.fetchrow(sql, memory_id, *vis_params)
        return await conn.fetchrow(
            "UPDATE memories "
            "SET recall_count = recall_count + 1, last_recalled_at = NOW() "
            "WHERE id=$1 AND deleted_at IS NULL AND archived_at IS NULL "
            f"RETURNING {_MEMORY_COLS}",
            memory_id,
        )

    async def find_duplicate_content_groups(
        self,
        tx: Transaction,
        *,
        namespace: str | None = None,
    ) -> list[Row]:
        conn = _postgres_tx(tx).conn
        return list(await conn.fetch(
            """
            SELECT
                owner_id,
                namespace,
                content_hash,
                COUNT(*)::int AS duplicate_count,
                ARRAY_AGG(id ORDER BY created ASC, id ASC) AS memory_ids,
                (ARRAY_AGG(id ORDER BY created ASC, id ASC))[1] AS canonical_id
            FROM memories
            WHERE deleted_at IS NULL
              AND archived_at IS NULL
              AND consolidated_into IS NULL
              AND content_hash IS NOT NULL
              AND ($1::text IS NULL OR namespace=$1)
            GROUP BY owner_id, namespace, content_hash
            HAVING COUNT(*) > 1
            ORDER BY duplicate_count DESC, owner_id ASC, namespace ASC, content_hash ASC
            """,
            namespace,
        ))

    async def consolidate_duplicate_memories(
        self,
        tx: Transaction,
        *,
        canonical_id: str,
        duplicate_ids: Sequence[str],
    ) -> int:
        if not duplicate_ids:
            return 0
        result = await _postgres_tx(tx).conn.execute(
            """
            UPDATE memories
            SET consolidated_into = $1,
                consolidated_at = NOW(),
                deleted_at = COALESCE(deleted_at, NOW()),
                updated = NOW()
            WHERE id = ANY($2::text[])
              AND id <> $1
              AND deleted_at IS NULL
              AND archived_at IS NULL
              AND consolidated_into IS NULL
              AND EXISTS (
                  SELECT 1 FROM memories
                  WHERE id = $1
                    AND deleted_at IS NULL
                    AND archived_at IS NULL
                    AND consolidated_into IS NULL
              )
            """,
            canonical_id,
            list(duplicate_ids),
        )
        return _pg_result_count(result)

    async def delete_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        requested_by: str | None = None,
        requested_at: Any = None,
        request_kind: str = "admin_purge",
        reason: str | None = None,
        source: Sequence[str] | None = None,
    ) -> Row | None:
        conn = _postgres_tx(tx).conn
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility, start_idx=2,
        )
        if requested_by is not None:
            target_where = "id=$1 AND deleted_at IS NULL"
            if vis_clause:
                target_where = f"{target_where} AND {vis_clause}"
            audit_start = len(vis_params) + 2
            source_array = list(source) if source is not None else None
            return await conn.fetchrow(
                f"""
                WITH target AS (
                    SELECT owner_id, namespace, id, content, category, subcategory
                      FROM memories
                     WHERE {target_where}
                ), audit AS (
                    INSERT INTO deletion_log (
                        memory_id, content_hash, owner_id, namespace,
                        requested_by, requested_at, request_kind, reason, source
                    )
                    SELECT
                        id,
                        encode(digest(COALESCE(content, ''), 'sha256'), 'hex'),
                        owner_id,
                        namespace,
                        ${audit_start},
                        COALESCE(${audit_start + 1}::timestamptz, NOW()),
                        ${audit_start + 2},
                        ${audit_start + 3},
                        ${audit_start + 4}::text[]
                      FROM target
                    RETURNING 1
                )
                DELETE FROM memories m
                 USING target
                 WHERE m.id = target.id
                RETURNING
                    target.owner_id,
                    target.namespace,
                    target.id,
                    target.content,
                    target.category,
                    target.subcategory
                """,
                memory_id,
                *vis_params,
                requested_by,
                requested_at,
                request_kind,
                reason,
                source_array,
            )
        if vis_clause:
            sql = (
                "DELETE FROM memories "
                f"WHERE id=$1 AND deleted_at IS NULL AND {vis_clause} "
                "RETURNING owner_id, namespace, id, content, category, subcategory"
            )
            return await conn.fetchrow(sql, memory_id, *vis_params)
        return await conn.fetchrow(
            "DELETE FROM memories WHERE id=$1 AND deleted_at IS NULL "
            "RETURNING owner_id, namespace, id, content, category, subcategory",
            memory_id,
        )

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
        include_archived: bool = False,
        boost_recency: bool = False,
        recency_weight: float = 0.15,
        search_trace_id: str | None = None,
        search_started_at: float | None = None,
    ) -> list[Row]:
        # Fail loudly on dim mismatches before the asyncpg cast layer
        # produces a vague DataError. Mirrors SqliteMemoryRepository.
        self._require_dim(embedding, "semantic_search")
        conn = _postgres_tx(tx).conn
        # $1 is the embedding vector, used in both SELECT (for the
        # similarity score) and ORDER BY. Passing as a parameter (not
        # interpolated) eliminates injection risk from a poisoned
        # embedding response.
        vec_str = "[" + ",".join(str(float(x)) for x in embedding) + "]"
        params: list[Any] = [vec_str]
        conditions: list[str] = ["embedding IS NOT NULL", "deleted_at IS NULL"]
        if not include_archived:
            conditions.append("archived_at IS NULL")
        for col, val in (
            ("category", category),
            ("subcategory", subcategory),
            ("source_provider", source_provider),
            ("source_model", source_model),
            ("source_agent", source_agent),
        ):
            if val is not None:
                params.append(val)
                conditions.append(f"{col}=${len(params)}")
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility, start_idx=len(params) + 1,
        )
        if vis_clause:
            conditions.append(vis_clause)
            params.extend(vis_params)
        candidate_limit = limit
        if boost_recency:
            candidate_limit = max(limit, min(limit * 4, 200))
        params.append(candidate_limit)
        recency_select = ""
        if boost_recency:
            recency_select = (
                ", embedding::text AS _embedding_text, "
                "EXP(-GREATEST(EXTRACT(EPOCH FROM (timezone('UTC', now()) - "
                "COALESCE(last_recalled_at, updated, created))), 0) / "
                f"{_RECENCY_E_FOLD_SECONDS}.0) AS _recency_boost"
            )
        sql = (
            f"SELECT {_MEMORY_COLS}, 1 - (embedding <=> $1::vector) AS similarity"
            f"{recency_select} "
            "FROM memories "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY embedding <=> $1::vector LIMIT ${len(params)}"
        )
        rows = list(await conn.fetch(sql, *params))
        _log_search_phase(search_trace_id, search_started_at, "ann_scan")
        if not boost_recency or len(rows) <= 1:
            _log_search_phase(search_trace_id, search_started_at, "rerank")
            return rows[:limit]

        candidates = [_parse_pgvector_text(row.get("_embedding_text")) for row in rows]
        recency_boost = [float(row.get("_recency_boost") or 0.0) for row in rows]
        weight_recency = max(0.0, min(1.0, float(recency_weight)))
        weight_cos = 1.0 - weight_recency
        ranking = _rerank_composite(
            embedding,
            candidates,
            recency_boost,
            weight_cos,
            weight_recency,
            limit,
        )
        reranked: list[Row] = []
        for idx, composite_score in ranking:
            row = rows[idx]
            enriched = dict(row.items()) if hasattr(row, "items") else dict(row)
            enriched["similarity"] = composite_score
            enriched["_composite_score"] = composite_score
            reranked.append(enriched)
        _log_search_phase(search_trace_id, search_started_at, "rerank")
        return reranked

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
        include_archived: bool = False,
    ) -> list[Row]:
        # plainto_tsquery treats user input as plain text — tsquery
        # operators like |, !, & are not interpreted. Prevents tsquery
        # operator injection.
        conn = _postgres_tx(tx).conn
        clean_query = query.strip()
        # FTS path: $1=query, $2=limit, filter+visibility params at $3+
        params: list[Any] = [clean_query, limit]
        conditions: list[str] = [
            "to_tsvector('english', content) @@ plainto_tsquery('english', $1)",
            "deleted_at IS NULL",
        ]
        if not include_archived:
            conditions.append("archived_at IS NULL")
        for col, val in (
            ("category", category),
            ("subcategory", subcategory),
            ("source_provider", source_provider),
            ("source_model", source_model),
            ("source_agent", source_agent),
        ):
            if val is not None:
                params.append(val)
                conditions.append(f"{col}=${len(params)}")
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility, start_idx=len(params) + 1,
        )
        if vis_clause:
            conditions.append(vis_clause)
            params.extend(vis_params)
        sql = (
            f"SELECT {_MEMORY_COLS}, "
            "ts_rank(to_tsvector('english', content), "
            "plainto_tsquery('english', $1)) AS rank "
            "FROM memories "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY rank DESC LIMIT $2"
        )
        try:
            return list(await conn.fetch(sql, *params))
        except Exception:
            # ILIKE fallback: same predicate shape, $1 becomes the LIKE
            # pattern, $2 still the limit.
            like_q = f"%{query}%"
            ilike_params: list[Any] = [like_q, limit]
            ilike_conditions: list[str] = ["content ILIKE $1", "deleted_at IS NULL"]
            if not include_archived:
                ilike_conditions.append("archived_at IS NULL")
            for col, val in (
                ("category", category),
                ("subcategory", subcategory),
                ("source_provider", source_provider),
                ("source_model", source_model),
                ("source_agent", source_agent),
            ):
                if val is not None:
                    ilike_params.append(val)
                    ilike_conditions.append(f"{col}=${len(ilike_params)}")
            ilike_vis_clause, ilike_vis_params, _ = _render_postgres_visibility(
                visibility, start_idx=len(ilike_params) + 1,
            )
            if ilike_vis_clause:
                ilike_conditions.append(ilike_vis_clause)
                ilike_params.extend(ilike_vis_params)
            ilike_sql = (
                f"SELECT {_MEMORY_COLS} FROM memories "
                f"WHERE {' AND '.join(ilike_conditions)} "
                "ORDER BY created DESC LIMIT $2"
            )
            return list(await conn.fetch(ilike_sql, *ilike_params))

    async def gather_stats(self, tx: Transaction) -> MemoryStatsRow:
        conn = _postgres_tx(tx).conn
        total = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL")
        native = await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE federation_source IS NULL AND deleted_at IS NULL",
        )
        federated = await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE federation_source IS NOT NULL AND deleted_at IS NULL",
        )
        peer_rows = await conn.fetch(
            "SELECT federation_source, COUNT(*) AS cnt FROM memories "
            "WHERE federation_source IS NOT NULL AND deleted_at IS NULL "
            "GROUP BY federation_source ORDER BY cnt DESC",
        )
        cat_rows = await conn.fetch(
            "SELECT category, COUNT(*) AS cnt FROM memories "
            "WHERE deleted_at IS NULL GROUP BY category",
        )
        sub_rows = await conn.fetch(
            "SELECT category, subcategory, COUNT(*) AS cnt FROM memories "
            "WHERE subcategory IS NOT NULL AND deleted_at IS NULL "
            "GROUP BY category, subcategory ORDER BY cnt DESC",
        )
        avg_quality = await conn.fetchval(
            "SELECT AVG(quality_rating) FROM memories "
            "WHERE quality_rating IS NOT NULL AND deleted_at IS NULL",
        )
        memories_by_subcategory: dict[str, dict[str, int]] = {}
        for r in sub_rows:
            memories_by_subcategory.setdefault(r["category"], {})[r["subcategory"]] = r["cnt"]
        return MemoryStatsRow(
            total_memories=int(total or 0),
            native_memories=int(native or 0),
            federated_memories=int(federated or 0),
            memories_by_peer={r["federation_source"]: r["cnt"] for r in peer_rows},
            memories_by_category={r["category"]: r["cnt"] for r in cat_rows},
            memories_by_subcategory=memories_by_subcategory,
            avg_quality_rating=float(avg_quality) if avg_quality is not None else None,
        )


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

    async def gather_stats(self, tx: Transaction) -> CompressionStatsRow:
        conn = _postgres_tx(tx).conn
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM memory_compressed_variants",
        ) or 0
        avg_ratio = await conn.fetchval(
            "SELECT AVG(v.compression_ratio) FROM memory_compressed_variants v",
        )
        unreviewed = await conn.fetchval(
            "SELECT COUNT(*) FROM memory_compressed_variants "
            "WHERE quality_score IS NULL",
        ) or 0
        return CompressionStatsRow(
            total_compressions=int(total),
            average_compression_ratio=float(avg_ratio) if avg_ratio is not None else None,
            unreviewed_compressions=int(unreviewed),
        )


class PostgresWebhookRepository(WebhookRepository):
    async def insert_subscription(
        self,
        tx: Transaction,
        *,
        subscription_id: str | None = None,
        url: str,
        events: Sequence[str],
        secret: str | None = None,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> str:
        subscription_id = subscription_id or str(uuid.uuid4())
        await _postgres_tx(tx).conn.execute(
            """
            INSERT INTO webhook_subscriptions (id, url, events, secret, owner_id, namespace)
            VALUES ($1::uuid, $2, $3::text[], $4, $5, $6)
            """,
            subscription_id,
            url,
            list(events),
            secret or "",
            owner_id,
            namespace,
        )
        return subscription_id

    async def dispatch_event(
        self,
        tx: Transaction,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[str]:
        conn = _postgres_tx(tx).conn
        query = """
            SELECT id, url, owner_id, namespace
            FROM webhook_subscriptions
            WHERE NOT revoked AND $1 = ANY(events)
        """
        args: list[Any] = [event_type]
        if owner_id is not None:
            args.append(owner_id)
            query += f" AND owner_id = ${len(args)}"
        if namespace is not None:
            args.append(namespace)
            query += f" AND namespace = ${len(args)}"
        subscriptions = await conn.fetch(query, *args)
        body = json.dumps(
            {"event": event_type, "timestamp": datetime.now(timezone.utc).isoformat(), "data": payload},
            separators=(",", ":"),
            sort_keys=True,
        )
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        delivery_ids: list[str] = []
        for sub in subscriptions:
            delivery_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO webhook_deliveries
                  (id, subscription_id, event_type, payload, payload_hash, status, writer_revision)
                VALUES ($1::uuid, $2, $3, $4, $5, 'pending', $6)
                """,
                delivery_id,
                sub["id"],
                event_type,
                body,
                body_hash,
                webhook_constants.NEW_CODE_WRITER_REVISION,
            )
            from mnemos.nats.webhook_events import publish_delivery_queued
            await publish_delivery_queued(
                delivery_id=delivery_id,
                subscription_id=sub["id"],
                event_type=event_type,
                url=sub["url"],
                payload_hash=body_hash,
                namespace=sub["namespace"],
                owner_id=sub["owner_id"],
            )
            await persistence_nats_events.publish_webhook_outbox_insert(
                delivery_id=delivery_id,
                subscription_id=sub["id"],
                event_type=event_type,
                url=sub["url"],
                payload_hash=body_hash,
                namespace=sub["namespace"],
                owner_id=sub["owner_id"],
            )
            delivery_ids.append(delivery_id)
        return delivery_ids

    async def fetch_deliveries(self, tx: Transaction, subscription_id: str | None = None) -> list[Row]:
        if subscription_id is None:
            return await _postgres_tx(tx).conn.fetch("SELECT * FROM webhook_deliveries ORDER BY created ASC")
        return await _postgres_tx(tx).conn.fetch(
            "SELECT * FROM webhook_deliveries WHERE subscription_id = $1::uuid ORDER BY created ASC",
            subscription_id,
        )


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
    _ALLOWED_PEER_COLS = {
        "name",
        "base_url",
        "auth_token",
        "namespace_filter",
        "category_filter",
        "enabled",
        "sync_interval_secs",
        "compat_mode",
    }

    async def fetch_memory_page(
        self,
        tx: Transaction,
        *,
        updated_after: Any | None = None,
        id_after: str | None = None,
        limit: int = 100,
    ) -> list[Row]:
        conn = _postgres_tx(tx).conn
        if updated_after is not None and id_after is not None:
            return await conn.fetch(
                """
                SELECT id, content, category, subcategory, metadata, owner_id, namespace, updated
                FROM memories
                WHERE deleted_at IS NULL
                  AND (updated > $1 OR (updated = $1 AND id > $2))
                ORDER BY updated ASC, id ASC
                LIMIT $3
                """,
                updated_after,
                id_after,
                limit,
            )
        return await conn.fetch(
            """
            SELECT id, content, category, subcategory, metadata, owner_id, namespace, updated
            FROM memories
            WHERE deleted_at IS NULL
            ORDER BY updated ASC, id ASC
            LIMIT $1
            """,
            limit,
        )

    async def create_peer(
        self,
        tx: Transaction,
        *,
        name: str,
        base_url: str,
        auth_token: str,
        namespace_filter: Sequence[str] | None,
        category_filter: Sequence[str] | None,
        enabled: bool,
        sync_interval_secs: int,
        compat_mode: str,
    ) -> Row:
        return await _postgres_tx(tx).conn.fetchrow(
            """
            INSERT INTO federation_peers
              (name, base_url, auth_token, namespace_filter, category_filter,
               enabled, sync_interval_secs, compat_mode)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            name,
            base_url,
            auth_token,
            list(namespace_filter) if namespace_filter is not None else None,
            list(category_filter) if category_filter is not None else None,
            enabled,
            sync_interval_secs,
            compat_mode,
        )

    async def list_peers(self, tx: Transaction) -> list[Row]:
        return list(await _postgres_tx(tx).conn.fetch("SELECT * FROM federation_peers ORDER BY name"))

    async def get_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        return await _postgres_tx(tx).conn.fetchrow(
            "SELECT * FROM federation_peers WHERE id = $1::uuid",
            peer_id,
        )

    async def update_peer(self, tx: Transaction, peer_id: str, updates: dict[str, Any]) -> Row | None:
        bad = set(updates) - self._ALLOWED_PEER_COLS
        if bad:
            raise ValueError(f"unknown federation peer fields: {sorted(bad)}")
        if not updates:
            return await self.get_peer(tx, peer_id)
        set_clauses = [f"{col}=${i + 2}" for i, col in enumerate(updates.keys())]
        set_clauses.append("updated=NOW()")
        return await _postgres_tx(tx).conn.fetchrow(
            f"UPDATE federation_peers SET {', '.join(set_clauses)} "
            "WHERE id=$1::uuid RETURNING *",
            peer_id,
            *updates.values(),
        )

    async def upsert_peer(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        base_url: str,
        name: str | None = None,
        enabled: bool = True,
    ) -> None:
        await _postgres_tx(tx).conn.execute(
            """
            INSERT INTO federation_peers (id, base_url, name, auth_token, enabled)
            VALUES ($1::uuid, $2, $3, '', $4)
            ON CONFLICT (id) DO UPDATE
            SET base_url = EXCLUDED.base_url,
                name = EXCLUDED.name,
                enabled = EXCLUDED.enabled
            """,
            peer_id,
            base_url,
            name,
            enabled,
        )

    async def delete_peer(self, tx: Transaction, peer_id: str) -> bool:
        result = await _postgres_tx(tx).conn.execute(
            "DELETE FROM federation_peers WHERE id = $1::uuid",
            peer_id,
        )
        return _pg_result_count(result) > 0

    async def fetch_sync_log(self, tx: Transaction, peer_id: str, limit: int) -> list[Row]:
        return list(await _postgres_tx(tx).conn.fetch(
            """
            SELECT id::text, started_at, finished_at, memories_pulled,
                   memories_new, memories_updated, error,
                   cursor_before, cursor_after
            FROM federation_sync_log
            WHERE peer_id = $1::uuid
            ORDER BY started_at DESC
            LIMIT $2
            """,
            peer_id,
            limit,
        ))

    async def feed_query(
        self,
        tx: Transaction,
        *,
        since_updated: Any | None,
        since_id: str | None,
        namespaces: Sequence[str],
        categories: Sequence[str],
        limit: int,
        prefer_compressed: bool,
    ) -> list[Row]:
        memory_query_parts = [_eligibility.eligible_for_federation("m")]
        tombstone_query_parts = [
            "m.federation_source IS NULL",
            "m.deleted_at IS NULL",
            "m.consolidated_into IS NOT NULL",
            "m.consolidated_at IS NOT NULL",
        ]
        args: list[Any] = []
        if since_updated is not None:
            args.append(since_updated)
            since_updated_arg = len(args)
            args.append(since_id)
            since_id_arg = len(args)
            memory_query_parts.append(
                f"(m.updated > ${since_updated_arg} "
                f"OR (m.updated = ${since_updated_arg} AND m.id > ${since_id_arg}))"
            )
            tombstone_query_parts.append(
                f"(m.consolidated_at > ${since_updated_arg} "
                f"OR (m.consolidated_at = ${since_updated_arg} AND m.id > ${since_id_arg}))"
            )
        if namespaces:
            args.append(list(namespaces))
            memory_query_parts.append(f"m.namespace = ANY(${len(args)})")
            tombstone_query_parts.append(f"m.namespace = ANY(${len(args)})")
        if categories:
            args.append(list(categories))
            memory_query_parts.append(f"m.category = ANY(${len(args)})")
            tombstone_query_parts.append(f"m.category = ANY(${len(args)})")
        args.append(limit)

        if prefer_compressed:
            use_variant = (
                "m.archived_at IS NULL "
                "AND v.compressed_content IS NOT NULL "
                "AND (2 * octet_length(to_json(v.compressed_content)::text)) "
                "  < (octet_length(to_json(m.content)::text) "
                "     + COALESCE(octet_length(to_json(m.verbatim_content)::text), 0))"
            )
            content_select = (
                f"CASE WHEN {use_variant} THEN v.compressed_content "
                "ELSE m.content END AS content,"
            )
            compressed_select = (
                f"CASE WHEN {use_variant} THEN v.compressed_content "
                "ELSE NULL::text END AS compressed_content,"
            )
            verbatim_select = (
                f"CASE WHEN {use_variant} THEN NULL "
                "ELSE m.verbatim_content END AS verbatim_content,"
            )
            join_compressed = "LEFT JOIN memory_compressed_variants v ON v.memory_id = m.id "
        else:
            content_select = "m.content,"
            compressed_select = "NULL::text AS compressed_content,"
            verbatim_select = "m.verbatim_content,"
            join_compressed = ""

        memory_where_clause = " AND ".join(memory_query_parts)
        tombstone_where_clause = " AND ".join(tombstone_query_parts)
        return list(await _postgres_tx(tx).conn.fetch(
            f"""
            SELECT *
            FROM (
                SELECT NULL::text AS type,
                       m.id, {content_select}
                       m.category, m.subcategory, m.metadata,
                       m.quality_rating, {verbatim_select}
                       m.owner_id, m.namespace,
                       m.permission_mode, m.source_model, m.source_provider,
                       m.source_session, m.source_agent, m.created, m.updated,
                       m.archived_at,
                       NULL::text AS consolidated_into,
                       NULL::timestamptz AS consolidated_at,
                       {compressed_select.rstrip(',')}
                FROM memories m
                {join_compressed}
                WHERE {memory_where_clause}

                UNION ALL

                SELECT 'consolidation'::text AS type,
                       m.id,
                       NULL::text AS content,
                       NULL::text AS category,
                       NULL::text AS subcategory,
                       NULL::jsonb AS metadata,
                       NULL::int AS quality_rating,
                       NULL::text AS verbatim_content,
                       NULL::text AS owner_id,
                       m.namespace,
                       NULL::smallint AS permission_mode,
                       NULL::text AS source_model,
                       NULL::text AS source_provider,
                       NULL::text AS source_session,
                       NULL::text AS source_agent,
                       m.created,
                       m.consolidated_at AS updated,
                       NULL::timestamptz AS archived_at,
                       m.consolidated_into,
                       m.consolidated_at,
                       NULL::text AS compressed_content
                FROM memories m
                WHERE {tombstone_where_clause}
            ) feed
            ORDER BY updated ASC, id ASC
            LIMIT ${len(args)}
            """,
            *args,
        ))

    async def get_feed_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        namespaces: Sequence[str],
        categories: Sequence[str],
    ) -> Row | None:
        query_parts = [_eligibility.eligible_for_federation("m"), "m.id = $1"]
        args: list[Any] = [memory_id]
        if namespaces:
            args.append(list(namespaces))
            query_parts.append(f"m.namespace = ANY(${len(args)})")
        if categories:
            args.append(list(categories))
            query_parts.append(f"m.category = ANY(${len(args)})")
        where_clause = " AND ".join(query_parts)
        return await _postgres_tx(tx).conn.fetchrow(
            f"""
            SELECT id, content, category, subcategory, metadata, quality_rating,
                   verbatim_content, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   created, updated, archived_at
            FROM memories m
            WHERE {where_clause}
            """,
            *args,
        )

    async def get_sync_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        return await _postgres_tx(tx).conn.fetchrow(
            """
            SELECT id::text, name, base_url, auth_token, namespace_filter,
                   category_filter, enabled, last_sync_cursor,
                   compat_mode
            FROM federation_peers WHERE id = $1::uuid
            """,
            peer_id,
        )

    async def update_peer_schema_check(self, tx: Transaction, peer_id: str, peer_version: str | None) -> None:
        await _postgres_tx(tx).conn.execute(
            """
            UPDATE federation_peers
            SET peer_mnemos_version = $2, last_schema_check_at = NOW()
            WHERE id = $1::uuid
            """,
            peer_id,
            peer_version,
        )

    async def record_schema_abort(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        peer_version: str | None,
        cursor_before: Any,
        error: str,
        is_transient: bool,
    ) -> None:
        conn = _postgres_tx(tx).conn
        await self.update_peer_schema_check(tx, peer_id, peer_version)
        log_id = await conn.fetchval(
            """
            INSERT INTO federation_sync_log (peer_id, cursor_before)
            VALUES ($1::uuid, $2) RETURNING id
            """,
            peer_id,
            cursor_before,
        )
        await self.finish_sync_log(
            tx,
            log_id=log_id,
            memories_pulled=0,
            memories_new=0,
            memories_updated=0,
            error=error,
            cursor_after=cursor_before,
        )
        if is_transient:
            await conn.execute(
                """
                UPDATE federation_peers
                SET last_sync_at = NOW()
                                  - (sync_interval_secs || ' seconds')::interval
                                  + INTERVAL '60 seconds',
                    last_error = $2,
                    last_error_at = NOW()
                WHERE id = $1::uuid
                """,
                peer_id,
                error,
            )
        else:
            await conn.execute(
                """
                UPDATE federation_peers
                SET last_sync_at = NOW(),
                    last_error = $2,
                    last_error_at = NOW()
                WHERE id = $1::uuid
                """,
                peer_id,
                error,
            )

    async def create_sync_log(self, tx: Transaction, peer_id: str, cursor_before: Any) -> Any:
        return await _postgres_tx(tx).conn.fetchval(
            """
            INSERT INTO federation_sync_log (peer_id, cursor_before)
            VALUES ($1::uuid, $2) RETURNING id
            """,
            peer_id,
            cursor_before,
        )

    async def finish_sync_log(
        self,
        tx: Transaction,
        *,
        log_id: Any,
        memories_pulled: int,
        memories_new: int,
        memories_updated: int,
        error: str | None,
        cursor_after: Any,
    ) -> None:
        await _postgres_tx(tx).conn.execute(
            """
            UPDATE federation_sync_log
            SET finished_at = NOW(),
                memories_pulled = $2,
                memories_new = $3,
                memories_updated = $4,
                error = $5,
                cursor_after = $6
            WHERE id = $1::uuid
            """,
            log_id,
            memories_pulled,
            memories_new,
            memories_updated,
            error,
            cursor_after,
        )

    async def record_sync_error(self, tx: Transaction, peer_id: str, error: str) -> None:
        await _postgres_tx(tx).conn.execute(
            """
            UPDATE federation_peers
            SET last_sync_at = NOW(), last_error = $2, last_error_at = NOW()
            WHERE id = $1::uuid
            """,
            peer_id,
            error,
        )

    async def record_sync_success(
        self,
        tx: Transaction,
        peer_id: str,
        cursor: Any,
        total_pulled: int,
    ) -> None:
        await _postgres_tx(tx).conn.execute(
            """
            UPDATE federation_peers
            SET last_sync_at = NOW(),
                last_sync_cursor = $2,
                last_error = NULL,
                last_error_at = NULL,
                total_pulled = total_pulled + $3
            WHERE id = $1::uuid
            """,
            peer_id,
            cursor,
            total_pulled,
        )

    async def list_due_peers(self, tx: Transaction, *, limit: int = 10) -> list[Row]:
        return list(await _postgres_tx(tx).conn.fetch(
            """
            SELECT id::text, name, sync_interval_secs, last_sync_at
            FROM federation_peers
            WHERE enabled
              AND (last_sync_at IS NULL
                   OR last_sync_at + (sync_interval_secs || ' seconds')::interval <= NOW())
            ORDER BY COALESCE(
                last_sync_at + (sync_interval_secs || ' seconds')::interval,
                'epoch'::timestamptz
            )
            LIMIT $1
            """,
            limit,
        ))

    async def fetch_federated_memory_marker(self, tx: Transaction, local_id: str) -> Row | None:
        return await _postgres_tx(tx).conn.fetchrow(
            "SELECT federation_remote_updated FROM memories "
            "WHERE id = $1 AND deleted_at IS NULL",
            local_id,
        )

    async def insert_federated_memory(
        self,
        tx: Transaction,
        *,
        local_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str,
        quality_rating: int,
        namespace: str,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        peer_name: str,
        remote_updated: Any,
    ) -> bool:
        try:
            await _postgres_tx(tx).conn.execute(
                """
                INSERT INTO memories
                  (id, content, category, subcategory, metadata, verbatim_content,
                   quality_rating, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   federation_source, federation_remote_updated, created, updated)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, 'federation', $8, 644,
                        $9, $10, $11, $12, $13, $14::timestamptz, NOW(),
                        $14::timestamptz)
                """,
                local_id,
                content,
                category,
                subcategory,
                metadata_json,
                verbatim_content,
                quality_rating,
                namespace,
                source_model,
                source_provider,
                source_session,
                source_agent,
                peer_name,
                remote_updated,
            )
            return True
        except asyncpg.UniqueViolationError:
            return False

    async def update_federated_memory_if_newer(
        self,
        tx: Transaction,
        *,
        local_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str,
        quality_rating: int,
        namespace: str,
        remote_updated: Any,
    ) -> bool:
        result = await _postgres_tx(tx).conn.execute(
            """
            UPDATE memories SET
              content = $2, category = $3, subcategory = $4,
              metadata = $5::jsonb, verbatim_content = $6,
              quality_rating = $7, namespace = $8,
              federation_remote_updated = $9::timestamptz,
              updated = $9::timestamptz
            WHERE id = $1
              AND deleted_at IS NULL
              AND (
                  federation_remote_updated IS NULL
                  OR federation_remote_updated < $9::timestamptz
              )
            """,
            local_id,
            content,
            category,
            subcategory,
            metadata_json,
            verbatim_content,
            quality_rating,
            namespace,
            remote_updated,
        )
        return _pg_result_count(result) > 0

    async def apply_consolidation_tombstone(
        self,
        tx: Transaction,
        *,
        local_id: str,
        local_canonical_id: str,
        consolidated_at: Any,
        remote_id: str,
        canonical_remote_id: str,
        peer_name: str,
    ) -> bool:
        result = await _postgres_tx(tx).conn.execute(
            """
            UPDATE memories
            SET consolidated_into = $2,
                consolidated_at = COALESCE($3::timestamptz, NOW()),
                permission_mode = 400,
                metadata = COALESCE(metadata, '{}'::jsonb)
                    || jsonb_build_object(
                        'federation_consolidation', jsonb_build_object(
                            'remote_id', $4,
                            'remote_consolidated_into', $5,
                            'peer', $6
                        )
                    )
            WHERE id = $1
              AND deleted_at IS NULL
              AND consolidated_into IS DISTINCT FROM $2
              AND EXISTS (
                  SELECT 1 FROM memories
                  WHERE id = $2 AND deleted_at IS NULL
              )
            """,
            local_id,
            local_canonical_id,
            consolidated_at,
            remote_id,
            canonical_remote_id,
            peer_name,
        )
        return _pg_result_count(result) > 0

    async def delete_federated_memory(self, tx: Transaction, peer_name: str, memory_id: str) -> int:
        local_id = f"fed:{peer_name}:{memory_id}"
        result = await _postgres_tx(tx).conn.execute(
            """
            DELETE FROM memories
            WHERE id = $1
              AND federation_source = $2
              AND deleted_at IS NULL
            """,
            local_id,
            peer_name,
        )
        return _pg_result_count(result)


class PostgresStateRepository(StateRepository):
    """state.value is now TEXT on PG (migrations_v4_2_state_value_text.sql).

    Pass-through with no JSON shape coupling — the column matches
    SqliteStateRepository's TEXT contract exactly. Callers who want
    JSON shape (e.g. the HTTP /v1/state route) wrap their payloads
    in json.dumps at the API edge.
    """

    async def get(
        self,
        tx: Transaction,
        key: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> Row | None:
        return await _postgres_tx(tx).conn.fetchrow(
            "SELECT key, value, updated::text AS updated, version, owner_id, namespace FROM state "
            "WHERE owner_id = $1 AND namespace = $2 AND key = $3 "
            "AND deleted_at IS NULL",
            owner_id,
            namespace,
            key,
        )

    async def set(
        self,
        tx: Transaction,
        key: str,
        value: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        expires_at: Any | None = None,
    ) -> Row | None:
        _ = expires_at
        return await _postgres_tx(tx).conn.fetchrow(
            """
            INSERT INTO state (owner_id, namespace, key, value, updated)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (owner_id, namespace, key) DO UPDATE
            SET value = $4,
                updated = NOW(),
                version = state.version + 1
            WHERE state.deleted_at IS NULL
            RETURNING key, value, updated::text AS updated, version, owner_id, namespace
            """,
            owner_id,
            namespace,
            key,
            value,
        )

    async def delete(
        self,
        tx: Transaction,
        key: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> bool:
        result = await _postgres_tx(tx).conn.execute(
            "DELETE FROM state WHERE owner_id = $1 AND namespace = $2 AND key = $3 "
            "AND deleted_at IS NULL",
            owner_id,
            namespace,
            key,
        )
        return _pg_result_count(result) > 0

    async def list_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Row]:
        args: list[Any] = [owner_id, namespace]
        sql = (
            "SELECT key, updated::text AS updated, version, owner_id, namespace FROM state "
            "WHERE owner_id = $1 AND namespace = $2 "
            "AND deleted_at IS NULL ORDER BY key"
        )
        if limit is not None:
            args.extend([limit, offset])
            sql += " LIMIT $3 OFFSET $4"
        return list(await _postgres_tx(tx).conn.fetch(sql, *args))

    async def delete_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> int:
        result = await _postgres_tx(tx).conn.execute(
            "DELETE FROM state WHERE owner_id = $1 AND namespace = $2 AND deleted_at IS NULL",
            owner_id,
            namespace,
        )
        return _pg_result_count(result)


class PostgresBackend(PersistenceBackend):
    """Postgres persistence facade backed by an asyncpg pool."""

    supports_listen_notify = True
    supports_advisory_locks = True
    supports_row_level_security = True
    supports_pgvector = True

    def __init__(self, pool: asyncpg.Pool, settings: Any):
        self._pool = pool
        self._settings = settings
        self._memories = PostgresMemoryRepository()
        # Wire the configured embedding dim into the memory repo so
        # semantic_search fails loudly on dim mismatches with the
        # operator-friendly error rather than the generic asyncpg
        # cast error. Settings shape mirrors what SqliteBackend uses.
        try:
            self._memories._expected_embedding_dim = int(
                getattr(settings.database, "embedding_dim", 768)
            )
        except (AttributeError, TypeError, ValueError):
            # Defensive: tests or stripped-down settings shapes may
            # not carry `database.embedding_dim`. Leave the slot as
            # None so the guard is a no-op rather than a hard failure
            # at construction.
            self._memories._expected_embedding_dim = None
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
        async with self._pool.acquire() as conn:
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
