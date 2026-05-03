"""Postgres persistence backend shell.

D.1 keeps the existing mnemos/db repository functions as the implementation
source of truth. These classes only adapt their asyncpg connection parameters
to the backend-neutral persistence interfaces.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import asyncpg

from mnemos.core.auth_context import UserContext
from mnemos.core.config import hot_rs_enabled
from mnemos.core.visibility import (
    read_visibility_predicate as _core_read_visibility_predicate,
)
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

logger = logging.getLogger(__name__)
_RECENCY_E_FOLD_SECONDS = 7 * 24 * 60 * 60


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
        verbatim_content: str | None,
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
            verbatim_content=verbatim_content,
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
            return await conn.fetchrow(sql, memory_id, *values, *vis_params)
        sql = (
            f"UPDATE memories SET {set_sql} WHERE id=$1 AND deleted_at IS NULL "
            f"RETURNING {_MEMORY_COLS}"
        )
        return await conn.fetchrow(sql, memory_id, *values)

    async def delete_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
    ) -> Row | None:
        conn = _postgres_tx(tx).conn
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility, start_idx=2,
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
    ) -> list[Row]:
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
        if not boost_recency or len(rows) <= 1:
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
            "SELECT key, value, owner_id, namespace FROM state "
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
    ) -> None:
        await _postgres_tx(tx).conn.execute(
            """
            INSERT INTO state (owner_id, namespace, key, value)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (owner_id, namespace, key) DO UPDATE
            SET value = EXCLUDED.value
            WHERE state.deleted_at IS NULL
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
    ) -> None:
        await _postgres_tx(tx).conn.execute(
            "DELETE FROM state WHERE owner_id = $1 AND namespace = $2 AND key = $3 "
            "AND deleted_at IS NULL",
            owner_id,
            namespace,
            key,
        )


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
